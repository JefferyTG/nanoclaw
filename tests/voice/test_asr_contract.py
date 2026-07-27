"""Offline contract tests for the Web-only ASR service.

These tests deliberately use a fake provider.  They neither execute FFmpeg nor
contact an ASR provider, but lock the public ``voice.asr`` boundary used by the
Web channel.
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from voice.asr.base import ASRAudio, ASRError, ASRProvider, ASRResult
from voice.asr.service import AudioTranscriptionService

class ASRContractTests(unittest.IsolatedAsyncioTestCase):
    """Contract for ``AudioTranscriptionService`` and its provider boundary."""

    class FakeProvider(ASRProvider):
        def __init__(self, result: ASRResult | Exception):
            self.result = result
            self.calls: list[ASRAudio] = []

        async def transcribe(self, audio: ASRAudio, **_kwargs) -> ASRResult:
            self.calls.append(audio)
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    def make_service(self, provider: ASRProvider, *, max_bytes: int = 1024):
        return AudioTranscriptionService(
            provider=provider,
            max_audio_bytes=max_bytes,
        )

    def patch_media_lifecycle(self, root: Path) -> ExitStack:
        """Avoid real FFmpeg while observing service-owned temp-directory cleanup."""

        created: list[Path] = []

        class TrackingTemporaryDirectory:
            def __init__(self, *args, **kwargs) -> None:
                self._directory = Path(tempfile.mkdtemp(dir=root, prefix="request-"))
                created.append(self._directory)

            def __enter__(self) -> str:
                return str(self._directory)

            def __exit__(self, exc_type, exc, traceback) -> None:
                __import__("shutil").rmtree(self._directory, ignore_errors=True)

        async def fake_normalize(audio: ASRAudio, *, directory: str, **_kwargs) -> ASRAudio:
            Path(directory, "normalized.wav").write_bytes(b"normalized-wav")
            return ASRAudio(b"normalized-wav", "audio.wav", "audio/wav")

        stack = ExitStack()
        stack.enter_context(
            patch("voice.asr.service.tempfile.TemporaryDirectory", TrackingTemporaryDirectory)
        )
        stack.enter_context(
            patch("voice.asr.service.media.normalize_to_pcm_wav", fake_normalize)
        )
        stack.created = created  # type: ignore[attr-defined]
        return stack

    async def test_nonempty_result_reaches_provider_and_request_temp_is_removed(self):
        with tempfile.TemporaryDirectory() as root:
            provider = self.FakeProvider(ASRResult(text="  转写结果  "))
            service = self.make_service(provider)
            with self.patch_media_lifecycle(Path(root)) as patches:
                result = await service.transcribe(
                    b"audio-bytes", filename="user-recording.webm", media_type="audio/webm"
                )

            self.assertEqual(result.text, "转写结果")
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(provider.calls[0].data, b"normalized-wav")
            self.assertEqual(provider.calls[0].filename, "audio.wav")
            self.assertEqual(provider.calls[0].media_type, "audio/wav")
            self.assertEqual(len(patches.created), 1)
            self.assertFalse(patches.created[0].exists())
            self.assertEqual(list(Path(root).iterdir()), [])

    async def test_empty_result_raises_and_request_temp_is_removed(self):
        with tempfile.TemporaryDirectory() as root:
            service = self.make_service(self.FakeProvider(ASRResult(text=" \n\t ")))
            with self.patch_media_lifecycle(Path(root)) as patches:
                with self.assertRaises(ASRError) as caught:
                    await service.transcribe(
                        b"audio-bytes", filename="recording.webm", media_type="audio/webm"
                    )
            self.assertEqual(caught.exception.category, "empty_transcript")
            self.assertEqual(len(patches.created), 1)
            self.assertFalse(patches.created[0].exists())
            self.assertEqual(list(Path(root).iterdir()), [])

    async def test_oversize_input_is_rejected_before_conversion(self):
        with tempfile.TemporaryDirectory() as root:
            provider = self.FakeProvider(ASRResult(text="unreachable"))
            service = self.make_service(provider, max_bytes=3)
            self.assertEqual(service.max_audio_bytes, 3)
            with self.assertRaises(ASRError) as caught:
                await service.transcribe(
                    b"four", filename="recording.webm", media_type="audio/webm"
                )
            self.assertEqual(caught.exception.category, "input_too_large")
            self.assertEqual(provider.calls, [])
            self.assertEqual(list(Path(root).iterdir()), [])
