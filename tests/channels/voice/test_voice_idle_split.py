"""TASK-026 子任务 2「voice 空闲自动分片」专项测试。

覆盖 ``VoiceChannel`` 经 ``inject_text`` 的惰性空闲分片（``_maybe_split_session``）：
1. 首次入站进入 session 0；推进超过 ``idle_ttl_sec`` 再入站 → 自动切到
   session 1（收到 InboundMessage 的 sender_id == "local:1"），旧会话保留
   （_session_seq 递增、未清任何会话）；
2. 时间未超阈值 → 不切（仍 session 0）；
3. ``idle_ttl_sec <= 0`` → 永不切；
4. 手动 ``/new`` 后活动时间重置（推进少量时间再入站不切）；
5. 内置命令计入活动时间但不触发分片（用户查 /sessions 不会被切走）。

时钟用可控 ``now_fn``（假时钟推进），不依赖真实 ``time.time``；入站消息
直接从 ``bus.inbound_queue`` 取（fake bus 捕获 InboundMessage 的既有写法）。
"""

import asyncio
import unittest

from bus.queue import MessageBus
from channels.voice import VoiceChannel


class _Clock:
    """可手动推进的可变时钟：作为 now_fn 注入，模拟时间流逝。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


class _RecordingPruner:
    """记录被调用序号的 mock pruner（用于断言「未清任何会话」）。"""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, seq: int) -> None:
        self.calls.append(seq)


class VoiceIdleSplitTests(unittest.IsolatedAsyncioTestCase):
    def _make_channel(
        self, *, idle_ttl_sec: float = 30.0, clock=None, session_pruner=None
    ):
        if clock is None:
            clock = _Clock()
        voice = VoiceChannel(
            MessageBus(),
            idle_ttl_sec=idle_ttl_sec,
            now_fn=clock,
            session_pruner=session_pruner,
        )
        emitted: list = []
        voice._reply_sink = emitted.append
        return voice, clock, emitted

    async def _inject(self, voice, text):
        """注入文本并取回对应 InboundMessage；命令/空文本不进 bus 时返回 None。"""
        before = voice.bus.inbound_queue.qsize()
        await voice.inject_text(text)
        if voice.bus.inbound_queue.qsize() == before:
            return None
        return await asyncio.wait_for(voice.bus.inbound_queue.get(), timeout=1)

    async def test_idle_exceeded_splits_to_new_session_keeping_old(self):
        pruner = _RecordingPruner()
        voice, clock, emitted = self._make_channel(
            idle_ttl_sec=30.0, session_pruner=pruner
        )
        # 首次入站进入 session 0
        msg0 = await self._inject(voice, "你好")
        self.assertEqual(msg0.sender_id, "local:0")
        self.assertEqual(voice._current_session, 0)
        self.assertEqual(voice._session_seq, 0)

        # 推进超过 idle_ttl_sec 后再入站 → 自动开新会话 session 1
        clock.advance(31.0)
        msg1 = await self._inject(voice, "还在吗")
        self.assertEqual(msg1.sender_id, "local:1")
        self.assertEqual(voice._current_session, 1)
        # 旧会话保留：seq 递增到 1，且没有清掉任何会话（pruner 零调用）
        self.assertEqual(voice._session_seq, 1)
        self.assertEqual(pruner.calls, [])
        # 轻提示告知用户开了新话题
        self.assertTrue(any("⏱️" in e and "会话 #1" in e for e in emitted))

    async def test_below_threshold_does_not_split(self):
        voice, clock, _emitted = self._make_channel(idle_ttl_sec=30.0)
        await self._inject(voice, "你好")
        clock.advance(29.0)  # 未超 30 秒
        msg = await self._inject(voice, "还没超时")
        self.assertEqual(msg.sender_id, "local:0")
        self.assertEqual(voice._current_session, 0)
        self.assertEqual(voice._session_seq, 0)

    async def test_disabled_idle_never_splits(self):
        # idle_ttl_sec = 0 → 自动分片禁用，推进再久也不切
        voice, clock, _emitted = self._make_channel(idle_ttl_sec=0)
        await self._inject(voice, "你好")
        clock.advance(100000.0)
        msg = await self._inject(voice, "很久之后")
        self.assertEqual(msg.sender_id, "local:0")
        self.assertEqual(voice._current_session, 0)
        self.assertEqual(voice._session_seq, 0)

    async def test_manual_new_resets_idle_timer(self):
        voice, clock, _emitted = self._make_channel(idle_ttl_sec=30.0)
        await self._inject(voice, "第一条")
        clock.advance(20.0)  # 快超但未超
        await voice.inject_text("/new")  # 手动开新会话 → 重置活动时间
        self.assertEqual(voice._current_session, 1)
        clock.advance(20.0)  # 距 /new 仅 20 秒（< 30）
        msg = await self._inject(voice, "第二条")
        self.assertEqual(msg.sender_id, "local:1")
        self.assertEqual(voice._current_session, 1)

    async def test_command_counts_as_activity_but_does_not_split(self):
        # 超时后先发命令再发消息：命令算活动（重置计时）但不触发分片，
        # 后续消息因此留在原会话——用户查 /sessions 不会被切走。
        voice, clock, _emitted = self._make_channel(idle_ttl_sec=30.0)
        await self._inject(voice, "你好")
        clock.advance(31.0)
        await voice.inject_text("/sessions")  # 命令：不触发分片、但重置活动时间
        self.assertEqual(voice._current_session, 0)  # 命令没有把会话切走
        msg = await self._inject(voice, "命令之后")
        self.assertEqual(msg.sender_id, "local:0")
        self.assertEqual(voice._current_session, 0)


if __name__ == "__main__":
    unittest.main()
