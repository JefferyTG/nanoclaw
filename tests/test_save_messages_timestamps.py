"""TASK-015（NC-MEM-002）save_messages 保留时间戳测试。

覆盖验收点：
- 保留模式（preserve_timestamps=True）：已有 timestamp 的消息写回后时间戳不变；
  缺失 timestamp 的消息补当前时间；**还能从原文件按身份找回被覆盖的原始时间戳**
  （NC-MEM-002 的关键——压缩/快照重建的输入消息本身不带 timestamp）；
- 默认模式（preserve_timestamps=False）：行为不变——整段覆盖写回统一写当前
  时间（取消补历史 / 子 Agent 落盘等调用点语义不受影响）；
- canonicalize 后仍能按身份挂回时间戳（含 tool_calls / tool 消息）；
- 压缩写回集成：AgentLoop 压缩路径走保留模式，尾部保留消息时间戳不变、
  新摘要消息取当前压缩时刻；TASK-007「压缩→无条件重建完整快照」紧随其后同样
  走保留模式，不会把时间戳再改写掉；
- 子 Agent 落盘（DummySessionManager.save_messages）接受新参数、仍为 no-op。
"""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from agent.context import ContextBuilder
from agent.loop import AgentLoop
from agent.memory import CompactionResult
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from agent.tools.spawn import DummySessionManager
from providers.base import LLMProvider, LLMResponse
from session.manager import SessionManager

OLD_TS = [
    "2026-08-01T10:00:00",
    "2026-08-01T10:01:00",
    "2026-08-01T10:02:00",
    "2026-08-01T10:03:00",
]


class SaveMessagesTimestampTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.manager = SessionManager(os.path.join(self._tmp.name, "sessions"))
        self.key = "cli:ts"

    def test_preserve_mode_keeps_existing_timestamps_and_fills_missing(self):
        """保留模式：已有时间戳原样保留；缺失补当前时间。"""
        before = datetime.now()
        self.manager.save_messages(
            self.key,
            [
                {"role": "user", "content": "旧消息", "timestamp": OLD_TS[0]},
                {"role": "assistant", "content": "新消息（无时间戳）"},
            ],
            preserve_timestamps=True,
        )
        after = datetime.now()
        records = self.manager.get_session_messages(self.key)
        self.assertEqual(records[0]["timestamp"], OLD_TS[0])  # 原样保留
        self.assertNotEqual(records[1]["timestamp"], OLD_TS[0])  # 不是旧时间戳
        filled = datetime.fromisoformat(records[1]["timestamp"])
        # 补的是写入时刻（在调用前后之间）
        self.assertGreaterEqual(filled, before - timedelta(seconds=1))
        self.assertLessEqual(filled, after + timedelta(seconds=1))

    def test_preserve_mode_recovers_timestamps_from_existing_file(self):
        """保留模式可从原文件找回原始时间戳（NC-MEM-002 核心场景）。

        模拟压缩：旧文件已带原始时间戳，覆盖写回的新列表本身不带 timestamp
        （压缩输入消息经 get_history/canonicalize 剥离），保留模式下按身份
        从旧文件找回，而非全部改写为当前时刻。
        """
        self.manager.save_messages(
            self.key,
            [
                {"role": "user", "content": "历史内容0", "timestamp": OLD_TS[0]},
                {"role": "assistant", "content": "历史内容1", "timestamp": OLD_TS[1]},
                {"role": "user", "content": "历史内容2", "timestamp": OLD_TS[2]},
                {"role": "assistant", "content": "历史内容3", "timestamp": OLD_TS[3]},
            ],
            preserve_timestamps=True,  # 建立带原始时间戳的旧文件
        )
        # 覆盖写回：新列表只有尾部 2 条（内容与旧文件一致，无 timestamp）
        self.manager.save_messages(
            self.key,
            [
                {"role": "system", "content": "[历史摘要]: 测试摘要"},
                {"role": "user", "content": "历史内容2"},
                {"role": "assistant", "content": "历史内容3"},
            ],
            preserve_timestamps=True,
        )
        records = self.manager.get_session_messages(self.key)
        self.assertEqual(len(records), 3)
        # 摘要消息无旧文件匹配 → 补当前时间
        self.assertNotIn(records[0]["timestamp"], OLD_TS)
        # 尾部保留消息按身份找回原始时间戳
        self.assertEqual(records[1]["timestamp"], OLD_TS[2])
        self.assertEqual(records[2]["timestamp"], OLD_TS[3])

    def test_default_mode_overwrites_all_timestamps(self):
        """默认模式行为不变：整段覆盖写回统一写当前时间。"""
        self.manager.save_messages(
            self.key,
            [
                {"role": "user", "content": "a", "timestamp": OLD_TS[0]},
                {"role": "assistant", "content": "b", "timestamp": OLD_TS[1]},
            ],
        )
        records = self.manager.get_session_messages(self.key)
        self.assertNotIn(records[0]["timestamp"], OLD_TS)
        self.assertNotIn(records[1]["timestamp"], OLD_TS)
        self.assertEqual(records[0]["timestamp"], records[1]["timestamp"])

    def test_preserve_mode_survives_canonicalize_tool_messages(self):
        """保留模式在 canonicalize（清洗 tool_calls、补工具结果）后仍能挂回时间戳。"""
        tool_calls = [{
            "id": "call-1", "type": "function",
            "function": {"name": "echo", "arguments": "{}"},
        }]
        self.manager.save_messages(
            self.key,
            [
                {"role": "assistant", "content": None, "tool_calls": tool_calls,
                 "timestamp": OLD_TS[0]},
                {"role": "tool", "tool_call_id": "call-1", "content": "结果",
                 "timestamp": OLD_TS[1]},
            ],
            preserve_timestamps=True,
        )
        records = self.manager.get_session_messages(self.key)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["timestamp"], OLD_TS[0])
        self.assertEqual(records[1]["timestamp"], OLD_TS[1])
        # canonicalize 后 tool_calls 只留三个合法字段，时间戳仍挂回
        self.assertEqual(
            list(records[0]["tool_calls"][0].keys()),
            ["id", "type", "function"],
        )

    def test_dummy_session_manager_accepts_preserve_kwarg(self):
        """子 Agent 落盘（DummySessionManager）接受新参数且仍是 no-op（语义不变）。"""
        dummy = DummySessionManager(os.path.join(self._tmp.name, "sessions"))
        # 带 preserve_timestamps 关键字调用不抛 TypeError、不落盘
        dummy.save_messages(
            "sub", [{"role": "user", "content": "x"}], preserve_timestamps=True
        )
        self.assertEqual(dummy.get_history("sub"), [])
        # 默认调用同样 no-op
        dummy.save_messages("sub", [{"role": "user", "content": "x"}])
        self.assertEqual(dummy.get_history("sub"), [])


