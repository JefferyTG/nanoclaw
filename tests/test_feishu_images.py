import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bus.queue import ImageRef, MessageBus, OutboundMessage
from channels.feishu import FeishuChannel
from config import NanoClawConfig, load_config


class _Store:
    def __init__(self, path):
        self.path = path
        self.saved = []
        self.refs = {}

    def save(self, session_key, data, ext, mime):
        ref = ImageRef("received-image", self.path, mime)
        self.saved.append((session_key, data, ext, mime))
        self.refs[(session_key, ref.id)] = ref
        return ref

    def resolve(self, session_key, image_id):
        return self.refs.get((session_key, image_id))


class _Response:
    code = 0
    msg = "ok"

    def success(self):
        return True


class _Client:
    def __init__(self, resource_bytes=b"incoming-png", content_type="image/png"):
        self.download_requests = []
        self.upload_requests = []
        self.message_requests = []
        self.resource_bytes = resource_bytes
        self.content_type = content_type
        self.download_started = None
        self.download_release = None
        self.im = SimpleNamespace(
            v1=SimpleNamespace(
                message_resource=SimpleNamespace(get=self.get_resource),
                image=SimpleNamespace(create=self.create_image),
                message=SimpleNamespace(create=self.create_message),
            )
        )

    def get_resource(self, request):
        self.download_requests.append(request)
        if self.download_started is not None:
            self.download_started.set()
        if self.download_release is not None:
            self.download_release.wait(timeout=2)
        return SimpleNamespace(
            success=lambda: True,
            code=0,
            msg="ok",
            file=SimpleNamespace(read=lambda: self.resource_bytes),
            raw=SimpleNamespace(headers={"content-type": self.content_type}),
        )

    def create_image(self, request):
        self.upload_requests.append(request)
        return SimpleNamespace(success=lambda: True, code=0, msg="ok",
                               data=SimpleNamespace(image_key="feishu-image-key"))

    def create_message(self, request):
        self.message_requests.append(request)
        return _Response()


