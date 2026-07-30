import json
import unittest
from datetime import UTC, datetime

from agent.tools.current_time import CurrentTimeTool


class CurrentTimeToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_shanghai_time_has_stable_fields(self):
        tool = CurrentTimeTool(clock=lambda: datetime(2026, 1, 1, 0, 30, tzinfo=UTC))
        value = json.loads(await tool.execute())
        self.assertEqual(value["datetime"], "2026-01-01T08:30:00+08:00")
        self.assertEqual(value["date"], "2026-01-01")
        self.assertEqual(value["time"], "08:30:00+08:00")
        self.assertEqual(value["weekday"], "Thursday")
        self.assertEqual(value["timezone"], "Asia/Shanghai")
        self.assertEqual(value["utc_offset"], "+08:00")

    async def test_dst_offset_uses_requested_iana_timezone(self):
        tool = CurrentTimeTool("UTC", clock=lambda: datetime(2026, 7, 1, 12, tzinfo=UTC))
        value = json.loads(await tool.execute(timezone="America/New_York"))
        self.assertEqual(value["datetime"], "2026-07-01T08:00:00-04:00")
        self.assertEqual(value["utc_offset"], "-04:00")

    async def test_invalid_timezone_is_a_safe_tool_error(self):
        tool = CurrentTimeTool(clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
        self.assertIn("有效的 IANA", await tool.execute(timezone="CST"))

    def test_invalid_default_timezone_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            CurrentTimeTool("not/a-zone")


if __name__ == "__main__":
    unittest.main()
