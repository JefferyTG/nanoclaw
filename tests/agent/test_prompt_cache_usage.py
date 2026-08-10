"""Offline coverage for OpenAI-compatible prompt-cache usage handling."""

import unittest
from types import SimpleNamespace

from providers.openai_compat import OpenAICompatProvider
from providers.usage import parse_prompt_cache_usage


class _Usage:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self):
        return self.payload


class _Create:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _provider(result):
    create = _Create(result)
    provider = object.__new__(OpenAICompatProvider)
    provider.model = "fake-model"
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=create))
    return provider, create


def _completion(usage):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content="ok", tool_calls=None, reasoning_content=None),
            finish_reason="stop",
        )],
        usage=_Usage(usage),
    )


class PromptCacheUsageTests(unittest.IsolatedAsyncioTestCase):
    def test_nested_openai_and_compatible_fields(self):
        parsed = parse_prompt_cache_usage(
            {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 40}}
        )
        self.assertTrue(parsed.available)
        self.assertEqual((parsed.input_tokens, parsed.cached_input_tokens, parsed.uncached_input_tokens), (100, 40, 60))
        self.assertEqual(parsed.cache_ratio, 0.4)

        compatible = parse_prompt_cache_usage(
            {"input_tokens": 20, "input_tokens_details": {"cached_tokens": 5}}
        )
        self.assertTrue(compatible.available)
        self.assertEqual(compatible.cached_input_tokens, 5)

    def test_missing_or_invalid_cache_data_is_unavailable(self):
        for payload in (
            {},
            {"prompt_tokens": 10},
            {"prompt_tokens": 10, "unrecognized_cached_tokens": 2},
            {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 11}},
            {"prompt_tokens": "10", "cached_tokens": 2},
        ):
            parsed = parse_prompt_cache_usage(payload)
            self.assertFalse(parsed.available)
            self.assertIsNone(parsed.cache_ratio)

    async def test_non_streaming_preserves_raw_usage_and_normalizes_cache_usage(self):
        provider, _ = _provider(_completion({"prompt_tokens": 12, "cached_input_tokens": 3}))
        response = await provider.chat([{"role": "user", "content": "hello"}])
        self.assertEqual(response.usage, {"prompt_tokens": 12, "cached_input_tokens": 3})
        self.assertEqual(response.cache_usage.uncached_input_tokens, 9)

    async def test_stream_requests_usage_and_reads_final_usage_chunk(self):
        async def stream():
            yield SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="hello", reasoning_content=None, tool_calls=None),
                    finish_reason=None,
                )],
                usage=None,
            )
            yield SimpleNamespace(choices=[], usage=_Usage({"input_tokens": 10, "input_tokens_details": {"cached_tokens": 7}}))

        provider, create = _provider(stream())
        events = [event async for event in provider.chat_stream([{"role": "user", "content": "hello"}])]
        self.assertEqual(create.calls[0]["stream_options"], {"include_usage": True})
        response = events[-1]["response"]
        self.assertEqual(response.content, "hello")
        self.assertEqual(response.cache_usage.cached_input_tokens, 7)

    async def test_stream_without_usage_remains_unavailable(self):
        async def stream():
            yield SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="hello", reasoning_content=None, tool_calls=None),
                    finish_reason="stop",
                )],
                usage=None,
            )

        provider, _ = _provider(stream())
        events = [event async for event in provider.chat_stream([{"role": "user", "content": "hello"}])]
        self.assertFalse(events[-1]["response"].cache_usage.available)

    async def test_stream_falls_back_when_compatible_sdk_rejects_usage_option(self):
        async def stream():
            yield SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="ok", reasoning_content=None, tool_calls=None),
                    finish_reason="stop",
                )],
                usage=None,
            )

        class RejectOnce:
            def __init__(self):
                self.calls = []

            async def create(self, **kwargs):
                self.calls.append(kwargs)
                if "stream_options" in kwargs:
                    raise TypeError("unexpected keyword argument 'stream_options'")
                return stream()

        create = RejectOnce()
        provider = object.__new__(OpenAICompatProvider)
        provider.model = "fake-model"
        provider._client = SimpleNamespace(chat=SimpleNamespace(completions=create))

        events = [event async for event in provider.chat_stream([
            {"role": "user", "content": "hello"}
        ])]

        self.assertEqual(len(create.calls), 2)
        self.assertIn("stream_options", create.calls[0])
        self.assertNotIn("stream_options", create.calls[1])
        self.assertEqual(events[-1]["response"].content, "ok")
        self.assertFalse(events[-1]["response"].cache_usage.available)


if __name__ == "__main__":
    unittest.main()
