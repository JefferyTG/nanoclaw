"""Privacy-safe prompt-cache telemetry for model calls and user turns.

Only token counts, deterministic hashes and structural counters are emitted.
Prompt text, messages, memory, tool arguments and credentials are never stored
or logged by this module.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Callable, Optional

from providers.usage import PromptCacheUsage


def stable_text_hash(value: str) -> str:
    """Return a short SHA-256 identifier without exposing the source text."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class CacheCallMetric:
    event: str
    turn: int
    call: int
    input_tokens: Optional[int]
    cached_input_tokens: Optional[int]
    uncached_input_tokens: Optional[int]
    cache_ratio: Optional[float]
    availability: str
    system_hash: str
    tools_hash: str
    history_messages: int
    tool_iteration: int
    phase: str


@dataclass(frozen=True, slots=True)
class CacheTurnMetric:
    event: str
    turn: int
    calls: int
    reported_calls: int
    input_tokens: Optional[int]
    cached_input_tokens: Optional[int]
    uncached_input_tokens: Optional[int]
    cache_ratio: Optional[float]
    availability: str
    system_hash: str
    tools_hash: str
    history_messages: int


class PromptCacheTurn:
    """Collect per-call cache usage and compute a weighted turn total."""

    def __init__(
        self,
        *,
        observer: "PromptCacheObserver",
        turn: int,
        system_hash: str,
        tools_hash: str,
        history_messages: int,
    ) -> None:
        self._observer = observer
        self.turn = turn
        self.system_hash = system_hash
        self.tools_hash = tools_hash
        self.history_messages = history_messages
        self.calls: list[CacheCallMetric] = []
        self.finished = False
        self._finished_metric: Optional[CacheTurnMetric] = None

    def record(
        self,
        usage: PromptCacheUsage,
        *,
        tool_iteration: int,
        phase: str = "react",
        system_hash: Optional[str] = None,
        tools_hash: Optional[str] = None,
        history_messages: Optional[int] = None,
    ) -> CacheCallMetric:
        metric = CacheCallMetric(
            event="prompt_cache_call",
            turn=self.turn,
            call=len(self.calls) + 1,
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            uncached_input_tokens=usage.uncached_input_tokens,
            cache_ratio=usage.cache_ratio,
            availability=usage.availability,
            system_hash=system_hash or self.system_hash,
            tools_hash=(tools_hash or self.tools_hash)[:16],
            history_messages=(
                self.history_messages if history_messages is None else history_messages
            ),
            tool_iteration=tool_iteration,
            phase=phase,
        )
        self.calls.append(metric)
        self._observer.calls.append(metric)
        self._observer._emit(metric)
        return metric

    def set_main_history_messages(self, count: int) -> None:
        """Update the structural count after an optional consolidation boundary."""
        self.history_messages = max(0, count)

    def finish(self) -> CacheTurnMetric:
        if self.finished:
            assert self._finished_metric is not None
            return self._finished_metric
        self.finished = True
        available = [call for call in self.calls if call.availability == "available"]
        known_input = [call.input_tokens for call in self.calls if call.input_tokens is not None]
        input_tokens = sum(known_input) if known_input else None

        # A turn ratio is only exact when every model call reported cached usage.
        # Never average call percentages: exact totals use sum(cached)/sum(input).
        if self.calls and len(available) == len(self.calls):
            cached_tokens = sum(call.cached_input_tokens or 0 for call in available)
            exact_input = sum(call.input_tokens or 0 for call in available)
            uncached_tokens = exact_input - cached_tokens
            ratio = cached_tokens / exact_input if exact_input else 0.0
            availability = "available"
            input_tokens = exact_input
        else:
            cached_tokens = None
            uncached_tokens = None
            ratio = None
            availability = "partial" if available or known_input else "unavailable"

        metric = CacheTurnMetric(
            event="prompt_cache_turn",
            turn=self.turn,
            calls=len(self.calls),
            reported_calls=len(available),
            input_tokens=input_tokens,
            cached_input_tokens=cached_tokens,
            uncached_input_tokens=uncached_tokens,
            cache_ratio=ratio,
            availability=availability,
            system_hash=self.system_hash,
            tools_hash=self.tools_hash,
            history_messages=self.history_messages,
        )
        self._observer.turns.append(metric)
        self._finished_metric = metric
        self._observer._emit(metric)
        return metric


class PromptCacheObserver:
    """Create turn collectors and emit JSON metadata to a configurable sink."""

    def __init__(self, emit: Optional[Callable[[str], None]] = None) -> None:
        self._sink = emit or print
        self._turn_sequence = 0
        self.calls: list[CacheCallMetric] = []
        self.turns: list[CacheTurnMetric] = []

    def start_turn(
        self, *, system_hash: str, tools_hash: str, history_messages: int
    ) -> PromptCacheTurn:
        self._turn_sequence += 1
        return PromptCacheTurn(
            observer=self,
            turn=self._turn_sequence,
            system_hash=system_hash,
            tools_hash=tools_hash[:16],
            history_messages=history_messages,
        )

    def _emit(self, metric: CacheCallMetric | CacheTurnMetric) -> None:
        payload = json.dumps(
            asdict(metric), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        self._sink("[prompt-cache] " + payload)
