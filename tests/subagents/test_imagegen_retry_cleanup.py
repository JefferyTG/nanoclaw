import asyncio
import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from agent.tools.imagegen import GenerateImageTool
from agent.tools.registry import ToolRegistry


class _ImageStore:
    def __init__(self):
        self.saved = []

    def save(self, session_key, raw, ext, mime):
        self.saved.append((session_key, raw, ext, mime))
        return SimpleNamespace(id="generated-1", mime=mime)


def _config(**image_overrides):
    image = {
        "api_key": "key",
        "base_url": "https://image.example.test/v1",
        "model": "test-model",
        "timeout_sec": 1,
        "total_timeout_sec": 10,
    }
    image.update(image_overrides)
    return SimpleNamespace(workspace=".", image_gen_model=image)


class _HangingClient:
    def __init__(self, close_delay=0):
        self.entered = asyncio.Event()
        self.closed = False
        self.close_delay = close_delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        self.closed = True

    async def post(self, *args, **kwargs):
        self.entered.set()
        await asyncio.Event().wait()


class GenerateImageRetryCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_transient_network_timeout_then_succeeds(self):
        attempts = 0

        async def handler(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ReadTimeout("temporary timeout", request=request)
            return httpx.Response(
                200,
                json={"data": [{"b64_json": base64.b64encode(b"image").decode()}]},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        store = _ImageStore()
        tool = GenerateImageTool(store, _config(), client_factory=lambda: client)
        with patch("agent.tools.imagegen.asyncio.sleep", new=AsyncMock()):
            result = await tool.execute("draw", session_key="web:user")

        self.assertIn("已文生图生成图片", result)
        self.assertEqual(attempts, 2)
        self.assertTrue(client.is_closed)
        self.assertEqual(store.saved, [("web:user", b"image", "png", "image/png")])

    async def test_retries_429_and_5xx_then_closes_client_and_responses(self):
        attempts = 0
        responses = []

        async def handler(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                response = httpx.Response(429)
            elif attempts == 2:
                response = httpx.Response(503)
            else:
                response = httpx.Response(
                    200,
                    json={"data": [{"b64_json": base64.b64encode(b"image").decode()}]},
                )
            responses.append(response)
            return response

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        store = _ImageStore()
        tool = GenerateImageTool(store, _config(), client_factory=lambda: client)
        with patch("agent.tools.imagegen.asyncio.sleep", new=AsyncMock()):
            result = await tool.execute("draw", session_key="web:user")

        self.assertIn("已文生图生成图片", result)
        self.assertEqual(attempts, 3)
        self.assertTrue(client.is_closed)
        self.assertTrue(all(response.is_closed for response in responses))
        self.assertEqual(store.saved, [("web:user", b"image", "png", "image/png")])

    async def test_total_timeout_closes_client_and_returns_friendly_result(self):
        client = _HangingClient()
        store = _ImageStore()
        tool = GenerateImageTool(
            store, _config(timeout_sec=10, total_timeout_sec=0.01),
            client_factory=lambda: client,
        )

        result = await tool.execute("draw", session_key="web:user")

        self.assertIn("生图任务超时", result)
        self.assertTrue(client.entered.is_set())
        self.assertTrue(client.closed)
        self.assertEqual(store.saved, [])

    async def test_download_response_is_closed_after_url_result(self):
        responses = []

        async def handler(request):
            if request.url.path.endswith("/images/generations"):
                response = httpx.Response(
                    200, json={"data": [{"url": "https://cdn.example.test/picture.png"}]}
                )
            else:
                response = httpx.Response(
                    200, content=b"png-bytes", headers={"content-type": "image/png"}
                )
            responses.append(response)
            return response

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        store = _ImageStore()
        tool = GenerateImageTool(store, _config(), client_factory=lambda: client)

        result = await tool.execute("draw", session_key="web:user")

        self.assertIn("已文生图生成图片", result)
        self.assertTrue(client.is_closed)
        self.assertTrue(all(response.is_closed for response in responses))
        self.assertEqual(store.saved, [("web:user", b"png-bytes", "png", "image/png")])

    async def test_outer_cancellation_is_not_swallowed_and_closes_client(self):
        client = _HangingClient()
        store = _ImageStore()
        tool = GenerateImageTool(
            store, _config(total_timeout_sec=10), client_factory=lambda: client
        )
        task = asyncio.create_task(tool.execute("draw", session_key="web:user"))
        await asyncio.wait_for(client.entered.wait(), 0.1)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(client.closed)
        self.assertEqual(store.saved, [])

    async def test_registry_budget_leaves_cleanup_grace_after_operation_deadline(self):
        tool = GenerateImageTool(_ImageStore(), _config(total_timeout_sec=12))

        self.assertEqual(tool.execution_timeout_sec, 17)

    async def test_registry_allows_internal_timeout_cleanup_to_finish(self):
        client = _HangingClient(close_delay=0.02)
        store = _ImageStore()
        tool = GenerateImageTool(
            store,
            _config(timeout_sec=10, total_timeout_sec=0.01),
            client_factory=lambda: client,
        )
        registry = ToolRegistry()
        registry.register(tool)

        result = await asyncio.wait_for(
            registry.execute(
                "generate_image", {"prompt": "draw", "session_key": "web:user"}
            ),
            0.2,
        )

        self.assertIn("生图任务超时", result)
        self.assertNotIn("工具 'generate_image' 执行超时", result)
        self.assertTrue(client.closed)
        self.assertEqual(store.saved, [])


if __name__ == "__main__":
    unittest.main()
