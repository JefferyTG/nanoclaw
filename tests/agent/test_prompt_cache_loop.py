from copy import deepcopy
import tempfile
import unittest

from agent.cache_observability import PromptCacheObserver
from agent.context import ContextBuilder
from agent.imagestore import ImageStore
from agent.loop import AgentLoop
from agent.memory import ContextCompactor
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from providers.base import LLMProvider, LLMResponse, ToolCallRequest
from providers.usage import PromptCacheUsage
from session.manager import SessionManager


class _RecordingProvider(LLMProvider):
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def chat(self, messages, tools=None, model=None, **kwargs):
        self.requests.append({
            "messages": deepcopy(messages),
            "tools": deepcopy(tools),
            "model": model,
        })
        return self.responses.pop(0)


class _EchoTool(Tool):
    name = "echo"
    description = "echo"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        return "echoed"


def _usage(input_tokens: int, cached_tokens: int) -> PromptCacheUsage:
    return PromptCacheUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        uncached_input_tokens=input_tokens - cached_tokens,
        cache_ratio=cached_tokens / input_tokens,
        availability="available",
    )


class PromptCacheLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_turn_next_turn_and_restart_share_exact_api_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(tmp)
            tools = ToolRegistry()
            tools.register(_EchoTool())
            tools.freeze()
            first_provider = _RecordingProvider([
                LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest(
                        "call-1", "echo", {}, reasoning_content="stable reasoning"
                    )],
                    finish_reason="tool_calls",
                    cache_usage=_usage(100, 20),
                ),
                LLMResponse(content="first answer", cache_usage=_usage(300, 240)),
                LLMResponse(content="second answer", cache_usage=_usage(500, 450)),
            ])
            observer = PromptCacheObserver(lambda _: None)
            loop = AgentLoop(
                first_provider,
                tools,
                ContextBuilder(tmp),
                sessions,
                session_key="cli:cache",
                cache_observer=observer,
            )

            await loop.run("first")
            restarted_provider = _RecordingProvider([
                LLMResponse(content="second answer", cache_usage=_usage(500, 450))
            ])
            restarted = AgentLoop(
                restarted_provider,
                tools,
                ContextBuilder(tmp),
                sessions,
                session_key="cli:cache",
                cache_observer=PromptCacheObserver(lambda _: None),
            )
            await loop.run("second")
            await restarted.run("second")

            initial_request = first_provider.requests[0]
            tool_followup = first_provider.requests[1]
            next_turn = first_provider.requests[2]
            restart_turn = restarted_provider.requests[0]
            self.assertEqual(
                tool_followup["messages"][:len(initial_request["messages"])],
                initial_request["messages"],
            )
            self.assertEqual(
                next_turn["messages"][:len(tool_followup["messages"])],
                tool_followup["messages"],
            )
            self.assertEqual(restart_turn, next_turn)
            self.assertEqual(next_turn["tools"], initial_request["tools"])

            first_turn = observer.turns[0]
            self.assertEqual(first_turn.calls, 2)
            self.assertEqual(first_turn.input_tokens, 400)
            self.assertEqual(first_turn.cached_input_tokens, 260)
            self.assertEqual(first_turn.uncached_input_tokens, 140)
            self.assertEqual(first_turn.cache_ratio, 0.65)
            self.assertEqual(first_turn.history_messages, 0)
            self.assertEqual(observer.turns[1].history_messages, 4)

    async def test_existing_multimodal_history_is_stable_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(tmp)
            image_store = ImageStore(tmp)
            image = image_store.save(
                "web:image", b"stable-image-bytes", "png", "image/png"
            )
            tools = ToolRegistry()
            provider = _RecordingProvider([
                LLMResponse(content="seen", cache_usage=_usage(100, 0)),
                LLMResponse(content="followup", cache_usage=_usage(200, 100)),
            ])
            loop = AgentLoop(
                provider,
                tools,
                ContextBuilder(tmp),
                sessions,
                session_key="web:image",
                image_store=image_store,
                base_model_multimodal=True,
                cache_observer=PromptCacheObserver(lambda _: None),
            )
            await loop.run("inspect", images=[image])
            restarted_provider = _RecordingProvider([
                LLMResponse(content="followup", cache_usage=_usage(200, 100))
            ])
            restarted = AgentLoop(
                restarted_provider,
                tools,
                ContextBuilder(tmp),
                sessions,
                session_key="web:image",
                image_store=image_store,
                base_model_multimodal=True,
                cache_observer=PromptCacheObserver(lambda _: None),
            )

            await loop.run("follow up")
            await restarted.run("follow up")

            initial = provider.requests[0]
            next_turn = provider.requests[1]
            self.assertEqual(
                next_turn["messages"][:len(initial["messages"])], initial["messages"]
            )
            self.assertEqual(restarted_provider.requests[0], next_turn)

    async def test_consolidation_call_is_included_in_weighted_turn_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(tmp)
            key = "cli:compressed"
            for index in range(8):
                sessions.save_message(key, {
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": "old" * 40,
                })
            provider = _RecordingProvider([
                LLMResponse(content="summary", cache_usage=_usage(100, 20)),
                LLMResponse(content="answer", cache_usage=_usage(200, 180)),
            ])
            observer = PromptCacheObserver(lambda _: None)
            loop = AgentLoop(
                provider,
                ToolRegistry(),
                ContextBuilder(tmp),
                sessions,
                session_key=key,
                compactor=ContextCompactor(provider, tmp, token_budget=20),
                cache_observer=observer,
            )

            await loop.run("new")

            metric = observer.turns[0]
            self.assertEqual(
                [call.phase for call in observer.calls], ["consolidation", "react"]
            )
            self.assertEqual(metric.calls, 2)
            self.assertEqual(metric.input_tokens, 300)
            self.assertEqual(metric.cached_input_tokens, 200)
            self.assertAlmostEqual(metric.cache_ratio, 2 / 3)
            # TASK-007：压缩发生后无条件注入一条新快照（含在发往主 ReAct 的
            # 历史里）→ 压缩回合 history_messages 比纯压缩结果多 1（快照）。
            # 该快照是压缩已破缓存后补回「磁盘最新记忆」的必要消息，属预期。
            self.assertEqual(metric.history_messages, 7)


if __name__ == "__main__":
    unittest.main()
