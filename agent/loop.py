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
import os
import time
from typing import List, Optional

from providers.base import LLMProvider
from agent.tools.registry import ToolRegistry
from agent.context import ContextBuilder
from agent.cache_observability import PromptCacheObserver, stable_text_hash
from agent.history import canonicalize_history
from session.manager import SessionManager
from agent.memory import MemoryConsolidation
from providers.usage import PromptCacheUsage


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
        # The return value is deliberately user-facing text, so callers such as
        # SpawnSubagentTool need a separate structured lifecycle outcome rather
        # than guessing timeout/error state from localized message strings.
        self.last_run_status = "idle"
        # 当前一轮由 generate_image（含子 Agent）产出的图片 ID。Gateway 在
        # run() 返回后把它们解析为 ImageRef，交给支持图片出站的渠道。
        self.last_generated_image_ids: list[str] = []

        # 工具调用签名滑动窗口（防爆用），存 "name:args_json"
        self._tool_call_history: List[str] = []
        # 跨轮次对话历史（不含 system），构造时从持久化层恢复，
        # 这样进程重启后也能接着上次的对话继续。
        self._session_history: List[dict] = session_manager.get_history(session_key)

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
        finally:
            if self._active_cache_turn is not None:
                self.last_cache_metrics = self._active_cache_turn.finish()
                self._active_cache_turn = None

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
        self._persist(user_record)

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
                # 离散路径：把整轮响应作为一次性事件推给 sink（无 sink 则返回正文）
                final_content = await self._emit_discrete(response, stream_sink)

            self._active_cache_turn.record(
                getattr(response, "cache_usage", PromptCacheUsage()),
                tool_iteration=tool_iteration,
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
            if tc.name in ("ask_image", "generate_image", "create_video", "query_video"):
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
                raise
            self._remember_generated_ids(gen_ids)
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
