"""Shared reminder persistence DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class TaskKind(StrEnum):
    MESSAGE = "message"
    AGENT = "agent"


class TaskStatus(StrEnum):
    ACTIVE = "active"
    RUNNING_AGENT = "running_agent"
    SENDING = "sending"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    RETRYABLE = "retryable"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Result reported by a channel adapter; no provider details are inferred."""

    success: bool
    retryable: bool = False
    code: int | str | None = None
    message: str | None = None
    provider_message_id: str | None = None

    def __post_init__(self) -> None:
        if self.success and self.retryable:
            raise ValueError("a successful delivery cannot be retryable")


@dataclass(frozen=True, slots=True)
class ReminderTarget:
    id: int
    target_id: str
    channel: str
    chat_id: str
    open_id: str
    active: bool
    created_at_utc: datetime
    updated_at_utc: datetime


@dataclass(frozen=True, slots=True)
class ReminderTask:
    id: int
    target_id: int
    kind: TaskKind
    status: TaskStatus
    subject: str
    delivery_text: str | None
    agent_prompt: str | None
    dtstart_local: datetime
    timezone: str
    rrule: str
    next_run_at_utc: datetime | None
    created_at_utc: datetime
    updated_at_utc: datetime


@dataclass(frozen=True, slots=True)
class ReminderExecution:
    id: int
    task_id: int
    scheduled_for_utc: datetime
    status: ExecutionStatus
    attempts: int
    agent_attempts: int
    send_attempts: int
    next_attempt_at_utc: datetime | None
    lease_owner: str | None
    lease_expires_at_utc: datetime | None
    agent_output: str | None
    delivery_result: DeliveryResult | None
    created_at_utc: datetime
    updated_at_utc: datetime


@dataclass(frozen=True, slots=True)
class ReminderWorkItem:
    """The complete, already-claimed payload a scheduler worker needs."""

    execution: ReminderExecution
    task_kind: TaskKind
    target_id: str
    chat_id: str
    open_id: str
    subject: str
    delivery_text: str | None
    agent_prompt: str | None


def delivery_result_from_row(row: dict[str, Any]) -> DeliveryResult | None:
    if row["delivery_success"] is None:
        return None
    return DeliveryResult(
        success=bool(row["delivery_success"]),
        retryable=bool(row["delivery_retryable"]),
        code=row["delivery_code"],
        message=row["delivery_message"],
        provider_message_id=row["provider_message_id"],
    )
