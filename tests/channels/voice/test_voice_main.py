"""TASK-027 第三步「main.py voice 渠道装配新参数」轻量构造测试。

main.py 的 voice 渠道装配在 ``amain()`` 内联（无 build_voice_channel 独立
函数，与 build_weixin_channel 不同），不便直接驱动完整装配。本测试按
main.py 装配处的**同款推导逻辑**做轻量构造测试（不真实启动渠道、不触发
麦克风/模型/网络）：

1. config.json → ``load_config``（voice 白名单合并，含 vad 子字典）→ 取
   ``cfg.voice``；
2. 按 main.py 装配处同款方式从 ``voice_settings`` 推导
   ``record_delay_sec`` / ``silence_timeout_sec`` / ``vad_params``（缺字段
   回退与 config.py 默认一致的兜底值）；
3. 构造 ``VoiceChannel``，断言三个 TASK-027 新参数真正流入渠道内部状态。

若日后 main.py 抽出 ``build_voice_channel`` 独立函数，本测试应改为直接
调用该函数（与 test_weixin_main 同模式）。
"""

import json
import os
import tempfile
import unittest

from config import NanoClawConfig, load_config
from bus.queue import MessageBus
from channels.voice import VoiceChannel


def _derive_voice_kwargs(voice_settings: dict) -> dict:
    """镜像 main.py voice 装配处的参数推导（record_delay_sec /
    silence_timeout_sec / vad_params）。

    voice_settings 来自 ``cfg.voice if isinstance(cfg.voice, dict) else {}``；
    缺字段兜底值与 config.py 默认一致（0.5 / 5.0 / None→渠道内置默认）。
    """
    voice_vad_cfg = (
        voice_settings.get("vad")
        if isinstance(voice_settings.get("vad"), dict)
        else None
    )
    return {
        "record_delay_sec": float(
            voice_settings.get("record_delay_sec", 0.5) or 0.5
        ),
        "silence_timeout_sec": float(
            voice_settings.get("silence_timeout_sec", 5.0) or 5.0
        ),
        "vad_params": voice_vad_cfg,
    }


class VoiceMainAssemblyTests(unittest.TestCase):
    def _load_voice_settings(self, voice: dict) -> dict:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump({"voice": voice}, file)
            file.flush()
            cfg = load_config(file.name)
        return cfg.voice if isinstance(cfg.voice, dict) else {}

    def test_new_params_flow_from_config_into_channel(self):
        """config.json 配新字段 + vad 部分覆盖 → load_config 合并 → main.py
        装配推导 → VoiceChannel 三个新参数全部生效。"""
        voice_settings = self._load_voice_settings(
            {
                "enabled": True,
                "record_delay_sec": 1.0,
                "silence_timeout_sec": 10.0,
                "vad": {"energy_threshold": 500.0, "silence_end_sec": 2.0},
            }
        )
        kwargs = _derive_voice_kwargs(voice_settings)
        channel = VoiceChannel(MessageBus(), **kwargs)

        self.assertEqual(channel._record_delay_sec, 1.0)
        self.assertEqual(channel._silence_timeout_sec, 10.0)
        # vad 缺省子字段由 load_config 白名单合并补默认后整体透传
        self.assertEqual(
            channel._vad_params,
            {
                "energy_threshold": 500.0,
                "silence_end_sec": 2.0,
                "min_voice_sec": 0.3,
                "block_sec": 0.05,
            },
        )

    def test_missing_new_fields_fall_back_to_defaults(self):
        """旧 config.json 只有 voice.enabled → load_config 白名单合并补全
        默认（record_delay_sec=0.5 / silence_timeout_sec=5.0 / vad 默认子
        字典）→ 装配推导后渠道三个参数全部取默认，不崩、不回退空。"""
        voice_settings = self._load_voice_settings({"enabled": True})
        kwargs = _derive_voice_kwargs(voice_settings)
        channel = VoiceChannel(MessageBus(), **kwargs)

        self.assertEqual(channel._record_delay_sec, 0.5)
        self.assertEqual(channel._silence_timeout_sec, 5.0)
        self.assertEqual(
            channel._vad_params,
            {
                "energy_threshold": 400.0,
                "silence_end_sec": 1.2,
                "min_voice_sec": 0.3,
                "block_sec": 0.05,
            },
        )

    def test_non_dict_vad_is_guarded_to_none(self):
        """voice.vad 非 dict（如字符串）→ 装配处 isinstance 守卫回退 None，
        channel 用内置默认，不崩（与 kws 同模式）。"""
        voice_settings = self._load_voice_settings(
            {"enabled": True, "record_delay_sec": 0.8, "vad": "oops"}
        )
        kwargs = _derive_voice_kwargs(voice_settings)
        channel = VoiceChannel(MessageBus(), **kwargs)
        self.assertEqual(channel._record_delay_sec, 0.8)
        self.assertEqual(channel._vad_params, {})

    def test_channel_level_vad_keys_are_filtered_inside_channel(self):
        """vad 里混入渠道级字段（max_duration_sec/device）→ 渠道内部
        _sanitize_vad_params 过滤，不进 record_audio_vad。"""
        voice_settings = self._load_voice_settings(
            {
                "enabled": True,
                "vad": {
                    "energy_threshold": 600.0,
                    "max_duration_sec": 99,
                    "device": 3,
                },
            }
        )
        kwargs = _derive_voice_kwargs(voice_settings)
        channel = VoiceChannel(MessageBus(), **kwargs)
        self.assertNotIn("max_duration_sec", channel._vad_params)
        self.assertNotIn("device", channel._vad_params)
        self.assertEqual(channel._vad_params["energy_threshold"], 600.0)

    def test_defaults_consistent_with_config_and_constructor(self):
        """装配兜底默认与 config.py 默认、渠道构造器默认三方一致。"""
        settings = NanoClawConfig().voice
        kwargs = _derive_voice_kwargs(settings)
        channel = VoiceChannel(MessageBus(), **kwargs)
        self.assertEqual(channel._record_delay_sec, settings["record_delay_sec"])
        self.assertEqual(
            channel._silence_timeout_sec, settings["silence_timeout_sec"]
        )
        self.assertEqual(
            channel._vad_params, settings["vad"],
            "voice.vad 默认应整体透传进渠道（含 4 个 VAD 参数）",
        )


if __name__ == "__main__":
    unittest.main()
