"""每日记忆（Daily Memory）。

把当天发生的重要事件追加到 ``memory/daily/YYYY-MM-DD.md``，按 ``## 分类`` 组织，
append-only。**不暴露为工具**——仅在程序内部两个时机调用：

1. ``/clear`` 清空会话前：总结当前会话重要事件，写入 daily；
2. ``MemoryConsolidation`` 压缩前：把即将被压成摘要的旧消息里的重要事件落 daily。

设计原则（大道至简）：daily 是「事件留痕」，与 ``MemoryConsolidation`` 的「token
压缩」职责不同——前者按天保存关键事实供日后检索，后者只是为腾 token 预算把
旧消息压短。二者在压缩触发点协作，但不互相替代。
"""

import os
from datetime import datetime
from typing import List, Optional

from providers.base import LLMProvider


# 让模型从对话里提取「值得记进 daily 的事件」的指令
_DAILY_EXTRACT_INSTRUCTION = (
    "请从以下对话中提取今天发生的重要事件、项目变化、用户新偏好。"
    "只输出关键事实，每条一行，省略寒暄、过程和客套。"
    "如果没有值得记录的内容，只输出「无」，不要输出其他任何内容。"
)


class DailyMemory:
    """按天追加事件到 ``<memory_dir>/daily/YYYY-MM-DD.md``。"""

    def __init__(self, memory_dir: str):
        """初始化。

        Args:
            memory_dir: 记忆根目录（与 MEMORY.md / USER.md 同级），
                daily 文件落在其下的 ``daily/`` 子目录。
        """
        self.daily_dir = os.path.join(memory_dir, "daily")
        os.makedirs(self.daily_dir, exist_ok=True)

    def _path(self) -> str:
        """今天的 daily 文件路径（按本地日期命名）。"""
        today = datetime.now().strftime("%Y-%m-%d")
        return os.path.join(self.daily_dir, f"{today}.md")

    def append(self, category: str, content: str) -> None:
        """追加一条事件到今天的 daily 文件。

        Args:
            category: 分类名（如「NanoClaw」「Personal」「会话总结」「压缩前保存」）。
            content: 事件内容；可多行，每行一条事实。空内容直接跳过。

        文件不存在时先写日期标题。多次 append 同分类会重复 ``## 分类`` 标题，
        这是 append-only 的简单实现；模型或人工可后续整理合并。
        """
        content = (content or "").strip()
        if not content:
            return

        path = self._path()
        # 文件不存在时先写头部（# 日期）
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# {datetime.now().strftime('%Y-%m-%d')}\n\n")

        with open(path, "a", encoding="utf-8") as f:
            f.write(f"## {category}\n\n")
            # 多行内容逐行加 - 前缀，保持列表格式
            for line in content.splitlines():
                line = line.strip()
                if line:
                    f.write(f"- {line}\n")
            f.write("\n")


def _messages_to_text(messages: List[dict]) -> str:
    """把消息列表拼成可读文本，供摘要模型消费。

    与 ``MemoryConsolidation._messages_to_text`` 同构：处理普通文本、纯工具调用、
    工具返回结果三类内容，缺失字段降级为空串不抛异常。
    """
    parts: List[str] = []
    for m in messages:
        role = m.get("role", "unknown")
        content = m.get("content") or ""
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {}) or {}
            args = fn.get("arguments", "")
            parts.append(f"[{role}] 调用工具 {fn.get('name', '?')}({args})")
        if content:
            parts.append(f"[{role}] {content}")
    return "\n".join(parts)


async def summarize_messages_to_daily(
    provider: LLMProvider,
    daily: Optional[DailyMemory],
    messages: List[dict],
    category: str = "会话总结",
) -> None:
    """调模型从 messages 提取重要事件，写入 daily。

    任何环节失败（模型异常、空结果、daily 为 None）都静默返回，不影响调用方
    主流程——daily 是 nice-to-have，不应阻断 clear / 压缩。

    Args:
        provider: 模型 Provider，用于生成摘要。
        daily: DailyMemory 实例；为 None 时直接返回（未启用 daily）。
        messages: 待提取的消息列表。
        category: 写入 daily 时的分类名。
    """
    if daily is None or not messages:
        return

    text = _messages_to_text(messages)
    if not text.strip():
        return

    prompt = f"{_DAILY_EXTRACT_INSTRUCTION}\n\n{text}"
    try:
        resp = await provider.chat(
            [{"role": "user", "content": prompt}], tools=None, model=None
        )
    except Exception:  # noqa: BLE001 - daily 失败不应影响主流程
        return

    # 模型失败或空内容：静默跳过
    if resp.finish_reason == "error" or not (resp.content or "").strip():
        return

    summary = resp.content.strip()
    # 模型判断「无值得记录」：不写
    if summary in ("无", "无。", "无值得记录"):
        return

    try:
        daily.append(category, summary)
    except Exception:  # noqa: BLE001 - 写盘失败不阻断主流程
        pass
