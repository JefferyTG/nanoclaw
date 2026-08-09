"""TASK-028 播放防炸麦（音量归一化/限幅）专项测试。

覆盖四层：

1. ``voice/kws/normalize.normalize_playback_pcm`` 单元测试：满幅音频被压回
   目标峰值内；低响度默认只压不抬；max_gain_db>0 受控抬升且不超目标；int16
   边界值（-32768/32767）转换无溢出；空数组/全零静音原样返回；极小峰值防
   放大爆炸；非 int16 输入拒绝；
2. ``voice/kws/player.play_audio`` 集成测试：mock 解码 + sd.play/sd.wait →
   DSP 处理后才进 sd.play（mock 收到的数组峰值已被限幅）；低响度原样播放；
   ``playback_params`` 透传生效；None 用默认；
3. ``config.voice.playback.*`` 白名单深度合并：默认值 / 部分字段覆盖未知丢弃 /
   旧 config 缺字段回退默认 / save_config 白名单过滤；
4. ``VoiceChannel`` 传参：``_sanitize_playback_params`` 类型归一与非法回退、
   ``send()`` 与唤醒回应播放都把 ``playback_params`` 透传给 ``play_audio``、
   main.py 装配推导同款。

不触发任何真实模型/网络/麦克风/输出设备（解码与播放全部 mock）。
"""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
import wave
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np

from config import NanoClawConfig, load_config, save_config
from bus.queue import MessageBus, OutboundMessage
from channels.voice import VoiceChannel
from voice.kws import player as player_module
from voice.kws.normalize import normalize_playback_pcm

_TARGET_ABS = 0.89 * 32767.0
_TARGET_ABS_INT = int(_TARGET_ABS) + 1  # 允许 1 LSB 舍入余量


def _full_scale_pcm(n: int = 8000) -> bytes:
    """满幅 int16 PCM bytes：440Hz 方波（+32767/-32768 交替，峰值 32768）。"""
    t = np.arange(n)
    wave_samples = (np.sign(np.sin(2 * np.pi * 440 * t / 24000)) * 32767).astype(
        np.int16
    )
    wave_samples[1::2] = -32768  # 混入 -32768 满幅边界
    return wave_samples.tobytes()


def _low_pcm(n: int = 8000, peak: int = 5000) -> bytes:
    """低响度 int16 PCM bytes（峰值 peak，远低于目标 0.89*32767）。"""
    t = np.arange(n)
    return (peak * np.sin(2 * np.pi * 440 * t / 24000)).astype(np.int16).tobytes()


# —— 1. normalize_playback_pcm 单元测试 ——


