"""TASK-004：记忆跨会话同步（快照 + 版本补丁机制）测试。

覆盖验收点：
- 补丁生成与插入位置（历史之后、本轮 user 之前）
- 补丁持久化（会话 JSONL 中出现补丁、下一轮仍在、模型仍记得）
- 自写刷基线（自己 write_file 后不重复提醒）
- 零注入（无变化时上下文不变）
- 重建快照触发（累积补丁超限 / 大量删除）
- daily/ 变化不触发补丁
- session.memory_revision 的持久化与重启恢复
- ContextBuilder 快照构建时记录 session 初始 revision
"""

import os
import tempfile
import unittest
from copy import deepcopy

from agent.cache_observability import PromptCacheObserver
from agent.context import ContextBuilder
from agent.loop import AgentLoop
from agent.memory_sync import (
    MemoryChangeLog,
    build_patch_message,
    build_snapshot_message,
    diff_lines,
    is_patch_message,
)
from agent.tools.filesystem import WriteFileTool
from agent.tools.registry import ToolRegistry
from providers.base import LLMProvider, LLMResponse, ToolCallRequest
from session.manager import SessionManager


class _RecordingProvider(LLMProvider):
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def chat(self, messages, tools=None, model=None):
        self.requests.append({
            "messages": deepcopy(messages),
            "tools": deepcopy(tools),
            "model": model,
        })
        return self.responses.pop(0)


class MemoryChangeLogTests(unittest.TestCase):
    def test_current_revision_zero_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(MemoryChangeLog(tmp).current_revision(), 0)

    def test_append_increments_revision_and_entries_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = MemoryChangeLog(tmp)
            r1 = log.append("workspace/memory/USER.md", "write", ["- a"], [])
            r2 = log.append("workspace/memory/MEMORY.md", "write", ["- b"], ["- old"])
            self.assertEqual((r1, r2), (1, 2))
            self.assertEqual(log.current_revision(), 2)
            after = log.entries_after(1)
            self.assertEqual([e["revision"] for e in after], [2])
            self.assertEqual(after[0]["file"], "workspace/memory/MEMORY.md")
            self.assertEqual(after[0]["added_lines"], ["- b"])
            self.assertEqual(after[0]["removed_lines"], ["- old"])


class PatchFormatTests(unittest.TestCase):
    def test_build_patch_message_small_diff(self):
        entries = [{
            "revision": 7,
            "file": "workspace/memory/USER.md",
            "operation": "write",
            "added_lines": ["- 用户设备：MacBook"],
            "removed_lines": ["- 用户设备：Windows"],
            "timestamp": "2026-08-05T00:00:00",
        }]
        msg = build_patch_message(entries)
        self.assertEqual(msg["role"], "system")
        self.assertIn('<memory_patch revision="7">', msg["content"])
        self.assertIn("workspace/memory/USER.md", msg["content"])
        self.assertIn("- 用户设备：MacBook", msg["content"])
        self.assertIn("- 用户设备：Windows", msg["content"])
        self.assertIn("该内容覆盖旧记忆中的冲突信息", msg["content"])
        self.assertIn("</memory_patch>", msg["content"])

    def test_build_patch_message_big_change_hint(self):
        entries = [{
            "revision": 8,
            "file": "workspace/memory/MEMORY.md",
            "operation": "write",
            "added_lines": [f"- line{i}" for i in range(21)],
            "removed_lines": [],
            "timestamp": "t",
        }]
        msg = build_patch_message(entries)
        self.assertIn("大改", msg["content"])
        self.assertIn("可 read_file 查看全文", msg["content"])
        self.assertNotIn("- line0", msg["content"])  # 大改不逐行列明细

    def test_diff_lines(self):
        added, removed = diff_lines("- a\n- b\n", "- a\n- c\n")
        self.assertEqual(added, ["- c"])
        self.assertEqual(removed, ["- b"])
        # 无变化的内容不出现在 diff 里
        self.assertNotIn("- a", added)
        self.assertNotIn("- a", removed)

    def test_build_snapshot_message(self):
        msg = build_snapshot_message("USER 内容", "MEMORY 内容", 9)
        self.assertEqual(msg["role"], "system")
        self.assertIn('<memory_snapshot revision="9">', msg["content"])
        self.assertIn("USER 内容", msg["content"])
        self.assertIn("MEMORY 内容", msg["content"])


class ContextSnapshotTests(unittest.TestCase):
    def test_context_records_snapshot_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = MemoryChangeLog(tmp)
            log.append("workspace/memory/USER.md", "write", ["a"], [])
            builder = ContextBuilder(
                tmp, memory_revision_provider=log.current_revision
            )
            self.assertEqual(builder.memory_revision, 1)
            log.append("workspace/memory/USER.md", "write", ["b"], [])
            builder.refresh_snapshot()
            self.assertEqual(builder.memory_revision, 2)

    def test_system_prompt_contains_snapshot_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompt = ContextBuilder(tmp).build_system_prompt()
            self.assertIn("【记忆快照】", prompt)
            self.assertIn("<memory_patch>", prompt)
            self.assertIn("覆盖快照中的旧信息", prompt)


class MemorySyncIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cross_session_patch_injected_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(os.path.join(tmp, "workspace", "sessions"))
            tools = ToolRegistry()
            tools.freeze()
            provider = _RecordingProvider([
                LLMResponse(content="b answer one"),
                LLMResponse(content="b answer two"),
            ])
            loop_b = AgentLoop(
                provider, tools, ContextBuilder(tmp), sessions, session_key="cli:b",
                cache_observer=PromptCacheObserver(lambda _: None),
            )
            # 会话 A（其他会话/进程）更新 USER.md
            await WriteFileTool(tmp).execute(
                "workspace/memory/USER.md", "- 用户设备：MacBook\n"
            )
            # 会话 B 的下一轮应自动收到补丁（无需提示词或模型自觉）
            await loop_b.run("继续")
            first = provider.requests[0]
            # 补丁插在历史之后、本轮 user 之前
            self.assertEqual(first["messages"][-1]["role"], "user")
            self.assertEqual(first["messages"][-1]["content"], "继续")
            patch_msg = first["messages"][-2]
            self.assertEqual(patch_msg["role"], "system")
            self.assertIn("<memory_patch", patch_msg["content"])
            self.assertIn("workspace/memory/USER.md", patch_msg["content"])
            self.assertIn("MacBook", patch_msg["content"])
            # 补丁持久化：会话 JSONL 中出现 system 补丁消息
            records = sessions.get_session_messages("cli:b")
            self.assertTrue(any(is_patch_message(r) for r in records))
            # 下一轮：补丁仍在历史中（模型仍记得），且不重复发新补丁
            await loop_b.run("再继续")
            second = provider.requests[1]
            patch_count = sum(1 for m in second["messages"] if is_patch_message(m))
            self.assertEqual(patch_count, 1)
            self.assertEqual(sessions.get_memory_revision("cli:b"), 1)

    async def test_self_write_refreshes_baseline_no_duplicate_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(os.path.join(tmp, "workspace", "sessions"))
            tools = ToolRegistry()
            tools.register(WriteFileTool(tmp))
            tools.freeze()
            provider = _RecordingProvider([
                LLMResponse(content=None, tool_calls=[ToolCallRequest(
                    "call-1", "write_file", {
                        "file_path": "workspace/memory/USER.md",
                        "content": "- 用户设备：MacBook\n",
                    }
                )]),
                LLMResponse(content="已记下"),
                LLMResponse(content="收到"),
            ])
            loop = AgentLoop(
                provider, tools, ContextBuilder(tmp), sessions, session_key="cli:self",
                cache_observer=PromptCacheObserver(lambda _: None),
            )
            await loop.run("记住我的设备是 MacBook")
            self.assertEqual(MemoryChangeLog(tmp).current_revision(), 1)
            # 自写刷基线：下一轮不给自己发「自己刚写的」补丁（零注入）
            await loop.run("继续")
            req = provider.requests[2]
            self.assertFalse(any(is_patch_message(m) for m in req["messages"]))
            self.assertEqual(sessions.get_memory_revision("cli:self"), 1)

    async def test_zero_injection_when_no_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(os.path.join(tmp, "workspace", "sessions"))
            tools = ToolRegistry()
            tools.freeze()
            provider = _RecordingProvider([
                LLMResponse(content="hi"), LLMResponse(content="again")
            ])
            loop = AgentLoop(
                provider, tools, ContextBuilder(tmp), sessions, session_key="cli:zero",
                cache_observer=PromptCacheObserver(lambda _: None),
            )
            await loop.run("你好")
            await loop.run("你好2")
            for req in provider.requests:
                self.assertFalse(any(is_patch_message(m) for m in req["messages"]))

    async def test_rebuild_snapshot_replaces_accumulated_patches(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(os.path.join(tmp, "workspace", "sessions"))
            tools = ToolRegistry()
            tools.freeze()
            provider = _RecordingProvider([LLMResponse(content="ok")])
            loop = AgentLoop(
                provider, tools, ContextBuilder(tmp), sessions, session_key="cli:rebuild",
                cache_observer=PromptCacheObserver(lambda _: None),
            )
            # 预置 21 条累积补丁（模拟多轮累积超阈值）
            old_patches = [{
                "role": "system",
                "content": (
                    f'<memory_patch revision="{i}">\n文件：workspace/memory/USER.md\n'
                    f'变更内容（新增）：\n- 条目{i}\n该内容覆盖旧记忆中的冲突信息。\n'
                    f"</memory_patch>"
                ),
            } for i in range(1, 22)]
            loop._session_history = old_patches
            sessions.save_messages("cli:rebuild", old_patches)
            # 新增一条变更（触发第 22 个补丁 → 重建快照）
            await WriteFileTool(tmp).execute("workspace/memory/USER.md", "- 最新条目\n")
            await loop.run("新的一轮")
            req = provider.requests[0]
            self.assertFalse(any(is_patch_message(m) for m in req["messages"]))
            snapshots = [
                m for m in req["messages"]
                if (m.get("content") or "").startswith("<memory_snapshot")
            ]
            self.assertEqual(len(snapshots), 1)
            self.assertIn("最新条目", snapshots[0]["content"])
            self.assertEqual(loop._memory_revision, 1)
            # 磁盘历史同样重建：旧补丁被清掉、快照在列
            records = sessions.get_session_messages("cli:rebuild")
            self.assertFalse(any(is_patch_message(r) for r in records))
            self.assertTrue(any(
                (r.get("content") or "").startswith("<memory_snapshot")
                for r in records
            ))

    async def test_large_deletion_triggers_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(os.path.join(tmp, "workspace", "sessions"))
            tools = ToolRegistry()
            tools.freeze()
            provider = _RecordingProvider([LLMResponse(content="ok")])
            loop = AgentLoop(
                provider, tools, ContextBuilder(tmp), sessions, session_key="cli:del",
                cache_observer=PromptCacheObserver(lambda _: None),
            )
            writer = WriteFileTool(tmp)
            await writer.execute(
                "workspace/memory/MEMORY.md",
                "".join(f"- 旧行{i}\n" for i in range(10)),
            )
            await writer.execute("workspace/memory/MEMORY.md", "- 只剩一行\n")
            await loop.run("继续")
            req = provider.requests[0]
            self.assertFalse(any(is_patch_message(m) for m in req["messages"]))
            snapshots = [
                m for m in req["messages"]
                if (m.get("content") or "").startswith("<memory_snapshot")
            ]
            self.assertEqual(len(snapshots), 1)
            self.assertIn("只剩一行", snapshots[0]["content"])

    async def test_daily_change_never_triggers_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(os.path.join(tmp, "workspace", "sessions"))
            tools = ToolRegistry()
            tools.freeze()
            provider = _RecordingProvider([LLMResponse(content="ok")])
            loop = AgentLoop(
                provider, tools, ContextBuilder(tmp), sessions, session_key="cli:daily",
                cache_observer=PromptCacheObserver(lambda _: None),
            )
            # daily/ 是流水账：只同步 USER.md / MEMORY.md，永不注入
            await WriteFileTool(tmp).execute(
                "workspace/memory/daily/2026-08-05.md", "流水账内容"
            )
            self.assertEqual(MemoryChangeLog(tmp).current_revision(), 0)
            await loop.run("继续")
            self.assertFalse(
                any(is_patch_message(m) for m in provider.requests[0]["messages"])
            )

    async def test_restart_resets_revision_no_duplicate_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(os.path.join(tmp, "workspace", "sessions"))
            tools = ToolRegistry()
            tools.freeze()
            loop_b = AgentLoop(
                _RecordingProvider([LLMResponse(content="one")]),
                tools, ContextBuilder(tmp), sessions, session_key="cli:restart",
                cache_observer=PromptCacheObserver(lambda _: None),
            )
            await WriteFileTool(tmp).execute("workspace/memory/USER.md", "- 新信息\n")
            await loop_b.run("继续")
            # 模拟重启：新 ContextBuilder（快照=最新内容）+ 新 AgentLoop
            restarted_provider = _RecordingProvider([LLMResponse(content="two")])
            restarted = AgentLoop(
                restarted_provider, tools, ContextBuilder(tmp), sessions,
                session_key="cli:restart",
                cache_observer=PromptCacheObserver(lambda _: None),
            )
            await restarted.run("重启后继续")
            req = restarted_provider.requests[0]
            # 重启后快照已含最新内容 → 不再新发补丁；历史中的旧补丁仍在
            patch_count = sum(1 for m in req["messages"] if is_patch_message(m))
            self.assertEqual(patch_count, 1)
            self.assertEqual(sessions.get_memory_revision("cli:restart"), 1)


class SessionMetaTests(unittest.TestCase):
    def test_memory_revision_persisted_and_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(os.path.join(tmp, "workspace", "sessions"))
            self.assertIsNone(sessions.get_memory_revision("cli:x"))
            sessions.set_memory_revision("cli:x", 42)
            self.assertEqual(sessions.get_memory_revision("cli:x"), 42)
            sessions.clear("cli:x")
            self.assertIsNone(sessions.get_memory_revision("cli:x"))


if __name__ == "__main__":
    unittest.main()
