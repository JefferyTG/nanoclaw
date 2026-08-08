"""飞书语音消息入站转写测试（TASK-020）。

mock 风格与 tests/test_feishu_images.py 对齐：mock ``_Client`` 的
``im.v1.message_resource.get`` 返回语音字节，mock 一个假 ASR service。
绝不触发真实硅基流动 API 调用。
"""

import asyncio
import json
import unittest
from types import SimpleNamespace

from bus.queue import MessageBus
from channels.feishu import FeishuChannel
from voice.asr.base import ASRError


class _FakeASR:
    """假 ASR 服务：记录调用参数，可按需抛错或返回固定文本。"""

    def __init__(self, text="语音转写出来的文字", error=None):
        self.text = text
        self.error = error
        self.calls = []

    async def transcribe(self, data, *, filename, media_type):
        self.calls.append((data, filename, media_type))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.text)


class _Client:
    def __init__(
        self,
        *,
        resource_bytes=b"fake-opus-bytes",
        content_type="audio/opus",
        download_failure=False,
    ):
        self.download_requests = []
        self.resource_bytes = resource_bytes
        self.content_type = content_type
        self.download_failure = download_failure
        self.im = SimpleNamespace(
            v1=SimpleNamespace(
                message_resource=SimpleNamespace(get=self.get_resource)
            )
        )

    def get_resource(self, request):
        self.download_requests.append(request)
        if self.download_failure:
            return SimpleNamespace(
                success=lambda: False, code=99999, msg="download failed",
                file=None, raw=None,
            )
        return SimpleNamespace(
            success=lambda: True,
            code=0,
            msg="ok",
            file=SimpleNamespace(read=lambda: self.resource_bytes),
            raw=SimpleNamespace(headers={"content-type": self.content_type}),
        )


