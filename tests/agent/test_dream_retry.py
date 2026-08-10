"""TASK-015 定时整理失败当日自动重试测试。

覆盖验收点：
- 失败（consolidate_today 返回 False）后在当日稍后重试（间隔可注入/可控次数），
  重试成功则 last_dream_date 才推进、daily 才写盘；
- 全部失败当日放弃（重试次数上限），last_dream_date 不提前推进；
- 重试等待期间跨日则放弃本轮重试（明天到点/下次启动兜底）；
- 旧式回调（返回 None）视为成功，不触发重试（向后兼容）；
- 与 _done_this_run + 串行锁去重配合：失败日期不入去重集合、可重试；
  成功后入集合，不重复整理。
"""

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from agent.daily import DailyMemory
from main import (
    DreamScheduler,
    DreamState,
    build_dream_components,
    run_dream_for_date,
)
from providers.base import LLMResponse
from session.manager import SessionManager


class FakeClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value

    def advance(self, delta):
        self.value += delta


class _ErrorProvider:
    async def chat(self, messages, tools=None, model=None, **kwargs):
        raise RuntimeError("boom")


class _DreamProvider:
    def __init__(self, content=None):
        self.content = content or json.dumps(
            {"会话总结": ["8-9 已整理"]}, ensure_ascii=False
        )

    async def chat(self, messages, tools=None, model=None, **kwargs):
        return LLMResponse(self.content)


def _write_session_file(sessions_dir, records):
    """写 cli_direct 会话并把 mtime 拨到消息日期之后（避开 mtime 剪枝）。"""
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


