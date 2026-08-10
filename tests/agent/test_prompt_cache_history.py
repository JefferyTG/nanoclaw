"""Regression coverage for deterministic cross-turn history replay."""

import tempfile
import unittest

from agent.history import canonicalize_history
from session.manager import SessionManager


def _tool_call(tool_id: str) -> dict:
    return {
        "id": tool_id,
        "type": "function",
        "function": {"name": "lookup", "arguments": "{}"},
    }


class PromptCacheHistoryTests(unittest.TestCase):
    def test_persisted_and_restarted_history_have_identical_canonical_shape(self):
        live_history = [
            {"role": "user", "content": "查一下"},
            {
                "role": "assistant", "content": None,
                "reasoning_content": "temporary private reasoning",
                "tool_calls": [_tool_call("call-1"), _tool_call("call-2")],
            },
            {"role": "tool", "tool_call_id": "call-2", "content": "two"},
            {"role": "tool", "tool_call_id": "call-1", "content": "one"},
            {"role": "assistant", "content": "完成"},
        ]
        expected = canonicalize_history(live_history)

        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(tmp)
            for message in live_history:
                sessions.save_message("cli:cache", message)

            # A fresh manager represents process restart, not reuse of memory.
            replayed = SessionManager(tmp).get_history("cli:cache")

        self.assertEqual(replayed, expected)
        self.assertEqual(replayed[1]["reasoning_content"], "temporary private reasoning")
        self.assertEqual([m["role"] for m in replayed], [
            "user", "assistant", "tool", "tool", "assistant",
        ])
        self.assertEqual([m["tool_call_id"] for m in replayed[2:4]], [
            "call-1", "call-2",
        ])

    def test_orphan_tool_never_crosses_a_canonical_boundary(self):
        history = canonicalize_history([
            {"role": "tool", "tool_call_id": "orphan", "content": "bad"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ])

        self.assertEqual(history, [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ])


if __name__ == "__main__":
    unittest.main()
