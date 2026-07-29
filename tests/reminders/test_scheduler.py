import asyncio
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from reminders.scheduler import ReminderScheduler, SystemClock


class FakeClock:
    def __init__(self):
        self.value = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self):
        return self.value

    def advance(self, delta):
        self.value += delta


@dataclass
class Execution:
    id: str
    task_id: str = "task"
    task_kind: str = "agent"
    delivery_text: str = "提醒我喝水"
    agent_prompt: str = "请提醒我喝水"
    scheduled_for: datetime | None = None
    output_text: str | None = None
    delivery_attempts: int = 0
    agent_attempts: int = 0


@dataclass
class Result:
    success: bool
    retryable: bool = False
    error: str | None = None
    message: str | None = None


class Repository:
    def __init__(self, clock, executions=()):
        self.clock, self.executions = clock, list(executions)
        self.calls, self.outputs, self.retries, self.failed, self.advanced = [], {}, [], [], []
        self.recovered = 0

    async def recover_expired_leases(self, now): self.recovered += 1
    async def next_wake_at(self, now): return self.clock.now() + timedelta(hours=1)
    async def claim_due(self, now, lease_until):
        return self.executions.pop(0) if self.executions else None
    async def save_output(self, execution_id, output_text): self.outputs[execution_id] = output_text
    async def mark_sent(self, execution_id, sent_at): self.calls.append(("sent", execution_id))
    async def schedule_retry(self, execution_id, retry_at, error): self.retries.append((execution_id, retry_at, error))
    async def mark_failed(self, execution_id, error): self.failed.append((execution_id, error))
    async def is_cancelled(self, execution_id): return execution_id in getattr(self, "cancelled", set())
    async def begin_delivery(self, execution_id): return execution_id not in getattr(self, "cancelled", set())
    async def schedule_agent_retry(self, execution_id, retry_at, error): self.calls.append(("agent-retry", execution_id, error))
    async def advance_schedule(self, task_id, scheduled_for, now): self.advanced.append(task_id)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def make(self, repo, clock, agent, delivery, **kwargs):
        return ReminderScheduler(repo, agent, delivery, clock=clock, retry_delay=lambda _: timedelta(minutes=7), **kwargs)

    async def test_persists_agent_output_before_delivery_and_uses_scheduled_session(self):
        clock = FakeClock(); execution = Execution("e1", scheduled_for=clock.now()); repo = Repository(clock, [execution])
        calls = []
        async def agent(prompt, session): calls.append((prompt, session)); return "已提醒"
        async def delivery(execution, text): self.assertEqual(execution.id, "e1"); self.assertEqual(repo.outputs["e1"], text); return Result(True)
        scheduler = self.make(repo, clock, agent, delivery)
        await scheduler._drain_due()
        self.assertEqual(calls, [("请提醒我喝水", "scheduled:task:e1")])
        self.assertEqual(repo.advanced, ["task"])

    async def test_delivery_retries_reuse_persisted_output_without_rerunning_agent(self):
        clock = FakeClock(); first = Execution("e1", scheduled_for=clock.now()); second = Execution("e1", scheduled_for=clock.now(), output_text="saved", delivery_attempts=1)
        repo = Repository(clock, [first, second]); agents = 0; delivered = []
        async def agent(_, __): nonlocal agents; agents += 1; return "saved"
        async def delivery(_, text): delivered.append(text); return Result(len(delivered) == 2, retryable=True, error="offline")
        scheduler = self.make(repo, clock, agent, delivery)
        await scheduler._drain_due()
        self.assertEqual(agents, 1); self.assertEqual(delivered, ["saved", "saved"])
        self.assertEqual(len(repo.retries), 1); self.assertEqual(repo.advanced, ["task"])

    async def test_non_retryable_and_third_failure_become_failed(self):
        clock = FakeClock(); repo = Repository(clock, [Execution("a", output_text="x"), Execution("b", output_text="x", delivery_attempts=2)])
        async def agent(_, __): self.fail("agent must not run")
        async def delivery(_, __): return Result(False, retryable=False, error="bad request")
        scheduler = self.make(repo, clock, agent, delivery)
        await scheduler._drain_due()
        self.assertEqual([item[0] for item in repo.failed], ["a", "b"])

    async def test_agent_exception_is_recoverable_and_delivery_timeout_retries(self):
        clock = FakeClock(); repo = Repository(clock, [Execution("agent"), Execution("timeout", output_text="saved")])
        async def agent(_, __): raise RuntimeError("model down")
        async def delivery(_, __): await asyncio.Event().wait()
        scheduler = self.make(repo, clock, agent, delivery, delivery_timeout=0.001)
        await scheduler._drain_due()
        self.assertEqual(repo.calls[0][:2], ("agent-retry", "agent")); self.assertEqual(repo.retries[0][0], "timeout")

    async def test_startup_recovery_wake_and_stop_do_not_need_real_sleep(self):
        clock = FakeClock(); repo = Repository(clock); waits = []
        async def agent(_, __): return "unused"
        async def delivery(_, __): return Result(True)
        async def wait(event, timeout):
            waits.append(timeout); clock.advance(timedelta(seconds=timeout)); scheduler.stop_requested = True; await scheduler.stop()
        scheduler = self.make(repo, clock, agent, delivery, wait=wait)
        await scheduler.run()
        self.assertEqual(repo.recovered, 1); self.assertEqual(waits, [300.0])
        scheduler.wake(); self.assertTrue(scheduler._wake_event.is_set())

    async def test_cancellation_propagates_without_marking_delivery_failed(self):
        clock = FakeClock(); repo = Repository(clock, [Execution("e1", output_text="saved")]); started = asyncio.Event()
        async def agent(_, __): self.fail("agent must not run")
        async def delivery(_, __):
            started.set()
            await asyncio.Event().wait()
        scheduler = self.make(repo, clock, agent, delivery)
        task = asyncio.create_task(scheduler._drain_due())
        await asyncio.wait_for(started.wait(), timeout=0.2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(repo.failed, [])
        self.assertEqual(repo.retries, [])

    async def test_cancel_task_keeps_run_loop_alive_for_next_execution(self):
        clock = FakeClock(); repo = Repository(clock, [Execution("cancel", output_text="saved"), Execution("next", output_text="saved")]); started = asyncio.Event(); cleaned = asyncio.Event(); delivered = []; next_delivered = asyncio.Event()
        async def agent(_, __): self.fail("agent must not run")
        async def delivery(execution, __):
            if execution.id == "cancel":
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
            delivered.append(execution.id)
            next_delivered.set()
        scheduler = self.make(repo, clock, agent, delivery)
        running = asyncio.create_task(scheduler.run())
        await asyncio.wait_for(started.wait(), timeout=0.2)
        scheduler.cancel_task("task")
        await asyncio.wait_for(next_delivered.wait(), timeout=0.2)
        await scheduler.stop()
        await asyncio.wait_for(running, timeout=0.2)
        self.assertTrue(cleaned.is_set())
        self.assertNotIn("task", scheduler._execution_tasks)
        self.assertTrue(scheduler._wake_event.is_set())
        self.assertEqual(delivered, ["next"])

    async def test_delivery_timeout_must_be_positive_and_message_is_retained(self):
        clock = FakeClock(); repo = Repository(clock, [Execution("e1", output_text="saved")])
        async def agent(_, __): return "unused"
        async def delivery(_, __): return Result(False, retryable=False, message="target unavailable")
        with self.assertRaises(ValueError):
            self.make(repo, clock, agent, delivery, delivery_timeout=0)
        scheduler = self.make(repo, clock, agent, delivery)
        await scheduler._drain_due()
        self.assertEqual(repo.failed, [("e1", "target unavailable")])

    async def test_message_bypasses_agent_and_cancelled_claim_has_no_external_effect(self):
        clock = FakeClock(); message = Execution("message", task_kind="message", delivery_text="直接提醒")
        cancelled = Execution("cancelled", task_kind="message", delivery_text="不得发送")
        repo = Repository(clock, [message, cancelled]); repo.cancelled = {"cancelled"}; delivered = []
        async def agent(_, __): self.fail("message tasks must bypass agent")
        async def delivery(execution, text): delivered.append((execution.id, text)); return Result(True)
        scheduler = self.make(repo, clock, agent, delivery)
        await scheduler._drain_due()
        self.assertEqual(delivered, [("message", "直接提醒")])
        self.assertEqual(repo.outputs["message"], "直接提醒")
        self.assertNotIn("cancelled", repo.outputs)

    async def test_configured_attempt_limits_bound_agent_and_delivery_retries(self):
        clock = FakeClock(); repo = Repository(clock, [Execution("agent", agent_attempts=1), Execution("delivery", output_text="saved", delivery_attempts=1)])
        async def agent(_, __): raise RuntimeError("unavailable")
        async def delivery(_, __): return Result(False, retryable=True, error="offline")
        scheduler = self.make(repo, clock, agent, delivery, max_agent_attempts=2, max_delivery_attempts=2)
        await scheduler._drain_due()
        self.assertEqual([item[0] for item in repo.failed], ["agent", "delivery"])

    async def test_system_clock_is_utc(self):
        self.assertEqual(SystemClock().now().tzinfo, timezone.utc)


if __name__ == "__main__":
    unittest.main()
