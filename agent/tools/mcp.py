"""MCP（Model Context Protocol）客户端接入层。

本模块让 NanoClaw 能像使用内置工具一样使用外部 MCP Server 暴露的工具。
设计上分两层：

1. ``MCPClientManager``：负责按配置启动一个或多个 MCP Server（通过 stdio
   子进程），维护到每个 Server 的 ``ClientSession``，并在连接时把远端工具
   拉成本地的 ``MCPTool`` 列表。连接是「尽力而为」的——某个 Server 超时或
   异常不会拖垮整体，只打印告警并跳过。

2. ``MCPTool``：把单个远端工具包装成 ``agent.tools.base.Tool`` 的子类，使其
   能无缝注册进现有的 ``ToolRegistry``，被 Agent 的 function-calling 正常
   调度。工具名统一加 ``{server}__{tool}`` 前缀，避免不同 Server 同名冲突。

关键约束（来自踩坑经验）：
- 所有 print 不使用 emoji（Windows GBK 终端会崩），统一用 ``[MCP]`` / ``[!]``。
- ``ClientSession`` 必须手动 ``__aenter__()`` 拿到 session 后再 ``initialize()``，
  否则其内部消息循环不启动，``initialize()`` 会永远卡死。
- ``stdio_client`` / ``ClientSession`` 都是异步上下文管理器；退出时要按
  session → stdio 的顺序 ``__aexit__`` 干净收尾，否则子进程可能残留。
"""

import asyncio
import os

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from agent.tools.base import Tool


