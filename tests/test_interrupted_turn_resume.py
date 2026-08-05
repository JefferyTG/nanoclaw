"""Regression tests: an interrupted turn must survive into the next model input.

TASK-010 根因：``AgentLoop._session_history``（模型下一轮输入唯一来源）只在
「正常完成 / 熔断 / 取消」三个点整段重建；而磁盘落盘走 ``_persist`` 每步都写、
**从不**同步内存。因此 `turn_timeout` 超时 / 模型 error 等「非完成返回」路径
结束后，同一 AgentLoop 实例的下一轮仍用旧的（停留在上一完整回合的）内存历史
拼上下文 → 中断回合及之前已落盘的消息全部「失忆」。

修复：``_persist`` 改为「落盘即同步」——每写一条磁盘就按磁盘顺序增量追加进
``_session_history``，磁盘是唯一事实源，任意中断路径内存都不丢。

这些测试覆盖：

1. 主场景：call1 返回 tool_calls → 工具执行 → call2 阻塞至超时（无最终文本）；
   断言磁盘与 ``_session_history`` 均含 [user, assistant(tc), tool]，且同一实例
   「继续」时模型输入含中断回合的工具消息。
2. loop-top 超时变体：turn_timeout 很小、工具执行耗光整轮预算 → 同样断言。
3. finish_reason=error 变体：call2 模型返回 error → 同样断言。
4. 取消路径回归：网页「停止」（CancelledError）修复后 user / 占位 assistant
   在内存与磁盘各只出现一次（不重复、幂等）；工具执行中取消复用中断记录。
5. 真重启路径守护：中断后新建第二个 AgentLoop（重新 get_history），上下文完整。
6. 压缩联动：超预算历史触发压缩 → 中断回合 → 接回，内存与磁盘一致。
"""

import asyncio
from copy import deepcopy
import os
import tempfile
import unittest

from agent.context import ContextBuilder
from agent.loop import AgentLoop
from agent.memory import ContextCompactor
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from providers.base import LLMProvider, LLMResponse, ToolCallRequest
from session.manager import SessionManager


# —— 测试工具 ——

class _EchoTool(Tool):
    name = "echo"
    description = "test echo tool"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        return "echo-result"


class _SlowTool(Tool):
    """执行耗时 0.3s，用于耗尽极小的 turn_timeout（loop-top 超时路径）。"""

    name = "slow"
    description = "test slow tool"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        await asyncio.sleep(0.3)
        return "slow-result"


class _BlockingTool(Tool):
    """execute 阻塞直到外层任务被取消（模拟取消发生在工具执行中）。"""

    name = "block"
    description = "test blocking tool"
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self):
        self.entered = asyncio.Event()

    async def execute(self, **kwargs):
        self.entered.set()
        await asyncio.Event().wait()
        return "unreachable"


class _HangThenRespondProvider(LLMProvider):
    """call1 返回 tool_calls；call2 挂起直到 wait_for 超时；call3 起返回最终回复。

    用于主场景：先正常完成一轮工具交换，再让下一个模型调用触发整轮超时，
    最后用同一个 AgentLoop 实例「继续」（call3）。
    """

    def __init__(self, tool_response: LLMResponse, final: LLMResponse):
        self.tool_response = tool_response
        self.final = final
        self.calls = 0
        self.requests = []

    async def chat(self, messages, tools=None, model=None):
        self.calls += 1
        self.requests.append(deepcopy(messages))
        if self.calls == 1:
            return self.tool_response
        if self.calls == 2:
            await asyncio.Event().wait()
            raise AssertionError("任务取消后不应继续执行")
        return self.final


