"""互联网搜索工具（Tavily 主通道 + DuckDuckGo 降级兜底）。

``WebSearchTool`` 让模型具备「联网查资料」的能力：当问题涉及实时信息、
最新新闻或不确定的外部知识时，模型可以调用它获取搜索结果。

通道策略（TASK-016）：
- **Tavily 主通道**：配置了 ``tavily_api_key`` 时优先调用
  ``https://api.tavily.com/search``（httpx 直调 REST，无新依赖）。返回结构化
  结果（title/url/content 正文片段），质量高、中文友好。
- **DuckDuckGo 降级兜底**：未配置 key / Tavily 请求失败（401/429/超时/解析
  错误）/ Tavily 返回空结果时，自动降级到现有 ``ddgs`` 通道，保证搜索永远可用。

底层 ``ddgs`` 库的 ``DDGS().text()`` 与 Tavily REST 调用都可能包含同步阻塞
部分，其中 ddgs 用 ``asyncio.to_thread`` 丢进线程池避免卡住事件循环；Tavily
走 httpx 原生异步客户端。

对外接口保持不变：``execute(query, max_results=5) -> str``，模型侧无感。
"""

import asyncio
from typing import Optional

import httpx
from ddgs import DDGS

from agent.tools.base import Tool

# Tavily Search REST 端点与请求超时（秒）。失败即降级 ddgs，不重试。
_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_TAVILY_TIMEOUT = 15.0


class WebSearchTool(Tool):
    """联网搜索工具：Tavily 主通道 + DuckDuckGo 降级兜底，返回结构化结果。"""

    name: str = "web_search"
    description: str = (
        "搜索互联网获取最新信息。当你需要查询实时信息、最新新闻或不确定的知识时使用。"
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词",
            },
            "max_results": {
                "type": "integer",
                "description": "最多返回几条搜索结果（默认 5）",
                "default": 5,
            },
        },
        "required": ["query"],
    }

    # 输出结果的最大字符数，超出则截断（防止海量结果冲刷上下文）
    _MAX_OUTPUT = 8000

    def __init__(self, config: Optional[object] = None, client_factory=None):
        """构造搜索工具。

        Args:
            config: 可选，共享的 ``NanoClawConfig`` 实例（读取 ``tavily_api_key``）。
                不传时惰性读取 config.json（兼容 main.py 现状 ``WebSearchTool()``）。
            client_factory: 可选，返回 ``httpx.AsyncClient`` 的工厂（测试注入 mock）。
        """
        self._config = config
        self._client_factory = client_factory or httpx.AsyncClient
        self._tavily_key: Optional[str] = None  # None=尚未惰性读取

    async def execute(self, query: str, max_results: int = 5) -> str:
        """执行互联网搜索，优先 Tavily，失败自动降级 DuckDuckGo。

        Args:
            query: 搜索关键词。
            max_results: 最多返回的结果条数，默认 5。

        Returns:
            格式化后的搜索结果文本；无结果返回提示语；出错返回错误信息。
            任何异常都不向外抛出，统一转成可读字符串。
        """
        api_key = self._get_tavily_key()
        if api_key:
            try:
                tavily_results = await self._search_tavily(
                    query, max_results, api_key
                )
                if tavily_results:
                    return self._format_results(tavily_results)
                # Tavily 返回空结果：视为无结果，继续走 ddgs 兜底
            except Exception:
                # 401/429/超时/解析错误等一律降级 ddgs，保证搜索永远可用
                pass

        # DuckDuckGo 降级兜底（保留原行为）
        try:
            # DDGS().text() 是同步阻塞调用，用 to_thread 包成异步，不阻塞事件循环
            results = await asyncio.to_thread(
                self._search_sync, query, max_results
            )
        except Exception as e:
            return f"搜索出错: {e}"

        if not results:
            return "未找到相关结果"

        return self._format_results(results)

    async def _search_tavily(
        self, query: str, max_results: int, api_key: str
    ) -> list:
        """调用 Tavily Search REST API，返回归一化的结果列表。

        Raises:
            任何网络/HTTP/解析异常向上抛，由 execute 统一降级 ddgs。
        """
        payload = {
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
        }
        async with self._client_factory(timeout=_TAVILY_TIMEOUT) as client:
            response = await client.post(_TAVILY_SEARCH_URL, json=payload)
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"Tavily HTTP {response.status_code}")
        data = response.json()
        # Tavily 返回 {"results": [{"title", "url", "content", ...}]}
        return data.get("results") or []

    def _format_results(self, results: list) -> str:
        """把结果格式化为与现状一致的 Markdown 风格文本。

        兼容两种结构：ddgs 的 ``title/href/body`` 与 Tavily 的 ``title/url/content``。
        """
        blocks = []
        for idx, item in enumerate(results, start=1):
            title = item.get("title", "")
            href = item.get("href") or item.get("url", "")
            body = item.get("body") or item.get("content", "")
            blocks.append(f"### {idx}. {title}\n链接: {href}\n{body}\n")

        output = "\n".join(blocks)

        # 结果过长则截断，保留提示信息
        if len(output) > self._MAX_OUTPUT:
            output = output[: self._MAX_OUTPUT] + "\n...[结果过长已截断]"

        return output

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
                # 配置读取失败不阻断搜索：当作未配置 key，走 ddgs
                self._tavily_key = ""
        return self._tavily_key

    @staticmethod
    def _search_sync(query: str, max_results: int) -> list:
        """同步执行 DuckDuckGo 搜索，供 ``asyncio.to_thread`` 调用。"""
        return DDGS().text(query, max_results=max_results)
