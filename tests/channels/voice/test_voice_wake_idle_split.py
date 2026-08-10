"""TASK-033「voice 渠道唤醒前空闲分片检查」专项测试。

覆盖 ``_handle_wake`` 中新增的 ``_maybe_split_session`` + ``_bump_activity``
调用（位于 ``_play_wake_reply`` 之后、``_enter_continuous`` 之前）：

1. 唤醒时隔超 ``idle_ttl_sec`` → 唤醒后 seq+1（分片成功，且发生在
   ``_continuous`` 被置 True 之前）；
2. 唤醒时隔未超 ``idle_ttl_sec`` → seq 不变（不触发分片）；
3. 连续对讲进行中 ``inject_text`` 不分片（``_maybe_split_session`` 短路）。

全部 mock ``play_audio`` / ``record_audio_vad``，不触发真实麦克风/模型/网络。
时钟用可控 ``now_fn``（假时钟推进），不依赖真实 ``time.time``。
"""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

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


class _FakeDetector:
    def __init__(self) -> None:
        self.on_wake = None
        self.started = False
        self.stopped = False

    async def start(self, on_wake=None) -> None:
        self.on_wake = on_wake
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _FakeASR:
    def __init__(self, text="今天天气怎么样", error=None):
        self.text = text
        self.error = error
        self.calls = []

    async def transcribe(self, data, *, filename, media_type):
        self.calls.append((data, filename, media_type))
        return __import__("types").SimpleNamespace(text=self.text)


class VoiceWakeIdleSplitTests(unittest.IsolatedAsyncioTestCase):
    """唤醒前空闲分片检查（TASK-033）。"""

    def _make_channel(self, *, idle_ttl_sec=30.0, clock=None):
        """构造一个装配了 kws_detector + asr 的渠道（但不配 TTS/wake_replies，
        使 _play_wake_reply 跳过回应直接返回 False）。"""
        if clock is None:
            clock = _Clock()
        bus = MessageBus()
        detector = _FakeDetector()
        asr = _FakeASR()
        channel = VoiceChannel(
            bus,
            kws_detector=detector,
            asr_service=asr,
            record_sec=2.0,
            record_delay_sec=0.0,
            idle_ttl_sec=idle_ttl_sec,
            now_fn=clock,
            # 不配 tts / wake_replies → _play_wake_reply 直接返回 False（跳过）
            tts_service=None,
            wake_replies=None,
            wake_replies_dir="/nonexistent/",
        )
        emitted: list = []
        channel._reply_sink = emitted.append
        return channel, clock, emitted

    async def _handle_wake_safe(self, channel):
        """调用 _handle_wake 并在结束后退出连续对讲，清理后台录音任务。

        _handle_wake 会进入连续对讲并启动 _schedule_next_listen（VAD 录音
        循环）；mock record_audio_vad 返回静默以避免无限录音，随后手动退出。
        """
        silent = b"\x00" * 32
        with patch(
            "channels.voice.record_audio_vad",
            new=AsyncMock(return_value=(silent, True)),
        ), patch("channels.voice.play_audio", new=AsyncMock()):
            await channel._handle_wake()
            # 等静默退出或手动退出
            await asyncio.sleep(0.05)
            channel._exit_continuous()
            if channel._listen_task is not None:
                await asyncio.gather(
                    channel._listen_task, return_exceptions=True
                )

    # —— 1. 唤醒时隔超 idle_ttl_sec → 分片成功 ——

    async def test_wake_after_idle_exceeded_splits_session(self):
        """距上次活动超过 idle_ttl_sec 时唤醒 → seq+1（分片在 _continuous
        袋 True 之前执行，不被短路）。"""
        clock = _Clock()
        channel, clock, emitted = self._make_channel(
            idle_ttl_sec=30.0, clock=clock
        )
        # 先有一次活动（bump_activity 使 _last_activity_ts = 1000）
        channel._bump_activity()
        self.assertEqual(channel._session_seq, 0)
        self.assertEqual(channel._current_session, 0)

        # 推进超过 idle_ttl_sec
        clock.advance(31.0)

        # 唤醒：_maybe_split_session 应在 _enter_continuous 之前触发分片
        await self._handle_wake_safe(channel)

        self.assertEqual(channel._session_seq, 1)
        self.assertEqual(channel._current_session, 1)
        # 轻提示已发出
        self.assertTrue(any("⏱️" in e and "会话 #1" in e for e in emitted))

    # —— 2. 唤醒时隔未超 idle_ttl_sec → 不分片 ——

    async def test_wake_within_idle_threshold_does_not_split(self):
        """距上次活动未超 idle_ttl_sec 时唤醒 → seq 不变（不触发分片）。"""
        clock = _Clock()
        channel, clock, emitted = self._make_channel(
            idle_ttl_sec=30.0, clock=clock
        )
        channel._bump_activity()
        clock.advance(20.0)  # 20 < 30，未超时

        await self._handle_wake_safe(channel)

        self.assertEqual(channel._session_seq, 0)
        self.assertEqual(channel._current_session, 0)
        # 无分片提示
        self.assertFalse(any("⏱️" in e for e in emitted))

    # —— 3. 连续对讲中 inject_text 不分片 ——

    async def test_continuous_mode_inject_text_does_not_split(self):
        """连续对讲进行中 inject_text 的 _maybe_split_session 短路，不分片。"""
        clock = _Clock()
        channel, clock, _emitted = self._make_channel(
            idle_ttl_sec=30.0, clock=clock
        )
        # 初始活动
        channel._bump_activity()
        clock.advance(100.0)  # 远超 idle_ttl_sec

        # 进入连续对讲
        channel._enter_continuous()
        self.assertTrue(channel._continuous)

        # inject_text：连续对讲中 _maybe_split_session 应短路
        before = channel.bus.inbound_queue.qsize()
        await channel.inject_text("连续对讲中说的话")
        self.assertEqual(channel.bus.inbound_queue.qsize(), before + 1)

        msg = await asyncio.wait_for(channel.bus.inbound_queue.get(), timeout=1)
        # 仍在 session 0，未分片
        self.assertEqual(msg.sender_id, "local:0")
        self.assertEqual(channel._session_seq, 0)
        self.assertEqual(channel._current_session, 0)

        # 清理
        channel._exit_continuous()


if __name__ == "__main__":
    unittest.main()
