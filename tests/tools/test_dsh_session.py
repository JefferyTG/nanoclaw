"""dsh_session 工具单元测试。

mock HTTP 层（httpx.MockTransport），不发真实请求、不依赖 DSH 实例。
覆盖：RPC 信封构造、list 会话展示、prompt 自动建会话/复用会话、read 增量
（before_seq）过滤、回合状态判断、cancel、未知 action、连接失败与业务错误的
可读报错。
"""

import unittest

import httpx

from agent.tools.dsh_session import DshSessionTool

WS = "/Users/xx/WorkBuddy/nanoclaw"


def make_tool(handler, base_url="http://127.0.0.1:3080", workspace=WS):
    """构造注入 MockTransport 的 DshSessionTool。"""

    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return DshSessionTool(
        workspace=workspace, base_url=base_url, client_factory=factory
    )


def json_handler(responder):
    """把 HTTP handler 包装成按 (path, payload) 分发的 MockTransport handler。

    responder 接收 (path, payload) 返回 (value_dict | None, error_dict | None)；
    返回 None value 时构造 ok:false 业务错误。
    """

    def handler(request):
        import json as _json

        body = _json.loads(request.content)
        path = request.url.path
        assert body["type"] == "client-request", body
        assert body["rpcId"], "rpcId 必须存在"
        method = body["method"]
        payload = body["payload"]
        value, error = responder(path, method, payload)
        result = {"ok": error is None}
        if value is not None:
            result["value"] = value
        if error is not None:
            result["error"] = error
        return httpx.Response(
            200,
            json={"type": "server-response", "rpcId": body["rpcId"], "result": result},
        )

    return handler


def _msg_event(seq, text, etype="assistant/message"):
    return {
        "event": {
            "type": etype,
            "seq": seq,
            "time": 1000 + seq,
            "data": {"message": {"content": [{"type": "text", "text": text}]}},
        }
    }


