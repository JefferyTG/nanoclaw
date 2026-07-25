"""网页抓取工具（URL → 纯文本）。

``WebFetchTool`` 让模型能「点开」某个具体链接、读取网页正文。通常配合
``WebSearchTool`` 使用：先搜索拿到候选链接，再用本工具抓取详情。

安全性要点：
- 只允许 ``http`` / ``https`` 协议，过滤 ``file://``、``ftp://`` 等危险协议。
- 通过 ``html2text`` 把 HTML 转成 Markdown 纯文本，避免把脚本/标签噪声塞进上下文。
- 所有网络异常（超时、DNS 失败、连接拒绝等）都被捕获并转成友好提示，不向外抛。
"""

import asyncio
import re
from urllib.parse import urlparse

import httpx
import html2text

from agent.tools.base import Tool


# 常见浏览器 UA，降低被站点拒绝的概率
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class WebFetchTool(Tool):
    """网页抓取工具：抓取指定 URL 的网页内容并转换为纯文本返回。"""

    name: str = "web_fetch"
    description: str = (
        "抓取指定 URL 的网页内容。当你需要阅读某个具体网页的详细内容时使用。"
        "通常配合 web_search 工具先搜索再抓取。"
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要抓取的网页 URL（需为 http 或 https）",
            },
        },
        "required": ["url"],
    }

    # 输出纯文本的最大字符数，超出则截断
    _MAX_OUTPUT = 12000

    async def execute(self, url: str) -> str:
        """抓取网页并转换为纯文本。

        Args:
            url: 目标网页地址，仅支持 http/https。

        Returns:
            转换后的纯文本；协议不合法 / HTTP 错误 / 网络异常时返回对应提示。
            任何异常都不向外抛出。
        """
        # 1) URL 安全检查
        try:
            parsed = urlparse(url)
        except Exception as e:
            return f"URL 解析失败: {e}"

        if parsed.scheme not in ("http", "https"):
            return "安全拦截：只允许 http/https 协议"

        # 2) 发送 HTTP GET（用 async with 确保连接正确关闭）
        try:
            async with httpx.AsyncClient(
                timeout=15, follow_redirects=True
            ) as client:
                response = await client.get(
                    url, headers={"User-Agent": _BROWSER_UA}
                )
        except Exception as e:
            return f"抓取出错: {e}"

        if response.status_code < 200 or response.status_code >= 300:
            return f"抓取失败：HTTP {response.status_code}"

        # 3) HTML 转纯文本（Markdown 风格）
        try:
            h = html2text.HTML2Text()
            h.ignore_links = False   # 保留链接
            h.ignore_images = True   # 忽略图片
            h.body_width = 0         # 不自动换行
            text = h.handle(response.text)
        except Exception as e:
            return f"内容转换失败: {e}"

        # 4) 清理输出：合并连续空行 + 超长截断
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) > self._MAX_OUTPUT:
            text = text[: self._MAX_OUTPUT] + "\n...(内容过长，已截断)"

        return text
