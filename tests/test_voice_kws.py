"""TASK-025 voice 渠道唤醒录音 ASR 闭环专项测试。

覆盖四层：
1. ``KwsWakeDetector``：队列满丢帧不阻塞回调、冷却防抖、confirm_hits 连续命中
   才触发、唤醒事件投递（真实 asyncio loop）、唤醒动作进行中合并、PortAudio
   打开失败返回可读错误、stop 清理资源。
2. ``record_audio``：mock sd.rec 返回假 PCM → 校验 WAV 头；PortAudioError
   转 KwsError。
3. ``VoiceChannel`` 唤醒闭环：mock detector 触发唤醒 + mock asr_service →
   ``inject_text`` 收到转写文本、ASR 失败走 ``_emit`` 友好提示、唤醒动作进行中
   合并、detector 启动失败降级空转。
4. ``config.voice`` 深度合并：旧配置只有 enabled → 保留 + 新字段补默认；未知
   字段丢弃；save_config 白名单过滤。

不触发任何真实模型/网络/麦克风（KWS 与录音全部 mock）。
"""

import asyncio
import io
import json
import os
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np
import sounddevice as sd

from config import NanoClawConfig, load_config, save_config
from bus.queue import MessageBus
from channels.voice import VoiceChannel
from voice.asr.base import ASRError
from voice.kws import recorder as recorder_module
from voice.kws.detector import KwsWakeDetector
from voice.kws.errors import KwsError


# —— 假 KWS 实现 ——


class _FakeSpotterStream:
    """每块（每次 accept_waveform）恰好产生一次可解码的假流。"""

    def __init__(self) -> None:
        self.accepted = 0
        self.iterations = 0

    def accept_waveform(self, sample_rate, samples) -> None:
        self.accepted += 1
        self.iterations = 0  # 新块重新允许解码一次


class _FakeSpotter:
    """假 KWS：每块 decode 一次，get_result 按块序号返回预置 outcome。"""

    def __init__(self, outcomes=(), default="miss") -> None:
        self._outcomes = list(outcomes)
        self._default = default

    def create_stream(self):
        return _FakeSpotterStream()

    def is_ready(self, stream) -> bool:
        return stream.iterations < 1

    def decode_stream(self, stream) -> None:
        stream.iterations += 1

    def get_result(self, stream):
        idx = stream.accepted - 1
        outcome = self._outcomes[idx] if 0 <= idx < len(self._outcomes) else self._default
        return "小奈小奈" if outcome == "hit" else None

    def reset_stream(self, stream) -> None:
        pass  # 模拟引擎 reset：不改变本块的解码计数


def _make_detector(tmpdir, spotter=None, **kwargs):
    """构造一个可在测试内直接驱动的 KwsWakeDetector（不真正 start）。"""
    spotter = spotter or _FakeSpotter(default="miss")
    detector = KwsWakeDetector(
        model_dir=Path(tmpdir),
        spotter_factory=lambda *a, **k: spotter,
        **kwargs,
    )
    detector._spotter = spotter
    detector._spotter_stream = spotter.create_stream()
    detector._loop = None  # 由测试按需设置
    return detector


