"""TASK-026 子任务 1「Agent 回复 → 甘雨 TTS → 播放默认输出」专项测试。

覆盖 ``VoiceChannel.send()`` 的回复下发策略：
1. ``tts_service`` 为 None → 直接 ``_emit`` 回文字（不合成、不播放）；
2. 合成成功 + 播放成功 → 不 ``_emit`` 文字（音频由 play_audio 播放）；
3. 合成抛异常（TTSError 等）→ 先轻提示再 ``_emit`` 原文，不静默不崩溃；
4. 播放抛异常（KwsError 等）→ 同上降级回文字；
5. 文本超 ``max_voice_chars``（>0）→ 不调 synthesize，直接 ``_emit`` 文字；
6. ``max_voice_chars`` ≤ 0 → 不截断，长文本仍走合成分支（播放 mock）。

另覆盖 config.voice 新增字段（idle_ttl_sec / max_sessions / max_voice_chars）
的默认值与白名单合并。

不触发任何真实模型 / 网络 / 音频输出设备（播放全部 mock）。
"""

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import NanoClawConfig, load_config
from bus.queue import MessageBus, OutboundMessage
from channels.voice import VoiceChannel
from voice.kws.errors import KwsError
from voice.tts.base import TTSError


def _outbound(text: str) -> OutboundMessage:
    return OutboundMessage(channel="voice", chat_id="direct", content=text)


class _FakeTTS:
    """可注入失败行为的假 TTS 服务：记录 synthesize 文本、可按需抛异常。"""

    def __init__(self, *, audio=b"audio", media_type="audio/wav", error=None):
        self.audio = audio
        self.media_type = media_type
        self.error = error
        self.synthesized: list = []

    async def synthesize(self, text):
        self.synthesized.append(text)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(audio=self.audio, media_type=self.media_type)


class VoiceTTSReplyTests(unittest.IsolatedAsyncioTestCase):
    """send() 的 TTS 合成播放与降级策略。"""

    def _make_channel(self, tts=None, max_voice_chars: int = 300):
        voice = VoiceChannel(
            MessageBus(), tts_service=tts, max_voice_chars=max_voice_chars
        )
        emitted: list = []
        voice._reply_sink = emitted.append
        return voice, emitted

    async def test_tts_none_emits_text_without_synthesize_or_play(self):
        """tts_service=None → 直接 _emit 文字，不合成、不播放。"""
        voice, emitted = self._make_channel(tts=None)
        with patch("channels.voice.play_audio", new_callable=AsyncMock) as play:
            await voice.send(_outbound("你好呀"))
        self.assertEqual(emitted, ["你好呀"])
        play.assert_not_awaited()

    async def test_success_synthesizes_and_plays_without_emit(self):
        """合成成功 + 播放成功 → 不 _emit 文字（音频由 play_audio 播）。"""
        tts = _FakeTTS(audio=b"RIFF....", media_type="audio/wav")
        voice, emitted = self._make_channel(tts=tts)
        with patch("channels.voice.play_audio", new_callable=AsyncMock) as play:
            await voice.send(_outbound("你好"))
        self.assertEqual(tts.synthesized, ["你好"])
        play.assert_awaited_once_with(b"RIFF....", "audio/wav")
        self.assertEqual(emitted, [])

    async def test_synthesize_failure_degrades_to_text(self):
        """合成抛 TTSError → 轻提示 + 原文 _emit，播放不被调用。"""
        tts = _FakeTTS(error=TTSError("provider_failed", "语音合成服务暂时不可用。"))
        voice, emitted = self._make_channel(tts=tts)
        with patch("channels.voice.play_audio", new_callable=AsyncMock) as play:
            await voice.send(_outbound("回复文字"))
        self.assertEqual(tts.synthesized, ["回复文字"])
        play.assert_not_awaited()
        self.assertEqual(len(emitted), 2)
        self.assertIn("🔇 语音播放失败", emitted[0])
        self.assertEqual(emitted[1], "回复文字")

    async def test_play_failure_degrades_to_text(self):
        """播放抛 KwsError → 轻提示 + 原文 _emit，不崩溃。"""
        tts = _FakeTTS(audio=b"audio", media_type="audio/wav")
        voice, emitted = self._make_channel(tts=tts)
        with patch(
            "channels.voice.play_audio",
            new_callable=AsyncMock,
            side_effect=KwsError("output_error", "回应播放失败（请检查输出设备/音量）"),
        ) as play:
            await voice.send(_outbound("回复文字"))
        self.assertEqual(tts.synthesized, ["回复文字"])
        play.assert_awaited_once_with(b"audio", "audio/wav")
        self.assertEqual(len(emitted), 2)
        self.assertIn("🔇 语音播放失败", emitted[0])
        self.assertEqual(emitted[1], "回复文字")

    async def test_text_over_limit_emits_without_synthesize(self):
        """文本超 max_voice_chars → 不调 synthesize，直接 _emit 文字。"""
        tts = _FakeTTS()
        voice, emitted = self._make_channel(tts=tts, max_voice_chars=10)
        long_text = "很" * 20
        with patch("channels.voice.play_audio", new_callable=AsyncMock) as play:
            await voice.send(_outbound(long_text))
        self.assertEqual(tts.synthesized, [])
        play.assert_not_awaited()
        self.assertEqual(emitted, [long_text])

    async def test_text_at_limit_still_synthesizes(self):
        """文本长度恰等于 max_voice_chars → 仍走合成分支（仅严格超出才截断）。"""
        tts = _FakeTTS(audio=b"a", media_type="audio/wav")
        voice, emitted = self._make_channel(tts=tts, max_voice_chars=3)
        with patch("channels.voice.play_audio", new_callable=AsyncMock) as play:
            await voice.send(_outbound("一二三"))
        self.assertEqual(tts.synthesized, ["一二三"])
        play.assert_awaited_once()
        self.assertEqual(emitted, [])

    async def test_unlimited_chars_synthesizes_long_text(self):
        """max_voice_chars ≤ 0 → 不截断，长文本仍走合成分支（播放 mock）。"""
        tts = _FakeTTS(audio=b"long-audio", media_type="audio/wav")
        voice, emitted = self._make_channel(tts=tts, max_voice_chars=0)
        long_text = "很" * 500
        with patch("channels.voice.play_audio", new_callable=AsyncMock) as play:
            await voice.send(_outbound(long_text))
        self.assertEqual(tts.synthesized, [long_text])
        play.assert_awaited_once_with(b"long-audio", "audio/wav")
        self.assertEqual(emitted, [])

    async def test_empty_text_keeps_emit_semantics(self):
        """空内容不触发 TTS（合成会因空文本报错），仍走 _emit 原语义。"""
        tts = _FakeTTS()
        voice, emitted = self._make_channel(tts=tts)
        with patch("channels.voice.play_audio", new_callable=AsyncMock) as play:
            await voice.send(_outbound(""))
        self.assertEqual(tts.synthesized, [])
        play.assert_not_awaited()
        self.assertEqual(emitted, [""])


