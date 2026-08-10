"""TASK-024 voice 本地语音渠道（无音频骨架）专项测试。

覆盖两层：
1. 完整链路（真实 MessageBus + VoiceChannel + Gateway + fake agent）：
   ``inject_text`` 入站 → Agent 收到（session_key 按多会话分片）→ 回复经
   ``_reply_sink`` 出站。不触发任何真实模型/网络（fake agent）。
2. 渠道命令与注入接口（/new、/switch、/sessions、/clear、/context、send、
   默认打印兜底）以及 config.voice 默认关闭。

会话 key 断言统一为 ``voice:local:<seq>``：sender_id=``local:<seq>``，
Gateway 按 ``f"{channel}:{sender_id}"`` 推导（与任务验收标准一致）。
"""

import asyncio
import contextlib
import io
import json
import tempfile
import time
import unittest

from config import NanoClawConfig, load_config
from bus.queue import InboundMessage, MessageBus, OutboundMessage
from channels.voice import VoiceChannel
from loguru import logger
from gateway import Gateway


class _FakeAgent:
    """同步返回固定文本的假 Agent：不触发任何模型/网络。

    TASK-032 起 voice 渠道也挂 stream_sink，假 Agent 需模拟真实 Agent 的
    ``done`` 事件推送（真实 AgentLoop 在回合结束时总是向 sink 补发 done），
    否则流式 sink 的 on_done 不会被调用、文本不会被 _emit，send() 也会因
    streamed=True 而 no-op，导致回复静默。
    """

    def __init__(self, session_key: str) -> None:
        self.session_key = session_key
        self.contents: list = []

    async def run(self, content, images=None, stream_sink=None):
        self.contents.append(content)
        if stream_sink is not None:
            await stream_sink({"type": "done", "content": "固定回复"})
        return "固定回复"


class VoiceChannelTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_for(self, cond, timeout: float = 2.0) -> None:
        """轮询等待条件满足（异步测试内避免裸 sleep 竞态）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("等待条件超时未满足")

    def _start_gateway(self, voice, factory):
        """启动真实 Gateway 的入站/出站消费循环（返回 task 供 finally 清理）。"""
        gateway = Gateway(voice.bus, [voice], factory)
        inbound_task = asyncio.create_task(gateway._process_inbound())
        outbound_task = asyncio.create_task(gateway._dispatch_outbound())
        return gateway, inbound_task, outbound_task

    async def _stop_tasks(self, *tasks) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # —— 完整链路 ——

    async def test_full_chain_inject_text_to_agent_to_reply_sink(self):
        bus = MessageBus()
        voice = VoiceChannel(bus)
        replies: list = []
        voice._reply_sink = replies.append
        created: list = []

        def factory(session_key):
            agent = _FakeAgent(session_key)
            created.append(agent)
            return agent

        _, inbound_task, outbound_task = self._start_gateway(voice, factory)
        try:
            await voice.inject_text("你好")
            await self._wait_for(lambda: bool(replies))
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0].session_key, "voice:local:0")
            self.assertIn("你好", created[0].contents[0])
            # 出站回复经注入的 reply_sink 收到
            self.assertEqual(replies, ["固定回复"])
        finally:
            await self._stop_tasks(inbound_task, outbound_task)

    async def test_new_creates_next_session_key(self):
        bus = MessageBus()
        voice = VoiceChannel(bus)
        voice._reply_sink = lambda _text: None
        created: list = []

        def factory(session_key):
            agent = _FakeAgent(session_key)
            created.append(agent)
            return agent

        _, inbound_task, outbound_task = self._start_gateway(voice, factory)
        try:
            await voice.inject_text("第一条")
            await self._wait_for(lambda: len(created) >= 1 and created[0].contents)
            self.assertEqual(created[0].session_key, "voice:local:0")

            await voice.inject_text("/new")  # 命令：不进 bus，仅切会话
            await voice.inject_text("第二条")
            await self._wait_for(lambda: len(created) >= 2 and created[1].contents)
            self.assertEqual(len(created), 2)
            # 下一条消息的 sender_id 变 local:1 → agent 收到的 session_key 是 voice:local:1
            self.assertEqual(created[1].session_key, "voice:local:1")
        finally:
            await self._stop_tasks(inbound_task, outbound_task)

    async def test_switch_returns_to_previous_session(self):
        bus = MessageBus()
        voice = VoiceChannel(bus)
        voice._reply_sink = lambda _text: None
        created: list = []

        def factory(session_key):
            agent = _FakeAgent(session_key)
            created.append(agent)
            return agent

        _, inbound_task, outbound_task = self._start_gateway(voice, factory)
        try:
            await voice.inject_text("第一条")
            await self._wait_for(lambda: len(created) >= 1 and created[0].contents)
            await voice.inject_text("/new")
            await voice.inject_text("第二条")
            await self._wait_for(lambda: len(created) >= 2 and created[1].contents)

            await voice.inject_text("/switch 0")
            await voice.inject_text("第三条")
            # 切回 local:0：不新建 Agent，第三条回到 created[0]（voice:local:0）
            await self._wait_for(lambda: len(created[0].contents) >= 2)
            self.assertEqual(len(created), 2)
            self.assertIn("第三条", created[0].contents[1])
        finally:
            await self._stop_tasks(inbound_task, outbound_task)

    # —— 命令与注入接口（直接驱动总线，确定性验证）——

    async def test_normal_message_wraps_inbound_and_publishes(self):
        bus = MessageBus()
        voice = VoiceChannel(bus)
        await voice.inject_text("  你好呀  ")
        msg = await asyncio.wait_for(bus.inbound_queue.get(), timeout=1)
        self.assertIsInstance(msg, InboundMessage)
        self.assertEqual(msg.channel, "voice")
        self.assertEqual(msg.sender_id, "local:0")
        self.assertEqual(msg.chat_id, "direct")
        self.assertEqual(msg.content, "你好呀")

    async def test_blank_text_is_noop(self):
        bus = MessageBus()
        voice = VoiceChannel(bus)
        await voice.inject_text("   ")
        self.assertTrue(bus.inbound_queue.empty())

    async def test_new_and_switch_change_published_sender_id(self):
        bus = MessageBus()
        voice = VoiceChannel(bus)
        await voice.inject_text("第一条")
        msg0 = await asyncio.wait_for(bus.inbound_queue.get(), timeout=1)
        self.assertEqual(msg0.sender_id, "local:0")

        await voice.inject_text("/new")
        await voice.inject_text("第二条")
        msg1 = await asyncio.wait_for(bus.inbound_queue.get(), timeout=1)
        self.assertEqual(msg1.sender_id, "local:1")

        await voice.inject_text("/switch 0")
        await voice.inject_text("第三条")
        msg2 = await asyncio.wait_for(bus.inbound_queue.get(), timeout=1)
        self.assertEqual(msg2.sender_id, "local:0")

    async def test_sessions_lists_with_current_marker(self):
        bus = MessageBus()
        voice = VoiceChannel(bus)
        emitted: list = []
        voice._reply_sink = emitted.append
        await voice.inject_text("/sessions")
        text = emitted[0]
        self.assertIn("会话 #0", text)
        self.assertIn("当前", text)
        # 新建会话后列表包含多个会话且当前标记随最新会话
        emitted.clear()
        await voice.inject_text("/new")
        await voice.inject_text("/sessions")
        text = emitted[-1]
        self.assertIn("会话 #0", text)
        self.assertIn("会话 #1", text)
        self.assertIn("当前", text)

    async def test_clear_calls_injected_callback_with_session_key(self):
        bus = MessageBus()
        voice = VoiceChannel(bus)
        cleared: list = []
        voice._clear_callback = cleared.append
        emitted: list = []
        voice._reply_sink = emitted.append
        await voice.inject_text("/clear")
        self.assertEqual(cleared, ["voice:local:0"])
        self.assertTrue(any("历史已清空" in e for e in emitted))
        # /new 之后 /clear 应清当前会话（voice:local:1）
        cleared.clear()
        await voice.inject_text("/new")
        await voice.inject_text("/clear")
        self.assertEqual(cleared, ["voice:local:1"])

    async def test_context_calls_callback_and_echoes(self):
        bus = MessageBus()
        voice = VoiceChannel(bus)
        queried: list = []
        voice._context_callback = (
            lambda key: (queried.append(key), "当前占用 123 tokens")[1]
        )
        emitted: list = []
        voice._reply_sink = emitted.append
        await voice.inject_text("/context")
        self.assertEqual(queried, ["voice:local:0"])
        self.assertTrue(any("当前占用 123 tokens" in e for e in emitted))

    async def test_switch_validates_range(self):
        bus = MessageBus()
        voice = VoiceChannel(bus)
        emitted: list = []
        voice._reply_sink = emitted.append
        await voice.inject_text("/switch 99")
        self.assertTrue(any("不存在" in e and "有效范围" in e for e in emitted))
        emitted.clear()
        await voice.inject_text("/switch abc")
        self.assertTrue(any("用法" in e for e in emitted))

    async def test_send_forwards_content_to_reply_sink(self):
        bus = MessageBus()
        voice = VoiceChannel(bus)
        received: list = []
        voice._reply_sink = received.append
        await voice.send(
            OutboundMessage(channel="voice", chat_id="direct", content="你好呀")
        )
        self.assertEqual(received, ["你好呀"])

    async def test_send_without_sink_falls_back_to_log_without_raise(self):
        bus = MessageBus()
        voice = VoiceChannel(bus)  # _reply_sink 保持 None
        buf = io.StringIO()
        handler_id = logger.add(buf, level="INFO")
        try:
            await voice.send(
                OutboundMessage(channel="voice", chat_id="direct", content="兜底回复")
            )
        finally:
            logger.remove(handler_id)
        self.assertIn("兜底回复", buf.getvalue())

    async def test_start_idles_until_stop_event(self):
        bus = MessageBus()
        voice = VoiceChannel(bus)
        start_task = asyncio.create_task(voice.start())
        await asyncio.sleep(0.05)
        self.assertFalse(start_task.done())  # 空转存活，未被意外结束
        await voice.stop()
        await asyncio.wait_for(start_task, timeout=1)
        self.assertTrue(start_task.done())


class VoiceConfigTests(unittest.TestCase):
    def test_voice_disabled_by_default(self):
        self.assertFalse(NanoClawConfig().voice["enabled"])

    def test_load_config_enables_voice_from_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump({"voice": {"enabled": True}}, file)
            file.flush()
            cfg = load_config(file.name)
        self.assertTrue(cfg.voice["enabled"])

    def test_missing_voice_key_keeps_default_disabled(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump({}, file)
            file.flush()
            cfg = load_config(file.name)
        self.assertFalse(cfg.voice["enabled"])


if __name__ == "__main__":
    unittest.main()
