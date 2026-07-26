"""记忆与会话检索工具。

把 ``agent.search.MemorySearcher`` 包装成 Tool 注册进 ToolRegistry，模型在对话中
即可主动调用 ``memory_search`` 检索长期记忆与会话历史。

设计（大道至简）：原计划拆成 ``memory_search`` + ``session_search`` 两个工具，但
二者都是「全文检索」、只是数据源不同。合并为**一个工具带 scope 参数**，模型只需
记一个工具，scope=memory/session/all 控制范围。默认 scope=memory（先搜记忆），
无果可换 session（搜历史会话），符合 memory_development.md 的搜索策略。
"""

from typing import Optional

from agent.search import MemorySearcher
from agent.tools.base import Tool


# 来源中文标签，供结果展示
_SOURCE_LABEL = {
    "user": "用户信息",
    "memory": "工作记忆",
    "daily": "每日记录",
    "session": "历史会话",
}


class MemorySearchTool(Tool):
    """检索长期记忆（USER/MEMORY/daily）与会话历史（sessions）。"""

    name = "memory_search"
    description = (
        "检索长期记忆与会话历史，用关键词做子串匹配（中文友好）。"
        "适用场景：用户问「之前我们怎么讨论 X」「你记得我提过 Y 吗」「上次那个 Z 是什么」时调用。"
        "scope 默认 memory（先搜记忆文件）；无果可换 session（搜历史会话）；或 all 全搜。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索关键词，建议用具体词（如「Apple Calendar」「安静」「Mem0」）",
            },
            "scope": {
                "type": "string",
                "enum": ["memory", "session", "all"],
                "description": "检索范围：memory=只搜记忆文件(USER/MEMORY/daily，默认)；session=只搜历史会话；all=都搜",
            },
            "limit": {
                "type": "integer",
                "description": "返回结果上限，默认 10",
            },
        },
        "required": ["query"],
    }

    def __init__(self, searcher: MemorySearcher) -> None:
        """持有共享的 MemorySearcher 实例。"""
        self.searcher = searcher

    async def execute(self, **kwargs) -> str:
        query = (kwargs.get("query") or "").strip()
        if not query:
            return "错误：缺少参数 query（检索词）。"

        scope = kwargs.get("scope") or "memory"
        if scope not in ("memory", "session", "all"):
            scope = "memory"
        limit = kwargs.get("limit") or 10
        if not isinstance(limit, int) or limit <= 0:
            limit = 10

        results = self.searcher.search(query, scope=scope, limit=limit)
        if not results:
            scope_hint = {
                "memory": "记忆文件",
                "session": "历史会话",
                "all": "记忆与会话",
            }.get(scope, "记忆")
            return (
                f"在{scope_hint}中未找到与「{query}」相关的内容。"
                + ("可尝试 scope=session 搜历史会话。" if scope == "memory" else "")
            )

        lines = [f"找到 {len(results)} 条与「{query}」相关的内容："]
        for r in results:
            label = _SOURCE_LABEL.get(r["source"], r["source"])
            lines.append(f"- [{label}] {r['ref']}")
            lines.append(f"  {r['snippet']}")
        return "\n".join(lines)
