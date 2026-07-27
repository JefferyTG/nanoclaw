"""Bounded orchestration for one complete text-to-speech request."""

import asyncio

from voice.tts.base import TTSError, TTSProvider, TTSResult

_MIB = 1024 * 1024

# These ranges cover modern pictographic emoji, flags, modifiers, and common
# legacy symbol emoji.  They deliberately leave ordinary Unicode letters,
# Chinese characters, digits, and punctuation untouched.
_EMOJI_RANGES = (
    (0x2600, 0x27BF),
    (0x1F000, 0x1FAFF),
    (0xE0020, 0xE007F),
)
_EMOJI_JOINERS_AND_VARIANTS = frozenset({0x200D, 0x20E3, 0xFE0E, 0xFE0F})


def _strip_emoji(text: str) -> str:
    """Remove emoji presentation code points while retaining ordinary text."""

    def is_emoji_codepoint(codepoint: int) -> bool:
        return codepoint in _EMOJI_JOINERS_AND_VARIANTS or any(
            start <= codepoint <= end for start, end in _EMOJI_RANGES
        )

    kept: list[str] = []
    index = 0
    while index < len(text):
        codepoint = ord(text[index])

        # Keycap emoji are a text base (0-9, # or *) followed by an optional
        # variation selector and U+20E3.  Remove the whole sequence so "1️⃣"
        # does not become a spoken plain "1" after the combining marks vanish.
        next_index = index + 1
        if next_index < len(text) and ord(text[next_index]) == 0xFE0F:
            next_index += 1
        if (
            text[index] in "#*0123456789"
            and next_index < len(text)
            and ord(text[next_index]) == 0x20E3
        ):
            index = next_index + 1
            continue

        # Some symbols (for example ©️, ™️ and arrows) become emoji only when
        # followed by VS16.  Keep their normal text form, but remove the emoji
        # presentation sequence as a unit.
        if index + 1 < len(text) and ord(text[index + 1]) == 0xFE0F:
            index += 2
            continue

        if not is_emoji_codepoint(codepoint):
            kept.append(text[index])
        index += 1

    return "".join(kept)


class TextToSpeechService:
    """Validate and synthesize text without retaining audio on disk."""

    def __init__(
        self,
        provider: TTSProvider,
        *,
        max_text_chars: int = 4000,
        max_audio_bytes: int = 16 * _MIB,
        max_concurrency: int = 2,
        timeout_sec: float = 60,
    ) -> None:
        if (
            max_text_chars <= 0
            or max_audio_bytes <= 0
            or max_concurrency <= 0
            or timeout_sec <= 0
        ):
            raise ValueError("TTS limits must be positive")
        self.provider = provider
        self.max_text_chars = max_text_chars
        self.max_audio_bytes = max_audio_bytes
        self.timeout_sec = timeout_sec
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def synthesize(self, text: str) -> TTSResult:
        """Synthesize bounded plain text and normalize provider failures."""

        if not isinstance(text, str):
            raise TTSError("input_invalid", "语音合成文本格式无效。")
        normalized = _strip_emoji(text).strip()
        if not normalized:
            raise TTSError("input_empty", "语音合成文本为空。")
        if len(normalized) > self.max_text_chars:
            raise TTSError("input_too_long", "语音合成文本超过允许长度。")

        async with self._semaphore:
            try:
                result = await asyncio.wait_for(
                    self.provider.synthesize(normalized), timeout=self.timeout_sec
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError as exc:
                raise TTSError("timeout", "语音合成超时，请稍后重试。") from exc
            except TTSError:
                raise
            except Exception as exc:  # Do not let provider details affect chat delivery.
                raise TTSError("provider_failed", "语音合成服务暂时不可用。") from exc

        if not isinstance(result, TTSResult) or not result.audio:
            raise TTSError("empty_audio", "语音合成服务未返回音频。")
        if len(result.audio) > self.max_audio_bytes:
            raise TTSError("audio_too_large", "语音合成音频超过允许大小。")
        return result
