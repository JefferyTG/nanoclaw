"""web_search 工具单元测试（Tavily 主通道 + DuckDuckGo 降级兜底）。

mock 网络层（httpx.MockTransport / patch DDGS），不发真实请求、不消耗 Tavily
credits。覆盖：Tavily 成功路径、Tavily 401/空结果/网络错误降级 ddgs、未配置 key
直接走 ddgs、返回文本不回显 API key、输出截断。
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from agent.tools.web_search import WebSearchTool

_DDGS_RESULTS = [
    {"title": "D1", "href": "https://ddg.example/1", "body": "ddg 摘要一"},
    {"title": "D2", "href": "https://ddg.example/2", "body": "ddg 摘要二"},
]

_TAVILY_RESULTS = [
    {"title": "T1", "url": "https://tav.example/1", "content": "tavily 内容一 " * 10},
    {"title": "T2", "url": "https://tav.example/2", "content": "tavily 内容二 " * 10},
]


def _make_client(handler):
    """返回接受 **kwargs 的 httpx.AsyncClient 工厂（MockTransport）。"""
    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return factory


def _cfg(key=""):
    return SimpleNamespace(tavily_api_key=key)


class WebSearchTavilyTests(unittest.IsolatedAsyncioTestCase):
    async def test_tavily_success_path_formats_results(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://api.tavily.com/search")
            payload = json.loads(request.content)
            self.assertEqual(payload["query"], "量子计算")
            self.assertEqual(payload["max_results"], 3)
            self.assertEqual(payload["api_key"], "tvly-test-key")
            return httpx.Response(200, json={"results": _TAVILY_RESULTS})

        tool = WebSearchTool(
            config=_cfg("tvly-test-key"), client_factory=_make_client(handler)
        )
        with patch("agent.tools.web_search.DDGS") as mock_ddgs:
            result = await tool.execute("量子计算", max_results=3)

        self.assertIn("### 1. T1", result)
        self.assertIn("链接: https://tav.example/1", result)
        self.assertIn("tavily 内容一", result)
        self.assertIn("### 2. T2", result)
        # Tavily 成功时不应走到 ddgs
        mock_ddgs.assert_not_called()

    async def test_tavily_http_401_falls_back_to_ddgs(self):
        def handler(request):
            return httpx.Response(401, json={"error": "unauthorized"})

        tool = WebSearchTool(
            config=_cfg("tvly-test-key"), client_factory=_make_client(handler)
        )
        with patch("agent.tools.web_search.DDGS") as mock_ddgs:
            mock_ddgs.return_value.text.return_value = _DDGS_RESULTS
            result = await tool.execute("新闻", max_results=2)

        self.assertIn("### 1. D1", result)
        self.assertIn("链接: https://ddg.example/1", result)
        mock_ddgs.return_value.text.assert_called_once_with("新闻", max_results=2)

    async def test_tavily_empty_results_falls_back_to_ddgs(self):
        def handler(request):
            return httpx.Response(200, json={"results": []})

        tool = WebSearchTool(
            config=_cfg("tvly-test-key"), client_factory=_make_client(handler)
        )
        with patch("agent.tools.web_search.DDGS") as mock_ddgs:
            mock_ddgs.return_value.text.return_value = _DDGS_RESULTS
            result = await tool.execute("空结果", max_results=2)

        self.assertIn("### 1. D1", result)

    async def test_tavily_network_error_falls_back_to_ddgs(self):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        tool = WebSearchTool(
            config=_cfg("tvly-test-key"), client_factory=_make_client(handler)
        )
        with patch("agent.tools.web_search.DDGS") as mock_ddgs:
            mock_ddgs.return_value.text.return_value = _DDGS_RESULTS
            result = await tool.execute("网络错误", max_results=2)

        self.assertIn("### 1. D1", result)

    async def test_output_never_exposes_api_key(self):
        secret = "tvly-super-secret-key-123"

        def handler(request):
            return httpx.Response(200, json={"results": _TAVILY_RESULTS})

        tool = WebSearchTool(
            config=_cfg(secret), client_factory=_make_client(handler)
        )
        result = await tool.execute("量子", max_results=2)
        self.assertNotIn(secret, result)

    async def test_long_output_truncated(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"title": "T", "url": "https://x/1", "content": "x" * 2000}
                        for _ in range(10)
                    ]
                },
            )

        tool = WebSearchTool(config=_cfg("k"), client_factory=_make_client(handler))
        result = await tool.execute("长结果", max_results=10)
        self.assertIn("结果过长已截断", result)
        self.assertLessEqual(len(result), WebSearchTool._MAX_OUTPUT + 30)


class WebSearchNoKeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_key_goes_straight_to_ddgs(self):
        tool = WebSearchTool(config=_cfg(""))
        with patch("agent.tools.web_search.DDGS") as mock_ddgs:
            mock_ddgs.return_value.text.return_value = _DDGS_RESULTS
            result = await tool.execute("无 key", max_results=2)

        self.assertIn("### 1. D1", result)
        mock_ddgs.return_value.text.assert_called_once()

    async def test_no_results_returns_hint(self):
        tool = WebSearchTool(config=_cfg(""))
        with patch("agent.tools.web_search.DDGS") as mock_ddgs:
            mock_ddgs.return_value.text.return_value = []
            result = await tool.execute("找不到", max_results=2)
        self.assertEqual(result, "未找到相关结果")

    async def test_ddgs_error_returns_friendly_message(self):
        tool = WebSearchTool(config=_cfg(""))
        with patch("agent.tools.web_search.DDGS") as mock_ddgs:
            mock_ddgs.return_value.text.side_effect = RuntimeError("boom")
            result = await tool.execute("报错", max_results=2)
        self.assertIn("搜索出错", result)


if __name__ == "__main__":
    unittest.main()
