"""网页抓取工具（URL → 纯文本，多级自动降级链）。

``WebFetchTool`` 让模型能「点开」某个具体链接、读取网页正文。通常配合
``WebSearchTool`` 使用：先搜索拿到候选链接，再用本工具抓取详情。

抓取降级链（TASK-016，每级失败或正文过短即进下一级）：

1. **httpx 静态抓取**（现状，免费）：GET + html2text。
2. **Jina Reader**（https://r.jina.ai/{url}，免费，可渲染 JS）：GET 带浏览器
   UA，结果同样 html2text/清洗。
3. **本机 Chrome 无头渲染**（免费，完整渲染）：``--headless=new --disable-gpu
   --dump-dom``，DOM 转文本；找不到 Chrome 时跳过本级。
4. **Tavily Extract**（烧 credit，最后王牌）：POST https://api.tavily.com/extract，
   仅配置了 ``tavily_api_key`` 才启用。

降级判定：HTTP 非 2xx / 抛异常 / 转换后正文过短（<200 字符，疑似 JS 空壳）→
自动进下一级；各级都失败返回友好错误信息（说明各级失败原因），不抛异常。

安全性要点：
- 只允许 ``http`` / ``https`` 协议，过滤 ``file://``、``ftp://`` 等危险协议。
- 只读 GET / 只读渲染，不执行页面脚本（Chrome ``--dump-dom`` 只渲染 DOM）。
- 不携带本地文件/密钥，无视页面注入指令。
- 通过 ``html2text`` 把 HTML 转成 Markdown 纯文本，避免把脚本/标签噪声塞进上下文。
- 所有网络/子进程异常都被捕获并转成友好提示，不向外抛。
"""

import asyncio
import os
import re
import shutil
import subprocess
from typing import Optional
from urllib.parse import urlparse

import httpx
import html2text

from agent.tools.base import Tool


# 常见浏览器 UA，降低被站点拒绝的概率
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Tavily Extract REST 端点与请求超时（秒）
_TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
_TAVILY_TIMEOUT = 30.0

# 本机 Chrome 候选路径（Mac 默认；找不到时跳过本级）
_CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

# 正文过短阈值：低于此长度视为「动态空壳」（JS 未渲染/需登录提示），进下一级
_MIN_TEXT_LEN = 200

# Chrome 无头渲染各阶段超时（秒）
_CHROME_TIMEOUT = 30


