"""工具注册表。

本模块提供 `ToolRegistry`，用于在单个 Agent 会话内集中管理所有 `Tool` 实例。
它负责三件事：把工具登记进来、对外暴露成 OpenAI 可用的函数定义列表、
按名字异步调度执行。Agent 只需持有一个 registry，就能统一处理「模型要求
调用某个工具」的全部链路，而不必自己维护映射表与异常包装。

设计要点：
- 内部用 ``dict[str, Tool]`` 存工具，key 为工具名（取自 ``tool.name``），
  保证同名工具只能存在一个，后注册的会自然覆盖前一个。
- ``get_definitions`` 复用每个工具自带的 ``to_function_definition``，并按工具名
  排序；最终装配后可冻结为不可变逻辑快照和稳定 hash。
- ``execute`` 是异步的，内部 ``await tool.execute``，并捕获任何异常：
  工具找不到、执行报错都不会让 Agent 主链路炸掉，而是把错误转成字符串
  返回，模型可以据此决定下一步（重试 / 换工具 / 直接回答）。
"""

from collections.abc import Iterable
from copy import deepcopy
import hashlib
import json
from typing import Dict, List, Optional

import asyncio

from agent.tools.base import Tool


# 普通工具的默认单次调用兜底超时（秒）。每个 Tool 可通过
# execution_timeout_sec 覆盖；None 表示不使用 Registry 外层超时，由工具自身
# 的生命周期和取消语义负责。Shell 工具有更紧的 60s 内部超时，会先于此值触发。
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
        self._frozen_definitions: Optional[tuple[dict, ...]] = None
        self._schema_hash: Optional[str] = None

    def register(self, tool: Tool) -> None:
        """注册一个工具。

        以 ``tool.name`` 作为 key 存入内部映射。若同名工具已存在，
        会被新工具覆盖（即允许热替换）。
        """
        if self._frozen_definitions is not None:
            raise RuntimeError("工具注册表已冻结，不能再修改 Schema")
        self._tools[tool.name] = tool

    def freeze(self) -> str:
        """冻结最终工具 Schema，并返回不含参数值的稳定 SHA-256 hash。

        冻结应发生在内置工具和 MCP 条件工具全部注册结束之后。工具定义按名称
        排序并深拷贝，后续每次模型请求都复用同一份逻辑快照；工具集合变化必须
        通过创建新的注册表（即新的显式 cache boundary）生效。
        """
        if self._frozen_definitions is None:
            definitions = self._build_definitions()
            self._frozen_definitions = tuple(deepcopy(definitions))
            payload = json.dumps(
                definitions,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self._schema_hash = hashlib.sha256(payload).hexdigest()
        return self._schema_hash or ""

    @property
    def is_frozen(self) -> bool:
        return self._frozen_definitions is not None

    @property
    def schema_hash(self) -> str:
        """返回当前确定性 Schema hash；未冻结时按当前内容即时计算。"""
        if self._schema_hash is not None:
            return self._schema_hash
        payload = json.dumps(
            self._build_definitions(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _build_definitions(self) -> List[dict]:
        return [
            self._tools[name].to_function_definition()
            for name in sorted(self._tools)
        ]

    def get_definitions(self) -> List[dict]:
        """返回所有工具的 OpenAI function-calling 定义列表。

        顺序按工具名确定，不受内置/MCP 注册先后影响。直接把返回值放进
        OpenAI 接口的 ``tools`` 参数即可。
        """
        if self._frozen_definitions is not None:
            return deepcopy(list(self._frozen_definitions))
        return deepcopy(self._build_definitions())

    def list_tools(self) -> List[str]:
        """返回所有已注册工具的名称列表（按名称排序）。"""
        return sorted(self._tools)

    def get(self, name: str) -> Optional[Tool]:
        """按名称返回工具实例；不存在时返回 ``None``。"""
        return self._tools.get(name)

    def get_many(self, names: Iterable[str]) -> List[Tool]:
        """按输入顺序返回存在的工具，忽略未知名称。"""
        return [self._tools[name] for name in names if name in self._tools]

    def iter_tools(self):
        """按名称顺序迭代工具实例，不暴露内部映射。"""
        return iter(self._tools[name] for name in sorted(self._tools))

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

        timeout = getattr(tool, "execution_timeout_sec", TOOL_EXEC_TIMEOUT)
        if timeout is None:
            # 不捕获 CancelledError：调用方关闭或回合取消时必须传递到工具，
            # 由工具的 finally/上下文管理器释放自身资源。
            try:
                return await tool.execute(**arguments)
            except Exception as exc:  # noqa: BLE001 - 统一把异常转成字符串反馈给模型
                return f"工具 '{name}' 执行出错：{exc}"

        try:
            return await asyncio.wait_for(tool.execute(**arguments), timeout=timeout)
        except asyncio.TimeoutError:
            # 兜底超时：避免无自带超时的工具卡死整轮
            return f"工具 '{name}' 执行超时（{timeout}秒），已终止"
        except Exception as exc:  # noqa: BLE001 - 统一把异常转成字符串反馈给模型
            return f"工具 '{name}' 执行出错：{exc}"
