"""会话上下文压缩（Context Compaction）。

当一轮对话的上下文消息接近模型上下文窗口上限时，把中间的「旧消息」压成一条
摘要，从而在不丢失关键信息的前提下腾出 token 预算。

核心思路：
    messages = [system] + 中间旧消息 + 末尾若干条
                          │
                          └─ 超出 token_budget 时，经 _summarize 压缩成
                             一条 {"role":"system","content":"[历史摘要]: ..."}
                             并追加写入 <workspace>/memory/HISTORY.md 留痕。

数据落点：
    - 压缩后的摘要消息保留在内存 messages 中，供后续轮次使用；
    - 同一份摘要同步写入 HISTORY.md（带时间戳），作为可审计的长期记忆轨迹。

职责边界（TASK-006 解耦）：
    - 本模块只负责「当前会话上下文压缩」，与长期记忆管理（daily / USER /
      MEMORY）彻底解耦：压缩不再写 daily，也不触碰任何长期记忆副作用；
    - 压缩结果通过 ``CompactionResult`` 显式返回，调用方按返回值落盘与决策，
      不依赖本模块的共享可变状态（无 ``last_estimate`` / ``last_consolidation``）。
"""

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from providers.base import LLMProvider
from providers.usage import PromptCacheUsage
from agent.cache_observability import stable_text_hash


