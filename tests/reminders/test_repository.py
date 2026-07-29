from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import sqlite3
import tempfile
import unittest
from pathlib import Path

from reminders.models import DeliveryResult, ExecutionStatus, TaskKind
from reminders.repository import ReminderRepository


NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "reminders.sqlite3"
        self.repo = ReminderRepository(self.path)
        self.target = self.repo.bind_feishu_target("chat", "owner", NOW)
        self.task = self.repo.create_task(target_id=self.target.target_id, kind=TaskKind.AGENT, subject="test", agent_prompt="do it", dtstart_local=datetime(2026, 1, 1, 12), timezone="UTC", rrule="RRULE:FREQ=DAILY", next_run_at_utc=NOW, now_utc=NOW)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_wal_and_restarts_preserve_rows(self):
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        self.assertEqual(ReminderRepository(self.path).get_task(self.task.id), self.task)

    def test_target_binding_rejects_other_open_id(self):
        self.assertEqual(self.repo.get_active_target().open_id, "owner")
        with self.assertRaises(PermissionError):
            self.repo.bind_feishu_target("new", "other", NOW)
        self.assertFalse(self.repo.cancel_task(self.task.id, target_id="other", now_utc=NOW))

    def test_binding_replaces_default_and_task_payload_is_strict(self):
        replacement = self.repo.bind_feishu_target("chat-2", "owner", NOW)
        self.assertTrue(replacement.active)
        self.assertEqual(replacement.target_id, self.target.target_id)
        with self.assertRaises(PermissionError):
            self.repo.bind_feishu_target("chat-3", "other", NOW)
        with self.assertRaises(ValueError):
            self.repo.create_task(target_id=replacement.target_id, kind=TaskKind.MESSAGE, subject="bad", agent_prompt="not allowed", dtstart_local=NOW, timezone="UTC", rrule="RRULE:FREQ=DAILY", next_run_at_utc=NOW, now_utc=NOW)
        with self.assertRaises(ValueError):
            self.repo.create_task(target_id=replacement.target_id, kind=TaskKind.MESSAGE, subject="bad rule", delivery_text="x", dtstart_local=NOW, timezone="UTC", rrule="FREQ=DAILY", next_run_at_utc=NOW, now_utc=NOW)

    def test_concurrent_claim_has_one_winner_and_lease_recovers(self):
        def claim(owner):
            return ReminderRepository(self.path).claim_due(now_utc=NOW, lease_owner=owner, lease_for=timedelta(seconds=1))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ["a", "b"]))
        winners = [item for group in results for item in group]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].execution.task_id, self.task.id)
        recovered = self.repo.claim_due(now_utc=NOW + timedelta(seconds=2), lease_owner="c", lease_for=timedelta(seconds=1))
        self.assertEqual([item.execution.lease_owner for item in recovered], ["c"])

    def test_cancel_race_and_agent_output_delivery_persistence(self):
        claim = self.repo.claim_due(now_utc=NOW, lease_owner="worker", lease_for=timedelta(minutes=1))[0]
        self.assertTrue(self.repo.record_agent_output(claim.execution.id, lease_owner="worker", output="generated", now_utc=NOW))
        self.assertTrue(self.repo.finish_execution(claim.execution.id, lease_owner="worker", result=DeliveryResult(False, retryable=True, code="rate_limit"), retry_at_utc=NOW + timedelta(minutes=1), next_run_at_utc=NOW, now_utc=NOW))
        self.assertEqual(self.repo.claim_due(now_utc=NOW, lease_owner="early", lease_for=timedelta(minutes=1)), [])
        retry = self.repo.claim_due(now_utc=NOW + timedelta(minutes=1), lease_owner="worker2", lease_for=timedelta(minutes=1))[0]
        self.assertEqual(retry.execution.agent_output, "generated")
        self.assertTrue(self.repo.cancel_task(self.task.id, target_id=self.target.target_id, now_utc=NOW))
        self.assertFalse(self.repo.is_claim_active(retry.execution.id, lease_owner="worker2"))
        self.assertFalse(self.repo.finish_execution(retry.execution.id, lease_owner="worker2", result=DeliveryResult(True), next_run_at_utc=None, now_utc=NOW))

    def test_unbind_removes_wake_and_lists_execution_ids(self):
        self.assertEqual(self.repo.next_wake_at(), NOW)
        item = self.repo.claim_due(now_utc=NOW, lease_owner="w", lease_for=timedelta(minutes=1))[0]
        self.assertEqual(self.repo.list_execution_ids(self.task.id), [item.execution.id])
        self.assertTrue(self.repo.unbind_feishu_target("owner", NOW))
        self.assertIsNone(self.repo.next_wake_at())
        with self.assertRaises(PermissionError):
            self.repo.bind_feishu_target("other-chat", "other", NOW)
        with self.assertRaises(PermissionError):
            self.repo.unbind_feishu_target("other", NOW)

    def test_recovery_and_agent_failure_are_persisted(self):
        old = self.repo.create_task(target_id=self.target.target_id, kind=TaskKind.MESSAGE, subject="old", delivery_text="x", dtstart_local=datetime(2026, 1, 1, 9), timezone="UTC", rrule="RRULE:FREQ=DAILY;COUNT=1", next_run_at_utc=datetime(2026, 1, 1, 9, tzinfo=UTC), now_utc=NOW)
        self.repo.recover_schedules(NOW)
        self.assertEqual(self.repo.get_task(old.id).status.value, "failed")
        item = self.repo.claim_due(now_utc=NOW, lease_owner="agent", lease_for=timedelta(minutes=1))[0]
        self.assertTrue(self.repo.record_agent_failure(item.execution.id, lease_owner="agent", retry_at_utc=NOW + timedelta(minutes=2), now_utc=NOW))
        execution = self.repo.get_execution(item.execution.id)
        self.assertEqual(execution.agent_attempts, 1)
        self.assertEqual(self.repo.get_task(self.task.id).status.value, "retry_wait")

    def test_message_output_and_begin_delivery_do_not_count_agent_attempt(self):
        message = self.repo.create_task(target_id=self.target.target_id, kind=TaskKind.MESSAGE, subject="static", delivery_text="body", dtstart_local=datetime(2026, 1, 1, 12), timezone="UTC", rrule="RRULE:FREQ=DAILY;COUNT=1", next_run_at_utc=NOW, now_utc=NOW)
        item = next(work for work in self.repo.claim_due(now_utc=NOW, lease_owner="message", lease_for=timedelta(minutes=1), limit=2) if work.execution.task_id == message.id)
        self.assertTrue(self.repo.save_output(item.execution.id, lease_owner="message", output_text="body", now_utc=NOW))
        self.assertTrue(self.repo.begin_delivery(item.execution.id, lease_owner="message", now_utc=NOW))
        self.assertEqual(self.repo.get_execution(item.execution.id).agent_attempts, 0)
