"""会话记忆压缩（Memory Consolidation）。

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
"""

import os
import re
from datetime import datetime
from typing import List, Optional

from providers.base import LLMProvider
from agent.daily import DailyMemory, summarize_messages_to_daily


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


_SUMMARY_INSTRUCTION = (
    "请用 3-5 句话概括以下对话的关键信息，保留重要的事实和结论，"
    "省略过程细节和寒暄。只输出摘要，不要其他内容。"
)


class MemoryConsolidation:
    """按 token 预算对会话历史做增量压缩。"""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: str,
        token_budget: int = 192_000,
        daily_memory: Optional[DailyMemory] = None,
    ):
        self.provider = provider
        self.workspace = workspace
        self.token_budget = token_budget
        # 每日记忆：压缩前把旧消息里的重要事件落 daily，避免关键事实随压缩丢失
        self.daily_memory = daily_memory

    def estimate_tokens(self, messages: List[dict]) -> int:
        """预估 messages 的总 token 数（极简启发式，非精确 tokenizer）。

        策略：每条消息固定开销 ~4 token（role 标记、结构 framing）；
        文本按 CJK 1.5 token/字、其他 0.25 token/字符估算；
        tool_calls 的 name + arguments 同样计入。
        整体偏保守（略高估），以便接近预算时提前压缩而非撑爆窗口。
        """
        total = 0
        for msg in messages:
            total += 4  # 每条消息的结构开销
            content = msg.get("content")
            if isinstance(content, str):
                total += _count_text(content)
            # 工具调用：函数名 + 参数一并计入
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {}) or {}
                total += _count_text(fn.get("name", ""))
                total += _count_text(fn.get("arguments", "") or "")
        return total

    async def maybe_consolidate(self, messages: List[dict]) -> List[dict]:
        """若 messages 超出 token 预算，则把中间旧消息压缩成一条摘要。

        结构：保留 ``messages[0]``（system 提示）与末尾 6 条，中间的旧消息
        经 ``_summarize`` 压缩为单条 system 摘要消息；摘要同时追加写入
        ``HISTORY.md``。预算内或无可压缩内容时，原样返回。
        """
        # 空列表直接返回，避免后续切片越界
        if not messages:
            return messages

        # 预算内：不压缩，原样返回
        if self.estimate_tokens(messages) <= self.token_budget:
            return messages

        # 安全护栏：可压缩的中间部分至少需要 1 条，否则没必要压缩，
        # 也避免切片把首条 system 重复算进 tail 导致消息重复。
        # （正常情况下，能超 192k 预算的对话长度必然远大于 7，这里只是兜底。）
        if len(messages) <= 7:
            return messages

        system_msg = messages[0]          # 第一条（system 提示），原样保留
        tail = messages[-6:]              # 末尾 6 条，原样保留
        old_messages = messages[1:-6]     # 中间待压缩的旧消息

        # 压缩前：把旧消息里的重要事件落 daily，避免关键事实随压缩丢失。
        # daily 是 nice-to-have，失败不影响压缩主流程。
        if self.daily_memory is not None:
            await summarize_messages_to_daily(
                self.provider, self.daily_memory, old_messages,
                category="压缩前保存",
            )

        summary = await self._summarize(old_messages)

        summary_msg = {
            "role": "system",
            "content": f"[历史摘要]: {summary}",
        }

        # 摘要落盘（保留审计轨迹）
        self._save_to_history(summary, len(old_messages))

        return [system_msg, summary_msg] + tail

    async def _summarize(self, messages: List[dict]) -> str:
        """把旧消息拼接成文本，调用模型生成 3-5 句话摘要。

        调用失败（含模型返回空内容）时返回固定占位串，保证压缩流程不中断。
        """
        text = self._messages_to_text(messages)
        prompt = f"{_SUMMARY_INSTRUCTION}\n\n{text}"
        summary_messages = [{"role": "user", "content": prompt}]

        try:
            resp = await self.provider.chat(summary_messages, tools=None, model=None)
        except Exception:
            # 即便 provider 自身已捕获异常，这里再兜一层，万无一失
            return "（摘要生成失败，旧消息已丢弃）"

        # provider 在 API 失败时返回 finish_reason="error"；空内容也视为失败
        if resp.finish_reason == "error" or not (resp.content or "").strip():
            return "（摘要生成失败，旧消息已丢弃）"

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
        """
        parts: List[str] = []
        for m in messages:
            role = m.get("role", "unknown")
            content = m.get("content") or ""

            # 工具调用：简述调用的工具与参数（content 可能为空）
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {}) or {}
                args = fn.get("arguments", "")
                parts.append(f"[{role}] 调用工具 {fn.get('name', '?')}({args})")

            if content:
                parts.append(f"[{role}] {content}")

        return "\n".join(parts)
