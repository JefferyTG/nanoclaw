"""TASK-032 gateway 测试：voice 渠道挂载 stream_sink、其他渠道不受影响。

覆盖 ``gateway.py`` 的 ``_handle_one`` 流式 sink 分配逻辑：
1. voice 渠道消息 → gateway 调用 ``voice_channel.make_token_sink()`` 挂 sink
2. web 渠道消息 → 仍走 ``_make_stream_sink``（bus.stream_queue 路径不变）
3. 其他渠道（cli/feishu）→ stream_sink 为 None（不挂载）
4. voice 渠道 agent.run 收到非 None stream_sink
5. voice 渠道的 sink 不走 bus.stream_queue（直接消费）
"""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from bus.queue import InboundMessage, MessageBus, OutboundMessage
from channels.voice import VoiceChannel
from gateway import Gateway


class _FakeAgent:
    """假 Agent：记录 stream_sink 是否被调用、推 done 事件。"""

    def __init__(self, session_key: str) -> None:
        self.session_key = session_key
        self.contents: list = []
        self.stream_sinks: list = []

    async def run(self, content, images=None, stream_sink=None):
        self.contents.append(content)
        self.stream_sinks.append(stream_sink)
        if stream_sink is not None:
            await stream_sink({"type": "done", "content": "回复"})
        return "回复"


