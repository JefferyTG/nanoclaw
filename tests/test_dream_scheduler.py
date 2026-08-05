"""Tests for TASK-011 第二阶段：定时做梦调度 + 启动补做 + dream_time 配置。

覆盖：
- config：dream_time 默认值 / 配置文件覆盖 / 缺省回退不报错（同 context_budget_tokens）；
- DreamState：dream_state.json 读写、缺失/损坏按无记录、写失败静默；
- should_catch_up：首次启动补昨天、已整理不重复补、超期只补最近 1 天；
- collect_messages_for_date：按日期过滤 + 过滤记忆补丁/快照消息；
- DreamScheduler：mock 时钟推进到 dream_time 触发执行、晚启动立即补跑当天、
  实例时区换算、stop 优雅关闭、整理异常不影响调度循环；
- 与 ReminderScheduler 独立 task 互不影响；
- run_dream_for_date：补做写 daily + 更新状态；模型失败不更新状态；
  无消息不调模型但标记已整理；
- dream_consolidate 返回值契约（True=完成 / False=模型失败）。
"""

import asyncio
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from config import NanoClawConfig, load_config
from main import (
    DreamScheduler,
    DreamState,
    collect_messages_for_date,
    run_dream_for_date,
    should_catch_up,
)
from agent.daily import DailyMemory, dream_consolidate
from providers.base import LLMResponse
from reminders.scheduler import ReminderScheduler
from session.manager import SessionManager


class FakeClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value

    def advance(self, delta):
        self.value += delta


class _DreamProvider:
    """固定返回做梦整理 JSON 的假 Provider；content 可替换。"""

    def __init__(self, content=None):
        self.requests = []
        self.content = content or json.dumps({
            "用户变化": ["用户偏好中文回复"],
            "项目进展": ["完成 TASK-011 第二阶段"],
            "会话总结": ["和用户聊了做梦机制"],
        }, ensure_ascii=False)

    async def chat(self, messages, tools=None, model=None):
        self.requests.append(messages)
        return LLMResponse(self.content)


class _ErrorProvider:
    async def chat(self, messages, tools=None, model=None):
        raise RuntimeError("boom")


class _EmptyProvider:
    async def chat(self, messages, tools=None, model=None):
        return LLMResponse(None, finish_reason="error")


MESSAGES = [
    {"role": "user", "content": "今天完成了做梦机制的第二阶段"},
    {"role": "assistant", "content": "好的，已整理到 daily。"},
]


# —— 配置：dream_time 缺省默认值 / 覆盖 / 缺省不报错 ——

class DreamTimeConfigTests(unittest.TestCase):
    def test_dream_time_default(self):
        self.assertEqual(NanoClawConfig().dream_time, "02:00")

    def test_config_file_overrides_dream_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text(json.dumps({"dream_time": "03:30"}), encoding="utf-8")
            self.assertEqual(load_config(str(path)).dream_time, "03:30")

    def test_missing_dream_time_falls_back_without_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text(json.dumps({"api_key": "x"}), encoding="utf-8")
            cfg = load_config(str(path))
            self.assertEqual(cfg.dream_time, "02:00")
            self.assertEqual(cfg.api_key, "x")  # 其他字段不受影响


# —— DreamState：状态文件读写 ——

class DreamStateTests(unittest.TestCase):
    def test_round_trip_and_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = DreamState(tmp)
            self.assertIsNone(state.read_last_dream_date())
            state.write_last_dream_date("2026-08-04")
            self.assertEqual(state.read_last_dream_date(), "2026-08-04")
            raw = Path(tmp, "dream_state.json").read_text(encoding="utf-8")
            self.assertEqual(json.loads(raw), {"last_dream_date": "2026-08-04"})

    def test_missing_or_corrupt_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(DreamState(tmp).read_last_dream_date())
            Path(tmp, "dream_state.json").write_text("{broken", encoding="utf-8")
            self.assertIsNone(DreamState(tmp).read_last_dream_date())

    def test_write_is_monotonic(self):
        """状态写入只前进不后退：并发下「补做昨天」不会把「已写今天」回退。"""
        with tempfile.TemporaryDirectory() as tmp:
            state = DreamState(tmp)
            state.write_last_dream_date("2026-08-05")  # 定时到点已写今天
            state.write_last_dream_date("2026-08-04")  # 启动补做随后写昨天
            self.assertEqual(state.read_last_dream_date(), "2026-08-05")

    def test_write_failure_is_silent(self):
        # memory_dir 不存在 → OSError 静默，不抛异常
        state = DreamState(os.path.join(tempfile.gettempdir(), "nope-xyz-dream", "memory"))
        state.write_last_dream_date("2026-08-04")  # 不应抛出


