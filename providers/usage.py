"""Normalize token-usage payloads without retaining prompt content.

OpenAI-compatible providers disagree on both the top-level input-token name and
where prompt-cache tokens live.  This module intentionally accepts only usage
metadata and exposes a small, provider-neutral result for callers that need to
aggregate cache effectiveness.
"""

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class PromptCacheUsage:
    """Cache metrics for one model request.

    ``availability`` is ``"unavailable"`` unless all four numeric metrics can
    be derived safely.  This prevents an omitted cache field from being
    misreported as a cache miss.
    """

    input_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    uncached_input_tokens: Optional[int] = None
    cache_ratio: Optional[float] = None
    availability: str = "unavailable"

    @property
    def available(self) -> bool:
        """Whether this response can safely contribute to cache aggregation."""
        return self.availability == "available"


def usage_to_dict(usage: Any) -> dict[str, Any]:
    """Convert SDK usage models to a plain dict while preserving raw fields."""
    if usage is None:
        return {}
    if isinstance(usage, Mapping):
        return dict(usage)
    model_dump = getattr(usage, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    values = getattr(usage, "__dict__", None)
    return dict(values) if isinstance(values, Mapping) else {}


def parse_prompt_cache_usage(usage: Any) -> PromptCacheUsage:
    """Parse common OpenAI-compatible cache fields conservatively.

    Supported input fields are ``prompt_tokens`` and ``input_tokens``. Cached
    tokens may be nested under their corresponding ``*_details`` mapping or be
    returned by compatible gateways as a top-level cached-token field.
    """
    payload = usage_to_dict(usage)
    input_tokens = _first_valid_int(payload, "prompt_tokens", "input_tokens")
    cached_tokens = _cached_tokens(payload)
    if input_tokens is None:
        return PromptCacheUsage()
    if cached_tokens is None or cached_tokens > input_tokens:
        # 仍报告供应商明确给出的输入 token，但不能把“未返回 cached 字段”
        # 误记成 0 命中或据此计算 uncached/ratio。
        return PromptCacheUsage(input_tokens=input_tokens)

    uncached_tokens = input_tokens - cached_tokens
    return PromptCacheUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        uncached_input_tokens=uncached_tokens,
        cache_ratio=cached_tokens / input_tokens if input_tokens else 0.0,
        availability="available",
    )


def _cached_tokens(payload: Mapping[str, Any]) -> Optional[int]:
    for details_name in ("prompt_tokens_details", "input_tokens_details"):
        details = payload.get(details_name)
        if isinstance(details, Mapping):
            cached = _first_valid_int(details, "cached_tokens", "cached_input_tokens")
            if cached is not None:
                return cached
    return _first_valid_int(
        payload,
        "cached_tokens",
        "cached_input_tokens",
        "prompt_cache_hit_tokens",
        "cache_read_input_tokens",
    )


def _first_valid_int(payload: Mapping[str, Any], *names: str) -> Optional[int]:
    for name in names:
        value = payload.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None