class KwsWakeDetectorTests(unittest.IsolatedAsyncioTestCase):
    # —— 构造校验 ——

    def test_constructor_missing_model_dir_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no-such-model"
            with self.assertRaises(KwsError) as cm:
                KwsWakeDetector(model_dir=missing)
            self.assertIn("KWS 模型目录不存在", str(cm.exception))

    def test_constructor_missing_keywords_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp)
            # 造齐模型文件，让校验走到关键词文件这一步
            (model_dir / "tokens.txt").write_text("t", encoding="utf-8")
            for name in (
                "encoder-epoch-12-avg-2-chunk-16-left-64",
                "decoder-epoch-12-avg-2-chunk-16-left-64",
                "joiner-epoch-12-avg-2-chunk-16-left-64",
            ):
                (model_dir / f"{name}.onnx").write_bytes(b"x")
            with self.assertRaises(KwsError) as cm:
                KwsWakeDetector(model_dir=tmp)
            self.assertIn("关键词文件不存在", str(cm.exception))

    # —— 队列满丢帧：回调绝不阻塞 ——

    def test_queue_full_drops_frames_without_blocking(self):
        detector = _make_detector(tempfile.mkdtemp())
        q = detector._queue
        self.assertEqual(q.maxsize, 64)
        frame = np.zeros((160, 1), dtype=np.int16)
        for _ in range(q.maxsize):
            q.put_nowait(frame)
        # 队列已满：回调必须立即返回并计数 dropped，而不是阻塞/抛错
        detector._audio_callback(frame, 160, None, None)
        self.assertEqual(detector.dropped, 1)
        self.assertEqual(q.qsize(), 64)
        # 有空位时不丢帧
        q.get_nowait()
        detector._audio_callback(frame, 160, None, None)
        self.assertEqual(detector.dropped, 1)
        self.assertEqual(q.qsize(), 64)

    def test_callback_status_only_counts(self):
        detector = _make_detector(tempfile.mkdtemp())
        detector._audio_callback(np.zeros((160, 1), dtype=np.int16), 160, None, "input overflow")
        self.assertEqual(detector.status_events, 1)

    # —— 唤醒投递 / 冷却防抖 / 连续命中（真实 asyncio loop） ——

    async def _start_manual(self, detector, on_wake):
        """手工挂载 loop 与 on_wake，便于直接驱动 _feed_block。"""
        detector._loop = asyncio.get_running_loop()
        detector._on_wake = on_wake

    async def test_wake_event_dispatches_to_loop(self):
        wakes: list = []

        async def on_wake():
            wakes.append("wake")

        with tempfile.TemporaryDirectory() as tmp:
            spotter = _FakeSpotter(outcomes=["hit"])
            detector = _make_detector(tmp, spotter=spotter)
            await self._start_manual(detector, on_wake)
            detector._feed_block(np.zeros((160, 1), dtype=np.int16))
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not wakes:
                await asyncio.sleep(0.005)
            self.assertEqual(wakes, ["wake"])
            self.assertEqual(detector.last_result, "小奈小奈")

    async def test_cooldown_merges_repeated_hits(self):
        wakes: list = []
        clock = {"now": 100.0}

        async def on_wake():
            wakes.append("wake")

        with tempfile.TemporaryDirectory() as tmp:
            spotter = _FakeSpotter(default="hit")  # 每帧都命中
            detector = _make_detector(
                tmp, spotter=spotter, time_fn=lambda: clock["now"], cooldown_sec=2.0
            )
            await self._start_manual(detector, on_wake)
            frame = np.zeros((160, 1), dtype=np.int16)
            detector._feed_block(frame)  # 第一次命中 → 触发
            await asyncio.sleep(0.05)
            self.assertEqual(wakes, ["wake"])
            detector._feed_block(frame)  # 冷却期内再次命中 → 不触发
            await asyncio.sleep(0.05)
            self.assertEqual(wakes, ["wake"])
            self.assertEqual(detector.coalesced_wakes, 0)  # 冷却去重不是动作合并
            clock["now"] += 3.0  # 冷却结束
            detector._feed_block(frame)  # 第三次命中 → 再次触发
            await asyncio.sleep(0.05)
            self.assertEqual(wakes, ["wake", "wake"])

    async def test_confirm_hits_requires_consecutive_hits(self):
        wakes: list = []

        async def on_wake():
            wakes.append("wake")

        with tempfile.TemporaryDirectory() as tmp:
            # 命中、miss、命中、命中：confirm_hits=2 时只有最后一次才触发
            spotter = _FakeSpotter(outcomes=["hit", "miss", "hit", "hit"])
            detector = _make_detector(tmp, spotter=spotter, confirm_hits=2)
            await self._start_manual(detector, on_wake)
            frame = np.zeros((160, 1), dtype=np.int16)
            detector._feed_block(frame)  # hit #1 → streak=1
            await asyncio.sleep(0.05)
            self.assertEqual(wakes, [])
            detector._feed_block(frame)  # miss → streak 清零
            await asyncio.sleep(0.05)
            self.assertEqual(wakes, [])
            detector._feed_block(frame)  # hit #1（再次）
            await asyncio.sleep(0.05)
            self.assertEqual(wakes, [])
            detector._feed_block(frame)  # hit #2 → 触发
            await asyncio.sleep(0.05)
            self.assertEqual(wakes, ["wake"])

    async def test_wake_in_progress_merges_new_event(self):
        wakes: list = []
        release = asyncio.Event()

        async def on_wake():
            wakes.append("wake")
            await release.wait()

        with tempfile.TemporaryDirectory() as tmp:
            spotter = _FakeSpotter(outcomes=["hit", "hit"])
            detector = _make_detector(tmp, spotter=spotter)
            detector._loop = asyncio.get_running_loop()
            detector._on_wake = on_wake
            task1 = asyncio.create_task(detector._dispatch_wake())
            await asyncio.sleep(0.01)  # on_wake 已进入等待
            task2 = asyncio.create_task(detector._dispatch_wake())
            await asyncio.sleep(0.01)
            self.assertEqual(len(wakes), 1)
            self.assertEqual(detector.coalesced_wakes, 1)
            release.set()
            await asyncio.gather(task1, task2)

    # —— start / stop 生命周期 ——

    async def test_start_portaudio_error_returns_readable_error(self):
        def raise_error():
            raise sd.PortAudioError("device busy / no default input")

        with tempfile.TemporaryDirectory() as tmp:
            spotter = _FakeSpotter()
            detector = _make_detector(tmp, spotter=spotter)
            detector._stream_factory = raise_error
            with self.assertRaises(KwsError) as cm:
                await detector.start(on_wake=lambda: None)
            message = str(cm.exception)
            self.assertIn("device busy", message)
            self.assertIn("麦克风", message)  # 权限/设备可读提示
            self.assertFalse(detector.running)
            self.assertIsNone(detector._worker)
            self.assertIsNone(detector._stream)

    async def test_start_stop_cleans_up_stream_and_worker(self):
        class FakeStream:
            def __init__(self) -> None:
                self.started = False
                self.closed = False

            def start(self) -> None:
                self.started = True

            def close(self) -> None:
                self.closed = True

        fake_stream = FakeStream()
        with tempfile.TemporaryDirectory() as tmp:
            spotter = _FakeSpotter()
            detector = _make_detector(tmp, spotter=spotter)
            detector._stream_factory = lambda: fake_stream
            await detector.start(on_wake=lambda: None)
            self.assertTrue(detector.running)
            self.assertIsNotNone(detector._worker)
            await detector.stop()
            self.assertFalse(detector.running)
            self.assertTrue(fake_stream.closed)
            self.assertIsNone(detector._worker)
            self.assertIsNone(detector._queue)
            # 重复 stop 安全
            await detector.stop()

    async def test_stream_factory_stream_is_started(self):
        """回归（TASK-025）：裸构造 InputStream 不自动采集，必须显式 start()。

        demo_kws.py 用 ``with sd.InputStream(...)`` 靠 context manager 自动
        start；集成到 KwsWakeDetector 后是裸构造返回，若不 ``stream.start()``
        则音频回调从不被调用、KWS worker 永远等不到数据（乖宝端到端验收发现
        「小奈小奈」无反应、无任何打印）。注入假 stream：detector.start() 后
        断言 start() 恰好调用一次；stop() 后 close() 被调用（sounddevice
        close 隐含 stop，释放流）。
        """

        class FakeStream:
            def __init__(self) -> None:
                self.started = 0
                self.closed = 0

            def start(self) -> None:
                self.started += 1

            def close(self) -> None:
                self.closed += 1

        fake_stream = FakeStream()
        with tempfile.TemporaryDirectory() as tmp:
            spotter = _FakeSpotter()
            detector = _make_detector(tmp, spotter=spotter)
            detector._stream_factory = lambda: fake_stream
            await detector.start(on_wake=lambda: None)
            self.assertTrue(detector.running)
            self.assertEqual(
                fake_stream.started, 1,
                "detector.start() 后输入流必须已被显式 start()（否则回调不触发）",
            )
            await detector.stop()
            self.assertFalse(detector.running)
            self.assertEqual(
                fake_stream.closed, 1,
                "stop() 后必须 close() 释放流（sounddevice close 隐含 stop）",
            )

    async def test_start_twice_raises(self):
        class FakeStream:
            def start(self) -> None:
                pass

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            detector = _make_detector(tmp)
            detector._stream_factory = lambda: FakeStream()
            await detector.start(on_wake=lambda: None)
            with self.assertRaises(KwsError) as cm:
                await detector.start(on_wake=lambda: None)
            self.assertIn("已在运行", str(cm.exception))
            await detector.stop()


