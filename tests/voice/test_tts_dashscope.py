"""DashScope 甘雨音色流式 TTS provider 与录音复刻接口的离线单元测试。

全部使用 fake realtime factory / fake post，不真连 WebSocket、不调复刻 API，
不触发任何真实 DashScope 请求。
"""

import asyncio
import base64
import struct
import unittest
from types import SimpleNamespace

from voice.tts.base import TTSError
from voice.tts.dashscope_realtime import (
    CUSTOMIZATION_URL,
    DEFAULT_MODEL,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_VOICE_ID,
    DashScopeRealtimeTTSProvider,
    VoiceCloneError,
    create_voice_by_clone,
    pcm_to_wav,
)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class FakeRealtimeClient:
    """模拟 QwenTtsRealtime 的同步接口；事件由自身按剧本触发。"""

    def __init__(
        self,
        callback=None,
        *,
        audio_chunks=(),
        fail_on=None,
        never_ready=False,
        done_event=True,
    ):
        self.callback = callback
        self.audio_chunks = audio_chunks
        self.fail_on = fail_on
        self.never_ready = never_ready
        self.done_event = done_event
        self.calls = []
        self.closed = False

    def connect(self):
        self.calls.append("connect")
        self.callback.on_open()
        if not self.never_ready:
            self.callback.on_event(
                {"type": "session.created", "session": {"id": "sess_fake"}}
            )

    def update_session(self, **kwargs):
        self.calls.append(("update_session", kwargs))
        if not self.never_ready:
            self.callback.on_event({"type": "session.updated"})

    def append_text(self, text):
        self.calls.append(("append_text", text))
        if self.fail_on == "append":
            self.callback.on_event(
                {"type": "error", "error": {"message": "boom"}}
            )

    def commit(self):
        self.calls.append("commit")
        if self.fail_on == "commit":
            self.callback.on_event(
                {"type": "error", "error": {"message": "boom"}}
            )
            return
        for chunk in self.audio_chunks:
            self.callback.on_event(
                {"type": "response.audio.delta", "delta": _b64(chunk)}
            )
        if self.done_event:
            self.callback.on_event({"type": "response.audio.done"})
            self.callback.on_event({"type": "response.done"})

    def finish(self):
        self.calls.append("finish")
        self.callback.on_event({"type": "session.finished"})

    def close(self):
        self.closed = True
        self.calls.append("close")


