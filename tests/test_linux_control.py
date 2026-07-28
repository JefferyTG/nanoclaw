"""Static and stopped-state checks for the Linux process controller."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "nanoclawctl"


class LinuxControlScriptTests(unittest.TestCase):
    def test_script_has_valid_bash_syntax_and_is_executable(self):
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_status_and_stop_are_safe_when_not_running(self):
        with tempfile.TemporaryDirectory() as run_dir:
            env = dict(os.environ, NANOCLAW_RUN_DIR=run_dir)
            status = subprocess.run(
                [str(SCRIPT), "status"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(status.returncode, 3)
            self.assertIn("未运行", status.stdout)

            stop = subprocess.run(
                [str(SCRIPT), "stop"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(stop.returncode, 0, stop.stderr)
            self.assertIn("未运行", stop.stdout)


if __name__ == "__main__":
    unittest.main()