# —— 录音模块 ——


class RecorderTests(unittest.IsolatedAsyncioTestCase):
    async def test_record_returns_valid_wav(self):
        fake = np.zeros((16000, 1), dtype=np.int16)
        with patch.object(recorder_module.sd, "rec", return_value=fake), patch.object(
            recorder_module.sd, "wait", return_value=None
        ):
            data = await recorder_module.record_audio(1.0, sample_rate=16000)
        self.assertTrue(data)
        with wave.open(io.BytesIO(data), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), 16000)
            self.assertEqual(wav.getnframes(), 16000)

    async def test_record_preserves_pcm_samples(self):
        fake = np.array([[0x1234], [0x00FF]], dtype=np.int16)
        with patch.object(recorder_module.sd, "rec", return_value=fake), patch.object(
            recorder_module.sd, "wait", return_value=None
        ):
            data = await recorder_module.record_audio(0.1, sample_rate=16000)
        with wave.open(io.BytesIO(data), "rb") as wav:
            self.assertEqual(wav.getnframes(), 2)
            raw = wav.readframes(2)
        self.assertEqual(raw, fake.reshape(-1).astype("<i2").tobytes())

    async def test_record_portaudio_error_raises_kws_error(self):
        with patch.object(
            recorder_module.sd,
            "rec",
            side_effect=recorder_module.sd.PortAudioError("no mic"),
        ):
            with self.assertRaises(KwsError) as cm:
                await recorder_module.record_audio(1.0)
            self.assertIn("录音失败", str(cm.exception))

    async def test_record_invalid_duration_raises(self):
        with self.assertRaises(KwsError):
            await recorder_module.record_audio(0.0)