def _html_to_text(html: str) -> str:
    """HTML → Markdown 风格纯文本（html2text）。"""
    h = html2text.HTML2Text()
    h.ignore_links = False   # 保留链接
    h.ignore_images = True   # 忽略图片
    h.body_width = 0         # 不自动换行
    return h.handle(html)


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

    def __init__(
        self,
        config: Optional[object] = None,
        client_factory=None,
        chrome_paths: Optional[tuple] = None,
    ) -> None:
        """构造抓取工具。

        Args:
            config: 可选，共享的 ``NanoClawConfig`` 实例（读取 ``tavily_api_key``，
                供第 4 级 Tavily Extract 使用）。不传时惰性读取 config.json。
            client_factory: 可选，返回 ``httpx.AsyncClient`` 的工厂（测试注入 mock）。
            chrome_paths: 可选，本机 Chrome 可执行文件候选路径（测试注入）。
        """
        self._config = config
        self._client_factory = client_factory or httpx.AsyncClient
        self._chrome_paths = tuple(chrome_paths or _CHROME_PATHS)
        self._tavily_key: Optional[str] = None  # None=尚未惰性读取
        self._failures: list[str] = []

    async def execute(self, url: str) -> str:
        """抓取网页并转换为纯文本，按降级链逐级尝试。

        Args:
            url: 目标网页地址，仅支持 http/https。

        Returns:
            转换后的纯文本；协议不合法 / 各级均失败时返回对应提示。
            任何异常都不向外抛出。
        """
        # 1) URL 安全检查
        try:
            parsed = urlparse(url)
        except Exception as e:
            return f"URL 解析失败: {e}"

        if parsed.scheme not in ("http", "https"):
            return "安全拦截：只允许 http/https 协议"

        # 2) 降级链：第1级 httpx → 第2级 Jina → 第3级 Chrome → 第4级 Tavily
        self._failures = []
        for level in (
            self._level_httpx,
            self._level_jina,
            self._level_chrome,
            self._level_tavily_extract,
        ):
            text = await level(url)
            if text is not None:
                return self._clean(text)

        # 3) 各级都失败：返回友好错误，说明各级失败原因，不抛异常
        detail = "；".join(self._failures) or "未知原因"
        return f"抓取失败：所有抓取通道均未返回有效内容。\n{detail}"

    # ---- 第 1 级：httpx 静态抓取（现状保留） ----

    async def _level_httpx(self, url: str) -> Optional[str]:
        """httpx GET + html2text。失败/过短返回 None 并记录原因。"""
        try:
            async with self._client_factory(
                timeout=15, follow_redirects=True
            ) as client:
                response = await client.get(
                    url, headers={"User-Agent": _BROWSER_UA}
                )
        except Exception as e:
            self._failures.append(f"第1级 httpx 静态抓取：{e}")
            return None

        if response.status_code < 200 or response.status_code >= 300:
            self._failures.append(
                f"第1级 httpx 静态抓取：HTTP {response.status_code}"
            )
            return None

        try:
            text = _html_to_text(response.text)
        except Exception as e:
            self._failures.append(f"第1级 httpx 静态抓取：内容转换失败 {e}")
            return None

        if len(text) < _MIN_TEXT_LEN:
            self._failures.append(
                f"第1级 httpx 静态抓取：正文过短（{len(text)} 字符，疑似动态空壳）"
            )
            return None
        return text

    # ---- 第 2 级：Jina Reader（免费，可渲染 JS） ----

    async def _level_jina(self, url: str) -> Optional[str]:
        """GET https://r.jina.ai/{url}（浏览器 UA）。失败/过短返回 None。"""
        jina_url = f"https://r.jina.ai/{url}"
        try:
            async with self._client_factory(
                timeout=30, follow_redirects=True
            ) as client:
                response = await client.get(
                    jina_url, headers={"User-Agent": _BROWSER_UA}
                )
        except Exception as e:
            self._failures.append(f"第2级 Jina Reader：{e}")
            return None

        if response.status_code < 200 or response.status_code >= 300:
            self._failures.append(
                f"第2级 Jina Reader：HTTP {response.status_code}"
            )
            return None

        try:
            # Jina 返回的是文本/Markdown（含 Title:/URL Source:/Markdown Content:），
            # html2text 对纯文本基本透传，安全。
            text = _html_to_text(response.text)
        except Exception as e:
            self._failures.append(f"第2级 Jina Reader：内容转换失败 {e}")
            return None

        if len(text) < _MIN_TEXT_LEN:
            self._failures.append(
                f"第2级 Jina Reader：正文过短（{len(text)} 字符）"
            )
            return None
        return text

    # ---- 第 3 级：本机 Chrome 无头渲染（完整渲染） ----

    async def _level_chrome(self, url: str) -> Optional[str]:
        """Chrome ``--headless=new --disable-gpu --dump-dom`` 只读渲染。"""
        chrome = self._find_chrome()
        if not chrome:
            self._failures.append("第3级 Chrome 无头渲染：未找到本机 Chrome，跳过")
            return None

        try:
            proc = await asyncio.create_subprocess_exec(
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--dump-dom",
                "--virtual-time-budget=20000",
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=_CHROME_TIMEOUT
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                self._failures.append(
                    f"第3级 Chrome 无头渲染：超时（{_CHROME_TIMEOUT} 秒）"
                )
                return None
        except Exception as e:
            self._failures.append(f"第3级 Chrome 无头渲染：{e}")
            return None

        try:
            dom = stdout.decode("utf-8", errors="replace")
            text = _html_to_text(dom)
        except Exception as e:
            self._failures.append(f"第3级 Chrome 无头渲染：内容转换失败 {e}")
            return None

        if len(text) < _MIN_TEXT_LEN:
            self._failures.append(
                f"第3级 Chrome 无头渲染：正文过短（{len(text)} 字符）"
            )
            return None
        return text

    def _find_chrome(self) -> Optional[str]:
        """按候选路径 + PATH 查找 Chrome 可执行文件；找不到返回 None。"""
        for path in self._chrome_paths:
            if os.path.exists(path):
                return path
        return (
            shutil.which("google-chrome")
            or shutil.which("chromium")
            or shutil.which("chrome")
        )

    # ---- 第 4 级：Tavily Extract（烧 credit，最后王牌） ----

    async def _level_tavily_extract(self, url: str) -> Optional[str]:
        """POST https://api.tavily.com/extract。仅配置了 key 才启用。"""
        api_key = self._get_tavily_key()
        if not api_key:
            self._failures.append("第4级 Tavily Extract：未配置 tavily_api_key，跳过")
            return None

        try:
            async with self._client_factory(timeout=_TAVILY_TIMEOUT) as client:
                response = await client.post(
                    _TAVILY_EXTRACT_URL,
                    json={"api_key": api_key, "urls": [url]},
                )
        except Exception as e:
            self._failures.append(f"第4级 Tavily Extract：{e}")
            return None

        if response.status_code < 200 or response.status_code >= 300:
            self._failures.append(
                f"第4级 Tavily Extract：HTTP {response.status_code}"
            )
            return None

        try:
            data = response.json()
        except Exception as e:
            self._failures.append(f"第4级 Tavily Extract：解析失败 {e}")
            return None

        results = data.get("results") or []
        content = ""
        if results:
            content = str(results[0].get("content") or "").strip()
        if not content or len(content) < _MIN_TEXT_LEN:
            self._failures.append("第4级 Tavily Extract：未返回有效正文")
            return None
        return content

    # ---- 公共清理 ----

    def _clean(self, text: str) -> str:
        """连续空行合并 + 超长截断（保留现有 12000 字符上限逻辑）。"""
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) > self._MAX_OUTPUT:
            text = text[: self._MAX_OUTPUT] + "\n...(内容过长，已截断)"
        return text

    def _get_tavily_key(self) -> str:
        """获取 Tavily API Key：优先注入的 config，否则惰性读 config.json。

        返回文本不含 key 本身，只用于请求体，绝不回显给模型。
        """
        if self._config is not None:
            return str(getattr(self._config, "tavily_api_key", "") or "")
        if self._tavily_key is None:
            try:
                from config import load_config

                self._tavily_key = str(load_config().tavily_api_key or "")
            except Exception:
                # 配置读取失败不阻断抓取：当作未配置 key，跳过第 4 级
                self._tavily_key = ""
        return self._tavily_key
