"""Tests for first-run identity onboarding and Gateway integration."""

import asyncio
import os
import tempfile
import unittest

from agent.identity import DEFAULT_IDENTITY, IDENTITY_PROMPT, IdentityBootstrapper
from bus.queue import InboundMessage, MessageBus
from channels.base import Channel
from gateway import Gateway


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
        return "normal reply"


class IdentityBootstrapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompts_then_atomically_creates_identity(self):
        with tempfile.TemporaryDirectory() as workspace:
            bootstrapper = IdentityBootstrapper(workspace)

            first = await bootstrapper.handle("web:one", "原始任务")
            self.assertEqual(first, IDENTITY_PROMPT)
            self.assertFalse(os.path.exists(os.path.join(workspace, "identity.md")))

            second = await bootstrapper.handle(
                "web:one", "你叫小南，是我的技术搭档，回答先给结论。"
            )
            self.assertIn("已根据你的描述", second)
            path = os.path.join(workspace, "identity.md")
            with open(path, "r", encoding="utf-8") as handle:
                content = handle.read()
            self.assertIn("你叫小南", content)
            self.assertIn("执行原则", content)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertIsNone(await bootstrapper.handle("web:two", "正常任务"))

    async def test_default_and_concurrent_sessions_share_one_instance_identity(self):
        with tempfile.TemporaryDirectory() as workspace:
            bootstrapper = IdentityBootstrapper(workspace)
            prompts = await asyncio.gather(
                bootstrapper.handle("web:one", "任务一"),
                bootstrapper.handle("feishu:two", "任务二"),
            )
            self.assertEqual(prompts, [IDENTITY_PROMPT, IDENTITY_PROMPT])

            reply = await bootstrapper.handle("feishu:two", "/default")
            self.assertIn("默认人设", reply)
            with open(
                os.path.join(workspace, "identity.md"), "r", encoding="utf-8"
            ) as handle:
                self.assertEqual(handle.read().strip(), DEFAULT_IDENTITY)
            self.assertIsNone(await bootstrapper.handle("web:one", "不应被消费"))

    async def test_rejects_identity_path_outside_workspace(self):
        with tempfile.TemporaryDirectory() as workspace:
            with self.assertRaises(ValueError):
                IdentityBootstrapper(workspace, "../identity.md")
            with self.assertRaises(ValueError):
                IdentityBootstrapper(workspace, "/tmp/identity.md")

    async def test_whitespace_only_identity_still_requires_onboarding(self):
        with tempfile.TemporaryDirectory() as workspace:
            with open(
                os.path.join(workspace, "identity.md"), "w", encoding="utf-8"
            ) as handle:
                handle.write("  \n")
            bootstrapper = IdentityBootstrapper(workspace)
            self.assertEqual(
                await bootstrapper.handle("cli:local0", "hello"), IDENTITY_PROMPT
            )

    async def test_gateway_does_not_create_agent_until_onboarding_finishes(self):
        with tempfile.TemporaryDirectory() as workspace:
            bus = MessageBus()
            channel = _Channel("cli", bus)
            bootstrapper = IdentityBootstrapper(workspace)
            agents = []

            def factory(_session_key):
                agent = _Agent()
                agents.append(agent)
                return agent

            gateway = Gateway(bus, [channel], factory, bootstrapper)
            lock = asyncio.Lock()
            msg = InboundMessage("cli", "local0", "direct", "先处理这个任务")

            await gateway._handle_one(msg, "cli:local0", lock)
            prompt = await bus.consume_outbound()
            self.assertEqual(prompt.content, IDENTITY_PROMPT)
            self.assertFalse(prompt.streamed)
            self.assertEqual(agents, [])

            msg.content = "你是我的项目助手，说话简洁。"
            await gateway._handle_one(msg, "cli:local0", lock)
            created = await bus.consume_outbound()
            self.assertIn("生成人设文件", created.content)
            self.assertEqual(agents, [])

            msg.content = "重新发送的任务"
            await gateway._handle_one(msg, "cli:local0", lock)
            normal = await bus.consume_outbound()
            self.assertEqual(normal.content, "normal reply")
            self.assertEqual(len(agents), 1)
            # 消息经 Gateway 注入时间戳前缀后整体交给 Agent（内容仍完整保留）
            self.assertRegex(
                agents[0].calls[0][0],
                r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] 重新发送的任务$",
            )

    async def test_gateway_shutdown_cancels_and_waits_for_inflight_messages(self):
        bus = MessageBus()
        channel = _Channel("cli", bus)
        started = asyncio.Event()
        cleaned = asyncio.Event()

        class _BlockingAgent:
            async def run(self, content, images=None, stream_sink=None):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()

        gateway = Gateway(bus, [channel], lambda _key: _BlockingAgent())
        processor = asyncio.create_task(gateway._process_inbound())
        try:
            await bus.publish_inbound(
                InboundMessage("cli", "local0", "direct", "blocking task")
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertEqual(len(gateway._inflight_tasks), 1)
            await gateway.shutdown()
            self.assertTrue(cleaned.is_set())
            self.assertEqual(gateway._inflight_tasks, set())
        finally:
            processor.cancel()
            await asyncio.gather(processor, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
