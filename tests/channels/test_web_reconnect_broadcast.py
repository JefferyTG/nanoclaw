"""TASK-046 webui 断线重连续流：事件按会话广播 + 回包转发 + 连接绑定回归测试。

覆盖：
- _bind_conn_to_key / _unbind_conn：连接绑定到会话集合、断开解绑、空集合清理；
- stream_event 带 session_key：广播给该会话所有存活连接（断线重连的新连接
  因此能续上事件流）；
- stream_event 不带 session_key 或集合查不到时：退回按 conn_id 单发；
- send：发起连接断开时按 session_key 转发给接管连接。
"""

import asyncio
import unittest

from bus.queue import MessageBus
from channels.web import WebChannel


class FakeBus:
    async def publish_inbound(self, message):
        pass


def make_channel():
    ch = WebChannel("web", FakeBus(), "127.0.0.1", 0, None, "config.json")
    ch._web_loop = None  # 测试里按需设置
    return ch


class ConnBindTests(unittest.TestCase):
    def test_bind_adds_to_set(self):
        ch = make_channel()
        ch._bind_conn_to_key("c1", "k0")
        self.assertEqual(ch._key_conns, {"k0": {"c1"}})

    def test_bind_moves_between_keys(self):
        ch = make_channel()
        ch._bind_conn_to_key("c1", "k0")
        ch._bind_conn_to_key("c1", "k1")
        self.assertEqual(ch._key_conns, {"k1": {"c1"}})  # 旧集合已移除

    def test_bind_multiple_conns_same_key(self):
        ch = make_channel()
        ch._bind_conn_to_key("c1", "k0")
        ch._bind_conn_to_key("c2", "k0")
        self.assertEqual(ch._key_conns, {"k0": {"c1", "c2"}})

    def test_unbind_removes_and_cleans_empty(self):
        ch = make_channel()
        ch._bind_conn_to_key("c1", "k0")
        ch._bind_conn_to_key("c2", "k0")
        ch._unbind_conn("c1")
        self.assertEqual(ch._key_conns, {"k0": {"c2"}})
        ch._unbind_conn("c2")
        self.assertEqual(ch._key_conns, {})


class StreamEventBroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def test_broadcast_to_all_conns_of_session(self):
        ch = make_channel()
        ch._web_loop = asyncio.get_running_loop()
        calls = []

        async def fake_send(ws, event):
            calls.append((id(ws), event))

        ch._ws_send_json = fake_send
        ws1, ws2 = object(), object()
        ch._conns["c1"] = ws1
        ch._conns["c2"] = ws2
        ch._key_conns["k0"] = {"c1", "c2"}
        await ch.stream_event("c1", {"type": "token", "content": "x"}, session_key="k0")
        # 广播给集合内两个存活连接
        self.assertEqual(len(calls), 2)
        self.assertEqual({cid for cid, _ in calls}, {id(ws1), id(ws2)})

    async def test_broadcast_only_to_alive_conns(self):
        ch = make_channel()
        ch._web_loop = asyncio.get_running_loop()
        calls = []

        async def fake_send(ws, event):
            calls.append(id(ws))

        ch._ws_send_json = fake_send
        ws_live = object()
        ch._conns["c_live"] = ws_live
        # c_dead 在集合里但不在 _conns（已断开）
        ch._key_conns["k0"] = {"c_dead", "c_live"}
        await ch.stream_event("c_dead", {"type": "token", "content": "x"}, session_key="k0")
        self.assertEqual(calls, [id(ws_live)])

    async def test_reconnected_conn_takes_over_stream(self):
        """断线重连核心：旧连接 c1 断开、新连接 c2 open 同一会话后，
        后续事件带 session_key 广播给 c2（续上事件流）。"""
        ch = make_channel()
        ch._web_loop = asyncio.get_running_loop()
        calls = []

        async def fake_send(ws, event):
            calls.append(id(ws))

        ch._ws_send_json = fake_send
        # 旧连接曾绑定 k0，后断开（_conns 移除 + _unbind_conn）
        ch._bind_conn_to_key("c1", "k0")
        ch._conns["c1"] = object()
        ch._unbind_conn("c1")
        # 新连接重连并 open 同一会话
        ws2 = object()
        ch._conns["c2"] = ws2
        ch._bind_conn_to_key("c2", "k0")
        # 事件仍以旧 chat_id 投递，但带 session_key → 应到达 c2
        await ch.stream_event("c1", {"type": "done", "content": "完成"}, session_key="k0")
        self.assertEqual(calls, [id(ws2)])

    async def test_no_session_key_falls_back_to_conn(self):
        ch = make_channel()
        ch._web_loop = asyncio.get_running_loop()
        calls = []

        async def fake_send(ws, event):
            calls.append(id(ws))

        ch._ws_send_json = fake_send
        ws1 = object()
        ch._conns["c1"] = ws1
        # 不带 session_key：退回按 conn_id 单发
        await ch.stream_event("c1", {"type": "token", "content": "x"})
        self.assertEqual(calls, [id(ws1)])

    async def test_session_key_unknown_falls_back_to_conn(self):
        ch = make_channel()
        ch._web_loop = asyncio.get_running_loop()
        calls = []

        async def fake_send(ws, event):
            calls.append(id(ws))

        ch._ws_send_json = fake_send
        ws1 = object()
        ch._conns["c1"] = ws1
        # 集合里没有该 conn_id 的会话：退回单发（兼容初始会话未绑定场景）
        await ch.stream_event("c1", {"type": "token", "content": "x"}, session_key="k_none")
        self.assertEqual(calls, [id(ws1)])

    async def test_dead_conn_without_key_drops(self):
        ch = make_channel()
        ch._web_loop = asyncio.get_running_loop()
        calls = []

        async def fake_send(ws, event):
            calls.append(id(ws))

        ch._ws_send_json = fake_send
        # 无 session_key 且 conn 已断开：丢弃
        await ch.stream_event("c_gone", {"type": "token", "content": "x"})
        self.assertEqual(calls, [])


class SendForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_forwards_to_takeover_conn(self):
        """断线重连后，非流式回包按 session_key 转发给接管连接。"""
        ch = make_channel()
        ch._web_loop = asyncio.get_running_loop()
        calls = []

        async def fake_send(ws, content):
            calls.append((id(ws), content))

        ch._ws_send = fake_send
        ws2 = object()
        ch._conns["c2"] = ws2
        ch._key_conns["k0"] = {"c2"}

        from bus.queue import OutboundMessage

        msg = OutboundMessage(
            channel="web",
            chat_id="c1",            # 旧连接已断（不在 _conns）
            content="回包内容",
            session_key="k0",
        )
        await ch.send(msg)
        self.assertEqual(calls, [(id(ws2), "回包内容")])

    async def test_send_to_original_conn_when_alive(self):
        ch = make_channel()
        ch._web_loop = asyncio.get_running_loop()
        calls = []

        async def fake_send(ws, content):
            calls.append((id(ws), content))

        ch._ws_send = fake_send
        ws1 = object()
        ch._conns["c1"] = ws1

        from bus.queue import OutboundMessage

        msg = OutboundMessage(channel="web", chat_id="c1", content="hi")
        await ch.send(msg)
        self.assertEqual(calls, [(id(ws1), "hi")])

    async def test_streamed_message_skipped(self):
        ch = make_channel()
        calls = []

        async def fake_send(ws, content):
            calls.append(content)

        ch._ws_send = fake_send

        from bus.queue import OutboundMessage

        msg = OutboundMessage(channel="web", chat_id="c1", content="已流式覆盖", streamed=True)
        await ch.send(msg)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
