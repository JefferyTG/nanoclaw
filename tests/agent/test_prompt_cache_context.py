import tempfile
import unittest
from pathlib import Path

from agent.context import ContextBuilder


class PromptCacheContextTests(unittest.TestCase):
    def _builder(self, workspace: str, summary: str = "- coding：编码") -> ContextBuilder:
        root = Path(workspace)
        (root / "workspace" / "memory").mkdir(parents=True)
        (root / "identity.md").write_text("identity v1", encoding="utf-8")
        (root / "workspace" / "memory" / "USER.md").write_text("user v1", encoding="utf-8")
        (root / "workspace" / "memory" / "MEMORY.md").write_text("memory v1", encoding="utf-8")
        return ContextBuilder(workspace, skills_summary="- skill：stable", agents_summary_provider=lambda: summary)

    def test_system_prompt_has_no_wall_clock_and_is_stable_across_builds(self):
        with tempfile.TemporaryDirectory() as workspace:
            builder = self._builder(workspace)
            self.assertEqual(builder.build_system_prompt(), builder.build_system_prompt())
            self.assertNotIn("【当前时间】", builder.build_system_prompt())
            self.assertIn("get_current_time", builder.build_system_prompt())

    def test_next_turn_extends_previous_request_with_exact_prefix(self):
        with tempfile.TemporaryDirectory() as workspace:
            builder = self._builder(workspace)
            first = builder.build_messages(current_message="first")
            second = builder.build_messages(
                history=[{"role": "user", "content": "first"}, {"role": "assistant", "content": "answer"}],
                current_message="second",
            )
            self.assertEqual(second[: len(first)], first)

    def test_files_and_agent_summary_change_only_after_explicit_refresh(self):
        with tempfile.TemporaryDirectory() as workspace:
            state = {"summary": "- coding：v1"}
            builder = self._builder(workspace, state["summary"])
            # Use a provider that can change after the session snapshot.
            builder.agents_summary_provider = lambda: state["summary"]
            before = builder.build_system_prompt()
            Path(workspace, "identity.md").write_text("identity v2", encoding="utf-8")
            state["summary"] = "- writing：v2"
            self.assertEqual(before, builder.build_system_prompt())
            builder.refresh_context()
            refreshed = builder.build_system_prompt()
            self.assertIn("identity v2", refreshed)
            self.assertIn("writing：v2", refreshed)


if __name__ == "__main__":
    unittest.main()
