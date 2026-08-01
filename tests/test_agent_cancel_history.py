"""Regression tests: cancelled (web "stop") turns must backfill conversation history.

After the user hits the web "stop" button, ``AgentLoop.run`` re-raises
``CancelledError`` without going through the normal
``_persist(final_msg) + _save_to_history(messages)`` tail, so neither the
in-memory ``_session_history`` nor the assistant side of this turn gets updated.
The next turn's ``_run`` builds its model context from ``_session_history``;
without a backfill the model would see two consecutive user messages and not
know what a follow-up "继续" is supposed to continue.

These tests cover:

1. Cancel during a blocking model call -> ``_session_history`` and the JSONL
   disk both contain ``[user, interrupted-assistant placeholder]``.
2. Reusing the same AgentLoop for the next "继续" turn -> the model request
   contains the previous user message and the stopped placeholder, not two
   consecutive user messages.
3. Cancel while the model already streamed partial text -> the placeholder
   carries that partial text plus the stopped marker.
4. Repeated backfill is idempotent (no duplicate appends, disk stays clean).
5. Cancel during a tool execution that produced image metadata reuses the
   existing ``_execute_tools`` interrupted record (no extra placeholder,
   disk & memory consistent).
"""

import asyncio
from copy import deepcopy
import os
import tempfile
import unittest

from agent.context import ContextBuilder
from agent.loop import AgentLoop
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from providers.base import LLMProvider, LLMResponse, ToolCallRequest
from session.manager import SessionManager


class _BlockingProvider(LLMProvider):
    """Provider whose chat blocks until the enclosing task is cancelled."""

    def __init__(self):
        self.entered = asyncio.Event()

    async def chat(self, messages, tools=None, model=None):
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("任务已取消后不应继续执行")


class _BlockThenRespondProvider(LLMProvider):
    """Provider that blocks on the first call, then answers on later calls.

    Lets the same AgentLoop instance be reused for a follow-up turn after the
    first turn is cancelled (the real web flow keeps one AgentLoop per session).
    """

    def __init__(self, response: LLMResponse):
        self.response = response
        self.requests = []
        self.calls = 0
        self.entered = asyncio.Event()

    async def chat(self, messages, tools=None, model=None):
        self.calls += 1
        self.requests.append(deepcopy(messages))
        if self.calls == 1:
            self.entered.set()
            await asyncio.Event().wait()
        return self.response


class _PartialStreamProvider(LLMProvider):
    """Provider that streams two tokens then blocks until cancelled."""

    def __init__(self):
        self.entered = asyncio.Event()

    async def chat(self, messages, tools=None, model=None):
        raise AssertionError("流式路径不应调用 chat()")

    async def chat_stream(self, messages, tools=None, model=None):
        yield {"type": "token", "content": "部分"}
        yield {"type": "token", "content": "回答"}
        self.entered.set()
        await asyncio.Event().wait()
        yield {"type": "done", "response": LLMResponse(content="部分回答")}


class _BlockingTool(Tool):
    """Tool whose execute blocks until the enclosing task is cancelled."""

    name = "block"
    description = "test blocking tool"
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self):
        self.entered = asyncio.Event()

    async def execute(self, **kwargs):
        self.entered.set()
        await asyncio.Event().wait()
        return "unreachable"


class _ScriptedProvider(LLMProvider):
    def __init__(self, responses):
        self.responses = list(responses)

    async def chat(self, messages, tools=None, model=None):
        return self.responses.pop(0)


