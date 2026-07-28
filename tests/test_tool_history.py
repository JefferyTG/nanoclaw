"""Regression tests for OpenAI-compatible tool message persistence and recovery."""

import tempfile
import unittest

from agent.context import ContextBuilder
from agent.loop import AgentLoop
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from providers.base import LLMProvider, LLMResponse, ToolCallRequest
from session.manager import SessionManager


class _ScriptedProvider(LLMProvider):
    def __init__(self, responses):
        self.responses = list(responses)

    async def chat(self, messages, tools=None, model=None):
        return self.responses.pop(0)


class _EchoTool(Tool):
    name = "echo"
    description = "test echo"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        return "echoed"


def _assistant_tool_message(*tool_call_ids):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": tool_call_id,
                "type": "function",
                "function": {"name": "echo", "arguments": "{}"},
            }
            for tool_call_id in tool_call_ids
        ],
    }


class ToolHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_tool_exchange_is_persisted_in_protocol_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(tmp)
            tools = ToolRegistry()
            tools.register(_EchoTool())
            loop = AgentLoop(
                provider=_ScriptedProvider([
                    LLMResponse(
                        None,
                        [
                            ToolCallRequest("call-1", "echo", {}),
                            ToolCallRequest("call-2", "echo", {"value": 2}),
                        ],
                        "tool_calls",
                    ),
                    LLMResponse("完成"),
                ]),
                tools=tools,
                context=ContextBuilder(tmp),
                session_manager=sessions,
                session_key="feishu:chat:0",
            )

            self.assertEqual(await loop.run("执行工具"), "完成")

            records = sessions.get_session_messages("feishu:chat:0")
            self.assertEqual(
                [record["role"] for record in records],
                ["user", "assistant", "tool", "tool", "assistant"],
            )
            self.assertEqual(records[1]["tool_calls"][0]["id"], "call-1")
            self.assertEqual(
                [records[2]["tool_call_id"], records[3]["tool_call_id"]],
                ["call-1", "call-2"],
            )

    async def test_legacy_tool_before_assistant_is_reordered_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(tmp)
            key = "feishu:legacy:0"
            sessions.save_message(key, {"role": "user", "content": "旧任务"})
            sessions.save_message(key, {
                "role": "tool", "tool_call_id": "call-a", "content": "result-a"
            })
            sessions.save_message(key, {
                "role": "tool", "tool_call_id": "call-b", "content": "result-b"
            })
            sessions.save_message(key, _assistant_tool_message("call-a", "call-b"))
            sessions.save_message(key, {"role": "assistant", "content": "旧回答"})

            history = sessions.get_history(key)

            self.assertEqual(
                [message["role"] for message in history],
                ["user", "assistant", "tool", "tool", "assistant"],
            )
            self.assertEqual(
                [history[2]["content"], history[3]["content"]],
                ["result-a", "result-b"],
            )

    async def test_orphan_tools_are_dropped_and_missing_results_are_filled(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(tmp)
            key = "web:legacy:0"
            sessions.save_message(key, {"role": "user", "content": "第一条"})
            sessions.save_message(key, {
                "role": "tool", "tool_call_id": "orphan", "content": "bad"
            })
            sessions.save_message(key, {"role": "user", "content": "第二条"})
            sessions.save_message(key, _assistant_tool_message("missing"))
            sessions.save_message(key, {"role": "assistant", "content": "继续"})

            history = sessions.get_history(key)

            self.assertEqual(
                [message["role"] for message in history],
                ["user", "user", "assistant", "tool", "assistant"],
            )
            self.assertEqual(history[3]["tool_call_id"], "missing")
            self.assertIn("自动补全", history[3]["content"])
            self.assertFalse(any(
                message.get("tool_call_id") == "orphan" for message in history
            ))


if __name__ == "__main__":
    unittest.main()