class _EchoTool(Tool):
    name = "echo"
    description = "test echo tool"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        return "echo-result"


class _SimpleProvider(LLMProvider):
    """压缩后主循环直接返回最终回复，不再调用工具。"""

    def __init__(self):
        self.requests = []

    async def chat(self, messages, tools=None, model=None):
        self.requests.append(messages)
        return LLMResponse("最终回复")


class _StubCompactor:
    """固定返回压缩结果的假压缩器（复用传入 messages 的 system/tail/user）。

    模拟生产 `maybe_compact`：保留首条 system、把中间旧消息压成一条摘要、
    保留末尾 3 条 tail 与末条当前用户消息。
    """

    def __init__(self):
        self.seen = None
        self.token_budget = 100

    async def maybe_compact(self, messages, tools=None, cache_turn=None):
        self.seen = messages
        system_msg = messages[0]
        current_user = messages[-1]
        history = messages[1:-1]
        tail = history[-3:]
        summary_msg = {"role": "system", "content": "[历史摘要]: 测试摘要"}
        return CompactionResult(
            messages=[system_msg, summary_msg] + tail + [current_user],
            compacted=True,
            estimated_tokens=100_000,
            token_budget=100,
            summarized_messages=len(history) - len(tail),
            preserved_tail_messages=len(tail),
        )


class CompressionWriteBackTimestampTests(unittest.IsolatedAsyncioTestCase):
    """压缩写回 + 压缩后快照重建都走保留模式，原始时间戳全程不被改写。"""

    async def _make_sessions(self, tmp):
        sessions = SessionManager(os.path.join(tmp, "sessions"))
        key = "web:compact-ts"
        # 预置 4 条带旧时间戳的历史
        for i, ts in enumerate(OLD_TS):
            sessions.save_message(key, {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"历史内容{i}",
            })
        raw = sessions.get_session_messages(key)
        for m, ts in zip(raw, OLD_TS):
            m["timestamp"] = ts
        sessions.save_messages(key, raw, preserve_timestamps=True)
        return sessions, key

    async def test_compaction_and_snapshot_rebuild_preserve_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, key = await self._make_sessions(tmp)
            compactor = _StubCompactor()
            provider = _SimpleProvider()
            registry = ToolRegistry()
            registry.register(_EchoTool())
            loop = AgentLoop(
                provider, registry, ContextBuilder(tmp), sessions,
                session_key=key, model="m", max_iterations=8, turn_timeout=0.5,
                compactor=compactor,
            )

            await loop.run("新消息")

            # 真实链路：压缩写回 [摘要, 尾部3条] → 压缩后无条件重建快照
            # （TASK-007）追加 <memory_snapshot> → 本轮 user/assistant 落盘。
            disk = sessions.get_session_messages(key)
            self.assertEqual(
                [m["role"] for m in disk],
                ["system", "assistant", "user", "assistant", "system", "user", "assistant"],
            )
            # 摘要消息时间戳 = 压缩时刻（不是任何旧时间戳）
            self.assertEqual(disk[0]["content"], "[历史摘要]: 测试摘要")
            self.assertNotIn(disk[0]["timestamp"], OLD_TS)
            # 尾部 3 条保留原始时间戳（快照重建未把它们改写掉）
            self.assertEqual([m["timestamp"] for m in disk[1:4]], OLD_TS[-3:])
            # 新生成的 <memory_snapshot> 取当前时刻（不在旧时间戳里）
            self.assertIn("<memory_snapshot", disk[4]["content"])
            self.assertNotIn(disk[4]["timestamp"], OLD_TS)
            # 本轮 user / assistant 继续正常落盘
            self.assertEqual(disk[5]["content"], "新消息")
            self.assertEqual(disk[6]["content"], "最终回复")
            self.assertNotIn(disk[5]["timestamp"], OLD_TS)
            self.assertNotIn(disk[6]["timestamp"], OLD_TS)


if __name__ == "__main__":
    unittest.main()
