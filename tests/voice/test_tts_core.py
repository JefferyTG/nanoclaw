"""Offline contracts for bounded edge-tts synthesis."""

import asyncio
import unittest

from voice.tts.base import TTSError, TTSProvider, TTSResult
from voice.tts.edge import EdgeTTSProvider
from voice.tts.service import TextToSpeechService


class FakeProvider(TTSProvider):
    def __init__(self, result: TTSResult | Exception | None = None) -> None:
        self.result = result or TTSResult(b"audio")
        self.calls: list[str] = []

    async def synthesize(self, text: str) -> TTSResult:
        self.calls.append(text)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class TTSCoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_strips_text_and_returns_audio(self):
        provider = FakeProvider(TTSResult(b"mp3"))
        service = TextToSpeechService(provider)

        result = await service.synthesize("  你好  ")

        self.assertEqual(provider.calls, ["你好"])
        self.assertEqual(result, TTSResult(b"mp3"))

    async def test_service_rejects_empty_and_oversize_text(self):
        provider = FakeProvider()
        service = TextToSpeechService(provider, max_text_chars=3)

        with self.assertRaises(TTSError) as empty:
            await service.synthesize(" \n ")
        with self.assertRaises(TTSError) as long:
            await service.synthesize("four")

        self.assertEqual(empty.exception.category, "input_empty")
        self.assertEqual(long.exception.category, "input_too_long")
        self.assertEqual(provider.calls, [])

    async def test_service_removes_emoji_before_calling_provider(self):
        provider = FakeProvider()
        service = TextToSpeechService(provider)

        await service.synthesize("你好👋，Hello🧑🏽‍💻！")

        self.assertEqual(provider.calls, ["你好，Hello！"])

    async def test_service_rejects_text_that_is_only_emoji_after_cleaning(self):
        provider = FakeProvider()
        service = TextToSpeechService(provider)

        with self.assertRaises(TTSError) as caught:
            await service.synthesize("  👩🏽‍💻❤️  ")

        self.assertEqual(caught.exception.category, "input_empty")
        self.assertEqual(provider.calls, [])

    async def test_service_removes_complete_keycap_and_symbol_emoji_sequences(self):
        provider = FakeProvider()
        service = TextToSpeechService(provider)

        await service.synthesize("第1项 1️⃣，版权©保留，提示ℹ️结束")

        self.assertEqual(provider.calls, ["第1项 ，版权©保留，提示结束"])

    async def test_service_enforces_timeout_and_audio_limit(self):
        class HangingProvider(TTSProvider):
            async def synthesize(self, text: str) -> TTSResult:
                await asyncio.Event().wait()

        with self.assertRaises(TTSError) as timed_out:
            await TextToSpeechService(HangingProvider(), timeout_sec=0.001).synthesize("hi")
        self.assertEqual(timed_out.exception.category, "timeout")

        with self.assertRaises(TTSError) as too_large:
            await TextToSpeechService(FakeProvider(TTSResult(b"1234")), max_audio_bytes=3).synthesize("hi")
        self.assertEqual(too_large.exception.category, "audio_too_large")

    async def test_service_propagates_cancellation(self):
        class HangingProvider(TTSProvider):
            async def synthesize(self, text: str) -> TTSResult:
                await asyncio.Event().wait()

        task = asyncio.create_task(TextToSpeechService(HangingProvider()).synthesize("hi"))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_service_bounds_provider_concurrency(self):
        entered = 0
        peak = 0
        release = asyncio.Event()

        class BlockingProvider(TTSProvider):
            async def synthesize(self, text: str) -> TTSResult:
                nonlocal entered, peak
                entered += 1
                peak = max(peak, entered)
                await release.wait()
                entered -= 1
                return TTSResult(text.encode())

        service = TextToSpeechService(BlockingProvider(), max_concurrency=2)
        tasks = [asyncio.create_task(service.synthesize(str(i))) for i in range(3)]
        for _ in range(10):
            await asyncio.sleep(0)
            if peak == 2:
                break
        self.assertEqual(peak, 2)
        release.set()
        await asyncio.gather(*tasks)

    async def test_edge_provider_collects_only_audio_and_forwards_options(self):
        captured = {}

        class Communicate:
            def __init__(self, *args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs

            async def stream(self):
                yield {"type": "WordBoundary", "offset": 0}
                yield {"type": "audio", "data": b"first"}
                yield {"type": "audio", "data": b"second"}

        provider = EdgeTTSProvider(
            voice="zh-CN-YunxiNeural",
            rate="+20%",
            connect_timeout_sec=3,
            receive_timeout_sec=8,
            communicate_factory=Communicate,
        )
        result = await provider.synthesize("hello")

        self.assertEqual(result, TTSResult(b"firstsecond"))
        self.assertEqual(captured["args"], ("hello", "zh-CN-YunxiNeural"))
        self.assertEqual(
            captured["kwargs"],
            {"rate": "+20%", "connect_timeout": 3, "receive_timeout": 8},
        )

    async def test_edge_provider_normalizes_error_and_rejects_empty_audio(self):
        class FailingCommunicate:
            def __init__(self, *args, **kwargs):
                pass

            async def stream(self):
                raise RuntimeError("provider detail")
                yield  # pragma: no cover - marks this as an async generator

        with self.assertRaises(TTSError) as failed:
            await EdgeTTSProvider(communicate_factory=FailingCommunicate).synthesize("hello")
        self.assertEqual(failed.exception.category, "provider_failed")

        class EmptyCommunicate:
            def __init__(self, *args, **kwargs):
                pass

            async def stream(self):
                yield {"type": "WordBoundary"}

        with self.assertRaises(TTSError) as empty:
            await EdgeTTSProvider(communicate_factory=EmptyCommunicate).synthesize("hello")
        self.assertEqual(empty.exception.category, "empty_audio")

    async def test_edge_provider_stops_before_oversize_audio_is_buffered(self):
        class Communicate:
            def __init__(self, *args, **kwargs):
                pass

            async def stream(self):
                yield {"type": "audio", "data": b"123"}
                yield {"type": "audio", "data": b"456"}

        provider = EdgeTTSProvider(max_audio_bytes=5, communicate_factory=Communicate)
        with self.assertRaises(TTSError) as caught:
            await provider.synthesize("hello")
        self.assertEqual(caught.exception.category, "audio_too_large")

    def test_edge_provider_validates_rate_and_integer_timeouts(self):
        with self.assertRaises(ValueError):
            EdgeTTSProvider(rate="fast")
        with self.assertRaises(ValueError):
            EdgeTTSProvider(connect_timeout_sec=10.0)
        with self.assertRaises(ValueError):
            EdgeTTSProvider(receive_timeout_sec=True)


if __name__ == "__main__":
    unittest.main()
