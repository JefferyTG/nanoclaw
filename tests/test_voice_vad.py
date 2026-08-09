"""TASK-027 静音检测 VAD 流式录音专项测试。

覆盖（全部 mock ``sounddevice.InputStream``，不开真实麦克风，不触发网络/模型）：

1. 有声 + 停顿：说完话停顿超过 ``silence_end_sec`` → 提前结束（wav 时长 <
   ``max_duration_sec``）且 ``is_silent=False``；
2. 全程静音：无人声 → 录满 ``max_duration_sec``，``is_silent=True``；
3. 持续有声：到 ``max_duration_sec`` 无静默 → 正常返回，``is_silent=False``；
4. 短促噪音：人声总时长 < ``min_voice_sec`` → ``is_silent=True``（防误判）；
5. 错误处理：InputStream 抛 PortAudioError → ``KwsError("mic_error")``；
   非法时长 → ``KwsError("invalid_duration")``；
6. 非法 silence_end_sec / min_voice_sec / energy_threshold 回退默认不崩。

用 numpy 合成「人声段（大振幅正弦波）+ 静音段（全零）」的帧序列，通过假
InputStream 按顺序同步投递；``record_audio_vad`` 的采集循环跑在
``asyncio.to_thread`` 里，对假流同样成立。
"""

import io
import unittest
import wave
from contextlib import contextmanager
from unittest.mock import patch

import numpy as np

from voice.kws import vad as vad_module
from voice.kws.errors import KwsError

SAMPLE_RATE = 16000
BLOCK_SEC = 0.05
BLOCK_SIZE = int(BLOCK_SEC * SAMPLE_RATE)  # 800 采样/帧


def _sine_block(amplitude: int = 8000) -> np.ndarray:
    """一帧 220Hz 大振幅正弦波（模拟人声），shape (BLOCK_SIZE, 1) int16。

    8000 振幅的 RMS ≈ 8000 / sqrt(2) ≈ 5657，远超默认能量阈值 400。
    """
    t = np.arange(BLOCK_SIZE) / SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * 220.0 * t)).astype(np.int16).reshape(-1, 1)


def _zero_block() -> np.ndarray:
    """一帧全零静音。"""
    return np.zeros((BLOCK_SIZE, 1), dtype=np.int16)


class _FakeInputStream:
    """模拟 sd.InputStream：``__enter__`` 时按顺序同步投递全部预置帧给 callback。

    callback 签名与 sounddevice 一致：``callback(indata, frames, time_info, status)``。
    同步投递意味着队列会先装满再被消费者循环按序处理，对状态机结果无影响。
    """

    def __init__(self, samplerate, blocksize, channels, dtype, device, callback):
        self.callback = callback
        self.blocksize = blocksize
        self.samplerate = samplerate
        self.device = device
        self.blocks: list[np.ndarray] = []
        self.entered = False

    def __enter__(self):
        self.entered = True
        for block in self.blocks:
            self.callback(block, block.shape[0], None, None)
        return self

    def __exit__(self, *exc):
        return False


@contextmanager
def _mock_stream(blocks):
    """把 vad 模块里的 ``sd.InputStream`` 换成投递给定帧序列的假流。

    用 side_effect 工厂把 vad 实际传入的参数（含真实 callback）透传给假流，
    再注入预置帧序列。
    """
    created: list[_FakeInputStream] = []

    def factory(samplerate, blocksize, channels, dtype, device, callback):
        fake = _FakeInputStream(samplerate, blocksize, channels, dtype, device, callback)
        fake.blocks = list(blocks)
        created.append(fake)
        return fake

    with patch.object(vad_module.sd, "InputStream", side_effect=factory):
        yield created[0] if created else None


