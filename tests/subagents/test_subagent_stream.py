"""Regression coverage for isolated sub-agent stream/replay metadata."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.context import ContextBuilder
from agent.loop import AgentLoop
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from agent.tools.spawn import DummySessionManager, SpawnSubagentTool
from providers.base import LLMProvider, LLMResponse, ToolCallRequest


class _ScriptedProvider(LLMProvider):
    def __init__(self, responses):
        self.responses = list(responses)

    async def chat(self, messages, tools=None, model=None, **kwargs):
        return self.responses.pop(0)


class _ImageTool(Tool):
    name = "generate_image"
    description = "test image"
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self):
        self.session_keys = []

    async def execute(self, session_key=None, stream_sink=None, _generated_ids=None, **kwargs):
        self.session_keys.append(session_key)
        if isinstance(_generated_ids, list):
            _generated_ids.append("child-image")
        if stream_sink:
            await stream_sink({"type": "image", "id": "child-image", "key": session_key})
        return "generated"


class _EchoTool(Tool):
    name = "echo"
    description = "test echo"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        return "echoed"


class _RecordingSession(DummySessionManager):
    def __init__(self):
        self.records = []

    def save_message(self, session_key, message):
        self.records.append(dict(message))


class _NestedLoop:
    """Tiny AgentLoop double that recursively invokes its injected spawn tool."""

    calls = 0

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.index = self.__class__.calls
        self.__class__.calls += 1

    async def run(self, task, stream_sink=None):
        if self.index == 0:
            await stream_sink({"type": "tool_call", "name": "spawn_subagent", "args": "{}"})
            await self.tools.execute(
                "spawn_subagent",
                {
                    "task": "inner",
                    "_parent_stream_sink": stream_sink,
                    "_parent_subagent_runs": self.subagent_runs_sink,
                },
            )
            await stream_sink({"type": "tool_result", "name": "spawn_subagent", "content": "ok", "duration_ms": 1})
        else:
            await stream_sink({"type": "thinking", "content": "inner thought"})
        await stream_sink({"type": "done", "content": task + " complete"})
        return task + " complete"


class SubagentStreamTests(unittest.IsolatedAsyncioTestCase):
    def _spawn(self, registry, provider_factory):
        return SpawnSubagentTool(
            provider_factory=provider_factory,
            tools_registry=registry,
            workspace=".",
            config=SimpleNamespace(max_iterations=4, turn_timeout_sec=30, model="m"),
            max_depth=3,
        )

    async def test_events_are_namespaced_and_image_uses_parent_session(self):
        image = _ImageTool()
        registry = ToolRegistry()
        registry.register(image)
        responses = [
            LLMResponse(None, [ToolCallRequest("img", "generate_image", {})], "tool_calls"),
            LLMResponse("child answer"),
        ]
        spawn = self._spawn(registry, lambda model: _ScriptedProvider(responses))
        events, image_ids, runs = [], [], []
        async def sink(event):
            events.append(event)

        result = await spawn.execute(
            task="draw",
            _parent_stream_sink=sink,
            _parent_session_key="web:parent",
            _parent_generated_ids=image_ids,
            _parent_subagent_runs=runs,
            _parent_tool_call_id="parent-call",
        )

        self.assertEqual(result, "child answer")
        self.assertEqual(image.session_keys, ["web:parent"])
        self.assertEqual(image_ids, ["child-image"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["tool_call_id"], "parent-call")
        self.assertIn("duration_ms", runs[0])
        self.assertEqual(runs[0]["image_ids"], ["child-image"])
        self.assertEqual(runs[0]["tool_steps"], [{"name": "generate_image", "status": "completed", "duration_ms": 0}])
        self.assertTrue(all(e["type"] == "subagent_event" for e in events))
        self.assertTrue(all("run_id" in e and "event" in e for e in events))
        self.assertEqual(events[-1]["event"]["type"], "done")
        self.assertEqual(events[-1]["event"]["status"], "completed")

    async def test_replay_metadata_is_stripped_before_model_call(self):
        registry = ToolRegistry()
        loop = AgentLoop(
            provider=_ScriptedProvider([LLMResponse("ok")]), tools=registry,
            context=ContextBuilder("."), session_manager=DummySessionManager(),
        )
        message = {
            "role": "assistant", "content": None, "tool_calls": [],
            "generated_images": ["i"], "subagent_runs": [{"run_id": "r"}],
        }
        cleaned = loop._history_item_to_api(message, False)
        self.assertNotIn("generated_images", cleaned)
        self.assertNotIn("subagent_runs", cleaned)

    async def test_parent_assistant_persists_child_replay_metadata(self):
        image = _ImageTool()
        registry = ToolRegistry()
        registry.register(image)
        child_responses = [
            LLMResponse(None, [ToolCallRequest("img", "generate_image", {})], "tool_calls"),
            LLMResponse("child answer"),
        ]
        spawn = self._spawn(registry, lambda model: _ScriptedProvider(child_responses))
        registry.register(spawn)
        session = _RecordingSession()
        parent = AgentLoop(
            provider=_ScriptedProvider([
                LLMResponse(None, [ToolCallRequest("spawn-id", "spawn_subagent", {"task": "draw"})], "tool_calls"),
                LLMResponse("parent answer"),
            ]),
            tools=registry, context=ContextBuilder("."), session_manager=session,
            session_key="web:parent",
        )
        events = []
        async def sink(event):
            events.append(event)

        await parent.run("please draw", stream_sink=sink)

        assistant = next(record for record in session.records if record.get("tool_calls"))
        self.assertEqual(assistant["generated_images"], ["child-image"])
        self.assertEqual(assistant["subagent_runs"][0]["tool_call_id"], "spawn-id")
        self.assertEqual(assistant["subagent_runs"][0]["status"], "completed")
        self.assertEqual(parent.last_generated_image_ids, ["child-image"])
        assistant_index = session.records.index(assistant)
        tool_index = next(
            index for index, record in enumerate(session.records)
            if record.get("tool_call_id") == "spawn-id"
        )
        self.assertLess(assistant_index, tool_index)
        self.assertTrue(any(event["type"] == "subagent_event" for event in events))

    async def test_nested_events_are_forwarded_once_without_rewrapping(self):
        registry = ToolRegistry()
        spawn = self._spawn(registry, lambda model: object())
        registry.register(spawn)
        events, runs = [], []
        async def sink(event):
            events.append(event)
        _NestedLoop.calls = 0
        with patch("agent.tools.spawn.AgentLoop", _NestedLoop):
            await spawn.execute(
                task="outer", _parent_stream_sink=sink,
                _parent_subagent_runs=runs,
            )

        run_ids = {event["run_id"] for event in events}
        self.assertEqual(len(run_ids), 2)
        self.assertTrue(all(event["type"] == "subagent_event" for event in events))
        self.assertFalse(any(event["event"].get("type") == "subagent_event" for event in events))
        self.assertEqual({item["depth"] for item in runs}, {1, 2})

    async def test_cancellation_emits_terminal_state_and_propagates(self):
        class _BlockingLoop(_NestedLoop):
            async def run(self, task, stream_sink=None):
                await asyncio.Event().wait()

        registry = ToolRegistry()
        spawn = self._spawn(registry, lambda model: object())
        events, runs = [], []
        async def sink(event):
            events.append(event)
        with patch("agent.tools.spawn.AgentLoop", _BlockingLoop):
            task = asyncio.create_task(spawn.execute(
                task="wait", _parent_stream_sink=sink, _parent_subagent_runs=runs,
            ))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(runs[0]["status"], "cancelled")
        self.assertIn("duration_ms", runs[0])
        self.assertEqual(events[-1]["event"]["status"], "cancelled")

    async def test_exception_emits_terminal_state_with_duration(self):
        class _FailingLoop(_NestedLoop):
            async def run(self, task, stream_sink=None):
                raise RuntimeError("boom")

        registry = ToolRegistry()
        spawn = self._spawn(registry, lambda model: object())
        events, runs = [], []
        async def sink(event):
            events.append(event)

        with patch("agent.tools.spawn.AgentLoop", _FailingLoop):
            result = await spawn.execute(
                task="fail", _parent_stream_sink=sink, _parent_subagent_runs=runs,
            )

        self.assertIn("boom", result)
        self.assertEqual(runs[0]["status"], "error")
        self.assertIn("duration_ms", runs[0])
        self.assertEqual(events[-1]["event"]["status"], "error")

    async def test_child_structured_timeout_status_is_forwarded(self):
        class _TimedOutLoop(_NestedLoop):
            last_run_status = "timed_out"

            async def run(self, task, stream_sink=None):
                await stream_sink({"type": "done", "content": "localized timeout"})
                return "localized timeout"

        registry = ToolRegistry()
        spawn = self._spawn(registry, lambda model: object())
        events, runs = [], []

        async def sink(event):
            events.append(event)

        with patch("agent.tools.spawn.AgentLoop", _TimedOutLoop):
            result = await spawn.execute(
                task="slow", _parent_stream_sink=sink, _parent_subagent_runs=runs,
            )

        self.assertEqual(result, "localized timeout")
        self.assertEqual(runs[0]["status"], "timed_out")
        self.assertEqual(events[-1]["event"]["status"], "timed_out")

    async def test_agent_loop_marks_model_timeout_and_iteration_limit(self):
        class _SlowProvider(LLMProvider):
            async def chat(self, messages, tools=None, model=None, **kwargs):
                await asyncio.sleep(0.05)
                return LLMResponse("too late")

        timeout_loop = AgentLoop(
            provider=_SlowProvider(), tools=ToolRegistry(), context=ContextBuilder("."),
            session_manager=DummySessionManager(), turn_timeout=0.01,
        )
        await timeout_loop.run("wait")
        self.assertEqual(timeout_loop.last_run_status, "timed_out")

        registry = ToolRegistry()
        registry.register(_EchoTool())
        iteration_loop = AgentLoop(
            provider=_ScriptedProvider([
                LLMResponse(
                    None,
                    [ToolCallRequest("echo-id", "echo", {})],
                    "tool_calls",
                )
            ]),
            tools=registry,
            context=ContextBuilder("."),
            session_manager=DummySessionManager(),
            max_iterations=1,
        )
        await iteration_loop.run("repeat")
        self.assertEqual(iteration_loop.last_run_status, "timed_out")

    async def test_parent_cancellation_persists_child_run_for_history(self):
        started = asyncio.Event()

        class _CancellableSpawnTool(Tool):
            execution_timeout_sec = None
            name = "spawn_subagent"
            description = "test cancellable child"
            parameters = {"type": "object", "properties": {}, "required": []}

            async def execute(self, _parent_subagent_runs=None, **kwargs):
                _parent_subagent_runs.append({
                    "run_id": "cancelled-child",
                    "agent_name": "测试子 Agent",
                    "depth": 1,
                    "status": "cancelled",
                    "final_result": "子任务已取消。",
                    "tool_steps": [],
                    "image_ids": [],
                })
                started.set()
                await asyncio.Event().wait()

        registry = ToolRegistry()
        registry.register(_CancellableSpawnTool())
        session = _RecordingSession()
        parent = AgentLoop(
            provider=_ScriptedProvider([
                LLMResponse(
                    None,
                    [ToolCallRequest("spawn-id", "spawn_subagent", {})],
                    "tool_calls",
                )
            ]),
            tools=registry,
            context=ContextBuilder("."),
            session_manager=session,
            session_key="web:cancelled",
        )
        task = asyncio.create_task(parent.run("cancel child"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        replay_record = next(
            record for record in session.records if record.get("subagent_runs")
        )
        self.assertNotIn("tool_calls", replay_record)
        self.assertEqual(replay_record["subagent_runs"][0]["status"], "cancelled")
        cleaned = parent._history_item_to_api(replay_record, False)
        self.assertNotIn("subagent_runs", cleaned)
        self.assertEqual(cleaned["role"], "assistant")


if __name__ == "__main__":
    unittest.main()