# —— 启动补做判定：should_catch_up ——

class ShouldCatchUpTests(unittest.TestCase):
    def test_first_run_catches_up_yesterday(self):
        # 首次启动无状态文件：last_dream_date 视为无，补做一次昨天
        self.assertEqual(should_catch_up(None, date(2026, 8, 5)), "2026-08-04")

    def test_yesterday_already_done_noop(self):
        self.assertIsNone(should_catch_up("2026-08-04", date(2026, 8, 5)))

    def test_stale_state_only_catches_yesterday(self):
        # last_dream_date 落后 3 天：只补最近 1 天（昨天），超期不回溯
        self.assertEqual(should_catch_up("2026-08-01", date(2026, 8, 5)), "2026-08-04")

    def test_today_marked_noop(self):
        self.assertIsNone(should_catch_up("2026-08-05", date(2026, 8, 5)))

    def test_future_marker_noop(self):
        self.assertIsNone(should_catch_up("2026-08-06", date(2026, 8, 5)))


# —— 消息收集：按日期过滤 + 过滤记忆补丁/快照 ——

class CollectMessagesTests(unittest.TestCase):
    @staticmethod
    def _write_session(sessions_dir, records):
        os.makedirs(sessions_dir, exist_ok=True)
        with open(os.path.join(sessions_dir, "cli_direct.jsonl"), "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def test_filters_by_date_and_patch_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_session(tmp, [
                {"role": "user", "content": "昨天的消息", "timestamp": "2026-08-04T10:00:00"},
                {"role": "user", "content": "今天的重要消息", "timestamp": "2026-08-05T10:00:00"},
                {"role": "system", "content": "<memory_patch revision=\"1\">...",
                 "timestamp": "2026-08-05T10:30:00"},
                {"role": "assistant", "content": "今天的回复", "timestamp": "2026-08-05T11:00:00"},
            ])
            sm = SessionManager(tmp)
            got = collect_messages_for_date(sm, "2026-08-05")
            contents = [m["content"] for m in got]
            self.assertIn("今天的重要消息", contents)
            self.assertIn("今天的回复", contents)
            self.assertNotIn("昨天的消息", contents)
            self.assertFalse(any("<memory_patch" in m.get("content", "") for m in got))

    def test_empty_sessions_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(os.path.join(tmp, "sessions"))
            self.assertEqual(collect_messages_for_date(sm, "2026-08-05"), [])


# —— 定时调度器：mock 时钟推进验证 ——

class DreamSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def make(self, consolidate, clock, dream_time="02:00", timezone="UTC", wait=None):
        return DreamScheduler(
            consolidate, dream_time=dream_time, timezone=timezone,
            clock=clock, wait=wait,
        )

    async def test_scheduled_runs_at_dream_time(self):
        """mock 时钟推进到 dream_time（02:00）触发一次整理。"""
        clock = FakeClock(datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc))
        calls = []

        async def consolidate():
            calls.append(clock.now())

        scheduler = self.make(consolidate, clock)

        async def wait(event, timeout):
            clock.advance(timedelta(seconds=timeout))
            if calls:  # 到点执行后，下一次等待时停止
                await scheduler.stop()

        scheduler._wait = wait
        await scheduler.run()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], datetime(2026, 8, 5, 2, 0, tzinfo=timezone.utc))

    async def test_late_start_runs_immediately_then_waits_until_tomorrow(self):
        """进程晚启动（已过 dream_time）：立即补跑当天，随后睡到明天到点。"""
        clock = FakeClock(datetime(2026, 8, 5, 3, 0, tzinfo=timezone.utc))
        calls = []
        waits = []

        async def consolidate():
            calls.append(clock.now())

        scheduler = self.make(consolidate, clock)

        async def wait(event, timeout):
            waits.append(timeout)
            clock.advance(timedelta(seconds=timeout))
            await scheduler.stop()

        scheduler._wait = wait
        await scheduler.run()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], datetime(2026, 8, 5, 3, 0, tzinfo=timezone.utc))
        self.assertEqual(waits, [82800.0])  # 睡到明天 02:00（23h）

    async def test_dream_time_respects_instance_timezone(self):
        """02:00 Asia/Shanghai = 前一天 18:00 UTC，按实例时区换算。"""
        # 2026-08-04 17:00 UTC = 上海 2026-08-05 01:00
        clock = FakeClock(datetime(2026, 8, 4, 17, 0, tzinfo=timezone.utc))
        calls = []

        async def consolidate():
            calls.append(clock.now())

        scheduler = self.make(consolidate, clock, timezone="Asia/Shanghai")

        async def wait(event, timeout):
            clock.advance(timedelta(seconds=timeout))
            if calls:
                await scheduler.stop()

        scheduler._wait = wait
        await scheduler.run()
        self.assertEqual(len(calls), 1)
        # 到点 = 上海 02:00 = UTC 2026-08-04 18:00
        self.assertEqual(calls[0], datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc))

    async def test_stop_during_wait_does_not_run(self):
        """等待中 stop：不执行整理，循环干净退出。"""
        clock = FakeClock(datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc))
        calls = []

        async def consolidate():
            calls.append(clock.now())

        scheduler = self.make(consolidate, clock)

        async def wait(event, timeout):
            await scheduler.stop()

        scheduler._wait = wait
        await scheduler.run()
        self.assertEqual(calls, [])

    async def test_consolidate_error_does_not_kill_loop(self):
        """整理函数抛异常：调度循环吞掉，继续运行到下一个等待/停止。"""
        clock = FakeClock(datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc))
        calls = []

        async def consolidate():
            calls.append(clock.now())
            if len(calls) == 1:
                raise RuntimeError("boom")

        scheduler = self.make(consolidate, clock)

        async def wait(event, timeout):
            clock.advance(timedelta(seconds=timeout))
            if len(calls) >= 1:
                await scheduler.stop()

        scheduler._wait = wait
        await scheduler.run()
        self.assertEqual(len(calls), 1)  # 异常被吞，循环存活并正常停止

    async def test_start_stop_lifecycle(self):
        """start() 创建独立后台 task，stop() 优雅关闭。"""
        clock = FakeClock(datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc))
        calls = []

        async def consolidate():
            calls.append(clock.now())

        scheduler = self.make(consolidate, clock)
        task = scheduler.start()
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        await scheduler.stop()
        self.assertTrue(task.done())
        self.assertEqual(calls, [])

    async def test_reminder_and_dream_schedulers_are_independent(self):
        """DreamScheduler 与 ReminderScheduler 是独立 task，互不影响。"""
        clock = FakeClock(datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc))
        dream_calls = []

        async def consolidate():
            dream_calls.append(clock.now())

        class EmptyRepository:
            async def recover_expired_leases(self, now):
                pass

            async def next_wake_at(self, now):
                return clock.now() + timedelta(hours=1)

            async def claim_due(self, now, lease_until):
                return None

        async def agent(prompt, session):
            return ""

        async def deliver(execution, output):
            return SimpleNamespace(success=True)

        reminder = ReminderScheduler(
            EmptyRepository(), agent, deliver, clock=clock,
            retry_delay=lambda _: timedelta(minutes=1),
        )
        dream = DreamScheduler(consolidate, dream_time="02:00", timezone="UTC", clock=clock)

        r_task = asyncio.create_task(reminder.run())
        d_task = asyncio.create_task(dream.run())
        await asyncio.sleep(0.05)
        # 只停 dream：reminder 不受影响，仍在运行
        await dream.stop()
        await asyncio.sleep(0.05)
        self.assertTrue(d_task.done())
        self.assertFalse(r_task.done())
        self.assertEqual(dream_calls, [])  # 未到点，dream 未执行
        # 再停 reminder：干净退出
        await reminder.stop()
        await asyncio.wait_for(r_task, timeout=1)
        self.assertTrue(r_task.done())


