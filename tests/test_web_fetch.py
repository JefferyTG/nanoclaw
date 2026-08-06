"""web_fetch 工具单元测试（四级自动降级链）。

mock 网络层（httpx.MockTransport）与 Chrome 子进程，不发真实请求、不消耗 Tavily
credits。覆盖：第1级成功、第1级失败降级第2级 Jina、第1/2级失败降级第3级 Chrome、
Chrome 缺失跳过第3级、正文过短触发降级、全部失败返回友好错误、非 http/https 协议
拦截、Tavily Extract 只在配置 key 时启用、输出截断。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from agent.tools.web_fetch import WebFetchTool

_HTML_OK = (
    "<html><body><h1>标题</h1><p>" + "正文内容" * 60 + "</p></body></html>"
)
_SHORT_HTML = "<html><body><p>You need to enable JavaScript</p></body></html>"


def _make_client(handler):
    """返回接受 **kwargs 的 httpx.AsyncClient 工厂（MockTransport）。"""
    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return factory


def _cfg(key=""):
    return SimpleNamespace(tavily_api_key=key)


class WebFetchLevel1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_level1_success_returns_plain_text(self):
        def handler(request):
            return httpx.Response(200, text=_HTML_OK)

        tool = WebFetchTool(config=_cfg(), client_factory=_make_client(handler))
        result = await tool.execute("https://example.com/page")
        self.assertIn("标题", result)
        self.assertIn("正文内容", result)
        # 第1级成功：不应出现第 2 级 Jina 的请求痕迹
        self.assertNotIn("r.jina.ai", result)

    async def test_non_http_protocol_blocked(self):
        tool = WebFetchTool(config=_cfg())
        self.assertEqual(
            await tool.execute("file:///etc/passwd"), "安全拦截：只允许 http/https 协议"
        )
        self.assertEqual(
            await tool.execute("ftp://example.com/x"), "安全拦截：只允许 http/https 协议"
        )

    async def test_long_output_truncated(self):
        def handler(request):
            return httpx.Response(
                200, text="<html><body><p>" + "长" * 50000 + "</p></body></html>"
            )

        tool = WebFetchTool(config=_cfg(), client_factory=_make_client(handler))
        result = await tool.execute("https://example.com/long")
        self.assertIn("内容过长，已截断", result)
        self.assertLessEqual(len(result), WebFetchTool._MAX_OUTPUT + 30)


class WebFetchDegradationTests(unittest.IsolatedAsyncioTestCase):
    async def test_level1_http_error_falls_to_level2_jina(self):
        def handler(request):
            if "r.jina.ai" in str(request.url):
                return httpx.Response(200, text=_HTML_OK)
            return httpx.Response(500, text="server error")

        tool = WebFetchTool(config=_cfg(), client_factory=_make_client(handler))
        result = await tool.execute("https://example.com/dynamic")
        self.assertIn("标题", result)
        self.assertIn("正文内容", result)

    async def test_short_body_triggers_downgrade(self):
        def handler(request):
            # 第1级返回 JS 空壳（<200 字符）→ 第2级 Jina 返回有效正文
            if "r.jina.ai" in str(request.url):
                return httpx.Response(200, text=_HTML_OK)
            return httpx.Response(200, text=_SHORT_HTML)

        tool = WebFetchTool(config=_cfg(), client_factory=_make_client(handler))
        result = await tool.execute("https://example.com/spa")
        self.assertIn("正文内容", result)

    async def test_level1_and_2_fail_falls_to_level3_chrome(self):
        def handler(request):
            if "r.jina.ai" in str(request.url):
                return httpx.Response(200, text=_SHORT_HTML)
            return httpx.Response(500)

        tool = WebFetchTool(config=_cfg(), client_factory=_make_client(handler))
        proc = AsyncMock()
        dom = "<html><body><h1>Chrome标题</h1><p>" + "渲染正文" * 100 + "</p></body></html>"
        proc.communicate = AsyncMock(return_value=(dom.encode("utf-8"), b""))
        with patch.object(tool, "_find_chrome", return_value="/fake/chrome"), patch(
            "agent.tools.web_fetch.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as m:
            result = await tool.execute("https://example.com/render")

        self.assertIn("Chrome标题", result)
        self.assertIn("渲染正文", result)
        # Chrome 参数必须是只读渲染，不带交互能力
        args = m.call_args[0]
        self.assertIn("--headless=new", args)
        self.assertIn("--dump-dom", args)
        self.assertIn("--disable-gpu", args)

    async def test_chrome_missing_skips_to_level4_tavily(self):
        def handler(request):
            if str(request.url).startswith("https://api.tavily.com/extract"):
                return httpx.Response(
                    200, json={"results": [{"content": "tavily 提取正文" * 100}]}
                )
            if "r.jina.ai" in str(request.url):
                return httpx.Response(200, text=_SHORT_HTML)
            return httpx.Response(500)

        tool = WebFetchTool(config=_cfg("tvly-test"), client_factory=_make_client(handler))
        with patch.object(tool, "_find_chrome", return_value=None):
            result = await tool.execute("https://example.com/tavily")
        self.assertIn("tavily 提取正文", result)

    async def test_tavily_extract_http_error_all_fail(self):
        def handler(request):
            if str(request.url).startswith("https://api.tavily.com/extract"):
                return httpx.Response(429, json={"error": "rate limited"})
            if "r.jina.ai" in str(request.url):
                return httpx.Response(200, text=_SHORT_HTML)
            return httpx.Response(500)

        tool = WebFetchTool(config=_cfg("tvly-test"), client_factory=_make_client(handler))
        with patch.object(tool, "_find_chrome", return_value=None):
            result = await tool.execute("https://example.com/fail2")
        self.assertIn("抓取失败", result)
        self.assertIn("HTTP 429", result)

    async def test_no_key_skips_tavily_and_all_fail_friendly(self):
        def handler(request):
            if "r.jina.ai" in str(request.url):
                return httpx.Response(200, text=_SHORT_HTML)
            return httpx.Response(500)

        tool = WebFetchTool(config=_cfg(""), client_factory=_make_client(handler))
        with patch.object(tool, "_find_chrome", return_value=None):
            result = await tool.execute("https://example.com/fail")

        self.assertIn("抓取失败", result)
        self.assertIn("第1级 httpx", result)
        self.assertIn("第2级 Jina", result)
        self.assertIn("第3级 Chrome", result)
        self.assertIn("第4级 Tavily", result)


if __name__ == "__main__":
    unittest.main()
