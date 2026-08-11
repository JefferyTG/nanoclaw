"""FcBridge 骨架单测：空工具默认路径 + call_id 配对执行器预留（TASK-037）。"""

import asyncio
import unittest

from voice.realtime_s2s.fc_bridge import FcBridge


class FcBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_tools_empty_and_no_executor_logs_only(self):
        bridge = FcBridge()
        self.assertEqual(bridge.tools, [])
        # 默认路径：收到函数调用事件不抛错（仅日志），call_id 仍被配对记录
        await bridge.handle(
            {
                "type": "response.function_call_arguments.done",
                "call_id": "call_1",
                "name": "get_weather",
                "arguments": "{}",
            }
        )
        self.assertEqual(bridge.pending(), {"call_1": ("get_weather", {})})

    async def test_executor_invoked_with_call_id_pairing(self):
        calls = []

        async def executor(call_id, name, arguments):
            calls.append((call_id, name, arguments))
            return "ok"

        bridge = FcBridge(executor=executor)
        await bridge.handle(
            {
                "type": "response.function_call_arguments.done",
                "call_id": "call_1",
                "name": "get_weather",
                "arguments": '{"city": "北京"}',
            }
        )
        self.assertEqual(calls, [("call_1", "get_weather", {"city": "北京"})])
        self.assertIn("call_1", bridge.pending())

    async def test_missing_call_id_ignored(self):
        bridge = FcBridge()
        await bridge.handle(
            {"type": "response.function_call_arguments.done", "name": "x"}
        )
        self.assertEqual(bridge.pending(), {})

    async def test_executor_failure_does_not_raise(self):
        async def executor(call_id, name, arguments):
            raise RuntimeError("boom")

        bridge = FcBridge(executor=executor)
        await bridge.handle(
            {
                "type": "response.function_call_arguments.done",
                "call_id": "c2",
                "name": "boom",
                "arguments": "",
            }
        )
        self.assertEqual(bridge.pending(), {"c2": ("boom", {})})

    async def test_invalid_arguments_json_treated_as_empty(self):
        bridge = FcBridge()
        await bridge.handle(
            {
                "type": "response.function_call_arguments.done",
                "call_id": "c3",
                "name": "x",
                "arguments": "not-json{{",
            }
        )
        self.assertEqual(bridge.pending(), {"c3": ("x", {})})
