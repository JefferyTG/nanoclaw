"""Tests for automatic wall-clock timestamp injection on inbound user messages.

Every inbound user turn gets a "[YYYY-MM-DD HH:MM]" prefix (instance timezone)
injected once, right before the message enters the AgentLoop, so the model has
a reliable "now" without having to call get_current_time.
"""

import asyncio
import re
import unittest
from datetime import datetime, timezone

from bus.queue import InboundMessage, MessageBus
from channels.base import Channel
from gateway import Gateway, _timestamp_prefix

PREFIX_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] ")


class _Channel(Channel):
    async def start(self):
        return None

    async def send(self, message):
        return None


class _Agent:
    def __init__(self):
        self.calls = []

    async def run(self, content, images=None, stream_sink=None):
        self.calls.append((content, images, stream_sink))
        return "ok"


class _Bootstrapper:
    """Fake onboarding: records raw text, lets every message pass through."""

    def __init__(self):
        self.seen = []

    async def handle(self, session_key, text):
        self.seen.append(text)
        return None  # identity already ready


class TimestampInjectionTests(unittest.IsolatedAsyncioTestCase):
    def test_timestamp_prefix_format_with_fixed_clock(self):
        fixed = datetime(2026, 7, 31, 15, 51, 23, tzinfo=timezone.utc)
        self.assertEqual(
            _timestamp_prefix("Asia/Shanghai", now=fixed), "[2026-07-31 23:51]"
        )
        self.assertEqual(_timestamp_prefix("UTC", now=fixed), "[2026-07-31 15:51]")

    def test_timestamp_prefix_has_only_date_and_minute(self):
        fixed = datetime(2026, 7, 31, 15, 51, 59, tzinfo=timezone.utc)
        prefix = _timestamp_prefix("Asia/Shanghai", now=fixed)
        self.assertRegex(prefix, r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]$")
        # 不包含秒 / 星期 / 时区偏移
        self.assertNotIn(":59", prefix)
        self.assertNotIn("Friday", prefix)
        self.assertNotIn("+08", prefix)

    async def test_user_message_gets_timestamp_prefix_before_agent(self):
        bus = MessageBus()
        agent = _Agent()
        gateway = Gateway(bus, [_Channel("cli", bus)], lambda _key: agent)

        await gateway._handle_one(
            InboundMessage("cli", "local0", "direct", "今天天气如何"),
            "cli:local0",
            asyncio.Lock(),
        )
        content = agent.calls[0][0]
        self.assertTrue(PREFIX_RE.match(content), content)
        self.assertTrue(content.endswith("今天天气如何"), content)

        # 第二轮同样注入（时间戳随各轮消息进入时生成，追加式固定保存）
        await gateway._handle_one(
            InboundMessage("cli", "local0", "direct", "第二句"),
            "cli:local0",
            asyncio.Lock(),
        )
        self.assertTrue(PREFIX_RE.match(agent.calls[1][0]), agent.calls[1][0])
        self.assertTrue(agent.calls[1][0].endswith("第二句"), agent.calls[1][0])

    async def test_image_default_prompt_gets_timestamp_prefix(self):
        bus = MessageBus()
        agent = _Agent()
        gateway = Gateway(bus, [_Channel("feishu", bus)], lambda _key: agent)

        await gateway._handle_one(
            InboundMessage("feishu", "chat:0", "chat", "请分析这张图片。"),
            "feishu:chat:0",
            asyncio.Lock(),
        )
        content = agent.calls[0][0]
        self.assertTrue(PREFIX_RE.match(content), content)
        self.assertTrue(content.endswith("请分析这张图片。"), content)

    async def test_bootstrapper_still_sees_raw_text(self):
        """人设引导流程不受影响：首次引导消息用原文，不注入时间戳。"""
        bus = MessageBus()
        agent = _Agent()
        bootstrapper = _Bootstrapper()
        gateway = Gateway(
            bus, [_Channel("web", bus)], lambda _key: agent, bootstrapper
        )

        await gateway._handle_one(
            InboundMessage("web", "u1", "c1", "原始文本"),
            "web:u1",
            asyncio.Lock(),
        )
        self.assertEqual(bootstrapper.seen, ["原始文本"])
        content = agent.calls[0][0]
        self.assertTrue(PREFIX_RE.match(content), content)
        self.assertTrue(content.endswith("原始文本"), content)


if __name__ == "__main__":
    unittest.main()
