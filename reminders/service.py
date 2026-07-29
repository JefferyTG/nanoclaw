"""Application service shared by reminder tools and Feishu binding commands."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from reminders.models import DeliveryResult, TaskKind
from reminders.schedule import ScheduleService, ScheduleSpec


_BIND_GUIDE = "尚未绑定提醒目标。请先在与机器人的飞书私聊中发送 /bind-reminders。"
_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


@dataclass(frozen=True, slots=True)
class ScheduledExecution:
    """Flat scheduler view over one claimed repository work item."""

    id: int
    task_id: int
    task_kind: TaskKind
    target_id: str
    delivery_text: str | None
    agent_prompt: str | None
    scheduled_for: datetime
    output_text: str | None
    delivery_attempts: int
    agent_attempts: int


class AsyncReminderRepository:
    """Thin asyncio adapter for the synchronous SQLite repository."""

    def __init__(
        self,
        repository,
        *,
        clock: Callable[[], datetime],
        once_grace: timedelta = timedelta(hours=1),
    ) -> None:
        self.repository = repository
        self.clock = clock
        self.once_grace = once_grace
        self.lease_owner = uuid.uuid4().hex
        self._claims: dict[int, ScheduledExecution] = {}
        self._saved_outputs: set[int] = set()
        self._sent_at: dict[int, datetime] = {}
        self._delivery_results: dict[int, DeliveryResult] = {}

    def remember_delivery_result(
        self, execution_id: int | str, result: DeliveryResult
    ) -> None:
        self._delivery_results[int(execution_id)] = result

    async def recover_expired_leases(self, now: datetime) -> None:
        def recover() -> None:
            self.repository.recover_expired_leases(now)
            self.repository.recover_schedules(now, offline_window=self.once_grace)

        await asyncio.to_thread(recover)

    async def next_wake_at(self, now: datetime) -> datetime | None:
        return await asyncio.to_thread(self.repository.next_wake_at, now)

    async def claim_due(
        self, now: datetime, lease_until: datetime
    ) -> ScheduledExecution | None:
        items = await asyncio.to_thread(
            self.repository.claim_due,
            now_utc=now,
            lease_owner=self.lease_owner,
            lease_for=lease_until - now,
            offline_window=self.once_grace,
            limit=1,
        )
        if not items:
            return None
        item = items[0]
        execution = item.execution
        scheduled = ScheduledExecution(
            id=execution.id,
            task_id=execution.task_id,
            task_kind=item.task_kind,
            target_id=item.target_id,
            delivery_text=item.delivery_text,
            agent_prompt=item.agent_prompt,
            scheduled_for=execution.scheduled_for_utc,
            output_text=execution.agent_output,
            delivery_attempts=execution.send_attempts,
            agent_attempts=execution.agent_attempts,
        )
        self._claims[execution.id] = scheduled
        if execution.agent_output is not None:
            self._saved_outputs.add(execution.id)
        return scheduled

    async def save_output(self, execution_id: str, output_text: str) -> None:
        execution_key = int(execution_id)
        claim = self._claim(execution_key)
        method = (
            self.repository.save_output
            if claim.task_kind is TaskKind.MESSAGE
            else self.repository.record_agent_output
        )
        kwargs = {
            "lease_owner": self.lease_owner,
            "now_utc": self.clock(),
        }
        if claim.task_kind is TaskKind.MESSAGE:
            kwargs["output_text"] = output_text
        else:
            kwargs["output"] = output_text
        changed = await asyncio.to_thread(method, execution_key, **kwargs)
        if not changed:
            raise RuntimeError("reminder claim was cancelled before output persisted")
        self._saved_outputs.add(execution_key)

    async def is_cancelled(self, execution_id: str) -> bool:
        execution_key = int(execution_id)
        if execution_key not in self._claims:
            return True
        active = await asyncio.to_thread(
            self.repository.is_claim_active,
            execution_key,
            lease_owner=self.lease_owner,
        )
        return not active

    async def begin_delivery(self, execution_id: str) -> bool:
        execution_key = int(execution_id)
        if execution_key not in self._claims:
            return False
        return await asyncio.to_thread(
            self.repository.begin_delivery,
            execution_key,
            lease_owner=self.lease_owner,
            now_utc=self.clock(),
        )

    async def schedule_agent_retry(
        self, execution_id: str, retry_at: datetime, error: str
    ) -> None:
        del error  # Execution rows retain attempt/state; provider text is not persisted.
        execution_key = int(execution_id)
        await asyncio.to_thread(
            self.repository.record_agent_failure,
            execution_key,
            lease_owner=self.lease_owner,
            retry_at_utc=retry_at,
            now_utc=self.clock(),
        )
        self._forget(execution_key)

    async def schedule_retry(
        self, execution_id: str, retry_at: datetime, error: str
    ) -> None:
        execution_key = int(execution_id)
        result = self._delivery_results.get(execution_key) or DeliveryResult(
            success=False,
            retryable=True,
            code="delivery_error",
            message=error,
        )
        if not result.retryable:
            result = DeliveryResult(
                success=False,
                retryable=True,
                code=result.code,
                message=result.message or error,
                provider_message_id=result.provider_message_id,
            )
        await asyncio.to_thread(
            self.repository.finish_execution,
            execution_key,
            lease_owner=self.lease_owner,
            result=result,
            retry_at_utc=retry_at,
            next_run_at_utc=None,
            now_utc=self.clock(),
        )
        self._forget(execution_key)

    async def mark_failed(self, execution_id: str, error: str) -> None:
        execution_key = int(execution_id)
        claim = self._claim(execution_key)
        if (
            claim.task_kind is TaskKind.AGENT
            and execution_key not in self._saved_outputs
        ):
            await asyncio.to_thread(
                self.repository.record_agent_failure,
                execution_key,
                lease_owner=self.lease_owner,
                retry_at_utc=None,
                now_utc=self.clock(),
            )
        else:
            result = self._delivery_results.get(execution_key) or DeliveryResult(
                success=False,
                retryable=False,
                code="delivery_failed",
                message=error,
            )
            await asyncio.to_thread(
                self.repository.finish_execution,
                execution_key,
                lease_owner=self.lease_owner,
                result=result,
                next_run_at_utc=None,
                now_utc=self.clock(),
            )
        self._forget(execution_key)

    async def mark_sent(self, execution_id: str, sent_at: datetime) -> None:
        self._sent_at[int(execution_id)] = sent_at

    async def advance_schedule(
        self, task_id: str, scheduled_for: datetime, now: datetime
    ) -> None:
        task_key = int(task_id)
        execution_key = next(
            (
                key
                for key, claim in self._claims.items()
                if claim.task_id == task_key and claim.scheduled_for == scheduled_for
            ),
            None,
        )
        if execution_key is None:
            raise RuntimeError("claimed reminder execution is no longer available")
        task = await asyncio.to_thread(self.repository.get_task, task_key)
        if task is None:
            raise RuntimeError("reminder task disappeared during completion")
        spec = ScheduleSpec.from_rrule(
            timezone=task.timezone,
            dtstart=task.dtstart_local,
            value=task.rrule,
        )
        next_run = ScheduleService.next_after(spec, scheduled_for)
        result = self._delivery_results.get(execution_key) or DeliveryResult(
            success=True, code="accepted", message="accepted"
        )
        await asyncio.to_thread(
            self.repository.finish_execution,
            execution_key,
            lease_owner=self.lease_owner,
            result=result,
            next_run_at_utc=next_run,
            now_utc=self._sent_at.get(execution_key, now),
        )
        self._forget(execution_key)

    def _claim(self, execution_id: int) -> ScheduledExecution:
        claim = self._claims.get(execution_id)
        if claim is None:
            raise RuntimeError("reminder execution is not claimed by this scheduler")
        return claim

    def _forget(self, execution_id: int) -> None:
        self._claims.pop(execution_id, None)
        self._saved_outputs.discard(execution_id)
        self._sent_at.pop(execution_id, None)
        self._delivery_results.pop(execution_id, None)


class ReminderService:
    """Validate user-facing operations before entering the SQLite state machine."""

    def __init__(
        self,
        repository,
        *,
        now: Callable[[], datetime] | None = None,
        cleanup_task: Callable[[int], Awaitable[None] | None] | None = None,
    ) -> None:
        self.repository = repository
        self._now = now or (lambda: datetime.now(UTC))
        self._cleanup_task = cleanup_task
        self._scheduler = None

    def attach_scheduler(self, scheduler) -> None:
        self._scheduler = scheduler

    def attach_cleanup(self, cleanup_task: Callable[[int], Awaitable[None] | None]) -> None:
        self._cleanup_task = cleanup_task

    def _wake(self) -> None:
        if self._scheduler is not None:
            self._scheduler.wake()

    async def _repo(self, method: str, *args, **kwargs):
        """Run the synchronous SQLite repository off the event loop.

        Awaitable fakes remain supported so application tests can stay compact.
        """
        result = await asyncio.to_thread(
            getattr(self.repository, method), *args, **kwargs
        )
        if inspect.isawaitable(result):
            return await result
        return result

    def _now_utc(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("reminder clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    async def bind_feishu(self, chat_id: str, open_id: str) -> str:
        if not chat_id or not open_id:
            return "⚠️ 飞书事件缺少私聊或用户标识，无法绑定提醒。"
        try:
            target = await self._repo(
                "bind_feishu_target",
                chat_id=chat_id,
                open_id=open_id,
                now_utc=self._now_utc(),
            )
        except PermissionError:
            return "⚠️ 当前实例已绑定其他飞书用户，只有原绑定用户可以重新绑定。"
        except (OSError, ValueError):
            return "⚠️ 绑定提醒失败，请检查本实例的提醒数据库配置。"
        self._wake()
        return f"✅ 已绑定主动提醒到当前飞书私聊（目标 {target.target_id}）。"

    async def unbind_feishu(self, chat_id: str, open_id: str) -> str:
        del chat_id  # open_id is the ownership boundary; chat_id may have changed.
        if not open_id:
            return "⚠️ 飞书事件缺少用户标识，无法解绑提醒。"
        try:
            changed = await self._repo(
                "unbind_feishu_target", open_id=open_id, now_utc=self._now_utc()
            )
        except PermissionError:
            return "⚠️ 只有当前绑定的飞书用户可以解绑提醒。"
        except (OSError, ValueError):
            return "⚠️ 解绑提醒失败，请稍后重试。"
        if not changed:
            return "当前没有由你绑定的提醒目标。"
        self._wake()
        return "✅ 已解绑主动提醒；已有任务会保留，并在同一用户重新绑定后恢复调度。"

    async def create_reminder(self, **kwargs) -> str:
        try:
            target = await self._repo("get_active_target")
        except OSError:
            return "错误：提醒数据库暂时不可用。"
        if target is None:
            return _BIND_GUIDE

        try:
            kind = TaskKind(str(kwargs.get("task_type", "")).strip().lower())
            subject = self._required_text(kwargs.get("subject"), "subject")
            delivery_text, agent_prompt = self._payload(kind, kwargs)
            spec = self._schedule_spec(kwargs)
            now = self._now_utc()
            preview = ScheduleService.preview(
                spec, now - timedelta(microseconds=1), limit=3
            )
            if not preview:
                raise ValueError("周期规则在当前时间之后没有可执行时间")
            task = await self._repo(
                "create_task",
                target_id=target.target_id,
                kind=kind,
                subject=subject,
                delivery_text=delivery_text,
                agent_prompt=agent_prompt,
                dtstart_local=spec.dtstart,
                timezone=spec.timezone,
                rrule=spec.to_rrule(),
                next_run_at_utc=preview[0],
                now_utc=now,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return f"错误：无法创建定时任务：{exc}"
        except OSError:
            return "错误：提醒数据库暂时不可用。"

        self._wake()
        lines = [f"已创建任务 #{task.id}：{task.subject}", "未来执行时间："]
        local_tz = ZoneInfo(spec.timezone)
        lines.extend(
            f"- {value.astimezone(local_tz).isoformat()} ({spec.timezone})"
            for value in preview
        )
        return "\n".join(lines)

    async def list_reminders(self, *, include_inactive: bool = False) -> str:
        try:
            target = await self._repo("get_active_target")
            if target is None:
                return _BIND_GUIDE
            tasks = await self._repo(
                "list_tasks_for_target",
                target.target_id,
                include_inactive=include_inactive,
            )
        except OSError:
            return "错误：提醒数据库暂时不可用。"
        if not tasks:
            return "当前没有符合条件的定时任务。"
        lines = ["定时任务："]
        for task in tasks:
            next_run = (
                task.next_run_at_utc.astimezone(ZoneInfo(task.timezone)).isoformat()
                if task.next_run_at_utc is not None
                else "—"
            )
            lines.append(
                f"- #{task.id} [{task.kind.value}/{task.status.value}] "
                f"{task.subject}；下次 {next_run} ({task.timezone})"
            )
        return "\n".join(lines)

    async def cancel_reminder(self, task_id: int) -> str:
        try:
            target = await self._repo("get_active_target")
            if target is None:
                return _BIND_GUIDE
            changed = await self._repo(
                "cancel_task",
                task_id,
                target_id=target.target_id,
                now_utc=self._now_utc(),
            )
        except OSError:
            return "错误：提醒数据库暂时不可用。"
        if not changed:
            return f"未找到可取消的任务 #{task_id}，或它已结束。"

        if self._scheduler is not None:
            self._scheduler.cancel_task(str(task_id))
        self._wake()
        if self._cleanup_task is not None:
            outcome = self._cleanup_task(task_id)
            if inspect.isawaitable(outcome):
                await outcome
        return f"已取消任务 #{task_id}。"

    @staticmethod
    def _required_text(value, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} 不能为空")
        return value.strip()

    @classmethod
    def _payload(cls, kind: TaskKind, values: dict) -> tuple[str | None, str | None]:
        delivery = values.get("delivery_text")
        prompt = values.get("agent_prompt")
        if kind is TaskKind.MESSAGE:
            if prompt not in (None, ""):
                raise ValueError("message 任务不能包含 agent_prompt")
            return cls._required_text(delivery, "delivery_text"), None
        if delivery not in (None, ""):
            raise ValueError("agent 任务不能包含 delivery_text")
        return None, cls._required_text(prompt, "agent_prompt")

    @staticmethod
    def _schedule_spec(values: dict) -> ScheduleSpec:
        start_raw = values.get("start_at")
        if not isinstance(start_raw, str):
            raise ValueError("start_at 必须是本地 ISO-8601 时间")
        start = datetime.fromisoformat(start_raw)
        if start.tzinfo is not None:
            raise ValueError("start_at 应为不带偏移的本地时间，时区请使用 timezone")

        until = None
        until_raw = values.get("until")
        if until_raw not in (None, ""):
            if not isinstance(until_raw, str):
                raise ValueError("until 必须是带时区 ISO-8601 时间")
            until = datetime.fromisoformat(until_raw)
            if until.tzinfo is None:
                raise ValueError("until 必须包含 UTC 偏移")

        weekday_values = values.get("by_weekday") or []
        if not isinstance(weekday_values, list):
            raise ValueError("by_weekday 必须是列表")
        try:
            weekdays = tuple(_WEEKDAYS[str(value).upper()] for value in weekday_values)
        except KeyError as exc:
            raise ValueError(f"无效星期：{exc.args[0]}") from exc

        monthdays = values.get("by_monthday") or []
        if not isinstance(monthdays, list) or any(
            not isinstance(value, int) or isinstance(value, bool) for value in monthdays
        ):
            raise ValueError("by_monthday 必须是整数列表")

        interval = values.get("interval", 1)
        count = values.get("count")
        if not isinstance(interval, int) or isinstance(interval, bool):
            raise ValueError("interval 必须是正整数")
        if count is not None and (
            not isinstance(count, int) or isinstance(count, bool)
        ):
            raise ValueError("count 必须是正整数")
        return ScheduleSpec(
            timezone=ReminderService._required_text(values.get("timezone"), "timezone"),
            dtstart=start,
            freq=ReminderService._required_text(values.get("frequency"), "frequency"),
            interval=interval,
            byweekday=weekdays,
            bymonthday=tuple(monthdays),
            count=count,
            until=until,
        )
