"""RealtimeBridge / WebChannel._serve_realtime 单测（TASK-044，全 fake，不真连云端）。

覆盖：
- 上行转发：浏览器音频帧 → client.send_event(input_audio_buffer.append)；
- 下行转发：client iter_events 事件 → 浏览器 WS 推送；
- 生命周期顺序：connect → create_session → close_session；
- 优雅关闭：create_session 失败仍调用 close_session；
- 并发互斥：第二路 /api/realtime 被拒（不踢旧通话）；
- 配置透传：api_key 缺失拒绝、voice/model/enable_websearch 与人设进 create_session。
"""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from channels.web import RealtimeBridge, WebChannel

TEXT = 1


class FakeWS:
    """可迭代的假浏览器 WS：记录 send_str / close，支持按脚本喂上行消息。"""

    def __init__(self, incoming=(), *, auto_close=False):
        self.incoming = list(incoming)
        self.auto_close = auto_close
        self.sent = []
        self.closed = False
        self._i = 0

    async def send_str(self, text):
        self.sent.append(text)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i < len(self.incoming):
            msg = self.incoming[self._i]
            self._i += 1
            return msg
        if self.auto_close:
            raise StopAsyncIteration
        # 模拟浏览器保持连接：挂起直到被取消
        await asyncio.Event().wait()
        raise StopAsyncIteration  # pragma: no cover - 永不到达（取消会先抛出）


class FakeClient:
    """假 RealtimeS2SClient：记录方法调用与上行事件，按脚本回放下行事件。"""

    def __init__(self, script=(), *, block_after_script=True):
        self.script = list(script)
        self.block_after_script = block_after_script
        self.calls = []
        self.sent = []
        self.session_kw = None

    async def connect(self):
        self.calls.append("connect")

    async def create_session(self, **kwargs):
        self.calls.append("create_session")
        self.session_kw = kwargs
        return {"type": "session.created"}

    async def send_event(self, event):
        self.sent.append(event)

    async def iter_events(self):
        for ev in self.script:
            yield ev
        if self.block_after_script:
            await asyncio.Event().wait()

    async def close_session(self):
        self.calls.append("close_session")


def _msg(obj):
    return SimpleNamespace(type=TEXT, data=json.dumps(obj))


class RealtimeBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_uplink_forwards_audio_and_lifecycle_order(self):
        client = FakeClient()
        ws = FakeWS(incoming=[
            _msg({"type": "input_audio_buffer.append", "audio": "AAAA"}),
            _msg({"type": "hangup"}),
        ])
        bridge = RealtimeBridge(
            client,
            instructions="persona",
            voice_type="S_ZUpBmlGb2",
            model="1.2.6.1",
            enable_websearch=True,
        )
        await bridge.run(ws)

        self.assertEqual(
            client.sent,
            [{"type": "input_audio_buffer.append", "audio": "AAAA"}],
        )
        # 生命周期顺序：connect → create_session → close_session
        self.assertEqual(client.calls, ["connect", "create_session", "close_session"])
        self.assertEqual(client.session_kw["instructions"], "persona")
        self.assertEqual(client.session_kw["voice_type"], "S_ZUpBmlGb2")
        self.assertEqual(client.session_kw["model"], "1.2.6.1")
        self.assertTrue(client.session_kw["enable_websearch"])
        # session.create 完成后推了 realtime.ready
        types = [json.loads(t)["event"]["type"] for t in ws.sent]
        self.assertIn("realtime.ready", types)

    async def test_downlink_forwards_client_events_to_ws(self):
        client = FakeClient(
            script=[
                {"type": "response.output_audio.delta", "delta": "BBBB"},
                {"type": "response.done"},
            ],
            block_after_script=False,
        )
        ws = FakeWS(incoming=[])
        bridge = RealtimeBridge(client, instructions="p")

        await bridge.run(ws)

        events = [json.loads(t)["event"] for t in ws.sent]
        types = [e["type"] for e in events]
        self.assertIn("realtime.ready", types)
        self.assertIn("response.output_audio.delta", types)
        self.assertIn("response.done", types)
        delta = next(e for e in events if e["type"] == "response.output_audio.delta")
        self.assertEqual(delta["delta"], "BBBB")
        self.assertIn("close_session", client.calls)

    async def test_downlink_skips_heartbeat_none(self):
        client = FakeClient(
            script=[None, {"type": "response.done"}],
            block_after_script=False,
        )
        ws = FakeWS(incoming=[])
        await RealtimeBridge(client, instructions="p").run(ws)
        events = [json.loads(t)["event"] for t in ws.sent]
        self.assertTrue(all(e["type"] != "None" for e in events))
        self.assertIn("response.done", [e["type"] for e in events])

    async def test_close_session_called_even_if_create_fails(self):
        client = FakeClient()

        async def fail_create(**kwargs):
            client.calls.append("create_session")
            raise RuntimeError("boom")

        client.create_session = fail_create
        ws = FakeWS(incoming=[])
        bridge = RealtimeBridge(client, instructions="p")

        with self.assertRaises(RuntimeError):
            await bridge.run(ws)

        self.assertEqual(client.calls, ["connect", "create_session", "close_session"])


class WebChannelRealtimeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _channel(realtime, factory=None):
        config = SimpleNamespace(realtime=realtime)
        return WebChannel(
            "web", None, "127.0.0.1", 0, config, "config.json",
            realtime_client_factory=factory,
        )

    async def test_second_call_rejected_busy_not_kicking_old(self):
        ch = self._channel({"api_key": "k", "voice": "v", "model": "m"})
        old = object()
        ch._active_realtime = old  # 已有通话
        ws = FakeWS(incoming=[])

        result = await ch._serve_realtime(ws)

        self.assertIs(result, ws)
        self.assertTrue(ws.closed)
        self.assertEqual(ch._active_realtime, old)  # 不踢旧通话
        err = json.loads(ws.sent[0])["event"]
        self.assertEqual(err["type"], "realtime.error")
        self.assertEqual(err["code"], "busy")

    async def test_no_api_key_rejected(self):
        ch = self._channel({"api_key": "", "voice": "v", "model": "m"})
        ws = FakeWS(incoming=[])

        await ch._serve_realtime(ws)

        err = json.loads(ws.sent[0])["event"]
        self.assertEqual(err["code"], "no_api_key")
        self.assertTrue(ws.closed)

    async def test_serve_realtime_builds_bridge_from_config(self):
        clients = []

        def factory():
            client = FakeClient()
            clients.append(client)
            return client

        ch = self._channel(
            {
                "api_key": "k",
                "voice": "S_ZUpBmlGb2",
                "model": "1.2.6.1",
                "enable_websearch": True,
            },
            factory=factory,
        )
        ws = FakeWS(incoming=[_msg({"type": "hangup"})])

        with mock.patch.object(ch, "_load_realtime_identity", return_value="persona"):
            await ch._serve_realtime(ws)

        self.assertEqual(len(clients), 1)
        client = clients[0]
        self.assertEqual(client.session_kw["instructions"], "persona")
        self.assertEqual(client.session_kw["voice_type"], "S_ZUpBmlGb2")
        self.assertEqual(client.session_kw["model"], "1.2.6.1")
        self.assertTrue(client.session_kw["enable_websearch"])
        self.assertIsNone(ch._active_realtime)  # 结束后释放互斥位
        self.assertTrue(ws.closed)


if __name__ == "__main__":
    unittest.main()
