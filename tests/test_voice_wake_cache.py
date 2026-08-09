"""TASK-029 唤醒回应本地缓存随机播放路径测试。

验证 _play_wake_reply 在以下场景的行为：
- 缓存非空时优先播放本地 WAV，不调云端 TTS；
- 缓存为空时回退云端 tts.synthesize + play_audio（向后兼容）；
- 懓加载：两次调用只扫目录一次；
- config 合并 wake_replies_dir，缺字段回退默认；
- random.choice 多次调用覆盖多条缓存音频；
- 集成：造临时目录放假 wav → VoiceChannel + _play_wake_reply → 断言播放缓存音频。
"""

import asyncio
import json
import os
import struct
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from channels.voice import VoiceChannel
from config import load_config, NanoClawConfig
from voice.tts.service import TTSResult


def _make_wav_bytes(marker: int = 0, frames: int = 100) -> bytes:
    """生成一个最小有效 WAV 文件（1ch / 16000Hz / int16），内容含 marker 区分。"""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        # 每帧 int16，用 marker 区分不同文件
        data = struct.pack(
            "<{}h".format(frames), *([marker] * frames)
        )
        w.writeframes(data)
    return buf.getvalue()


class TestPlayWakeReplyUsesCache(unittest.IsolatedAsyncioTestCase):
    """缓存非空时优先播放本地 WAV，不调云端 TTS。"""

    async def test_play_wake_reply_uses_cache(self):
        # 造临时目录放假 wav
        with tempfile.TemporaryDirectory() as tmpdir:
            wav1 = _make_wav_bytes(marker=1)
            wav2 = _make_wav_bytes(marker=2)
            with open(os.path.join(tmpdir, "wake_001.wav"), "wb") as f:
                f.write(wav1)
            with open(os.path.join(tmpdir, "wake_002.wav"), "wb") as f:
                f.write(wav2)

            bus = MagicMock()
            tts_service = MagicMock()
            # 让 synthesize 抛异常——如果被调用说明缓存路径没走
            tts_service.synthesize = AsyncMock(
                side_effect=AssertionError("tts.synthesize 不应被调用")
            )

            channel = VoiceChannel(
                bus,
                tts_service=tts_service,
                wake_replies=["哎，我在呢，你说吧"],
                wake_replies_dir=tmpdir,
            )

            with patch(
                "channels.voice.play_audio",
                new_callable=AsyncMock,
            ) as mock_play:
                result = await channel._play_wake_reply()

            self.assertTrue(result)
            mock_play.assert_awaited_once()
            # 传入的 audio_bytes 应该是缓存中的某一条
            played_bytes = mock_play.call_args.args[0]
            self.assertIn(played_bytes, (wav1, wav2))
            # media_type 为 audio/wav
            self.assertEqual(mock_play.call_args.args[1], "audio/wav")
            # playback_params 透传
            self.assertIn("playback_params", mock_play.call_args.kwargs)
            # tts.synthesize 未被调用
            tts_service.synthesize.assert_not_awaited()


