"""Static regression checks for the no-build Web subagent activity UI."""

import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "webui" / "index.html"


def _script() -> str:
    html = PAGE.read_text(encoding="utf-8")
    match = re.search(r"<script>\s*(.*?)\s*</script>", html, re.DOTALL)
    assert match, "inline Web UI script is missing"
    return match.group(1)


class WebSubagentUiTests(unittest.TestCase):
    def test_subagent_events_have_isolated_renderer_and_timer_cleanup(self):
        script = _script()
        self.assertIn("function onSubagentEvent", script)
        self.assertIn("t === 'subagent_event'", script)
        self.assertIn("clearSubagentTimers(cur)", script)
        self.assertIn("tgt.closest('.sa-head')", script)
        self.assertIn("classList.toggle('expanded')", script)
        self.assertIn("status === 'cancelled'", script)
        self.assertIn("status === 'timed_out'", script)
        self.assertIn("status === 'completed'", script)
        self.assertIn("panel.output.textContent.indexOf(finalContent) === -1", script)
        self.assertIn("root.style.marginLeft", script)
        # The subagent token branch must not call the parent answer/TTS functions.
        branch = re.search(
            r"type === 'thinking' \|\| type === 'token'\) \{(.*?)\n    \} else if \(type === 'tool_call'",
            script,
            re.DOTALL,
        )
        self.assertIsNotNone(branch)
        self.assertNotIn("collectTtsToken", branch.group(1))
        self.assertNotIn("turn.answer", branch.group(1))

    def test_history_supports_new_metadata_and_legacy_spawn_tool_records(self):
        script = _script()
        self.assertIn("m.subagent_runs", script)
        self.assertIn("Array.isArray(run.tool_steps)", script)
        self.assertIn("toolResults[run.tool_call_id]", script)
        self.assertIn("function oldSpawnFallback", script)
        self.assertIn("toolResults[call.id]", script)
        self.assertIn("JSON.parse(fn.arguments || '{}')", script)
        # Images remain driven by generated_images, not duplicated from subagent metadata.
        self.assertIn("m.generated_images", script)
        self.assertNotIn("run.generated_images", script)

    def test_inline_script_is_valid_javascript(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8") as handle:
            handle.write(_script())
            handle.flush()
            result = subprocess.run(
                ["node", "--check", handle.name], text=True, capture_output=True, check=False
            )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
