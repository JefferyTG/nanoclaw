"""Channel-agnostic audio transcription interfaces."""

from voice.asr.base import ASRAudio, ASRError, ASRProvider, ASRResult
from voice.asr.openai_compat import OpenAICompatibleASRProvider
from voice.asr.service import AudioTranscriptionService

__all__ = [
    "ASRAudio",
    "ASRError",
    "ASRProvider",
    "ASRResult",
    "AudioTranscriptionService",
    "OpenAICompatibleASRProvider",
]
