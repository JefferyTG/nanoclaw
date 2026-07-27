"""WebChannel 的本地 TTS HTTP 桥接回归测试。"""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web

import channels.web as web_module
from channels.web import WebChannel
from voice.tts.base import TTSError


class FakeRequest:
    def __init__(self, body=None, error=None):
        self.body = body
        self.error = error

    async def json(self):
        if self.error:
            raise self.error
        return self.body


class FakeTTS:
    def __init__(self, result=None, *, max_text_chars=100):
        self.result = result if result is not None else SimpleNamespace(
            audio=b"fake-mp3", media_type="audio/mpeg"
        )
        self.max_text_chars = max_text_chars
        self.calls = []

    async def synthesize(self, text):
        self.calls.append(text)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


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


class WebTTSTests(unittest.IsolatedAsyncioTestCase):
    def make_channel(self, tts_service=None):
        channel = WebChannel(
            "web", FakeBus(), "127.0.0.1", 0, None, "config.json",
            tts_service=tts_service,
        )
        channel._loop = asyncio.get_running_loop()
        return channel

    async def json_body(self, response):
        return response.status, json.loads(response.text)

    async def test_tts_success_returns_audio_and_uses_main_loop_service(self):
        service = FakeTTS(SimpleNamespace(audio=b"mp3", media_type="audio/mpeg"))
        response = await self.make_channel(service)._handle_tts(FakeRequest({"text": "  你好  "}))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"mp3")
        self.assertEqual(response.content_type, "audio/mpeg")
        self.assertEqual(response.headers["Cache-Control"], "no-store, no-cache, must-revalidate, max-age=0")
        self.assertEqual(service.calls, ["你好"])

    async def test_tts_rejects_bad_empty_and_too_long_text(self):
        service = FakeTTS(max_text_chars=2)
        channel = self.make_channel(service)
        for body in (None, {}, {"text": 123}, {"text": " \t "}):
            status, result = await self.json_body(await channel._handle_tts(FakeRequest(body)))
            self.assertEqual(status, 400)
            self.assertEqual(result["error"]["code"], "tts_failed")
        status, result = await self.json_body(await channel._handle_tts(FakeRequest({"text": "过长文本"})))
        self.assertEqual(status, 413)
        self.assertEqual(result["error"]["code"], "tts_failed")
        self.assertEqual(service.calls, [])

    async def test_tts_unavailable_and_errors_are_safe(self):
        status, result = await self.json_body(await self.make_channel()._handle_tts(FakeRequest({"text": "你好"})))
        self.assertEqual(status, 503)
        self.assertEqual(result["error"]["code"], "tts_failed")

        channel = self.make_channel(FakeTTS(TTSError("provider", "安全错误")))
        status, result = await self.json_body(await channel._handle_tts(FakeRequest({"text": "你好"})))
        self.assertEqual(status, 422)
        self.assertEqual(result["error"]["message"], "安全错误")

        channel = self.make_channel(FakeTTS(RuntimeError("secret upstream details")))
        status, result = await self.json_body(await channel._handle_tts(FakeRequest({"text": "你好"})))
        self.assertEqual(status, 422)
        self.assertNotIn("secret", result["error"]["message"])

    async def test_tts_cancellation_cancels_main_loop_future(self):
        cancelled = asyncio.Event()
        started = asyncio.Event()

        class HangingTTS:
            max_text_chars = 100

            async def synthesize(self, _text):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        task = asyncio.create_task(
            self.make_channel(HangingTTS())._handle_tts(FakeRequest({"text": "你好"}))
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(cancelled.wait(), timeout=1)

    async def test_plain_websocket_chat_remains_independent_of_tts(self):
        channel = self.make_channel(FakeTTS())
        fake_ws = FakeWebSocket([SimpleNamespace(type=web.WSMsgType.TEXT, data="普通文本")])
        with patch.object(web_module.web, "WebSocketResponse", return_value=fake_ws):
            await channel._handle_ws(SimpleNamespace())
        await asyncio.sleep(0.01)
        self.assertEqual([item.content for item in channel.bus.inbound], ["普通文本"])
        self.assertEqual(channel.tts_service.calls, [])


if __name__ == "__main__":
    unittest.main()
