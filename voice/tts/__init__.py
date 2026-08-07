"""Text-to-speech contracts and providers for web-only playback."""

from voice.tts.base import TTSError, TTSProvider, TTSResult
from voice.tts.dashscope_realtime import (
    DEFAULT_MODEL,
    DEFAULT_VOICE_ID,
    DashScopeRealtimeTTSProvider,
    VoiceCloneError,
    create_voice_by_clone,
    pcm_to_wav,
)
from voice.tts.edge import EdgeTTSProvider
from voice.tts.service import TextToSpeechService

__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_VOICE_ID",
    "DashScopeRealtimeTTSProvider",
    "EdgeTTSProvider",
    "TTSError",
    "TTSProvider",
    "TTSResult",
    "TextToSpeechService",
    "VoiceCloneError",
    "create_voice_by_clone",
    "pcm_to_wav",
]