# ---------------------------------------------------------------------------
# MCPTool：把单个远端工具包装成本地 Tool
# ---------------------------------------------------------------------------
class MCPTool(Tool):
    """远端 MCP 工具的本地包装。

    通过持有 ``MCPClientManager`` 引用，``execute`` 时把调用转发给对应 Server
    的 ``call_tool``。工具名格式为 ``{server}__{tool_name}``，确保跨 Server 唯一。
    """

    def __init__(
        self,
        server_name: str,
        tool_name: str,
        description: str,
        input_schema: dict,
        manager: "MCPClientManager",
    ) -> None:
        self._server = server_name
        self._tool = tool_name
        self._description = description or ""
        # inputSchema 应为 JSON Schema dict；防御性兜底为空 object
        self._input_schema = input_schema or {"type": "object", "properties": {}}
        self._manager = manager

    @property
    def name(self) -> str:
        return f"{self._server}__{self._tool}"

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict:
        return self._input_schema

    async def execute(self, **kwargs) -> str:
        """把调用转发给远端 MCP Server 的对应工具。"""
        return await self._manager.call_tool(self._server, self._tool, kwargs)

    def to_function_definition(self) -> dict:
        """直接用远端 inputSchema 构造 OpenAI function-calling 定义。

        不依赖基类对 ``parameters`` 的二次加工，原样透传 MCP 提供的 JSON Schema，
        保证字段（含 required / 枚举 / 描述）与远端完全一致。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ---------------------------------------------------------------------------
# MCPClientManager：管理一个或多个 MCP Server 的连接
# ---------------------------------------------------------------------------
class MCPClientManager:
    """MCP 客户端管理器。

    负责管理多个 MCP Server 的 stdio 连接生命周期：

    - ``__init__``：仅保存配置，不建立任何连接。
    - ``connect_all``：遍历配置，逐个 ``_connect_one``，每个都用
      ``asyncio.wait_for(timeout)`` 包裹，超时/异常则跳过并打印告警。
    - ``get_tools``：返回所有已成功连接的 Server 暴露的 ``MCPTool`` 列表。
    - ``call_tool``：按 server/tool 名字把调用投到对应 session。
    - ``shutdown``：按 session → stdio 顺序退出所有上下文管理器，回收子进程。
    """

    def __init__(self, mcp_config: dict) -> None:
        # server_name -> server_config（含 command / args / env / cwd 等）
        self._servers = mcp_config or {}
        self._context_managers = {}   # stdio_client 上下文管理器
        self._session_managers = {}   # ClientSession 上下文管理器
        self._sessions = {}           # 已连接、已 initialize 的 ClientSession
        self._tools: list = []        # MCPTool 列表

    async def _connect_one(self, server_name: str, server_config: dict) -> None:
        """连接单个 MCP Server 并拉取它的工具清单。

        任何一步失败都向上抛出，由 ``connect_all`` 统一捕获处理。
        """
        if not isinstance(server_config, dict):
            raise ValueError("server_config 必须是 dict（含 command/args）")

        command = server_config.get("command")
        args = server_config.get("args") or []
        if not command:
            raise ValueError("server_config 缺少 'command'")

        # env 与当前进程环境合并（用户提供的覆盖系统环境），保证 `uv`/`python`
        # 等命令仍在 PATH 上；若不合并，子进程可能找不到可执行文件。
        env = {**os.environ}
        if server_config.get("env"):
            env.update(server_config["env"])

        params = StdioServerParameters(
            command=command,
            args=list(args),
            env=env,
            cwd=server_config.get("cwd"),   # 可选：指定子进程工作目录
        )

        # 1) 进入 stdio_client 上下文，拿到 (read, write) 双向流
        ctx = stdio_client(params)
        self._context_managers[server_name] = ctx
        read_stream, write_stream = await ctx.__aenter__()

        # 2) 手动 __aenter__ ClientSession（关键：否则内部消息循环不启动）
        session_mgr = ClientSession(read_stream, write_stream)
        self._session_managers[server_name] = session_mgr
        session = await session_mgr.__aenter__()

        # 3) 初始化握手
        await session.initialize()

        # 4) 拉取工具清单，逐个包装成 MCPTool
        result = await session.list_tools()
        added = 0
        for tool in result.tools:
            self._tools.append(
                MCPTool(
                    server_name=server_name,
                    tool_name=tool.name,
                    description=getattr(tool, "description", "") or "",
                    input_schema=getattr(tool, "inputSchema", None),
                    manager=self,
                )
            )
            added += 1

        self._sessions[server_name] = session
        print(f"[MCP] 已连接 Server '{server_name}'，加载工具 {added} 个")

    async def connect_all(self, timeout: float = 30.0) -> None:
        """连接配置中的所有 Server。

        每个 Server 的连接用 ``asyncio.wait_for(timeout)`` 包裹，超时或异常则
        跳过该 Server 并打告警，绝不中断其余 Server 的连接。
        """
        if not self._servers:
            print("[MCP] 未配置任何 MCP Server，跳过连接")
            return

        for server_name, server_config in self._servers.items():
            try:
                await asyncio.wait_for(
                    self._connect_one(server_name, server_config), timeout=timeout
                )
            except asyncio.TimeoutError:
                print(f"[!] 连接 MCP Server '{server_name}' 超时（>{timeout}s），已跳过")
                await self._cleanup_one(server_name)
            except Exception as exc:  # noqa: BLE001 - 单 Server 失败不应影响整体
                print(f"[!] 连接 MCP Server '{server_name}' 失败：{exc}，已跳过")
                await self._cleanup_one(server_name)

    def get_tools(self) -> list:
        """返回所有已成功连接的 Server 暴露的 MCPTool 列表。"""
        return list(self._tools)

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> str:
        """调用指定 Server 的某个工具，返回其文本结果。

        返回字符串（成功结果或错误信息），便于直接回填给模型。
        """
        session = self._sessions.get(server_name)
        if session is None:
            return f"错误：MCP Server '{server_name}' 未连接或已断开"

        try:
            result = await session.call_tool(tool_name, arguments or {})
        except Exception as exc:  # noqa: BLE001 - 调用异常转成字符串反馈给模型
            return f"错误：调用 MCP 工具 '{server_name}__{tool_name}' 失败：{exc}"

        # 把 content 块拼成文本；优先取 TextContent 的 .text
        parts: list = []
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            parts.append(text if text is not None else str(block))
        output = "\n".join(parts).strip() or "(MCP 工具返回空内容)"

        if getattr(result, "isError", False):
            return f"[MCP 工具返回错误] {output}"
        return output

    async def _cleanup_one(self, server_name: str) -> None:
        """收尾单个 Server 的会话与子进程（用于连接失败回滚）。"""
        session_mgr = self._session_managers.pop(server_name, None)
        if session_mgr is not None:
            try:
                await session_mgr.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - 清理失败不抛出
                pass
        ctx = self._context_managers.pop(server_name, None)
        if ctx is not None:
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - 清理失败不抛出
                pass
        self._sessions.pop(server_name, None)

    async def shutdown(self) -> None:
        """停止所有连接：按 session → stdio 顺序退出上下文，回收子进程。"""
        # 1) 先退出所有 ClientSession（取消内部消息循环）
        for server_name in list(self._session_managers.keys()):
            mgr = self._session_managers.pop(server_name)
            try:
                await mgr.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - 退出异常忽略，继续收尾
                pass

        # 2) 再退出所有 stdio_client（关闭子进程管道、回收进程）
        for server_name in list(self._context_managers.keys()):
            ctx = self._context_managers.pop(server_name)
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - 退出异常忽略，继续收尾
                pass

        self._sessions.clear()
        self._tools.clear()
        print("[MCP] 所有 MCP Server 连接已关闭")