# —— 启动补做：写 daily + 更新状态 ——

class CatchUpConsolidationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _write_session(sessions_dir, records):
        os.makedirs(sessions_dir, exist_ok=True)
        with open(os.path.join(sessions_dir, "cli_direct.jsonl"), "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    async def test_catchup_consolidates_yesterday_and_marks_state(self):
        """补做昨天：只整理昨天的会话消息，写 daily 并更新状态。"""
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = os.path.join(tmp, "memory")
            sessions_dir = os.path.join(tmp, "sessions")
            os.makedirs(memory_dir)
            self._write_session(sessions_dir, [
                {"role": "user", "content": "昨天完成了 TASK-011", "timestamp": "2026-08-04T10:00:00"},
                {"role": "assistant", "content": "已整理", "timestamp": "2026-08-04T10:05:00"},
                {"role": "user", "content": "今天的事不归昨天", "timestamp": "2026-08-05T10:00:00"},
            ])
            daily = DailyMemory(memory_dir)
            provider = _DreamProvider()
            state = DreamState(memory_dir)
            sm = SessionManager(sessions_dir)

            await run_dream_for_date(provider, daily, sm, state, "2026-08-04")

            content = daily.read("2026-08-04")
            self.assertIn("# 2026-08-04", content)
            self.assertIn("## 用户变化", content)
            self.assertIn("- 用户偏好中文回复", content)
            self.assertEqual(state.read_last_dream_date(), "2026-08-04")
            self.assertNotIn("今天的事不归昨天", content)  # 今天消息未混入

    async def test_catchup_model_failure_does_not_mark_state(self):
        """模型调用失败：静默返回，不写 daily、不更新状态（下次启动可重试）。"""
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = os.path.join(tmp, "memory")
            sessions_dir = os.path.join(tmp, "sessions")
            os.makedirs(memory_dir)
            self._write_session(sessions_dir, [
                {"role": "user", "content": "昨天的事", "timestamp": "2026-08-04T10:00:00"},
            ])
            daily = DailyMemory(memory_dir)
            state = DreamState(memory_dir)
            sm = SessionManager(sessions_dir)

            await run_dream_for_date(_ErrorProvider(), daily, sm, state, "2026-08-04")

            self.assertIsNone(state.read_last_dream_date())
            self.assertEqual(daily.read("2026-08-04"), "")

    async def test_catchup_no_messages_marks_done_without_model(self):
        """该日期无会话消息：不调模型，但标记已整理（避免每次启动重复尝试）。"""
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = os.path.join(tmp, "memory")
            os.makedirs(memory_dir)
            daily = DailyMemory(memory_dir)
            provider = _DreamProvider()
            state = DreamState(memory_dir)
            sm = SessionManager(os.path.join(tmp, "sessions"))

            await run_dream_for_date(provider, daily, sm, state, "2026-08-04")

            self.assertEqual(provider.requests, [])
            self.assertEqual(state.read_last_dream_date(), "2026-08-04")

    async def test_dream_consolidate_return_contract(self):
        """dream_consolidate 返回值契约：True=完成，False=模型失败/空响应。"""
        with tempfile.TemporaryDirectory() as tmp:
            daily = DailyMemory(os.path.join(tmp, "memory"))
            self.assertTrue(await dream_consolidate(
                _DreamProvider(), daily, "2026-08-04", MESSAGES))
            self.assertFalse(await dream_consolidate(
                _ErrorProvider(), daily, "2026-08-04", MESSAGES))
            self.assertFalse(await dream_consolidate(
                _EmptyProvider(), daily, "2026-08-04", MESSAGES))
            self.assertTrue(await dream_consolidate(
                _DreamProvider(), daily, "2026-08-04", []))  # 无消息=无事可做
            self.assertTrue(await dream_consolidate(
                _DreamProvider(), None, "2026-08-04", MESSAGES))  # daily 未启用


if __name__ == "__main__":
    unittest.main()
