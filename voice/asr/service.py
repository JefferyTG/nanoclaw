"""Bounded ASR orchestration with private temporary media files."""

import asyncio
import tempfile
from typing import Optional

from voice import media
from voice.asr.base import ASRAudio, ASRError, ASRProvider, ASRResult

_MIB = 1024 * 1024


class AudioTranscriptionService:
    """Validate, normalize, and transcribe a complete audio message."""

    def __init__(
        self,
        provider: ASRProvider,
        max_audio_bytes: int = 10 * _MIB,
        max_duration_sec: float = 120,
        max_concurrency: int = 2,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        language: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> None:
        if max_audio_bytes <= 0 or max_duration_sec <= 0 or max_concurrency <= 0:
            raise ValueError("ASR limits must be positive")
        self.provider = provider
        self.max_audio_bytes = max_audio_bytes
        self.max_duration_sec = max_duration_sec
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.language = language
        self.prompt = prompt
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def transcribe(
        self, data: bytes, *, filename: str, media_type: str
    ) -> ASRResult:
        """Transcribe one untrusted channel payload without retaining media on disk."""

        if not data:
            raise ASRError("input_invalid", "音频内容为空。")
        if len(data) > self.max_audio_bytes:
            raise ASRError("input_too_large", "音频文件超过允许大小。")
        if not filename or not media_type:
            raise ASRError("input_invalid", "音频文件缺少文件名或媒体类型。")
        declared_type = media_type.split(";", 1)[0].strip().lower()
        if not (declared_type.startswith("audio/") or declared_type == "application/octet-stream"):
            raise ASRError("input_invalid", "上传内容不是支持的音频类型。")

        raw = ASRAudio(data, filename, media_type)
        async with self._semaphore:
            try:
                with tempfile.TemporaryDirectory(prefix="nanoclaw-asr-") as directory:
                    normalized = await media.normalize_to_pcm_wav(
                        raw,
                        directory=directory,
                        ffmpeg_path=self.ffmpeg_path,
                        ffprobe_path=self.ffprobe_path,
                        timeout_sec=30,
                        max_duration_sec=self.max_duration_sec,
                    )
                    result = await self.provider.transcribe(
                        normalized, language=self.language, prompt=self.prompt
                    )
            except media.MediaError as exc:
                raise ASRError(exc.category, exc.message) from exc

        text = result.text.strip()
        if not text:
            raise ASRError("empty_transcript", "语音转写未返回可用文本。")
        return ASRResult(text, language=result.language, request_id=result.request_id)
