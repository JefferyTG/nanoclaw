import json
import unittest

from agent.cache_observability import PromptCacheObserver, stable_text_hash
from providers.usage import PromptCacheUsage


class PromptCacheObservabilityTests(unittest.TestCase):
    def test_turn_ratio_is_weighted_by_token_totals(self):
        lines = []
        observer = PromptCacheObserver(lines.append)
        turn = observer.start_turn(
            system_hash=stable_text_hash("private system"),
            tools_hash="a" * 64,
            history_messages=7,
        )
        turn.record(
            PromptCacheUsage(100, 50, 50, 0.5, "available"), tool_iteration=0
        )
        turn.record(
            PromptCacheUsage(300, 270, 30, 0.9, "available"), tool_iteration=1
        )

        total = turn.finish()

        self.assertEqual(total.input_tokens, 400)
        self.assertEqual(total.cached_input_tokens, 320)
        self.assertEqual(total.uncached_input_tokens, 80)
        self.assertEqual(total.cache_ratio, 0.8)
        self.assertNotEqual(total.cache_ratio, 0.7)  # not mean(0.5, 0.9)
        payloads = [json.loads(line.removeprefix("[prompt-cache] ")) for line in lines]
        self.assertEqual([p["event"] for p in payloads], [
            "prompt_cache_call", "prompt_cache_call", "prompt_cache_turn"
        ])
        serialized = "\n".join(lines)
        self.assertNotIn("private system", serialized)
        self.assertNotIn("user message", serialized)
        self.assertNotIn("tool arguments", serialized)

    def test_missing_cached_usage_never_becomes_zero_hit(self):
        observer = PromptCacheObserver(lambda _: None)
        turn = observer.start_turn(
            system_hash="system", tools_hash="tools", history_messages=0
        )
        turn.record(PromptCacheUsage(input_tokens=123), tool_iteration=0)

        total = turn.finish()

        self.assertEqual(total.input_tokens, 123)
        self.assertIsNone(total.cached_input_tokens)
        self.assertIsNone(total.uncached_input_tokens)
        self.assertIsNone(total.cache_ratio)
        self.assertEqual(total.availability, "partial")


if __name__ == "__main__":
    unittest.main()
