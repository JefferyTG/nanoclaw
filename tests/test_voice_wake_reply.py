"""TASK-025 方案 B「唤醒确认回应」专项测试。

覆盖三层：
1. ``voice/kws/player.py``：mock sd.play/sd.wait → ``play_audio`` 调用链正确
   （真实小 WAV bytes 走真实 ffmpeg 解码）；PortAudioError → KwsError；空音频
   与非法音频 → KwsError；mock 解码路径不依赖 ffmpeg。
2. ``VoiceChannel`` 唤醒回应：mock tts_service + mock play_audio → 唤醒时
   **先合成回应再录音**（顺序断言）；tts_service=None / wake_replies 空或非
   list → 跳过回应直接录音；synthesize / play 抛异常 → 降级录音不崩溃。
3. ``config.voice.wake_replies`` 深度合并：默认补全 / 自定义覆盖 / 非 list
   透传且渠道侧容错。

不触发任何真实模型/网络/麦克风/输出设备（播放全部 mock）。
"""

import asyncio
import io
import json
import shutil
import tempfile
import time
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np

from config import load_config
from bus.queue import MessageBus
from channels.voice import VoiceChannel
from voice.kws import player as player_module
from voice.kws.errors import KwsError
from voice.tts.base import TTSError


def _make_wav(sample_rate: int = 24000, frames: int = 2400) -> bytes:
    """生成一段真实的小 WAV（0.1s 440Hz 正弦），供 ffmpeg 真实解码用。"""
    t = np.arange(frames) / sample_rate
    pcm = (np.sin(2 * np.pi * 440.0 * t) * 0.3 * 32767).astype("<i2").tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


_ffmpeg_available = shutil.which("ffmpeg") is not None


# —— 播放模块 ——


class PlayerTests(unittest.IsolatedAsyncioTestCase):
    async def test_play_audio_decodes_wav_and_plays_to_default_device(self):
        wav = _make_wav()
        with patch.object(player_module.sd, "play") as mock_play, patch.object(
            player_module.sd, "wait", return_value=None
        ) as mock_wait:
            await player_module.play_audio(wav, "audio/wav")

        self.assertEqual(mock_wait.call_count, 1)
        # sd.play 收到解码后的 int16 mono 数组 + 24000 采样率 + 系统默认输出
        self.assertEqual(mock_play.call_count, 1)
        args, kwargs = mock_play.call_args
        data = args[0]
        self.assertIsInstance(data, np.ndarray)
        self.assertEqual(data.dtype, np.int16)
        self.assertEqual(data.ndim, 1)
        self.assertEqual(data.size, 2400)  # 0.1s @24k
        self.assertEqual(kwargs.get("samplerate"), 24000)
        self.assertIsNone(kwargs.get("device"))

    async def test_play_audio_custom_sample_rate_and_device(self):
        wav = _make_wav(sample_rate=16000, frames=1600)
        with patch.object(player_module.sd, "play") as mock_play, patch.object(
            player_module.sd, "wait", return_value=None
        ):
            await player_module.play_audio(
                wav, "audio/wav", sample_rate=16000, device=3
            )
        args, kwargs = mock_play.call_args
        self.assertEqual(kwargs.get("samplerate"), 16000)
        self.assertEqual(kwargs.get("device"), 3)

    @unittest.skipUnless(_ffmpeg_available, "ffmpeg 未安装")
    async def test_play_audio_invalid_audio_raises_kws_error(self):
        # 非法音频字节 → ffmpeg 解码失败 → KwsError（可读提示，不暴露命令输出）
        with patch.object(player_module.sd, "play") as mock_play, patch.object(
            player_module.sd, "wait"
        ):
            with self.assertRaises(KwsError) as cm:
                await player_module.play_audio(b"not-a-wav", "audio/wav")
        self.assertIn("无法解码", str(cm.exception))
        mock_play.assert_not_called()

    async def test_play_audio_empty_audio_raises_before_ffmpeg(self):
        with self.assertRaises(KwsError) as cm:
            await player_module.play_audio(b"", "audio/wav")
        self.assertEqual(cm.exception.category, "audio_empty")

    async def test_play_audio_invalid_sample_rate_raises(self):
        with self.assertRaises(KwsError) as cm:
            await player_module.play_audio(b"x", "audio/wav", sample_rate=0)
        self.assertEqual(cm.exception.category, "invalid_rate")

    async def test_play_audio_portaudio_error_raises_kws_error(self):
        wav = _make_wav()
        with patch.object(
            player_module.sd,
            "play",
            side_effect=player_module.sd.PortAudioError("no output device"),
        ):
            with self.assertRaises(KwsError) as cm:
                await player_module.play_audio(wav, "audio/wav")
        self.assertEqual(cm.exception.category, "output_error")
        self.assertIn("回应播放失败", str(cm.exception))
        self.assertIn("输出设备", str(cm.exception))

    async def test_play_audio_mocked_decode_calls_sd_play_wait(self):
        # 不依赖 ffmpeg：mock 解码步骤，验证 sd.play/sd.wait 调用链与 PCM 传参
        fake_pcm = np.zeros((8,), dtype="<i2").tobytes()
        with patch.object(
            player_module,
            "_decode_to_pcm_s16le",
            new=AsyncMock(return_value=fake_pcm),
        ) as mock_decode, patch.object(
            player_module.sd, "play"
        ) as mock_play, patch.object(
            player_module.sd, "wait", return_value=None
        ) as mock_wait:
            await player_module.play_audio(b"whatever", "audio/mpeg")

        mock_decode.assert_awaited_once()
        args, kwargs = mock_decode.call_args
        self.assertEqual(args, (b"whatever", "audio/mpeg"))
        self.assertEqual(mock_play.call_count, 1)
        played = mock_play.call_args.args[0]
        self.assertIsInstance(played, np.ndarray)
        self.assertEqual(played.dtype, np.int16)
        self.assertEqual(played.size, 8)
        self.assertEqual(mock_play.call_args.kwargs.get("samplerate"), 24000)
        self.assertEqual(mock_wait.call_count, 1)