class VoiceTTSConfigTests(unittest.TestCase):
    """config.voice 新增字段：默认值 + 白名单合并。"""

    def test_voice_defaults_include_tts_fields(self):
        cfg = NanoClawConfig()
        self.assertEqual(cfg.voice["idle_ttl_sec"], 1800)
        self.assertEqual(cfg.voice["max_sessions"], 50)
        self.assertEqual(cfg.voice["max_voice_chars"], 300)

    def test_load_config_merges_new_fields_and_drops_unknown(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump(
                {
                    "voice": {
                        "enabled": True,
                        "idle_ttl_sec": 60,
                        "max_sessions": 3,
                        "max_voice_chars": 100,
                        "bogus_field": 123,  # 未知字段应被白名单丢弃
                    }
                },
                file,
            )
            file.flush()
            cfg = load_config(file.name)
        self.assertEqual(cfg.voice["idle_ttl_sec"], 60)
        self.assertEqual(cfg.voice["max_sessions"], 3)
        self.assertEqual(cfg.voice["max_voice_chars"], 100)
        self.assertNotIn("bogus_field", cfg.voice)
        # 未配置的既有字段保持默认
        self.assertEqual(cfg.voice["record_sec"], 8.0)
        self.assertEqual(cfg.voice["wake_replies"], ["哎，我在呢，你说吧"])

    def test_old_config_gets_new_fields_from_defaults(self):
        # 旧 config.json 只有 voice.enabled → 新字段自动补默认
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump({"voice": {"enabled": True}}, file)
            file.flush()
            cfg = load_config(file.name)
        self.assertEqual(cfg.voice["idle_ttl_sec"], 1800)
        self.assertEqual(cfg.voice["max_sessions"], 50)
        self.assertEqual(cfg.voice["max_voice_chars"], 300)


if __name__ == "__main__":
    unittest.main()
