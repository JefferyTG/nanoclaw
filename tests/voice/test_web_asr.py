"""WebChannel 的本地 ASR HTTP 桥接回归测试。"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web

from channels.web import WebChannel
import channels.web as web_module
from voice.asr.base import ASRError


class FakeUpload:
    def __init__(self, data: bytes, filename="recording.webm", content_type="audio/webm"):
        self.file = SimpleNamespace(read=lambda: data)
        self.filename = filename
        self.content_type = content_type


class FakeRequest:
    def __init__(self, data):
        self._data = data

    async def post(self):
        return self._data


class FakeASR:
    def __init__(self, result="你好"):
        self.result = result
        self.calls = []

    async def transcribe(self, data, *, filename, media_type):
        self.calls.append((data, filename, media_type))
        if isinstance(self.result, Exception):
            raise self.result
        return SimpleNamespace(text=self.result)


class FakeBus:
    def __init__(self):
        self.inbound = []

    async def publish_inbound(self, message):
        self.inbound.append(message)


class FakeWebSocket:
    def __init__(self, messages):
        self.messages = messages
        self.sent = []

    async def prepare(self, request):
        return None

    def __aiter__(self):
        async def iterator():
            for message in self.messages:
                yield message
        return iterator()

    async def send_str(self, text):
        self.sent.append(text)


class WebASRTests(unittest.IsolatedAsyncioTestCase):
    def make_channel(self, asr_service=None):
        channel = WebChannel("web", FakeBus(), "127.0.0.1", 0, None, "config.json", asr_service=asr_service)
        channel._loop = asyncio.get_running_loop()
        return channel

    async def json_body(self, response):
        return response.status, __import__("json").loads(response.text)

    async def test_asr_rejects_unconfigured_service(self):
        status, body = await self.json_body(await self.make_channel()._handle_asr(FakeRequest({})))
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "asr_unavailable")

    async def test_asr_success_uses_main_loop_service(self):
        service = FakeASR("  转写结果  ")
        status, body = await self.json_body(await self.make_channel(service)._handle_asr(
            FakeRequest({"file": FakeUpload(b"audio")})
        ))
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "text": "转写结果"})
        self.assertEqual(service.calls, [(b"audio", "recording.webm", "audio/webm")])

    async def test_asr_rejects_empty_and_oversize_uploads(self):
        channel = self.make_channel(FakeASR())
        status, body = await self.json_body(await channel._handle_asr(FakeRequest({"file": FakeUpload(b"")})))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "empty_file")
        channel.asr_service.max_audio_bytes = 3
        status, body = await self.json_body(await channel._handle_asr(
            FakeRequest({"file": FakeUpload(b"1234")})
        ))
        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "file_too_large")

    async def test_asr_error_is_structured(self):
        status, body = await self.json_body(await self.make_channel(FakeASR(ASRError("media_invalid", "解码失败")))._handle_asr(
            FakeRequest({"file": FakeUpload(b"audio")})
        ))
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], {"code": "asr_failed", "message": "解码失败"})

    async def test_plain_websocket_text_still_publishes_inbound_message(self):
        channel = self.make_channel()
        fake_ws = FakeWebSocket([SimpleNamespace(type=web.WSMsgType.TEXT, data="普通文本")])
        with patch.object(web_module.web, "WebSocketResponse", return_value=fake_ws):
            await channel._handle_ws(SimpleNamespace())
        # run_coroutine_threadsafe 先把创建任务的回调排入同一 loop；给它一个
        # 完整调度周期，模拟 Web 后台线程把消息投递到主 loop 的真实路径。
        await asyncio.sleep(0.01)
        self.assertEqual(len(channel.bus.inbound), 1)
        self.assertEqual(channel.bus.inbound[0].content, "普通文本")


if __name__ == "__main__":
    unittest.main()
