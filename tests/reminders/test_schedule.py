from datetime import UTC, datetime, timedelta
import unittest

from reminders.schedule import ScheduleService, ScheduleSpec


class ScheduleTests(unittest.TestCase):
    def test_once_uses_canonical_daily_count_one(self):
        spec = ScheduleSpec.once(datetime(2026, 4, 1, 9, 0), "Asia/Shanghai")
        self.assertEqual(spec.to_rrule(), "RRULE:FREQ=DAILY;COUNT=1")

    def test_rejects_invalid_input(self):
        with self.assertRaises(ValueError):
            ScheduleSpec("No/Such_Zone", datetime(2026, 1, 1), count=1)
        with self.assertRaises(ValueError):
            ScheduleSpec("UTC", datetime(2026, 1, 1), count=0)
        with self.assertRaises(ValueError):
            ScheduleSpec("UTC", datetime(2026, 1, 1), count=1, until=datetime.now(UTC))
        with self.assertRaises(ValueError):
            ScheduleSpec("America/New_York", datetime(2026, 3, 8, 2, 30))
        with self.assertRaises(ValueError):
            ScheduleSpec.from_rrule(timezone="UTC", dtstart=datetime(2026, 1, 1), value="FREQ=DAILY")

    def test_dst_gap_is_skipped_and_overlap_uses_first_fold(self):
        gap = ScheduleSpec("America/New_York", datetime(2026, 3, 7, 2, 30), count=3)
        dates = list(ScheduleService.occurrences(gap))
        self.assertEqual([value.astimezone().date().isoformat() for value in dates], ["2026-03-07", "2026-03-09", "2026-03-10"])
        overlap = ScheduleSpec.once(datetime(2026, 11, 1, 1, 30), "America/New_York")
        occurrence = ScheduleService.next_after(overlap, None)
        self.assertEqual(occurrence, datetime(2026, 11, 1, 5, 30, tzinfo=UTC))

    def test_month_end_weekday_count_and_until(self):
        monthly = ScheduleSpec("UTC", datetime(2026, 1, 31, 9), freq="MONTHLY", count=3)
        self.assertEqual([item.date().isoformat() for item in ScheduleService.occurrences(monthly)], ["2026-01-31", "2026-03-31", "2026-05-31"])
        weekly = ScheduleSpec("UTC", datetime(2026, 1, 1, 9), freq="WEEKLY", byweekday=(0, 2), count=3)
        self.assertEqual([item.date().isoformat() for item in ScheduleService.occurrences(weekly)], ["2026-01-05", "2026-01-07", "2026-01-12"])
        bounded = ScheduleSpec("UTC", datetime(2026, 1, 1, 9), until=datetime(2026, 1, 3, 9, tzinfo=UTC))
        self.assertEqual(len(list(ScheduleService.occurrences(bounded))), 3)

    def test_preview_next_and_recovery_are_bounded(self):
        spec = ScheduleSpec("UTC", datetime(2026, 1, 1, 9))
        now = datetime(2026, 1, 3, 12, tzinfo=UTC)
        self.assertEqual(ScheduleService.preview(spec, now), [datetime(2026, 1, 4, 9, tzinfo=UTC), datetime(2026, 1, 5, 9, tzinfo=UTC), datetime(2026, 1, 6, 9, tzinfo=UTC)])
        self.assertEqual(ScheduleService.next_after(spec, datetime(2026, 1, 3, 9, tzinfo=UTC)), datetime(2026, 1, 4, 9, tzinfo=UTC))
        self.assertEqual(ScheduleService.recovery_occurrence(spec, now), datetime(2026, 1, 3, 9, tzinfo=UTC))
        one = ScheduleSpec.once(datetime(2026, 1, 1, 9), "UTC")
        self.assertIsNone(ScheduleService.recovery_occurrence(one, now, timedelta(hours=1)))