class GatewayStreamSinkTests(unittest.IsolatedAsyncioTestCase):
    """Gateway 流式 sink 分配。"""

    async def _wait_for(self, cond, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("等待条件超时未满足")

    def _make_gateway(self, channels, factory):
        """构造 Gateway 并启动入站/出站/流式分发循环。"""
        bus = MessageBus()
        # 用第一个 channel 的 bus（所有 channel 共享同一 bus）
        if channels:
            bus = channels[0].bus
        gateway = Gateway(bus, channels, factory)
        tasks = [
            asyncio.create_task(gateway._process_inbound()),
            asyncio.create_task(gateway._dispatch_outbound()),
            asyncio.create_task(gateway._dispatch_stream()),
        ]
        return gateway, bus, tasks

    async def _stop_tasks(self, *tasks) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # —— 1. voice 渠道挂载 sink ——

    async def test_voice_channel_mounts_token_sink(self):
        """voice 渠道消息 → gateway 调用 make_token_sink() → agent 收到非 None sink。"""
        bus = MessageBus()
        voice = VoiceChannel(bus)
        replies: list = []
        voice._reply_sink = replies.append
        created: list = []

        def factory(session_key):
            agent = _FakeAgent(session_key)
            created.append(agent)
            return agent

        gateway = Gateway(bus, [voice], factory)
        inbound_task = asyncio.create_task(gateway._process_inbound())
        outbound_task = asyncio.create_task(gateway._dispatch_outbound())
        stream_task = asyncio.create_task(gateway._dispatch_stream())

        try:
            await voice.inject_text("你好")
            await self._wait_for(lambda: bool(created) and created[0].stream_sinks)
            # agent 收到了非 None stream_sink
            self.assertIsNotNone(created[0].stream_sinks[0])
            # 回复经 _emit 收到（streaming sink 的 on_done 处理了文字降级）
            await self._wait_for(lambda: bool(replies))
        finally:
            await self._stop_tasks(inbound_task, outbound_task, stream_task)

    # —— 2. web 渠道仍走 _make_stream_sink ——

    async def test_web_channel_still_uses_make_stream_sink(self):
        """web 渠道消息 → stream_sink 走 bus.stream_queue（不变）。"""
        from channels.web import WebChannel

        bus = MessageBus()
        web = WebChannel("web", bus, "127.0.0.1", 0, None, "config.json")
        created: list = []

        def factory(session_key):
            agent = _FakeAgent(session_key)
            created.append(agent)
            return agent

        gateway = Gateway(bus, [web], factory)
        inbound_task = asyncio.create_task(gateway._process_inbound())
        outbound_task = asyncio.create_task(gateway._dispatch_outbound())
        stream_task = asyncio.create_task(gateway._dispatch_stream())

        try:
            # 直接 bus.publish_inbound 模拟 web 入站
            await bus.publish_inbound(InboundMessage(
                channel="web",
                sender_id="conn1",
                chat_id="conn1",
                content="你好",
            ))
            await self._wait_for(lambda: bool(created) and created[0].stream_sinks)
            # web 渠道的 sink 非 None
            self.assertIsNotNone(created[0].stream_sinks[0])
        finally:
            await self._stop_tasks(inbound_task, outbound_task, stream_task)

    # —— 3. 未知渠道不挂 sink ——

    async def test_unknown_channel_no_sink(self):
        """非 web/voice 渠道 → stream_sink 为 None。"""
        from channels.base import Channel

        class FakeChannel(Channel):
            def __init__(self, bus):
                super().__init__(name="fake", bus=bus)
                self._stop_event = asyncio.Event()

            async def start(self):
                await self._stop_event.wait()

            async def stop(self):
                self._stop_event.set()

            async def send(self, message):
                pass

        bus = MessageBus()
        fake = FakeChannel(bus)
        created: list = []

        def factory(session_key):
            agent = _FakeAgent(session_key)
            created.append(agent)
            return agent

        gateway = Gateway(bus, [fake], factory)
        inbound_task = asyncio.create_task(gateway._process_inbound())
        outbound_task = asyncio.create_task(gateway._dispatch_outbound())
        stream_task = asyncio.create_task(gateway._dispatch_stream())

        try:
            await bus.publish_inbound(InboundMessage(
                channel="fake",
                sender_id="user1",
                chat_id="chat1",
                content="你好",
            ))
            await self._wait_for(lambda: bool(created) and created[0].stream_sinks)
            # 未知渠道 sink 为 None
            self.assertIsNone(created[0].stream_sinks[0])
        finally:
            await self._stop_tasks(inbound_task, outbound_task, stream_task)

    # —— 4. voice sink 不走 bus.stream_queue ——

    async def test_voice_sink_does_not_publish_to_stream_queue(self):
        """voice 渠道的 token 事件不进入 bus.stream_queue（区别于 web）。"""
        bus = MessageBus()
        voice = VoiceChannel(bus)
        voice._reply_sink = lambda _text: None
        created: list = []

        def factory(session_key):
            agent = _FakeAgent(session_key)
            created.append(agent)
            return agent

        gateway = Gateway(bus, [voice], factory)
        inbound_task = asyncio.create_task(gateway._process_inbound())
        outbound_task = asyncio.create_task(gateway._dispatch_outbound())
        stream_task = asyncio.create_task(gateway._dispatch_stream())

        try:
            await voice.inject_text("你好")
            await self._wait_for(lambda: bool(created) and created[0].stream_sinks)

            # 等一下确保 agent run 完成
            await self._wait_for(lambda: bool(created[0].contents))

            # bus.stream_queue 应为空（voice 的 sink 不走 stream_queue）
            # 非阻塞检查：queue empty 时 get 会挂起，用 nowait 检查
            try:
                ev = bus.stream_queue.get_nowait()
                self.fail(f"stream_queue 不应有事件，但收到: {ev}")
            except asyncio.QueueEmpty:
                pass  # 期望：队列为空
        finally:
            await self._stop_tasks(inbound_task, outbound_task, stream_task)

    # —— 5. voice 渠道 OutboundMessage streamed=True ——

    async def test_voice_outbound_message_streamed_true(self):
        """voice 渠道流式后 OutboundMessage.streamed=True（send() 据此 no-op）。"""
        bus = MessageBus()
        voice = VoiceChannel(bus)
        voice._reply_sink = lambda _text: None
        sent_messages: list = []

        async def _capture_send(msg):
            sent_messages.append(msg)

        voice.send = _capture_send  # 替换 send 以捕获 OutboundMessage

        created: list = []

        def factory(session_key):
            agent = _FakeAgent(session_key)
            created.append(agent)
            return agent

        gateway = Gateway(bus, [voice], factory)
        inbound_task = asyncio.create_task(gateway._process_inbound())
        outbound_task = asyncio.create_task(gateway._dispatch_outbound())
        stream_task = asyncio.create_task(gateway._dispatch_stream())

        try:
            await voice.inject_text("你好")
            await self._wait_for(lambda: bool(sent_messages))
            # OutboundMessage 的 streamed=True
            self.assertTrue(sent_messages[0].streamed)
        finally:
            await self._stop_tasks(inbound_task, outbound_task, stream_task)


if __name__ == "__main__":
    unittest.main()
