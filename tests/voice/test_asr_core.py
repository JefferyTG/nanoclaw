import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from voice import media
from voice.asr.base import ASRAudio, ASRError, ASRProvider, ASRResult
from voice.asr.openai_compat import OpenAICompatibleASRProvider
from voice.asr.service import AudioTranscriptionService


class _Provider(ASRProvider):
    def __init__(self, text="hello"):
        self.text = text
        self.calls = []

    async def transcribe(self, audio, *, language=None, prompt=None):
        self.calls.append((audio, language, prompt))
        return ASRResult(self.text)


class _Process:
    def __init__(self, stdout=b"", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.killed = False

    async def communicate(self):
        return self.stdout, b""

    def kill(self):
        self.killed = True

    def terminate(self):
        self.killed = True

    async def wait(self):
        return self.returncode


class ASRCoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_compatible_request_and_response(self):
        captured = {}

        async def handler(request):
            captured["url"] = str(request.url)
            captured["body"] = request.content
            captured["auth"] = request.headers["authorization"]
            return httpx.Response(200, json={"text": " transcript "}, headers={"x-request-id": "r1"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            provider = OpenAICompatibleASRProvider("key", "https://example.test/v1/", "model", http_client=client)
            result = await provider.transcribe(ASRAudio(b"wav", "voice.wav", "audio/wav"), language="zh", prompt="names")
        finally:
            await client.aclose()

        self.assertEqual(result.text, " transcript ")
        self.assertEqual(result.request_id, "r1")
        self.assertEqual(captured["url"], "https://example.test/v1/audio/transcriptions")
        self.assertEqual(captured["auth"], "Bearer key")
        self.assertIn(b'name="model"', captured["body"])
        self.assertIn(b'name="file"; filename="voice.wav"', captured["body"])
        self.assertNotIn(b'name="response_format"', captured["body"])

    async def test_provider_retries_rate_limit_once(self):
        attempts = 0

        async def handler(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, headers={"retry-after": "0"})
            return httpx.Response(200, json={"text": "ok"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            provider = OpenAICompatibleASRProvider("key", "https://example.test/v1", "model", max_retries=1, http_client=client)
            with patch("voice.asr.openai_compat.asyncio.sleep", new=AsyncMock()):
                result = await provider.transcribe(ASRAudio(b"x", "a.wav", "audio/wav"))
        finally:
            await client.aclose()
        self.assertEqual(result.text, "ok")
        self.assertEqual(attempts, 2)

    async def test_provider_classifies_authentication_error(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(401)))
        try:
            provider = OpenAICompatibleASRProvider("key", "https://example.test/v1", "model", http_client=client)
            with self.assertRaisesRegex(ASRError, "HTTP 401") as caught:
                await provider.transcribe(ASRAudio(b"x", "a.wav", "audio/wav"))
        finally:
            await client.aclose()
        self.assertEqual(caught.exception.category, "authentication")

    async def test_provider_does_not_retry_unknown_network_delivery(self):
        attempts = 0

        async def handler(request):
            nonlocal attempts
            attempts += 1
            raise httpx.ReadTimeout("timed out", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            provider = OpenAICompatibleASRProvider(
                "key", "https://example.test/v1", "model", max_retries=3, http_client=client
            )
            with self.assertRaises(ASRError) as caught:
                await provider.transcribe(ASRAudio(b"x", "a.wav", "audio/wav"))
        finally:
            await client.aclose()
        self.assertEqual(caught.exception.category, "network")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(attempts, 1)

    async def test_media_normalizes_with_mocked_subprocess(self):
        commands = []

        async def create_process(*args, **kwargs):
            commands.append(args)
            if args[0] == "ffprobe":
                return _Process(json.dumps({"streams": [{"index": 0}], "format": {"duration": "1.25"}}).encode())
            Path(args[-1]).write_bytes(b"RIFF....WAVE")
            return _Process()

        with tempfile.TemporaryDirectory() as directory:
            with patch("voice.media.asyncio.create_subprocess_exec", side_effect=create_process):
                audio = await media.normalize_to_pcm_wav(
                    ASRAudio(b"source", "voice.webm", "audio/webm"),
                    directory=directory,
                    ffmpeg_path="ffmpeg",
                    ffprobe_path="ffprobe",
                    timeout_sec=1,
                    max_duration_sec=2,
                )
        self.assertEqual(audio.filename, "audio.wav")
        self.assertEqual(audio.media_type, "audio/wav")
        self.assertEqual(audio.data, b"RIFF....WAVE")
        self.assertIn("-select_streams", commands[0])
        self.assertEqual(commands[0][commands[0].index("-select_streams") + 1], "a:0")

    async def test_cancelled_media_process_is_reaped(self):
        class HangingProcess:
            def __init__(self):
                self.returncode = None
                self.terminated = False

            async def communicate(self):
                await asyncio.Event().wait()

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def kill(self):
                self.returncode = -9

            async def wait(self):
                return self.returncode

        process = HangingProcess()
        with patch("voice.media.asyncio.create_subprocess_exec", return_value=process):
            task = asyncio.create_task(
                media._run(["ffmpeg"], timeout_sec=30, failure_category="media_invalid")
            )
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.terminated)

    async def test_timed_out_media_process_is_reaped(self):
        class HangingProcess:
            def __init__(self):
                self.returncode = None
                self.terminated = False

            async def communicate(self):
                await asyncio.Event().wait()

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def kill(self):
                self.returncode = -9

            async def wait(self):
                return self.returncode

        process = HangingProcess()
        with patch("voice.media.asyncio.create_subprocess_exec", return_value=process):
            with self.assertRaises(media.MediaError) as caught:
                await media._run(
                    ["ffmpeg"], timeout_sec=0.001, failure_category="media_invalid"
                )
        self.assertEqual(caught.exception.category, "media_timeout")
        self.assertTrue(process.terminated)

    async def test_nonzero_media_process_returns_safe_error(self):
        process = _Process(returncode=1)
        with patch("voice.media.asyncio.create_subprocess_exec", return_value=process):
            with self.assertRaises(media.MediaError) as caught:
                await media._run(
                    ["ffprobe"], timeout_sec=1, failure_category="media_invalid"
                )
        self.assertEqual(caught.exception.category, "media_invalid")
        self.assertEqual(str(caught.exception), "音频文件无法处理。")

    async def test_media_rejects_duration_before_ffmpeg(self):
        calls = []

        async def create_process(*args, **kwargs):
            calls.append(args[0])
            return _Process(
                json.dumps(
                    {"streams": [{"index": 0}], "format": {"duration": "3.0"}}
                ).encode()
            )

        with tempfile.TemporaryDirectory() as directory:
            with patch("voice.media.asyncio.create_subprocess_exec", side_effect=create_process):
                with self.assertRaises(media.MediaError) as caught:
                    await media.normalize_to_pcm_wav(
                        ASRAudio(b"source", "voice.webm", "audio/webm"),
                        directory=directory,
                        ffmpeg_path="ffmpeg",
                        ffprobe_path="ffprobe",
                        timeout_sec=1,
                        max_duration_sec=2,
                    )
        self.assertEqual(caught.exception.category, "input_too_long")
        self.assertEqual(calls, ["ffprobe"])

    async def test_service_enforces_size_and_rejects_empty_text(self):
        provider = _Provider("")
        service = AudioTranscriptionService(provider, max_audio_bytes=3)
        with self.assertRaises(ASRError) as too_large:
            await service.transcribe(b"1234", filename="a.wav", media_type="audio/wav")
        self.assertEqual(too_large.exception.category, "input_too_large")

        with patch("voice.asr.service.media.normalize_to_pcm_wav", new=AsyncMock(return_value=ASRAudio(b"wav", "audio.wav", "audio/wav"))):
            with self.assertRaises(ASRError) as empty:
                await service.transcribe(b"123", filename="a.wav", media_type="audio/wav")
        self.assertEqual(empty.exception.category, "empty_transcript")

    async def test_service_passes_normalized_audio_and_options(self):
        provider = _Provider("  hello  ")
        service = AudioTranscriptionService(provider, language="zh", prompt="NanoClaw")
        normalized = ASRAudio(b"wav", "audio.wav", "audio/wav")
        directories = []

        async def normalize(*args, directory, **kwargs):
            directories.append(directory)
            return normalized

        with patch("voice.asr.service.media.normalize_to_pcm_wav", side_effect=normalize):
            result = await service.transcribe(b"source", filename="voice.webm", media_type="audio/webm")
        self.assertEqual(result.text, "hello")
        self.assertEqual(provider.calls, [(normalized, "zh", "NanoClaw")])
        self.assertFalse(Path(directories[0]).exists())


if __name__ == "__main__":
    unittest.main()
