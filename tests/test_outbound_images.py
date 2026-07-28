"""Cross-layer tests for generated images attached to outbound messages."""

import asyncio
import tempfile
import unittest

from agent.context import ContextBuilder
from agent.imagestore import ImageStore
from agent.loop import AgentLoop
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from agent.tools.spawn import DummySessionManager
from bus.queue import InboundMessage, MessageBus
from channels.base import Channel
from gateway import Gateway
from providers.base import LLMProvider, LLMResponse, ToolCallRequest


class _Provider(LLMProvider):
    def __init__(self, responses):
        self.responses = list(responses)

    async def chat(self, messages, tools=None, model=None):
        return self.responses.pop(0)


class _GenerateImageTool(Tool):
    name = "generate_image"
    description = "test image generation"
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, store):
        self.store = store

    async def execute(
        self, session_key=None, stream_sink=None, _generated_ids=None, **kwargs
    ):
        ref = self.store.save(
            session_key, b"\x89PNG\r\n\x1a\nimage", "png", "image/png"
        )
        _generated_ids.append(ref.id)
        return f"image_id={ref.id}"


class _Channel(Channel):
    async def start(self):
        return None

    async def send(self, message):
        return None


class OutboundImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_collects_generated_ids_and_resets_each_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ImageStore(tmp)
            registry = ToolRegistry()
            registry.register(_GenerateImageTool(store))
            provider = _Provider([
                LLMResponse(
                    None,
                    [ToolCallRequest("image-call", "generate_image", {})],
                    "tool_calls",
                ),
                LLMResponse("图片已生成"),
                LLMResponse("下一轮没有图片"),
            ])
            loop = AgentLoop(
                provider=provider,
                tools=registry,
                context=ContextBuilder(tmp),
                session_manager=DummySessionManager(),
                session_key="feishu:chat:0",
                image_store=store,
            )

            await loop.run("生成图片")
            self.assertEqual(len(loop.last_generated_image_ids), 1)
            self.assertIsNotNone(
                store.resolve("feishu:chat:0", loop.last_generated_image_ids[0])
            )

            await loop.run("普通消息")
            self.assertEqual(loop.last_generated_image_ids, [])

    async def test_gateway_resolves_generated_ids_into_outbound_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ImageStore(tmp)
            ref = store.save(
                "feishu:chat:0", b"\x89PNG\r\n\x1a\nimage", "png", "image/png"
            )

            class _Agent:
                image_store = store
                last_generated_image_ids = []

                async def run(self, content, images=None, stream_sink=None):
                    self.last_generated_image_ids = [ref.id]
                    return "带图回复"

            bus = MessageBus()
            gateway = Gateway(
                bus, [_Channel("feishu", bus)], lambda _key: _Agent()
            )
            await gateway._handle_one(
                InboundMessage("feishu", "chat:0", "chat", "画一张图"),
                "feishu:chat:0",
                asyncio.Lock(),
            )
            outbound = await bus.consume_outbound()
            self.assertEqual(outbound.content, "带图回复")
            self.assertEqual([image.id for image in outbound.images], [ref.id])


if __name__ == "__main__":
    unittest.main()
