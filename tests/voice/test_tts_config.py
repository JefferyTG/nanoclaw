"""TTS configuration and composition-root regression tests."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from config import NanoClawConfig, load_config
from main import build_tts_service
from voice.tts.dashscope_realtime import (
    DEFAULT_MODEL,
    DEFAULT_VOICE_ID,
    DashScopeRealtimeTTSProvider,
)
from voice.tts.service import TextToSpeechService


class TTSConfigTests(unittest.TestCase):
    def test_tts_backend_is_ready_by_default_while_ui_owns_opt_in(self):
        service = build_tts_service(NanoClawConfig())
        self.assertIsInstance(service, TextToSpeechService)
        self.assertEqual(service.provider.rate, "+0%")
        self.assertEqual(service.provider.voice, "zh-CN-XiaoxiaoNeural")
        self.assertEqual(service.max_text_chars, 4000)

    def test_tts_can_be_disabled_without_affecting_other_services(self):
        cfg = NanoClawConfig()
        cfg.tts_model = {**cfg.tts_model, "enabled": False}
        self.assertIsNone(build_tts_service(cfg))

    def test_partial_tts_config_keeps_defaults(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump({"tts_model": {"voice": "zh-CN-YunxiNeural"}}, file)
            file.flush()
            cfg = load_config(file.name)
        self.assertTrue(cfg.tts_model["enabled"])
        self.assertEqual(cfg.tts_model["voice"], "zh-CN-YunxiNeural")
        self.assertEqual(cfg.tts_model["max_audio_bytes"], 16 * 1024 * 1024)
        # dashscope_realtime 分支字段随默认值存在，且 provider 默认仍是 edge_tts
        self.assertEqual(cfg.tts_model["provider"], "edge_tts")
        self.assertEqual(
            cfg.tts_model["dashscope_realtime"]["voice_id"], DEFAULT_VOICE_ID
        )

    def test_unknown_provider_is_disabled(self):
        cfg = NanoClawConfig()
        cfg.tts_model = {**cfg.tts_model, "provider": "some_other_provider"}
        self.assertIsNone(build_tts_service(cfg))


class DashScopeBuildTests(unittest.TestCase):
    def test_dashscope_realtime_builds_with_valid_config(self):
        cfg = NanoClawConfig()
        cfg.tts_model = {
            **cfg.tts_model,
            "provider": "dashscope_realtime",
            "dashscope_realtime": {
                **cfg.tts_model["dashscope_realtime"],
                "api_key": "sk-ws-test",
            },
        }
        service = build_tts_service(cfg)
        self.assertIsInstance(service, TextToSpeechService)
        self.assertIsInstance(service.provider, DashScopeRealtimeTTSProvider)
        self.assertEqual(service.provider.voice_id, DEFAULT_VOICE_ID)
        self.assertEqual(service.provider.model, DEFAULT_MODEL)
        self.assertEqual(service.provider.api_key, "sk-ws-test")

    def test_dashscope_realtime_disabled_without_api_key(self):
        cfg = NanoClawConfig()
        cfg.tts_model = {**cfg.tts_model, "provider": "dashscope_realtime"}
        # 明确清掉环境变量，保证缺 api_key 路径确定触发
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": ""}):
            self.assertIsNone(build_tts_service(cfg))

    def test_dashscope_realtime_disabled_without_voice_id(self):
        cfg = NanoClawConfig()
        cfg.tts_model = {
            **cfg.tts_model,
            "provider": "dashscope_realtime",
            "dashscope_realtime": {
                **cfg.tts_model["dashscope_realtime"],
                "api_key": "sk-ws-test",
                "voice_id": "",
            },
        }
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": ""}):
            self.assertIsNone(build_tts_service(cfg))

    def test_dashscope_realtime_env_api_key_overrides_config(self):
        cfg = NanoClawConfig()
        cfg.tts_model = {**cfg.tts_model, "provider": "dashscope_realtime"}
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "sk-ws-env"}):
            service = build_tts_service(cfg)
        self.assertIsInstance(service, TextToSpeechService)
        self.assertEqual(service.provider.api_key, "sk-ws-env")

    def test_dashscope_realtime_invalid_params_disabled(self):
        cfg = NanoClawConfig()
        cfg.tts_model = {
            **cfg.tts_model,
            "provider": "dashscope_realtime",
            "dashscope_realtime": {
                **cfg.tts_model["dashscope_realtime"],
                "api_key": "sk-ws-test",
                "sample_rate": "abc",
            },
        }
        self.assertIsNone(build_tts_service(cfg))


if __name__ == "__main__":
    unittest.main()