# —— voice 渠道唤醒闭环 ——


class _FakeDetector:
    """记录 on_wake / start / stop 调用的假 KWS 检测器。"""

    def __init__(self) -> None:
        self.on_wake = None
        self.started = False
        self.stopped = False
        self.start_error = None

    async def start(self, on_wake=None) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.on_wake = on_wake
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _FakeASR:
    def __init__(self, text="今天天气怎么样"):
        self.text = text
        self.calls = []

    async def transcribe(self, data, *, filename, media_type):
        self.calls.append((data, filename, media_type))
        if isinstance(self.text, Exception):
            raise self.text
        return SimpleNamespace(text=self.text)


class VoiceChannelWakeTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_for(self, cond, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("等待条件超时未满足")

    async def test_wake_records_transcribes_and_injects(self):
        bus = MessageBus()
        detector = _FakeDetector()
        asr = _FakeASR(text="今天天气怎么样")
        channel = VoiceChannel(bus, kws_detector=detector, asr_service=asr, record_sec=2.0)
        emitted: list = []
        channel._reply_sink = emitted.append

        start_task = asyncio.create_task(channel.start())
        await self._wait_for(lambda: detector.started)
        wav = b"RIFF-fake-wav"
        with patch("channels.voice.record_audio", new=AsyncMock(return_value=wav)):
            await detector.on_wake()
        # 转写文本作为 InboundMessage 进入 bus（Agent 消费前可见）
        msg = await asyncio.wait_for(bus.inbound_queue.get(), timeout=1)
        self.assertEqual(msg.channel, "voice")
        self.assertEqual(msg.content, "今天天气怎么样")
        # ASR 收到的是录音 WAV + 约定文件名 / media_type
        self.assertEqual(asr.calls, [(wav, "voice_wake.wav", "audio/wav")])
        # 唤醒启动提示已 emit
        self.assertTrue(any("唤醒监听已启动" in e for e in emitted))
        await channel.stop()
        await asyncio.wait_for(start_task, timeout=1)
        self.assertTrue(detector.stopped)

    async def test_wake_asr_failure_emits_friendly_prompt(self):
        bus = MessageBus()
        detector = _FakeDetector()
        asr = _FakeASR(text=ASRError("provider_unavailable", "语音转写服务暂时不可用。"))
        channel = VoiceChannel(bus, kws_detector=detector, asr_service=asr, record_sec=2.0)
        emitted: list = []
        channel._reply_sink = emitted.append

        start_task = asyncio.create_task(channel.start())
        await self._wait_for(lambda: detector.started)
        with patch("channels.voice.record_audio", new=AsyncMock(return_value=b"wav")):
            await detector.on_wake()
        self.assertTrue(
            any("没听清" in e and "语音转写服务暂时不可用" in e for e in emitted)
        )
        self.assertTrue(bus.inbound_queue.empty())
        await channel.stop()
        await asyncio.wait_for(start_task, timeout=1)

    async def test_wake_without_asr_is_disabled(self):
        bus = MessageBus()
        detector = _FakeDetector()
        channel = VoiceChannel(bus, kws_detector=detector, asr_service=None)  # 链路禁用
        emitted: list = []
        channel._reply_sink = emitted.append

        start_task = asyncio.create_task(channel.start())
        await self._wait_for(lambda: detector.started)
        with patch("channels.voice.record_audio") as mocked:
            await detector.on_wake()
        mocked.assert_not_awaited()  # 未录音 → 未转写 → 无入站
        self.assertTrue(bus.inbound_queue.empty())
        await channel.stop()
        await asyncio.wait_for(start_task, timeout=1)

    async def test_wake_action_in_progress_merges_new_wake(self):
        bus = MessageBus()
        detector = _FakeDetector()
        asr = _FakeASR(text="你好")
        channel = VoiceChannel(bus, kws_detector=detector, asr_service=asr, record_sec=2.0)
        gate = asyncio.Event()

        async def slow_record(*args, **kwargs):
            await gate.wait()
            return b"wav"

        with patch("channels.voice.record_audio", new=slow_record):
            task1 = asyncio.create_task(channel._on_wake())
            await asyncio.sleep(0.02)  # 第一次唤醒处理进行中
            task2 = asyncio.create_task(channel._on_wake())  # 第二次被合并
            await asyncio.sleep(0.02)
            self.assertEqual(channel._coalesced_wakes, 1)
            gate.set()
            await asyncio.gather(task1, task2)
        # 只有一次录音/转写/入站
        msg = await asyncio.wait_for(bus.inbound_queue.get(), timeout=1)
        self.assertEqual(msg.content, "你好")
        self.assertTrue(bus.inbound_queue.empty())

    async def test_start_detector_failure_degrades_to_idle(self):
        bus = MessageBus()
        detector = _FakeDetector()
        detector.start_error = KwsError("mic_error", "打开麦克风失败。")
        channel = VoiceChannel(bus, kws_detector=detector, asr_service=_FakeASR())
        emitted: list = []
        channel._reply_sink = emitted.append
        start_task = asyncio.create_task(channel.start())
        await asyncio.sleep(0.05)
        # 启动失败 → 降级为空转：渠道仍存活，未被意外结束
        self.assertFalse(start_task.done())
        self.assertTrue(any("唤醒未就绪" in e for e in emitted))
        self.assertIsNone(channel._kws_detector)  # 链路已禁用
        await channel.stop()
        await asyncio.wait_for(start_task, timeout=1)

    async def test_full_wake_loop_detector_to_bus(self):
        """真闭环：真实 KwsWakeDetector（注入假 spotter/stream）+ VoiceChannel。

        覆盖 worker 线程 → call_soon_threadsafe → channel._on_wake → 录音 →
        ASR → inject_text → bus 的全链路接线（不触碰真实麦克风/模型）。
        """
        import tempfile as _tmp
        from pathlib import Path as _Path

        bus = MessageBus()
        asr = _FakeASR(text="今天天气怎么样")
        spotter = _FakeSpotter(default="hit")

        class FakeStream:
            def __init__(self) -> None:
                self.started = False
                self.closed = False

            def start(self) -> None:
                self.started = True

            def close(self) -> None:
                self.closed = True

        fake_stream = FakeStream()
        with _tmp.TemporaryDirectory() as tmp:
            detector = KwsWakeDetector(
                model_dir=_Path(tmp),
                spotter_factory=lambda *a, **k: spotter,
                stream_factory=lambda: fake_stream,
                cooldown_sec=0.1,
            )
            channel = VoiceChannel(
                bus, kws_detector=detector, asr_service=asr, record_sec=2.0
            )
            emitted: list = []
            channel._reply_sink = emitted.append
            start_task = asyncio.create_task(channel.start())
            await self._wait_for(lambda: detector.running)
            with patch("channels.voice.record_audio", new=AsyncMock(return_value=b"wav")):
                # 向 detector 队列投喂一帧 → worker 线程推理命中 → 唤醒闭环
                detector._queue.put_nowait(np.zeros((160, 1), dtype=np.int16))
                msg = await asyncio.wait_for(bus.inbound_queue.get(), timeout=2)
            self.assertEqual(msg.channel, "voice")
            self.assertEqual(msg.content, "今天天气怎么样")
            self.assertEqual(asr.calls, [(b"wav", "voice_wake.wav", "audio/wav")])
            self.assertTrue(any("唤醒监听已启动" in e for e in emitted))
            await channel.stop()
            await asyncio.wait_for(start_task, timeout=1)
            self.assertTrue(fake_stream.closed)

    async def test_detector_none_start_idles(self):
        # TASK-024 行为回归：无检测器时空转等待 stop
        bus = MessageBus()
        channel = VoiceChannel(bus)
        start_task = asyncio.create_task(channel.start())
        await asyncio.sleep(0.05)
        self.assertFalse(start_task.done())
        await channel.stop()
        await asyncio.wait_for(start_task, timeout=1)


# —— config.voice 深度合并 ——


class VoiceConfigTests(unittest.TestCase):
    def test_voice_deep_merge_old_config_keeps_enabled_and_fills_defaults(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump({"voice": {"enabled": True}}, file)
            file.flush()
            cfg = load_config(file.name)
        self.assertTrue(cfg.voice["enabled"])
        self.assertEqual(cfg.voice["record_sec"], 8.0)
        kws = cfg.voice["kws"]
        self.assertEqual(
            kws["model_dir"],
            "voice/kws/models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01",
        )
        self.assertEqual(kws["sample_rate"], 16000)
        self.assertEqual(kws["cooldown_sec"], 2.0)
        self.assertEqual(kws["confirm_hits"], 1)
        self.assertIsNone(kws["device"])
        self.assertIs(kws["int8"], False)

    def test_voice_kws_unknown_fields_dropped_and_known_merged(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump(
                {
                    "voice": {
                        "enabled": True,
                        "foo": 1,
                        "record_sec": 5.0,
                        "kws": {"bar": 2, "sample_rate": 8000, "model_dir": "/tmp/x"},
                    }
                },
                file,
            )
            file.flush()
            cfg = load_config(file.name)
        self.assertNotIn("foo", cfg.voice)
        self.assertEqual(cfg.voice["record_sec"], 5.0)
        self.assertNotIn("bar", cfg.voice["kws"])
        self.assertEqual(cfg.voice["kws"]["sample_rate"], 8000)
        self.assertEqual(cfg.voice["kws"]["model_dir"], "/tmp/x")
        # 未配置的 kws 子字段保持默认
        self.assertEqual(cfg.voice["kws"]["cooldown_sec"], 2.0)
        self.assertIsNone(cfg.voice["kws"]["device"])

    def test_voice_non_dict_is_guarded_at_usage_site(self):
        # 非 dict voice 值：load_config 原样透传（与 asr_model/weixin 等 dict 字段
        # 语义一致），main.py / 渠道侧用 isinstance 守卫回退 {}，不崩溃。
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump({"voice": "oops"}, file)
            file.flush()
            cfg = load_config(file.name)
        self.assertEqual(cfg.voice, "oops")
        self.assertEqual({} if not isinstance(cfg.voice, dict) else cfg.voice, {})

    def test_save_config_filters_voice_fields(self):
        cfg = NanoClawConfig()
        cfg.voice["enabled"] = True
        cfg.voice["record_sec"] = 3.0
        cfg.voice["kws"]["sample_rate"] = 8000
        cfg.voice["kws"]["unknown"] = 123
        path = tempfile.mktemp(suffix=".json")
        try:
            save_config(cfg, path)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertTrue(data["voice"]["enabled"])
            self.assertEqual(data["voice"]["record_sec"], 3.0)
            self.assertEqual(data["voice"]["kws"]["sample_rate"], 8000)
            self.assertNotIn("unknown", data["voice"]["kws"])
            self.assertEqual(data["voice"]["kws"]["model_dir"], cfg.voice["kws"]["model_dir"])
        finally:
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    unittest.main()
