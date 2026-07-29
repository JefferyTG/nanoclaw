import asyncio
import unittest
from types import SimpleNamespace

try:
    from reminders.models import DeliveryResult
except ModuleNotFoundError:  # The core reminder task is integrated separately.
    DeliveryResult = None

from bus.queue import MessageBus, OutboundMessage
from channels.base import Channel
from channels.feishu import FeishuChannel
from gateway import Gateway


class _ResultChannel(Channel):
    def __init__(self, bus, result=None, error=None):
        super().__init__("test", bus)
        self.result, self.error, self.calls = result, error, 0

    async def start(self):
        pass

    async def send(self, message):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class _Response:
    def __init__(self, code=0, msg="ok", message_id="om_1"):
        self.code, self.msg = code, msg
        self.data = SimpleNamespace(message_id=message_id)

    def success(self):
        return self.code == 0


class _Client:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.im = SimpleNamespace(v1=SimpleNamespace(
            message=SimpleNamespace(create=self.create),
            image=SimpleNamespace(create=self.create),
        ))

    def create(self, request):
        self.calls += 1
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@unittest.skipIf(DeliveryResult is None, "requires the shared reminders.models module")
class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def _one_dispatch(self, gateway):
        task = asyncio.create_task(gateway._dispatch_outbound())
        await asyncio.sleep(0)
        return task

    async def test_gateway_keeps_ordinary_messages_ack_free(self):
        bus = MessageBus()
        channel = _ResultChannel(bus)
        task = await self._one_dispatch(Gateway(bus, [channel], lambda _: None))
        await bus.publish_outbound(OutboundMessage("test", "chat", "normal"))
        await asyncio.sleep(0)
        self.assertEqual(channel.calls, 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_gateway_completes_missing_channel_and_send_exception_once(self):
        bus = MessageBus()
        gateway = Gateway(bus, [], lambda _: None)
        task = await self._one_dispatch(gateway)
        missing = asyncio.get_running_loop().create_future()
        await bus.publish_outbound(OutboundMessage("missing", "chat", "x", delivery_future=missing))
        self.assertFalse((await missing).success)

        broken = _ResultChannel(bus, error=RuntimeError("boom"))
        gateway._channel_map["test"] = broken
        failed = asyncio.get_running_loop().create_future()
        await bus.publish_outbound(OutboundMessage("test", "chat", "x", delivery_future=failed))
        self.assertEqual((await failed).message, "boom")
        self.assertTrue(failed.done())
        delivered = asyncio.get_running_loop().create_future()
        gateway._channel_map["test"] = _ResultChannel(
            bus, DeliveryResult(
                success=True, retryable=False, code=0, message="accepted",
                provider_message_id="om_next",
            )
        )
        await bus.publish_outbound(OutboundMessage("test", "chat", "next", delivery_future=delivered))
        self.assertTrue((await delivered).success)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_feishu_returns_success_and_classifies_transient_failures(self):
        channel = FeishuChannel("feishu", MessageBus(), "id", "secret")
        channel._client = _Client([_Response(0, message_id="om_ok")])
        result = await channel.send(OutboundMessage("feishu", "chat", "hello"))
        self.assertTrue(result.success)
        self.assertEqual(result.provider_message_id, "om_ok")
        self.assertEqual(channel._client.calls, 1)

        channel._client = _Client([OSError("network")])
        transient = await channel.send(OutboundMessage("feishu", "chat", "hello"))
        self.assertFalse(transient.success)
        self.assertTrue(transient.retryable)
        self.assertEqual(channel._client.calls, 1)

    async def test_feishu_classifies_permanent_and_server_failures(self):
        channel = FeishuChannel("feishu", MessageBus(), "id", "secret")
        channel._client = _Client([_Response(403, "forbidden")])
        permanent = await channel.send(OutboundMessage("feishu", "chat", "hello"))
        self.assertFalse(permanent.success)
        self.assertFalse(permanent.retryable)

        channel._client = _Client([_Response(503, "unavailable")])
        transient = await channel.send(OutboundMessage("feishu", "chat", "hello"))
        self.assertFalse(transient.success)
        self.assertTrue(transient.retryable)
        self.assertEqual(channel._client.calls, 1)

    async def test_feishu_stops_after_text_chunk_failure(self):
        channel = FeishuChannel("feishu", MessageBus(), "id", "secret")
        channel._client = _Client([_Response(), _Response(400, "bad target")])
        content = "a" * 4001
        result = await channel.send(OutboundMessage("feishu", "chat", content))
        self.assertFalse(result.success)
        self.assertEqual(channel._client.calls, 2)

    async def test_image_upload_failure_stops_before_text(self):
        channel = FeishuChannel("feishu", MessageBus(), "id", "secret")
        channel._client = _Client([_Response()])

        async def fail_upload(ref):
            return None, DeliveryResult(
                success=False, retryable=False, code=400, message="bad image",
                provider_message_id=None,
            )

        channel._upload_image = fail_upload
        result = await channel.send(OutboundMessage(
            "feishu", "chat", "text must not send", images=[object()],
            delivery_future=asyncio.get_running_loop().create_future(),
        ))
        self.assertFalse(result.success)
        self.assertEqual(channel._client.calls, 0)

    async def test_ordinary_chat_image_failure_keeps_text_fallback(self):
        channel = FeishuChannel("feishu", MessageBus(), "id", "secret")
        channel._client = _Client([_Response()])

        async def fail_upload(ref):
            return None, DeliveryResult(
                success=False, retryable=False, code=400, message="bad image",
                provider_message_id=None,
            )

        channel._upload_image = fail_upload
        result = await channel.send(OutboundMessage(
            "feishu", "chat", "text fallback", images=[object()]
        ))
        self.assertFalse(result.success)
        self.assertEqual(channel._client.calls, 1)

    async def test_image_message_failure_is_classified(self):
        channel = FeishuChannel("feishu", MessageBus(), "id", "secret")
        channel._client = _Client([_Response(400, "invalid chat")])

        async def uploaded(ref):
            return "image_key", None

        channel._upload_image = uploaded
        result = await channel.send(OutboundMessage(
            "feishu", "chat", "", images=[object()]
        ))
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        self.assertEqual(result.code, 400)

    async def test_gateway_none_result_and_done_ack_are_safe(self):
        bus = MessageBus()
        channel = _ResultChannel(bus, result=None)
        task = await self._one_dispatch(Gateway(bus, [channel], lambda _: None))
        future = asyncio.get_running_loop().create_future()
        await bus.publish_outbound(OutboundMessage("test", "chat", "x", delivery_future=future))
        self.assertIn("no delivery result", (await future).message)

        completed = asyncio.get_running_loop().create_future()
        completed.set_result(DeliveryResult(
            success=True, retryable=False, code=0, message="already",
            provider_message_id=None,
        ))
        await bus.publish_outbound(OutboundMessage("test", "chat", "x", delivery_future=completed))
        await asyncio.sleep(0)
        self.assertEqual(completed.result().message, "already")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()
