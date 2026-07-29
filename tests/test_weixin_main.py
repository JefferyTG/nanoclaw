"""Composition-root tests for the optional Weixin channel."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from bus.queue import MessageBus
from config import NanoClawConfig
from main import build_weixin_channel, watch_channel_start_failures


class WeixinCompositionTests(unittest.TestCase):
    def test_disabled_channel_is_not_built(self):
        self.assertIsNone(build_weixin_channel(NanoClawConfig(), MessageBus(), None))

    def test_enabled_channel_receives_startup_only_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = NanoClawConfig(workspace=tmp)
            cfg.weixin = {
                **cfg.weixin,
                "enabled": True,
                "bridge_command": ["node", "bridge.mjs"],
                "allowed_user_ids": ["user-1"],
                "image_merge_window_sec": 3.5,
                "request_timeout_sec": 12,
            }
            channel = build_weixin_channel(cfg, MessageBus(), object())

        self.assertEqual(channel.bridge_command, ("node", "bridge.mjs"))
        self.assertEqual(channel.allowed_user_ids, frozenset({"user-1"}))
        self.assertEqual(channel.command_timeout_sec, 12)
        self.assertEqual(channel.image_merge_window_sec, 3.5)
        self.assertTrue(channel.auto_login)
        self.assertEqual(
            channel.state_dir,
            Path(os.path.realpath(os.path.join(tmp, "workspace/weixin"))),
        )

    def test_invalid_command_and_allowlist_are_rejected(self):
        cfg = NanoClawConfig()
        cfg.weixin = {**cfg.weixin, "enabled": True, "bridge_command": "node bridge"}
        with self.assertRaisesRegex(ValueError, "bridge_command"):
            build_weixin_channel(cfg, MessageBus(), None)

        cfg.weixin = {
            **NanoClawConfig().weixin,
            "enabled": True,
            "allowed_user_ids": "*",
        }
        with self.assertRaisesRegex(ValueError, "allowed_user_ids"):
            build_weixin_channel(cfg, MessageBus(), None)

    def test_state_dir_must_remain_in_ignored_runtime_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = NanoClawConfig(workspace=tmp)
            cfg.weixin = {
                **cfg.weixin,
                "enabled": True,
                "state_dir": os.path.join(tmp, "outside"),
            }
            with self.assertRaisesRegex(ValueError, "state_dir"):
                build_weixin_channel(cfg, MessageBus(), None)


class ChannelStartupWatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_background_start_does_not_stop_application(self):
        async def completed():
            return None

        task = asyncio.create_task(completed())
        watcher = asyncio.create_task(
            watch_channel_start_failures(
                [task], [type("Channel", (), {"name": "weixin"})()]
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertFalse(watcher.done())
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)

    async def test_failed_background_start_is_surfaced(self):
        async def failed():
            raise OSError("missing bridge")

        task = asyncio.create_task(failed())
        with self.assertRaisesRegex(RuntimeError, "weixin.*missing bridge"):
            await watch_channel_start_failures(
                [task], [type("Channel", (), {"name": "weixin"})()]
            )


if __name__ == "__main__":
    unittest.main()
