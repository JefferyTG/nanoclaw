"""Small, channel-independent contracts for text-to-speech."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class TTSResult:
    """Synthesized audio ready for an HTTP response."""

    audio: bytes
    media_type: str = "audio/mpeg"


class TTSError(Exception):
    """A safe, categorized error suitable for channel-facing handling."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message

    def __str__(self) -> str:
        return self.message


class TTSProvider(ABC):
    """A provider converts one complete text string into audio."""

    @abstractmethod
    async def synthesize(self, text: str) -> TTSResult:
        """Synthesize a complete response without retaining it on disk."""
        ...
