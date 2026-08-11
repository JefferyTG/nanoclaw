"""RealtimeS2SClient 单测：fake WebSocket，不真连云端（TASK-037）。

覆盖：连接与会话创建、事件收发、优雅关闭（session.close → session.closed →
断 ws）、关闭超时兜底、5xx 重连、4xx 直接失败、未连接时发送报错。
"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from voice.realtime_s2s.client import DEFAULT_MODEL_VERSION, DEFAULT_VOICE, RealtimeS2SClient


class _StatusError(RuntimeError):
    """带 HTTP 状态码的假连接异常（模拟 websockets InvalidStatus）。"""

    def __init__(self, status: int):
        super().__init__(f"http {status}")
        self.status_code = status


class _FakeWS:
    """记录客户端发送、按脚本回放服务端事件的假 WebSocket。"""

    def __init__(self, script=()):
        self._script = list(script)
        self.sent: list[str] = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._script:
            raise StopAsyncIteration
        item = self._script.pop(0)
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True


def _factory(script=()):
    """返回 (ws_factory, ws, calls)；calls 记录连接参数（url/headers）。

    ws_factory 为 async 工厂（与 client._open_with_retry 的 await 调用一致）。
    """
    ws = _FakeWS(script)
    calls = {}

    async def factory(url, headers):
        calls["url"] = url
        calls["headers"] = headers
        return ws

    return factory, ws, calls


class RealtimeClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_sends_api_key_header(self):
        factory, ws, calls = _factory([{"type": "session.created"}])
        client = RealtimeS2SClient("test-key", ws_factory=factory)
        await client.connect()
        self.assertEqual(calls["headers"], {"X-Api-Key": "test-key"})
        self.assertIn("openspeech.bytedance.com", calls["url"])
        await client.disconnect()

    async def test_connect_disables_websocket_ping_like_official_demo(self):
        ws = _FakeWS()
        connect = AsyncMock(return_value=ws)
        with patch("voice.realtime_s2s.client.websockets.connect", connect):
            client = RealtimeS2SClient("test-key")
            await client.connect()
            self.assertIsNone(connect.await_args.kwargs["ping_interval"])
            await client.disconnect()

    async def test_create_session_sends_correct_payload(self):
        factory, ws, _ = _factory([{"type": "session.created"}])
        client = RealtimeS2SClient("test-key", ws_factory=factory)
        await client.connect()
        event = await client.create_session(
            instructions="你是小奈",
            voice_type="zh_female_vv_jupiter_bigtts",
            tools=[],
            enable_websearch=False,
        )
        self.assertEqual(event["type"], "session.created")
        first = json.loads(ws.sent[0])
        self.assertEqual(first["type"], "session.create")
        self.assertTrue(first["event_id"].startswith("evt_"))
        session = first["session"]
        self.assertEqual(session["model"], DEFAULT_MODEL_VERSION)  # 固定字符串版本号
        self.assertIn("id", session)  # 客户端会话 id（官方 demo 携带）
        self.assertEqual(
            session["audio"]["input"]["format"], {"type": "pcm", "rate": 16000}
        )
        self.assertEqual(
            session["audio"]["output"]["format"],
            {"type": "pcm_s16le", "rate": 24000},
        )
        self.assertEqual(session["audio"]["output"]["voice"], DEFAULT_VOICE)
        self.assertEqual(session["instructions"], "你是小奈")
        self.assertEqual(session["tools"], [])
        # extension 在事件顶层（asr/tts/dialog 三块透传），websearch 默认关
        ext = first.get("extension") or {}
        self.assertEqual(set(ext), {"asr", "tts", "dialog"})
        # TASK-038：dialog.extra 对齐官方 py demo（enable_loudness_norm 等）
        self.assertEqual(
            ext["dialog"]["extra"],
            {
                "enable_loudness_norm": True,
                "enable_music": False,
                "audit_response": "抱歉，这个问题我无法回答，你可以换个其他话题，我会尽力为你提供帮助。",
            },
        )
        await client.disconnect()

    async def test_iter_events_yields_server_events_in_order(self):
        factory, ws, _ = _factory(
            [
                {"type": "response.start", "response": {"id": "r1"}},
                {"type": "output_audio.delta", "response_id": "r1", "audio": "AQID"},
                {"type": "response.done", "response": {"id": "r1"}},
            ]
        )
        client = RealtimeS2SClient("k", ws_factory=factory)
        await client.connect()
        types = [
            e["type"]
            async for e in client.iter_events(poll_interval=0.05)
            if e is not None  # 过滤轮询心跳 None
        ]
        self.assertEqual(types, ["response.start", "output_audio.delta", "response.done"])
        await client.disconnect()

    async def test_close_session_sends_close_then_waits_closed(self):
        factory, ws, _ = _factory(
            [{"type": "session.created"}, {"type": "session.closed"}]
        )
        client = RealtimeS2SClient("k", ws_factory=factory)
        await client.connect()
        await client.create_session()
        await client.close_session()
        sent_types = [json.loads(s)["type"] for s in ws.sent]
        self.assertEqual(sent_types[-1], "session.close")
        self.assertTrue(json.loads(ws.sent[-1])["event_id"].startswith("evt_"))
        self.assertTrue(ws.closed)

    async def test_close_session_disconnects_on_timeout(self):
        # 服务端不回 session.closed → close_timeout_sec 后照常断开兜底
        factory, ws, _ = _factory([{"type": "session.created"}])
        client = RealtimeS2SClient("k", ws_factory=factory, close_timeout_sec=0.05)
        await client.connect()
        await client.create_session()
        await client.close_session()
        self.assertTrue(ws.closed)

    async def test_connect_retries_on_5xx_then_succeeds(self):
        attempts = {"n": 0}

        async def factory(url, headers):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise _StatusError(503)
            return _FakeWS([{"type": "session.created"}])

        client = RealtimeS2SClient("k", ws_factory=factory, max_reconnects=2)
        await client.connect()
        self.assertEqual(attempts["n"], 3)
        await client.disconnect()

    async def test_connect_fails_immediately_on_4xx(self):
        async def factory(url, headers):
            raise _StatusError(401)

        client = RealtimeS2SClient("k", ws_factory=factory, max_reconnects=2)
        with self.assertRaises(RuntimeError):
            await client.connect()

    async def test_send_event_before_connect_raises(self):
        client = RealtimeS2SClient("k")
        with self.assertRaises(ConnectionError):
            await client.send_event({"type": "session.create", "session": {}})
