"""工具注册表。

本模块提供 `ToolRegistry`，用于在单个 Agent 会话内集中管理所有 `Tool` 实例。
它负责三件事：把工具登记进来、对外暴露成 OpenAI 可用的函数定义列表、
按名字异步调度执行。Agent 只需持有一个 registry，就能统一处理「模型要求
调用某个工具」的全部链路，而不必自己维护映射表与异常包装。

设计要点：
- 内部用 ``dict[str, Tool]`` 存工具，key 为工具名（取自 ``tool.name``），
  保证同名工具只能存在一个，后注册的会自然覆盖前一个。
- ``get_definitions`` 直接复用每个工具自带的 ``to_function_definition``，
  注册表本身不关心具体格式，避免两边各写一份导致不一致。
- ``execute`` 是异步的，内部 ``await tool.execute``，并捕获任何异常：
  工具找不到、执行报错都不会让 Agent 主链路炸掉，而是把错误转成字符串
  返回，模型可以据此决定下一步（重试 / 换工具 / 直接回答）。
"""

from typing import Dict, List

import asyncio

from agent.tools.base import Tool


# 单个工具执行的兜底超时（秒）：任何工具（含没有自带超时的工具）的单次调用
# 超过此值即被强制终止，转成错误字符串返回，避免「一个工具卡死」把整个回合
# 无限拖住。Shell 工具有更紧的 60s 内部超时，会先于此值触发。
TOOL_EXEC_TIMEOUT = 180


class ToolRegistry:
    """工具注册表：集中注册、枚举与调度 Tool 实例。

    Agent 持有本类的单个实例即可，典型用法::

        registry = ToolRegistry()
        registry.register(WebSearchTool())
        registry.register(EchoTool())

        # 把可调用函数清单交给模型
        tools = registry.get_definitions()

        # 模型决定调用 echo，参数通过 arguments 传入
        result = await registry.execute("echo", {"text": "hello"})

    内部结构为 ``{工具名: Tool 实例}``，同名注册会覆盖。
    """

    def __init__(self) -> None:
        # name -> Tool 实例 的映射表
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册一个工具。

        以 ``tool.name`` 作为 key 存入内部映射。若同名工具已存在，
        会被新工具覆盖（即允许热替换）。
        """
        self._tools[tool.name] = tool

    def get_definitions(self) -> List[dict]:
        """返回所有工具的 OpenAI function-calling 定义列表。

        顺序与注册顺序一致（基于 dict 的插入序）。直接把返回值放进
        OpenAI 接口的 ``tools`` 参数即可。
        """
        return [tool.to_function_definition() for tool in self._tools.values()]

    def list_tools(self) -> List[str]:
        """返回所有已注册工具的名称列表（按注册顺序）。"""
        return list(self._tools.keys())

    async def execute(self, name: str, arguments: dict) -> str:
        """按名称异步执行某个工具。

        参数：
            name: 工具名（即 ``Tool.name``）。
            arguments: 传给工具 ``execute`` 的关键字参数。

        返回：
            - 执行成功：工具的返回字符串。
            - 工具不存在：形如 ``"错误：未找到工具 'xxx'"`` 的提示。
            - 执行抛异常：形如 ``"工具 'xxx' 执行出错：<异常信息>"`` 的提示。

        任何情况下都不会向外抛出未捕获异常，便于模型安全处理失败。
        """
        tool = self._tools.get(name)
        if tool is None:
            return f"错误：未找到工具 '{name}'"

        try:
            return await asyncio.wait_for(
                tool.execute(**arguments), timeout=TOOL_EXEC_TIMEOUT
            )
        except asyncio.TimeoutError:
            # 兜底超时：避免无自带超时的工具卡死整轮
            return f"工具 '{name}' 执行超时（{TOOL_EXEC_TIMEOUT}秒），已终止"
        except Exception as exc:  # noqa: BLE001 - 统一把异常转成字符串反馈给模型
            return f"工具 '{name}' 执行出错：{exc}"
