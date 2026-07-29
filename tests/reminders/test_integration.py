"""Composition-level tests for reminder configuration and prompt rules."""

import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from agent.context import ContextBuilder
from agent.tools.reminders import (
    CancelReminderTool,
    CreateReminderTool,
    ListRemindersTool,
)
from config import NanoClawConfig, load_config
from bus.queue import MessageBus
from channels.weixin import encode_weixin_target
from main import build_reminder_service, publish_reminder_delivery
from reminders.models import DeliveryResult, TaskKind
from reminders.repository import ReminderRepository
from reminders.scheduler import ReminderScheduler
from reminders.service import AsyncReminderRepository, ReminderService


class ReminderIntegrationTests(unittest.TestCase):
    def test_reminder_config_defaults_and_partial_merge(self):
        defaults = NanoClawConfig().reminders
        self.assertTrue(defaults["enabled"])
        self.assertEqual(defaults["database_path"], "workspace/reminders.db")
        self.assertEqual(defaults["delivery_timeout_sec"], 30)

        with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as handle:
            json.dump({"reminders": {"max_sleep_seconds": 7200}}, handle)
            handle.flush()
            merged = load_config(handle.name).reminders

        self.assertEqual(merged["max_sleep_seconds"], 7200)
        self.assertEqual(merged["database_path"], "workspace/reminders.db")
        self.assertEqual(merged["max_delivery_attempts"], 3)
        self.assertEqual(merged["max_agent_attempts"], 3)

    def test_composition_uses_a_dedicated_database_under_workspace(self):
        with tempfile.TemporaryDirectory() as workspace:
            config = NanoClawConfig(
                workspace=workspace,
                reminders={
                    **NanoClawConfig().reminders,
                    "database_path": "state/reminders.sqlite3",
                },
            )
            repository, service = build_reminder_service(config)

            self.assertIsInstance(repository, ReminderRepository)
            self.assertIsInstance(service, ReminderService)
            self.assertTrue(Path(workspace, "state", "reminders.sqlite3").exists())

            config.reminders = {"enabled": False}
            self.assertEqual(build_reminder_service(config), (None, None))

    def test_system_prompt_requires_tools_and_disambiguation(self):
        with tempfile.TemporaryDirectory() as workspace:
            prompt = ContextBuilder(workspace).build_system_prompt()

        self.assertIn("create_reminder", prompt)
        self.assertIn("list_reminders", prompt)
        self.assertIn("cancel_reminder", prompt)
        self.assertIn("每隔一天/每隔两天", prompt)
        self.assertIn("/bind-reminders", prompt)
        self.assertIn("飞书或微信私聊", prompt)


class _FakeReminderService:
    def __init__(self):
        self.calls = []

    async def create_reminder(self, **kwargs):
        self.calls.append(("create", kwargs))
        return "created"

    async def list_reminders(self, **kwargs):
        self.calls.append(("list", kwargs))
        return "listed"

    async def cancel_reminder(self, task_id):
        self.calls.append(("cancel", task_id))
        return "cancelled"


class ReminderToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_delegate_without_accepting_chat_id(self):
        service = _FakeReminderService()
        create = CreateReminderTool(service)
        self.assertNotIn("chat_id", create.parameters["properties"])
        result = await create.execute(
            task_type="message",
            subject="喝水",
            delivery_text="该喝水啦。",
            start_at="2026-08-01T09:00:00",
            timezone="Asia/Shanghai",
            frequency="DAILY",
            count=1,
        )
        self.assertEqual(result, "created")
        self.assertEqual(service.calls[0][0], "create")

        self.assertEqual(await ListRemindersTool(service).execute(), "listed")
        self.assertEqual(await CancelReminderTool(service).execute(task_id=7), "cancelled")
        self.assertEqual(service.calls[-1], ("cancel", 7))

    async def test_cancel_rejects_invalid_id_before_service(self):
        service = _FakeReminderService()
        result = await CancelReminderTool(service).execute(task_id=True)
        self.assertIn("正整数", result)
        self.assertEqual(service.calls, [])


class ReminderRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 8, 1, 1, tzinfo=UTC)
        self.repository = ReminderRepository(
            Path(self.tempdir.name) / "reminders.sqlite3"
        )
        self.target = self.repository.bind_target(
            "feishu", "chat", "owner", self.now
        )

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    def create_task(self, kind: TaskKind, **payload):
        return self.repository.create_task(
            target_id=self.target.target_id,
            kind=kind,
            subject="test",
            dtstart_local=datetime(2026, 8, 1, 1),
            timezone="UTC",
            rrule="RRULE:FREQ=DAILY;COUNT=1",
            next_run_at_utc=self.now,
            now_utc=self.now,
            **payload,
        )

    def make_scheduler(self, agent_runner, deliver):
        adapter = AsyncReminderRepository(
            self.repository,
            clock=lambda: self.now,
            once_grace=timedelta(hours=1),
        )

        async def delivery(execution, output):
            result = await deliver(execution, output)
            adapter.remember_delivery_result(execution.id, result)
            return result

        scheduler = ReminderScheduler(
            adapter,
            agent_runner,
            delivery,
            clock=type("Clock", (), {"now": lambda _self: self.now})(),
            retry_delay=lambda _attempt: timedelta(minutes=1),
        )
        return scheduler

    async def test_service_requires_binding_then_creates_lists_and_cancels(self):
        self.repository.unbind_target("feishu", "owner", self.now)
        cleaned = []

        async def cleanup(task_id):
            cleaned.append(task_id)

        service = ReminderService(
            self.repository, now=lambda: self.now, cleanup_task=cleanup
        )
        missing = await service.create_reminder(
            task_type="message",
            subject="喝水",
            delivery_text="该喝水了。",
            start_at="2026-08-01T01:00:00",
            timezone="UTC",
            frequency="DAILY",
            count=1,
        )
        self.assertIn("/bind-reminders", missing)
        self.assertIn(
            "已绑定", await service.bind_feishu("new-chat", "owner")
        )

        created = await service.create_reminder(
            task_type="message",
            subject="喝水",
            delivery_text="该喝水了。",
            start_at="2026-08-01T01:00:00",
            timezone="UTC",
            frequency="DAILY",
            count=3,
        )
        self.assertIn("未来执行时间", created)
        self.assertEqual(created.count("\n- "), 3)
        self.assertIn("喝水", await service.list_reminders())

        task_id = self.repository.list_tasks_for_target(self.target.target_id)[0].id
        self.assertEqual(await service.cancel_reminder(task_id), f"已取消任务 #{task_id}。")
        self.assertEqual(cleaned, [task_id])
        self.assertIn("cancelled", await service.list_reminders(include_inactive=True))

    async def test_active_or_system_suspended_target_rejects_another_owner(self):
        service = ReminderService(self.repository, now=lambda: self.now)
        weixin_target = encode_weixin_target("account", "user")

        refused = await service.bind_weixin(
            weixin_target,
            weixin_target,
        )
        self.assertIn("/unbind-reminders", refused)

        await service.suspend_target_id(
            self.target.target_id,
            expected_binding_revision=self.target.binding_revision,
        )
        still_refused = await service.bind_weixin(weixin_target, weixin_target)
        self.assertIn("/unbind-reminders", still_refused)

        rebound = await service.bind_feishu("new-chat", "owner")
        self.assertIn("飞书私聊", rebound)

    async def test_owner_unbind_allows_weixin_rebind_and_keeps_tasks(self):
        task = self.create_task(TaskKind.MESSAGE, delivery_text="迁移后仍需发送。")
        service = ReminderService(self.repository, now=lambda: self.now)
        weixin_target = encode_weixin_target("account", "user")

        unbound = await service.unbind_feishu("chat", "owner")
        self.assertIn("任意支持的飞书或微信私聊", unbound)
        rebound = await service.bind_weixin(weixin_target, weixin_target)
        self.assertIn("微信私聊", rebound)

        target = self.repository.get_active_target()
        self.assertEqual(target.target_id, self.target.target_id)
        self.assertEqual((target.channel, target.recipient_id), ("weixin", weixin_target))
        self.assertIn("test", await service.list_reminders())
        self.assertEqual(self.repository.get_task(task.id).target_id, self.target.id)

    async def test_suspended_owner_can_explicitly_release_before_transfer(self):
        service = ReminderService(self.repository, now=lambda: self.now)
        weixin_target = encode_weixin_target("account", "user")
        await service.suspend_target_id(
            self.target.target_id,
            expected_binding_revision=self.target.binding_revision,
        )

        released = await service.unbind_feishu("chat", "owner")
        rebound = await service.bind_weixin(weixin_target, weixin_target)

        self.assertIn("任意支持的飞书或微信私聊", released)
        self.assertIn("微信私聊", rebound)
        self.assertEqual(self.repository.get_active_target().target_id, self.target.target_id)

    async def test_non_owner_cannot_release_target(self):
        service = ReminderService(self.repository, now=lambda: self.now)

        refused = await service.unbind_feishu("other-chat", "other-owner")

        self.assertIn("只有当前绑定的飞书用户", refused)
        self.assertEqual(self.repository.get_active_target().target_id, self.target.target_id)

    async def test_weixin_delivery_routes_by_persisted_target_and_stable_id(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ReminderRepository(Path(directory) / "reminders.sqlite3")
            recipient = encode_weixin_target("account", "user")
            target = repository.bind_target(
                "weixin", recipient, recipient, self.now
            )
            service = ReminderService(repository, now=lambda: self.now)
            adapter = AsyncReminderRepository(repository, clock=lambda: self.now)
            bus = MessageBus()
            execution = SimpleNamespace(id=41, target_id=target.target_id)

            delivery = asyncio.create_task(publish_reminder_delivery(
                execution,
                "该喝水了。",
                repository=repository,
                service=service,
                async_repository=adapter,
                bus=bus,
            ))
            outbound = await bus.consume_outbound()
            self.assertEqual(outbound.channel, "weixin")
            self.assertEqual(outbound.chat_id, recipient)
            self.assertEqual(outbound.correlation_id, "reminder:41")
            outbound.delivery_future.set_result(
                DeliveryResult(True, code="ok", provider_message_id="wx-accepted")
            )
            result = await delivery
            self.assertTrue(result.success)

    async def test_rebound_target_delivers_existing_task_to_weixin(self):
        task = self.create_task(TaskKind.MESSAGE, delivery_text="迁移后的提醒。")
        service = ReminderService(self.repository, now=lambda: self.now)
        recipient = encode_weixin_target("account", "user")
        self.assertIn("已解绑", await service.unbind_feishu("chat", "owner"))
        self.assertIn("已绑定", await service.bind_weixin(recipient, recipient))

        adapter = AsyncReminderRepository(self.repository, clock=lambda: self.now)
        bus = MessageBus()
        execution = SimpleNamespace(id=43, target_id=self.target.target_id)
        delivery = asyncio.create_task(publish_reminder_delivery(
            execution,
            "迁移后的提醒。",
            repository=self.repository,
            service=service,
            async_repository=adapter,
            bus=bus,
        ))
        outbound = await bus.consume_outbound()
        self.assertEqual(outbound.channel, "weixin")
        self.assertEqual(outbound.chat_id, recipient)
        outbound.delivery_future.set_result(DeliveryResult(True, code="ok"))
        self.assertTrue((await delivery).success)
        self.assertEqual(self.repository.get_task(task.id).target_id, self.target.id)

    async def test_context_missing_suspends_weixin_target(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ReminderRepository(Path(directory) / "reminders.sqlite3")
            recipient = encode_weixin_target("account", "user")
            target = repository.bind_target(
                "weixin", recipient, recipient, self.now
            )
            service = ReminderService(repository, now=lambda: self.now)
            adapter = AsyncReminderRepository(repository, clock=lambda: self.now)
            bus = MessageBus()
            execution = SimpleNamespace(id=42, target_id=target.target_id)

            delivery = asyncio.create_task(publish_reminder_delivery(
                execution,
                "提醒正文",
                repository=repository,
                service=service,
                async_repository=adapter,
                bus=bus,
            ))
            outbound = await bus.consume_outbound()
            outbound.delivery_future.set_result(DeliveryResult(
                False, code="context_missing", message="interaction required"
            ))
            result = await delivery
            self.assertEqual(result.code, "context_missing")
            self.assertIsNone(repository.get_active_target())
            stored = repository.get_target_by_public_id(
                target.target_id, active_only=False
            )
            self.assertFalse(stored.active)
            other_recipient = encode_weixin_target("account", "other-user")
            refused = await service.bind_weixin(other_recipient, other_recipient)
            self.assertIn("/unbind-reminders", refused)
            self.assertIn("已绑定", await service.bind_weixin(recipient, recipient))

    async def test_stale_weixin_failure_cannot_suspend_rebound_feishu_target(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ReminderRepository(Path(directory) / "reminders.sqlite3")
            recipient = encode_weixin_target("account", "user")
            target = repository.bind_target("weixin", recipient, recipient, self.now)
            service = ReminderService(repository, now=lambda: self.now)
            adapter = AsyncReminderRepository(repository, clock=lambda: self.now)
            bus = MessageBus()
            execution = SimpleNamespace(id=44, target_id=target.target_id)

            delivery = asyncio.create_task(publish_reminder_delivery(
                execution,
                "旧微信投递",
                repository=repository,
                service=service,
                async_repository=adapter,
                bus=bus,
            ))
            outbound = await bus.consume_outbound()
            self.assertEqual(outbound.channel, "weixin")

            self.assertIn("已解绑", await service.unbind_weixin(recipient, recipient))
            self.assertIn("已绑定", await service.bind_feishu("new-chat", "new-owner"))
            outbound.delivery_future.set_result(DeliveryResult(
                False, code="context_missing", message="stale Weixin context"
            ))
            self.assertEqual((await delivery).code, "context_missing")

            rebound = repository.get_active_target()
            self.assertEqual(rebound.target_id, target.target_id)
            self.assertEqual((rebound.channel, rebound.owner_id), ("feishu", "new-owner"))
            self.assertGreater(rebound.binding_revision, target.binding_revision)

    async def test_static_message_completes_without_agent_and_persists_receipt(self):
        task = self.create_task(TaskKind.MESSAGE, delivery_text="该喝水了。")

        async def agent_runner(_prompt, _session):
            self.fail("message reminder must not invoke the Agent")

        async def deliver(execution, output):
            self.assertEqual(execution.target_id, self.target.target_id)
            self.assertEqual(output, "该喝水了。")
            return DeliveryResult(
                True, code="0", provider_message_id="om_accepted"
            )

        await self.make_scheduler(agent_runner, deliver)._drain_due()

        stored_task = self.repository.get_task(task.id)
        execution = self.repository.get_execution(
            self.repository.list_execution_ids(task.id)[0]
        )
        self.assertEqual(stored_task.status.value, "completed")
        self.assertEqual(execution.agent_output, "该喝水了。")
        self.assertEqual(execution.agent_attempts, 0)
        self.assertEqual(execution.send_attempts, 1)
        self.assertEqual(
            execution.delivery_result.provider_message_id, "om_accepted"
        )

    async def test_agent_delivery_retry_reuses_the_persisted_output(self):
        task = self.create_task(TaskKind.AGENT, agent_prompt="生成今日简报")
        agent_calls = []
        delivery_calls = []

        async def agent_runner(prompt, session_key):
            agent_calls.append((prompt, session_key))
            return "同一份已生成简报"

        async def deliver(_execution, output):
            delivery_calls.append(output)
            if len(delivery_calls) == 1:
                return DeliveryResult(
                    False, retryable=True, code=503, message="unavailable"
                )
            return DeliveryResult(True, code=0)

        scheduler = self.make_scheduler(agent_runner, deliver)
        await scheduler._drain_due()
        self.now += timedelta(minutes=1)
        await scheduler._drain_due()

        self.assertEqual(
            agent_calls,
            [("生成今日简报", f"scheduled:{task.id}:1")],
        )
        self.assertEqual(delivery_calls, ["同一份已生成简报"] * 2)
        execution = self.repository.get_execution(
            self.repository.list_execution_ids(task.id)[0]
        )
        self.assertEqual(execution.agent_attempts, 1)
        self.assertEqual(execution.send_attempts, 2)
        self.assertEqual(self.repository.get_task(task.id).status.value, "completed")


if __name__ == "__main__":
    unittest.main()
