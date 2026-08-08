"""飞书出站语音（语音对语音）测试（TASK-021）。

mock 风格与 tests/test_feishu_audio.py / tests/test_feishu_images.py 对齐：
mock ``_Client`` 的下载 / 上传 / 发送三个入口，mock 假 ASR 与假 TTS service。
OPUS 转换走本机真实 ffmpeg（libopus 已确认可用），绝不触发真实 DashScope
付费 TTS 调用。失败路径（合成 / 转换 / 上传 / 发送）逐一验证文字兜底。
"""

import asyncio
import io
import json
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import patch

from bus.queue import MessageBus, OutboundMessage
from channels.feishu import FeishuChannel
from voice.asr.base import ASRError
from voice.media import MediaError
from voice.tts.base import TTSResult, TTSError


def _make_wav(duration_sec: float = 0.1, sample_rate: int = 16000) -> bytes:
    """生成一段合法的极小 PCM WAV，供真实 ffmpeg 转 OPUS 使用。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * int(sample_rate * duration_sec))
    return buf.getvalue()


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


class _FakeTTS:
    """假 TTS 服务：记录合成文本，可按需抛错或返回固定音频。"""

    def __init__(self, audio=_make_wav(), media_type="audio/wav", error=None):
        self.audio = audio
        self.media_type = media_type
        self.error = error
        self.calls = []

    async def synthesize(self, text):
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        if not self.audio:
            raise TTSError("empty_audio", "语音合成服务未返回音频。")
        return TTSResult(audio=self.audio, media_type=self.media_type)


class _Client:
    """同时覆盖入站语音下载 + 出站音频上传 / 消息发送的假 IM client。"""

    def __init__(
        self,
        *,
        resource_bytes=b"fake-opus-bytes",
        content_type="audio/opus",
        upload_failure=False,
        send_failure=False,
    ):
        self.download_requests = []
        self.upload_requests = []
        self.message_requests = []
        self.resource_bytes = resource_bytes
        self.content_type = content_type
        self.upload_failure = upload_failure
        self.send_failure = send_failure
        self.im = SimpleNamespace(
            v1=SimpleNamespace(
                message_resource=SimpleNamespace(get=self.get_resource),
                file=SimpleNamespace(create=self.create_file),
                message=SimpleNamespace(create=self.create_message),
            )
        )

    def get_resource(self, request):
        self.download_requests.append(request)
        return SimpleNamespace(
            success=lambda: True,
            code=0,
            msg="ok",
            file=SimpleNamespace(read=lambda: self.resource_bytes),
            raw=SimpleNamespace(headers={"content-type": self.content_type}),
        )

    def create_file(self, request):
        self.upload_requests.append(request)
        if self.upload_failure:
            return SimpleNamespace(
                success=lambda: False, code=99998, msg="upload failed", data=None
            )
        return SimpleNamespace(
            success=lambda: True,
            code=0,
            msg="ok",
            data=SimpleNamespace(file_key="feishu-audio-key"),
        )

    def create_message(self, request):
        self.message_requests.append(request)
        if self.send_failure:
            return SimpleNamespace(
                success=lambda: False, code=99997, msg="send failed"
            )
        return SimpleNamespace(
            success=lambda: True,
            code=0,
            msg="ok",
            data=SimpleNamespace(message_id="om_out_1"),
        )


class FeishuVoiceReplyTests(unittest.IsolatedAsyncioTestCase):
    async def _channel(
        self, asr=None, tts=None, client=None, max_voice_chars: int = 300
    ):
        channel = FeishuChannel(
            "feishu",
            MessageBus(),
            "id",
            "secret",
            asr_service=asr,
            tts_service=tts,
            max_voice_chars=max_voice_chars,
        )
        channel._loop = asyncio.get_running_loop()
        channel._client = client or _Client()
        return channel

    def _audio_event(
        self,
        *,
        chat_id="chat-1",
        message_id="om_audio_1",
        file_key="voice_1",
        sender_open_id="ou_1",
    ):
        content = {"file_key": file_key} if file_key is not None else {}
        message = SimpleNamespace(
            message_type="audio",
            chat_type="p2p",
            chat_id=chat_id,
            message_id=message_id,
            content=json.dumps(content),
            mentions=[],
        )
        sender = SimpleNamespace(
            sender_id=SimpleNamespace(open_id=sender_open_id)
        )
        return SimpleNamespace(
            event=SimpleNamespace(message=message, sender=sender)
        )

    def _text_event(self, text="你好", chat_id="chat-1"):
        message = SimpleNamespace(
            message_type="text",
            chat_type="p2p",
            chat_id=chat_id,
            content=json.dumps({"text": text}),
            mentions=[],
        )
        return SimpleNamespace(
            event=SimpleNamespace(
                message=message,
                sender=SimpleNamespace(
                    sender_id=SimpleNamespace(open_id="ou_1")
                ),
            )
        )

    # —— 语音入站 → 出站语音（语音对语音）——

    async def test_voice_inbound_then_reply_is_audio(self):
        asr = _FakeASR(text="帮我看看这个")
        tts = _FakeTTS()
        channel = await self._channel(asr=asr, tts=tts)

        channel._on_message(self._audio_event())
        inbound = await asyncio.wait_for(channel.bus.consume_inbound(), timeout=1)
        self.assertEqual(inbound.content, "帮我看看这个")
        # 语音入站成功转写 → 标记待发语音回复
        self.assertIn("chat-1", channel._voice_reply_pending)

        result = await channel.send(
            OutboundMessage("feishu", "chat-1", "好的，我帮你看看。", None)
        )
        self.assertTrue(result.success)
        requests = channel._client.message_requests
        self.assertEqual([r.request_body.msg_type for r in requests], ["audio"])
        self.assertEqual(
            json.loads(requests[0].request_body.content),
            {"file_key": "feishu-audio-key"},
        )
        # 只上传一次 OPUS 音频，且不再发文字
        self.assertEqual(len(channel._client.upload_requests), 1)
        upload = channel._client.upload_requests[0]
        self.assertEqual(upload.request_body.file_type, "opus")
        # TTS 合成的是 Agent 回复文本（不是 ASR 转写文本）
        self.assertEqual(tts.calls, ["好的，我帮你看看。"])

    async def test_asr_failure_does_not_set_voice_reply_marker(self):
        channel = await self._channel(
            asr=_FakeASR(error=ASRError("provider_down", "上游服务暂时不可用。")),
            tts=_FakeTTS(),
        )
        channel._on_message(self._audio_event())
        outbound = await asyncio.wait_for(channel.bus.consume_outbound(), timeout=1)

        self.assertIn("⚠️ 语音转写失败", outbound.content)
        self.assertEqual(channel._voice_reply_pending, set())

    # —— 语音标记消费与清除 ——

    async def test_marker_is_consumed_after_one_voice_reply(self):
        tts = _FakeTTS()
        channel = await self._channel(asr=_FakeASR(), tts=tts)
        channel._voice_reply_pending.add("chat-1")

        await channel.send(
            OutboundMessage("feishu", "chat-1", "第一次回复", None)
        )
        self.assertNotIn("chat-1", channel._voice_reply_pending)

        await channel.send(
            OutboundMessage("feishu", "chat-1", "第二次回复", None)
        )
        requests = channel._client.message_requests
        self.assertEqual(
            [r.request_body.msg_type for r in requests], ["audio", "text"]
        )
        self.assertEqual(
            json.loads(requests[1].request_body.content),
            {"text": "第二次回复"},
        )

    # —— 兜底：失败一律回文字原文，不静默、不崩溃 ——

    async def test_tts_synthesis_failure_falls_back_to_text(self):
        tts = _FakeTTS(error=TTSError("provider_failed", "语音合成服务暂时不可用。"))
        channel = await self._channel(asr=_FakeASR(), tts=tts)
        channel._voice_reply_pending.add("chat-1")

        await channel.send(
            OutboundMessage("feishu", "chat-1", "回复文字原文", None)
        )
        requests = channel._client.message_requests
        self.assertEqual([r.request_body.msg_type for r in requests], ["text"])
        self.assertEqual(
            json.loads(requests[0].request_body.content), {"text": "回复文字原文"}
        )
        self.assertEqual(channel._client.upload_requests, [])
        self.assertNotIn("chat-1", channel._voice_reply_pending)

    async def test_opus_conversion_failure_falls_back_to_text(self):
        channel = await self._channel(asr=_FakeASR(), tts=_FakeTTS())
        channel._voice_reply_pending.add("chat-1")

        with patch(
            "channels.feishu.encode_to_opus",
            side_effect=MediaError("media_invalid", "音频转换失败"),
        ):
            await channel.send(
                OutboundMessage("feishu", "chat-1", "回复文字原文", None)
            )
        requests = channel._client.message_requests
        self.assertEqual([r.request_body.msg_type for r in requests], ["text"])
        self.assertEqual(channel._client.upload_requests, [])
        self.assertEqual(
            json.loads(requests[0].request_body.content), {"text": "回复文字原文"}
        )

    async def test_upload_failure_falls_back_to_text(self):
        channel = await self._channel(
            asr=_FakeASR(),
            tts=_FakeTTS(),
            client=_Client(upload_failure=True),
        )
        channel._voice_reply_pending.add("chat-1")

        await channel.send(
            OutboundMessage("feishu", "chat-1", "回复文字原文", None)
        )
        # 上传确实尝试过，但最终回文字
        self.assertEqual(len(channel._client.upload_requests), 1)
        requests = channel._client.message_requests
        self.assertEqual([r.request_body.msg_type for r in requests], ["text"])
        self.assertEqual(
            json.loads(requests[0].request_body.content), {"text": "回复文字原文"}
        )

    async def test_send_failure_falls_back_to_text(self):
        channel = await self._channel(
            asr=_FakeASR(),
            tts=_FakeTTS(),
            client=_Client(send_failure=True),
        )
        channel._voice_reply_pending.add("chat-1")

        await channel.send(
            OutboundMessage("feishu", "chat-1", "回复文字原文", None)
        )
        requests = channel._client.message_requests
        self.assertEqual(
            [r.request_body.msg_type for r in requests], ["audio", "text"]
        )
        self.assertEqual(
            json.loads(requests[1].request_body.content), {"text": "回复文字原文"}
        )

    # —— 长文本 / tts 未注入 ——

    async def test_long_text_reply_stays_text(self):
        tts = _FakeTTS()
        channel = await self._channel(
            asr=_FakeASR(), tts=tts, max_voice_chars=10
        )
        channel._voice_reply_pending.add("chat-1")

        long_text = "这是一段超过十个字的长文本回复内容"
        await channel.send(
            OutboundMessage("feishu", "chat-1", long_text, None)
        )
        # 不硬转语音：TTS 未收到文本，只发文字
        self.assertEqual(tts.calls, [])
        requests = channel._client.message_requests
        self.assertEqual([r.request_body.msg_type for r in requests], ["text"])
        self.assertEqual(
            json.loads(requests[0].request_body.content), {"text": long_text}
        )
        # 标记仍被单次消费，不残留
        self.assertNotIn("chat-1", channel._voice_reply_pending)

    async def test_tts_service_none_reply_stays_text(self):
        channel = await self._channel(asr=_FakeASR(), tts=None)
        channel._voice_reply_pending.add("chat-1")

        await channel.send(
            OutboundMessage("feishu", "chat-1", "回复文字原文", None)
        )
        requests = channel._client.message_requests
        self.assertEqual([r.request_body.msg_type for r in requests], ["text"])
        self.assertEqual(
            json.loads(requests[0].request_body.content), {"text": "回复文字原文"}
        )
        self.assertEqual(channel._client.upload_requests, [])
        self.assertNotIn("chat-1", channel._voice_reply_pending)

    # —— 不回归 ——

    async def test_text_inbound_reply_stays_text(self):
        tts = _FakeTTS()
        channel = await self._channel(asr=_FakeASR(), tts=tts)
        channel._on_message(self._text_event("你好"))
        inbound = await asyncio.wait_for(channel.bus.consume_inbound(), timeout=1)

        self.assertEqual(inbound.content, "你好")
        # 文字入站不设语音标记
        self.assertNotIn("chat-1", channel._voice_reply_pending)
        await channel.send(
            OutboundMessage("feishu", "chat-1", "你好呀", None)
        )
        requests = channel._client.message_requests
        self.assertEqual([r.request_body.msg_type for r in requests], ["text"])
        # 文字回复不触发 TTS / 不尝试上传
        self.assertEqual(tts.calls, [])
        self.assertEqual(channel._client.upload_requests, [])

    async def test_voice_inbound_without_pending_reply_keeps_existing_text_flow(self):
        """语音入站后回复未命中标记（如标记已被消费）时，普通文字回复不回归。"""
        channel = await self._channel(asr=_FakeASR(), tts=_FakeTTS())

        await channel.send(
            OutboundMessage("feishu", "chat-1", "普通文字回复", None)
        )
        requests = channel._client.message_requests
        self.assertEqual([r.request_body.msg_type for r in requests], ["text"])


if __name__ == "__main__":
    unittest.main()
