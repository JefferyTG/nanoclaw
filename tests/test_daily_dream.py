"""Tests for TASK-011 做梦整理（dream_consolidate / DailyMemory.write_dream）。

覆盖：
- 固定结构：做梦整理写入固定分类，不产生重复分类标题；
- 去重：同一事实两次整理只落一条（行哈希兜底）；
- 合并：已有 daily 内容按分类保留，非固定分类不丢数据；
- 静默失败：模型异常 / 空结果不抛异常、不写盘；
- 回归：/clear 的 summarize_messages_to_daily 行为不变。
"""

import json
import os
import tempfile
import unittest
from datetime import datetime

from agent.daily import (
    DailyMemory,
    _fact_hash,
    _parse_daily_sections,
    _parse_dream_sections,
    _try_json_sections,
    dream_consolidate,
    summarize_messages_to_daily,
)
from providers.base import LLMResponse


class _DreamProvider:
    """固定返回做梦整理 JSON 的假 Provider；content 可替换。"""

    def __init__(self, content=None):
        self.requests = []
        self.content = content or json.dumps({
            "用户变化": ["用户偏好中文回复"],
            "项目进展": ["完成 TASK-011 第一阶段"],
            "会话总结": ["和用户聊了周末计划"],
        }, ensure_ascii=False)

    async def chat(self, messages, tools=None, model=None):
        self.requests.append(messages)
        return LLMResponse(self.content)


class _ErrorProvider:
    async def chat(self, messages, tools=None, model=None):
        raise RuntimeError("boom")


class _EmptyProvider:
    async def chat(self, messages, tools=None, model=None):
        return LLMResponse(None, finish_reason="error")


MESSAGES = [
    {"role": "user", "content": "我喜欢用中文交流"},
    {"role": "assistant", "content": "好的，之后都用中文回复。"},
    {"role": "user", "content": "今天我们完成了 TASK-011 第一阶段。"},
]


class DreamParseUnitTests(unittest.TestCase):
    def test_parse_daily_sections(self):
        content = (
            "# 2026-08-05\n\n"
            "## 会话总结\n\n"
            "- 聊了 A\n"
            "- 聊了 B\n\n"
            "## 压缩前保存\n\n"
            "- 压缩了 3 条\n"
        )
        sections = _parse_daily_sections(content)
        self.assertEqual(sections, [
            ["会话总结", ["聊了 A", "聊了 B"]],
            ["压缩前保存", ["压缩了 3 条"]],
        ])

    def test_fact_hash_normalizes_dash_and_whitespace(self):
        self.assertEqual(_fact_hash("- 用户偏好中文"), _fact_hash("用户偏好中文"))
        self.assertEqual(_fact_hash(" 用户偏好中文 "), _fact_hash("用户偏好中文"))

    def test_try_json_sections_with_code_fence(self):
        text = '```json\n{"用户变化": ["A"], "未知": ["x"]}\n```'
        obj = _try_json_sections(text)
        self.assertEqual(obj, {"用户变化": ["A"], "未知": ["x"]})

    def test_parse_dream_sections_filters_non_categories(self):
        content = json.dumps({
            "用户变化": ["A"],
            "项目进展": ["B"],
            "其他字段": ["C"],
        }, ensure_ascii=False)
        obj = _parse_dream_sections(content)
        self.assertEqual(obj, {"用户变化": ["A"], "项目进展": ["B"]})

    def test_parse_dream_sections_markdown_fallback(self):
        content = (
            "## 用户变化\n- 用户换了新设备\n\n"
            "## 项目进展\n- 完成 X\n\n"
            "## 未知\n- 忽略\n"
        )
        obj = _parse_dream_sections(content)
        self.assertEqual(obj, {"用户变化": ["用户换了新设备"], "项目进展": ["完成 X"]})