class NormalizeUnitTests(unittest.TestCase):
    def _full_scale_sine(self) -> np.ndarray:
        n = 8000
        t = np.arange(n) / 24000.0
        data = (np.sin(2 * np.pi * 440 * t) * 32767.0).round().astype(np.int16)
        data[0] = 32767  # 保证峰值恰好满幅
        return data

    def test_full_scale_audio_is_limited_to_target_peak(self):
        """满幅正弦 → 默认（soft_clip=True）处理后峰值 ≤ target_peak。"""
        data = self._full_scale_sine()
        self.assertEqual(int(np.max(np.abs(data))), 32767)
        out = normalize_playback_pcm(data)
        self.assertEqual(out.dtype, np.int16)
        self.assertLessEqual(int(np.max(np.abs(out))), _TARGET_ABS_INT)

    def test_full_scale_square_limited_with_soft_clip_off(self):
        """满幅方波 + soft_clip=False（纯 clip 路径）→ 峰值同样 ≤ target_peak。"""
        data = np.array([32767, -32768] * 1000, dtype=np.int16)
        out = normalize_playback_pcm(data, soft_clip=False)
        self.assertEqual(out.dtype, np.int16)
        self.assertLessEqual(int(np.max(np.abs(out))), _TARGET_ABS_INT)

    def test_low_level_unchanged_with_default_max_gain(self):
        """低响度（峰值 5000）+ 默认 max_gain_db=0 → 只压不抬，峰值不变。"""
        data = (5000 * np.sin(2 * np.pi * 440 * np.arange(8000) / 24000)).astype(
            np.int16
        )
        out = normalize_playback_pcm(data)
        self.assertIsNot(out, data)  # 返回副本而非原地改
        self.assertTrue(np.array_equal(out, data))
        self.assertEqual(int(np.max(np.abs(out))), 5000)

    def test_negative_or_none_max_gain_is_compress_only(self):
        """max_gain_db<0 / None → 视为 0（只压不抬），低响度不被放大。"""
        data = (5000 * np.sin(2 * np.pi * 440 * np.arange(8000) / 24000)).astype(
            np.int16
        )
        for mg in (-3.0, None):
            out = normalize_playback_pcm(data, max_gain_db=mg)
            self.assertTrue(np.array_equal(out, data), f"max_gain_db={mg}")

    def test_max_gain_boost_does_not_exceed_target(self):
        """max_gain_db=6：低响度被抬升（峰值变大）但不超过 target_abs。"""
        data = (5000 * np.sin(2 * np.pi * 440 * np.arange(8000) / 24000)).astype(
            np.int16
        )
        out = normalize_playback_pcm(data, max_gain_db=6.0)
        peak_out = int(np.max(np.abs(out)))
        self.assertGreater(peak_out, 5000)  # 确实被抬升
        self.assertLessEqual(peak_out, _TARGET_ABS_INT)

    def test_int16_boundary_values_no_overflow(self):
        """-32768/32767 混入 → 结果 dtype=int16、无 NaN/inf、峰值 ≤ 目标。"""
        data = np.array(
            [32767, -32768, 32767, -32768, 0, 12345, -23456], dtype=np.int16
        )
        for soft_clip in (True, False):
            out = normalize_playback_pcm(data, soft_clip=soft_clip)
            self.assertEqual(out.dtype, np.int16)
            self.assertTrue(np.all(np.isfinite(out.astype(np.float64))))
            self.assertLessEqual(int(np.max(np.abs(out))), _TARGET_ABS_INT)

    def test_all_minus_32768_not_mistaken_for_silence(self):
        """全 -32768 满幅边界（int16 无正表示）不能被误判为静音放行。"""
        data = np.full(200, -32768, dtype=np.int16)
        out = normalize_playback_pcm(data, soft_clip=False)
        self.assertEqual(out.dtype, np.int16)
        self.assertLessEqual(int(np.max(np.abs(out))), _TARGET_ABS_INT)

    def test_empty_and_silence_returned_as_is(self):
        """空数组 / 全零静音 → 原样返回（同一对象、值相等）。"""
        empty = np.array([], dtype=np.int16)
        self.assertIs(normalize_playback_pcm(empty), empty)
        self.assertEqual(normalize_playback_pcm(empty).size, 0)

        silence = np.zeros(100, dtype=np.int16)
        out = normalize_playback_pcm(silence)
        self.assertIs(out, silence)
        self.assertTrue(np.array_equal(out, silence))

    def test_tiny_peak_boost_is_clamped_no_explosion(self):
        """极小峰值 + 巨大 max_gain_db → gain 钳制 ±48dB，无爆炸、输出有限。"""
        data = np.array([1, -1, 0, 1], dtype=np.int16)
        out = normalize_playback_pcm(data, max_gain_db=48.0)
        self.assertEqual(out.dtype, np.int16)
        self.assertTrue(np.all(np.isfinite(out.astype(np.float64))))
        # 48dB ≈ 251 倍，1 → ~251，远小于 int16 溢出
        self.assertLessEqual(int(np.max(np.abs(out))), 1000)

    def test_non_int16_input_raises(self):
        """dtype 非 int16 → ValueError（函数契约只接受 int16）。"""
        with self.assertRaises(ValueError):
            normalize_playback_pcm(np.zeros(10, dtype=np.float32))
        with self.assertRaises(ValueError):
            normalize_playback_pcm(np.zeros(10, dtype=np.int32))

    def test_invalid_target_peak_raises(self):
        """target_peak 非正（0 / 负数 / NaN / 非数字）→ ValueError。"""
        data = np.array([1000, -2000], dtype=np.int16)
        for bad in (0.0, -0.1, float("nan"), "abc", None):
            with self.assertRaises(ValueError):
                normalize_playback_pcm(data, target_peak=bad)

    def test_non_contiguous_input_is_handled(self):
        """非连续内存输入（stride 视图）也能正确处理。"""
        base = np.arange(16, dtype=np.int16)
        view = base[::2]  # 非连续视图
        out = normalize_playback_pcm(view)
        self.assertEqual(out.dtype, np.int16)
        self.assertTrue(np.array_equal(out, view))

    def test_returns_int16_round_trip(self):
        """输出恒为 int16，且峰值约束在任何 target_peak 配置下成立。"""
        data = self._full_scale_sine()
        for tp in (0.5, 0.7, 0.89, 0.95):
            out = normalize_playback_pcm(data, target_peak=tp)
            self.assertEqual(out.dtype, np.int16)
            self.assertLessEqual(
                int(np.max(np.abs(out))), int(tp * 32767.0) + 1
            )


