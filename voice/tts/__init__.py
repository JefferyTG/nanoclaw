"""Text-to-speech contracts and providers for web-only playback."""

from voice.tts.base import TTSError, TTSProvider, TTSResult
from voice.tts.edge import EdgeTTSProvider
from voice.tts.service import TextToSpeechService

__all__ = [
    "EdgeTTSProvider",
    "TTSError",
    "TTSProvider",
    "TTSResult",
    "TextToSpeechService",
]
