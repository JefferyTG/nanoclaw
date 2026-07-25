"""子 Agent 衍生工具（SpawnSubagentTool）。

允许主 Agent 派生一个临时的 ``AgentLoop`` 去执行一个独立的子任务，
并把子任务的结果收集回来。子 Agent 具备：

- 独立的 Provider（由 ``provider_factory`` 按指定 model 或默认创建；
  默认模型可经 ``config.subagent_model`` 配置，实现子 Agent 与主 Agent 用不同模型）；
- 从父级工具注册表复制而来的工具集（自动跳过 ``spawn_subagent`` 自身，
  避免无限自指）；
- 一条固定的一句话 System Prompt（"你是任务专员，完成任务直接输出结果"），
  不走主 Agent 的人设 / 记忆装配；
- 一个 ``DummySessionManager``，不落盘、不跨轮持久化（子任务无状态、跑完即弃）。

递归深度受 ``current_depth`` / ``max_depth`` 控制：只有还有余量时，
子 Agent 才会被注入一个 depth+1 的 ``SpawnSubagentTool``，从而支持
「任务分解 → 子任务再分解」的多层结构，但绝不会超过 ``max_depth`` 层。
"""

from typing import Optional

from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from agent.context import ContextBuilder
from session.manager import SessionManager
from agent.loop import AgentLoop


# 子 Agent 的固定 System Prompt：轻量、专注，不走主 Agent 的人设/记忆装配
_SUBAGENT_SYSTEM_PROMPT = "你是任务专员，完成任务直接输出结果"


class DummySessionManager(SessionManager):
    """不持久化、不跨轮记忆的会话管理器，专供一次性子 Agent 使用。

    所有读写方法均为 no-op：子任务应当是「无状态、跑完即弃」的，
    不需要把历史写盘，也不需要跨进程恢复。
    """

    def get_history(self, session_key: str) -> list:
        return []

    def save_message(self, session_key: str, message: dict) -> None:
        pass

    def save_messages(self, session_key: str, messages: list) -> None:
        pass

    def clear(self, session_key: str) -> None:
        pass


class _TaskContextBuilder(ContextBuilder):
    """子 Agent 专用上下文构建器：System Prompt 固定为那一句话。"""

    def build_system_prompt(self) -> str:
        return _SUBAGENT_SYSTEM_PROMPT


class SpawnSubagentTool(Tool):
    """派生一个临时子 Agent 去执行子任务，并返回其结果。

    主 Agent 可用它把「大任务」拆给子 Agent 处理，子 Agent 拥有独立
    Provider 与（复制自父级的）工具集；递归深度受 ``max_depth`` 约束，
    防止无界嵌套。
    """

    name = "spawn_subagent"
    description = (
        "派生一个独立的子 Agent 去完成一个明确的子任务，并返回子任务的执行结果。"
        "适用于需要把复杂任务拆解、或需要独立上下文处理的场景。"
        "参数 task 描述子任务（尽量清晰、自包含）；可选 model 指定子 Agent 使用的模型。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "要交给子 Agent 完成的子任务描述，应清晰、自包含。",
            },
            "model": {
                "type": "string",
                "description": "可选，子 Agent 使用的模型名；省略则使用默认模型。",
            },
        },
        "required": ["task"],
    }

    def __init__(
        self,
        provider_factory,
        tools_registry: ToolRegistry,
        workspace: str,
        current_depth: int = 0,
        max_depth: int = 2,
        config=None,
    ):
        # provider_factory: 接收 model(可 None) 返回 LLMProvider 实例；
        #   model 为 None 时应由 factory 回退到默认模型。
        self.provider_factory = provider_factory
        # tools_registry: 父级工具注册表，子 Agent 的工具从这里复制（跳过自身）
        self.tools_registry = tools_registry
        self.workspace = workspace
        self.current_depth = current_depth
        self.max_depth = max_depth
        # config: 可选，提供 config.subagent_model 作为子 Agent 默认模型来源；
        #   为 None 时由 provider_factory 回退到主模型。
        self.config = config

    async def execute(self, **kwargs) -> str:
        task = kwargs.get("task")
        if not task:
            return "错误：spawn_subagent 需要必填参数 task（子任务描述）。"
        # 模型解析优先级：调用时显式 model > config.subagent_model > config.model
        # （config 为 None 时退化为 provider_factory 的默认模型）。
        explicit_model = kwargs.get("model")
        default_model = None
        if self.config is not None:
            default_model = getattr(self.config, "subagent_model", None) or getattr(
                self.config, "model", None
            )
        model = explicit_model or default_model

        # 1) 创建子 Agent 的 Provider
        provider = self.provider_factory(model)

        # 2) 复制父级工具，跳过 spawn_subagent 自身避免自指。
        #    直接遍历注册表内部的工具实例表（项目未提供公开的「迭代全部工具」方法）。
        child_registry = ToolRegistry()
        for tool in self.tools_registry._tools.values():
            if tool.name == "spawn_subagent":
                continue
            child_registry.register(tool)

        # 3) 若还有深度余量，给子 Agent 也注入一个 depth+1 的衍生工具，
        #    使其能继续分解更细的子任务（受 max_depth 限制）。
        if self.current_depth + 1 < self.max_depth:
            child_registry.register(
                SpawnSubagentTool(
                    provider_factory=self.provider_factory,
                    tools_registry=self.tools_registry,
                    workspace=self.workspace,
                    current_depth=self.current_depth + 1,
                    max_depth=self.max_depth,
                    config=self.config,
                )
            )

        # 4) 子 Agent 的上下文：固定一句话 System Prompt，不加载人设/记忆
        context = _TaskContextBuilder(self.workspace)

        # 5) 子 Agent 的会话管理：Dummy（不落盘、无状态）
        session_manager = DummySessionManager()

        # 6) 创建并运行临时子 Agent（不启用压缩：子任务短、无需历史压缩）
        agent = AgentLoop(
            provider=provider,
            tools=child_registry,
            context=context,
            session_manager=session_manager,
            session_key=f"spawn:{self.current_depth + 1}",
            model=model,
        )

        try:
            result = await agent.run(task)
        except Exception as exc:  # noqa: BLE001 - 子任务失败不应连累主 Agent
            return f"子 Agent 执行出错：{exc}"

        return result or "（子 Agent 未返回有效结果）"
