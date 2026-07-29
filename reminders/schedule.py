"""RFC 5545 recurrence calculations, evaluated in the task's IANA timezone."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import rrule


_FREQUENCIES = {"HOURLY": rrule.HOURLY, "DAILY": rrule.DAILY, "WEEKLY": rrule.WEEKLY, "MONTHLY": rrule.MONTHLY}
_WEEKDAYS = (rrule.MO, rrule.TU, rrule.WE, rrule.TH, rrule.FR, rrule.SA, rrule.SU)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """Strict recurrence input. ``dtstart`` is a local wall-clock datetime.

    Weekdays use RFC values Monday=0 through Sunday=6.  A one-shot schedule is
    represented as ``freq='DAILY', count=1`` and is intentionally not a
    separate storage shape.
    """

    timezone: str
    dtstart: datetime
    freq: str = "DAILY"
    interval: int = 1
    byweekday: tuple[int, ...] = ()
    bymonthday: tuple[int, ...] = ()
    count: int | None = None
    until: datetime | None = None

    def __post_init__(self) -> None:
        try:
            tz = ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {self.timezone}") from exc
        freq = self.freq.upper()
        object.__setattr__(self, "freq", freq)
        if freq not in _FREQUENCIES:
            raise ValueError(f"unsupported frequency: {freq}")
        if self.interval < 1:
            raise ValueError("interval must be positive")
        if self.count is not None and self.count < 1:
            raise ValueError("count must be positive")
        if self.count is not None and self.until is not None:
            raise ValueError("COUNT and UNTIL cannot be combined")
        if self.dtstart.tzinfo is not None and self.dtstart.utcoffset() is None:
            raise ValueError("dtstart has an invalid timezone")
        local = self.dtstart.replace(tzinfo=None)
        if not _is_valid_local(local, tz):
            raise ValueError("dtstart is a nonexistent local time")
        object.__setattr__(self, "dtstart", local)
        if any(day < 0 or day > 6 for day in self.byweekday):
            raise ValueError("byweekday values must be 0 (Monday) through 6 (Sunday)")
        if len(set(self.byweekday)) != len(self.byweekday):
            raise ValueError("byweekday cannot contain duplicates")
        if any(day == 0 or day < -31 or day > 31 for day in self.bymonthday):
            raise ValueError("bymonthday values must be -31 through -1 or 1 through 31")
        if len(set(self.bymonthday)) != len(self.bymonthday):
            raise ValueError("bymonthday cannot contain duplicates")
        if self.until is not None:
            until = _utc(self.until)
            if until < self.local_dtstart.astimezone(UTC):
                raise ValueError("until cannot precede dtstart")
            object.__setattr__(self, "until", until)

    @classmethod
    def once(cls, when: datetime, timezone: str) -> "ScheduleSpec":
        """Build the canonical one-shot representation required by RFC 5545."""
        return cls(timezone=timezone, dtstart=when, freq="DAILY", count=1)

    @property
    def local_dtstart(self) -> datetime:
        return self.dtstart.replace(tzinfo=ZoneInfo(self.timezone), fold=0)

    def to_rrule(self) -> str:
        parts = [f"FREQ={self.freq}"]
        if self.interval != 1:
            parts.append(f"INTERVAL={self.interval}")
        if self.byweekday:
            names = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
            parts.append("BYDAY=" + ",".join(names[day] for day in sorted(self.byweekday)))
        if self.bymonthday:
            parts.append("BYMONTHDAY=" + ",".join(str(day) for day in sorted(self.bymonthday)))
        if self.count is not None:
            parts.append(f"COUNT={self.count}")
        if self.until is not None:
            parts.append("UNTIL=" + self.until.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"))
        return "RRULE:" + ";".join(parts)

    @classmethod
    def from_rrule(cls, *, timezone: str, dtstart: datetime, value: str) -> "ScheduleSpec":
        """Parse only the RFC subset emitted by :meth:`to_rrule`."""
        if not value.startswith("RRULE:"):
            raise ValueError("RRULE prefix is required")
        text = value.removeprefix("RRULE:")
        fields: dict[str, str] = {}
        for item in text.split(";"):
            key, separator, raw = item.partition("=")
            if not separator or key in fields:
                raise ValueError("invalid RRULE")
            fields[key] = raw
        unsupported = set(fields) - {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "COUNT", "UNTIL"}
        if unsupported or "FREQ" not in fields:
            raise ValueError("unsupported RRULE")
        days = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
        until = datetime.strptime(fields["UNTIL"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC) if "UNTIL" in fields else None
        return cls(
            timezone=timezone, dtstart=dtstart, freq=fields["FREQ"], interval=int(fields.get("INTERVAL", "1")),
            byweekday=tuple(days[item] for item in fields.get("BYDAY", "").split(",") if item),
            bymonthday=tuple(int(item) for item in fields.get("BYMONTHDAY", "").split(",") if item),
            count=int(fields["COUNT"]) if "COUNT" in fields else None, until=until,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "timezone": self.timezone, "dtstart": self.dtstart.isoformat(), "freq": self.freq,
            "interval": self.interval, "byweekday": list(self.byweekday),
            "bymonthday": list(self.bymonthday), "count": self.count,
            "until": self.until.isoformat() if self.until else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "ScheduleSpec":
        return cls(
            timezone=str(value["timezone"]), dtstart=datetime.fromisoformat(str(value["dtstart"])),
            freq=str(value.get("freq", "DAILY")), interval=int(value.get("interval", 1)),
            byweekday=tuple(int(v) for v in value.get("byweekday", ())),
            bymonthday=tuple(int(v) for v in value.get("bymonthday", ())), count=(int(value["count"]) if value.get("count") is not None else None),
            until=(datetime.fromisoformat(str(value["until"])) if value.get("until") else None),
        )


def _is_valid_local(value: datetime, tz: ZoneInfo) -> bool:
    aware = value.replace(tzinfo=tz, fold=0)
    return aware.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == value


class ScheduleService:
    """Pure recurrence service. Callers own the scheduler clock and policy."""

    @staticmethod
    def occurrences(spec: ScheduleSpec, after_utc: datetime | None = None) -> Iterable[datetime]:
        rule = rrule.rrule(
            _FREQUENCIES[spec.freq], dtstart=spec.local_dtstart, interval=spec.interval,
            byweekday=[_WEEKDAYS[d] for d in spec.byweekday] or None,
            bymonthday=list(spec.bymonthday) or None, count=None, until=spec.until,
        )
        after = _utc(after_utc) if after_utc else None
        emitted = 0
        for occurrence in rule:
            # dateutil may emit a DST gap as a wall-clock value. RFC 5545 says
            # such invalid local times must be ignored.
            if not _is_valid_local(occurrence.replace(tzinfo=None), ZoneInfo(spec.timezone)):
                continue
            emitted += 1
            if spec.count is not None and emitted > spec.count:
                break
            value = occurrence.astimezone(UTC)
            if after is None or value > after:
                yield value

    @classmethod
    def next_after(cls, spec: ScheduleSpec, occurrence_utc: datetime | None) -> datetime | None:
        return next(iter(cls.occurrences(spec, occurrence_utc)), None)

    @classmethod
    def preview(cls, spec: ScheduleSpec, after_utc: datetime, limit: int = 3) -> list[datetime]:
        if not 1 <= limit <= 3:
            raise ValueError("preview limit must be between 1 and 3")
        iterator = cls.occurrences(spec, after_utc)
        return [value for _, value in zip(range(limit), iterator)]

    @classmethod
    def recovery_occurrence(
        cls, spec: ScheduleSpec, now_utc: datetime, offline_window: timedelta = timedelta(hours=1)
    ) -> datetime | None:
        """Return one occurrence after restart: latest due, otherwise next future.

        A one-shot older than the bounded offline window is skipped. Recurring
        schedules deliberately return only one latest missed occurrence, never a
        batch catch-up.  The caller can then create/claim precisely that one.
        """
        now = _utc(now_utc)
        if offline_window < timedelta(0):
            raise ValueError("offline_window cannot be negative")
        latest_due: datetime | None = None
        next_future: datetime | None = None
        for occurrence in cls.occurrences(spec):
            if occurrence <= now:
                latest_due = occurrence
                continue
            next_future = occurrence
            break
        if spec.count == 1:
            if latest_due is not None and now - latest_due <= offline_window:
                return latest_due
            return next_future
        return latest_due or next_future
