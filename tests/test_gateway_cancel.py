"""Regression tests for the Web "stop" (cancel) flow.

Covers the three layers of the cancel path:

1. Gateway._process_inbound intercepts the bus control message
   (``raw={"ctl": "cancel"}``, exactly what ``WebChannel._handle_ws`` publishes
   when the browser sends ``{"ctl":true,"type":"cancel"}``) and cancels the
   active in-flight turn for that session — the control message never enters
   the chat flow, and the session lock is released so later turns proceed.
2. Gateway._cancel_session is a no-op when no turn is running.
3. AgentLoop.run emits ``{"type":"done","content":"⏹ 已停止"}`` to the stream
   sink when its task is cancelled, then re-raises ``CancelledError`` so the
   task is truly cancelled (lock / resources released upstream), instead of
   being swallowed by an ``except Exception``.
"""

import asyncio
import os
import tempfile
import unittest

from bus.queue import InboundMessage, MessageBus
from channels.base import Channel
from gateway import Gateway

from agent.context import ContextBuilder
from agent.loop import AgentLoop
from agent.tools.registry import ToolRegistry
from providers.base import LLMProvider
from session.manager import SessionManager


class _Channel(Channel):
    async def start(self):
        return None

    async def send(self, message):
        return None


class _BlockingAgent:
    """Agent whose run blocks until cancelled, signalling entry/exit."""

    def __init__(self):
        self.started = asyncio.Event()
        self.released = asyncio.Event()

    async def run(self, content, images=None, stream_sink=None):
        self.started.set()
        try:
            await asyncio.Event().wait()  # 阻塞直到任务被取消
        finally:
            self.released.set()
        return "unreachable"


class _BlockingProvider(LLMProvider):
    """Provider whose chat blocks until the enclosing task is cancelled."""

    def __init__(self):
        self.entered = asyncio.Event()

    async def chat(self, messages, tools=None, model=None):
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("任务已取消后不应继续执行")


class GatewayCancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_ctl_message_cancels_active_turn(self):
        bus = MessageBus()
        agent = _BlockingAgent()
        gateway = Gateway(bus, [_Channel("web", bus)], lambda _key: agent)

        consumer = asyncio.create_task(gateway._process_inbound())
        try:
            # 1) 发一条用户消息：_handle_one 持锁阻塞在 agent.run，被登记为在途回合
            await bus.publish_inbound(
                InboundMessage("web", "u1", "c1", "干活")
            )
            await asyncio.wait_for(agent.started.wait(), timeout=1)
            self.assertIn("web:u1", gateway._active_tasks)

            # 2) 发取消控制消息（与 WebChannel ctl 分支发布的消息同构）
            await bus.publish_inbound(
                InboundMessage("web", "u1", "c1", "", raw={"ctl": "cancel"})
            )
            await asyncio.wait_for(agent.released.wait(), timeout=1)

            # 3) 回合被真正取消：在途登记解除（锁由 async with 释放）
            for _ in range(100):
                if "web:u1" not in gateway._active_tasks:
                    break
                await asyncio.sleep(0.01)
            self.assertNotIn("web:u1", gateway._active_tasks)
        finally:
            consumer.cancel()
            try:
                await asyncio.wait_for(consumer, timeout=1)
            except asyncio.CancelledError:
                pass

    async def test_cancel_with_no_active_turn_is_noop(self):
        bus = MessageBus()
        gateway = Gateway(bus, [_Channel("web", bus)], lambda _key: _BlockingAgent())
        gateway._cancel_session("web:u1")  # 不应抛异常
        self.assertNotIn("web:u1", gateway._active_tasks)

    async def test_agent_run_emits_done_and_re_raises_on_cancel(self):
        provider = _BlockingProvider()
        with tempfile.TemporaryDirectory() as tmp:
            registry = ToolRegistry()
            context = ContextBuilder(tmp)
            session = SessionManager(os.path.join(tmp, "sessions"))
            loop = AgentLoop(
                provider, registry, context, session,
                session_key="web:u1", model="m", max_iterations=8,
                turn_timeout=30,
            )
            events = []

            async def sink(event):
                events.append(event)

            task = asyncio.create_task(loop.run("干活", stream_sink=sink))
            await asyncio.wait_for(provider.entered.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)

            # CancelledError 未被吞掉：任务真正取消
            self.assertTrue(task.cancelled())
            self.assertEqual(loop.last_run_status, "cancelled")
            # 已向流式 sink 补发 done（前端据此恢复发送按钮）
            done_events = [e for e in events if e.get("type") == "done"]
            self.assertEqual(len(done_events), 1)
            self.assertEqual(done_events[0]["content"], "⏹ 已停止")

    async def test_agent_run_without_sink_cancel_still_re_raises(self):
        """无 stream_sink（CLI/飞书）被取消时不发 done，但照样上抛 CancelledError。"""
        provider = _BlockingProvider()
        with tempfile.TemporaryDirectory() as tmp:
            loop = AgentLoop(
                provider, ToolRegistry(), ContextBuilder(tmp),
                SessionManager(os.path.join(tmp, "sessions")),
                session_key="cli:direct", model="m", max_iterations=8,
                turn_timeout=30,
            )
            task = asyncio.create_task(loop.run("干活"))
            await asyncio.wait_for(provider.entered.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
            self.assertTrue(task.cancelled())
            self.assertEqual(loop.last_run_status, "cancelled")


if __name__ == "__main__":
    unittest.main()