class DashScopeTTSTests(unittest.IsolatedAsyncioTestCase):
    def make_provider(self, client, **overrides):
        captured = []

        def factory(*, model, callback):
            captured.append((model, callback))
            client.callback = callback
            return client

        provider = DashScopeRealtimeTTSProvider(
            api_key="sk-ws-test",
            voice_id=DEFAULT_VOICE_ID,
            model=DEFAULT_MODEL,
            realtime_factory=factory,
            **overrides,
        )
        return provider, captured

    async def test_normal_synthesis_returns_wav(self):
        pcm = bytes(range(256)) * 8
        client = FakeRealtimeClient(audio_chunks=(pcm, pcm))
        provider, captured = self.make_provider(client)

        result = await provider.synthesize("你好，世界")

        # 返回 WAV：RIFF 头 + 44 字节头 + 完整 PCM
        self.assertEqual(result.media_type, "audio/wav")
        self.assertEqual(result.audio[:4], b"RIFF")
        self.assertEqual(len(result.audio), 44 + len(pcm) * 2)
        fmt = struct.unpack("<4sI4s4sIHHIIHH4sI", result.audio[:44])
        self.assertEqual(fmt[6], 1)  # channels=mono
        self.assertEqual(fmt[7], DEFAULT_SAMPLE_RATE)  # sample_rate=24000
        self.assertEqual(fmt[10], 16)  # bits=16
        self.assertEqual(result.audio[44:], pcm + pcm)

        # 会话顺序：connect → update_session(commit) → append_text → commit
        self.assertEqual(captured[0][0], DEFAULT_MODEL)
        kinds = [c if isinstance(c, str) else c[0] for c in client.calls]
        self.assertEqual(kinds[:4], ["connect", "update_session", "append_text", "commit"])
        self.assertIn("finish", kinds)
        self.assertIn("close", kinds)
        update_kwargs = next(
            c[1] for c in client.calls
            if isinstance(c, tuple) and c[0] == "update_session"
        )
        self.assertEqual(update_kwargs["voice"], DEFAULT_VOICE_ID)
        self.assertEqual(update_kwargs["mode"], "commit")
        self.assertEqual(update_kwargs["sample_rate"], DEFAULT_SAMPLE_RATE)
        append_text = next(
            c[1] for c in client.calls
            if isinstance(c, tuple) and c[0] == "append_text"
        )
        self.assertEqual(append_text, "你好，世界")

    async def test_empty_text_is_rejected_without_client(self):
        client = FakeRealtimeClient()
        provider, _ = self.make_provider(client)

        with self.assertRaises(TTSError) as caught:
            await provider.synthesize("   ")
        self.assertEqual(caught.exception.category, "input_empty")
        self.assertEqual(client.calls, [])

    async def test_provider_failure_from_error_event(self):
        client = FakeRealtimeClient(fail_on="append")
        provider, _ = self.make_provider(client)

        with self.assertRaises(TTSError) as caught:
            await provider.synthesize("你好")
        self.assertEqual(caught.exception.category, "provider_failed")

    async def test_provider_failure_when_session_never_ready(self):
        client = FakeRealtimeClient(never_ready=True)
        provider, _ = self.make_provider(client, session_ready_timeout_sec=0.05)

        with self.assertRaises(TTSError) as caught:
            await provider.synthesize("你好")
        self.assertEqual(caught.exception.category, "provider_failed")

    async def test_timeout_when_no_completion_event(self):
        # 音频已流出但服务端始终不返回 response.done：等待总体超时
        client = FakeRealtimeClient(audio_chunks=(b"pcm",), done_event=False)
        provider, _ = self.make_provider(client, overall_timeout_sec=0.1)

        with self.assertRaises(TTSError) as caught:
            await provider.synthesize("你好")
        self.assertEqual(caught.exception.category, "timeout")

    async def test_audio_too_large_is_rejected_during_streaming(self):
        client = FakeRealtimeClient(audio_chunks=(b"123", b"456"))
        provider, _ = self.make_provider(client, max_audio_bytes=5)

        with self.assertRaises(TTSError) as caught:
            await provider.synthesize("你好")
        self.assertEqual(caught.exception.category, "audio_too_large")

    async def test_empty_audio_after_session_is_error(self):
        # 服务端正常结束但没有任何音频字节
        client = FakeRealtimeClient(audio_chunks=(), done_event=True)
        provider, _ = self.make_provider(client)

        with self.assertRaises(TTSError) as caught:
            await provider.synthesize("你好")
        self.assertEqual(caught.exception.category, "empty_audio")

    async def test_concurrent_synthesis_is_isolated(self):
        captured = []

        def factory(*, model, callback):
            client = FakeRealtimeClient(audio_chunks=(b"pcm-a",))
            client.callback = callback
            captured.append(client)
            return client

        provider = DashScopeRealtimeTTSProvider(
            api_key="sk-ws-test",
            voice_id=DEFAULT_VOICE_ID,
            realtime_factory=factory,
        )

        results = await asyncio.gather(
            *[provider.synthesize("并发合成") for _ in range(3)]
        )

        self.assertEqual(len(results), 3)
        for result in results:
            self.assertTrue(result.audio.startswith(b"RIFF"))
        # 每次合成使用独立 client / callback，互不共享状态
        self.assertEqual(len(captured), 3)
        self.assertEqual(len({id(c) for c in captured}), 3)

    async def test_voice_id_is_read_per_call(self):
        client = FakeRealtimeClient(audio_chunks=(b"pcm",))
        provider, _ = self.make_provider(client)

        await provider.synthesize("一")
        # 运行时切换音色（复刻返回的新 voice_id）立即生效
        provider.voice_id = "qwen-tts-vc-myclone-voice-new-00000000000000-0000"
        await provider.synthesize("二")

        voices = [
            c[1]["voice"]
            for c in client.calls
            if isinstance(c, tuple) and c[0] == "update_session"
        ]
        self.assertEqual(voices[0], DEFAULT_VOICE_ID)
        self.assertEqual(
            voices[1], "qwen-tts-vc-myclone-voice-new-00000000000000-0000"
        )

    async def test_api_key_injected_before_client_construction(self):
        # SDK 在构造 QwenTtsRealtime 时快照 dashscope.api_key，注入必须先于 factory。
        import dashscope

        recorded = {}

        def factory(*, model, callback):
            recorded["api_key_at_construction"] = dashscope.api_key
            client = FakeRealtimeClient(audio_chunks=(b"pcm",))
            client.callback = callback
            return client

        provider = DashScopeRealtimeTTSProvider(
            api_key="sk-ws-order-check",
            voice_id=DEFAULT_VOICE_ID,
            realtime_factory=factory,
        )
        previous = dashscope.api_key
        dashscope.api_key = ""  # 清掉全局，验证确实是 provider 注入
        try:
            result = await provider.synthesize("顺序校验")
            self.assertTrue(result.audio.startswith(b"RIFF"))
            self.assertEqual(
                recorded["api_key_at_construction"], "sk-ws-order-check"
            )
        finally:
            dashscope.api_key = previous

    def test_constructor_validates_inputs(self):
        with self.assertRaises(ValueError):
            DashScopeRealtimeTTSProvider(api_key="", voice_id="v")
        with self.assertRaises(ValueError):
            DashScopeRealtimeTTSProvider(api_key="k", voice_id="")
        with self.assertRaises(ValueError):
            DashScopeRealtimeTTSProvider(api_key="k", voice_id="v", sample_rate=0)
        with self.assertRaises(ValueError):
            DashScopeRealtimeTTSProvider(
                api_key="k", voice_id="v", overall_timeout_sec=0
            )
        with self.assertRaises(ValueError):
            DashScopeRealtimeTTSProvider(
                api_key="k", voice_id="v", max_audio_bytes=0
            )

    def test_pcm_to_wav_header(self):
        wav = pcm_to_wav(b"\x00" * 10)
        self.assertEqual(len(wav), 54)
        self.assertEqual(wav[:4], b"RIFF")
        fmt = struct.unpack("<4sI4s4sIHHIIHH4sI", wav[:44])
        self.assertEqual(fmt[7], DEFAULT_SAMPLE_RATE)