class VADRecorderTests(unittest.IsolatedAsyncioTestCase):
    MAX_SEC = 5.0
    SILENCE_END_SEC = 1.2
    MIN_VOICE_SEC = 0.3
    THRESHOLD = 400.0

    async def _run(self, blocks, **kwargs):
        with _mock_stream(blocks):
            return await vad_module.record_audio_vad(
                self.MAX_SEC, sample_rate=SAMPLE_RATE, **kwargs
            )

    def _frames(self, wav: bytes) -> int:
        with wave.open(io.BytesIO(wav), "rb") as w:
            return w.getnframes()

    async def test_voice_then_silence_ends_early(self):
        # 1.0s 人声 + 1.5s 静音（> silence_end_sec 1.2s）→ 提前结束，wav < max
        blocks = [_sine_block()] * 20 + [_zero_block()] * 30
        wav, is_silent = await self._run(blocks)
        self.assertFalse(is_silent)
        self.assertIsNotNone(wav)
        with wave.open(io.BytesIO(wav), "rb") as w:
            self.assertEqual(w.getnchannels(), 1)
            self.assertEqual(w.getsampwidth(), 2)
            self.assertEqual(w.getframerate(), SAMPLE_RATE)
        frames = self._frames(wav)
        # 20 块人声 + 24 块静音（1.2s）即提前结束，共 2.2s << 5.0s
        self.assertLess(frames / SAMPLE_RATE, self.MAX_SEC)
        self.assertEqual(frames, (20 + 24) * BLOCK_SIZE)

    async def test_all_silence_is_silent(self):
        # 5.0s 全静音：不会提前结束，录满上限，is_silent=True
        blocks = [_zero_block()] * 100
        wav, is_silent = await self._run(blocks)
        self.assertTrue(is_silent)
        self.assertIsNotNone(wav)
        self.assertEqual(self._frames(wav), 100 * BLOCK_SIZE)

    async def test_continuous_voice_hits_max_duration(self):
        # 5.0s 持续有声、无静默：录满上限正常返回，is_silent=False
        blocks = [_sine_block()] * 100
        wav, is_silent = await self._run(blocks)
        self.assertFalse(is_silent)
        self.assertIsNotNone(wav)
        self.assertEqual(self._frames(wav), 100 * BLOCK_SIZE)

    async def test_short_noise_is_silent(self):
        # 0.1s 短促噪音 + 静音：会提前结束，但人声总时长 < min_voice_sec → 判静默
        blocks = [_sine_block()] * 2 + [_zero_block()] * 30
        wav, is_silent = await self._run(blocks)
        self.assertTrue(is_silent)
        self.assertIsNotNone(wav)
        self.assertLess(self._frames(wav) / SAMPLE_RATE, self.MAX_SEC)

    async def test_voice_plus_pause_then_voice_resets_silence(self):
        # 人声 → 停顿 0.5s（< silence_end_sec）→ 再人声：不提前结束，is_silent=False
        blocks = (
            [_sine_block()] * 10
            + [_zero_block()] * 10  # 0.5s
            + [_sine_block()] * 10
            + [_zero_block()] * 30
        )
        wav, is_silent = await self._run(blocks)
        self.assertFalse(is_silent)
        # 10 + 10 + 10 + 24 = 54 块 = 2.7s < 5.0s（第二段之后的静音触发提前结束）
        self.assertEqual(self._frames(wav), (10 + 10 + 10 + 24) * BLOCK_SIZE)

    async def test_portaudio_error_raises_kws_error(self):
        with patch.object(
            vad_module.sd,
            "InputStream",
            side_effect=vad_module.sd.PortAudioError("no mic"),
        ):
            with self.assertRaises(KwsError) as cm:
                await vad_module.record_audio_vad(5.0)
        self.assertEqual(cm.exception.category, "mic_error")
        self.assertIn("录音失败", str(cm.exception))

    async def test_invalid_duration_raises(self):
        with self.assertRaises(KwsError) as cm:
            await vad_module.record_audio_vad(0.0)
        self.assertEqual(cm.exception.category, "invalid_duration")
        with self.assertRaises(KwsError) as cm:
            await vad_module.record_audio_vad(0.00001)  # 采样点不足 1
        self.assertEqual(cm.exception.category, "invalid_duration")

    async def test_invalid_params_fall_back_defaults(self):
        # silence_end_sec=0 / min_voice_sec=-1 / threshold=NaN → 回退默认，不崩
        blocks = [_sine_block()] * 20 + [_zero_block()] * 30
        wav, is_silent = await self._run(
            blocks,
            silence_end_sec=0,
            min_voice_sec=-1,
            energy_threshold=float("nan"),
        )
        self.assertIsNotNone(wav)
        # 20 块人声（1.0s）≥ 回退的 min_voice_sec 0.3s → 判定有人声
        self.assertFalse(is_silent)


if __name__ == "__main__":
    unittest.main()
