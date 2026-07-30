"""Stable, provider-facing representation of persisted conversation history.

The live ReAct loop may keep provider-specific transient fields while it is
finishing a tool exchange.  Once a message crosses a turn or process boundary,
this module produces the single canonical representation used for replay.
"""

from copy import deepcopy
from typing import Iterable


_TRANSIENT_FIELDS = frozenset({"timestamp"})


def canonicalize_history_message(message: dict) -> dict:
    """Return a copy suitable for cross-turn persistence and replay.

    Top-level assistant ``reasoning_content`` is deliberately retained when a
    provider returned it for a tool call: the live loop already replays that
    exact field, so dropping it only after the turn would break the next exact
    prefix and make restart behavior differ.  It remains forbidden inside
    individual ``tool_calls`` elements.  Lightweight image/UI references are
    retained because ``AgentLoop`` converts them to API content at request time.
    """
    result = {
        key: deepcopy(value)
        for key, value in message.items()
        if key not in _TRANSIENT_FIELDS
    }
    if isinstance(result.get("tool_calls"), list):
        # OpenAI-compatible history accepts reasoning only at the assistant
        # message top level.  Provider-specific fields nested in tool_calls are
        # never allowed to leak across the canonical boundary.
        cleaned_calls = []
        for raw_call in result["tool_calls"]:
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function")
            if not isinstance(function, dict):
                function = {}
            cleaned_calls.append({
                "id": raw_call.get("id"),
                "type": raw_call.get("type", "function"),
                "function": {
                    "name": function.get("name"),
                    "arguments": function.get("arguments", ""),
                },
            })
        result["tool_calls"] = cleaned_calls
    return result


def canonicalize_history(messages: Iterable[dict]) -> list[dict]:
    """Normalize a history stream into valid OpenAI tool-call order.

    The function is pure and idempotent.  It repairs legacy ``tool → assistant``
    ordering, fills a missing declared result with a deterministic placeholder,
    and drops orphan tool results.  Tool results are emitted in the order of
    their corresponding declarations, not file arrival order.
    """
    normalized: list[dict] = []
    leading_tools: list[dict] = []
    pending_ids: list[str] = []
    pending_tools: dict[str, dict] = {}

    def close_pending() -> None:
        nonlocal pending_ids, pending_tools
        for tool_call_id in pending_ids:
            normalized.append(pending_tools.get(tool_call_id, {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": "（历史记录中缺失对应的工具结果，已由会话管理器自动补全）",
            }))
        pending_ids = []
        pending_tools = {}

    for raw_message in messages:
        message = canonicalize_history_message(raw_message)
        role = message.get("role")

        if pending_ids:
            if role == "tool":
                tool_call_id = message.get("tool_call_id")
                if tool_call_id in pending_ids and tool_call_id not in pending_tools:
                    pending_tools[tool_call_id] = message
                if all(tool_id in pending_tools for tool_id in pending_ids):
                    close_pending()
                continue
            close_pending()

        if role == "tool":
            leading_tools.append(message)
            continue

        if role == "assistant" and message.get("tool_calls"):
            expected_ids: list[str] = []
            for tool_call in message.get("tool_calls") or []:
                tool_call_id = tool_call.get("id")
                if tool_call_id and tool_call_id not in expected_ids:
                    expected_ids.append(tool_call_id)
            if not expected_ids:
                cleaned = dict(message)
                cleaned.pop("tool_calls", None)
                normalized.append(cleaned)
                leading_tools = []
                continue

            normalized.append(message)
            pending_ids = expected_ids
            pending_tools = {
                tool_message["tool_call_id"]: tool_message
                for tool_message in leading_tools
                if tool_message.get("tool_call_id") in pending_ids
            }
            leading_tools = []
            if all(tool_id in pending_tools for tool_id in pending_ids):
                close_pending()
            continue

        leading_tools = []
        normalized.append(message)

    if pending_ids:
        close_pending()
    return normalized