class DshEnvelopeTests(unittest.IsolatedAsyncioTestCase):
    """信封与 RPC 底层。"""

    async def test_envelope_shape_and_method_path(self):
        seen = {}

        def responder(path, method, payload):
            seen["path"] = path
            seen["method"] = method
            seen["payload"] = payload
            return {"items": []}, None

        tool = make_tool(json_handler(responder))
        await tool.execute(action="list")
        self.assertEqual(seen["path"], "/api/session.list")
        self.assertEqual(seen["method"], "session.list")
        self.assertEqual(seen["payload"], {})

    async def test_connection_error_returns_readable_message(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        tool = make_tool(handler)
        result = await tool.execute(action="list")
        self.assertIn("DSH 服务未连接", result)
        self.assertIn("dsh web", result)

    async def test_business_error_passthrough(self):
        def responder(path, method, payload):
            return None, {"code": "session-not-found", "message": "会话不存在"}

        tool = make_tool(json_handler(responder))
        result = await tool.execute(action="read", session_id="s1")
        self.assertIn("session-not-found", result)
        self.assertIn("会话不存在", result)

    async def test_unknown_action(self):
        tool = make_tool(json_handler(lambda *a: (None, None)))
        result = await tool.execute(action="fly")
        self.assertIn("未知 action", result)


class DshListTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_shows_sessions_and_project_first(self):
        def responder(path, method, payload):
            return {
                "items": [
                    {
                        "sessionId": "session-aaaa-0001",
                        "running": False,
                        "cwd": "/other/project",
                        "projections": {"values": {"title": "别的项目会话"}},
                    },
                    {
                        "sessionId": "session-bbbb-0002",
                        "running": True,
                        "cwd": WS,
                        "projections": {"values": {"title": "本项目运行中"}},
                    },
                ]
            }, None

        tool = make_tool(json_handler(responder))
        result = await tool.execute(action="list")
        self.assertIn("DSH 会话列表", result)
        self.assertIn("本项目运行中", result)
        self.assertIn("运行中", result)
        # 本项目会话排在前面
        self.assertLess(result.index("本项目运行中"), result.index("别的项目会话"))

    async def test_list_empty(self):
        def responder(path, method, payload):
            return {"items": []}, None

        tool = make_tool(json_handler(responder))
        result = await tool.execute(action="list")
        self.assertIn("目前没有会话", result)


class DshPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_creates_session_when_missing(self):
        calls = []

        def responder(path, method, payload):
            calls.append((path, payload))
            if method == "session.create":
                return {"sessionId": "session-new-1", "agentPreset": "standard"}, None
            if method == "session.prompt":
                return {"accepted": True}, None
            return None, {"code": "unexpected", "message": method}

        tool = make_tool(json_handler(responder))
        result = await tool.execute(
            action="prompt", message="读 PROJECT.md 给方案"
        )
        self.assertIn("session-new-1", result)
        self.assertIn("已派发", result)
        self.assertEqual(calls[0][0], "/api/session.create")
        self.assertEqual(calls[0][1], {"cwd": WS})  # 自动建会话用项目工作区
        self.assertEqual(calls[1][0], "/api/session.prompt")
        self.assertEqual(calls[1][1]["mode"], "queue")
        self.assertEqual(
            calls[1][1]["content"], [{"type": "text", "text": "读 PROJECT.md 给方案"}]
        )

    async def test_prompt_reuses_given_session(self):
        calls = []

        def responder(path, method, payload):
            calls.append(method)
            return {"accepted": True}, None

        tool = make_tool(json_handler(responder))
        result = await tool.execute(
            action="prompt", session_id="session-abc", message="继续改"
        )
        self.assertIn("session-abc", result)
        self.assertEqual(calls, ["session.prompt"])  # 没有 create

    async def test_prompt_requires_message(self):
        tool = make_tool(json_handler(lambda *a: (None, None)))
        result = await tool.execute(action="prompt")
        self.assertIn("必须带 message", result)

    async def test_prompt_normalizes_relative_workspace_to_absolute(self):
        """回归（2026-08-14）：config.workspace 默认是相对路径 '.'，而 DSH
        session.create 要求 cwd 是绝对路径——工具必须自己 abspath 规范化。"""
        import os

        def responder(path, method, payload):
            if method == "session.create":
                self.assertTrue(os.path.isabs(payload["cwd"]), payload["cwd"])
                self.assertFalse(payload["cwd"].startswith("."))
                return {"sessionId": "session-abs-1"}, None
            return {"accepted": True}, None

        # 用相对路径构造（模拟 main.py 传入 config.workspace='.'）
        rel_tool = make_tool(json_handler(responder), workspace=".")
        result = await rel_tool.execute(action="prompt", message="派活")
        self.assertIn("session-abs-1", result)


class DshReadTests(unittest.IsolatedAsyncioTestCase):
    def _history(self, events):
        def responder(path, method, payload):
            return {"events": events}, None

        return responder

    async def test_read_returns_new_reply_with_last_seq(self):
        events = [
            _msg_event(3, "旧回复"),
            _msg_event(10, "新回复内容"),
            {"event": {"type": "turn/end", "seq": 11}},
        ]
        tool = make_tool(json_handler(self._history(events)))
        result = await tool.execute(action="read", session_id="s1", before_seq=5)
        self.assertIn("新回复内容", result)
        self.assertNotIn("旧回复", result)
        self.assertIn("已完成", result)
        self.assertIn("before_seq=11", result)

    async def test_read_without_before_seq_returns_latest(self):
        events = [_msg_event(3, "唯一回复")]
        tool = make_tool(json_handler(self._history(events)))
        result = await tool.execute(action="read", session_id="s1")
        self.assertIn("唯一回复", result)
        self.assertIn("before_seq=3", result)

    async def test_read_still_running_when_no_turn_end(self):
        events = [_msg_event(3, "旧回复")]
        tool = make_tool(json_handler(self._history(events)))
        result = await tool.execute(action="read", session_id="s1", before_seq=5)
        self.assertIn("还在干活", result)
        self.assertIn("before_seq=3", result)

    async def test_read_done_without_new_reply(self):
        events = [
            _msg_event(3, "旧回复"),
            {"event": {"type": "turn/end", "seq": 4}},
        ]
        tool = make_tool(json_handler(self._history(events)))
        result = await tool.execute(action="read", session_id="s1", before_seq=5)
        self.assertIn("回合已完成但无新回复", result)

    async def test_read_requires_session_id(self):
        tool = make_tool(json_handler(lambda *a: (None, None)))
        result = await tool.execute(action="read")
        self.assertIn("必须带 session_id", result)


class DshCancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel(self):
        seen = {}

        def responder(path, method, payload):
            seen.update(payload)
            return {"cancelled": True}, None

        tool = make_tool(json_handler(responder))
        result = await tool.execute(action="cancel", session_id="s1")
        self.assertIn("已请求取消", result)
        self.assertEqual(seen, {"sessionId": "s1"})

    async def test_cancel_requires_session_id(self):
        tool = make_tool(json_handler(lambda *a: (None, None)))
        result = await tool.execute(action="cancel")
        self.assertIn("必须带 session_id", result)


if __name__ == "__main__":
    unittest.main()