class VoiceCloneTests(unittest.IsolatedAsyncioTestCase):
    async def test_clone_returns_voice_id(self):
        audio = b"fake-recording-bytes"

        async def fake_post(url, *, json, headers, timeout_sec):
            self.assertEqual(url, CUSTOMIZATION_URL)
            self.assertEqual(json["model"], "qwen-voice-enrollment")
            self.assertEqual(json["input"]["action"], "create")
            self.assertEqual(json["input"]["target_model"], DEFAULT_MODEL)
            self.assertTrue(
                json["input"]["audio"]["data"].startswith(
                    "data:audio/mpeg;base64,"
                )
            )
            self.assertEqual(headers["Authorization"], "Bearer sk-ws-test")
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"output": {"voice": "voice_abc"}},
                text="",
            )

        voice_id = await create_voice_by_clone(
            audio, api_key="sk-ws-test", post=fake_post
        )
        self.assertEqual(voice_id, "voice_abc")

    async def test_clone_accepts_voice_id_field(self):
        async def fake_post(url, *, json, headers, timeout_sec):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"output": {"voice_id": "voice_xyz"}},
                text="",
            )

        voice_id = await create_voice_by_clone(
            b"x", api_key="sk-ws-test", post=fake_post
        )
        self.assertEqual(voice_id, "voice_xyz")

    async def test_clone_http_error_raises(self):
        async def fake_post(url, *, json, headers, timeout_sec):
            return SimpleNamespace(
                status_code=400, json=lambda: {}, text="bad request"
            )

        with self.assertRaises(VoiceCloneError) as caught:
            await create_voice_by_clone(b"x", api_key="sk-ws-test", post=fake_post)
        self.assertIn("400", str(caught.exception))

    async def test_clone_missing_voice_id_raises(self):
        async def fake_post(url, *, json, headers, timeout_sec):
            return SimpleNamespace(
                status_code=200, json=lambda: {"output": {}}, text=""
            )

        with self.assertRaises(VoiceCloneError):
            await create_voice_by_clone(b"x", api_key="sk-ws-test", post=fake_post)

    async def test_clone_network_error_raises(self):
        async def fake_post(url, *, json, headers, timeout_sec):
            raise RuntimeError("connection refused")

        with self.assertRaises(VoiceCloneError):
            await create_voice_by_clone(b"x", api_key="sk-ws-test", post=fake_post)

    async def test_clone_rejects_empty_audio(self):
        async def unused(*args, **kwargs):
            raise AssertionError("不应发起网络请求")

        with self.assertRaises(ValueError):
            await create_voice_by_clone(b"", api_key="sk-ws-test", post=unused)


if __name__ == "__main__":
    unittest.main()