class _ScriptedResponsesProvider(LLMProvider):
    """按剧本依次返回响应，并记录每次请求（深拷贝）。

    用于 finish_reason=error / loop-top 超时变体：call2 不一定要被调用
    （loop-top 在调用前就终止），因此不能像 _HangThenRespondProvider 那样
    硬编码第 2 次挂起。
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.requests = []

    async def chat(self, messages, tools=None, model=None):
        self.calls += 1
        self.requests.append(deepcopy(messages))
        return self.responses.pop(0)


class _BlockingProvider(LLMProvider):
    """chat 阻塞直到外层任务被取消（模拟网页「停止」）。"""

    def __init__(self):
        self.entered = asyncio.Event()

    async def chat(self, messages, tools=None, model=None):
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("任务取消后不应继续执行")


class _SummaryProvider:
    """压缩摘要用的固定输出 Provider（结构化摘要失败时降级为散文）。"""

    async def chat(self, messages, tools=None, model=None):
        return LLMResponse("stable summary")


def _echo_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    return registry


def _new_loop(tmp, sessions, key, provider, registry, **kwargs):
    kwargs.setdefault("turn_timeout", 0.2)
    return AgentLoop(
        provider,
        registry,
        ContextBuilder(tmp),
        sessions,
        session_key=key,
        model="m",
        max_iterations=8,
        **kwargs,
    )


def _tool_calls_response(name="echo", call_id="call-1") -> LLMResponse:
    return LLMResponse(
        None,
        [ToolCallRequest(call_id, name, {})],
        "tool_calls",
    )


class InterruptedTurnResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_turn_kept_in_memory_and_disk_then_resumed(self):
        """主场景（对应本案）：call1 工具调用 → call2 阻塞至超时，无最终文本。

        修复前：中断回合只落盘不同步内存，同一实例「继续」时模型输入缺该回合。
        修复后：内存 = 磁盘 = [user, assistant(tc), tool]，下一轮可见。
        """
        provider = _HangThenRespondProvider(
            _tool_calls_response(), LLMResponse("已完成")
        )
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(os.path.join(tmp, "sessions"))
            loop = _new_loop(tmp, sessions, "web:interrupt", provider, _echo_registry())

            reply = await loop.run("开始任务")
            self.assertEqual(loop.last_run_status, "timed_out")
            self.assertIn("超时", reply)

            # 内存与磁盘都含完整中断回合（真实 tool 结果，而非占位符）
            self.assertEqual(
                [m["role"] for m in loop._session_history],
                ["user", "assistant", "tool"],
            )
            self.assertEqual(loop._session_history[1]["tool_calls"][0]["id"], "call-1")
            self.assertEqual(loop._session_history[2]["content"], "echo-result")
            disk = sessions.get_history("web:interrupt")
            self.assertEqual([m["role"] for m in disk], ["user", "assistant", "tool"])
            self.assertEqual(disk[2]["content"], "echo-result")

            # 同一 AgentLoop 实例「继续」：模型输入包含中断回合的工具消息
            reply = await loop.run("继续")
            self.assertEqual(reply, "已完成")
            self.assertEqual(provider.calls, 3)
            resumed = provider.requests[2]
            self.assertEqual(
                [m["role"] for m in resumed],
                ["system", "user", "assistant", "tool", "user"],
            )
            self.assertEqual(resumed[3]["tool_call_id"], "call-1")
            self.assertEqual(resumed[3]["content"], "echo-result")
            self.assertEqual(resumed[4]["content"], "继续")

    async def test_loop_top_timeout_after_tool_exchange_kept(self):
        """loop-top 超时变体：极小 turn_timeout + 慢工具耗尽整轮预算。

        触发 :796-802 的整轮超时检查（在下一个模型调用之前就终止），
        同样要求中断回合完整保留并可接回。
        """
        provider = _ScriptedResponsesProvider([
            _tool_calls_response(name="slow", call_id="call-slow"),
            LLMResponse("已完成"),
        ])
        registry = ToolRegistry()
        registry.register(_SlowTool())
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(os.path.join(tmp, "sessions"))
            loop = _new_loop(
                tmp, sessions, "web:loop-top", provider, registry, turn_timeout=0.05
            )

            reply = await loop.run("开始任务")
            self.assertEqual(loop.last_run_status, "timed_out")
            self.assertIn("时间上限", reply)
            # 第二个模型调用（超时变体中的"下一个调用"）从未发生
            self.assertEqual(provider.calls, 1)

            self.assertEqual(
                [m["role"] for m in loop._session_history],
                ["user", "assistant", "tool"],
            )
            self.assertEqual(loop._session_history[2]["content"], "slow-result")
            disk = sessions.get_history("web:loop-top")
            self.assertEqual([m["role"] for m in disk], ["user", "assistant", "tool"])
            self.assertEqual(disk[2]["content"], "slow-result")

            reply = await loop.run("继续")
            self.assertEqual(reply, "已完成")
            resumed = provider.requests[1]
            self.assertEqual(
                [m["role"] for m in resumed],
                ["system", "user", "assistant", "tool", "user"],
            )
            self.assertEqual(resumed[3]["content"], "slow-result")
            self.assertEqual(resumed[4]["content"], "继续")

    async def test_error_turn_kept_in_memory_and_disk_then_resumed(self):
        """finish_reason=error 变体：call2 模型返回 error（:865），无最终文本。"""
        provider = _ScriptedResponsesProvider([
            _tool_calls_response(),
            LLMResponse("模型错误", finish_reason="error"),
            LLMResponse("已完成"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(os.path.join(tmp, "sessions"))
            loop = _new_loop(tmp, sessions, "web:error", provider, _echo_registry())

            reply = await loop.run("开始任务")
            self.assertEqual(loop.last_run_status, "error")
            self.assertEqual(reply, "模型错误")

            self.assertEqual(
                [m["role"] for m in loop._session_history],
                ["user", "assistant", "tool"],
            )
            self.assertEqual(loop._session_history[2]["content"], "echo-result")
            disk = sessions.get_history("web:error")
            self.assertEqual([m["role"] for m in disk], ["user", "assistant", "tool"])
            self.assertEqual(disk[2]["content"], "echo-result")

            reply = await loop.run("继续")
            self.assertEqual(reply, "已完成")
            resumed = provider.requests[2]
            self.assertEqual(
                [m["role"] for m in resumed],
                ["system", "user", "assistant", "tool", "user"],
            )
            self.assertEqual(resumed[3]["content"], "echo-result")
            self.assertEqual(resumed[4]["content"], "继续")

    async def _cancel_and_await(self, task):
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        self.assertTrue(task.cancelled())

    async def test_cancel_after_fix_has_no_duplicate_records(self):
        """取消路径回归：_persist 同步内存后，取消分支不再重复追加。

        修复后 user / 占位 assistant 在内存与磁盘各只出现一次；重复调用
        _record_cancelled_turn 幂等。
        """
        provider = _BlockingProvider()
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(os.path.join(tmp, "sessions"))
            loop = _new_loop(tmp, sessions, "web:cancel-reg", provider, _echo_registry())

            task = asyncio.create_task(loop.run("继续"))
            await asyncio.wait_for(provider.entered.wait(), timeout=1)
            await self._cancel_and_await(task)

            self.assertEqual(loop.last_run_status, "cancelled")
            history = loop._session_history
            self.assertEqual([m["role"] for m in history], ["user", "assistant"])
            self.assertEqual(
                len([m for m in history if m["role"] == "user"]), 1
            )
            self.assertEqual(
                len([m for m in history if m["role"] == "assistant"]), 1
            )
            self.assertIn("上一轮回答被用户手动停止", history[1]["content"])

            # 磁盘干净：与内存一致，无重复
            disk = sessions.get_history("web:cancel-reg")
            self.assertEqual([m["role"] for m in disk], ["user", "assistant"])
            self.assertEqual(disk[0]["content"], "继续")

            # 幂等：重复补历史无副作用
            first = list(loop._session_history)
            loop._record_cancelled_turn()
            self.assertEqual(loop._session_history, first)
            self.assertEqual(
                len([m for m in loop._session_history if m["role"] == "user"]), 1
            )

    async def test_cancel_during_tool_keeps_single_interrupt_record(self):
        """取消发生在工具执行中：_execute_tools 已落盘的中断记录被复用，
        _record_cancelled_turn 不重复生成/追加占位 assistant。"""
        tool = _BlockingTool()
        registry = ToolRegistry()
        registry.register(tool)
        provider = _ScriptedResponsesProvider([
            _tool_calls_response(name="block", call_id="call-b"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(os.path.join(tmp, "sessions"))
            loop = _new_loop(
                tmp, sessions, "web:cancel-tool", provider, registry,
                generated_ids_sink=["img-9"],
            )

            task = asyncio.create_task(loop.run("画图"))
            await asyncio.wait_for(tool.entered.wait(), timeout=1)
            await self._cancel_and_await(task)

            history = loop._session_history
            self.assertEqual([m["role"] for m in history], ["user", "assistant"])
            self.assertEqual(
                len([m for m in history if m["role"] == "assistant"]), 1
            )
            self.assertIn("本轮工具执行已取消", history[-1]["content"])
            self.assertEqual(history[-1].get("generated_images"), ["img-9"])
            disk = sessions.get_history("web:cancel-tool")
            self.assertEqual([m["role"] for m in disk], ["user", "assistant"])
            self.assertEqual(
                len([m for m in disk if m["role"] == "assistant"]), 1
            )

    async def test_restart_recovers_interrupted_turn_from_disk(self):
        """真重启路径守护：中断回合后新建第二个 AgentLoop（重新 get_history），
        从磁盘全量恢复中断回合，保护现有正确行为不回归。"""
        provider1 = _HangThenRespondProvider(
            _tool_calls_response(), LLMResponse("已完成")
        )
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(os.path.join(tmp, "sessions"))
            loop1 = _new_loop(tmp, sessions, "web:restart", provider1, _echo_registry())
            await loop1.run("开始任务")
            self.assertEqual(loop1.last_run_status, "timed_out")

            # 新实例（真进程重启等价：重新 get_history + canonicalize）
            provider2 = _ScriptedResponsesProvider([LLMResponse("接续完成")])
            fresh = _new_loop(tmp, sessions, "web:restart", provider2, _echo_registry())
            self.assertEqual(
                [m["role"] for m in fresh._session_history],
                ["user", "assistant", "tool"],
            )
            self.assertEqual(fresh._session_history[2]["content"], "echo-result")

            reply = await fresh.run("继续")
            self.assertEqual(reply, "接续完成")
            resumed = provider2.requests[0]
            self.assertEqual(
                [m["role"] for m in resumed],
                ["system", "user", "assistant", "tool", "user"],
            )
            self.assertEqual(resumed[3]["content"], "echo-result")
            self.assertEqual(resumed[4]["content"], "继续")

    async def test_compaction_then_interrupt_then_resume_consistent(self):
        """压缩联动：超预算历史触发压缩 → 中断回合 → 接回，内存与磁盘一致。

        压缩发生在 _run 开头（save_messages 覆盖写回 + 重建 _session_history），
        之后 _persist 增量追加中断回合；断言压缩后的历史与中断回合在内存/磁盘
        两边一致，且接回时模型输入含中断回合的工具消息。
        """
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(os.path.join(tmp, "sessions"))
            key = "web:compact"
            # 预置超预算历史（>7 条且远超 token 预算），下一轮必然触发压缩
            for i in range(10):
                sessions.save_message(key, {
                    "role": "user" if i % 2 == 0 else "assistant",
                    "content": "这是一段足够长的历史内容" * 30,
                })
            compactor = ContextCompactor(_SummaryProvider(), tmp, token_budget=50)
            provider1 = _HangThenRespondProvider(
                _tool_calls_response(), LLMResponse("已完成")
            )
            loop1 = _new_loop(
                tmp, sessions, key, provider1, _echo_registry(), compactor=compactor
            )

            await loop1.run("继续")
            self.assertEqual(loop1.last_run_status, "timed_out")

            # 压缩把中间历史压成一条 system 摘要，中断回合完整保留在末尾
            history = loop1._session_history
            self.assertIn("system", [m["role"] for m in history])
            self.assertEqual(
                [m["role"] for m in history[-3:]],
                ["user", "assistant", "tool"],
            )
            self.assertEqual(history[-1]["content"], "echo-result")
            # 内存与磁盘一致（磁盘含摘要 + 中断回合）
            disk = sessions.get_history(key)
            self.assertEqual(
                [m["role"] for m in disk[-3:]],
                ["user", "assistant", "tool"],
            )
            self.assertEqual(disk[-1]["content"], "echo-result")

            # 接回：新实例从磁盘恢复，模型输入含中断回合的工具消息
            provider2 = _ScriptedResponsesProvider([LLMResponse("接续完成")])
            fresh = _new_loop(
                tmp, sessions, key, provider2, _echo_registry(), compactor=compactor
            )
            reply = await fresh.run("继续")
            self.assertEqual(reply, "接续完成")
            resumed = provider2.requests[0]
            tool_msgs = [m for m in resumed if m["role"] == "tool"]
            self.assertTrue(tool_msgs, "接回时模型输入应含中断回合的工具消息")
            self.assertEqual(tool_msgs[0]["content"], "echo-result")
            self.assertEqual(resumed[-1]["content"], "继续")


if __name__ == "__main__":
    unittest.main()
