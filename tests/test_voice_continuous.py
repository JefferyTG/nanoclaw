"""TASK-027 第二步「voice 渠道连续对讲状态机」专项测试。

覆盖 ``VoiceChannel`` 的连续对讲状态机（全部 mock record_audio_vad /
play_audio，不触发真实麦克风/模型/网络）：

1. 连续对讲循环：唤醒 → 播回应 → 进入连续对讲 → VAD 录音（有人声）→ ASR →
   inject_text → Agent 回复经 send() 播完 → **自动调度下一轮录音**（断言第二轮
   record_audio_vad 被调用）；
2. [END] 结束语：send() 收到含 [END] 的文本 → **TTS 合成前剥离标记**（播出的告别
   语不含标记）→ 播完退出连续对讲（不再启动下一轮录音）；纯文字兜底路径同样剥离
   并退出；剥离后为空 → 不播任何东西直接退出；
3. 静默退出：VAD 返回 is_silent=True 累计超 ``silence_timeout_sec`` → 退出连续
   模式（标志清除 / 轻提示发出 / 静默累计清零）；未超时继续听；有人声轮次清零
   静默累计（影响后续退出所需轮数）；
4. 分片暂停：连续对讲进行中 ``_maybe_split_session`` 不动作（假时钟推进验证），
   退出后恢复 TASK-026 分片逻辑；
5. 防重入：``_schedule_next_listen`` 在已有轮次运行时重复调用不重复创建任务；
   send() 在非连续对讲模式不调度下一轮（TASK-026 语义回归）。

回归：现有 voice 渠道测试（test_voice_channel / test_voice_idle_split /
test_voice_tts_reply / test_voice_wake_reply / test_voice_kws / test_voice_vad /
test_voice_prune）不得失败（由 discover 全量回归验证）。
"""

import asyncio
import io
import time
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bus.queue import MessageBus, OutboundMessage
from channels.voice import VoiceChannel


def _outbound(text: str) -> OutboundMessage:
    return OutboundMessage(channel="voice", chat_id="direct", content=text)


def _silent_wav(duration_sec: float, sample_rate: int = 16000) -> bytes:
    """生成一段纯静音 WAV（时长 = duration_sec），供静默轮次累计时长用。"""
    frames = int(duration_sec * sample_rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


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
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.text)


class _FakeTTS:
    def __init__(self, audio=b"audio", media_type="audio/wav", error=None):
        self.audio = audio
        self.media_type = media_type
        self.error = error
        self.synthesized: list = []

    async def synthesize(self, text):
        self.synthesized.append(text)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(audio=self.audio, media_type=self.media_type)


class VoiceContinuousTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_for(self, cond, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("等待条件超时未满足")

    async def _inject(self, voice, text):
        """注入文本并取回对应 InboundMessage；命令/空文本不进 bus 时返回 None。"""
        before = voice.bus.inbound_queue.qsize()
        await voice.inject_text(text)
        if voice.bus.inbound_queue.qsize() == before:
            return None
        return await asyncio.wait_for(voice.bus.inbound_queue.get(), timeout=1)

    # —— 1. 连续对讲循环 ——

    async def test_continuous_mode_round_trip_auto_schedules_next_round(self):
        """唤醒 → 首轮录音(有人声) → inject_text → send() 播完 → 自动第二轮录音。"""
        bus = MessageBus()
        detector = _FakeDetector()
        asr = _FakeASR(text="今天天气怎么样")
        tts = _FakeTTS()
        channel = VoiceChannel(
            bus,
            kws_detector=detector,
            asr_service=asr,
            record_sec=2.0,
            record_delay_sec=0.0,
            tts_service=tts,
            wake_replies=["哎，我在呢，你说吧"],
            wake_replies_dir="/nonexistent/",
        )
        emitted: list = []
        channel._reply_sink = emitted.append
        wav = b"RIFF-voice"
        with patch(
            "channels.voice.record_audio_vad",
            new=AsyncMock(return_value=(wav, False)),
        ) as mock_vad, patch(
            "channels.voice.play_audio", new=AsyncMock()
        ) as mock_play:
            await channel._handle_wake()  # 播回应 → 进入连续对讲 → 首轮监听
            self.assertTrue(channel._continuous)

            # 首轮：VAD 录音 → ASR → inject_text → 入站
            msg = await asyncio.wait_for(bus.inbound_queue.get(), timeout=1)
            self.assertEqual(msg.content, "今天天气怎么样")
            self.assertEqual(mock_vad.await_count, 1)
            self.assertEqual(asr.calls, [(wav, "voice_round.wav", "audio/wav")])

            # 模拟 Agent 回复经 send() 播完 → 自动调度下一轮录音
            await channel.send(_outbound("你好呀"))
            mock_play.assert_awaited()
            self.assertEqual(tts.synthesized, ["哎，我在呢，你说吧", "你好呀"])
            await self._wait_for(lambda: mock_vad.await_count >= 2)  # 第二轮已启动
        # 收尾：退出连续对讲，避免残留后台任务
        channel._exit_continuous()
        self.assertEqual(emitted, [])  # 全程无降级/错误提示

    # —— 2. [END] 结束语 ——

    async def test_send_strips_end_marker_plays_goodbye_and_exits(self):
        """[END] 在 TTS 合成前剥离；播出的告别语不含标记；播完退出不续听。"""
        bus = MessageBus()
        tts = _FakeTTS()
        channel = VoiceChannel(bus, tts_service=tts)
        emitted: list = []
        channel._reply_sink = emitted.append
        channel._enter_continuous()
        with patch("channels.voice.record_audio_vad", new=AsyncMock()) as mock_vad, patch(
            "channels.voice.play_audio", new=AsyncMock()
        ) as mock_play:
            await channel.send(_outbound("拜拜啦，下次聊 [END]"))
        self.assertEqual(tts.synthesized, ["拜拜啦，下次聊"])  # 剥离标记后合成
        mock_play.assert_awaited_once_with(
            b"audio", "audio/wav", playback_params={}
        )
        self.assertFalse(channel._continuous)  # 播完退出连续对讲回待唤醒
        self.assertEqual(mock_vad.await_count, 0)  # 不再触发下一轮录音
        self.assertEqual(emitted, [])  # 音频路径不 _emit 文字

    async def test_send_end_marker_at_head_is_stripped(self):
        """标记出现在开头同样被剥离（（末尾或含）[END]）。"""
        bus = MessageBus()
        tts = _FakeTTS()
        channel = VoiceChannel(bus, tts_service=tts)
        channel._enter_continuous()
        with patch("channels.voice.record_audio_vad", new=AsyncMock()) as mock_vad, patch(
            "channels.voice.play_audio", new=AsyncMock()
        ):
            await channel.send(_outbound("[END]拜拜，下次再聊"))
        self.assertEqual(tts.synthesized, ["拜拜，下次再聊"])
        self.assertFalse(channel._continuous)
        self.assertEqual(mock_vad.await_count, 0)

    async def test_send_end_marker_only_exits_without_playing(self):
        """剥离后为空：不播放任何东西直接退出（合成/播放都不触发）。"""
        bus = MessageBus()
        tts = _FakeTTS()
        channel = VoiceChannel(bus, tts_service=tts)
        channel._enter_continuous()
        with patch("channels.voice.record_audio_vad", new=AsyncMock()) as mock_vad, patch(
            "channels.voice.play_audio", new=AsyncMock()
        ) as mock_play:
            await channel.send(_outbound("[END]"))
        self.assertEqual(tts.synthesized, [])
        mock_play.assert_not_awaited()
        self.assertFalse(channel._continuous)
        self.assertEqual(mock_vad.await_count, 0)

    async def test_send_end_marker_stripped_in_text_fallback(self):
        """纯文字兜底路径（tts 未配置）同样剥离标记并退出。"""
        bus = MessageBus()
        channel = VoiceChannel(bus)  # tts_service=None → 文字兜底
        emitted: list = []
        channel._reply_sink = emitted.append
        channel._enter_continuous()
        with patch("channels.voice.record_audio_vad", new=AsyncMock()) as mock_vad:
            await channel.send(_outbound("拜拜啦[END]"))
        self.assertEqual(emitted, ["拜拜啦"])  # 标记绝不出现在文字内容里
        self.assertFalse(channel._continuous)
        self.assertEqual(mock_vad.await_count, 0)

    async def test_send_end_marker_outside_continuous_strips_but_no_extra_effect(self):
        """非连续对讲模式收到 [END]：只剥离播放，不调度也不产生副作用。"""
        bus = MessageBus()
        tts = _FakeTTS()
        channel = VoiceChannel(bus, tts_service=tts)
        with patch("channels.voice.record_audio_vad", new=AsyncMock()) as mock_vad, patch(
            "channels.voice.play_audio", new=AsyncMock()
        ):
            await channel.send(_outbound("再见[END]"))
        self.assertEqual(tts.synthesized, ["再见"])
        self.assertFalse(channel._continuous)  # 本来就不在连续对讲
        self.assertEqual(mock_vad.await_count, 0)

    # —— 3. 静默退出 ——

    async def test_silence_timeout_exits_continuous_mode(self):
        """is_silent=True 累计 ≥ silence_timeout_sec → 退出回待唤醒 + 轻提示。"""
        bus = MessageBus()
        detector = _FakeDetector()
        asr = _FakeASR()
        channel = VoiceChannel(
            bus,
            kws_detector=detector,
            asr_service=asr,
            record_sec=2.0,
            record_delay_sec=0.0,
            silence_timeout_sec=5.0,
        )
        emitted: list = []
        channel._reply_sink = emitted.append
        silent = _silent_wav(2.0)  # 每轮静默实际时长 2s
        with patch(
            "channels.voice.record_audio_vad",
            new=AsyncMock(return_value=(silent, True)),
        ) as mock_vad:
            await channel._handle_wake()
            # 轮1 2s < 5 → 轮2 4s < 5 → 轮3 6s ≥ 5 → 退出回待唤醒
            await self._wait_for(lambda: not channel._continuous)
            await self._wait_for(lambda: channel._listen_task is None)
        self.assertEqual(mock_vad.await_count, 3)
        self.assertTrue(any("待机" in e for e in emitted))
        self.assertFalse(channel._continuous)
        self.assertEqual(channel._silence_accum_sec, 0.0)  # 退出时静默累计清零

    async def test_silence_below_timeout_keeps_listening(self):
        """静默累计未超阈值 → 继续下一轮监听，不退出。"""
        bus = MessageBus()
        detector = _FakeDetector()
        asr = _FakeASR()
        channel = VoiceChannel(
            bus,
            kws_detector=detector,
            asr_service=asr,
            record_sec=2.0,
            record_delay_sec=0.0,
            silence_timeout_sec=10**6,  # 阈值极大：多轮静默也远达不到退出
        )
        silent = _silent_wav(2.0)
        with patch(
            "channels.voice.record_audio_vad",
            new=AsyncMock(return_value=(silent, True)),
        ) as mock_vad:
            await channel._handle_wake()
            await self._wait_for(lambda: mock_vad.await_count >= 2)  # 第二轮已启动
            self.assertTrue(channel._continuous)  # 累计 4s < 100s，仍在连续对讲
            self.assertGreaterEqual(channel._silence_accum_sec, 2.0)
            # 收尾：主动退出并回收后台轮次任务，避免无限续听
            task = channel._listen_task
            channel._exit_continuous()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(channel._continuous)

    async def test_voice_round_resets_silence_accumulation(self):
        """有人声轮次清零静默累计：清零与否影响退出所需的录音轮数。"""
        bus = MessageBus()
        detector = _FakeDetector()
        asr = _FakeASR(text="说句话")
        tts = _FakeTTS()
        channel = VoiceChannel(
            bus,
            kws_detector=detector,
            asr_service=asr,
            record_sec=2.0,
            record_delay_sec=0.0,
            silence_timeout_sec=3.0,  # 1s×3 = 3 ≥ 3 才退出
            tts_service=tts,
            wake_replies=["哎，我在呢，你说吧"],
            wake_replies_dir="/nonexistent/",
        )
        emitted: list = []
        channel._reply_sink = emitted.append
        silent = _silent_wav(1.0)  # 1s 静音轮
        results = [
            (silent, True),          # 轮1：静默 1s → 累计 1.0
            (b"RIFF-voice", False),  # 轮2：有人声 → 清零 + 入站
            (silent, True), (silent, True), (silent, True),  # 轮3~5：再累计 3s
        ]
        calls: list = []

        async def fake_vad(*args, **kwargs):
            calls.append(1)
            return results.pop(0) if results else (silent, True)

        with patch("channels.voice.record_audio_vad", side_effect=fake_vad), patch(
            "channels.voice.play_audio", new=AsyncMock()
        ):
            await channel._handle_wake()
            # 轮1 静默 1s → 累计 1.0；轮2 有人声 → 清零 + 入站
            msg = await asyncio.wait_for(bus.inbound_queue.get(), timeout=1)
            self.assertEqual(msg.content, "说句话")
            self.assertEqual(channel._silence_accum_sec, 0.0)  # 有人声已清零
            # 模拟 Agent 回复 → send() → 轮3 起继续静默累计
            await channel.send(_outbound("好的"))
            # 清零生效 → 需再 3 轮静默（1+1+1=3 ≥ 3）退出 → 共 5 次录音
            await self._wait_for(lambda: not channel._continuous)
            await self._wait_for(lambda: channel._listen_task is None)
        self.assertEqual(len(calls), 5)
        self.assertTrue(any("待机" in e for e in emitted))

    # —— 4. 空闲分片暂停 ——

    async def test_continuous_mode_suppresses_idle_split_and_restores(self):
        """连续对讲期间不分片；退出后超时照旧分片（TASK-026 恢复）。"""
        clock = _Clock()
        bus = MessageBus()
        channel = VoiceChannel(bus, idle_ttl_sec=30.0, now_fn=clock)
        emitted: list = []
        channel._reply_sink = emitted.append

        msg0 = await self._inject(channel, "你好")
        self.assertEqual(msg0.sender_id, "local:0")

        # 进入连续对讲：即使远超 idle_ttl 也不分片
        channel._enter_continuous()
        clock.advance(100)
        msg1 = await self._inject(channel, "连续对讲中")
        self.assertEqual(msg1.sender_id, "local:0")
        self.assertEqual(channel._current_session, 0)

        # 退出连续对讲后恢复空闲分片
        channel._exit_continuous()
        clock.advance(100)
        msg2 = await self._inject(channel, "退出之后")
        self.assertEqual(msg2.sender_id, "local:1")
        self.assertTrue(any("⏱️" in e and "会话 #1" in e for e in emitted))

    def test_maybe_split_session_short_circuits_when_continuous(self):
        """同步断言：连续对讲中 _maybe_split_session 直接 return，退出后恢复。"""
        clock = _Clock()
        bus = MessageBus()
        channel = VoiceChannel(bus, idle_ttl_sec=30.0, now_fn=clock)
        channel._bump_activity()  # t = 1000
        clock.advance(1000)
        channel._enter_continuous()
        channel._maybe_split_session()
        self.assertEqual(channel._current_session, 0)  # 连续对讲中未分片
        channel._exit_continuous()
        channel._maybe_split_session()
        self.assertEqual(channel._current_session, 1)  # 退出后恢复分片

    # —— 5. 防重入 & 语义回归 ——

    async def test_schedule_next_listen_prevents_duplicate_rounds(self):
        """连续对讲中 _schedule_next_listen 重复调用只真正启动一个轮次。"""
        bus = MessageBus()
        channel = VoiceChannel(
            bus,
            asr_service=_FakeASR(text="你好"),
            record_sec=2.0,
            record_delay_sec=0.0,
        )
        channel._enter_continuous()
        gate = asyncio.Event()
        calls: list = []

        async def gated(*args, **kwargs):
            calls.append(1)
            await gate.wait()
            return (b"wav", False)

        with patch("channels.voice.record_audio_vad", new=gated):
            channel._schedule_next_listen()
            channel._schedule_next_listen()  # 防重入：已有轮次 → 不重复创建
            channel._schedule_next_listen()
            await asyncio.sleep(0.02)
            self.assertEqual(len(calls), 1)  # 只有一个轮次真正启动录音
            task = channel._listen_task
            self.assertIsNotNone(task)
            self.assertFalse(task.done())
            # 轮次仍在跑时再调度 → 仍不重复
            channel._schedule_next_listen()
            await asyncio.sleep(0.02)
            self.assertEqual(len(calls), 1)
            gate.set()
            await asyncio.wait_for(asyncio.shield(task), timeout=1)
        self.assertIsNone(channel._listen_task)  # 轮次结束清引用
        channel._exit_continuous()

    async def test_send_outside_continuous_does_not_schedule_next_round(self):
        """TASK-026 语义回归：非连续对讲模式下 send() 播完不调度下一轮录音。"""
        bus = MessageBus()
        tts = _FakeTTS()
        channel = VoiceChannel(bus, tts_service=tts)
        with patch("channels.voice.record_audio_vad", new=AsyncMock()) as mock_vad, patch(
            "channels.voice.play_audio", new=AsyncMock()
        ):
            await channel.send(_outbound("普通回复"))
        self.assertEqual(tts.synthesized, ["普通回复"])
        self.assertFalse(channel._continuous)
        self.assertEqual(mock_vad.await_count, 0)

    # —— 构造参数与工具方法 ——

    def test_strip_end_marker_helper(self):
        self.assertEqual(VoiceChannel._strip_end_marker("拜拜 [END]"), ("拜拜", True))
        self.assertEqual(VoiceChannel._strip_end_marker("前缀[END]后缀"), ("前缀后缀", True))
        self.assertEqual(VoiceChannel._strip_end_marker("[END]"), ("", True))
        self.assertEqual(VoiceChannel._strip_end_marker("没有标记"), ("没有标记", False))
        self.assertEqual(VoiceChannel._strip_end_marker(""), ("", False))

    def test_constructor_new_params_defaults(self):
        """record_delay_sec / silence_timeout_sec / vad_params 默认值。"""
        channel = VoiceChannel(MessageBus())
        self.assertEqual(channel._record_delay_sec, 0.5)
        self.assertEqual(channel._silence_timeout_sec, 5.0)
        self.assertEqual(channel._vad_params, {})
        self.assertFalse(channel._continuous)
        self.assertIsNone(channel._listen_task)

    def test_constructor_normalizes_params(self):
        """非法参数回退默认；vad_params 过滤渠道级字段。"""
        channel = VoiceChannel(
            MessageBus(),
            record_delay_sec=-1,  # 负值 → 0
            silence_timeout_sec=None,  # → 默认 5.0
            vad_params={
                "energy_threshold": 800.0,
                "silence_end_sec": 0.8,
                "device": 99,  # 渠道级字段应被过滤，避免关键字冲突
                "max_duration_sec": 999,
            },
        )
        self.assertEqual(channel._record_delay_sec, 0.0)
        self.assertEqual(channel._silence_timeout_sec, 5.0)
        self.assertEqual(
            channel._vad_params,
            {"energy_threshold": 800.0, "silence_end_sec": 0.8},
        )


if __name__ == "__main__":
    unittest.main()
