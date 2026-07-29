"""Persistent, locally scheduled reminders.

This package deliberately contains no scheduler loop or channel integration.  It
offers the value objects, RFC 5545 recurrence calculations and SQLite storage
needed by those layers.
"""

from reminders.models import (
    DeliveryResult,
    ExecutionStatus,
    ReminderExecution,
    ReminderTarget,
    ReminderTask,
    ReminderWorkItem,
    TaskKind,
    TaskStatus,
)
from reminders.repository import ReminderRepository
from reminders.schedule import ScheduleService, ScheduleSpec

__all__ = [
    "DeliveryResult",
    "ExecutionStatus",
    "ReminderExecution",
    "ReminderRepository",
    "ReminderTarget",
    "ReminderTask",
    "ReminderWorkItem",
    "ScheduleService",
    "ScheduleSpec",
    "TaskKind",
    "TaskStatus",
]
