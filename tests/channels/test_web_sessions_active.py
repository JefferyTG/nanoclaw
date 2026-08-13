"""TASK-045 webui 会话活跃状态：/api/sessions 的 active/active_since 字段回归测试。

覆盖：
- WebChannel._handle_list_sessions 为每项补全 active + active_since；
- 注入的 _active_sessions_callback 返回 {session_key: ISO} 时，命中项 active=True、
  未命中项 active=False、active_since=null（JSON null）；
- 回调未注入 / 返回非 dict / 抛异常时，全部降级为 active=False（不拖垮列表）；
- Gateway.get_active_sessions 只返回未 done 的回合、已 done 的被过滤。
"""

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace

from bus.queue import MessageBus
from channels.web import WebChannel
from gateway import Gateway
from session.manager import SessionManager


class FakeBus:
    def __init__(self):
        self.inbound = []

    async def publish_inbound(self, message):
        self.inbound.append(message)


def make_channel_with_session(tmp: str, session_key: str = "web:conn:0"):
    """构造带真实 SessionManager（含一个会话）的 WebChannel。"""
    sessions = os.path.join(tmp, "sessions")
    os.makedirs(sessions, exist_ok=True)
    sm = SessionManager(sessions)
    sm.save_message(session_key, {"role": "user", "content": "帮我整理这份日志"})
    # config=None：不装配 FileStore，专注测试会话列表字段
    return WebChannel("web", FakeBus(), "127.0.0.1", 0, None, "config.json",
                      session_manager=sm)


class WebSessionsActiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_flag_true_for_running_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = make_channel_with_session(tmp)
            ch._active_sessions_callback = lambda: {"web:conn:0": "2026-08-13T12:00:00"}
            resp = await ch._handle_list_sessions(None)
            body = json.loads(resp.text)
            self.assertEqual(len(body["sessions"]), 1)
            it = body["sessions"][0]
            self.assertEqual(it["key"], "web:conn:0")
            self.assertTrue(it["active"])
            self.assertEqual(it["active_since"], "2026-08-13T12:00:00")

    async def test_active_false_when_not_in_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = make_channel_with_session(tmp)
            # 回调只回报另一个会话活跃，本会话应判为非活跃
            ch._active_sessions_callback = lambda: {"web:other:0": "2026-08-13T12:00:00"}
            resp = await ch._handle_list_sessions(None)
            body = json.loads(resp.text)
            it = body["sessions"][0]
            self.assertFalse(it["active"])
            self.assertIsNone(it["active_since"])

    async def test_no_callback_degrades_to_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = make_channel_with_session(tmp)  # 未注入回调
            resp = await ch._handle_list_sessions(None)
            body = json.loads(resp.text)
            it = body["sessions"][0]
            self.assertFalse(it["active"])
            self.assertIsNone(it["active_since"])

    async def test_callback_returns_non_dict_degrades(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = make_channel_with_session(tmp)
            ch._active_sessions_callback = lambda: "not-a-dict"
            resp = await ch._handle_list_sessions(None)
            body = json.loads(resp.text)
            it = body["sessions"][0]
            self.assertFalse(it["active"])
            self.assertIsNone(it["active_since"])

    async def test_callback_raises_degrades(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = make_channel_with_session(tmp)
            ch._active_sessions_callback = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
            # 异常不应冒泡，应降级为全非活跃
            resp = await ch._handle_list_sessions(None)
            body = json.loads(resp.text)
            it = body["sessions"][0]
            self.assertFalse(it["active"])
            self.assertIsNone(it["active_since"])

    async def test_session_manager_none_returns_empty(self):
        ch = WebChannel("web", FakeBus(), "127.0.0.1", 0, None, "config.json")
        resp = await ch._handle_list_sessions(None)
        body = json.loads(resp.text)
        self.assertEqual(body["sessions"], [])


class GatewayActiveSessionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_filters_done_tasks(self):
        gw = Gateway(MessageBus(), [], lambda k: None)
        # 一个很快完成的任务 + 一个持续运行的任务
        async def noop():
            return None

        done_task = asyncio.create_task(noop())
        live_task = asyncio.create_task(asyncio.sleep(10))
        gw._active_tasks["web:done"] = done_task
        gw._active_tasks["web:live"] = live_task
        gw._active_tasks_started["web:done"] = datetime(2026, 8, 13, 12, 0, 0)
        gw._active_tasks_started["web:live"] = datetime(2026, 8, 13, 12, 5, 0)
        await asyncio.sleep(0)  # 让 noop 跑完
        out = gw.get_active_sessions()
        self.assertNotIn("web:done", out)  # 已 done 被过滤
        self.assertIn("web:live", out)      # 仍在跑
        self.assertEqual(out["web:live"], "2026-08-13T12:05:00")
        live_task.cancel()

    async def test_skips_entries_without_start_time(self):
        gw = Gateway(MessageBus(), [], lambda k: None)
        live_task = asyncio.create_task(asyncio.sleep(10))
        gw._active_tasks["web:live"] = live_task
        # 未登记开始时间：应跳过，避免返回无意义的活跃项
        out = gw.get_active_sessions()
        self.assertNotIn("web:live", out)
        live_task.cancel()

    def test_empty_when_no_tasks(self):
        gw = Gateway(MessageBus(), [], lambda k: None)
        self.assertEqual(gw.get_active_sessions(), {})


if __name__ == "__main__":
    unittest.main()
