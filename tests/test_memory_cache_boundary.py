"""Tests for context-size estimation and deterministic compaction boundaries.

TASK-006 适配：``MemoryConsolidation`` → ``ContextCompactor``，``maybe_consolidate``
→ ``maybe_compact``（返回 ``CompactionResult``）；压缩不再写 daily（无共享可变
字段 ``last_estimate`` / ``last_consolidation``）。
"""

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from agent.memory import CompactionResult, ContextCompactor
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


class ContextCompactorTests(unittest.IsolatedAsyncioTestCase):
    async def test_estimate_includes_multimodal_payload_and_tool_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            compactor = ContextCompactor(_SummaryProvider(), tmp)
            multimodal_msg = {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/png;base64," + "a" * 400,
                    }},
                ],
            }
            plain_msg = {
                "role": "user",
                "content": [{"type": "text", "text": "look" + "a" * 400}],
            }
            tools = [{"type": "function", "function": {
                "name": "inspect", "description": "b" * 200,
                "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
            }}]

            with_tools = compactor.estimate_tokens([multimodal_msg], tools)
            without_tools = compactor.estimate_tokens([multimodal_msg])
            multimodal = compactor.estimate_tokens([multimodal_msg])
            plain = compactor.estimate_tokens([plain_msg])

        # 工具 schema 计入估算；图片 base64（0.75 token/字符）高于等长纯文本
        self.assertGreater(with_tools, without_tools)
        self.assertGreater(multimodal, plain)
        self.assertGreater(with_tools, 100)

    async def test_compaction_result_fields_and_stable_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            compactor = ContextCompactor(_SummaryProvider(), tmp, token_budget=20)
            messages = [{"role": "system", "content": "fixed"}] + [
                {"role": "user" if i % 2 else "assistant", "content": "x" * 80}
                for i in range(8)
            ]
            result = await compactor.maybe_compact(
                messages, tools=[{"type": "function", "function": {"name": "x"}}]
            )

        self.assertIsInstance(result, CompactionResult)
        self.assertTrue(result.compacted)
        self.assertEqual(result.messages[0], messages[0])
        self.assertEqual(result.messages[-6:], messages[-6:])
        self.assertEqual(result.token_budget, 20)
        self.assertEqual(result.summarized_messages, 2)
        self.assertEqual(result.preserved_tail_messages, 6)
        self.assertGreater(result.estimated_tokens, result.token_budget)

    async def test_within_budget_returns_same_messages_uncompacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            compactor = ContextCompactor(_SummaryProvider(), tmp, token_budget=10_000)
            messages = [{"role": "system", "content": "fixed"}, {"role": "user", "content": "hi"}]
            result = await compactor.maybe_compact(messages)
        self.assertIs(result.messages, messages)
        self.assertFalse(result.compacted)
        self.assertEqual(result.summarized_messages, 0)
        self.assertEqual(result.preserved_tail_messages, 0)

    async def test_failed_summary_preserves_original_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            compactor = ContextCompactor(
                _FailingSummaryProvider(), tmp, token_budget=20
            )
            messages = [{"role": "system", "content": "fixed"}] + [
                {"role": "user", "content": "x" * 80} for _ in range(8)
            ]

            result = await compactor.maybe_compact(messages)

        self.assertIs(result.messages, messages)
        self.assertFalse(result.compacted)
        self.assertEqual(result.summarized_messages, 0)

    async def test_tail_never_splits_tool_exchange(self):
        with tempfile.TemporaryDirectory() as tmp:
            compactor = ContextCompactor(_SummaryProvider(), tmp, token_budget=20)
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

            result = await compactor.maybe_compact(messages)

        self.assertEqual(result.messages[2], messages[4])
        self.assertEqual(result.messages[3:5], messages[5:7])

    async def test_multimodal_bytes_are_not_sent_to_text_summarizer(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = _SummaryProvider()
            compactor = ContextCompactor(provider, tmp, token_budget=20)
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

            await compactor.maybe_compact(messages)

        summary_prompt = provider.requests[0][0]["content"]
        self.assertNotIn(secret_base64, summary_prompt)
        self.assertNotIn("data:image", summary_prompt)
        self.assertIn("图片内容已省略", summary_prompt)

    # —— TASK-006 新增：独立实例互不影响 ——

    async def test_independent_instances_do_not_interfere(self):
        """两个不同 session 的压缩互不影响：A 压缩不改变 B 的任何状态/判断。"""
        with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
            compactor_a = ContextCompactor(_SummaryProvider(), tmp_a, token_budget=20)
            compactor_b = ContextCompactor(_SummaryProvider(), tmp_b, token_budget=100_000)

            # 会话 A 超预算 → 压缩
            over_budget = [{"role": "system", "content": "fixed"}] + [
                {"role": "user" if i % 2 else "assistant", "content": "x" * 80}
                for i in range(8)
            ]
            result_a = await compactor_a.maybe_compact(over_budget)

            # 会话 B 预算内 → 不压缩；A 的压缩结果/状态不泄漏到 B
            within_budget = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
            result_b = await compactor_b.maybe_compact(within_budget)

            self.assertTrue(result_a.compacted)
            self.assertFalse(result_b.compacted)
            self.assertIs(result_b.messages, within_budget)
            self.assertEqual(compactor_b.token_budget, 100_000)
            # 无共享可变字段（旧 last_consolidation / last_estimate 已删除）
            self.assertFalse(hasattr(compactor_a, "last_consolidation"))
            self.assertFalse(hasattr(compactor_a, "last_estimate"))
            self.assertFalse(hasattr(compactor_b, "last_consolidation"))
            self.assertFalse(hasattr(compactor_b, "last_estimate"))

    async def test_same_instance_no_cross_turn_state_leak(self):
        """同一实例连续调用：上一轮压缩不影响下一轮「预算内不压缩」的判断。"""
        with tempfile.TemporaryDirectory() as tmp:
            compactor = ContextCompactor(_SummaryProvider(), tmp, token_budget=20)
            over_budget = [{"role": "system", "content": "fixed"}] + [
                {"role": "user", "content": "x" * 80} for _ in range(8)
            ]
            first = await compactor.maybe_compact(over_budget)
            self.assertTrue(first.compacted)

            within_budget = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
            second = await compactor.maybe_compact(within_budget)
            self.assertFalse(second.compacted)
            self.assertIs(second.messages, within_budget)

    # —— TASK-006 新增：压缩不写 daily ——

    async def test_compaction_never_writes_daily(self):
        """压缩流程不再调用 summarize_messages_to_daily / DailyMemory.append，
        也不在磁盘上产生 daily 目录（mock DailyMemory 断言未被调用）。"""
        with tempfile.TemporaryDirectory() as tmp:
            compactor = ContextCompactor(_SummaryProvider(), tmp, token_budget=20)
            messages = [{"role": "system", "content": "fixed"}] + [
                {"role": "user" if i % 2 else "assistant", "content": "x" * 80}
                for i in range(8)
            ]
            with patch("agent.daily.DailyMemory.append") as mock_append, \
                    patch("agent.daily.summarize_messages_to_daily", new=AsyncMock()) as mock_summarize:
                result = await compactor.maybe_compact(messages)

            self.assertTrue(result.compacted)
            mock_append.assert_not_called()
            mock_summarize.assert_not_called()
            # 压缩只写 HISTORY.md（审计轨迹），不创建 daily 目录
            history_path = os.path.join(tmp, "memory", "HISTORY.md")
            self.assertTrue(os.path.exists(history_path))
            daily_dir = os.path.join(tmp, "memory", "daily")
            self.assertFalse(os.path.exists(daily_dir))


if __name__ == "__main__":
    unittest.main()
