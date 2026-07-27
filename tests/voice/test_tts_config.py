"""TTS configuration and composition-root regression tests."""

import json
import tempfile
import unittest

from config import NanoClawConfig, load_config
from main import build_tts_service
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


if __name__ == "__main__":
    unittest.main()
