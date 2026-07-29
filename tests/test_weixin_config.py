"""Weixin configuration regression tests."""

import json
import tempfile
import unittest

from config import NanoClawConfig, load_config, save_config


class WeixinConfigTests(unittest.TestCase):
    def test_weixin_is_disabled_and_deny_all_by_default(self):
        settings = NanoClawConfig().weixin
        self.assertFalse(settings["enabled"])
        self.assertEqual(settings["allowed_user_ids"], [])
        self.assertEqual(
            settings["bridge_command"],
            ["node", "integrations/weixin_bridge/bridge.mjs"],
        )
        self.assertEqual(settings["state_dir"], "workspace/weixin")
        self.assertEqual(settings["image_merge_window_sec"], 10.0)

    def test_partial_file_config_keeps_safe_defaults(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump(
                {"weixin": {"enabled": True, "allowed_user_ids": ["wx-user"]}},
                file,
            )
            file.flush()
            cfg = load_config(file.name)

        self.assertTrue(cfg.weixin["enabled"])
        self.assertEqual(cfg.weixin["allowed_user_ids"], ["wx-user"])
        self.assertEqual(cfg.weixin["state_dir"], "workspace/weixin")
        self.assertEqual(cfg.weixin["image_merge_window_sec"], 10.0)
        self.assertEqual(cfg.weixin["request_timeout_sec"], 30)

    def test_image_merge_window_can_be_overridden(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump({"weixin": {"image_merge_window_sec": 3.5}}, file)
            file.flush()
            cfg = load_config(file.name)

        self.assertEqual(cfg.weixin["image_merge_window_sec"], 3.5)

    def test_save_round_trip_does_not_add_credentials(self):
        cfg = NanoClawConfig()
        cfg.weixin = {
            **cfg.weixin,
            "enabled": True,
            "allowed_user_ids": ["wx-user"],
            "bot_token": "must-not-leak",
            "context_token": "must-not-leak",
        }
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as file:
            save_config(cfg, file.name)
            file.seek(0)
            saved = json.load(file)
            self.assertNotIn("bot_token", saved["weixin"])
            self.assertNotIn("context_token", saved["weixin"])
            loaded = load_config(file.name)

        expected = dict(cfg.weixin)
        expected.pop("bot_token")
        expected.pop("context_token")
        self.assertEqual(loaded.weixin, expected)

    def test_file_cannot_inject_bridge_owned_secrets(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump(
                {
                    "weixin": {
                        "enabled": True,
                        "bot_token": "must-not-load",
                        "cursor": "must-not-load",
                    }
                },
                file,
            )
            file.flush()
            cfg = load_config(file.name)

        self.assertTrue(cfg.weixin["enabled"])
        self.assertNotIn("bot_token", cfg.weixin)
        self.assertNotIn("cursor", cfg.weixin)


if __name__ == "__main__":
    unittest.main()
