"""realtime 配置测试：config.json 读取（白名单合并）+ voice 互斥校验（TASK-037）。"""

import json
import tempfile
import unittest

from channels.realtime import DEFAULT_VOICE_TYPE
from config import load_config
from main import _validate_voice_realtime_exclusive


class RealtimeConfigTests(unittest.TestCase):
    def _load(self, payload: dict):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as f:
            json.dump(payload, f)
            f.flush()
            return load_config(f.name)

    def test_realtime_defaults_when_absent(self):
        cfg = self._load({})
        rt = cfg.realtime
        self.assertFalse(rt["enabled"])
        self.assertEqual(rt["voice"], DEFAULT_VOICE_TYPE)
        self.assertEqual(rt["api_key"], "")
        self.assertEqual(
            rt["kws"]["model_dir"],
            "voice/kws/models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01",
        )
        self.assertEqual(rt["silence_timeout_sec"], 5.0)

    def test_realtime_partial_override_merges(self):
        cfg = self._load(
            {
                "realtime": {
                    "enabled": True,
                    "api_key": "uuid-key",
                    "voice": "zh_female_xiaohe_jupiter_bigtts",
                }
            }
        )
        rt = cfg.realtime
        self.assertTrue(rt["enabled"])
        self.assertEqual(rt["api_key"], "uuid-key")
        self.assertEqual(rt["voice"], "zh_female_xiaohe_jupiter_bigtts")
        # 未配置字段补默认
        self.assertEqual(rt["silence_timeout_sec"], 5.0)
        self.assertEqual(rt["model"], "1.2.6.1")
        self.assertIsNotNone(rt["kws"])

    def test_unknown_realtime_fields_dropped(self):
        cfg = self._load(
            {
                "realtime": {
                    "enabled": True,
                    "bogus_field": 1,
                    "instructions": "旧的人设配置不应再生效",
                    "interrupt_energy_threshold": 400.0,
                    "kws": {"model_dir": "x", "bogus_kws": 2},
                }
            }
        )
        rt = cfg.realtime
        self.assertNotIn("bogus_field", rt)
        self.assertNotIn("instructions", rt)
        self.assertNotIn("interrupt_energy_threshold", rt)
        self.assertEqual(rt["kws"]["model_dir"], "x")
        self.assertNotIn("bogus_kws", rt["kws"])

    def test_save_config_whitelists_realtime(self):
        from config import save_config

        cfg = self._load({"realtime": {"enabled": True, "api_key": "uuid-key"}})
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as f:
            save_config(cfg, f.name)
            saved = json.load(open(f.name, encoding="utf-8"))
        self.assertEqual(saved["realtime"]["enabled"], True)
        self.assertEqual(saved["realtime"]["api_key"], "uuid-key")
        self.assertNotIn("bogus", saved["realtime"])

    # —— voice / realtime 麦克风互斥（镜像 main.py 的 _validate_voice_realtime_exclusive）——

    def test_voice_realtime_mutual_exclusion_raises(self):
        with self.assertRaises(ValueError):
            _validate_voice_realtime_exclusive(
                {"enabled": True}, {"enabled": True}
            )

    def test_voice_realtime_single_enable_ok(self):
        # 只有 voice 或只有 realtime → 不抛
        _validate_voice_realtime_exclusive({"enabled": True}, {"enabled": False})
        _validate_voice_realtime_exclusive({"enabled": False}, {"enabled": True})
        _validate_voice_realtime_exclusive({}, {})

    def test_voice_realtime_both_disabled_ok(self):
        _validate_voice_realtime_exclusive(
            {"enabled": False}, {"enabled": False}
        )
