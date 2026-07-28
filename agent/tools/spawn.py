"""通用/场景两种模式的临时子 Agent 衍生工具。

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

import asyncio
import os
import time
import uuid
from typing import Optional

from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from agent.tools.skills_tools import (
    ListSkillsTool,
    LoadSkillTool,
    ReadSkillResourceTool,
)
from agent.context import ContextBuilder
from agent.profiles import AgentProfileError, AgentProfileLoader
from agent.scene_policy import SCENE_FORBIDDEN_TOOLS
from agent.scene_assets import SceneSkillAssetError, SceneSkillAssets, SceneToolAssets
from agent.skills import CompositeSkillsLoader, SkillsLoader
from agent.tool_factories import ToolFactoryError, ToolFactoryRegistry
from session.manager import SessionManager
from agent.loop import AgentLoop


# 子 Agent 的固定 System Prompt：轻量、专注，不走主 Agent 的人设/记忆装配
_SUBAGENT_SYSTEM_PROMPT = "你是任务专员，完成任务直接输出结果"
_SUBAGENT_RESULT_LIMIT = 8_000


def _bounded_text(value, limit: int) -> str:
    """Keep persisted replay metadata useful without letting one run bloat JSONL."""
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "…（已截断）"

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
    """子 Agent 专用上下文：固定任务提示词，可附受限 Skill 摘要。"""

    def __init__(self, workspace: str, system_prompt: str, skills_summary: str = ""):
        super().__init__(workspace, skills_summary=skills_summary)
        self.system_prompt = system_prompt

    def build_system_prompt(self) -> str:
        prompt = self.system_prompt
        if self.skills_summary:
            prompt += "\n\n## 可用技能\n" + self.skills_summary
        return prompt


class _SceneFilesystemTool(Tool):
    """Apply scene-specific realpath boundaries to generic filesystem tools."""

    def __init__(
        self,
        delegate: Tool,
        workspace: str,
        skills_dir: str,
        allowed_skill_dirs: list[str],
        protected_roots: Optional[list[str]] = None,
    ) -> None:
        self.delegate = delegate
        self.workspace = os.path.realpath(os.path.abspath(workspace))
        self.skills_dir = os.path.realpath(os.path.abspath(skills_dir))
        self.allowed_skill_dirs = [os.path.realpath(path) for path in allowed_skill_dirs]
        self.protected_roots = [
            os.path.realpath(path) for path in (protected_roots or [])
        ]

    @property
    def name(self) -> str:
        return self.delegate.name

    @property
    def description(self) -> str:
        return self.delegate.description

    @property
    def parameters(self) -> dict:
        return self.delegate.parameters

    @staticmethod
    def _inside(path: str, root: str) -> bool:
        try:
            return os.path.commonpath((path, root)) == root
        except ValueError:
            return False

    async def execute(self, **kwargs) -> str:
        key = "dir_path" if self.name == "list_dir" else "file_path"
        user_path = kwargs.get(key, "")
        if not isinstance(user_path, str):
            return "错误：文件路径必须是字符串。"
        lexical_target = os.path.abspath(os.path.join(self.workspace, user_path))
        target = os.path.realpath(lexical_target)
        if not self._inside(lexical_target, self.workspace) or not self._inside(
            target, self.workspace
        ):
            return "错误：场景 Agent 文件路径越过工作区边界。"
        if any(self._inside(target, root) for root in self.protected_roots):
            return "错误：场景 Agent 无权访问 Agent Profile、私有 Skill 或私有工具配置。"
        if self._inside(target, self.skills_dir):
            if self.name == "write_file":
                return "错误：场景 Agent 不能修改共享 Skill；请由主 Agent 或用户配置。"
            if not any(
                self._inside(target, allowed) for allowed in self.allowed_skill_dirs
            ):
                return (
                    "错误：场景 Agent 无权访问未授权的 Skill 目录；"
                    "请使用 list_skills / load_skill 访问白名单 Skill。"
                )
        return await self.delegate.execute(**kwargs)


class SpawnSubagentTool(Tool):
    """派生一个临时子 Agent 去执行子任务，并返回其结果。

    主 Agent 可用它把「大任务」拆给子 Agent 处理，子 Agent 拥有独立
    Provider 与（复制自父级的）工具集；递归深度受 ``max_depth`` 约束，
    防止无界嵌套。
    """

    # 子 Agent 的完整生命周期由其 AgentLoop 的 max_iterations 和
    # turn_timeout 管理，不应被普通工具的 Registry 兜底超时提前取消。
    execution_timeout_sec = None

    name = "spawn_subagent"
    description = (
        "派生一个独立的子 Agent 去完成一个明确的子任务，并返回子任务的执行结果。"
        "适用于需要把复杂任务拆解、或需要独立上下文处理的场景。"
        "不指定 agent_name 时派生通用子 Agent；指定时加载对应场景 Agent Profile。"
        "参数 task 应清晰、自包含；可选 model 可临时覆盖子 Agent 模型。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "要交给子 Agent 完成的子任务描述，应清晰、自包含。",
            },
            "agent_name": {
                "type": "string",
                "description": "可选，已创建的场景 Agent 名称；省略则使用通用临时子 Agent。",
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
        skills_loader: Optional[SkillsLoader] = None,
        root_skills_loader: Optional[SkillsLoader] = None,
        profile_loader: Optional[AgentProfileLoader] = None,
        scene_skill_assets: Optional[SceneSkillAssets] = None,
        scene_tool_assets: Optional[SceneToolAssets] = None,
        tool_factories: Optional[ToolFactoryRegistry] = None,
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
        self.skills_loader = skills_loader
        self.root_skills_loader = root_skills_loader or skills_loader
        self.profile_loader = profile_loader
        self.scene_skill_assets = scene_skill_assets or SceneSkillAssets(workspace)
        self.scene_tool_assets = scene_tool_assets or SceneToolAssets(workspace)
        self.tool_factories = tool_factories or ToolFactoryRegistry()
        self.current_depth = current_depth
        self.max_depth = max_depth
        # config: 可选，提供 config.subagent_model 作为子 Agent 默认模型来源；
        #   为 None 时由 provider_factory 回退到主模型。
        self.config = config

    async def execute(self, **kwargs) -> str:
        task = kwargs.get("task")
        if not task:
            return "错误：spawn_subagent 需要必填参数 task（子任务描述）。"
        agent_name = kwargs.get("agent_name")
        if agent_name is not None and (
            not isinstance(agent_name, str) or not agent_name.strip()
        ):
            return "错误：agent_name 必须是非空字符串；省略该参数可使用通用子 Agent。"

        profile = None
        if agent_name:
            if self.profile_loader is None:
                return "错误：当前实例未配置场景 Agent Profile Loader。"
            try:
                profile = self.profile_loader.get_profile(agent_name.strip())
            except (AgentProfileError, OSError, ValueError, UnicodeDecodeError) as exc:
                return f"错误：无法加载场景 Agent '{agent_name}'：{exc}"
            if profile is None:
                available = self.profile_loader.build_summary()
                suffix = available if available else "（当前没有可用的场景 Agent）"
                return f"错误：场景 Agent '{agent_name}' 不存在。\n当前可用 Agent：\n{suffix}"

        # 模型解析优先级：调用时 model > Profile.model > subagent_model > model。
        explicit_model = kwargs.get("model")
        default_model = None
        if self.config is not None:
            default_model = getattr(self.config, "subagent_model", None) or getattr(
                self.config, "model", None
            )
        model = explicit_model or (profile.model if profile else None) or default_model

        # 这些内部参数只由父 AgentLoop 注入，不属于模型可见 schema。子 Agent
        # 使用父 session_key 保存图片；其内部事件统一封装为 subagent_event，
        # 从而不会把 child 的 token/done 混入父回复流。
        parent_stream_sink = kwargs.get("_parent_stream_sink")
        parent_session_key = kwargs.get("_parent_session_key")
        parent_generated_ids = kwargs.get("_parent_generated_ids")
        parent_subagent_runs = kwargs.get("_parent_subagent_runs")
        parent_tool_call_id = kwargs.get("_parent_tool_call_id")
        run_id = uuid.uuid4().hex
        depth = self.current_depth + 1
        display_name = profile.name if profile is not None else "通用子 Agent"
        run_record = {
            "run_id": run_id,
            "agent_name": display_name,
            "depth": depth,
            "tool_call_id": parent_tool_call_id,
            "status": "running",
            "final_result": "",
            "tool_steps": [],
            "image_ids": [],
        }
        started_at = None
        finished = False

        async def emit(event: dict) -> None:
            if parent_stream_sink is None:
                return
            # Nested SpawnSubagentTool already sends namespaced events through
            # its parent adapter. Forward them once, preserving the inner run's
            # identity rather than wrapping them in every ancestor namespace.
            if event.get("type") == "subagent_event":
                await parent_stream_sink(event)
                return
            event_type = event.get("type", "unknown")
            # AgentLoop emits a child-level done before run() returns. Delay the
            # forwarded terminal event until finish() so it has an explicit
            # status and cannot be mistaken for the parent AgentLoop's done.
            if event_type == "done":
                return
            if event_type == "tool_call":
                run_record["tool_steps"].append({
                    "name": event.get("name", ""),
                    "status": "running",
                })
            elif event_type == "tool_result":
                for step in reversed(run_record["tool_steps"]):
                    if step["name"] == event.get("name") and step["status"] == "running":
                        step["status"] = "completed"
                        step["duration_ms"] = event.get("duration_ms", 0)
                        break
            elif event_type == "image" and event.get("id"):
                run_record["image_ids"].append(event["id"])
            await parent_stream_sink({
                "type": "subagent_event",
                "run_id": run_id,
                "agent_name": display_name,
                "depth": depth,
                "event": dict(event),
            })

        async def finish(status: str, content: str = "") -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            run_record["status"] = status
            run_record["final_result"] = _bounded_text(content, _SUBAGENT_RESULT_LIMIT)
            if started_at is not None:
                run_record["duration_ms"] = int((time.monotonic() - started_at) * 1000)
            if isinstance(parent_subagent_runs, list):
                parent_subagent_runs.append(dict(run_record))
            if parent_stream_sink is not None:
                await parent_stream_sink({
                    "type": "subagent_event",
                    "run_id": run_id,
                    "agent_name": display_name,
                    "depth": depth,
                    "event": {"type": "done", "status": status, "content": content},
                })

        # 1) 创建子 Agent 的 Provider
        provider = self.provider_factory(model)

        child_registry = ToolRegistry()
        if profile is None:
            # 通用模式：继承父级工具，仅跳过原始 spawn；深度允许时注入新的 spawn。
            for tool in self.tools_registry.iter_tools():
                if tool.name != "spawn_subagent":
                    child_registry.register(tool)
            if self.current_depth + 1 < self.max_depth:
                child_registry.register(
                    SpawnSubagentTool(
                        provider_factory=self.provider_factory,
                        tools_registry=child_registry,
                        workspace=self.workspace,
                        skills_loader=self.skills_loader,
                        root_skills_loader=self.root_skills_loader,
                        profile_loader=self.profile_loader,
                        scene_skill_assets=self.scene_skill_assets,
                        scene_tool_assets=self.scene_tool_assets,
                        tool_factories=self.tool_factories,
                        current_depth=depth,
                        max_depth=self.max_depth,
                        config=self.config,
                    )
                )
            skills_summary = (
                self.skills_loader.build_skills_summary() if self.skills_loader else ""
            )
            context = _TaskContextBuilder(
                self.workspace, _SUBAGENT_SYSTEM_PROMPT, skills_summary
            )
            session_suffix = f"generic:{self.current_depth + 1}"
        else:
            # 场景模式：只装配 Profile 白名单和目标 Agent 的私有资产。
            forbidden = sorted(set(profile.tools) & SCENE_FORBIDDEN_TOOLS)
            if forbidden:
                return "错误：场景 Agent 禁止直接使用以下控制面管理工具：" + ", ".join(
                    forbidden
                )
            unavailable = [
                name for name in profile.tools if self.tools_registry.get(name) is None
            ]
            if unavailable:
                return "错误：Agent Profile 引用了当前不可用的工具：" + ", ".join(unavailable)
            known_skills = (
                {skill["name"] for skill in self.root_skills_loader.list_skills()}
                if self.root_skills_loader is not None
                else set()
            )
            missing_skills = [name for name in profile.skills if name not in known_skills]
            if missing_skills:
                return "错误：Agent Profile 引用了当前不可用的 Skill：" + ", ".join(
                    missing_skills
                )
            shared_skills = (
                self.root_skills_loader.filtered(profile.skills)
                if self.root_skills_loader is not None
                else SkillsLoader("__missing_skills__", allowed_names=[])
            )
            try:
                private_skills = self.scene_skill_assets.for_agent(
                    profile.name, allowed_names=profile.private_skills
                )
                known_private_skills = {
                    skill["name"] for skill in private_skills.list_skills()
                }
            except (SceneSkillAssetError, OSError, UnicodeDecodeError) as exc:
                return f"错误：无法加载场景 Agent 私有 Skill：{exc}"
            missing_private_skills = [
                name for name in profile.private_skills if name not in known_private_skills
            ]
            if missing_private_skills:
                return "错误：Agent Profile 引用了不存在的私有 Skill：" + ", ".join(
                    missing_private_skills
                )
            try:
                filtered_skills = CompositeSkillsLoader(
                    [("共享", shared_skills), ("私有", private_skills)]
                )
            except ValueError as exc:
                return f"错误：无法装配场景 Agent Skill：{exc}"
            allowed_skill_dirs = [
                os.path.dirname(skill["path"]) for skill in shared_skills.list_skills()
            ]
            wants_spawn = "spawn_subagent" in profile.tools
            for name in profile.tools:
                if name in (
                    "spawn_subagent",
                    "list_skills",
                    "load_skill",
                    "read_skill_resource",
                ):
                    continue
                tool = self.tools_registry.get(name)
                if tool is not None:
                    if name in ("read_file", "write_file", "list_dir"):
                        tool = _SceneFilesystemTool(
                            tool,
                            self.workspace,
                            shared_skills.skills_dir,
                            allowed_skill_dirs,
                            protected_roots=[self.profile_loader.profiles_dir],
                        )
                    child_registry.register(tool)
            private_manifests = []
            try:
                for name in profile.private_tools:
                    if name in SCENE_FORBIDDEN_TOOLS:
                        return f"错误：私有工具名称 '{name}' 属于场景 Agent 禁用能力。"
                    manifest = self.scene_tool_assets.load_tool(profile.name, name)
                    if manifest is None:
                        return f"错误：Agent Profile 引用了不存在的私有工具：{name}"
                    private_manifests.append(manifest)
                private_tools = self.tool_factories.build_many(private_manifests)
            except (SceneSkillAssetError, ToolFactoryError) as exc:
                return f"错误：无法装配场景 Agent 私有工具：{exc}"
            reserved_tool_names = set(self.tools_registry.list_tools())
            duplicate_tools = sorted(
                reserved_tool_names & {tool.name for tool in private_tools}
            )
            if duplicate_tools:
                return "错误：私有工具名称与主工具注册表冲突：" + ", ".join(duplicate_tools)
            for tool in private_tools:
                child_registry.register(tool)
            if profile.skills or profile.private_skills or any(
                name in profile.tools
                for name in ("list_skills", "load_skill", "read_skill_resource")
            ):
                child_registry.register(ListSkillsTool(filtered_skills))
                child_registry.register(LoadSkillTool(filtered_skills))
                child_registry.register(ReadSkillResourceTool(filtered_skills))
            if wants_spawn and self.current_depth + 1 < self.max_depth:
                # Bind delegation to the scene's already-filtered registry so a
                # child cannot gain capabilities that its parent did not have.
                child_registry.register(
                    SpawnSubagentTool(
                        provider_factory=self.provider_factory,
                        tools_registry=child_registry,
                        workspace=self.workspace,
                        skills_loader=filtered_skills,
                        root_skills_loader=self.root_skills_loader,
                        profile_loader=self.profile_loader,
                        scene_skill_assets=self.scene_skill_assets,
                        scene_tool_assets=self.scene_tool_assets,
                        tool_factories=self.tool_factories,
                        current_depth=depth,
                        max_depth=self.max_depth,
                        config=self.config,
                    )
                )
            context = _TaskContextBuilder(
                self.workspace,
                profile.system_prompt,
                filtered_skills.build_skills_summary(),
            )
            session_suffix = f"scene:{profile.name}"

        session_manager = DummySessionManager()

        agent = AgentLoop(
            provider=provider,
            tools=child_registry,
            context=context,
            session_manager=session_manager,
            # The child remains stateless, but image tools must write under the
            # parent key so `/image?key=...` and historical replay resolve them.
            session_key=parent_session_key or f"spawn:{session_suffix}",
            model=model,
            max_iterations=getattr(self.config, "max_iterations", 32),
            turn_timeout=getattr(self.config, "turn_timeout_sec", 600),
            generated_ids_sink=(
                parent_generated_ids if isinstance(parent_generated_ids, list) else None
            ),
            subagent_runs_sink=(
                parent_subagent_runs if isinstance(parent_subagent_runs, list) else None
            ),
        )

        started_at = time.monotonic()
        try:
            await emit({"type": "start", "task": task})
            if parent_stream_sink is None:
                result = await agent.run(task)
            else:
                result = await agent.run(task, stream_sink=emit)
        except asyncio.CancelledError:
            # Cancellation is lifecycle control, not a normal tool failure. It
            # must reach the caller so image/http resources in the child unwind.
            await finish("cancelled", "子 Agent 已取消。")
            raise
        except Exception as exc:  # noqa: BLE001 - child failures are tool results
            result = f"子 Agent 执行出错：{exc}"
            await finish("error", result)
            return result

        result = result or "（子 Agent 未返回有效结果）"
        status = getattr(agent, "last_run_status", "completed")
        if status not in {"completed", "error", "timed_out"}:
            status = "completed"
        await finish(status, result)
        return result
