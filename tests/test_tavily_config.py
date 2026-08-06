"""TASK-016 tavily_api_key 配置层单元测试。

覆盖：默认空值、config.json 读取、环境变量 TAVILY_API_KEY 最高优先级覆盖、
save_config 白名单写回、config.example.json 含示例字段（空值、不含真实 key）。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import NanoClawConfig, load_config, save_config


class TavilyConfigTests(unittest.TestCase):
    def test_default_is_empty(self):
        self.assertEqual(NanoClawConfig().tavily_api_key, "")

    def test_load_from_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text('{"tavily_api_key":"tvly-from-file"}', encoding="utf-8")
            self.assertEqual(load_config(str(path)).tavily_api_key, "tvly-from-file")

    def test_env_var_overrides_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text('{"tavily_api_key":"tvly-from-file"}', encoding="utf-8")
            with patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-from-env"}, clear=False):
                self.assertEqual(
                    load_config(str(path)).tavily_api_key, "tvly-from-env"
                )

    def test_missing_env_and_file_fallback_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text("{}", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                self.assertEqual(load_config(str(path)).tavily_api_key, "")

    def test_save_config_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            config = NanoClawConfig(tavily_api_key="tvly-round-trip")
            save_config(config, str(path))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["tavily_api_key"], "tvly-round-trip")
            self.assertEqual(load_config(str(path)).tavily_api_key, "tvly-round-trip")

    def test_example_json_has_empty_example_field(self):
        example = json.loads(
            Path("config.example.json").read_text(encoding="utf-8")
        )
        self.assertIn("tavily_api_key", example)
        self.assertEqual(example["tavily_api_key"], "")


if __name__ == "__main__":
    unittest.main()