# —— voice 渠道唤醒回应 ——


class _FakeDetector:
    """记录 on_wake / start / stop 调用的假 KWS 检测器。"""

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
    def __init__(self, text="今天天气怎么样") -> None:
        self.text = text

    async def transcribe(self, data, *, filename, media_type):
        return SimpleNamespace(text=self.text)


class VoiceWakeReplyTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_for(self, cond, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("等待条件超时未满足")

    async def _run_wake(self, channel, events):
        """挂起 detector 的 on_wake（已 patch play_audio / record_audio）。"""

        async def _fake_play(audio, media_type, *args, **kwargs):
            events.append("play")

        async def _fake_record(*args, **kwargs):
            events.append("record")
            return b"wav"

        detector = channel._kws_detector
        start_task = asyncio.create_task(channel.start())
        await self._wait_for(lambda: detector.started)
        with patch("channels.voice.play_audio", new=_fake_play), patch(
            "channels.voice.record_audio", new=_fake_record
        ):
            await detector.on_wake()
        await channel.stop()
        await asyncio.wait_for(start_task, timeout=1)

    async def test_wake_synthesizes_reply_then_records(self):
        """方案 B 核心：先合成回应 → 播完 → 才录音（顺序断言）。"""
        bus = MessageBus()
        detector = _FakeDetector()
        events: list = []

        class _TTS:
            async def synthesize(self, text):
                events.append("synthesize")
                return SimpleNamespace(audio=b"audio", media_type="audio/wav")

        channel = VoiceChannel(
            bus,
            kws_detector=detector,
            asr_service=_FakeASR(),
            record_sec=2.0,
            tts_service=_TTS(),
            wake_replies=["哎，我在呢，你说吧"],
        )
        await self._run_wake(channel, events)
        self.assertEqual(events, ["synthesize", "play", "record"])
        msg = await asyncio.wait_for(bus.inbound_queue.get(), timeout=1)
        self.assertEqual(msg.content, "今天天气怎么样")
        self.assertTrue(bus.inbound_queue.empty())

    async def test_wake_reply_text_randomly_chosen_from_list(self):
        """回应文本由 random.choice 从列表随机挑选，后续加条目即自动随机。"""
        bus = MessageBus()
        detector = _FakeDetector()
        events: list = []
        synthesized: list = []

        class _TTS:
            async def synthesize(self, text):
                synthesized.append(text)
                events.append("synthesize")
                return SimpleNamespace(audio=b"audio", media_type="audio/wav")

        channel = VoiceChannel(
            bus,
            kws_detector=detector,
            asr_service=_FakeASR(),
            record_sec=2.0,
            tts_service=_TTS(),
            wake_replies=["第一句", "第二句"],
        )
        with patch(
            "channels.voice.random.choice",
            side_effect=lambda seq: seq[1],
        ) as mock_choice:
            await self._run_wake(channel, events)
        # choice 收到完整列表；synthesize 收到被选中的那条
        self.assertEqual(mock_choice.call_args.args[0], ["第一句", "第二句"])
        self.assertEqual(synthesized, ["第二句"])
        self.assertEqual(events, ["synthesize", "play", "record"])

    async def test_wake_without_tts_skips_reply_and_records(self):
        """tts_service=None → 跳过回应直接录音（向后兼容既有行为）。"""
        bus = MessageBus()
        detector = _FakeDetector()
        events: list = []
        channel = VoiceChannel(
            bus, kws_detector=detector, asr_service=_FakeASR(), record_sec=2.0
        )
        await self._run_wake(channel, events)
        self.assertEqual(events, ["record"])  # 无 synthesize / play

    async def test_wake_empty_replies_skips_reply(self):
        bus = MessageBus()
        detector = _FakeDetector()
        events: list = []

        class _TTS:
            async def synthesize(self, text):
                events.append("synthesize")
                return SimpleNamespace(audio=b"a", media_type="audio/wav")

        channel = VoiceChannel(
            bus,
            kws_detector=detector,
            asr_service=_FakeASR(),
            record_sec=2.0,
            tts_service=_TTS(),
            wake_replies=[],
        )
        await self._run_wake(channel, events)
        self.assertEqual(events, ["record"])

    async def test_wake_non_list_replies_skips_reply(self):
        bus = MessageBus()
        detector = _FakeDetector()
        events: list = []

        class _TTS:
            async def synthesize(self, text):
                events.append("synthesize")
                return SimpleNamespace(audio=b"a", media_type="audio/wav")

        channel = VoiceChannel(
            bus,
            kws_detector=detector,
            asr_service=_FakeASR(),
            record_sec=2.0,
            tts_service=_TTS(),
            wake_replies="not-a-list",  # 非法配置：渠道侧容错
        )
        await self._run_wake(channel, events)
        self.assertEqual(events, ["record"])

    async def test_wake_synthesize_failure_degrades_to_record(self):
        """合成抛 TTSError → 降级跳过回应直接录音，不崩溃、入站不丢。"""
        bus = MessageBus()
        detector = _FakeDetector()
        emitted: list = []
        events: list = []

        class _TTS:
            async def synthesize(self, text):
                events.append("synthesize")
                raise TTSError("provider_failed", "语音合成服务暂时不可用。")

        channel = VoiceChannel(
            bus,
            kws_detector=detector,
            asr_service=_FakeASR(),
            record_sec=2.0,
            tts_service=_TTS(),
            wake_replies=["哎，我在呢，你说吧"],
        )
        channel._reply_sink = emitted.append
        await self._run_wake(channel, events)
        self.assertEqual(events, ["synthesize", "record"])
        self.assertTrue(any("🔇 回应播放失败" in e for e in emitted))
        msg = await asyncio.wait_for(bus.inbound_queue.get(), timeout=1)
        self.assertEqual(msg.content, "今天天气怎么样")

    async def test_wake_play_failure_degrades_to_record(self):
        """播放抛 KwsError → 降级跳过回应继续录音，轻提示不阻塞唤醒。"""
        bus = MessageBus()
        detector = _FakeDetector()
        emitted: list = []
        events: list = []

        class _TTS:
            async def synthesize(self, text):
                events.append("synthesize")
                return SimpleNamespace(audio=b"audio", media_type="audio/wav")

        async def _fake_play(audio, media_type, *args, **kwargs):
            events.append("play")
            raise KwsError("output_error", "回应播放失败。")

        async def _fake_record(*args, **kwargs):
            events.append("record")
            return b"wav"

        channel = VoiceChannel(
            bus,
            kws_detector=detector,
            asr_service=_FakeASR(),
            record_sec=2.0,
            tts_service=_TTS(),
            wake_replies=["哎，我在呢，你说吧"],
        )
        channel._reply_sink = emitted.append
        start_task = asyncio.create_task(channel.start())
        await self._wait_for(lambda: detector.started)
        with patch("channels.voice.play_audio", new=_fake_play), patch(
            "channels.voice.record_audio", new=_fake_record
        ):
            await detector.on_wake()
        await channel.stop()
        await asyncio.wait_for(start_task, timeout=1)
        self.assertEqual(events, ["synthesize", "play", "record"])
        self.assertTrue(any("🔇 回应播放失败" in e for e in emitted))
        msg = await asyncio.wait_for(bus.inbound_queue.get(), timeout=1)
        self.assertEqual(msg.content, "今天天气怎么样")


# —— config.voice.wake_replies 深度合并 ——


class WakeRepliesConfigTests(unittest.TestCase):
    def test_wake_replies_default_filled_for_old_config(self):
        # 旧 config.json 只有 voice.enabled → wake_replies 自动补默认
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump({"voice": {"enabled": True}}, file)
            file.flush()
            cfg = load_config(file.name)
        self.assertEqual(cfg.voice["wake_replies"], ["哎，我在呢，你说吧"])

    def test_wake_replies_custom_merged(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump(
                {
                    "voice": {
                        "enabled": True,
                        "wake_replies": ["第一句", "第二句"],
                    }
                },
                file,
            )
            file.flush()
            cfg = load_config(file.name)
        self.assertEqual(cfg.voice["wake_replies"], ["第一句", "第二句"])
        # 未配置字段保持默认
        self.assertEqual(cfg.voice["record_sec"], 8.0)

    def test_wake_replies_non_list_passthrough_is_tolerated(self):
        # 非 list 值 load_config 原样透传（与 voice 其它字段语义一致），
        # 渠道侧 isinstance 守卫跳过回应，不崩溃（见渠道测试）。
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump(
                {"voice": {"enabled": True, "wake_replies": "oops"}}, file
            )
            file.flush()
            cfg = load_config(file.name)
        self.assertEqual(cfg.voice["wake_replies"], "oops")


if __name__ == "__main__":
    unittest.main()
