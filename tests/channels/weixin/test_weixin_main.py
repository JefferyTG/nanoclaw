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
            bind = object()
            unbind = object()
            suspend = object()
            cfg = NanoClawConfig(workspace=tmp)
            cfg.weixin = {
                **cfg.weixin,
                "enabled": True,
                "bridge_command": ["node", "bridge.mjs"],
                "allowed_user_ids": ["user-1"],
                "image_merge_window_sec": 3.5,
                "merge_max_messages": 15,
                "request_timeout_sec": 12,
            }
            channel = build_weixin_channel(
                cfg,
                MessageBus(),
                object(),
                bind_callback=bind,
                unbind_callback=unbind,
                suspend_callback=suspend,
            )

        self.assertEqual(channel.bridge_command, ("node", "bridge.mjs"))
        self.assertEqual(channel.allowed_user_ids, frozenset({"user-1"}))
        self.assertEqual(channel.command_timeout_sec, 12)
        # main.py（冻结）仍按 image_merge_window_sec 传参：通道把旧名映射到新名
        self.assertEqual(channel.merge_window_sec, 3.5)
        self.assertEqual(channel.image_merge_window_sec, 3.5)
        # main.py 现按 merge_max_messages 透传：配置 15 → 通道 15
        self.assertEqual(channel.merge_max_messages, 15)
        self.assertTrue(channel.auto_login)
        self.assertIs(channel._bind_callback, bind)
        self.assertIs(channel._unbind_callback, unbind)
        self.assertIs(channel._suspend_callback, suspend)
        self.assertEqual(
            channel.state_dir,
            Path(os.path.realpath(os.path.join(tmp, "workspace/weixin"))),
        )

    def test_file_store_flows_through_build_weixin_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = NanoClawConfig(workspace=tmp)
            cfg.weixin = {
                **cfg.weixin,
                "enabled": True,
                "bridge_command": ["node", "bridge.mjs"],
                "allowed_user_ids": ["user-1"],
            }
            file_store = object()
            channel = build_weixin_channel(
                cfg, MessageBus(), None, file_store=file_store
            )
            self.assertIs(channel.file_store, file_store)

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


class WeixinCompositionMergeConfigTests(unittest.TestCase):
    def test_new_merge_config_flows_through_load_config_into_channel(self):
        import json
        import tempfile

        from config import load_config
        from main import build_weixin_channel

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "workspace": tmp,
                    "weixin": {
                        "enabled": True,
                        "bridge_command": ["node", "bridge.mjs"],
                        "allowed_user_ids": ["user-1"],
                        "merge_window_sec": 3.5,
                        "merge_max_messages": 15,
                    },
                }, f)
            cfg = load_config(os.path.join(tmp, "config.json"))
            channel = build_weixin_channel(
                cfg,
                MessageBus(),
                object(),
                bind_callback=None,
                unbind_callback=None,
                suspend_callback=None,
            )

        self.assertEqual(cfg.weixin["merge_window_sec"], 3.5)
        # 兼容镜像：main.py 读 image_merge_window_sec 时得到同一生效值
        self.assertEqual(cfg.weixin["image_merge_window_sec"], 3.5)
        self.assertEqual(channel.merge_window_sec, 3.5)
        # merge_max_messages 经配置层解析后由 main.py 透传 → 通道生效
        self.assertEqual(cfg.weixin["merge_max_messages"], 15)
        self.assertEqual(channel.merge_max_messages, 15)

    def test_invalid_merge_config_values_fall_back_to_constructor_defaults(self):
        # 回归：非法字符串（如 "abc"）不再在 main.py 强转时抛 ValueError，
        # 而是下沉到 WeixinChannel 构造器兜底到默认值。
        with tempfile.TemporaryDirectory() as tmp:
            cfg = NanoClawConfig(workspace=tmp)
            cfg.weixin = {
                **cfg.weixin,
                "enabled": True,
                "bridge_command": ["node", "bridge.mjs"],
                "allowed_user_ids": ["user-1"],
                "image_merge_window_sec": "abc",
                "merge_max_messages": "xyz",
            }
            channel = build_weixin_channel(
                cfg,
                MessageBus(),
                object(),
                bind_callback=None,
                unbind_callback=None,
                suspend_callback=None,
            )

        self.assertEqual(channel.merge_window_sec, 8.0)
        self.assertEqual(channel.image_merge_window_sec, 8.0)
        self.assertEqual(channel.merge_max_messages, 10)

    def test_missing_merge_config_uses_defaults_consistent_with_constructor(self):
        # 未配置合并窗口时，main.py 兜底 8.0 与 config.py/构造器默认一致。
        with tempfile.TemporaryDirectory() as tmp:
            cfg = NanoClawConfig(workspace=tmp)
            cfg.weixin = {
                **cfg.weixin,
                "enabled": True,
                "bridge_command": ["node", "bridge.mjs"],
                "allowed_user_ids": ["user-1"],
            }
            channel = build_weixin_channel(cfg, MessageBus(), object())

        self.assertEqual(channel.merge_window_sec, 8.0)
        self.assertEqual(channel.merge_max_messages, 10)




if __name__ == "__main__":
    unittest.main()