class TestPlayWakeReplyCacheEmptyFallback(unittest.IsolatedAsyncioTestCase):
    """缓存为空（空目录/不存在）→ 回退云端 tts.synthesize + play_audio。"""

    async def test_cache_empty_dir_fallback(self):
        """空目录 → 回退云端合成。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            bus = MagicMock()
            fake_audio = _make_wav_bytes(marker=99)
            tts_service = MagicMock()
            tts_service.synthesize = AsyncMock(
                return_value=TTSResult(
                    audio=fake_audio,
                    media_type="audio/wav",
                )
            )

            channel = VoiceChannel(
                bus,
                tts_service=tts_service,
                wake_replies=["哎，我在呢，你说吧"],
                wake_replies_dir=tmpdir,  # 空目录
            )

            with patch(
                "channels.voice.play_audio",
                new_callable=AsyncMock,
            ) as mock_play:
                result = await channel._play_wake_reply()

            self.assertTrue(result)
            tts_service.synthesize.assert_awaited_once()
            mock_play.assert_awaited_once()
            self.assertEqual(mock_play.call_args.args[0], fake_audio)

    async def test_cache_dir_not_exists_fallback(self):
        """目录不存在 → 回退云端合成。"""
        bus = MagicMock()
        fake_audio = _make_wav_bytes(marker=99)
        tts_service = MagicMock()
        tts_service.synthesize = AsyncMock(
            return_value=TTSResult(
                audio=fake_audio,
                media_type="audio/wav",
            )
        )

        channel = VoiceChannel(
            bus,
            tts_service=tts_service,
            wake_replies=["哎，我在呢，你说吧"],
            wake_replies_dir="/nonexistent/path/abc/",
        )

        with patch(
            "channels.voice.play_audio",
            new_callable=AsyncMock,
        ) as mock_play:
            result = await channel._play_wake_reply()

        self.assertTrue(result)
        tts_service.synthesize.assert_awaited_once()
        mock_play.assert_awaited_once()

    async def test_cache_empty_no_tts_skip(self):
        """缓存为空且 tts_service 为 None → 返回 False（跳过回应）。"""
        bus = MagicMock()
        channel = VoiceChannel(
            bus,
            tts_service=None,
            wake_replies=["哎，我在呢，你说吧"],
            wake_replies_dir="/nonexistent/path/abc/",
        )

        with patch(
            "channels.voice.play_audio",
            new_callable=AsyncMock,
        ) as mock_play:
            result = await channel._play_wake_reply()

        self.assertFalse(result)
        mock_play.assert_not_awaited()


class TestPlayWakeReplyLazyLoad(unittest.IsolatedAsyncioTestCase):
    """两次调用只扫目录一次（第二次 _wake_audio_cache 已非 None 不再扫）。"""

    async def test_lazy_load_scans_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wav1 = _make_wav_bytes(marker=1)
            with open(os.path.join(tmpdir, "wake_001.wav"), "wb") as f:
                f.write(wav1)

            bus = MagicMock()
            tts_service = MagicMock()
            tts_service.synthesize = AsyncMock(
                side_effect=AssertionError("不应调用 TTS")
            )

            channel = VoiceChannel(
                bus,
                tts_service=tts_service,
                wake_replies=["哎，我在呢"],
                wake_replies_dir=tmpdir,
            )

            self.assertIsNone(channel._wake_audio_cache)

            with patch("channels.voice.play_audio", new_callable=AsyncMock):
                await channel._play_wake_reply()

            # 第一次调用后缓存已加载
            self.assertIsNotNone(channel._wake_audio_cache)
            self.assertEqual(len(channel._wake_audio_cache), 1)

            with patch(
                "channels.voice.os.listdir"
            ) as mock_listdir, patch(
                "channels.voice.play_audio", new_callable=AsyncMock
            ):
                await channel._play_wake_reply()

            # 第二次调用不应再扫目录
            mock_listdir.assert_not_called()


class TestWakeRepliesDirConfig(unittest.TestCase):
    """config 合并 wake_replies_dir，缺字段回退默认。"""

    def test_default_has_wake_replies_dir(self):
        """默认 config 包含 wake_replies_dir 字段。"""
        cfg = NanoClawConfig()
        self.assertIn("wake_replies_dir", cfg.voice)
        self.assertEqual(
            cfg.voice["wake_replies_dir"],
            "workspace/voice/wake_replies/",
        )

    def test_config_file_override_wake_replies_dir(self):
        """config.json 中的 wake_replies_dir 覆盖默认。"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(
                {
                    "voice": {
                        "enabled": True,
                        "wake_replies_dir": "custom/wake/dir/",
                    }
                },
                f,
            )
            path = f.name

        try:
            cfg = load_config(path)
            self.assertTrue(cfg.voice["enabled"])
            self.assertEqual(
                cfg.voice["wake_replies_dir"], "custom/wake/dir/"
            )
            # 其他默认字段保持不变
            self.assertEqual(cfg.voice["record_sec"], 8.0)
            self.assertEqual(
                cfg.voice["wake_replies"], ["哎，我在呢，你说吧"]
            )
        finally:
            os.unlink(path)

    def test_config_missing_wake_replies_dir_fallback(self):
        """旧 config.json 缺 wake_replies_dir → 回退默认，不报错。"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(
                {
                    "voice": {
                        "enabled": True,
                    }
                },
                f,
            )
            path = f.name

        try:
            cfg = load_config(path)
            self.assertTrue(cfg.voice["enabled"])
            self.assertEqual(
                cfg.voice["wake_replies_dir"],
                "workspace/voice/wake_replies/",
            )
        finally:
            os.unlink(path)


class TestRandomChoice(unittest.IsolatedAsyncioTestCase):
    """多次调用覆盖多条缓存音频（验证 random.choice 正常工作）。"""

    async def test_random_choice_varies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # 放 3 条不同的假 wav
            wavs = []
            for i in range(3):
                w = _make_wav_bytes(marker=i + 1)
                wavs.append(w)
                with open(
                    os.path.join(tmpdir, f"wake_{i:03d}.wav"), "wb"
                ) as f:
                    f.write(w)

            bus = MagicMock()
            tts_service = MagicMock()
            tts_service.synthesize = AsyncMock(
                side_effect=AssertionError("不应调用 TTS")
            )

            channel = VoiceChannel(
                bus,
                tts_service=tts_service,
                wake_replies=["哎，我在呢"],
                wake_replies_dir=tmpdir,
            )

            played = set()
            for _ in range(20):
                with patch(
                    "channels.voice.play_audio",
                    new_callable=AsyncMock,
                ) as mock_play:
                    await channel._play_wake_reply()
                    played.add(mock_play.call_args.args[0])

            # 20 次调用应该覆盖多条不同音频（不严格断言全部，但至少 >1）
            self.assertGreater(len(played), 1)
            # 所有播放的音频都来自缓存
            for b in played:
                self.assertIn(b, wavs)


class TestIntegrationWakeCache(unittest.IsolatedAsyncioTestCase):
    """集成：造临时目录放 2~3 条假 wav → VoiceChannel 构造 + _play_wake_reply
    → 断言播放的是缓存音频之一。"""

    async def test_integration_full_flow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_a = _make_wav_bytes(marker=10)
            wav_b = _make_wav_bytes(marker=20)
            wav_c = _make_wav_bytes(marker=30)
            for fname, data in (
                ("wake_a.wav", wav_a),
                ("wake_b.wav", wav_b),
                ("wake_c.wav", wav_c),
            ):
                with open(os.path.join(tmpdir, fname), "wb") as f:
                    f.write(data)

            bus = MagicMock()
            tts_service = MagicMock()
            tts_service.synthesize = AsyncMock(
                side_effect=AssertionError("不应调用 TTS")
            )

            channel = VoiceChannel(
                bus,
                tts_service=tts_service,
                wake_replies=["哎，我在呢，你说吧"],
                wake_replies_dir=tmpdir,
            )

            with patch(
                "channels.voice.play_audio",
                new_callable=AsyncMock,
            ) as mock_play:
                result = await channel._play_wake_reply()

            self.assertTrue(result)
            mock_play.assert_awaited_once()
            played = mock_play.call_args.args[0]
            self.assertIn(played, (wav_a, wav_b, wav_c))
            self.assertEqual(mock_play.call_args.args[1], "audio/wav")


class TestNonWakeFilesIgnored(unittest.IsolatedAsyncioTestCase):
    """目录下非 wake_*.wav 文件应被忽略。"""

    async def test_non_wake_files_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_good = _make_wav_bytes(marker=1)
            with open(os.path.join(tmpdir, "wake_001.wav"), "wb") as f:
                f.write(wav_good)
            # 非 wake_ 前缀
            with open(os.path.join(tmpdir, "reply_001.wav"), "wb") as f:
                f.write(_make_wav_bytes(marker=2))
            # 非 .wav 扩展
            with open(os.path.join(tmpdir, "wake_002.txt"), "w") as f:
                f.write("not audio")

            bus = MagicMock()
            tts_service = MagicMock()
            tts_service.synthesize = AsyncMock(
                side_effect=AssertionError("不应调用 TTS")
            )

            channel = VoiceChannel(
                bus,
                tts_service=tts_service,
                wake_replies=["哎，我在呢"],
                wake_replies_dir=tmpdir,
            )

            with patch(
                "channels.voice.play_audio",
                new_callable=AsyncMock,
            ) as mock_play:
                result = await channel._play_wake_reply()

            self.assertTrue(result)
            mock_play.assert_awaited_once()
            self.assertEqual(mock_play.call_args.args[0], wav_good)


class TestCachePlaybackFallbackToTts(unittest.IsolatedAsyncioTestCase):
    """缓存播放失败 → 降级尝试云端合成。"""

    async def test_cache_playback_fail_fallback_tts(self):
        from voice.kws.errors import KwsError

        with tempfile.TemporaryDirectory() as tmpdir:
            wav1 = _make_wav_bytes(marker=1)
            with open(os.path.join(tmpdir, "wake_001.wav"), "wb") as f:
                f.write(wav1)

            bus = MagicMock()
            fake_tts_audio = _make_wav_bytes(marker=99)
            tts_service = MagicMock()
            tts_service.synthesize = AsyncMock(
                return_value=TTSResult(
                    audio=fake_tts_audio,
                    media_type="audio/wav",
                )
            )

            channel = VoiceChannel(
                bus,
                tts_service=tts_service,
                wake_replies=["哎，我在呢，你说吧"],
                wake_replies_dir=tmpdir,
            )

            # play_audio 第一次调用失败（缓存播放），第二次成功（TTS 播放）
            call_count = 0

            async def fake_play(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise KwsError("output_error", "播放失败")

            with patch(
                "channels.voice.play_audio",
                side_effect=fake_play,
            ) as mock_play:
                result = await channel._play_wake_reply()

            self.assertTrue(result)
            self.assertEqual(mock_play.await_count, 2)
            # 第二次调用播放的是 TTS 合成音频
            second_call_audio = mock_play.await_args_list[1].args[0]
            self.assertEqual(second_call_audio, fake_tts_audio)
            tts_service.synthesize.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
