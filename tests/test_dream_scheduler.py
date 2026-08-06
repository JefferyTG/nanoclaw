"""Tests for TASK-011 定时做梦调度 + 启动补做 + dream_time 配置；
TASK-013 语义修正（定时整理昨天 / 补做最后有消息日期 / 晚启动竞态 / 无消息不推进）。

覆盖：
- config：dream_time 默认值 / 配置文件覆盖 / 缺省回退不报错（同 context_budget_tokens）；
- DreamState：dream_state.json 读写、缺失/损坏按无记录、写失败静默、只前进不后退；
- find_last_active_date（替代旧 should_catch_up）：从昨天往前找最后一个有消息的
  日期（last < D < today）、只补一天、今天消息不算、patch/snapshot 不计为有消息；
- collect_messages_for_date：按日期过滤 + 过滤记忆补丁/快照消息 + mtime 剪枝；
- DreamScheduler：mock 时钟推进到 dream_time 触发执行、晚启动立即补跑（目标=昨天）、
  实例时区换算、stop 优雅关闭、整理异常不影响调度循环；
- 与 ReminderScheduler 独立 task 互不影响；
- run_dream_for_date：有消息补做写 daily + 更新状态；模型失败不更新状态；
  无消息不调模型、不推进状态；
- DreamPipelineTests（TASK-013 验收）：定时到点整理昨天、连续每日不重不漏、
  停机多天补最后有消息日期、晚启动竞态修复、旧代码「last=当天」遗留状态仍恢复昨天、
  无消息不推进、last 只前进不后退；
- TASK-014 睡眠唤醒补做：单进程多日睡眠（8-9 → 8-13）唤醒时在整理昨天之外补做
  「最后有消息日期」（如 8-9），跨日==1 不触发、与启动补做共用去重、无消息不推进；
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
    build_dream_components,
    collect_messages_for_date,
    find_last_active_date,
    run_dream_for_date,
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


def _write_session_file(sessions_dir, records):
    """写一个 cli_direct 会话文件，并把文件 mtime 拨到「最后一条消息日期 + 1 天」。

    测试常使用相对系统时钟的「未来」时间戳（如 2026-08-10 的消息），而
    collect_messages_for_date 现在按会话文件 mtime 剪枝：若 mtime 早于目标日零点
    会误判该日无消息。把 mtime 拨到消息日期之后可避免剪枝误伤，同时不影响
    「确实没有消息的日期」仍被正确剪枝。
    """
    os.makedirs(sessions_dir, exist_ok=True)
    path = os.path.join(sessions_dir, "cli_direct.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    latest = None
    for r in records:
        ts = r.get("timestamp")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if latest is None or dt > latest:
            latest = dt
    if latest is not None:
        mtime = (latest + timedelta(days=1)).timestamp()
        os.utime(path, (mtime, mtime))


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


# —— 启动补做目标：find_last_active_date（从昨天往前找最后一个有消息的日期）——

class FindLastActiveDateTests(unittest.TestCase):
    @staticmethod
    def _write_session(sessions_dir, records):
        _write_session_file(sessions_dir, records)

    def test_first_run_no_messages_returns_none(self):
        # 首次启动无会话/无消息：不补做
        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(os.path.join(tmp, "sessions"))
            self.assertIsNone(find_last_active_date(sm, None, date(2026, 8, 5)))

    def test_first_run_yesterday_has_messages(self):
        # 首次启动、昨天有消息：补做昨天
        with tempfile.TemporaryDirectory() as tmp:
            self._write_session(tmp, [
                {"role": "user", "content": "x", "timestamp": "2026-08-04T10:00:00"},
            ])
            sm = SessionManager(tmp)
            self.assertEqual(find_last_active_date(sm, None, date(2026, 8, 5)), "2026-08-04")

    def test_multi_day_downtime_targets_last_active_date(self):
        # 停机多天：8-9 有消息、8-10/11/12 无消息 → 8-13 启动 → 目标 2026-08-09（不是 8-12）
        with tempfile.TemporaryDirectory() as tmp:
            self._write_session(tmp, [
                {"role": "user", "content": "8-9 的消息", "timestamp": "2026-08-09T10:00:00"},
            ])
            sm = SessionManager(tmp)
            self.assertEqual(find_last_active_date(sm, None, date(2026, 8, 13)), "2026-08-09")

    def test_already_covered_or_future_marker_noop(self):
        # last 已覆盖昨天 / == 昨天 / 未来标记：均不重复补做
        with tempfile.TemporaryDirectory() as tmp:
            self._write_session(tmp, [
                {"role": "user", "content": "x", "timestamp": "2026-08-04T10:00:00"},
            ])
            sm = SessionManager(tmp)
            self.assertIsNone(find_last_active_date(sm, "2026-08-04", date(2026, 8, 5)))
            self.assertIsNone(find_last_active_date(sm, "2026-08-05", date(2026, 8, 5)))
            self.assertIsNone(find_last_active_date(sm, "2026-08-06", date(2026, 8, 5)))

    def test_finds_latest_active_date_after_last(self):
        # 多天有消息：返回 last 之后最近的日期（只补一天）
        with tempfile.TemporaryDirectory() as tmp:
            self._write_session(tmp, [
                {"role": "user", "content": "x", "timestamp": "2026-08-09T10:00:00"},
                {"role": "user", "content": "y", "timestamp": "2026-08-11T10:00:00"},
            ])
            sm = SessionManager(tmp)
            self.assertEqual(find_last_active_date(sm, "2026-08-08", date(2026, 8, 13)), "2026-08-11")
            self.assertEqual(find_last_active_date(sm, None, date(2026, 8, 13)), "2026-08-11")

    def test_today_messages_do_not_count(self):
        # 今天有消息不算：补做目标是今天之前的最后有消息日期
        with tempfile.TemporaryDirectory() as tmp:
            self._write_session(tmp, [
                {"role": "user", "content": "昨天", "timestamp": "2026-08-04T10:00:00"},
                {"role": "user", "content": "今天", "timestamp": "2026-08-05T10:00:00"},
            ])
            sm = SessionManager(tmp)
            self.assertEqual(find_last_active_date(sm, None, date(2026, 8, 5)), "2026-08-04")
            self.assertEqual(find_last_active_date(sm, "2026-08-04", date(2026, 8, 5)), None)

    def test_patch_snapshot_only_date_is_not_active(self):
        # 只有 memory_patch/snapshot 的日期视为无消息（不补做）
        with tempfile.TemporaryDirectory() as tmp:
            self._write_session(tmp, [
                {"role": "system", "content": "<memory_patch revision=\"1\">x</memory_patch>",
                 "timestamp": "2026-08-04T10:00:00"},
                {"role": "system", "content": "<memory_snapshot>y</memory_snapshot>",
                 "timestamp": "2026-08-04T11:00:00"},
            ])
            sm = SessionManager(tmp)
            self.assertIsNone(find_last_active_date(sm, None, date(2026, 8, 5)))


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
        """进程晚启动（已过 dream_time）：立即补跑一次（目标=昨天，由 consolidate 决定），随后睡到明天到点。"""
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

    async def test_multi_day_wake_triggers_wake_catch_up_once(self):
        """TASK-014：进程 8-9 起运行、睡眠到 8-13 唤醒（同一实例跨日>1）→
        唤醒分支额外调用一次 on_wake_catch_up，且只调一次。"""
        clock = FakeClock(datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc))
        consolidate_calls = []
        catch_up_calls = []

        async def consolidate():
            consolidate_calls.append(clock.now())

        async def catch_up():
            catch_up_calls.append(clock.now())

        scheduler = DreamScheduler(
            consolidate, on_wake_catch_up=catch_up,
            dream_time="02:00", timezone="UTC", clock=clock,
        )

        waits = []

        async def wait(event, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                # 8-9 03:00 起运行后机器睡眠，直接跳到 8-13 09:00 唤醒
                clock.advance(timedelta(days=4, hours=6))
            else:
                await scheduler.stop()

        scheduler._wait = wait
        await scheduler.run()

        self.assertEqual(len(consolidate_calls), 2)   # 8-9 首轮 + 8-13 唤醒轮
        self.assertEqual(len(catch_up_calls), 1)      # 唤醒补做恰好一次
        self.assertEqual(
            catch_up_calls[0], datetime(2026, 8, 13, 9, 0, tzinfo=timezone.utc))

    async def test_daily_cross_day_does_not_trigger_wake_catch_up(self):
        """TASK-014：正常每日连续运行（跨日==1）不触发唤醒补做，仍只整理昨天。"""
        clock = FakeClock(datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc))
        consolidate_calls = []
        catch_up_calls = []

        async def consolidate():
            consolidate_calls.append(clock.now())

        async def catch_up():
            catch_up_calls.append(clock.now())

        scheduler = DreamScheduler(
            consolidate, on_wake_catch_up=catch_up,
            dream_time="02:00", timezone="UTC", clock=clock,
        )

        waits = []

        async def wait(event, timeout):
            waits.append(timeout)
            clock.advance(timedelta(seconds=timeout))
            if len(waits) >= 2:
                await scheduler.stop()

        scheduler._wait = wait
        await scheduler.run()

        self.assertEqual(len(consolidate_calls), 2)   # 8-9 补跑 + 8-10 到点
        self.assertEqual(catch_up_calls, [])          # 跨日==1 不触发补做

    async def test_wake_catch_up_error_does_not_kill_loop(self):
        """TASK-014：唤醒补做回调抛异常被静默吞掉，不破坏调度循环（同 consolidate）。"""
        clock = FakeClock(datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc))
        consolidate_calls = []

        async def consolidate():
            consolidate_calls.append(clock.now())

        async def catch_up():
            raise RuntimeError("boom")

        scheduler = DreamScheduler(
            consolidate, on_wake_catch_up=catch_up,
            dream_time="02:00", timezone="UTC", clock=clock,
        )

        waits = []

        async def wait(event, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                clock.advance(timedelta(days=4, hours=6))
            else:
                await scheduler.stop()

        scheduler._wait = wait
        await scheduler.run()  # 不应抛出

        self.assertEqual(len(consolidate_calls), 2)   # 循环存活，唤醒轮仍完成

    async def test_multi_day_wake_without_catch_up_injection_unchanged(self):
        """TASK-014 向后兼容：未注入 on_wake_catch_up（默认 None）时，
        多日唤醒行为与旧版完全一致（不额外补做）。"""
        clock = FakeClock(datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc))
        consolidate_calls = []

        async def consolidate():
            consolidate_calls.append(clock.now())

        scheduler = DreamScheduler(
            consolidate, dream_time="02:00", timezone="UTC", clock=clock,
        )  # 不传 on_wake_catch_up

        waits = []

        async def wait(event, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                clock.advance(timedelta(days=4, hours=6))
            else:
                await scheduler.stop()

        scheduler._wait = wait
        await scheduler.run()

        self.assertEqual(len(consolidate_calls), 2)   # 8-9 + 8-13 唤醒轮，无额外补做

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

    async def test_no_messages_does_not_advance_state(self):
        """该日期无会话消息：不调模型、不推进 last_dream_date（无消息日期不做、不推进）。"""
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = os.path.join(tmp, "memory")
            os.makedirs(memory_dir)
            daily = DailyMemory(memory_dir)
            provider = _DreamProvider()
            state = DreamState(memory_dir)
            sm = SessionManager(os.path.join(tmp, "sessions"))

            await run_dream_for_date(provider, daily, sm, state, "2026-08-04")

            self.assertEqual(provider.requests, [])
            self.assertIsNone(state.read_last_dream_date())

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




# —— TASK-013 验收：定时整理昨天 / 补做最后有消息日期 / 晚启动竞态 / 无消息不推进 ——

class DreamPipelineTests(unittest.IsolatedAsyncioTestCase):
    """用 build_dream_components 组装完整做梦管线（注入假时钟），验证 TASK-013 语义。

    覆盖验收标准：定时到点整理昨天、连续每日不重不漏、停机多天补最后有消息日期、
    晚启动竞态修复（含旧代码「last=当天」遗留状态）、无消息不推进、last 只前进不后退。
    """

    def _make(self, tmp, clock, records=None):
        memory_dir = os.path.join(tmp, "memory")
        sessions_dir = os.path.join(tmp, "sessions")
        os.makedirs(memory_dir)
        if records:
            _write_session_file(sessions_dir, records)
        daily = DailyMemory(memory_dir)
        provider = _DreamProvider()
        state = DreamState(memory_dir)
        sm = SessionManager(sessions_dir)
        comps = build_dream_components(
            provider, daily, sm, state, timezone="UTC", clock=clock
        )
        return comps, provider, daily, state

    async def test_scheduled_targets_yesterday(self):
        """定时到点语义：8-10 02:00 的 consolidate_today 整理「2026-08-09」（昨天全天）。"""
        clock = FakeClock(datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as tmp:
            comps, provider, daily, state = self._make(tmp, clock, records=[
                {"role": "user", "content": "8-9 白天的重要消息", "timestamp": "2026-08-09T10:00:00"},
                {"role": "user", "content": "8-10 凌晨的消息不归昨天", "timestamp": "2026-08-10T00:30:00"},
            ])
            await comps.consolidate_today()
            content = daily.read("2026-08-09")
            self.assertIn("# 2026-08-09", content)          # 整理的是 8-9
            self.assertIn("- 用户偏好中文回复", content)      # _DreamProvider 固定输出落盘
            self.assertEqual(daily.read("2026-08-10"), "")  # 8-10（当天）不被整理
            self.assertEqual(state.read_last_dream_date(), "2026-08-09")
            self.assertEqual(len(provider.requests), 1)   # 8-9 有消息 → 模型被调用一次

    async def test_consecutive_days_no_skip_no_dup(self):
        """正常每日连续运行：8-10 02:00 整理 8-9、8-11 02:00 整理 8-10（不重不漏）。"""
        clock = FakeClock(datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as tmp:
            comps, provider, daily, state = self._make(tmp, clock, records=[
                {"role": "user", "content": "8-9 的事", "timestamp": "2026-08-09T12:00:00"},
                {"role": "user", "content": "8-10 的事", "timestamp": "2026-08-10T12:00:00"},
            ])
            scheduler = comps.scheduler

            async def wait(event, timeout):
                clock.advance(timedelta(seconds=timeout))
                if daily.read("2026-08-10"):
                    await scheduler.stop()

            scheduler._wait = wait
            await scheduler.run()

            self.assertIn("- 用户偏好中文回复", daily.read("2026-08-09"))
            self.assertIn("- 用户偏好中文回复", daily.read("2026-08-10"))
            self.assertEqual(state.read_last_dream_date(), "2026-08-10")

    async def test_stale_catch_up_targets_last_active_date(self):
        """停机多天：8-9 有消息、8-10/11/12 无消息 → 8-13 启动 → 补做 2026-08-09（不是 8-12）。"""
        clock = FakeClock(datetime(2026, 8, 13, 9, 25, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as tmp:
            comps, provider, daily, state = self._make(tmp, clock, records=[
                {"role": "user", "content": "8-9 的消息", "timestamp": "2026-08-09T10:00:00"},
            ])
            await comps.catch_up_yesterday()
            self.assertIn("# 2026-08-09", daily.read("2026-08-09"))
            self.assertEqual(daily.read("2026-08-12"), "")
            self.assertEqual(state.read_last_dream_date(), "2026-08-09")  # 只推进到 8-9

    async def test_late_start_recovers_yesterday_not_today(self):
        """晚启动竞态修复：进程 8-6 09:25 启动（last=08-05、08-05 有消息未整理）→
        补做 08-05 成功，不被「当天补跑」顶掉（last 不推进到 8-06）。"""
        clock = FakeClock(datetime(2026, 8, 6, 9, 25, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = os.path.join(tmp, "memory")
            sessions_dir = os.path.join(tmp, "sessions")
            os.makedirs(memory_dir)
            _write_session_file(sessions_dir, [
                {"role": "user", "content": "8-5 白天没被整理的消息", "timestamp": "2026-08-05T14:00:00"},
                {"role": "user", "content": "8-6 今天的消息", "timestamp": "2026-08-06T09:00:00"},
            ])
            daily = DailyMemory(memory_dir)
            provider = _DreamProvider()
            state = DreamState(memory_dir)
            state.write_last_dream_date("2026-08-05")  # 旧代码留下的状态：last=8-5 但 8-5 全天未整理
            sm = SessionManager(sessions_dir)
            comps = build_dream_components(provider, daily, sm, state, timezone="UTC", clock=clock)

            # 模拟 build_shared 的完整启动：定时调度（晚启动立即补跑）与启动补做并发执行
            scheduler_task = asyncio.create_task(comps.scheduler.run())
            catch_up_task = asyncio.create_task(comps.catch_up_yesterday())
            await asyncio.sleep(0.1)
            await comps.scheduler.stop()
            await asyncio.gather(catch_up_task, scheduler_task, return_exceptions=True)

            self.assertIn("- 用户偏好中文回复", daily.read("2026-08-05"))  # 8-5 被补做
            self.assertEqual(daily.read("2026-08-06"), "")                # 当天不被整理
            self.assertEqual(state.read_last_dream_date(), "2026-08-05")  # last 未被顶到 8-6

    async def test_legacy_last_marked_today_still_recovers_yesterday(self):
        """旧代码遗留：last 被「当天补跑」推进到当天（last=8-10、今天=8-10）时，
        定时补跑仍整理昨天 8-9，不被状态误导跳过。"""
        clock = FakeClock(datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = os.path.join(tmp, "memory")
            sessions_dir = os.path.join(tmp, "sessions")
            os.makedirs(memory_dir)
            _write_session_file(sessions_dir, [
                {"role": "user", "content": "8-9 的消息", "timestamp": "2026-08-09T12:00:00"},
            ])
            daily = DailyMemory(memory_dir)
            provider = _DreamProvider()
            state = DreamState(memory_dir)
            state.write_last_dream_date("2026-08-10")  # 旧代码「当天补跑」把 last 推到当天
            sm = SessionManager(sessions_dir)
            comps = build_dream_components(provider, daily, sm, state, timezone="UTC", clock=clock)

            await comps.consolidate_today()

            self.assertIn("# 2026-08-09", daily.read("2026-08-09"))
            self.assertEqual(state.read_last_dream_date(), "2026-08-10")  # 单调：不回退

    async def test_no_messages_does_not_advance_in_pipeline(self):
        """无消息日期（昨天空转）：定时到点不调模型、不推进 last_dream_date。"""
        clock = FakeClock(datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as tmp:
            comps, provider, daily, state = self._make(tmp, clock)  # 无任何会话消息
            await comps.consolidate_today()
            self.assertEqual(provider.requests, [])
            self.assertIsNone(state.read_last_dream_date())


    async def test_late_start_no_double_consolidation(self):
        """晚启动且 last<昨天、昨天有消息：调度器补跑与启动补做指向同一目标，
        经本进程去重只整理一次（模型只被调用一次、不重复写盘）。"""
        clock = FakeClock(datetime(2026, 8, 6, 9, 25, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = os.path.join(tmp, "memory")
            sessions_dir = os.path.join(tmp, "sessions")
            os.makedirs(memory_dir)
            _write_session_file(sessions_dir, [
                {"role": "user", "content": "8-5 的消息", "timestamp": "2026-08-05T12:00:00"},
            ])
            daily = DailyMemory(memory_dir)
            provider = _DreamProvider()
            state = DreamState(memory_dir)
            state.write_last_dream_date("2026-08-04")  # last < 昨天：两者都会指向 8-5
            sm = SessionManager(sessions_dir)
            comps = build_dream_components(provider, daily, sm, state, timezone="UTC", clock=clock)

            scheduler_task = asyncio.create_task(comps.scheduler.run())
            catch_up_task = asyncio.create_task(comps.catch_up_yesterday())
            await asyncio.sleep(0.1)
            await comps.scheduler.stop()
            await asyncio.gather(catch_up_task, scheduler_task, return_exceptions=True)

            self.assertEqual(len(provider.requests), 1)  # 只整理一次
            self.assertIn("- 用户偏好中文回复", daily.read("2026-08-05"))
            self.assertEqual(state.read_last_dream_date(), "2026-08-05")

    async def test_wake_after_multi_day_sleep_catches_up_last_active(self):
        """TASK-014 验收：进程 8-9 起运行（last=8-8）、睡眠跨到 8-13 唤醒 →
        除整理昨天（8-12 无消息 no-op）外，补做「最后一个有消息的日期」8-9
        （不是 8-12），无消息日期不推进 last。"""
        clock = FakeClock(datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = os.path.join(tmp, "memory")
            sessions_dir = os.path.join(tmp, "sessions")
            os.makedirs(memory_dir)
            _write_session_file(sessions_dir, [
                {"role": "user", "content": "8-9 的消息", "timestamp": "2026-08-09T10:00:00"},
            ])
            daily = DailyMemory(memory_dir)
            provider = _DreamProvider()
            state = DreamState(memory_dir)
            state.write_last_dream_date("2026-08-08")  # 8-9 02:00 已整理完 8-8
            sm = SessionManager(sessions_dir)
            comps = build_dream_components(provider, daily, sm, state, timezone="UTC", clock=clock)
            scheduler = comps.scheduler

            waits = []

            async def wait(event, timeout):
                waits.append(timeout)
                if len(waits) == 1:
                    # 8-9 03:00 之后机器睡眠，直接跳到 8-13 09:00 唤醒
                    clock.advance(timedelta(days=4, hours=6))
                else:
                    await scheduler.stop()

            scheduler._wait = wait
            await scheduler.run()

            self.assertIn("# 2026-08-09", daily.read("2026-08-09"))  # 补做的是 8-9
            self.assertEqual(daily.read("2026-08-12"), "")           # 昨天 8-12 无消息不写
            self.assertEqual(state.read_last_dream_date(), "2026-08-09")  # 只推进到 8-9
            self.assertEqual(len(provider.requests), 1)              # 只调一次模型（8-9）

    async def test_wake_catch_up_shares_dedup_with_startup(self):
        """TASK-014 验收：唤醒补做与启动补做共用去重（_done_this_run + 锁）——
        启动已补做的日期，唤醒补做不会重复调模型。"""
        clock = FakeClock(datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = os.path.join(tmp, "memory")
            sessions_dir = os.path.join(tmp, "sessions")
            os.makedirs(memory_dir)
            _write_session_file(sessions_dir, [
                {"role": "user", "content": "8-8 的消息", "timestamp": "2026-08-08T10:00:00"},
            ])
            daily = DailyMemory(memory_dir)
            provider = _DreamProvider()
            state = DreamState(memory_dir)
            state.write_last_dream_date("2026-08-07")
            sm = SessionManager(sessions_dir)
            comps = build_dream_components(provider, daily, sm, state, timezone="UTC", clock=clock)
            scheduler = comps.scheduler

            # 启动补做：8-9 启动时补做「最后有消息日期」= 8-8（模型调用 1 次）
            await comps.catch_up_yesterday()
            self.assertEqual(len(provider.requests), 1)

            # 睡眠到 8-13 唤醒：唤醒补做仍指向 8-8，但已被 _done_this_run 去重，不再调模型
            waits = []

            async def wait(event, timeout):
                waits.append(timeout)
                if len(waits) == 1:
                    clock.advance(timedelta(days=4, hours=6))  # 8-9 03:00 → 8-13 09:00
                else:
                    await scheduler.stop()

            scheduler._wait = wait
            await scheduler.run()

            self.assertEqual(len(provider.requests), 1)  # 不重复调模型
            self.assertEqual(state.read_last_dream_date(), "2026-08-08")

    async def test_state_never_regresses_in_pipeline(self):
        """last 只前进不后退：last=8-06 时补做 8-05 不回退状态（补做内容仍写盘）。"""
        clock = FakeClock(datetime(2026, 8, 6, 9, 25, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = os.path.join(tmp, "memory")
            sessions_dir = os.path.join(tmp, "sessions")
            os.makedirs(memory_dir)
            _write_session_file(sessions_dir, [
                {"role": "user", "content": "8-5 的消息", "timestamp": "2026-08-05T10:00:00"},
            ])
            daily = DailyMemory(memory_dir)
            provider = _DreamProvider()
            state = DreamState(memory_dir)
            state.write_last_dream_date("2026-08-06")  # 更晚的整理记录
            sm = SessionManager(sessions_dir)
            comps = build_dream_components(provider, daily, sm, state, timezone="UTC", clock=clock)

            await comps.consolidate_today()  # 昨天=8-5，但 last 已是 8-6

            self.assertIn("- 用户偏好中文回复", daily.read("2026-08-05"))  # 内容仍补做
            self.assertEqual(state.read_last_dream_date(), "2026-08-06")   # 状态不回退


if __name__ == "__main__":
    unittest.main()
