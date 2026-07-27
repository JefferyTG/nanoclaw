"""Small, channel-independent contracts for audio transcription."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ASRAudio:
    """Audio payload ready to be sent to an ASR provider."""

    data: bytes
    filename: str
    media_type: str


@dataclass(frozen=True)
class ASRResult:
    """Normalized ASR response returned to a caller."""

    text: str
    language: Optional[str] = None
    request_id: Optional[str] = None


class ASRError(Exception):
    """A safe, categorized error suitable for channel-facing handling."""

    def __init__(
        self,
        category: str,
        message: str,
        *,
        retryable: bool = False,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.retryable = retryable
        self.status_code = status_code

    def __str__(self) -> str:
        return self.message


class ASRProvider(ABC):
    """A provider receives already-normalized audio and returns text."""

    @abstractmethod
    async def transcribe(
        self,
        audio: ASRAudio,
        *,
        language: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> ASRResult:
        """Transcribe one complete audio recording."""
        ...
