"""Agent 主循环（ReAct 编排）。

``AgentLoop`` 把前面各模块串成一条完整的执行链：

    System Prompt ──▶ 模型 ──▶ 有 tool_calls? ──是──▶ 执行工具 ──▶ 回填结果 ──┐
                        ▲                                                      │
                        └──────────────────── 否（直接回答）◀──────────────────┘

每一轮：
1. 用 ``ContextBuilder`` 组装 messages（含跨轮 history）。
2. 调 ``LLMProvider.chat`` 拿 ``LLMResponse``。
3. 若模型要调用工具：把 assistant（带 tool_calls）和 tool（执行结果）两条消息
   依次回写进 messages，再回到第 2 步；如此往复，直到模型不再要工具，或触发
   上限 / 错误 / 防爆熔断。

两个关键工程点：

A. tool_calls 消息格式（硅基流动兼容）
   assistant 消息里的 ``tool_calls`` 数组，每个元素**只能有** ``id`` / ``type`` /
   ``function`` 三个字段。``reasoning_content`` **绝不能**塞进 tool_calls 元素，
   否则硅基流动重放历史会报 error code 20015。模型若返回了推理内容，统一放到
   assistant 消息的**顶层** ``reasoning_content`` 字段。

B. 工具调用防爆（循环熔断）
   用滑动窗口记录最近的工具调用签名 ``name:args_json``，异常重复即预警/熔断，
   防止模型陷入「调同一个工具 → 得到同样结果 → 再调」的死循环把 token 烧光。
"""

import asyncio
import base64
import json
import logging
import os
import time
from typing import List, Optional

from providers.base import LLMProvider
from agent.tools.registry import ToolRegistry
from agent.context import ContextBuilder
from agent.cache_observability import (
    CacheCallMetric,
    CacheTurnMetric,
    PromptCacheObserver,
    stable_text_hash,
)
from agent.history import canonicalize_history
from session.manager import SessionManager
from agent.memory import MemoryConsolidation
from agent.memory_sync import (
    MemoryChangeLog,
    build_patch_message,
    build_snapshot_message,
    estimate_patch_tokens,
    estimate_text_tokens,
    is_patch_message,
    read_memory_files,
)
from providers.usage import PromptCacheUsage

logger = logging.getLogger("nanoclaw.agent.loop")