# —— 2. play_audio 集成测试 ——


class PlayAudioNormalizeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def _run_play(self, pcm: bytes, playback_params=None):
        """mock 解码 + sd.play/sd.wait，返回 sd.play 收到的数组。"""
        with patch.object(
            player_module,
            "_decode_to_pcm_s16le",
            new=AsyncMock(return_value=pcm),
        ), patch.object(player_module.sd, "play") as mock_play, patch.object(
            player_module.sd, "wait", return_value=None
        ):
            await player_module.play_audio(
                b"whatever", "audio/wav", playback_params=playback_params
            )
        self.assertEqual(mock_play.call_count, 1)
        return mock_play.call_args.args[0]

    async def test_full_scale_audio_limited_before_sd_play(self):
        """满幅音频 → sd.play 收到的数组峰值已被 DSP 限幅（≤ target_peak）。"""
        played = await self._run_play(_full_scale_pcm())
        self.assertIsInstance(played, np.ndarray)
        self.assertEqual(played.dtype, np.int16)
        self.assertLessEqual(int(np.max(np.abs(played))), _TARGET_ABS_INT)

    async def test_low_level_audio_plays_unchanged(self):
        """低响度音频 → 正常返回，sd.play 收到峰值不变（只压不抬）。"""
        played = await self._run_play(_low_pcm(peak=5000))
        self.assertEqual(int(np.max(np.abs(played))), 5000)

    async def test_playback_params_target_peak_applied(self):
        """playback_params 透传：自定义 target_peak=0.5 → 峰值 ≤ 0.5 目标。"""
        played = await self._run_play(
            _full_scale_pcm(), playback_params={"target_peak": 0.5}
        )
        self.assertLessEqual(int(np.max(np.abs(played))), int(0.5 * 32767) + 1)

    async def test_playback_params_none_uses_default(self):
        """playback_params=None（向后兼容既有调用）→ 仍走默认 DSP（防炸麦）。"""
        played = await self._run_play(_full_scale_pcm(), playback_params=None)
        self.assertLessEqual(int(np.max(np.abs(played))), _TARGET_ABS_INT)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg 未安装")
    async def test_real_ffmpeg_decode_full_scale_wav_is_limited(self):
        """真实 ffmpeg 解码满幅 WAV → 播放前已被限幅（端到端）。"""
        n = 2400
        t = np.arange(n) / 24000.0
        pcm = (np.sin(2 * np.pi * 440 * t) * 32767.0).astype("<i2")
        pcm[0] = 32767
        buffer = __import__("io").BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(pcm.tobytes())
        with patch.object(player_module.sd, "play") as mock_play, patch.object(
            player_module.sd, "wait", return_value=None
        ):
            await player_module.play_audio(buffer.getvalue(), "audio/wav")
        played = mock_play.call_args.args[0]
        self.assertEqual(played.dtype, np.int16)
        self.assertLessEqual(int(np.max(np.abs(played))), _TARGET_ABS_INT)


# —— 3. config.voice.playback.* 白名单合并 ——


