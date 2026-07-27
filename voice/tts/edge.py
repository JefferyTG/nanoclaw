"""edge-tts provider that aggregates streamed MP3 chunks in memory."""

from collections.abc import Callable
from io import BytesIO
import re
from typing import Any

import edge_tts

from voice.tts.base import TTSError, TTSProvider, TTSResult


class EdgeTTSProvider(TTSProvider):
    """Synthesize text using Microsoft's Edge TTS WebSocket service."""

    def __init__(
        self,
        *,
        voice: str = "zh-CN-XiaoxiaoNeural",
        rate: str = "+0%",
        connect_timeout_sec: int = 10,
        receive_timeout_sec: int = 60,
        max_audio_bytes: int = 16 * 1024 * 1024,
        communicate_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(voice, str) or not voice.strip():
            raise ValueError("voice must not be empty")
        if not isinstance(rate, str) or re.fullmatch(r"[+-]\d+%", rate) is None:
            raise ValueError("rate must be a signed percentage")
        if (
            type(connect_timeout_sec) is not int
            or type(receive_timeout_sec) is not int
            or connect_timeout_sec <= 0
            or receive_timeout_sec <= 0
        ):
            raise ValueError("edge-tts timeouts must be positive")
        if type(max_audio_bytes) is not int or max_audio_bytes <= 0:
            raise ValueError("max_audio_bytes must be a positive integer")
        self.voice = voice
        self.rate = rate
        self.connect_timeout_sec = connect_timeout_sec
        self.receive_timeout_sec = receive_timeout_sec
        self.max_audio_bytes = max_audio_bytes
        self._communicate_factory = communicate_factory or edge_tts.Communicate

    async def synthesize(self, text: str) -> TTSResult:
        """Collect only audio events emitted by ``edge_tts.Communicate.stream``."""

        try:
            communicate = self._communicate_factory(
                text,
                self.voice,
                rate=self.rate,
                connect_timeout=self.connect_timeout_sec,
                receive_timeout=self.receive_timeout_sec,
            )
            audio = BytesIO()
            async for chunk in communicate.stream():
                if chunk.get("type") != "audio":
                    continue
                data = chunk.get("data")
                if isinstance(data, bytes):
                    if audio.tell() + len(data) > self.max_audio_bytes:
                        raise TTSError("audio_too_large", "语音合成音频超过允许大小。")
                    audio.write(data)
            result = audio.getvalue()
        except TTSError:
            raise
        except Exception as exc:  # Provider details must not reach the web client.
            raise TTSError("provider_failed", "语音合成服务暂时不可用。") from exc

        if not result:
            raise TTSError("empty_audio", "语音合成服务未返回音频。")
        return TTSResult(audio=result)