class AgentCancelHistoryTests(unittest.IsolatedAsyncioTestCase):
    def _loop(self, tmp, provider, session_key="web:cancel", **kwargs):
        return AgentLoop(
            provider,
            kwargs.pop("tools", ToolRegistry()),
            ContextBuilder(tmp),
            SessionManager(os.path.join(tmp, "sessions")),
            session_key=session_key,
            model="m",
            max_iterations=8,
            turn_timeout=30,
            **kwargs,
        )

    async def _cancel_and_await(self, task):
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        self.assertTrue(task.cancelled())

    async def test_cancel_backfills_user_and_interrupted_assistant(self):
        provider = _BlockingProvider()
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(os.path.join(tmp, "sessions"))
            loop = self._loop(tmp, provider, "web:cancel")

            task = asyncio.create_task(loop.run("继续"))
            await asyncio.wait_for(provider.entered.wait(), timeout=1)
            await self._cancel_and_await(task)

            self.assertEqual(loop.last_run_status, "cancelled")
            history = loop._session_history
            self.assertEqual([m["role"] for m in history], ["user", "assistant"])
            self.assertEqual(history[0]["content"], "继续")
            self.assertIn("上一轮回答被用户手动停止", history[1]["content"])

            # 磁盘与内存一致
            disk = sessions.get_history("web:cancel")
            self.assertEqual([m["role"] for m in disk], ["user", "assistant"])
            self.assertEqual(disk[0]["content"], "继续")
            self.assertEqual(disk[1]["content"], history[1]["content"])

            # 重建 AgentLoop（模拟进程重启 / 换新实例）也能从磁盘恢复同样的上下文
            restarted = self._loop(tmp, _BlockingProvider(), "web:cancel")
            self.assertEqual(
                [m["role"] for m in restarted._session_history], ["user", "assistant"]
            )
            self.assertEqual(restarted._session_history[0]["content"], "继续")

    async def test_next_continue_turn_after_cancel_has_backfilled_context(self):
        """修复的核心场景：取消后复用同一 AgentLoop，下一轮「继续」的模型请求
        包含 [上一条 user, 中断占位 assistant, 本轮 user]，而非两条连续 user。"""
        provider = _BlockThenRespondProvider(LLMResponse(content="好，继续。"))
        with tempfile.TemporaryDirectory() as tmp:
            loop = self._loop(tmp, provider, "web:continue")
            task = asyncio.create_task(loop.run("帮我写周报"))
            await asyncio.wait_for(provider.entered.wait(), timeout=1)
            await self._cancel_and_await(task)

            reply = await loop.run("继续")
            self.assertEqual(reply, "好，继续。")
            self.assertEqual(provider.calls, 2)

            messages = provider.requests[1]
            roles = [m["role"] for m in messages]
            self.assertEqual(roles, ["system", "user", "assistant", "user"])
            self.assertEqual(messages[1]["content"], "帮我写周报")
            self.assertIn("上一轮回答被用户手动停止", messages[2]["content"])
            self.assertEqual(messages[3]["content"], "继续")

    async def test_cancel_preserves_streamed_partial_answer(self):
        provider = _PartialStreamProvider()
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(os.path.join(tmp, "sessions"))
            loop = self._loop(tmp, provider, "web:partial")
            events = []

            async def sink(event):
                events.append(event)

            task = asyncio.create_task(loop.run("继续", stream_sink=sink))
            await asyncio.wait_for(provider.entered.wait(), timeout=1)
            await self._cancel_and_await(task)

            history = loop._session_history
            self.assertEqual([m["role"] for m in history], ["user", "assistant"])
            assistant = history[-1]
            self.assertTrue(assistant["content"].startswith("部分回答"))
            self.assertIn("⏹ 回答被用户停止", assistant["content"])

            # 前端已收到流式 token 与 done（取消语义不变）
            tokens = "".join(
                e["content"] for e in events if e.get("type") == "token"
            )
            self.assertEqual(tokens, "部分回答")
            done_events = [e for e in events if e.get("type") == "done"]
            self.assertEqual(len(done_events), 1)
            self.assertEqual(done_events[0]["content"], "⏹ 已停止")

    async def test_repeated_backfill_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(os.path.join(tmp, "sessions"))
            loop = self._loop(tmp, _BlockingProvider(), "web:idem")
            # 模拟取消分支的现场：user_record 尚未写盘 + 已有部分回答
            loop._last_user_record = {"role": "user", "content": "干活"}
            loop._partial_answer = "部分"

            loop._record_cancelled_turn()
            first = list(loop._session_history)
            loop._record_cancelled_turn()  # 重复调用必须无副作用

            self.assertEqual(loop._session_history, first)
            self.assertEqual(
                len([m for m in loop._session_history if m["role"] == "user"]), 1
            )
            self.assertEqual(
                len([m for m in loop._session_history if m["role"] == "assistant"]), 1
            )
            disk = sessions.get_history("web:idem")
            self.assertEqual([m["role"] for m in disk], ["user", "assistant"])

    async def test_cancel_during_tool_reuses_interrupted_record(self):
        tool = _BlockingTool()
        registry = ToolRegistry()
        registry.register(tool)
        provider = _ScriptedProvider([
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest("call-1", "block", {})],
                finish_reason="tool_calls",
            ),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(os.path.join(tmp, "sessions"))
            loop = self._loop(
                tmp, provider, "web:tool",
                tools=registry, generated_ids_sink=["img-1"],
            )

            task = asyncio.create_task(loop.run("画图"))
            await asyncio.wait_for(tool.entered.wait(), timeout=1)
            await self._cancel_and_await(task)

            history = loop._session_history
            self.assertEqual([m["role"] for m in history], ["user", "assistant"])
            assistant = history[-1]
            self.assertIn("本轮工具执行已取消", assistant["content"])
            self.assertEqual(assistant.get("generated_images"), ["img-1"])
            # 只有一条 assistant 中断记录，没有重复的通用占位
            self.assertEqual(
                len([m for m in history if m["role"] == "assistant"]), 1
            )
            # 磁盘与内存一致
            disk = sessions.get_session_messages("web:tool")
            self.assertEqual(len([r for r in disk if r["role"] == "user"]), 1)
            self.assertEqual(len([r for r in disk if r["role"] == "assistant"]), 1)


if __name__ == "__main__":
    unittest.main()
