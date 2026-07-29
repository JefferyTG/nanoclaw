"""SQLite persistence and atomic state changes for reminder workers.

The class is synchronous by design: an asyncio scheduler can call it through a
small executor adapter, while its time and retry policy remain explicit.
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

from reminders.models import DeliveryResult, ExecutionStatus, ReminderExecution, ReminderTarget, ReminderTask, ReminderWorkItem, TaskKind, TaskStatus, delivery_result_from_row
from reminders.schedule import ScheduleService, ScheduleSpec


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


class ReminderRepository:
    def __init__(self, db_path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        self.db_path = str(db_path)
        self.busy_timeout_ms = busy_timeout_ms
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > 2:
                raise RuntimeError(f"unsupported reminders database version: {version}")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS targets (
                    id INTEGER PRIMARY KEY, target_id TEXT NOT NULL UNIQUE,
                    channel TEXT NOT NULL, recipient_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK(active IN (0, 1)),
                    rebind_released INTEGER NOT NULL DEFAULT 0 CHECK(rebind_released IN (0, 1)),
                    binding_revision INTEGER NOT NULL DEFAULT 0 CHECK(binding_revision >= 0),
                    created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL REFERENCES targets(id),
                    kind TEXT NOT NULL CHECK(kind IN ('message', 'agent')),
                    status TEXT NOT NULL CHECK(status IN ('active','running_agent','sending','retry_wait','completed','cancelled','failed')),
                    subject TEXT NOT NULL, delivery_text TEXT, agent_prompt TEXT,
                    dtstart_local TEXT NOT NULL, timezone TEXT NOT NULL, rrule TEXT NOT NULL,
                    next_run_at_utc TEXT, created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
                    CHECK((kind = 'message' AND delivery_text IS NOT NULL AND agent_prompt IS NULL)
                       OR (kind = 'agent' AND agent_prompt IS NOT NULL AND delivery_text IS NULL))
                );
                CREATE INDEX IF NOT EXISTS tasks_wake ON tasks(status, next_run_at_utc);
                CREATE TABLE IF NOT EXISTS executions (
                    id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    scheduled_for_utc TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','claimed','succeeded','retryable','failed','cancelled')),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                    agent_attempts INTEGER NOT NULL DEFAULT 0 CHECK(agent_attempts >= 0),
                    send_attempts INTEGER NOT NULL DEFAULT 0 CHECK(send_attempts >= 0),
                    next_attempt_at_utc TEXT, lease_owner TEXT, lease_expires_at_utc TEXT,
                    agent_output TEXT, delivery_success INTEGER, delivery_retryable INTEGER,
                    delivery_code TEXT, delivery_message TEXT, provider_message_id TEXT,
                    created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
                    UNIQUE(task_id, scheduled_for_utc),
                    CHECK((lease_owner IS NULL) = (lease_expires_at_utc IS NULL))
                );
                CREATE INDEX IF NOT EXISTS execution_retry ON executions(status, next_attempt_at_utc);
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(targets)")}
            if version == 0 and {"chat_id", "open_id"}.issubset(columns):
                # The original database was Feishu-only.  Add rather than rebuild
                # the table, so SQLite preserves every target/task/execution row
                # and their integer foreign keys exactly as stored.
                conn.execute("ALTER TABLE targets ADD COLUMN recipient_id TEXT")
                conn.execute("ALTER TABLE targets ADD COLUMN owner_id TEXT")
                conn.execute("UPDATE targets SET recipient_id=chat_id, owner_id=open_id")
                columns.update({"recipient_id", "owner_id"})
            if not {"recipient_id", "owner_id"}.issubset(columns):
                raise RuntimeError("targets table has no supported identity columns")
            if "rebind_released" not in columns:
                # v0/v1 inactive targets cannot be assumed to have been
                # explicitly released.  Keep their historical lock intact.
                conn.execute(
                    "ALTER TABLE targets ADD COLUMN rebind_released "
                    "INTEGER NOT NULL DEFAULT 0 CHECK(rebind_released IN (0, 1))"
                )
                columns.add("rebind_released")
            if "binding_revision" not in columns:
                conn.execute(
                    "ALTER TABLE targets ADD COLUMN binding_revision "
                    "INTEGER NOT NULL DEFAULT 0 CHECK(binding_revision >= 0)"
                )
                columns.add("binding_revision")
            # A migrated v0 table retains its NOT NULL chat_id/open_id columns
            # so foreign keys and integer IDs never need a destructive rebuild.
            # Remember that physical compatibility shape for future writes.
            self._legacy_identity_columns = {"chat_id", "open_id"}.issubset(columns)
            missing = conn.execute(
                "SELECT 1 FROM targets WHERE recipient_id IS NULL OR recipient_id='' OR owner_id IS NULL OR owner_id='' LIMIT 1"
            ).fetchone()
            if missing:
                raise RuntimeError("targets migration left an incomplete identity")
            conn.execute("DROP INDEX IF EXISTS one_active_feishu_target")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS one_active_target ON targets(active) WHERE active=1"
            )
            conn.execute("PRAGMA user_version = 2")

    def get_active_target(self) -> ReminderTarget | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM targets WHERE active = 1").fetchone()
        return self._target(row) if row else None

    def get_target_by_public_id(self, target_id: str, *, active_only: bool = True) -> ReminderTarget | None:
        query = "SELECT * FROM targets WHERE target_id=?"
        if active_only:
            query += " AND active=1"
        with self._connection() as conn:
            row = conn.execute(query, (target_id,)).fetchone()
        return self._target(row) if row else None

    def find_target_by_open_id(self, open_id: str) -> ReminderTarget | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM targets WHERE channel='feishu' AND owner_id=? ORDER BY updated_at_utc DESC LIMIT 1", (open_id,)).fetchone()
        return self._target(row) if row else None

    def bind_target(self, channel: str, recipient_id: str, owner_id: str, now_utc: datetime) -> ReminderTarget:
        if channel not in {"feishu", "weixin"}:
            raise ValueError("unsupported reminder channel")
        if not recipient_id or not owner_id:
            raise ValueError("channel, recipient_id, and owner_id are required")
        now = _time(now_utc)
        with self._connection() as conn:
            # Serialize the ownership check with the following update.  A
            # deferred transaction would let two claimants both observe the
            # released row before either one writes it.
            conn.execute("BEGIN IMMEDIATE")
            target = conn.execute("SELECT * FROM targets ORDER BY id LIMIT 1").fetchone()
            if target:
                same_owner = (
                    target["channel"] == channel and target["owner_id"] == owner_id
                )
                if not same_owner and (
                    target["active"] or not target["rebind_released"]
                ):
                    raise PermissionError("default target belongs to another channel or owner")
                if self._legacy_identity_columns:
                    conn.execute(
                        "UPDATE targets SET channel=?,recipient_id=?,owner_id=?,chat_id=?,open_id=?,"
                        "active=1,rebind_released=0,binding_revision=binding_revision+1,"
                        "updated_at_utc=? WHERE id=?",
                        (
                            channel,
                            recipient_id,
                            owner_id,
                            recipient_id,
                            owner_id,
                            now,
                            target["id"],
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE targets SET channel=?,recipient_id=?,owner_id=?,active=1,"
                        "rebind_released=0,binding_revision=binding_revision+1,"
                        "updated_at_utc=? WHERE id=?",
                        (channel, recipient_id, owner_id, now, target["id"]),
                    )
                row = conn.execute("SELECT * FROM targets WHERE id = ?", (target["id"],)).fetchone()
            else:
                target_id = str(uuid.uuid4())
                if self._legacy_identity_columns:
                    cur = conn.execute(
                        "INSERT INTO targets(target_id,channel,recipient_id,owner_id,chat_id,open_id,"
                        "active,rebind_released,binding_revision,created_at_utc,updated_at_utc) "
                        "VALUES (?,?,?,?,?,?,1,0,1,?,?)",
                        (target_id, channel, recipient_id, owner_id, recipient_id, owner_id, now, now),
                    )
                else:
                    cur = conn.execute(
                        "INSERT INTO targets(target_id,channel,recipient_id,owner_id,active,"
                        "rebind_released,binding_revision,created_at_utc,updated_at_utc) "
                        "VALUES (?, ?, ?, ?, 1, 0, 1, ?, ?)",
                        (target_id, channel, recipient_id, owner_id, now, now),
                    )
                row = conn.execute("SELECT * FROM targets WHERE id = ?", (cur.lastrowid,)).fetchone()
        return self._target(row)

    def unbind_target(self, channel: str, owner_id: str, now_utc: datetime) -> bool:
        with self._connection() as conn:
            owner = conn.execute("SELECT channel,owner_id FROM targets ORDER BY id LIMIT 1").fetchone()
            if owner and (owner["channel"] != channel or owner["owner_id"] != owner_id):
                raise PermissionError("default target belongs to another channel or owner")
            result = conn.execute(
                "UPDATE targets SET active=0, rebind_released=1, updated_at_utc=? "
                "WHERE channel=? AND owner_id=? AND rebind_released=0",
                (_time(now_utc), channel, owner_id),
            )
        return result.rowcount == 1

    # Compatibility wrappers keep the existing Feishu channel/API stable while
    # callers transition to the channel-neutral names.
    def bind_feishu_target(self, chat_id: str, open_id: str, now_utc: datetime) -> ReminderTarget:
        return self.bind_target("feishu", chat_id, open_id, now_utc)

    def unbind_feishu_target(self, open_id: str, now_utc: datetime) -> bool:
        return self.unbind_target("feishu", open_id, now_utc)

    def suspend_target(
        self,
        target_id: str,
        now_utc: datetime,
        *,
        expected_binding_revision: int,
    ) -> bool:
        with self._connection() as conn:
            result = conn.execute(
                "UPDATE targets SET active=0, rebind_released=0, updated_at_utc=? "
                "WHERE target_id=? AND binding_revision=? AND active=1",
                (_time(now_utc), target_id, expected_binding_revision),
            )
        return result.rowcount == 1

    def create_task(self, *, target_id: str, kind: TaskKind, subject: str, dtstart_local: datetime, timezone: str, rrule: str, next_run_at_utc: datetime | None, now_utc: datetime, delivery_text: str | None = None, agent_prompt: str | None = None) -> ReminderTask:
        if next_run_at_utc is None:
            raise ValueError("active task requires next_run_at_utc")
        if not subject.strip() or (kind is TaskKind.MESSAGE) != (delivery_text is not None) or (kind is TaskKind.AGENT) != (agent_prompt is not None):
            raise ValueError("kind payload contract violated")
        local = dtstart_local.replace(tzinfo=None)
        spec = ScheduleSpec.from_rrule(timezone=timezone, dtstart=local, value=rrule)
        if spec.to_rrule() != rrule:
            raise ValueError("rrule must be canonical")
        now = _time(now_utc)
        with self._connection() as conn:
            target = conn.execute("SELECT id FROM targets WHERE target_id = ? AND active = 1", (target_id,)).fetchone()
            if not target:
                raise ValueError("active target does not exist")
            cur = conn.execute("INSERT INTO tasks(target_id,kind,status,subject,delivery_text,agent_prompt,dtstart_local,timezone,rrule,next_run_at_utc,created_at_utc,updated_at_utc) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)", (target["id"], kind.value, subject, delivery_text, agent_prompt, local.isoformat(), timezone, rrule, _time(next_run_at_utc), now, now))
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
        return self._task(row)

    def get_task(self, task_id: int) -> ReminderTask | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task(row) if row else None

    def list_tasks_for_target(self, target_id: str, *, include_inactive: bool = False) -> list[ReminderTask]:
        query = "SELECT t.* FROM tasks t JOIN targets x ON x.id=t.target_id WHERE x.target_id=?"
        if not include_inactive:
            query += " AND t.status NOT IN ('completed','cancelled','failed')"
        query += " ORDER BY t.next_run_at_utc IS NULL, t.next_run_at_utc, t.id"
        with self._connection() as conn:
            rows = conn.execute(query, (target_id,)).fetchall()
        return [self._task(row) for row in rows]

    def list_execution_ids(self, task_id: int) -> list[int]:
        with self._connection() as conn:
            rows = conn.execute("SELECT id FROM executions WHERE task_id=? ORDER BY scheduled_for_utc, id", (task_id,)).fetchall()
        return [row['id'] for row in rows]

    def get_execution(self, execution_id: int) -> ReminderExecution | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM executions WHERE id = ?", (execution_id,)).fetchone()
        return self._execution(row) if row else None

    def recover_expired_leases(self, now_utc: datetime) -> int:
        now = _time(now_utc)
        with self._connection() as conn:
            result = conn.execute("UPDATE executions SET status = 'pending', lease_owner = NULL, lease_expires_at_utc = NULL, updated_at_utc = ? WHERE status = 'claimed' AND lease_expires_at_utc <= ?", (now, now))
            conn.execute("UPDATE tasks SET status = 'active', updated_at_utc = ? WHERE status IN ('running_agent','sending') AND EXISTS (SELECT 1 FROM executions e WHERE e.task_id = tasks.id AND e.status = 'pending')", (now,))
        return result.rowcount

    def recover_schedules(self, now_utc: datetime, offline_window: timedelta = timedelta(hours=1)) -> int:
        """Store one latest missed recurring occurrence; fail stale one-shots."""
        now = _time(now_utc); changed = 0
        with self._connection() as conn:
            rows = conn.execute("SELECT t.* FROM tasks t JOIN targets x ON x.id=t.target_id WHERE t.status='active' AND x.active=1 AND t.next_run_at_utc <= ?", (now,)).fetchall()
            for row in rows:
                spec = ScheduleSpec.from_rrule(timezone=row["timezone"], dtstart=datetime.fromisoformat(row["dtstart_local"]), value=row["rrule"])
                recovery = ScheduleService.recovery_occurrence(spec, now_utc, offline_window)
                if recovery is None:
                    conn.execute("UPDATE tasks SET status='failed',next_run_at_utc=NULL,updated_at_utc=? WHERE id=?", (now, row["id"]))
                else:
                    conn.execute("UPDATE tasks SET next_run_at_utc=?,updated_at_utc=? WHERE id=?", (_time(recovery), now, row["id"]))
                changed += 1
        return changed

    def next_wake_at(self, now_utc: datetime | None = None) -> datetime | None:
        with self._connection() as conn:
            row = conn.execute("SELECT MIN(COALESCE(e.next_attempt_at_utc,t.next_run_at_utc)) AS wake FROM tasks t JOIN targets x ON x.id=t.target_id AND x.active=1 LEFT JOIN executions e ON e.task_id=t.id AND e.status='retryable' WHERE t.status IN ('active','retry_wait')").fetchone()
        return _date(row["wake"])

    def claim_due(
        self,
        *,
        now_utc: datetime,
        lease_owner: str,
        lease_for: timedelta,
        offline_window: timedelta = timedelta(hours=1),
        limit: int = 1,
    ) -> list[ReminderWorkItem]:
        if not lease_owner or lease_for <= timedelta(0) or limit <= 0:
            raise ValueError("lease_owner, lease_for and limit must be positive")
        self.recover_expired_leases(now_utc)
        self.recover_schedules(now_utc, offline_window=offline_window)
        now, expiry = _time(now_utc), _time(now_utc + lease_for)
        claimed: list[ReminderWorkItem] = []
        with self._connection() as conn:
            candidates = conn.execute("SELECT t.id FROM tasks t JOIN targets x ON x.id=t.target_id AND x.active=1 WHERE t.status IN ('active','retry_wait') AND t.next_run_at_utc <= ? ORDER BY t.next_run_at_utc LIMIT ?", (now, limit)).fetchall()
            for candidate in candidates:
                task = conn.execute("SELECT * FROM tasks WHERE id=? AND status IN ('active','retry_wait')", (candidate["id"],)).fetchone()
                scheduled = task["next_run_at_utc"]
                conn.execute("INSERT INTO executions(task_id,scheduled_for_utc,status,attempts,lease_owner,lease_expires_at_utc,created_at_utc,updated_at_utc) VALUES (?,?,'claimed',1,?,?,?,?) ON CONFLICT(task_id,scheduled_for_utc) DO UPDATE SET status='claimed',attempts=executions.attempts+1,lease_owner=excluded.lease_owner,lease_expires_at_utc=excluded.lease_expires_at_utc,updated_at_utc=excluded.updated_at_utc WHERE executions.status IN ('pending','retryable') AND (executions.next_attempt_at_utc IS NULL OR executions.next_attempt_at_utc <= excluded.updated_at_utc)", (task["id"], scheduled, lease_owner, expiry, now, now))
                execution = conn.execute("SELECT * FROM executions WHERE task_id=? AND scheduled_for_utc=? AND status='claimed' AND lease_owner=?", (task["id"], scheduled, lease_owner)).fetchone()
                if not execution:
                    continue
                status = TaskStatus.RUNNING_AGENT if task["kind"] == TaskKind.AGENT else TaskStatus.SENDING
                conn.execute("UPDATE tasks SET status=?,updated_at_utc=? WHERE id=?", (status.value, now, task["id"]))
                claimed.append(self._work_item(conn, execution))
        return claimed

    def get_work_item(self, execution_id: int) -> ReminderWorkItem | None:
        with self._connection() as conn:
            execution = conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
            return self._work_item(conn, execution) if execution else None

    def save_output(self, execution_id: int, *, lease_owner: str, output_text: str, now_utc: datetime) -> bool:
        now = _time(now_utc)
        with self._connection() as conn:
            result = conn.execute("UPDATE executions SET agent_output=?,updated_at_utc=? WHERE id=? AND status='claimed' AND lease_owner=?", (output_text, now, execution_id, lease_owner))
        return result.rowcount == 1

    def record_agent_output(self, execution_id: int, *, lease_owner: str, output: str, now_utc: datetime) -> bool:
        now = _time(now_utc)
        with self._connection() as conn:
            result = conn.execute("UPDATE executions SET agent_output=?,agent_attempts=agent_attempts+1,updated_at_utc=? WHERE id=? AND status='claimed' AND lease_owner=?", (output, now, execution_id, lease_owner))
            if result.rowcount:
                conn.execute("UPDATE tasks SET status='sending',updated_at_utc=? WHERE id=(SELECT task_id FROM executions WHERE id=?) AND status='running_agent'", (now, execution_id))
        return result.rowcount == 1

    def record_agent_failure(self, execution_id: int, *, lease_owner: str, retry_at_utc: datetime | None, now_utc: datetime) -> bool:
        now = _time(now_utc)
        with self._connection() as conn:
            row = conn.execute("SELECT task_id,scheduled_for_utc FROM executions WHERE id=? AND status='claimed' AND lease_owner=?", (execution_id, lease_owner)).fetchone()
            if not row:
                return False
            status = 'retryable' if retry_at_utc else 'failed'
            conn.execute("UPDATE executions SET status=?,agent_attempts=agent_attempts+1,next_attempt_at_utc=?,lease_owner=NULL,lease_expires_at_utc=NULL,updated_at_utc=? WHERE id=?", (status, _time(retry_at_utc) if retry_at_utc else None, now, execution_id))
            task_status = TaskStatus.RETRY_WAIT if retry_at_utc else TaskStatus.FAILED
            conn.execute("UPDATE tasks SET status=?,updated_at_utc=? WHERE id=?", (task_status.value, now, row["task_id"]))
        return True

    schedule_agent_retry = record_agent_failure

    def begin_delivery(self, execution_id: int, *, lease_owner: str, now_utc: datetime) -> bool:
        with self._connection() as conn:
            result = conn.execute("UPDATE tasks SET status='sending',updated_at_utc=? WHERE id=(SELECT task_id FROM executions WHERE id=?) AND status IN ('running_agent','sending') AND EXISTS (SELECT 1 FROM executions e JOIN targets x ON x.id=(SELECT target_id FROM tasks WHERE id=e.task_id) WHERE e.id=? AND e.status='claimed' AND e.lease_owner=? AND x.active=1)", (_time(now_utc), execution_id, execution_id, lease_owner))
        return result.rowcount == 1

    def defer_delivery(
        self,
        execution_id: int,
        *,
        lease_owner: str,
        now_utc: datetime,
        reason: str | None = None,
    ) -> bool:
        """Release a claimed execution whose persisted target is now inactive.

        This is deliberately not a delivery failure: no send-attempt counter or
        retry policy is consumed.  A later rebind claims the same occurrence.
        ``reason`` is accepted for the asynchronous adapter's diagnostic API but
        is not persisted to avoid overwriting a prior delivery result.
        """
        del reason
        now = _time(now_utc)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT e.task_id FROM executions e JOIN tasks t ON t.id=e.task_id "
                "JOIN targets x ON x.id=t.target_id "
                "WHERE e.id=? AND e.status='claimed' AND e.lease_owner=? AND x.active=0",
                (execution_id, lease_owner),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE executions SET status='pending', lease_owner=NULL, "
                "lease_expires_at_utc=NULL, updated_at_utc=? WHERE id=?",
                (now, execution_id),
            )
            conn.execute(
                "UPDATE tasks SET status='active', updated_at_utc=? WHERE id=? "
                "AND status<>'cancelled'",
                (now, row["task_id"]),
            )
        return True

    release_claim_for_inactive_target = defer_delivery

    def is_claim_active(self, execution_id: int, *, lease_owner: str) -> bool:
        with self._connection() as conn:
            row = conn.execute("SELECT 1 FROM executions e JOIN tasks t ON t.id=e.task_id JOIN targets x ON x.id=t.target_id WHERE e.id=? AND e.status='claimed' AND e.lease_owner=? AND t.status IN ('running_agent','sending') AND x.active=1", (execution_id, lease_owner)).fetchone()
        return row is not None

    def schedule_retry(self, execution_id: int, *, lease_owner: str, result: DeliveryResult, retry_at_utc: datetime, now_utc: datetime) -> bool:
        return self.finish_execution(execution_id, lease_owner=lease_owner, result=result, retry_at_utc=retry_at_utc, next_run_at_utc=None, now_utc=now_utc)

    def finish_execution(self, execution_id: int, *, lease_owner: str, result: DeliveryResult, next_run_at_utc: datetime | None, now_utc: datetime, retry_at_utc: datetime | None = None) -> bool:
        now = _time(now_utc)
        with self._connection() as conn:
            row = conn.execute("SELECT task_id,scheduled_for_utc FROM executions WHERE id=? AND status='claimed' AND lease_owner=?", (execution_id, lease_owner)).fetchone()
            if not row:
                return False
            status = ExecutionStatus.SUCCEEDED if result.success else (ExecutionStatus.RETRYABLE if result.retryable else ExecutionStatus.FAILED)
            conn.execute("UPDATE executions SET status=?,send_attempts=send_attempts+1,next_attempt_at_utc=?,lease_owner=NULL,lease_expires_at_utc=NULL,delivery_success=?,delivery_retryable=?,delivery_code=?,delivery_message=?,provider_message_id=?,updated_at_utc=? WHERE id=?", (status.value, _time(retry_at_utc) if retry_at_utc else None, int(result.success), int(result.retryable), result.code, result.message, result.provider_message_id, now, execution_id))
            task_status = TaskStatus.ACTIVE if result.success and next_run_at_utc else (TaskStatus.COMPLETED if result.success else (TaskStatus.RETRY_WAIT if result.retryable else TaskStatus.FAILED))
            # Preserve the original occurrence while retrying.  next_wake_at
            # reads execution.next_attempt_at_utc, and claim_due's upsert gates
            # on that value; changing the task timestamp would create a second
            # execution and could rerun an already-successful Agent.
            task_wake = row["scheduled_for_utc"] if result.retryable else (_time(next_run_at_utc) if next_run_at_utc else None)
            conn.execute("UPDATE tasks SET status=?,next_run_at_utc=?,updated_at_utc=? WHERE id=? AND status<>'cancelled'", (task_status.value, task_wake, now, row["task_id"]))
        return True

    mark_sent = finish_execution

    def cancel_task(self, task_id: int, *, target_id: str, now_utc: datetime) -> bool:
        now = _time(now_utc)
        with self._connection() as conn:
            result = conn.execute("UPDATE tasks SET status='cancelled',next_run_at_utc=NULL,updated_at_utc=? WHERE id=? AND target_id=(SELECT id FROM targets WHERE target_id=?) AND status NOT IN ('completed','cancelled')", (now, task_id, target_id))
            conn.execute("UPDATE executions SET status='cancelled',lease_owner=NULL,lease_expires_at_utc=NULL,updated_at_utc=? WHERE task_id=? AND status IN ('pending','retryable','claimed')", (now, task_id))
        return result.rowcount == 1

    @staticmethod
    def _target(row: sqlite3.Row) -> ReminderTarget:
        return ReminderTarget(row['id'], row['target_id'], row['channel'], row['recipient_id'], row['owner_id'], bool(row['active']), bool(row['rebind_released']), row['binding_revision'], _date(row['created_at_utc']), _date(row['updated_at_utc']))
    @staticmethod
    def _task(row: sqlite3.Row) -> ReminderTask:
        return ReminderTask(row['id'], row['target_id'], TaskKind(row['kind']), TaskStatus(row['status']), row['subject'], row['delivery_text'], row['agent_prompt'], datetime.fromisoformat(row['dtstart_local']), row['timezone'], row['rrule'], _date(row['next_run_at_utc']), _date(row['created_at_utc']), _date(row['updated_at_utc']))
    def _work_item(self, conn: sqlite3.Connection, execution: sqlite3.Row) -> ReminderWorkItem:
        row = conn.execute("SELECT x.target_id,x.channel,x.recipient_id,x.owner_id,t.* FROM tasks t JOIN targets x ON x.id=t.target_id WHERE t.id=?", (execution['task_id'],)).fetchone()
        return ReminderWorkItem(self._execution(execution), TaskKind(row['kind']), row['target_id'], row['channel'], row['recipient_id'], row['owner_id'], row['subject'], row['delivery_text'], row['agent_prompt'])
    @staticmethod
    def _execution(row: sqlite3.Row) -> ReminderExecution:
        return ReminderExecution(row['id'], row['task_id'], _date(row['scheduled_for_utc']), ExecutionStatus(row['status']), row['attempts'], row['agent_attempts'], row['send_attempts'], _date(row['next_attempt_at_utc']), row['lease_owner'], _date(row['lease_expires_at_utc']), row['agent_output'], delivery_result_from_row(row), _date(row['created_at_utc']), _date(row['updated_at_utc']))
