"""互联网搜索工具（基于 DuckDuckGo，通过 ddgs 库实现）。

``WebSearchTool`` 让模型具备「联网查资料」的能力：当问题涉及实时信息、
最新新闻或不确定的外部知识时，模型可以调用它获取搜索结果。

底层 ``ddgs`` 库的 ``DDGS().text()`` 是**同步阻塞**调用，而我们的工具是
异步接口，因此用 ``asyncio.to_thread`` 把它丢到线程池里跑，避免卡住事件循环。
"""

import asyncio
from typing import Optional

from ddgs import DDGS

from agent.tools.base import Tool


class WebSearchTool(Tool):
    """联网搜索工具：用 DuckDuckGo 检索互联网并返回结构化结果。"""

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

    async def execute(self, query: str, max_results: int = 5) -> str:
        """执行 DuckDuckGo 文本搜索，返回格式化后的结果列表。

        Args:
            query: 搜索关键词。
            max_results: 最多返回的结果条数，默认 5。

        Returns:
            格式化后的搜索结果文本；无结果返回提示语；出错返回错误信息。
            任何异常都不向外抛出，统一转成可读字符串。
        """
        try:
            # DDGS().text() 是同步阻塞调用，用 to_thread 包成异步，不阻塞事件循环
            results = await asyncio.to_thread(
                self._search_sync, query, max_results
            )
        except Exception as e:
            return f"搜索出错: {e}"

        if not results:
            return "未找到相关结果"

        # 逐条格式化为 Markdown 风格文本
        blocks = []
        for idx, item in enumerate(results, start=1):
            title = item.get("title", "")
            href = item.get("href", "")
            body = item.get("body", "")
            blocks.append(f"### {idx}. {title}\n链接: {href}\n{body}\n")

        output = "\n".join(blocks)

        # 结果过长则截断，保留提示信息
        if len(output) > self._MAX_OUTPUT:
            output = output[: self._MAX_OUTPUT] + "\n...[结果过长已截断]"

        return output

    @staticmethod
    def _search_sync(query: str, max_results: int) -> list:
        """同步执行搜索，供 ``asyncio.to_thread`` 调用。"""
        return DDGS().text(query, max_results=max_results)
