"""The single async loop that runs persisted reminder executions.

The scheduler deliberately owns no reminder state.  ``ReminderRepository`` is
the transaction boundary: it selects only the newest due occurrence for a
recurring task, expires stale leases on startup, applies the one-shot grace
window, and advances COUNT/UNTIL schedules.  This keeps a restart from
reconstructing schedule policy in memory.

Repository protocol (all methods are async)::

    recover_expired_leases(now)
    next_wake_at(now) -> datetime | None
    claim_due(now, lease_until) -> execution | None
    save_output(execution_id, output_text)
    mark_sent(execution_id, sent_at)
    schedule_retry(execution_id, retry_at, error)
    mark_failed(execution_id, error)
    is_cancelled(execution_id) -> bool
    begin_delivery(execution_id) -> bool
    schedule_agent_retry(execution_id, retry_at, error)
    advance_schedule(task_id, scheduled_for, now)

An execution supplies ``id``, ``task_id``, ``task_kind`` (``"message"`` or
``"agent"``), ``delivery_text``, ``agent_prompt``, ``scheduled_for``,
``output_text``, ``delivery_attempts`` and ``agent_attempts``.  ``deliver`` is
called as ``deliver(execution, output_text)`` so the adapter can resolve the
execution's persisted target.  Delivery returns an object with ``success``,
``retryable`` and optional ``error`` attributes.  ``begin_delivery`` atomically
transitions a non-cancelled execution to ``sending``; it is the final repository
fence before outbound effects, so cancellation races after a claim are harmless.
``is_cancelled`` avoids starting a newly-cancelled Agent run; a concurrent
cancel after this check is still fenced by ``begin_delivery``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class DeliveryResult(Protocol):
    success: bool
    retryable: bool
    code: str | None
    error: str | None
    message: str | None


class ReminderRepository(Protocol):
    async def recover_expired_leases(self, now: datetime) -> None: ...
    async def next_wake_at(self, now: datetime) -> datetime | None: ...
    async def claim_due(self, now: datetime, lease_until: datetime) -> Any | None: ...
    async def save_output(self, execution_id: str, output_text: str) -> None: ...
    async def mark_sent(self, execution_id: str, sent_at: datetime) -> None: ...
    async def schedule_retry(self, execution_id: str, retry_at: datetime, error: str) -> None: ...
    async def mark_failed(self, execution_id: str, error: str) -> None: ...
    async def is_cancelled(self, execution_id: str) -> bool: ...
    async def begin_delivery(self, execution_id: str) -> bool: ...
    async def defer_delivery(self, execution_id: str, reason: str) -> None: ...
    async def schedule_agent_retry(self, execution_id: str, retry_at: datetime, error: str) -> None: ...
    async def advance_schedule(self, task_id: str, scheduled_for: datetime, now: datetime) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        from datetime import timezone

        return datetime.now(timezone.utc)


async def _default_wait(event: asyncio.Event, timeout: float) -> None:
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except TimeoutError:
        pass


class ReminderScheduler:
    """Wake only for persisted work, with an Event for prompt external wakes."""

    _DEFERRED_DELIVERY_CODES = frozenset(
        {"target_unbound", "context_missing", "session_expired"}
    )

    def __init__(
        self,
        repository: ReminderRepository,
        agent_runner: Callable[[str, str], Awaitable[str]],
        deliver: Callable[[Any, str], Awaitable[DeliveryResult]],
        *,
        clock: Clock | None = None,
        lease_duration: timedelta = timedelta(minutes=5),
        max_sleep: timedelta = timedelta(minutes=5),
        delivery_timeout: float = 30.0,
        max_delivery_attempts: int = 3,
        max_agent_attempts: int = 3,
        retry_delay: Callable[[int], timedelta] | None = None,
        wait: Callable[[asyncio.Event, float], Awaitable[None]] = _default_wait,
    ) -> None:
        if lease_duration <= timedelta():
            raise ValueError("lease_duration must be positive")
        if max_sleep < timedelta(minutes=1):
            raise ValueError("max_sleep must be at least one minute; do not poll by seconds")
        if max_delivery_attempts < 1 or max_agent_attempts < 1:
            raise ValueError("attempt limits must be positive")
        if delivery_timeout <= 0:
            raise ValueError("delivery_timeout must be positive")
        self.repository = repository
        self.agent_runner = agent_runner
        self.deliver = deliver
        self.clock = clock or SystemClock()
        self.lease_duration = lease_duration
        self.max_sleep = max_sleep
        self.delivery_timeout = delivery_timeout
        self.max_delivery_attempts = max_delivery_attempts
        self.max_agent_attempts = max_agent_attempts
        self.retry_delay = retry_delay or (lambda attempt: timedelta(minutes=2 ** (attempt - 1)))
        self._wait = wait
        self._wake_event = asyncio.Event()
        self._stopped = False
        self._task: asyncio.Task[None] | None = None
        # Entries exist only while an Agent or delivery operation is in flight.
        self._execution_tasks: dict[str, asyncio.Task[None]] = {}

    def wake(self) -> None:
        """Interrupt the current dynamic wait after a repository mutation."""
        self._wake_event.set()

    def cancel_task(self, task_id: str) -> None:
        """Cancel an in-flight operation after durable repository cancellation.

        This mapping accelerates shutdown of paid Agent work or external sends;
        it is not a source of reminder state and is removed when work finishes.
        """
        task = self._execution_tasks.get(task_id)
        if task is not None:
            task.cancel()
        self.wake()

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._stopped = False
            self._task = asyncio.create_task(self.run(), name="reminder-scheduler")
        return self._task

    async def stop(self) -> None:
        self._stopped = True
        self.wake()
        task = self._task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run(self) -> None:
        await self.repository.recover_expired_leases(self.clock.now())
        while not self._stopped:
            await self._drain_due()
            if self._stopped:
                return
            # Clear before reading the repository: a wake racing with that read
            # remains set and makes the following wait return immediately.
            self._wake_event.clear()
            now = self.clock.now()
            wake_at = await self.repository.next_wake_at(now)
            delay = self.max_sleep.total_seconds()
            if wake_at is not None:
                delay = min(delay, max(0.0, (wake_at - now).total_seconds()))
            await self._wait(self._wake_event, delay)

    async def _drain_due(self) -> None:
        """Claim until the repository has no immediately due execution left."""
        while not self._stopped:
            now = self.clock.now()
            execution = await self.repository.claim_due(now, now + self.lease_duration)
            if execution is None:
                return
            task_id = str(getattr(execution, "task_id"))
            execution_task = asyncio.create_task(self._run_execution(execution))
            self._execution_tasks[task_id] = execution_task
            try:
                await execution_task
            except asyncio.CancelledError:
                # ``cancel_task`` intentionally stops only this paid/external
                # operation.  Do not let it kill the scheduler loop.  A real
                # scheduler shutdown or parent cancellation must still escape.
                if self._stopped or asyncio.current_task().cancelling():
                    raise
            finally:
                if self._execution_tasks.get(task_id) is execution_task:
                    self._execution_tasks.pop(task_id, None)

    async def _run_execution(self, execution: Any) -> None:
        output = getattr(execution, "output_text", None)
        execution_id = str(getattr(execution, "id"))
        if output is None:
            if await self.repository.is_cancelled(execution_id):
                # ``is_cancelled`` also covers a target becoming inactive
                # before output generation.  Best-effort release prevents its
                # claim from waiting for lease expiry; a truly cancelled task
                # may already be terminal, so a no-op/rejection is harmless.
                try:
                    await self.repository.defer_delivery(execution_id, "target_unbound")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                return
            if getattr(execution, "task_kind") == "message":
                output = str(getattr(execution, "delivery_text"))
                await self.repository.save_output(execution_id, output)
            else:
                output = await self._run_agent(execution)
                # ``None`` means the agent was put back into retry state (or
                # terminally failed).  A saved output continues directly to
                # delivery in this same lease.
                if output is None:
                    return

        if not await self.repository.begin_delivery(execution_id):
            # A target may have been unbound after this execution was claimed.
            # Release the lease instead of counting this as a delivery failure,
            # so a later rebind can deliver the already-persisted output.
            await self.repository.defer_delivery(execution_id, "target_unbound")
            return
        await self._deliver(execution, output)

    async def _run_agent(self, execution: Any) -> str | None:
        execution_id = str(getattr(execution, "id"))
        try:
            output = await self.agent_runner(
                str(getattr(execution, "agent_prompt")),
                f"scheduled:{getattr(execution, 'task_id')}:{execution_id}",
            )
            # Persist before even attempting external delivery: all delivery
            # retries (including after restart) reuse this exact output.
            await self.repository.save_output(execution_id, output)
            return output
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempts = int(getattr(execution, "agent_attempts", 0)) + 1
            if attempts < self.max_agent_attempts:
                await self.repository.schedule_agent_retry(
                    execution_id, self.clock.now() + self.retry_delay(attempts), str(exc)
                )
            else:
                await self.repository.mark_failed(execution_id, str(exc))
            return None

    async def _deliver(self, execution: Any, output: str) -> None:
        execution_id = str(getattr(execution, "id"))
        try:
            result = await asyncio.wait_for(
                self.deliver(execution, output), timeout=self.delivery_timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_delivery_failure(execution, str(exc), retryable=True)
            return

        if bool(getattr(result, "success", False)):
            now = self.clock.now()
            await self.repository.mark_sent(execution_id, now)
            await self.repository.advance_schedule(
                str(getattr(execution, "task_id")), getattr(execution, "scheduled_for"), now
            )
            return
        code = getattr(result, "code", None)
        if code in self._DEFERRED_DELIVERY_CODES:
            # These outcomes mean delivery is temporarily impossible because
            # the persisted target/session is unavailable.  They deliberately
            # do not consume a Scheduler delivery attempt or become terminal.
            await self.repository.defer_delivery(execution_id, code)
            return
        await self._handle_delivery_failure(
            execution,
            str(getattr(result, "error", None) or getattr(result, "message", "delivery failed")),
            retryable=bool(getattr(result, "retryable", False)),
        )

    async def _handle_delivery_failure(
        self, execution: Any, error: str, *, retryable: bool
    ) -> None:
        attempts = int(getattr(execution, "delivery_attempts", 0)) + 1
        execution_id = str(getattr(execution, "id"))
        if retryable and attempts < self.max_delivery_attempts:
            await self.repository.schedule_retry(
                execution_id, self.clock.now() + self.retry_delay(attempts), error
            )
        else:
            await self.repository.mark_failed(execution_id, error)
