"""ASR configuration and composition-root regression tests."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from config import NanoClawConfig, load_config, save_config
from main import build_asr_service
from voice.asr.service import AudioTranscriptionService


class ASRConfigTests(unittest.TestCase):
    def test_asr_is_disabled_by_default(self):
        self.assertIsNone(build_asr_service(NanoClawConfig()))

    def test_enabled_config_builds_channel_independent_service(self):
        cfg = NanoClawConfig()
        cfg.asr_model = {
            **cfg.asr_model,
            "enabled": True,
            "api_key": "test-key",
            "base_url": "https://example.test/v1",
            "model": "test-asr",
        }
        service = build_asr_service(cfg)
        self.assertIsInstance(service, AudioTranscriptionService)
        self.assertEqual(service.max_audio_bytes, 10 * 1024 * 1024)
        self.assertEqual(service.provider.model, "test-asr")

    def test_partial_file_config_keeps_defaults_and_env_key_wins(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump({"asr_model": {"enabled": True, "model": "partial"}}, file)
            file.flush()
            with patch.dict(os.environ, {"ASR_API_KEY": "env-key"}, clear=False):
                cfg = load_config(file.name)
        self.assertTrue(cfg.asr_model["enabled"])
        self.assertEqual(cfg.asr_model["model"], "partial")
        self.assertEqual(cfg.asr_model["base_url"], "https://api.openai.com/v1")
        self.assertEqual(cfg.asr_model["api_key"], "env-key")

    def test_env_asr_key_is_not_persisted_by_config_save(self):
        cfg = NanoClawConfig()
        cfg.asr_model = {**cfg.asr_model, "enabled": True, "api_key": "env-key"}
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as file:
            with patch.dict(os.environ, {"ASR_API_KEY": "env-key"}, clear=False):
                save_config(cfg, file.name)
            file.seek(0)
            saved = json.load(file)
        self.assertEqual(saved["asr_model"]["api_key"], "")


if __name__ == "__main__":
    unittest.main()