class VoicePlaybackConfigTests(unittest.TestCase):
    def _load_voice(self, voice: dict) -> dict:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump({"voice": voice}, file)
            file.flush()
            cfg = load_config(file.name)
        return cfg.voice if isinstance(cfg.voice, dict) else {}

    def test_default_playback_fields(self):
        """默认 voice 配置含 TASK-028 新字段 playback.*（0.89 / 0.0 / True）。"""
        settings = NanoClawConfig().voice
        self.assertEqual(
            settings["playback"],
            {"target_peak": 0.89, "max_gain_db": 0.0, "soft_clip": True},
        )

    def test_load_config_partial_merge_drops_unknown_keeps_defaults(self):
        """config.json 带 playback 部分字段：白名单合并，未知键丢弃，缺省保持默认。"""
        voice = self._load_voice(
            {
                "enabled": True,
                "playback": {"target_peak": 0.5, "bogus_param": 123},
            }
        )
        playback = voice["playback"]
        self.assertEqual(playback["target_peak"], 0.5)
        self.assertNotIn("bogus_param", playback)
        self.assertEqual(playback["max_gain_db"], 0.0)
        self.assertIs(playback["soft_clip"], True)

    def test_old_config_missing_playback_falls_back_default(self):
        """旧 config.json 只有 voice.enabled → playback 默认补全。"""
        voice = self._load_voice({"enabled": True})
        self.assertEqual(
            voice["playback"],
            {"target_peak": 0.89, "max_gain_db": 0.0, "soft_clip": True},
        )

    def test_playback_non_dict_is_guarded_at_usage_site(self):
        """playback 非 dict → load_config 原样透传（与 vad/voice 同模式），
        安全兜底在 main.py / 渠道侧（isinstance 守卫 / 清洗回 {}）。"""
        voice = self._load_voice({"enabled": True, "playback": "oops"})
        self.assertEqual(voice["playback"], "oops")
        self.assertEqual(
            {} if not isinstance(voice["playback"], dict) else voice["playback"],
            {},
        )

    def test_save_config_filters_playback_fields(self):
        """save_config 按 _VOICE_PLAYBACK_FIELDS 白名单过滤 playback 未知键。"""
        cfg = NanoClawConfig()
        cfg.voice["enabled"] = True
        cfg.voice["playback"]["target_peak"] = 0.7
        cfg.voice["playback"]["soft_clip"] = False
        cfg.voice["playback"]["bogus"] = 999
        path = tempfile.mktemp(suffix=".json")
        try:
            save_config(cfg, path)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            playback = data["voice"]["playback"]
            self.assertEqual(playback["target_peak"], 0.7)
            self.assertIs(playback["soft_clip"], False)
            self.assertNotIn("bogus", playback)
            self.assertIn("max_gain_db", playback)  # 默认子字段保留
        finally:
            if os.path.exists(path):
                os.unlink(path)


# —— 4. VoiceChannel 传参 ——


class VoicePlaybackChannelTests(unittest.TestCase):
    def test_sanitize_non_dict_returns_empty(self):
        self.assertEqual(VoiceChannel._sanitize_playback_params(None), {})
        self.assertEqual(VoiceChannel._sanitize_playback_params("oops"), {})
        self.assertEqual(VoiceChannel._sanitize_playback_params([]), {})

    def test_sanitize_types_normalized_and_unknown_dropped(self):
        out = VoiceChannel._sanitize_playback_params(
            {
                "target_peak": "0.7",
                "max_gain_db": 3,
                "soft_clip": "false",
                "bogus": 123,
            }
        )
        self.assertEqual(
            out, {"target_peak": 0.7, "max_gain_db": 3.0, "soft_clip": False}
        )

    def test_sanitize_invalid_values_fall_back_default(self):
        out = VoiceChannel._sanitize_playback_params(
            {
                "target_peak": "abc",  # 非法 → 默认 0.89（但仍进 out，语义为默认）
                "max_gain_db": None,  # None → 不传
                "soft_clip": "maybe",  # 无法识别 → 默认 True
                "target_peak_bad": 0.0,  # 未知键丢弃
            }
        )
        self.assertEqual(out["target_peak"], 0.89)
        self.assertNotIn("max_gain_db", out)
        self.assertIs(out["soft_clip"], True)

    def test_sanitize_non_positive_target_peak_dropped(self):
        out = VoiceChannel._sanitize_playback_params(
            {"target_peak": -1.0, "max_gain_db": 2.0}
        )
        self.assertNotIn("target_peak", out)
        self.assertEqual(out["max_gain_db"], 2.0)

    def test_init_stores_sanitized_playback_params(self):
        ch = VoiceChannel(
            MessageBus(),
            playback_params={"target_peak": 0.7, "bogus": 1},
        )
        self.assertEqual(ch._playback_params, {"target_peak": 0.7})

    def test_init_none_playback_params_stores_empty(self):
        ch = VoiceChannel(MessageBus())
        self.assertEqual(ch._playback_params, {})