class FeishuImageTests(unittest.IsolatedAsyncioTestCase):
    def test_merge_window_config_defaults_to_ten_and_can_be_overridden(self):
        self.assertEqual(NanoClawConfig().feishu_image_merge_window_sec, 10.0)
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as config_file:
            json.dump({"feishu_image_merge_window_sec": 3.5}, config_file)
            config_file.flush()
            self.assertEqual(
                load_config(config_file.name).feishu_image_merge_window_sec, 3.5
            )

    async def test_image_event_downloads_saves_and_publishes_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _Store(str(Path(tmp) / "received.png"))
            channel = FeishuChannel(
                "feishu", MessageBus(), "id", "secret", store,
                image_merge_window_sec=0,
            )
            channel._loop = asyncio.get_running_loop()
            client = _Client()
            channel._client = client
            message = SimpleNamespace(
                message_type="image", chat_type="p2p", chat_id="chat-1",
                message_id="om_1", content=json.dumps({"image_key": "img_1"}),
                mentions=[],
            )
            event = SimpleNamespace(message=message, sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_1")
            ))

            channel._on_message(SimpleNamespace(event=event))
            inbound = await asyncio.wait_for(channel.bus.consume_inbound(), timeout=1)

            self.assertEqual(inbound.content, "请分析这张图片。")
            self.assertEqual(inbound.images[0].id, "received-image")
            self.assertEqual(store.saved, [
                ("feishu:chat-1:0", b"incoming-png", "png", "image/png")
            ])
            request = client.download_requests[0]
            self.assertEqual((request.message_id, request.file_key, request.type),
                             ("om_1", "img_1", "image"))

    async def test_image_keeps_session_selected_when_event_was_received(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _Store(str(Path(tmp) / "received.png"))
            channel = FeishuChannel(
                "feishu", MessageBus(), "id", "secret", store,
                image_merge_window_sec=0,
            )
            channel._loop = asyncio.get_running_loop()
            client = _Client()
            client.download_started = threading.Event()
            client.download_release = threading.Event()
            channel._client = client
            message = SimpleNamespace(
                message_type="image", chat_type="p2p", chat_id="chat-1",
                message_id="om_1", content=json.dumps({"image_key": "img_1"}),
                mentions=[],
            )
            event = SimpleNamespace(message=message, sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_1")
            ))

            channel._on_message(SimpleNamespace(event=event))
            started = await asyncio.to_thread(client.download_started.wait, 1)
            self.assertTrue(started)
            channel._session_state("chat-1")["current"] = 1
            client.download_release.set()
            inbound = await asyncio.wait_for(channel.bus.consume_inbound(), timeout=1)

            self.assertEqual(inbound.sender_id, "chat-1:0")
            self.assertEqual(store.saved[0][0], "feishu:chat-1:0")

    async def test_followup_text_merges_with_image_even_while_download_is_slow(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _Store(str(Path(tmp) / "received.png"))
            channel = FeishuChannel(
                "feishu", MessageBus(), "id", "secret", store,
                image_merge_window_sec=0.2,
            )
            channel._loop = asyncio.get_running_loop()
            client = _Client()
            client.download_started = threading.Event()
            client.download_release = threading.Event()
            channel._client = client
            image_message = SimpleNamespace(
                message_type="image", chat_type="p2p", chat_id="chat-1",
                message_id="om_1", content=json.dumps({"image_key": "img_1"}),
                mentions=[],
            )
            text_message = SimpleNamespace(
                message_type="text", chat_type="p2p", chat_id="chat-1",
                content=json.dumps({"text": "这里为什么没有响应？"}), mentions=[],
            )
            sender = SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_1"))

            channel._on_message(SimpleNamespace(
                event=SimpleNamespace(message=image_message, sender=sender)
            ))
            started = await asyncio.to_thread(client.download_started.wait, 1)
            self.assertTrue(started)
            channel._on_message(SimpleNamespace(
                event=SimpleNamespace(message=text_message, sender=sender)
            ))
            await asyncio.sleep(0)
            client.download_release.set()
            inbound = await asyncio.wait_for(channel.bus.consume_inbound(), timeout=1)

            self.assertEqual(inbound.content, "这里为什么没有响应？")
            self.assertEqual(len(inbound.images), 1)
            await asyncio.sleep(0.25)
            self.assertTrue(channel.bus.inbound_queue.empty())

    async def test_each_additional_image_resets_merge_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _Store(str(Path(tmp) / "received.png"))
            channel = FeishuChannel(
                "feishu", MessageBus(), "id", "secret", store,
                image_merge_window_sec=0.08,
            )
            channel._loop = asyncio.get_running_loop()
            channel._client = _Client()
            sender = SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_1"))

            def image_event(message_id, image_key):
                message = SimpleNamespace(
                    message_type="image", chat_type="p2p", chat_id="chat-1",
                    message_id=message_id,
                    content=json.dumps({"image_key": image_key}), mentions=[],
                )
                return SimpleNamespace(event=SimpleNamespace(
                    message=message, sender=sender
                ))

            channel._on_message(image_event("om_1", "img_1"))
            await asyncio.sleep(0.05)
            channel._on_message(image_event("om_2", "img_2"))
            await asyncio.sleep(0.05)
            self.assertTrue(channel.bus.inbound_queue.empty())

            inbound = await asyncio.wait_for(channel.bus.consume_inbound(), timeout=0.2)
            self.assertEqual(inbound.content, "请分析这些图片。")
            self.assertEqual(len(inbound.images), 2)

    async def test_clear_discards_image_that_is_still_downloading(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _Store(str(Path(tmp) / "received.png"))
            channel = FeishuChannel(
                "feishu", MessageBus(), "id", "secret", store,
                image_merge_window_sec=0.2,
            )
            channel._loop = asyncio.get_running_loop()
            client = _Client()
            client.download_started = threading.Event()
            client.download_release = threading.Event()
            channel._client = client
            sender = SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_1"))
            image_message = SimpleNamespace(
                message_type="image", chat_type="p2p", chat_id="chat-1",
                message_id="om_1", content=json.dumps({"image_key": "img_1"}),
                mentions=[],
            )
            clear_message = SimpleNamespace(
                message_type="text", chat_type="p2p", chat_id="chat-1",
                content=json.dumps({"text": "/clear"}), mentions=[],
            )

            channel._on_message(SimpleNamespace(
                event=SimpleNamespace(message=image_message, sender=sender)
            ))
            started = await asyncio.to_thread(client.download_started.wait, 1)
            self.assertTrue(started)
            channel._on_message(SimpleNamespace(
                event=SimpleNamespace(message=clear_message, sender=sender)
            ))
            await asyncio.sleep(0)
            client.download_release.set()
            outbound = await asyncio.wait_for(channel.bus.consume_outbound(), timeout=1)
            await asyncio.sleep(0.05)

            self.assertIn("历史已清空", outbound.content)
            self.assertEqual(store.saved, [])
            self.assertTrue(channel.bus.inbound_queue.empty())

    async def test_unsupported_image_returns_error_instead_of_agent_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _Store(str(Path(tmp) / "received.bin"))
            channel = FeishuChannel("feishu", MessageBus(), "id", "secret", store)
            channel._loop = asyncio.get_running_loop()
            channel._client = _Client(b"not-an-image", "application/octet-stream")
            message = SimpleNamespace(
                message_type="image", chat_type="p2p", chat_id="chat-1",
                message_id="om_1", content=json.dumps({"image_key": "img_1"}),
                mentions=[],
            )
            event = SimpleNamespace(message=message, sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_1")
            ))

            channel._on_message(SimpleNamespace(event=event))
            outbound = await asyncio.wait_for(channel.bus.consume_outbound(), timeout=1)

            self.assertIn("仅支持", outbound.content)
            self.assertEqual(store.saved, [])
            self.assertTrue(channel.bus.inbound_queue.empty())

    async def test_recognized_image_id_is_uploaded_then_sent_without_text_regression(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "generated.png"
            image_path.write_bytes(b"generated-png")
            store = _Store(str(image_path))
            key = "feishu:chat-1:0"
            store.refs[(key, "generated-1")] = ImageRef(
                "generated-1", str(image_path), "image/png"
            )
            channel = FeishuChannel("feishu", MessageBus(), "id", "secret", store)
            channel._client = _Client()
            # Simulate /new arriving before the original Agent reply: the
            # structured ImageRef must still be sent without resolving current.
            channel._session_state("chat-1")["current"] = 1

            await channel.send(OutboundMessage(
                "feishu", "chat-1", "已生成图片（image_id=generated-1）。", None,
                images=[store.refs[(key, "generated-1")]],
            ))

            requests = channel._client.message_requests
            self.assertEqual([r.request_body.msg_type for r in requests], ["image", "text"])
            self.assertEqual(json.loads(requests[0].request_body.content),
                             {"image_key": "feishu-image-key"})
            self.assertEqual(json.loads(requests[1].request_body.content),
                             {"text": "已生成图片（image_id=generated-1）。"})
            self.assertEqual(len(channel._client.upload_requests), 1)

            # Normal text keeps the original single text-message behavior.
            await channel.send(OutboundMessage("feishu", "chat-1", "普通回复", None))
            self.assertEqual([r.request_body.msg_type for r in requests],
                             ["image", "text", "text"])

    async def test_image_upload_rejection_does_not_block_text_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "generated.png"
            image_path.write_bytes(b"too-large-for-test")
            store = _Store(str(image_path))
            ref = ImageRef("generated-1", str(image_path), "image/png")
            channel = FeishuChannel("feishu", MessageBus(), "id", "secret", store)
            channel._client = _Client()

            with patch("channels.feishu._MAX_OUTBOUND_IMAGE_BYTES", 4):
                await channel.send(OutboundMessage(
                    "feishu", "chat-1", "文字仍应发送", images=[ref]
                ))

            self.assertEqual(channel._client.upload_requests, [])
            self.assertEqual(
                [r.request_body.msg_type for r in channel._client.message_requests],
                ["text"],
            )