class DreamConsolidateTests(unittest.IsolatedAsyncioTestCase):
    def _make_daily(self, tmp):
        return DailyMemory(os.path.join(tmp, "memory"))

    async def test_dream_writes_fixed_structure(self):
        """做梦整理写入固定分类结构，且不产生重复分类标题。"""
        with tempfile.TemporaryDirectory() as tmp:
            daily = self._make_daily(tmp)
            provider = _DreamProvider()
            await dream_consolidate(provider, daily, "2026-08-05", MESSAGES)

            content = daily.read("2026-08-05")
            self.assertIn("# 2026-08-05", content)
            self.assertEqual(content.count("## 用户变化"), 1)
            self.assertEqual(content.count("## 项目进展"), 1)
            self.assertEqual(content.count("## 会话总结"), 1)
            self.assertIn("- 用户偏好中文回复", content)
            self.assertIn("- 完成 TASK-011 第一阶段", content)

    async def test_dream_dedup_same_fact_twice(self):
        """同一事实两次整理只落一条（行哈希兜底去重）。"""
        with tempfile.TemporaryDirectory() as tmp:
            daily = self._make_daily(tmp)
            provider = _DreamProvider()
            await dream_consolidate(provider, daily, "2026-08-05", MESSAGES)
            await dream_consolidate(provider, daily, "2026-08-05", MESSAGES)

            content = daily.read("2026-08-05")
            self.assertEqual(content.count("- 用户偏好中文回复"), 1)
            self.assertEqual(content.count("- 完成 TASK-011 第一阶段"), 1)
            self.assertEqual(content.count("- 和用户聊了周末计划"), 1)
            self.assertEqual(content.count("## 用户变化"), 1)

    async def test_dream_dedup_within_single_output(self):
        """单次模型输出内部重复的事实也只落一条。"""
        with tempfile.TemporaryDirectory() as tmp:
            daily = self._make_daily(tmp)
            provider = _DreamProvider(json.dumps({
                "会话总结": ["同一条事实", "同一条事实"],
            }, ensure_ascii=False))
            await dream_consolidate(provider, daily, "2026-08-05", MESSAGES)
            content = daily.read("2026-08-05")
            self.assertEqual(content.count("- 同一条事实"), 1)

    async def test_dream_merges_existing_and_preserves_other_categories(self):
        """已有 daily 内容按分类保留，非固定分类不丢数据。"""
        with tempfile.TemporaryDirectory() as tmp:
            daily = self._make_daily(tmp)
            # 先模拟 /clear 写入 + 历史遗留分类
            daily.append("会话总结", "已有会话事实")
            daily.append("压缩前保存", "旧审计事实")
            provider = _DreamProvider()
            await dream_consolidate(provider, daily, "2026-08-05", MESSAGES)

            content = daily.read("2026-08-05")
            self.assertIn("- 已有会话事实", content)          # 已有内容保留
            self.assertIn("- 旧审计事实", content)            # 非固定分类保留
            self.assertEqual(content.count("## 压缩前保存"), 1)
            self.assertEqual(content.count("## 会话总结"), 1)  # 不重复标题

    async def test_dream_passes_existing_content_to_model(self):
        """模型提示中携带已记录内容，供模型做语义去重判断。"""
        with tempfile.TemporaryDirectory() as tmp:
            daily = self._make_daily(tmp)
            daily.append("会话总结", "已有会话事实")
            provider = _DreamProvider()
            await dream_consolidate(provider, daily, "2026-08-05", MESSAGES)
            self.assertEqual(len(provider.requests), 1)
            prompt = provider.requests[0][0]["content"]
            self.assertIn("已记录的 daily 内容", prompt)
            self.assertIn("已有会话事实", prompt)

    async def test_dream_silent_on_provider_error(self):
        """模型异常时静默返回，不写盘、不抛异常。"""
        with tempfile.TemporaryDirectory() as tmp:
            daily = self._make_daily(tmp)
            provider = _ErrorProvider()
            await dream_consolidate(provider, daily, "2026-08-05", MESSAGES)
            self.assertEqual(daily.read("2026-08-05"), "")

    async def test_dream_silent_on_empty_response(self):
        """模型返回 error / 空内容时静默返回。"""
        with tempfile.TemporaryDirectory() as tmp:
            daily = self._make_daily(tmp)
            provider = _EmptyProvider()
            await dream_consolidate(provider, daily, "2026-08-05", MESSAGES)
            self.assertEqual(daily.read("2026-08-05"), "")

    async def test_dream_noop_when_daily_none(self):
        """daily 为 None 时直接返回，不调模型。"""
        provider = _DreamProvider()
        await dream_consolidate(provider, None, "2026-08-05", MESSAGES)
        self.assertEqual(provider.requests, [])

    async def test_dream_noop_when_no_messages(self):
        """messages 为空时直接返回，不调模型。"""
        with tempfile.TemporaryDirectory() as tmp:
            daily = self._make_daily(tmp)
            provider = _DreamProvider()
            await dream_consolidate(provider, daily, "2026-08-05", [])
            self.assertEqual(provider.requests, [])
            self.assertEqual(daily.read("2026-08-05"), "")


class ClearStillWritesDailyTests(unittest.IsolatedAsyncioTestCase):
    """回归：/clear 触发写入行为不变（append-only + 分类标题）。"""

    async def test_clear_still_writes_daily(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = DailyMemory(os.path.join(tmp, "memory"))
            provider = _DreamProvider("值得记录的事实")
            await summarize_messages_to_daily(
                provider, daily, MESSAGES, category="会话总结"
            )
            today = datetime.now().strftime("%Y-%m-%d")
            content = daily.read(today)
            self.assertIn("## 会话总结", content)
            self.assertIn("- 值得记录的事实", content)


if __name__ == "__main__":
    unittest.main()