class VoicePlaybackSendTests(unittest.IsolatedAsyncioTestCase):
    def _make_tts(self):
        tts = MagicMock()
        tts.synthesize = AsyncMock(
            return_value=MagicMock(audio=b"wav", media_type="audio/wav")
        )
        return tts

    async def test_send_passes_playback_params_to_play_audio(self):
        ch = VoiceChannel(
            MessageBus(), playback_params={"target_peak": 0.5}
        )
        ch._tts_service = self._make_tts()
        with patch("channels.voice.play_audio", new=AsyncMock()) as play:
            await ch.send(
                OutboundMessage(
                    channel="voice", chat_id="local:0", content="你好呀"
                )
            )
        play.assert_awaited_once()
        _, kwargs = play.call_args
        self.assertEqual(kwargs["playback_params"], {"target_peak": 0.5})

    async def test_wake_reply_passes_playback_params_to_play_audio(self):
        ch = VoiceChannel(
            MessageBus(),
            playback_params={"target_peak": 0.6, "soft_clip": False},
        )
        ch._tts_service = self._make_tts()
        ch._wake_replies = ["哎，我在呢，你说吧"]
        with patch("channels.voice.play_audio", new=AsyncMock()) as play:
            ok = await ch._play_wake_reply()
        self.assertTrue(ok)
        play.assert_awaited_once()
        _, kwargs = play.call_args
        self.assertEqual(
            kwargs["playback_params"], {"target_peak": 0.6, "soft_clip": False}
        )


# —— 5. main.py 装配推导（轻量镜像，参考 test_voice_main） ——


def _derive_voice_playback_cfg(voice_settings: dict):
    """镜像 main.py voice 装配处的 playback 推导：``get("playback") or {}``。"""
    return voice_settings.get("playback") or {}


class VoiceMainPlaybackAssemblyTests(unittest.TestCase):
    def _load_voice_settings(self, voice: dict) -> dict:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump({"voice": voice}, file)
            file.flush()
            cfg = load_config(file.name)
        return cfg.voice if isinstance(cfg.voice, dict) else {}

    def test_playback_cfg_flows_from_config_into_channel(self):
        """config 配 playback 覆盖 → main 推导 → VoiceChannel 内部生效。"""
        voice_settings = self._load_voice_settings(
            {
                "enabled": True,
                "playback": {
                    "target_peak": 0.7,
                    "max_gain_db": 3.0,
                    "soft_clip": False,
                },
            }
        )
        cfg = _derive_voice_playback_cfg(voice_settings)
        channel = VoiceChannel(MessageBus(), playback_params=cfg)
        self.assertEqual(
            channel._playback_params,
            {"target_peak": 0.7, "max_gain_db": 3.0, "soft_clip": False},
        )

    def test_missing_playback_uses_config_defaults(self):
        """旧 config 无 playback → load_config 补默认 → 渠道拿到默认值。"""
        voice_settings = self._load_voice_settings({"enabled": True})
        cfg = _derive_voice_playback_cfg(voice_settings)
        self.assertEqual(
            cfg,
            {"target_peak": 0.89, "max_gain_db": 0.0, "soft_clip": True},
        )
        channel = VoiceChannel(MessageBus(), playback_params=cfg)
        self.assertEqual(channel._playback_params, cfg)

    def test_non_dict_playback_is_guarded_to_empty(self):
        """playback 非 dict → main 推导透传原值，渠道 _sanitize_playback_params
        清洗回 {}，安全不崩（与 vad 非 dict 同模式）。"""
        voice_settings = self._load_voice_settings(
            {"enabled": True, "playback": "oops"}
        )
        cfg = _derive_voice_playback_cfg(voice_settings)
        self.assertEqual(cfg, "oops")  # main 的 `or {}` 对 truthy 原值不生效
        channel = VoiceChannel(MessageBus(), playback_params=cfg)
        self.assertEqual(channel._playback_params, {})


if __name__ == "__main__":
    unittest.main()
