"""Tests for context-size estimation and deterministic consolidation boundaries."""

import tempfile
import unittest

from agent.memory import MemoryConsolidation
from agent.daily import _messages_to_text as daily_messages_to_text
from providers.base import LLMResponse


class _SummaryProvider:
    def __init__(self):
        self.requests = []

    async def chat(self, messages, tools=None, model=None):
        self.requests.append(messages)
        return LLMResponse("stable summary")


class _FailingSummaryProvider:
    async def chat(self, messages, tools=None, model=None):
        return LLMResponse(None, finish_reason="error")


class MemoryCacheBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_estimate_includes_multimodal_payload_and_tool_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryConsolidation(_SummaryProvider(), tmp)
            messages = [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/png;base64," + "a" * 400,
                    }},
                ],
            }]
            tools = [{"type": "function", "function": {
                "name": "inspect", "description": "b" * 200,
                "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
            }}]

            total = memory.estimate_tokens(messages, tools)

        self.assertGreater(total, 100)
        self.assertGreater(memory.last_estimate["tool_schema_tokens"], 0)
        self.assertEqual(memory.last_estimate["tool_count"], 1)

    async def test_consolidation_reports_a_stable_head_and_tail_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryConsolidation(_SummaryProvider(), tmp, token_budget=20)
            messages = [{"role": "system", "content": "fixed"}] + [
                {"role": "user" if i % 2 else "assistant", "content": "x" * 80}
                for i in range(8)
            ]
            compressed = await memory.maybe_consolidate(
                messages, tools=[{"type": "function", "function": {"name": "x"}}]
            )

        self.assertEqual(compressed[0], messages[0])
        self.assertEqual(compressed[-6:], messages[-6:])
        self.assertEqual(memory.last_consolidation, {
            "consolidated": True,
            "estimated_tokens": memory.last_consolidation["estimated_tokens"],
            "token_budget": 20,
            "preserved_head_messages": 1,
            "preserved_tail_messages": 6,
            "summarized_messages": 2,
        })

    async def test_failed_summary_preserves_original_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryConsolidation(
                _FailingSummaryProvider(), tmp, token_budget=20
            )
            messages = [{"role": "system", "content": "fixed"}] + [
                {"role": "user", "content": "x" * 80} for _ in range(8)
            ]

            result = await memory.maybe_consolidate(messages)

        self.assertIs(result, messages)
        self.assertEqual(memory.last_consolidation["reason"], "summary_failed")

    async def test_tail_never_splits_tool_exchange(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryConsolidation(_SummaryProvider(), tmp, token_budget=20)
            messages = [
                {"role": "system", "content": "fixed"},
                {"role": "user", "content": "old" * 50},
                {"role": "assistant", "content": "old" * 50},
                {"role": "user", "content": "old" * 50},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "type": "function", "function": {
                        "name": "x", "arguments": "{}"
                    }},
                    {"id": "c2", "type": "function", "function": {
                        "name": "x", "arguments": "{}"
                    }},
                ]},
                {"role": "tool", "tool_call_id": "c1", "content": "one"},
                {"role": "tool", "tool_call_id": "c2", "content": "two"},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": "next"},
                {"role": "assistant", "content": "answer"},
            ]

            result = await memory.maybe_consolidate(messages)

        self.assertEqual(result[2], messages[4])
        self.assertEqual(result[3:5], messages[5:7])

    async def test_multimodal_bytes_are_not_sent_to_text_summarizer(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = _SummaryProvider()
            memory = MemoryConsolidation(provider, tmp, token_budget=20)
            secret_base64 = "SENSITIVE_BASE64_PAYLOAD" * 20
            messages = [
                {"role": "system", "content": "fixed"},
                {"role": "user", "content": [
                    {"type": "text", "text": "inspect this"},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/png;base64," + secret_base64,
                    }},
                ]},
            ] + [
                {"role": "assistant", "content": "tail" * 30} for _ in range(7)
            ]

            await memory.maybe_consolidate(messages)

        summary_prompt = provider.requests[0][0]["content"]
        self.assertNotIn(secret_base64, summary_prompt)
        self.assertNotIn("data:image", summary_prompt)
        self.assertIn("图片内容已省略", summary_prompt)
        daily_prompt = daily_messages_to_text(messages)
        self.assertNotIn(secret_base64, daily_prompt)
        self.assertNotIn("data:image", daily_prompt)


if __name__ == "__main__":
    unittest.main()