class DreamRetryTests(unittest.IsolatedAsyncioTestCase):
    def _make_scheduler(self, consolidate, clock, retries=3, interval=1800, **kw):
        return DreamScheduler(
            consolidate,
            dream_time="02:00",
            timezone="UTC",
            clock=clock,
            dream_max_retries=retries,
            dream_retry_interval=interval,
            **kw,
        )

    async def test_failure_then_success_retries_and_advances_state(self):
        """第一次失败 → 间隔后重试成功：只推进到真正完成的日期，不提前推进。"""
        clock = FakeClock(datetime(2026, 8, 5, 2, 0, tzinfo=timezone.utc))
        attempts = []

        async def consolidate():
            attempts.append(clock.now())
            return len(attempts) >= 2  # 第一次失败，第二次成功

        scheduler = self._make_scheduler(consolidate, clock)
        waits = []

        async def wait(event, timeout):
            waits.append(timeout)
            clock.advance(timedelta(seconds=timeout))
            if len(waits) >= 2:  # 重试间隔消费完后停（避免进入明天循环）
                await scheduler.stop()

        scheduler._wait = wait
        await scheduler.run()

        self.assertEqual(len(attempts), 2)
        # 第一次等待是重试间隔（1800s），不是「睡到明天到点」
        self.assertEqual(waits[0], 1800.0)
        # 重试成功发生在 02:30
        self.assertEqual(attempts[1], datetime(2026, 8, 5, 2, 30, tzinfo=timezone.utc))

    async def test_all_failures_give_up_after_max_retries(self):
        """全部失败：初始 1 次 + 3 次重试后当日放弃（不无限重试）。"""
        clock = FakeClock(datetime(2026, 8, 5, 2, 0, tzinfo=timezone.utc))
        attempts = []

        async def consolidate():
            attempts.append(clock.now())
            return False

        scheduler = self._make_scheduler(consolidate, clock, retries=3)
        waits = []

        async def wait(event, timeout):
            waits.append(timeout)
            clock.advance(timedelta(seconds=timeout))
            if len(waits) >= 4:
                await scheduler.stop()

        scheduler._wait = wait
        await scheduler.run()

        # 初始 1 次 + 3 次重试 = 4 次；重试间隔都是 1800s
        self.assertEqual(len(attempts), 4)
        self.assertEqual(waits[:3], [1800.0, 1800.0, 1800.0])

    async def test_cross_midnight_gives_up_retry(self):
        """重试等待期间跨日：放弃本轮重试（明天到点/下次启动兜底）。"""
        clock = FakeClock(datetime(2026, 8, 5, 2, 0, tzinfo=timezone.utc))
        attempts = []

        async def consolidate():
            attempts.append(clock.now())
            return False

        scheduler = self._make_scheduler(consolidate, clock, retries=3)
        waits = []

        async def wait(event, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                clock.advance(timedelta(days=1, hours=2))  # 重试等待跨到第二天
            else:
                clock.advance(timedelta(seconds=timeout))
                await scheduler.stop()

        scheduler._wait = wait
        await scheduler.run()

        # 8-5 初始 1 次 + 跨日放弃前的 1 次重试，之后不再为 8-5 重试
        # （8-6 是新一天到点/晚启动的独立 consolidate，不计入 8-5 重试预算）
        self.assertEqual(len(attempts), 2)

    async def test_none_return_is_success_no_retry(self):
        """旧式回调返回 None：视为成功，不触发重试（向后兼容）。"""
        clock = FakeClock(datetime(2026, 8, 5, 2, 0, tzinfo=timezone.utc))
        attempts = []

        async def consolidate():
            attempts.append(clock.now())
            return None

        scheduler = self._make_scheduler(consolidate, clock)
        waits = []

        async def wait(event, timeout):
            waits.append(timeout)
            clock.advance(timedelta(seconds=timeout))
            await scheduler.stop()

        scheduler._wait = wait
        await scheduler.run()

        self.assertEqual(len(attempts), 1)
        self.assertNotIn(1800.0, waits)  # 没有重试间隔等待

    async def test_run_dream_for_date_false_then_true_only_advances_on_success(self):
        """run_dream_for_date 契约：False 不推进 last；重试成功才推进。"""
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = os.path.join(tmp, "memory")
            sessions_dir = os.path.join(tmp, "sessions")
            os.makedirs(memory_dir)
            _write_session_file(sessions_dir, [
                {"role": "user", "content": "8-9 的事", "timestamp": "2026-08-09T12:00:00"},
            ])
            daily = DailyMemory(memory_dir)
            state = DreamState(memory_dir)
            sm = SessionManager(sessions_dir)

            self.assertFalse(await run_dream_for_date(
                _ErrorProvider(), daily, sm, state, "2026-08-09"
            ))
            self.assertIsNone(state.read_last_dream_date())  # 失败不推进

            self.assertTrue(await run_dream_for_date(
                _DreamProvider(), daily, sm, state, "2026-08-09"
            ))
            self.assertEqual(state.read_last_dream_date(), "2026-08-09")
            self.assertIn("- 8-9 已整理", daily.read("2026-08-09"))


class DreamRetryPipelineTests(unittest.IsolatedAsyncioTestCase):
    """用 build_dream_components 组装完整管线：失败→当日重试→成功推进。"""

    async def test_pipeline_failure_then_retry_success(self):
        clock = FakeClock(datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = os.path.join(tmp, "memory")
            sessions_dir = os.path.join(tmp, "sessions")
            os.makedirs(memory_dir)
            _write_session_file(sessions_dir, [
                {"role": "user", "content": "8-9 的事", "timestamp": "2026-08-09T12:00:00"},
            ])
            daily = DailyMemory(memory_dir)
            state = DreamState(memory_dir)
            sm = SessionManager(sessions_dir)

            class _FlakyProvider:
                def __init__(self):
                    self.calls = 0

                async def chat(self, messages, tools=None, model=None, **kwargs):
                    self.calls += 1
                    if self.calls == 1:
                        raise RuntimeError("boom")
                    return LLMResponse(json.dumps(
                        {"会话总结": ["8-9 已整理"]}, ensure_ascii=False
                    ))

            provider = _FlakyProvider()
            comps = build_dream_components(
                provider, daily, sm, state, timezone="UTC", clock=clock,
                dream_max_retries=3, dream_retry_interval=1800,
            )
            scheduler = comps.scheduler
            waits = []

            async def wait(event, timeout):
                waits.append(timeout)
                clock.advance(timedelta(seconds=timeout))
                if len(waits) >= 2:
                    await scheduler.stop()

            scheduler._wait = wait
            await scheduler.run()

            # 第一次失败（02:00）→ 30 分钟后重试成功（02:30）→ 推进 last 并写 daily
            self.assertEqual(provider.calls, 2)
            self.assertEqual(state.read_last_dream_date(), "2026-08-09")
            self.assertIn("- 8-9 已整理", daily.read("2026-08-09"))
            self.assertEqual(waits[0], 1800.0)  # 重试间隔

    async def test_pipeline_retry_respects_dedup_no_duplicate_model_calls(self):
        """重试成功入 _done_this_run 去重集合：后续触发不再重复调模型。"""
        clock = FakeClock(datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = os.path.join(tmp, "memory")
            sessions_dir = os.path.join(tmp, "sessions")
            os.makedirs(memory_dir)
            _write_session_file(sessions_dir, [
                {"role": "user", "content": "8-9 的事", "timestamp": "2026-08-09T12:00:00"},
            ])
            daily = DailyMemory(memory_dir)
            state = DreamState(memory_dir)
            sm = SessionManager(sessions_dir)

            class _FlakyProvider:
                def __init__(self):
                    self.calls = 0

                async def chat(self, messages, tools=None, model=None, **kwargs):
                    self.calls += 1
                    if self.calls == 1:
                        raise RuntimeError("boom")
                    return LLMResponse(json.dumps(
                        {"会话总结": ["8-9 已整理"]}, ensure_ascii=False
                    ))

            provider = _FlakyProvider()
            comps = build_dream_components(
                provider, daily, sm, state, timezone="UTC", clock=clock,
            )
            scheduler = comps.scheduler

            # 重试成功
            async def wait(event, timeout):
                clock.advance(timedelta(seconds=timeout))
                if provider.calls >= 2:
                    await scheduler.stop()

            scheduler._wait = wait
            await scheduler.run()
            self.assertEqual(provider.calls, 2)
            self.assertEqual(state.read_last_dream_date(), "2026-08-09")

            # 启动补做指向同一日期（8-9）→ _done_this_run 已含 → 不再调模型
            calls_before = provider.calls
            await comps.catch_up_yesterday()
            self.assertEqual(provider.calls, calls_before)


if __name__ == "__main__":
    unittest.main()
