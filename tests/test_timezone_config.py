import json
import tempfile
import unittest
from pathlib import Path

from config import NanoClawConfig, load_config, save_config


class TimezoneConfigTests(unittest.TestCase):
    def test_default_and_round_trip(self):
        self.assertEqual(NanoClawConfig().timezone, "Asia/Shanghai")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            config = NanoClawConfig(timezone="America/New_York")
            save_config(config, str(path))

            self.assertEqual(load_config(str(path)).timezone, "America/New_York")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["timezone"],
                             "America/New_York")

    def test_invalid_file_timezone_falls_back_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text('{"timezone":"not/a-zone"}', encoding="utf-8")
            self.assertEqual(load_config(str(path)).timezone, "Asia/Shanghai")


if __name__ == "__main__":
    unittest.main()