class AgentLoop:
    """Agent 对话主循环：串联上下文、模型与工具调度。"""

    # 工具结果预览的最大字符数（超出则只打印前一部分，避免刷屏）
    _PREVIEW_LIMIT = 800

    def __init__(
        self,
        provider: LLMProvider,
        tools: ToolRegistry,
        context: ContextBuilder,
        session_manager: SessionManager,
        session_key: str = "cli:direct",
        model: Optional[str] = None,
        max_iterations: int = 32,
        memory: Optional[MemoryConsolidation] = None,
        turn_timeout: int = 600,
        image_store=None,
        base_model_multimodal: bool = False,
        generated_ids_sink: Optional[list] = None,
        subagent_runs_sink: Optional[list] = None,
        cache_observer: Optional[PromptCacheObserver] = None,
    ):
        self.provider = provider
        self.tools = tools
        self.context = context
        self.session_manager = session_manager
        self.session_key = session_key
        self.model = model
        self.max_iterations = max_iterations
        # 整轮超时（秒）：任何一轮对话的墙钟耗时超过此值即强制终止并回传提示，
        # 防止「工具卡死 / 模型反复重试同一失败工具」导致回合永不结束、卡住会话。
        self.turn_timeout = turn_timeout
        # 会话压缩器：为 None 时不启用压缩（保持向后兼容）
        self.memory = memory
        # 图片存储：基础模型为多模态、需要把历史图片还原成多模态 content 时用
        self.image_store = image_store
        # 基础模型是否自带视觉：true→图片直传；false→抽走图片、用 ask_image 工具
        self.base_model_multimodal = base_model_multimodal
        # Child loops are normally ephemeral, but may contribute image/run
        # replay metadata to the parent assistant tool-call record.
        self.generated_ids_sink = generated_ids_sink
        self.subagent_runs_sink = subagent_runs_sink
        self.cache_observer = cache_observer or PromptCacheObserver()
        self._active_cache_turn = None
        self.last_cache_metrics = None
        # 回合内最后一次模型调用（CacheCallMetric），供 usage_turn 事件的
        # last_usage 使用：进度条「占用」沿用最近一次调用而非回合累计。
        self._last_call_metric: Optional[CacheCallMetric] = None
        # The return value is deliberately user-facing text, so callers such as
        # SpawnSubagentTool need a separate structured lifecycle outcome rather
        # than guessing timeout/error state from localized message strings.
        self.last_run_status = "idle"
        # 当前一轮由 generate_image（含子 Agent）产出的图片 ID。Gateway 在
        # run() 返回后把它们解析为 ImageRef，交给支持图片出站的渠道。
        self.last_generated_image_ids: list[str] = []

        # —— 网页「停止」取消回合时补历史的暂存状态（命名风格与 last_run_status 一致）——
        # 本轮 user_record（_run 里构造、_persist 写盘的那条），取消时补进
        # _session_history / 磁盘，让下一轮「继续」能看到上一轮用户消息。
        self._last_user_record: Optional[dict] = None
        # 上述 user_record 是否已写盘（_run 的 _persist 成功后置 True；取消发生在
        # _persist(user_record) 之前时仍为 False，需由取消分支补写）。
        self._user_record_persisted: bool = False
        # 本轮已生成的部分回答文本（流式 token 累积 / 离散 response.content），
        # 取消时随中断占位 assistant 记录一并保留，供模型下一轮正确接续。
        self._partial_answer: Optional[str] = None
        # 工具执行中取消时 _execute_tools 已落盘的中断 assistant 记录（生图/子 Agent
        # 场景，含 generated_images / subagent_runs 元数据）；取消分支直接复用，
        # 避免再补一条通用占位导致重复。
        self._interrupt_record: Optional[dict] = None
        # 本轮取消补历史是否已完成（幂等：重复取消 / 重复调用不重复追加）。
        self._cancel_history_recorded: bool = False

        # 工具调用签名滑动窗口（防爆用），存 "name:args_json"
        self._tool_call_history: List[str] = []
        # 跨轮次对话历史（不含 system），构造时从持久化层恢复，
        # 这样进程重启后也能接着上次的对话继续。
        self._session_history: List[dict] = session_manager.get_history(session_key)

        # —— 记忆跨会话同步（TASK-004）——
        # 会话创建/重启时 ContextBuilder 刚构建完快照（内容 = 当时文件），
        # 因此会话初始 revision 取当时的全局 revision（覆盖历史持久化值：
        # 重启后快照已含最新内容，若不重置会重复补丁）。
        self._changelog = MemoryChangeLog(self.context.workspace)
        self._memory_revision: int = (
            self.context.memory_revision
            if self.context.memory_revision is not None
            else self._changelog.current_revision()
        )
        try:
            self.session_manager.set_memory_revision(
                self.session_key, self._memory_revision
            )
        except Exception:  # noqa: BLE001 - 元数据写失败不阻断会话创建
            logger.exception(
                "持久化会话 memory_revision 失败，session_key=%s", self.session_key
            )

    @staticmethod
    def _print_thinking(reasoning: str) -> None:
        """打印模型的思考过程，用醒目样式与正常回复区分。

        思考过程是模型的「内心独白」（为什么这么做、打算调什么工具），
        用暗灰色 + 缩进 + 💭 标记，和最终回复（NanoClaw> ...）明显区隔。
        """
        if not reasoning:
            return
        # ANSI: 2=暗淡, 3=斜体；90=灰色。终端不支持时也只是多几个可见字符，无害。
        print("\033[2;3;90m┌─ 💭 思考过程 " + "─" * 30 + "\033[0m")
        for line in reasoning.strip().splitlines():
            print("\033[2;3;90m│ " + line + "\033[0m")
        print("\033[2;3;90m└" + "─" * 44 + "\033[0m")

    @staticmethod
    def _print_tool_call(name: str, args_json: str) -> None:
        """打印模型选择调用的工具及参数，与思考过程、正常回复区分。"""
        print("\033[36m🔧 调用工具 → " + name + "(" + args_json + ")\033[0m")

    @staticmethod
    def _print_tool_result(name: str, result: str) -> None:
        """打印工具执行结果，与思考过程、工具调用明显区分。

        结果过长时只预览前 ``_PREVIEW_LIMIT`` 个字符，避免大段输出冲刷屏幕；
        同时提示总字符数，方便用户判断是否需要进一步查看。
        """
        if len(result) <= AgentLoop._PREVIEW_LIMIT:
            preview = result
            tail = ""
        else:
            preview = result[: AgentLoop._PREVIEW_LIMIT]
            tail = (
                f"\n... (结果过长，仅预览前 {AgentLoop._PREVIEW_LIMIT} 字符，"
                f"实际共 {len(result)} 字符)"
            )
        # ANSI: 32=绿色，对应「执行产出」，与青色(调用)/灰色(思考)区分
        print("\033[32m📤 工具结果 " + name + ":\033[0m")
        for line in (preview.splitlines() or [""]):
            print("\033[32m  " + line + "\033[0m")
        if tail:
            print("\033[2;32m  " + tail + "\033[0m")

    # —— 图片消息装配（多模态直传 vs 占位符，由 base_model_multimodal 决定）——
    @staticmethod
    def _img_id(img) -> Optional[str]:
        """从 ImageRef 或历史元数据 dict 中取出 image_id。"""
        if hasattr(img, "id"):
            return img.id
        if isinstance(img, dict):
            return img.get("id")
        return None

    @staticmethod
    def _img_mime(img) -> str:
        """从 ImageRef 或历史元数据 dict 中取出 mime。"""
        if hasattr(img, "mime"):
            return img.mime
        if isinstance(img, dict):
            return img.get("mime", "image/png")
        return "image/png"

    def _img_path_mime(self, img, session_key):
        """解析出图片落盘路径与 mime（用于多模态直传）。

        当前消息的 ImageRef 自带 path；历史元数据 dict 需经 ImageStore 按
        session_key + id 找回。找不到返回 None。
        """
        if hasattr(img, "path"):
            return img.path, self._img_mime(img)
        if isinstance(img, dict) and self.image_store is not None and session_key:
            ref = self.image_store.resolve(session_key, img.get("id"))
            if ref is not None:
                return ref.path, ref.mime
        return None

    def _user_content(self, text: str, images, base_mm: bool):
        """把一条用户消息（文本 + 可选图片）装配成发给模型的 content。

        - base_mm=True：返回多模态 list（text + 每张图的 image_url）。
        - base_mm=False：返回 str，正文后追加占位符（含 image_id），引导模型
          按需调用 ask_image 工具。
        """
        if base_mm:
            content = [{"type": "text", "text": text}]
            for img in (images or []):
                pm = self._img_path_mime(img, self.session_key)
                if pm and os.path.exists(pm[0]):
                    path, mime = pm
                    try:
                        with open(path, "rb") as f:
                            b64 = base64.b64encode(f.read()).decode("ascii")
                        content.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        })
                    except Exception:  # noqa: BLE001 - 读图失败则该图跳过
                        continue
            return content
        # 纯文本基础模型：抽走图片，正文加占位符
        ids = [self._img_id(r) for r in (images or []) if self._img_id(r)]
        if ids:
            return (
                text
                + "\n[用户附 "
                + str(len(ids))
                + " 张图片，image_id："
                + ", ".join(ids)
                + "。如需理解或回答关于图片内容的问题，请调用 ask_image 工具，"
                "传入对应 image_id 与你的（可含上下文的）问题。]"
            )
        return text

    def _history_item_to_api(self, msg: dict, base_mm: bool) -> dict:
        """把一条历史消息清洗成可发给模型的格式。

        - 带 images 元数据的 user 消息：按 base_mm 还原成多模态 content 或占位符；
        - 其它消息：剥掉可能残留的 images 字段，其余原样返回。
        """
        if msg.get("role") == "user" and msg.get("images"):
            text = msg.get("content") or ""
            new_content = self._user_content(text, msg["images"], base_mm)
            return {"role": "user", "content": new_content}
        # 剥掉渲染专用元数据（OpenAI 不认），避免回传 API 报 400；
        # generated_images / subagent_runs 都是历史回放标记，仅前端用。
        # 它们不能进入模型上下文，否则兼容 OpenAI 的 API 会拒绝未知字段。
        return {
            k: v
            for k, v in msg.items()
            if k not in ("images", "generated_images", "subagent_runs")
        }

    async def run(self, user_message: str, images=None, stream_sink=None) -> str:
        """Run one user turn and always finalize its privacy-safe cache metrics."""
        self._active_cache_turn = None
        try:
            return await self._run(user_message, images=images, stream_sink=stream_sink)
        except asyncio.CancelledError:
            # 网页「停止」：回合被取消时必须向流式 sink 补发 done，让前端把
            # 回合标记为「已停止」并恢复发送按钮；随后原样向上抛出，保证任务
            # 真正取消、锁与资源由上层 finally / async with 正常回收。绝不能
            # 用 except Exception 吞掉 CancelledError（它是 BaseException）。
            self.last_run_status = "cancelled"
            # 先把本轮 user 消息与中断占位 assistant 补进历史（同步、无 await，
            # 先于可能被再次取消打断的 done 发送），这样下一轮「继续」能看到
            # 「上一轮 user + 回答被停止」，模型才能正确接续上下文。
            try:
                self._record_cancelled_turn()
            except Exception:  # noqa: BLE001 - 补历史失败不能影响取消语义本身
                logger.exception("AgentLoop 取消时补历史失败，session_key=%s", self.session_key)
            if stream_sink is not None:
                try:
                    await stream_sink({"type": "done", "content": "⏹ 已停止"})
                except Exception:  # noqa: BLE001 - 停止标记发送失败不影响取消本身
                    pass
            raise
        finally:
            # 回合收尾：无论正常 / 错误 / 取消，都要把缓存观测收敛成回合汇总。
            # 推送 usage_turn 事件前先把 _active_cache_turn 置空（清理先行），
            # 之后即使推送抛异常也不影响回合语义；失败只记日志，绝不吞
            # CancelledError（原取消已由 except 分支 re-raise 继续上抛）。
            if self._active_cache_turn is not None:
                turn = self._active_cache_turn
                self._active_cache_turn = None
                metric = None
                try:
                    metric = turn.finish()
                except Exception:  # noqa: BLE001 - 观测收尾失败不影响回合语义
                    logger.exception(
                        "回合缓存指标收尾失败，session_key=%s", self.session_key
                    )
                self.last_cache_metrics = metric
                if stream_sink is not None and metric is not None:
                    try:
                        await stream_sink(self._build_turn_event(metric))
                    except asyncio.CancelledError:
                        # 取消场景：不吞取消；让原取消/新取消继续向上传播
                        raise
                    except Exception:  # noqa: BLE001 - 汇总推送失败不影响回合
                        logger.exception(
                            "推送 usage_turn 事件失败，session_key=%s",
                            self.session_key,
                        )

    def get_context_usage(self) -> dict:
        """当前会话上下文占用概览（估算 + 上次真实 usage）。

        供各渠道 ``/context`` 命令与 Web 进度条查询，不经过模型：
            - system_tokens / history_tokens / estimate_total：System 段与
              会话历史（不含当前输入）的估算 token（复用 MemoryConsolidation
              的 CJK 启发式，非精确 tokenizer）；
            - last_usage：上次完成回合的真实 usage（``input_tokens`` /
              ``cached`` / ``uncached`` / ``cache_ratio`` / ``availability``），
              尚无真实数据时为 ``None``；
            - budget / ratio：预算与估算占用比（未启用压缩器时均为 ``None``）。
        """
        budget = self.memory.token_budget if self.memory is not None else None
        system_tokens = None
        history_tokens = None
        try:
            system_prompt = self.context.build_system_prompt()
            system_tokens = self._estimate_tokens_for(
                [{"role": "system", "content": system_prompt}]
            )
            history_clean = [
                self._history_item_to_api(m, self.base_model_multimodal)
                for m in self._session_history
            ]
            history_tokens = self._estimate_tokens_for(history_clean)
        except Exception:  # noqa: BLE001 - 估算失败只降级为 None，不阻断查询
            logger.exception("上下文占用估算失败，session_key=%s", self.session_key)

        estimate_total = (
            (system_tokens or 0) + (history_tokens or 0)
            if system_tokens is not None or history_tokens is not None
            else None
        )
        ratio = (estimate_total / budget) if (budget and estimate_total) else None

        last = self.last_cache_metrics
        last_usage = None
        if last is not None:
            last_usage = {
                "input_tokens": last.input_tokens,
                "cached": last.cached_input_tokens,
                "uncached": last.uncached_input_tokens,
                "cache_ratio": last.cache_ratio,
                "availability": last.availability,
                "calls": last.calls,
            }

        return {
            "budget": budget,
            "system_tokens": system_tokens,
            "history_tokens": history_tokens,
            "estimate_total": estimate_total,
            "ratio": ratio,
            "last_usage": last_usage,
        }

    @staticmethod
    def _estimate_tokens_for(messages: list) -> int:
        """按 MemoryConsolidation 的启发式估算消息 token（不污染其 last_estimate）。"""
        total = 0
        for msg in messages:
            total += 4  # 每条消息的结构开销
            total += MemoryConsolidation._estimate_value(msg.get("content"))
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {}) or {}
                total += MemoryConsolidation._estimate_value(fn.get("name", ""))
                total += MemoryConsolidation._estimate_value(
                    fn.get("arguments", "") or ""
                )
        return total

    async def _emit_usage_event(self, stream_sink, usage, messages, tools) -> None:
        """每次模型响应后向流式 sink 推送 usage 事件；失败只记日志不影响回合。"""
        try:
            event = self._build_usage_event(usage, messages, tools)
            if event is not None:
                await stream_sink(event)
        except Exception:  # noqa: BLE001 - 观测事件失败不能影响对话主流程
            logger.exception("推送 usage 事件失败，session_key=%s", self.session_key)

    def _build_usage_event(self, usage, messages, tools) -> Optional[dict]:
        """构造 ``{type: "usage", ...}`` 事件；真实 usage 缺失时回退估算。"""
        budget = self.memory.token_budget if self.memory is not None else None
        event: dict = {"type": "usage", "budget": budget}
        if usage is not None and usage.input_tokens is not None:
            event.update({
                "input_tokens": usage.input_tokens,
                "cached": usage.cached_input_tokens,
                "uncached": usage.uncached_input_tokens,
                "cache_ratio": usage.cache_ratio,
            })
            event["ratio"] = (usage.input_tokens / budget) if budget else None
            event["estimate"] = None
        else:
            estimate = self._estimate_tokens_for(messages)
            if tools:
                estimate += MemoryConsolidation._estimate_value(tools)
            event["estimate"] = estimate
            event["ratio"] = (estimate / budget) if budget else None
        return event

    def _build_turn_event(self, metric: CacheTurnMetric) -> dict:
        """构造 ``{type: "usage_turn", ...}`` 回合汇总事件（Web 进度条整轮概览）。

        字段：
        - 整轮汇总：turn / calls / input_tokens / cached / uncached /
          cache_ratio / availability（供应商缺报时 input_tokens / cached 可能为
          None，前端据此优雅降级）；
        - 占用语义：进度条「占用」沿用**最近一次调用**的 input/预算
          （last_usage / last_ratio），而不是回合累计——多轮调用累计可能超过
          预算，而进度条要反映的是当前上下文实际占用，而非整轮吞吐。
        """
        budget = self.memory.token_budget if self.memory is not None else None
        last_usage = None
        last_ratio = None
        last = self._last_call_metric
        if last is not None and last.input_tokens is not None:
            last_usage = {
                "input_tokens": last.input_tokens,
                "cached": last.cached_input_tokens,
                "uncached": last.uncached_input_tokens,
                "cache_ratio": last.cache_ratio,
                "availability": last.availability,
            }
            if budget:
                last_ratio = last.input_tokens / budget
        return {
            "type": "usage_turn",
            "turn": metric.turn,
            "calls": metric.calls,
            "input_tokens": metric.input_tokens,
            "cached": metric.cached_input_tokens,
            "uncached": metric.uncached_input_tokens,
            "cache_ratio": metric.cache_ratio,
            "availability": metric.availability,
            "budget": budget,
            "last_usage": last_usage,
            "last_ratio": last_ratio,
            "ratio": last_ratio,  # 占用沿用最近一次调用，而非回合累计
            "estimate": None,
        }

    def _record_cancelled_turn(self) -> None:
        """网页「停止」取消回合后，把本轮 user 消息与中断占位 assistant 补进历史。

        正常回合在结束时用 ``_persist(final_msg) + _save_to_history(messages)``
        同时更新磁盘与内存 ``_session_history``；取消回合不会走到那里，导致下一轮
        ``_run`` 用 ``_session_history`` 构建 messages 时缺上一轮 user 与 assistant
        回复，模型看到连续两条 user 消息、不知道「继续」接什么。本方法把：

            [上一条 user, 中断占位 assistant（含已生成的部分回答）]

        以与 ``canonicalize_history`` 兼容的格式补进 ``_session_history`` 并写盘
        （user_record 若已由 ``_run`` 写盘则跳过，幂等），让下一轮模型能看到
        「上一轮回答被停止」从而正确接续。

        幂等保证：
        - ``_cancel_history_recorded`` 守卫，整轮只补一次；
        - user_record 磁盘已写则不再写（``_user_record_persisted``）；
        - 工具执行中取消时 ``_execute_tools`` 已落盘的中断记录直接复用
          （``self._interrupt_record``），不重复生成通用占位；
        - 取消发生在 user_record 构造之前（如会话压缩阶段）时磁盘上没有任何
          本轮记录，直接返回，避免产生孤立的占位 assistant。
        """
        if self._cancel_history_recorded:
            return
        self._cancel_history_recorded = True
        if self._last_user_record is None:
            return

        records_to_append: List[dict] = []

        # 1) 用户消息：_run 的 _persist 已写盘则跳过，否则补写（幂等）
        if not self._user_record_persisted:
            self._persist(self._last_user_record)
            self._user_record_persisted = True
        records_to_append.append(self._last_user_record)

        # 2) 中断占位 assistant：工具取消已落盘则复用，否则生成通用占位
        #    （若已有部分回答文本则带上，让模型知道上一轮生成到哪一步）。
        if self._interrupt_record is not None:
            interrupt_record = self._interrupt_record
        else:
            partial = self._partial_answer or ""
            if partial:
                content = f"{partial}\n\n（⏹ 回答被用户停止）"
            else:
                content = "（上一轮回答被用户手动停止）"
            interrupt_record = {"role": "assistant", "content": content}
            self._persist(interrupt_record)
        records_to_append.append(interrupt_record)

        # 统一走 canonicalize_history：保证与磁盘格式、_history_item_to_api 兼容，
        # 且重复调用（_cancel_history_recorded 已拦）也不会产生重复/脏数据。
        self._session_history = canonicalize_history(
            [*self._session_history, *records_to_append]
        )

    def _sync_memory_patch(self, messages: list) -> None:
        """每轮对话前：跨会话记忆补丁同步（TASK-004）。

        全局 revision > 会话 revision 时，从 changelog 取落后期间的变更 →
        生成 <memory_patch> system 消息 → 插入历史之后、本轮 user 之前 →
        持久化进会话 JSONL → 更新 session.memory_revision。
        累积补丁过多（>20 条 / >1000 tokens / 大量删除 / 历史压缩联动）时，
        改为重建完整记忆快照并替换历史里的旧补丁。
        任何失败静默降级：只记日志，绝不阻塞/挂掉本轮对话。
        """
        try:
            global_rev = self._changelog.current_revision()
        except Exception:  # noqa: BLE001
            logger.exception("读取记忆变更日志失败，本轮跳过记忆同步")
            return
        if global_rev <= self._memory_revision:
            return  # 零注入：无变化时上下文与现在完全一致
        try:
            entries = self._changelog.entries_after(self._memory_revision)
        except Exception:  # noqa: BLE001
            logger.exception("读取落后期间记忆变更失败，静默推进基线")
            self._advance_memory_revision(global_rev)
            return
        if not entries:
            # changelog 有 revision 但落后区间无条目（理论不发生）：静默推进
            self._advance_memory_revision(global_rev)
            return
        consolidated = bool(
            self.memory is not None
            and self.memory.last_consolidation.get("consolidated", False)
        )
        if self._should_rebuild_memory(entries, consolidated):
            self._rebuild_memory_snapshot(messages, global_rev)
        else:
            self._apply_memory_patch(messages, entries)

    def _advance_memory_revision(self, revision: int) -> None:
        """推进会话 revision（内存 + 持久化），供补丁应用/重建快照/降级共用。"""
        self._memory_revision = revision
        try:
            self.session_manager.set_memory_revision(self.session_key, revision)
        except Exception:  # noqa: BLE001
            logger.exception(
                "持久化会话 memory_revision 失败，session_key=%s", self.session_key
            )

    def _should_rebuild_memory(self, entries: list, consolidated: bool) -> bool:
        """判断是发补丁还是重建完整快照。

        触发重建条件（任务卡 §5）：
        - 累积补丁（历史中已有 + 本次新增）超过 20 条
        - 补丁总量估算超过约 1000 tokens
        - 本次变更发生大量删除（removed_lines >= 10）
        - 会话历史压缩联动（本轮刚压缩过，旧补丁可能被摘要吞掉）
        """
        existing = [m for m in self._session_history if is_patch_message(m)]
        patch_count = len(existing) + 1
        patch_tokens = sum(
            estimate_text_tokens(m.get("content") or "") for m in existing
        ) + estimate_patch_tokens(entries)
        removed_total = sum(
            len(e.get("removed_lines") or []) for e in entries
        )
        return bool(
            consolidated
            or patch_count > 20
            or patch_tokens > 1000
            or removed_total >= 10
        )

    def _apply_memory_patch(self, messages: list, entries: list) -> None:
        """生成补丁 → 插入 messages（历史之后、user 之前）→ 持久化 → 推进基线。"""
        try:
            patch_msg = build_patch_message(entries)
        except Exception:  # noqa: BLE001 - 补丁生成失败静默降级
            logger.exception("生成记忆补丁失败，跳过本轮补丁")
            self._advance_memory_revision(entries[-1]["revision"])
            return
        latest = entries[-1]["revision"]
        # messages = [system] + 历史 + [本轮 user]；补丁插在历史之后、user 之前
        if len(messages) >= 2 and messages[-1].get("role") == "user":
            messages[-1:-1] = [patch_msg]
        # 补丁持久化进会话历史（不持久化则下一轮只剩旧快照，等于忘记更新）
        self._session_history = canonicalize_history(
            [*self._session_history, patch_msg]
        )
        try:
            self.session_manager.save_message(self.session_key, patch_msg)
        except Exception:  # noqa: BLE001 - 持久化失败不阻断本轮
            logger.exception("持久化记忆补丁到会话 JSONL 失败，session_key=%s", self.session_key)
        self._advance_memory_revision(latest)

    def _rebuild_memory_snapshot(self, messages: list, global_rev: int) -> None:
        """重建完整记忆快照：旧快照 + 所有补丁的等价物，替换历史里的旧补丁。

        一次破缓存后，新快照继续稳定。重建失败回退为补丁模式。
        """
        try:
            user_text, memory_text = read_memory_files(self.context.workspace)
            snapshot_msg = build_snapshot_message(user_text, memory_text, global_rev)
        except Exception:  # noqa: BLE001 - 重建失败回退补丁模式
            logger.exception("重建记忆快照失败，回退为补丁模式")
            try:
                entries = self._changelog.entries_after(self._memory_revision)
            except Exception:  # noqa: BLE001
                self._advance_memory_revision(global_rev)
                return
            if entries:
                self._apply_memory_patch(messages, entries)
            else:
                self._advance_memory_revision(global_rev)
            return
        history = canonicalize_history(
            [m for m in self._session_history if not is_patch_message(m)]
            + [snapshot_msg]
        )
        self._session_history = history
        try:
            self.session_manager.save_messages(self.session_key, history)
        except Exception:  # noqa: BLE001 - 重建后落盘失败不阻断本轮
            logger.exception("重建快照后持久化会话历史失败，session_key=%s", self.session_key)
        self._advance_memory_revision(global_rev)
        # 用新历史重建本轮 messages（保留首条 system 与末条本轮 user）
        if len(messages) >= 2 and messages[-1].get("role") == "user":
            base_mm = self.base_model_multimodal
            history_clean = [self._history_item_to_api(m, base_mm) for m in history]
            messages[:] = [messages[0], *history_clean, messages[-1]]

    async def _run(self, user_message: str, images=None, stream_sink=None) -> str:
        """处理一轮用户消息，返回模型最终文本回复。

        参数：
            user_message: 本轮用户输入。
            images: 可选，随消息附带的图片引用列表（``List[ImageRef]``）。为 None
                表示纯文本消息。
            stream_sink: 可选，一个 ``async def sink(event: dict)`` 回调。当提供时，
                Agent 在推理/执行过程中的「逐步事件」（思考、工具调用、工具结果、
                逐字生成的最终回答）会通过它实时推送，供支持流式展示的渠道
                （如网页）渐进式渲染。不提供则保持原有的纯终端打印行为。

        事件约定见 ``bus.queue.StreamEvent`` 的文档。
        """
        self.last_run_status = "running"
        self.last_generated_image_ids = []
        # 重置取消补历史的本轮暂存（上一轮取消/异常残留不允许泄漏进本轮）
        self._last_user_record = None
        self._user_record_persisted = False
        self._partial_answer = None
        self._interrupt_record = None
        self._cancel_history_recorded = False
        # 回合内最后一次模型调用观测，回合结束由 _build_turn_event 读取
        self._last_call_metric = None
        base_mm = self.base_model_multimodal

        # 1. 构建初始 messages（注入跨轮历史 + 当前输入）
        #    当前消息与历史消息都按 base_mm 分支装配：
        #    - 多模态基础模型：图片以多模态 content 直传；
        #    - 纯文本基础模型：抽走图片、正文追加占位符（含 image_id），
        #      由模型按需调用 ask_image 工具。
        current_content = self._user_content(user_message, images, base_mm)
        history_clean = [self._history_item_to_api(m, base_mm) for m in self._session_history]
        # Freeze one logical tool snapshot for every model call in this user turn.
        # The top-level registry is frozen after MCP startup; ephemeral child
        # registries still get a deterministic, name-sorted per-turn snapshot.
        tool_definitions = self.tools.get_definitions()
        messages = self.context.build_messages(
            history=history_clean,
            current_message=current_content,
        )
        self._active_cache_turn = self.cache_observer.start_turn(
            system_hash=stable_text_hash(str(messages[0].get("content") or "")),
            tools_hash=self.tools.schema_hash,
            history_messages=len(history_clean),
        )
        # 1.5 会话压缩：当估算 token 超预算时，把中间旧消息压成一条摘要。
        #     MemoryConsolidation 仅在 messages 超出 token_budget 才触发压缩，
        #     预算内原样返回；memory 为 None 表示未启用（保持向后兼容）。
        if self.memory is not None:
            compressed = await self.memory.maybe_consolidate(
                messages, tools=tool_definitions, cache_turn=self._active_cache_turn
            )
            if self.memory.last_consolidation.get("consolidated", False):
                new_history = canonicalize_history(compressed[1:-1])
                self._session_history = new_history
                self.session_manager.save_messages(self.session_key, new_history)
                boundary = self.memory.last_consolidation
                print(
                    f"\033[2;35m🗜️  会话历史已压缩：{len(messages)} 条 → "
                    f"{len(compressed)} 条；估算 {boundary.get('estimated_tokens')} / "
                    f"预算 {boundary.get('token_budget')} token\033[0m"
                )
            messages = compressed
        # 1.6 记忆跨会话同步（TASK-004）：全局 revision 落后时插入
        #     <memory_patch> system 消息（历史之后、本轮 user 之前），或
        #     累积过多时重建完整记忆快照。无变化时零注入（前缀缓存命中）。
        #     失败静默降级，绝不阻塞对话。
        self._sync_memory_patch(messages)
        # Exclude the system and current user message.  If consolidation ran,
        # this is the actual history count sent to the main ReAct provider.
        self._active_cache_turn.set_main_history_messages(max(0, len(messages) - 2))
        # 持久化当前用户消息（原文 + 图片元数据；图片字节由 ImageStore 落盘，
        # 此处仅记 id/mime，发送时再按 base_mm 分支还原成多模态 content 或占位符）
        user_record = {"role": "user", "content": user_message}
        if images:
            user_record["images"] = [
                {"id": self._img_id(r), "mime": self._img_mime(r)} for r in images
            ]
        # 提升到 self：取消（停止）时 run() 的取消分支据此补历史。user_record
        # 构造与 _persist 之间无 await，取消只会发生在 _persist 成功之后；
        # _user_record_persisted 用于「取消发生在写盘之前」边界的幂等保护。
        self._last_user_record = user_record
        self._user_record_persisted = False
        self._persist(user_record)
        self._user_record_persisted = True

        # 是否走流式路径：仅当挂载了 sink 且 Provider 支持 chat_stream 时。
        # 否则走原有的 chat() 离散路径（有 sink 时也 emit 离散事件，保证网页可见）。
        use_stream = stream_sink is not None and hasattr(self.provider, "chat_stream")

        # 2. 主循环：模型调用工具直到给出最终回答，或触达上限/错误/熔断
        turn_start = time.monotonic()
        for tool_iteration in range(self.max_iterations):
            # 整轮超时保护：墙钟耗时超限即终止并回传提示，避免「工具卡死 /
            # 模型反复重试同一失败工具」导致回合永不结束、卡住整个会话。
            if time.monotonic() - turn_start > self.turn_timeout:
                msg = (
                    f"本轮处理已超过时间上限（{self.turn_timeout}秒），已自动终止。"
                    f"若任务确实需要更久，可在配置中调大 turn_timeout_sec。"
                )
                if stream_sink is not None:
                    await stream_sink({"type": "done", "content": msg})
                self.last_run_status = "timed_out"
                return msg

            if use_stream:
                # 关键修复：流式调用必须用 wait_for 包一层超时——否则一旦模型
                # 在「已建连但不吐数据」状态下挂起（兼容实现 / 网络抖动极常见），
                # async for chunk 会永远阻塞，整轮卡死、网页端永久「思考中」。
                # 离散路径的 chat() 已自带 wait_for，这里补齐对称性。
                try:
                    response, final_content = await asyncio.wait_for(
                        self._run_streamed(messages, tool_definitions, stream_sink),
                        timeout=self.turn_timeout,
                    )
                except asyncio.TimeoutError:
                    msg = "模型响应超时，已终止本轮。"
                    if stream_sink is not None:
                        await stream_sink({"type": "done", "content": msg})
                    self.last_run_status = "timed_out"
                    return msg
            else:
                try:
                    response = await asyncio.wait_for(
                        self.provider.chat(
                            messages, tool_definitions, self.model
                        ),
                        timeout=self.turn_timeout,
                    )
                except asyncio.TimeoutError:
                    msg = "模型响应超时，已终止本轮。"
                    if stream_sink is not None:
                        await stream_sink({"type": "done", "content": msg})
                    self.last_run_status = "timed_out"
                    return msg
                # 离散路径：把整轮响应作为一次性事件推给 sink（无 sink 则返回正文）。
                # 先记录部分回答文本（含工具轮的前置正文），供取消分支补历史；
                # 取消若发生在 _emit_discrete 的 sink await 期间也不丢。
                self._partial_answer = getattr(response, "content", None) or ""
                final_content = await self._emit_discrete(response, stream_sink)

            call_metric = self._active_cache_turn.record(
                getattr(response, "cache_usage", PromptCacheUsage()),
                tool_iteration=tool_iteration,
            )
            # 记录回合内最后一次模型调用（供 usage_turn 事件的 last_usage：
            # 进度条「占用」沿用最近一次调用而非累计）。
            self._last_call_metric = call_metric

            # 每次模型响应后推送 usage 事件（Web 进度条 / 缓存命中率实时更新）。
            # 真实 usage 缺失时回退到估算；推送失败只记日志，不影响回合。
            if stream_sink is not None:
                await self._emit_usage_event(
                    stream_sink,
                    getattr(response, "cache_usage", PromptCacheUsage()),
                    messages,
                    tool_definitions,
                )

            # b. 模型侧异常：直接退出返回错误信息。流式与离散路径都要补 done，
            # 否则网页端收不到「收尾」事件会一直停在「思考中」。
            if response.finish_reason == "error":
                reply = final_content or "未知错误"
                if stream_sink is not None:
                    await stream_sink({"type": "done", "content": reply})
                self.last_run_status = "error"
                return reply

            # c. 模型请求调用工具
            if response.has_tool_calls:
                # 离散路径：打印本轮思考（流式路径已在 _run_streamed 内打印）
                if not use_stream:
                    reasoning = (
                        response.tool_calls[0].reasoning_content
                        or response.reasoning_content
                    )
                    self._print_thinking(reasoning or "")
                    if response.content:
                        print("\033[2;3;90m  " + response.content.strip() + "\033[0m")

                # —— 构造 assistant 消息（严格 OpenAI 格式）——
                assistant_msg = {
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                            },
                        }
                        for tc in response.tool_calls
                    ],
                }
                if response.tool_calls[0].reasoning_content:
                    assistant_msg["reasoning_content"] = response.tool_calls[0].reasoning_content
                messages.append(assistant_msg)
                # 持久化挪到执行工具之后（generate_image 需要把生成的 image_id 写回
                # assistant_msg 元数据，供历史回放渲染图片）；见 _execute_tools 末尾。
                stop = await self._execute_tools(response, messages, stream_sink, assistant_msg)
                if stop is not None:
                    # 流式路径必须补 done，否则网页端卡在「思考中」
                    if stream_sink is not None:
                        await stream_sink({"type": "done", "content": stop})
                    self.last_run_status = "error"
                    return stop
                continue

            # d. 模型不再调用工具 → 给出最终回答
            if not use_stream:
                # 离散路径：打印最终回复前的思考过程
                self._print_thinking(response.reasoning_content or "")

            # 收尾事件：通知网页端本轮完成（携带最终完整回答）
            if stream_sink is not None:
                await stream_sink({"type": "done", "content": final_content})

            final_msg = {"role": "assistant", "content": final_content}
            messages.append(final_msg)
            self._persist(final_msg)
            self._save_to_history(messages)
            self.last_run_status = "completed"
            return final_content

        # 3. 跑满 max_iterations 仍未结束 → 超时
        timeout_msg = (
            f"已超过最大迭代次数（{self.max_iterations}），"
            f"未能在限定步数内完成任务。"
        )
        if stream_sink is not None:
            await stream_sink({"type": "done", "content": timeout_msg})
        self.last_run_status = "timed_out"
        return timeout_msg

    async def _run_streamed(
        self, messages: list, tool_definitions: list[dict], stream_sink
    ) -> tuple:
        """流式路径：逐字消费 Provider.chat_stream，把增量实时推给 sink。

        返回 ``(LLMResponse, final_content)``。仅转发 ``reasoning`` / ``token``
        事件（供网页渐进式渲染），``done`` 由 ``run`` 在结尾统一补发以避免重复。
        终端侧沿用原有打印风格，保持服务端日志可用。
        """
        content_buf: list = []
        call_start = time.monotonic()
        try:
            async for ev in self.provider.chat_stream(
                messages, tool_definitions, self.model
            ):
                # 单次流式模型调用也受整轮超时约束，避免模型连接挂死拖垮整轮
                if time.monotonic() - call_start > self.turn_timeout:
                    response = type(
                        "R", (), {"finish_reason": "error", "reasoning_content": None,
                                  "content": "模型响应超时，已终止本轮。", "has_tool_calls": False}
                    )()
                    return response, "模型响应超时，已终止本轮。"
                etype = ev.get("type")
                if etype == "reasoning":
                    await stream_sink({"type": "thinking", "content": ev["content"]})
                elif etype == "token":
                    await stream_sink({"type": "token", "content": ev["content"]})
                    content_buf.append(ev["content"])
                elif etype == "done":
                    response = ev["response"]
                    break
            else:
                # 正常不会走到（chat_stream 必 yield 一个 done），兜底为 error
                response = type(
                    "R", (), {"finish_reason": "error", "reasoning_content": None,
                              "content": "", "has_tool_calls": False}
                )()
            final_content = "".join(content_buf)

            # 终端打印本轮推理/正文（与离散路径日志风格一致）
            if getattr(response, "reasoning_content", None):
                self._print_thinking(response.reasoning_content)
            if getattr(response, "content", None):
                print("\033[2;3;90m  " + response.content.strip() + "\033[0m")
            return response, final_content
        finally:
            # 取消 / 超时 / 异常时也要把已生成的文本留在 self（try/finally 保证
            # 任何退出路径都执行），供 run() 的取消分支补历史使用。
            self._partial_answer = "".join(content_buf)

    async def _emit_discrete(self, response, stream_sink) -> str:
        """离散路径（非流式）：把整轮响应作为一次性事件推给 sink。

        无 sink 时只返回正文（保持原行为）；有 sink 时推送 thinking + 单个
        token（整段正文），供网页一次性渲染。
        """
        if stream_sink is None:
            return response.content or ""
        reasoning = (
            response.tool_calls[0].reasoning_content or response.reasoning_content
            if response.has_tool_calls else response.reasoning_content
        )
        if reasoning:
            await stream_sink({"type": "thinking", "content": reasoning})
        if response.content:
            await stream_sink({"type": "token", "content": response.content})
        return response.content or ""

    async def _execute_tools(self, response, messages: list, stream_sink,
                              assistant_msg=None) -> Optional[str]:
        """执行本轮所有工具调用，并把过程推给 sink（若提供）。

        返回：
            - ``None``：正常执行完毕，调用方继续下一轮（把结果喂回模型）。
            - 字符串：已终止本轮（熔断），该字符串即为返回给用户的终止说明。

        参数：
            assistant_msg: 本轮的 assistant(tool_calls) 消息。持久化由本方法负责
                （而非调用方提前持久化），以便把 generate_image 生成的 image_id
                写回元数据，供历史回放渲染图片。
        """
        # 收集本轮 generate_image 生成的 image_id 与子 Agent 运行摘要，执行完后
        # 写回 assistant_msg 元数据，供网页历史回放。两者均不得进入模型上下文。
        gen_ids: list = (
            self.generated_ids_sink if isinstance(self.generated_ids_sink, list) else []
        )
        subagent_runs: list = (
            self.subagent_runs_sink
            if isinstance(self.subagent_runs_sink, list)
            else []
        )
        # 工具结果先只进入本轮内存消息；整组执行结束后再按
        # assistant(tool_calls) → tool 的协议顺序一次性追加到会话文件。
        # 旧实现边执行边持久化 tool，最后才写 assistant，导致重启恢复时报 400。
        tool_records: dict[str, dict] = {}

        for tc in response.tool_calls:
            sig_args = json.dumps(tc.arguments, ensure_ascii=False)
            verdict = self._check_tool_loop(tc.name, sig_args)

            # 熔断：强制终止本轮（先补齐所有 tool 消息，保证落盘自洽）
            if verdict is not None and "熔断" in verdict:
                for t in response.tool_calls:
                    if t.id in tool_records:
                        continue
                    close_msg = {
                        "role": "tool",
                        "tool_call_id": t.id,
                        "content": "（会话因工具调用熔断被强制终止，工具结果缺失）",
                    }
                    messages.append(close_msg)
                    tool_records[t.id] = close_msg
                self._persist_tool_exchange(
                    assistant_msg,
                    response.tool_calls,
                    tool_records,
                    gen_ids,
                    subagent_runs,
                )
                self._save_to_history(messages)
                return verdict

            # 警告：回填 SYSTEM_ERROR 给模型，跳过本次执行
            if verdict is not None:
                warn_msg = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": verdict,
                }
                messages.append(warn_msg)
                tool_records[tc.id] = warn_msg
                continue

            # 防爆通过：真正执行工具并回填结果
            self._print_tool_call(tc.name, sig_args)
            if stream_sink is not None:
                await stream_sink({"type": "tool_call", "name": tc.name, "args": sig_args})
            # ask_image / generate_image / create_video / query_video 都是跨会话
            # 共享单例，需注入当前 session_key 才能按会话定位图片/视频落盘路径；
            # 其它工具忽略该参数。
            exec_args = dict(tc.arguments)
            if tc.name in (
                "ask_image", "generate_image", "create_video", "query_video",
                "write_file",
            ):
                # write_file 需要 session_key 以便写记忆文件成功后刷新会话基线
                # （自写刷基线，属内部机制，不进模型可见 schema）
                exec_args["session_key"] = self.session_key
            # generate_image 还需挂载 stream_sink（实时把图片推给网页端内联显示）
            # 与一个收集列表（把生成的 image_id 回写给主循环，用于历史回放持久化）；
            # 非网页渠道 stream_sink 为 None，工具退化为"仅落盘 + 文本结果"。
            if tc.name == "generate_image":
                exec_args["stream_sink"] = stream_sink
                exec_args["_generated_ids"] = gen_ids
            elif tc.name == "spawn_subagent":
                # 子 Agent 继承的是父会话归属，而不是临时 child session；这样其
                # 生成的图片可由父会话的历史 API 稳定找回。stream sink 会在工具
                # 内转换为隔离的 subagent_event，绝不会发送 child 的顶层 done。
                exec_args["_parent_stream_sink"] = stream_sink
                exec_args["_parent_session_key"] = self.session_key
                exec_args["_parent_generated_ids"] = gen_ids
                exec_args["_parent_subagent_runs"] = subagent_runs
                exec_args["_parent_tool_call_id"] = tc.id
            # 记录工具执行耗时（毫秒），随 tool_result 事件推给网页端展示
            t_start = time.monotonic()
            try:
                result = await self.tools.execute(tc.name, exec_args)
            except asyncio.CancelledError:
                self._remember_generated_ids(gen_ids)
                # A child run may already have emitted useful terminal metadata
                # before cancellation reaches this parent loop. Persist a valid,
                # standalone assistant record so historical Web sessions can
                # replay that run without storing an unmatched tool_calls item.
                if gen_ids or subagent_runs:
                    interrupted_record = {
                        "role": "assistant",
                        "content": "（本轮工具执行已取消）",
                    }
                    if gen_ids:
                        interrupted_record["generated_images"] = list(gen_ids)
                    if subagent_runs:
                        interrupted_record["subagent_runs"] = list(subagent_runs)
                    self._persist(interrupted_record)
                    # 交给 run() 的取消分支复用（已落盘，直接进 _session_history），
                    # 避免再补一条通用占位导致同一轮出现两条中断记录。
                    self._interrupt_record = interrupted_record
                raise
            self._remember_generated_ids(gen_ids)
            # 自写刷基线（TASK-004）：本会话写完文件后，若目标是记忆文件，
            # 系统已递增全局 revision；立刻推进本会话 revision，防止下一轮
            # 给自己发「自己刚写的」补丁（死机制不靠模型自觉）。
            if tc.name == "write_file":
                self._memory_revision = self._changelog.current_revision()
                try:
                    self.session_manager.set_memory_revision(
                        self.session_key, self._memory_revision
                    )
                except Exception:  # noqa: BLE001 - 基线持久化失败不影响本轮
                    logger.exception(
                        "持久化自写基线失败，session_key=%s", self.session_key
                    )
            duration_ms = int((time.monotonic() - t_start) * 1000)
            self._print_tool_result(tc.name, result)
            if stream_sink is not None:
                await stream_sink({
                    "type": "tool_result",
                    "name": tc.name,
                    "content": result,
                    "duration_ms": duration_ms,
                })
            tool_msg = {
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            }
            messages.append(tool_msg)
            tool_records[tc.id] = tool_msg

        # 正常执行完毕：按 OpenAI 协议顺序落盘整组交换。展示元数据只附加在
        # assistant 的持久化副本上，内存 messages 保持 API 兼容格式。
        self._persist_tool_exchange(
            assistant_msg,
            response.tool_calls,
            tool_records,
            gen_ids,
            subagent_runs,
        )
        return None

    def _persist_tool_exchange(
        self,
        assistant_msg: Optional[dict],
        tool_calls: list,
        tool_records: dict[str, dict],
        generated_ids: list,
        subagent_runs: list,
    ) -> None:
        """以 ``assistant(tool_calls) → tool...`` 顺序持久化一次工具交换。"""
        if assistant_msg is None:
            # 没有前置 assistant 时绝不能单独保存 tool；这只可能出现在外部
            # 直接调用私有方法的兼容场景，正常 run() 始终会传 assistant_msg。
            return
        record = dict(assistant_msg)
        if generated_ids:
            record["generated_images"] = list(generated_ids)
        if subagent_runs:
            record["subagent_runs"] = list(subagent_runs)
        self._persist(record)
        for tool_call in tool_calls:
            tool_record = tool_records.get(tool_call.id)
            if tool_record is not None:
                self._persist(tool_record)

    def _remember_generated_ids(self, image_ids: list) -> None:
        """把本轮新增图片汇总到稳定、去重的出站列表。"""
        for image_id in image_ids:
            if image_id and image_id not in self.last_generated_image_ids:
                self.last_generated_image_ids.append(image_id)

    def _check_tool_loop(self, tool_name: str, tool_args_json: str) -> Optional[str]:
        """工具调用防爆检测。

        返回：
            - ``None``：放行（并把签名记入窗口）。
            - 字符串：预警或熔断信息。调用方据此决定 skip 执行或终止本轮。

        逻辑：统计同一签名在滑动窗口中的出现次数，过多即判定为潜在死循环。
        """
        sig = f"{tool_name}:{tool_args_json}"
        count = self._tool_call_history.count(sig)

        if count >= 20:
            return (
                f"工具调用熔断：'{tool_name}' 被重复调用次数过多（>=20），"
                f"疑似陷入死循环，已强制终止本次会话回合。"
            )
        if count >= 10:
            return (
                f"系统提示：工具 '{tool_name}' 已重复调用较多次数（>=10），"
                f"请尝试换一种方式或检查参数，避免陷入循环。"
            )

        # 放行：记入窗口，超长则丢弃最旧的一条（保持最近 30 次）
        self._tool_call_history.append(sig)
        if len(self._tool_call_history) > 30:
            self._tool_call_history.pop(0)
        return None

    def _persist(self, msg: dict) -> None:
        """把一条消息同步持久化到会话文件（供跨进程恢复）。

        仅持久化「用户消息 / assistant 回复 / tool 结果」这三类对话消息；
        system 提示由 ContextBuilder 每次重建，不入库。
        """
        self.session_manager.save_message(self.session_key, msg)

    def _save_to_history(self, messages_snapshot: List[dict]) -> None:
        """保存本轮新增对话到跨轮历史（去掉首条 system 提示）。"""
        # messages = [system] + 历史 + 本轮新增；去掉 system 后即干净的对话流
        self._session_history = canonicalize_history(messages_snapshot[1:])

    def clear_history(self) -> None:
        """清空历史并显式刷新慢变上下文，开始新的 cache boundary。"""
        self._tool_call_history.clear()
        self._session_history.clear()
        refresh = getattr(self.context, "refresh_context", None)
        if callable(refresh):
            refresh()
        # 同步清空磁盘上的 JSONL，避免下次启动又把旧历史读回来
        self.session_manager.clear(self.session_key)
        # 快照已刷新为最新内容 → 会话 revision 同步到当前全局 revision，
        # 避免把「快照里已含的最新内容」当待同步变更再发一次补丁
        self._memory_revision = self._changelog.current_revision()
        try:
            self.session_manager.set_memory_revision(
                self.session_key, self._memory_revision
            )
        except Exception:  # noqa: BLE001 - 元数据写失败不阻断清空
            logger.exception(
                "持久化清空后 memory_revision 失败，session_key=%s", self.session_key
            )