class FeishuAudioTests(unittest.IsolatedAsyncioTestCase):
    async def _channel(self, asr=None, client=None):
        channel = FeishuChannel(
            "feishu", MessageBus(), "id", "secret", asr_service=asr
        )
        channel._loop = asyncio.get_running_loop()
        channel._client = client or _Client()
        return channel

    def _audio_event(
        self,
        *,
        chat_type="p2p",
        chat_id="chat-1",
        message_id="om_audio_1",
        file_key="voice_1",
        mentions=None,
        sender_open_id="ou_1",
    ):
        content = {"file_key": file_key} if file_key is not None else {}
        message = SimpleNamespace(
            message_type="audio", chat_type=chat_type, chat_id=chat_id,
            message_id=message_id, content=json.dumps(content),
            mentions=mentions or [],
        )
        sender = SimpleNamespace(
            sender_id=SimpleNamespace(open_id=sender_open_id)
        )
        return SimpleNamespace(
            event=SimpleNamespace(message=message, sender=sender)
        )

    async def test_audio_message_is_downloaded_transcribed_and_published(self):
        asr = _FakeASR(text="帮我看看这个")
        channel = await self._channel(asr=asr)
        channel._on_message(self._audio_event())
        inbound = await asyncio.wait_for(channel.bus.consume_inbound(), timeout=1)

        self.assertEqual(inbound.channel, "feishu")
        self.assertEqual(inbound.content, "帮我看看这个")
        self.assertEqual(inbound.sender_id, "chat-1:0")
        self.assertEqual(inbound.chat_id, "chat-1")
        self.assertEqual(inbound.raw["chat_type"], "p2p")
        self.assertEqual(inbound.raw["sender_open_id"], "ou_1")
        # 飞书语音/音频资源必须用 type=file，不能用 image
        request = channel._client.download_requests[0]
        self.assertEqual(
            (request.message_id, request.file_key, request.type),
            ("om_audio_1", "voice_1", "file"),
        )
        data, filename, media_type = asr.calls[0]
        self.assertEqual(data, b"fake-opus-bytes")
        self.assertTrue(filename.startswith("voice_1."))
        self.assertEqual(media_type, "audio/opus")
        self.assertTrue(channel.bus.outbound_queue.empty())

    async def test_asr_error_returns_error_hint_and_does_not_crash(self):
        asr = _FakeASR(error=ASRError("provider_down", "上游服务暂时不可用。"))
        channel = await self._channel(asr=asr)
        channel._on_message(self._audio_event())
        outbound = await asyncio.wait_for(channel.bus.consume_outbound(), timeout=1)

        self.assertIn("⚠️ 语音转写失败", outbound.content)
        self.assertIn("上游服务暂时不可用", outbound.content)
        self.assertTrue(channel.bus.inbound_queue.empty())

    async def test_missing_asr_service_returns_not_enabled_hint(self):
        channel = await self._channel(asr=None)
        channel._on_message(self._audio_event())
        outbound = await asyncio.wait_for(channel.bus.consume_outbound(), timeout=1)

        self.assertIn("⚠️ 当前实例未启用语音转写（ASR）", outbound.content)
        # 未启用时不触发下载，也不投递 Agent 消息
        self.assertEqual(channel._client.download_requests, [])
        self.assertTrue(channel.bus.inbound_queue.empty())

    async def test_missing_file_key_returns_resource_error(self):
        channel = await self._channel(asr=_FakeASR())
        channel._on_message(self._audio_event(file_key=None))
        outbound = await asyncio.wait_for(channel.bus.consume_outbound(), timeout=1)

        self.assertIn("⚠️ 语音消息缺少资源标识", outbound.content)
        self.assertEqual(channel._client.download_requests, [])
        self.assertTrue(channel.bus.inbound_queue.empty())

    async def test_missing_message_id_returns_resource_error(self):
        channel = await self._channel(asr=_FakeASR())
        channel._on_message(self._audio_event(message_id=None))
        outbound = await asyncio.wait_for(channel.bus.consume_outbound(), timeout=1)

        self.assertIn("⚠️ 语音消息缺少资源标识", outbound.content)
        self.assertEqual(channel._client.download_requests, [])
        self.assertTrue(channel.bus.inbound_queue.empty())

    async def test_download_failure_returns_read_error(self):
        channel = await self._channel(
            asr=_FakeASR(), client=_Client(download_failure=True)
        )
        channel._on_message(self._audio_event())
        outbound = await asyncio.wait_for(channel.bus.consume_outbound(), timeout=1)

        self.assertIn("⚠️ 从飞书读取语音失败", outbound.content)
        self.assertTrue(channel.bus.inbound_queue.empty())

    async def test_missing_content_type_falls_back_to_octet_stream(self):
        asr = _FakeASR(text="兜底")
        channel = await self._channel(
            asr=asr, client=_Client(content_type="")
        )
        channel._on_message(self._audio_event())
        inbound = await asyncio.wait_for(channel.bus.consume_inbound(), timeout=1)

        self.assertEqual(inbound.content, "兜底")
        data, filename, media_type = asr.calls[0]
        self.assertEqual(media_type, "application/octet-stream")
        self.assertIn("voice_1", filename)

    async def test_ogg_sniff_used_when_content_type_missing(self):
        asr = _FakeASR(text="ogg")
        channel = await self._channel(
            asr=asr,
            client=_Client(resource_bytes=b"OggS-fake-opus", content_type=""),
        )
        channel._on_message(self._audio_event())
        inbound = await asyncio.wait_for(channel.bus.consume_inbound(), timeout=1)

        self.assertEqual(inbound.content, "ogg")
        data, filename, media_type = asr.calls[0]
        self.assertEqual(media_type, "audio/ogg")
        self.assertTrue(filename.endswith(".ogg"))

    async def test_group_audio_without_mention_is_ignored(self):
        channel = await self._channel(asr=_FakeASR())
        channel._on_message(
            self._audio_event(chat_type="group", mentions=[])
        )
        await asyncio.sleep(0.05)

        self.assertTrue(channel.bus.inbound_queue.empty())
        self.assertTrue(channel.bus.outbound_queue.empty())
        self.assertEqual(channel._client.download_requests, [])

    async def test_text_messages_still_flow_unchanged(self):
        """白名单加 audio 不影响 text 既有行为（image 由 test_feishu_images 覆盖）。"""
        channel = await self._channel(asr=_FakeASR())
        message = SimpleNamespace(
            message_type="text", chat_type="p2p", chat_id="chat-1",
            content=json.dumps({"text": "你好"}), mentions=[],
        )
        event = SimpleNamespace(event=SimpleNamespace(
            message=message,
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_1")
            ),
        ))
        channel._on_message(event)
        inbound = await asyncio.wait_for(channel.bus.consume_inbound(), timeout=1)

        self.assertEqual(inbound.content, "你好")
        self.assertEqual(inbound.sender_id, "chat-1:0")
        self.assertTrue(channel.bus.outbound_queue.empty())


if __name__ == "__main__":
    unittest.main()