# 粗略 token 估算用：匹配 CJK 表意文字及中文常用标点/全角符号
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def _count_text(text: str) -> int:
    """估算单段文本的 token 数（极简启发式，非精确 tokenizer）。

    - CJK 字符（中文/日文等）：约 1.5 token/字（保守偏多，便于提前压缩）
    - 其他字符（英文、数字、符号）：约 0.25 token/字符（即 4 字符≈1 token）
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    non_cjk = len(text) - cjk
    return int(cjk * 1.5 + non_cjk * 0.25) + 1


# ===== 摘要输入降噪（TASK-008）=====
# role=tool 返回结果在摘要输入中保留的首尾窗口上限（字符）：只留结论性片段，
# 不携带完整 stdout/stderr、文件全文、搜索结果正文等噪声。
_TOOL_CONTENT_LIMIT = 200
# tool_calls arguments 解析失败时的安全截断上限（字符）
_ARG_RAW_LIMIT = 200
# arguments 中单个字符串值截断上限（字符）
_ARG_VALUE_LIMIT = 120
# 整个 arguments 重序列化后的上限（字符）
_ARG_RENDER_LIMIT = 400
# 大字段黑名单：这些参数携带全文/正文/长提示等噪声，摘要输入一律丢弃
_HEAVY_ARG_KEYS = frozenset({
    "content", "prompt", "text", "body", "html", "payload", "data",
    "file_content", "patch", "script", "code", "message", "output",
    "full_text", "markdown", "instructions", "article", "report",
})
# 敏感键黑名单：命中即整键丢弃（键名先转 snake_case 再比对，可覆盖 camelCase
# 变体如 fileContent）。与 _HEAVY_ARG_KEYS 合并使用，防 token/secret/api_key
# 等凭据字段挂在非大字段键名下透传。
_SENSITIVE_ARG_KEYS = frozenset({
    "token", "secret", "api_key", "apikey", "password",
    "credential", "credentials", "auth", "authorization",
    "access_key", "private_key", "session_id", "cookie",
})
# 图类工具：prompt/question 是轻量关键事实（描述生成/查看了什么图），摘要必须
# 保留；其余工具仍走全局黑名单（丢弃 prompt 等大字段）。
_IMAGE_TOOLS = frozenset({"generate_image", "ask_image"})
# 图类工具保留的轻量键白名单（image_url 若为 base64 data URI 会在值级被丢弃）
_IMAGE_TOOL_KEYS = frozenset({
    "prompt", "question", "size", "image_id", "image_url", "file_path",
})
# 哨兵：表示该值应整体丢弃（敏感值 / base64 图片数据）
_DROP = object()
# base64 字符集（A-Za-z0-9+/，尾部至多两个 =）
_BASE64_CHARS_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


def _to_snake_case(name: str) -> str:
    """把键名归一为 snake_case（fileContent → file_content），供黑名单比对。"""
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()


_SUMMARY_INSTRUCTION = (
    "请用 3-5 句话概括以下对话的关键信息，保留重要的事实和结论，"
    "省略过程细节和寒暄。只输出摘要，不要其他内容。"
)


@dataclass
class CompactionResult:
    """一次压缩尝试的结构化结果（显式返回，替代共享可变状态）。

    字段说明：
        messages: 压缩后应继续使用的消息列表；未压缩时与原列表为同一对象。
        compacted: 是否真正发生了压缩。
        estimated_tokens: 压缩前的估算 token 数。
        token_budget: 本次压缩使用的 token 预算。
        summarized_messages: 被压成摘要的旧消息条数（未压缩/摘要失败为 0）。
        preserved_tail_messages: 保留的尾部消息条数（未压缩/摘要失败为 0）。
    """
    messages: List[dict]
    compacted: bool
    estimated_tokens: int
    token_budget: int
    summarized_messages: int = 0
    preserved_tail_messages: int = 0


class ContextCompactor:
    """按 token 预算对会话历史做增量压缩（每会话独立实例，无跨会话共享状态）。"""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: str,
        token_budget: int = 192_000,
    ):
        self.provider = provider
        self.workspace = workspace
        self.token_budget = token_budget

    @staticmethod
    def _estimate_value(value) -> int:
        """Estimate arbitrary OpenAI content/schema values without logging them."""
        if value is None:
            return 0
        if isinstance(value, str):
            if value.startswith("data:image/") and ";base64," in value:
                header, payload = value.split(",", 1)
                # Base64/image tokens vary by multimodal provider and often do
                # not follow normal prose tokenization.  0.75 token/character
                # is deliberately conservative; the provider may additionally
                # meter vision patches outside text usage.
                return _count_text(header) + int(len(payload) * 0.75) + 1
            return _count_text(value)
        if isinstance(value, (int, float, bool)):
            return _count_text(str(value))
        if isinstance(value, list):
            return 2 + sum(ContextCompactor._estimate_value(item) for item in value)
        if isinstance(value, dict):
            return 2 + sum(
                _count_text(str(key)) + ContextCompactor._estimate_value(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            )
        return _count_text(str(value))

    def estimate_tokens(self, messages: List[dict], tools: Optional[List[dict]] = None) -> int:
        """预估 messages 的总 token 数（极简启发式，非精确 tokenizer）。

        策略：每条消息固定开销 ~4 token（role 标记、结构 framing）；
        文本按 CJK 1.5 token/字、其他 0.25 token/字符估算；
        tool_calls 的 name + arguments 同样计入。
        整体偏保守（略高估），以便接近预算时提前压缩而非撑爆窗口。
        """
        total = 0
        for msg in messages:
            total += 4  # 每条消息的结构开销
            total += self._estimate_value(msg.get("content"))
            # 工具调用：函数名 + 参数一并计入
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {}) or {}
                total += _count_text(fn.get("name", ""))
                total += _count_text(fn.get("arguments", "") or "")
        tool_tokens = self._estimate_value(tools) if tools else 0
        total += tool_tokens
        return total

    async def maybe_compact(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        cache_turn=None,
    ) -> CompactionResult:
        """若 messages 超出 token 预算，则把中间旧消息压缩成一条摘要。

        结构：保留 ``messages[0]``（system 提示）与末尾 6 条，中间的旧消息
        经 ``_summarize`` 压缩为单条 system 摘要消息；摘要同时追加写入
        ``HISTORY.md``。预算内或无可压缩内容时，原样返回（``compacted=False``，
        ``messages`` 与入参同一对象）。摘要失败时保留原历史，避免上下文丢失。
        """
        # 空列表直接返回，避免后续切片越界
        if not messages:
            return CompactionResult(
                messages=messages, compacted=False,
                estimated_tokens=0, token_budget=self.token_budget,
            )

        # 预算内：不压缩，原样返回
        estimated_tokens = self.estimate_tokens(messages, tools)
        if estimated_tokens <= self.token_budget:
            return CompactionResult(
                messages=messages, compacted=False,
                estimated_tokens=estimated_tokens, token_budget=self.token_budget,
            )

        # 安全护栏：可压缩的中间部分至少需要 1 条，否则没必要压缩，
        # 也避免切片把首条 system 重复算进 tail 导致消息重复。
        # （正常情况下，能超预算的对话长度必然远大于 7，这里只是兜底。）
        if len(messages) <= 7:
            return CompactionResult(
                messages=messages, compacted=False,
                estimated_tokens=estimated_tokens, token_budget=self.token_budget,
            )

        system_msg = messages[0]          # 第一条（system 提示），原样保留
        tail_start = max(1, len(messages) - 6)
        # 不能从 assistant(tool_calls) → tool... 交换中间切开；若固定 6 条
        # 落在 tool 结果上，就向前扩展到声明这些调用的 assistant。
        while tail_start > 1 and messages[tail_start].get("role") == "tool":
            tail_start -= 1
        tail = messages[tail_start:]
        old_messages = messages[1:tail_start]
        if not old_messages:
            return CompactionResult(
                messages=messages, compacted=False,
                estimated_tokens=estimated_tokens, token_budget=self.token_budget,
            )

        summary = await self._summarize(old_messages, cache_turn=cache_turn)
        if not summary:
            # Context correctness wins over token pressure.  A failed summary
            # must not replace the only copy of the old conversation.
            return CompactionResult(
                messages=messages, compacted=False,
                estimated_tokens=estimated_tokens, token_budget=self.token_budget,
            )

        summary_msg = {
            "role": "system",
            "content": f"[历史摘要]: {summary}",
        }

        # 摘要落盘（保留审计轨迹）
        self._save_to_history(summary, len(old_messages))

        return CompactionResult(
            messages=[system_msg, summary_msg] + tail,
            compacted=True,
            estimated_tokens=estimated_tokens,
            token_budget=self.token_budget,
            summarized_messages=len(old_messages),
            preserved_tail_messages=len(tail),
        )

    async def _summarize(self, messages: List[dict], cache_turn=None) -> Optional[str]:
        """把旧消息拼接成文本，调用模型生成 3-5 句话摘要。

        调用失败（含模型返回空内容）时返回 None，由调用方保留原历史。
        """
        text = self._messages_to_text(messages)
        prompt = f"{_SUMMARY_INSTRUCTION}\n\n{text}"
        summary_messages = [{"role": "user", "content": prompt}]

        try:
            resp = await self.provider.chat(summary_messages, tools=None, model=None)
        except Exception:
            if cache_turn is not None:
                cache_turn.record(
                    PromptCacheUsage(),
                    tool_iteration=-1,
                    phase="consolidation",
                    system_hash=stable_text_hash(""),
                    tools_hash=stable_text_hash("[]"),
                    history_messages=len(messages),
                )
            # 即便 provider 自身已捕获异常，这里再兜一层，万无一失
            return None

        if cache_turn is not None:
            cache_turn.record(
                getattr(resp, "cache_usage", PromptCacheUsage()),
                tool_iteration=-1,
                phase="consolidation",
                system_hash=stable_text_hash(""),
                tools_hash=stable_text_hash("[]"),
                history_messages=len(messages),
            )

        # provider 在 API 失败时返回 finish_reason="error"；空内容也视为失败
        if resp.finish_reason == "error" or not (resp.content or "").strip():
            return None

        return resp.content.strip()

    def _save_to_history(self, summary: str, original_count: int) -> None:
        """把压缩摘要追加写入 ``<workspace>/memory/HISTORY.md``。"""
        memory_dir = os.path.join(self.workspace, "memory")
        os.makedirs(memory_dir, exist_ok=True)
        path = os.path.join(memory_dir, "HISTORY.md")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 末尾多补一个空行，保证多次压缩块之间视觉分隔
        block = (
            f"## {now}\n"
            f"压缩了 {original_count} 条旧消息\n\n"
            f"{summary}\n\n"
            f"---\n\n"
        )
        with open(path, "a", encoding="utf-8") as f:
            f.write(block)

    @staticmethod
    def _messages_to_text(messages: List[dict]) -> str:
        """把消息列表拼成可读文本，供摘要模型消费。

        处理三类内容：普通文本 content、纯工具调用（content 为空）、
        工具返回结果（role=tool）。缺失字段一律降级为空串，不抛异常。

        TASK-008 降噪（只作用于摘要输入视图）：
        - tool_calls 的 ``arguments`` 只保留轻量参数摘要（路径/文件名/URL/
          query/command 等），丢弃 content/prompt/全文等大字段，以及
          token/secret/api_key 等敏感键；
        - role=tool 的返回结果只保留结论（写文件结论 / shell 退出码+首尾 /
          搜索标题 / 通用首尾窗口），不携带文件全文与大段输出；
        - 用户 / assistant 普通文本与图片占位行为保持不变。

        注意（规范顺序）：本方法依赖 ``assistant(tool_calls)`` 消息先于其
        ``role=tool`` 结果消息（OpenAI 协议顺序）。非规范顺序下按
        ``tool_call_id`` 反查工具名可能误配（先到的 tool 消息查不到声明方），
        导致降噪分支选择偏差。
        """
        parts: List[str] = []
        tool_names: dict = {}  # tool_call_id -> 工具名，供 role=tool 消息定位
        for m in messages:
            role = m.get("role", "unknown")

            # 工具调用：记录 id→名称 映射，并输出降噪后的轻量参数摘要
            # （content 可能为空，纯工具调用只输出这行）
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {}) or {}
                name = fn.get("name", "?")
                tool_names[tc.get("id", "")] = name
                args = ContextCompactor._denoise_tool_call_args(
                    fn.get("arguments", ""), tool_name=name
                )
                parts.append(f"[{role}] 调用工具 {name}({args})")

            # 工具返回结果：按工具名降噪；其余消息走既有 _summary_content
            if role == "tool":
                tool_name = tool_names.get(m.get("tool_call_id", ""), "")
                content = ContextCompactor._denoise_tool_content(
                    tool_name, m.get("content")
                )
            else:
                content = ContextCompactor._summary_content(m.get("content"))

            if content:
                parts.append(f"[{role}] {content}")

        return "\n".join(parts)

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        """首尾窗口截断：超长时保留开头与结尾，中间用省略号代替。

        用于保住关键结论（如 shell 退出码在末尾、写文件结论在开头）。
        """
        if not text:
            return ""
        if len(text) <= limit:
            return text
        head = (limit * 2) // 3
        tail = limit - head
        return text[:head] + "…" + text[-tail:]

    @staticmethod
    def _denoise_arg_value(value, image_tool: bool = False):
        """递归清洗单个参数值：丢弃大字段与敏感值，超长字符串截断。

        - 键名先转 snake_case 再比对黑名单（fileContent → file_content），
          _HEAVY_ARG_KEYS（全文/正文等大字段）与 _SENSITIVE_ARG_KEYS
          （token/secret/api_key 等凭据）命中即整键丢弃；
        - 图类工具（image_tool=True）改用 _IMAGE_TOOL_KEYS 白名单，
          保留 prompt/question/size/image_id 等轻量关键事实；
        - 字符串值先做敏感特征检测（_is_sensitive_value），命中即丢弃；
        - 字符串本身是嵌套 JSON（dict/list）时递归清洗，防 config 等
          字符串字段内嵌密钥（如 ``{"config": "{\"api_key\": ...}"}``）；
        - 其余非敏感但超长的字符串保持首尾截断行为不变。
        """
        if isinstance(value, dict):
            cleaned = {}
            for key, item in value.items():
                key_norm = _to_snake_case(str(key))
                if image_tool:
                    if key_norm not in _IMAGE_TOOL_KEYS:
                        continue
                elif (
                    key_norm in _HEAVY_ARG_KEYS
                    or key_norm in _SENSITIVE_ARG_KEYS
                ):
                    continue
                item_clean = ContextCompactor._denoise_arg_value(
                    item, image_tool=image_tool
                )
                if item_clean is _DROP:
                    continue
                cleaned[key] = item_clean
            return cleaned
        if isinstance(value, list):
            out = []
            for item in value:
                item_clean = ContextCompactor._denoise_arg_value(
                    item, image_tool=image_tool
                )
                if item_clean is not _DROP:
                    out.append(item_clean)
            return out
        if isinstance(value, str):
            if ContextCompactor._is_sensitive_value(value):
                return _DROP
            # base64 图片数据（data:image/...;base64,...）不进摘要
            if value.startswith("data:") and ";base64," in value:
                return _DROP
            # 字符串本身是嵌套 JSON：递归清洗（防内嵌密钥透传）
            stripped = value.strip()
            if stripped[:1] in ("{", "["):
                try:
                    parsed = json.loads(stripped)
                except Exception:
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    cleaned = ContextCompactor._denoise_arg_value(
                        parsed, image_tool=image_tool
                    )
                    return json.dumps(cleaned, ensure_ascii=False)
            if len(value) > _ARG_VALUE_LIMIT:
                return ContextCompactor._truncate(
                    value, _ARG_VALUE_LIMIT
                )
        return value

    @staticmethod
    def _denoise_tool_call_args(arguments: str, tool_name: str = None) -> str:
        """把 tool_calls 的 ``arguments``（JSON 字符串）压缩成轻量参数摘要。

        解析成功：按字段筛选——默认保留路径/文件名/URL/query/command 等轻量
        字段，丢弃 content/prompt/全文等大字段与 token/secret 等敏感键；
        图类工具（generate_image/ask_image）改用白名单保留 prompt/question
        关键事实。超长值截断后重序列化。
        解析失败（非 JSON 或非对象）：降级为安全截断，绝不把原文全文灌进摘要输入。
        """
        if not arguments:
            return ""
        try:
            parsed = json.loads(arguments)
        except Exception:
            return ContextCompactor._truncate(
                arguments, _ARG_RAW_LIMIT
            )
        if not isinstance(parsed, dict):
            return ContextCompactor._truncate(
                arguments, _ARG_RAW_LIMIT
            )
        image_tool = (tool_name or "").lower() in _IMAGE_TOOLS
        cleaned = ContextCompactor._denoise_arg_value(
            parsed, image_tool=image_tool
        )
        rendered = json.dumps(cleaned, ensure_ascii=False)
        if len(rendered) > _ARG_RENDER_LIMIT:
            return ContextCompactor._truncate(
                rendered, _ARG_RENDER_LIMIT
            )
        return rendered

    @staticmethod
    def _titles_only(text: str) -> str:
        """从 web_search 结果里只提取标题级信息（### N. 标题 / 链接: url）。

        提取不到标题行时（如错误提示、无结果），回退为首尾窗口截断。
        """
        kept = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("###") or stripped.startswith("链接:"):
                kept.append(stripped)
        if kept:
            return "\n".join(kept)
        return ContextCompactor._truncate(text, _TOOL_CONTENT_LIMIT)

    @staticmethod
    def _is_sensitive_value(value) -> bool:
        """特征检测字符串是否携带敏感值（密钥/凭据/Token/JWT/AWS Key/私钥块）。

        命中即由调用方整体丢弃，不做截断（截断只会把密钥片段带进窗口）。
        """
        if not isinstance(value, str):
            return False
        s = value.strip()
        if not s:
            return False
        # OpenAI 风格密钥（sk-...）、AWS Access Key（AKIA...）、JWT（eyJ...）
        if s.startswith("sk-") or s.startswith("AKIA") or s.startswith("eyJ"):
            return True
        # 超长 base64（长度>80 且仅 base64 字符集）：疑似密钥/加密载荷
        if len(s) > 80 and _BASE64_CHARS_RE.match(s):
            return True
        # PEM 私钥块：BEGIN (RSA|EC|OPENSSH|PRIVATE) KEY
        if re.search(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY", s):
            return True
        return False

    @staticmethod
    def _denoise_tool_content(name: str, content) -> str:
        """对 role=tool 的返回结果做降噪，只留结论性信息（TASK-008）。

        - 搜索结果（web_search）：只留标题与链接，省略正文；
        - shell 类（exec）：额外把「标准错误:」之后的细节收窄为退出码，
          避免 stderr 中的密钥行随首尾窗口进入；
        - write_file / read_file / web_fetch / 大段代码 / 未知工具：统一走
          首尾窗口截断（写文件结果本身已是「成功/失败 + 路径 + 字符数」结论，
          仅加超长护栏）；
        - 非字符串内容（图片等）回退 _summary_content 既有占位行为。
        """
        if not isinstance(content, str):
            return ContextCompactor._summary_content(content)
        if not content:
            return ""
        tool = (name or "").lower()
        if tool == "web_search":
            return ContextCompactor._titles_only(content)
        if tool == "exec":
            content = ContextCompactor._exec_narrow_stderr(content)
        # 其余工具：通用首尾窗口截断
        return ContextCompactor._truncate(content, _TOOL_CONTENT_LIMIT)

    @staticmethod
    def _exec_narrow_stderr(content: str) -> str:
        """exec 结果：把「标准错误:」之后的 stderr 细节收窄为退出码行。

        保留标记本身（提示该调用产生过标准错误），密钥/日志细节不进首尾窗口。
        无标记时原样返回（可能只是超时/短错误提示）。
        """
        marker = "标准错误:"
        if marker not in content:
            return content
        head_part, tail_part = content.split(marker, 1)
        exit_part = ""
        for line in tail_part.splitlines():
            if "[退出码" in line:
                exit_part = line
        return f"{head_part}{marker}\n{exit_part}"

    @staticmethod
    def _summary_content(content) -> str:
        """Serialize text for summaries without copying image bytes or URLs."""
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return "" if content is None else "[非文本内容已省略]"
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                parts.append("[非文本内容已省略]")
                continue
            if item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif item.get("type") in ("image", "image_url"):
                parts.append("[图片内容已省略；参考相邻对话中的视觉结论]")
            else:
                parts.append("[非文本内容已省略]")
        return " ".join(parts)
