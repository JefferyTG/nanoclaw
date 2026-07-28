"""Main-Agent-only tools for managing reusable scene-agent assets."""

import yaml

from agent.profiles import AgentProfileError, AgentProfileLoader
from agent.scene_policy import SCENE_FORBIDDEN_TOOLS
from agent.scene_assets import SceneSkillAssetError, SceneSkillAssets, SceneToolAssets
from agent.skills import SkillsLoader
from agent.tool_factories import ToolFactoryError, ToolFactoryRegistry
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry


def _profile_or_error(loader: AgentProfileLoader, name: object):
    try:
        profile = loader.get_profile(name)  # type: ignore[arg-type]
    except (AgentProfileError, OSError, ValueError, UnicodeDecodeError) as exc:
        return None, f"无法加载场景 Agent：{exc}"
    if profile is None:
        return None, f"场景 Agent '{name}' 不存在。"
    return profile, ""


class CreateAgentTool(Tool):
    """Validate requested shared capabilities and persist a v2 Profile."""

    name = "create_agent"
    description = (
        "创建一个可长期复用的场景 Agent Profile。必须明确提供最小共享工具和 Skill "
        "白名单；私有能力创建后再用 create_agent_skill/create_agent_tool 添加。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "唯一名称，只能含字母、数字、_、-。"},
            "description": {"type": "string", "description": "给主 Agent 看的简短职责。"},
            "system_prompt": {"type": "string", "description": "场景 Agent 的系统提示词。"},
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "允许使用的现有共享工具名称白名单，可包含高权限工具。",
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "允许访问的共享 Skill 名称白名单。",
            },
            "model": {
                "type": "string",
                "description": "可选默认模型；留空则使用全局子 Agent 默认模型。",
            },
        },
        "required": ["name", "description", "system_prompt", "tools", "skills"],
    }

    def __init__(
        self,
        profile_loader: AgentProfileLoader,
        tools_registry: ToolRegistry,
        skills_loader: SkillsLoader,
    ) -> None:
        self.profile_loader = profile_loader
        self.tools_registry = tools_registry
        self.skills_loader = skills_loader

    async def execute(self, **kwargs) -> str:
        requested_tools = kwargs.get("tools")
        requested_skills = kwargs.get("skills")
        if not isinstance(requested_tools, list) or any(
            not isinstance(name, str) or not name.strip() for name in requested_tools
        ):
            return "错误：tools 必须是字符串列表。"
        if not isinstance(requested_skills, list) or any(
            not isinstance(name, str) or not name.strip() for name in requested_skills
        ):
            return "错误：skills 必须是字符串列表。"

        tool_names = [name.strip() for name in requested_tools]
        forbidden = sorted(set(tool_names) & SCENE_FORBIDDEN_TOOLS)
        if forbidden:
            return "错误：场景 Agent 禁止直接使用以下控制面管理工具：" + ", ".join(forbidden)
        known_tools = set(self.tools_registry.list_tools())
        unknown_tools = sorted(set(tool_names) - known_tools)
        if unknown_tools:
            return "错误：以下工具不存在：" + ", ".join(unknown_tools)
        skill_names = [name.strip() for name in requested_skills]
        known_skills = {skill["name"] for skill in self.skills_loader.list_skills()}
        unknown_skills = sorted(set(skill_names) - known_skills)
        if unknown_skills:
            return "错误：以下 Skill 不存在：" + ", ".join(unknown_skills)

        data = {
            "name": kwargs.get("name"),
            "description": kwargs.get("description"),
            "system_prompt": kwargs.get("system_prompt"),
            "model": kwargs.get("model") or "",
            "tools": tool_names,
            "skills": skill_names,
            "private_tools": [],
            "private_skills": [],
        }
        try:
            profile = self.profile_loader.create_profile(data)
        except (AgentProfileError, OSError) as exc:
            return f"创建场景 Agent 失败：{exc}"

        return "\n".join(
            [
                f"已创建场景 Agent '{profile.name}'：{profile.description}",
                "共享工具白名单：" + (", ".join(profile.tools) if profile.tools else "（无）"),
                "共享 Skill 白名单：" + (", ".join(profile.skills) if profile.skills else "（无）"),
                "私有能力：（无，可由主 Agent 后续创建）",
            ]
        )


class ListAgentsTool(Tool):
    """List persisted scene agents; generic temporary children are omitted."""

    name = "list_agents"
    description = "列出当前所有可派遣的场景 Agent 名称和职责简介。"
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, profile_loader: AgentProfileLoader) -> None:
        self.profile_loader = profile_loader

    async def execute(self, **kwargs) -> str:
        summary = self.profile_loader.build_summary()
        if not summary:
            return "当前没有已创建的场景 Agent。"
        return "可派遣的场景 Agent：\n" + summary


class CreateAgentSkillTool(Tool):
    """Create and attach one private Skill; never exposed to a scene child."""

    name = "create_agent_skill"
    description = "为已存在的场景 Agent 创建只属于它的私有 Skill，并加入 Profile。"
    parameters = {
        "type": "object",
        "properties": {
            "agent_name": {"type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "instructions": {"type": "string", "description": "SKILL.md 正文。"},
        },
        "required": ["agent_name", "name", "description", "instructions"],
    }

    def __init__(
        self,
        profile_loader: AgentProfileLoader,
        assets: SceneSkillAssets,
        shared_skills: SkillsLoader,
    ) -> None:
        self.profile_loader = profile_loader
        self.assets = assets
        self.shared_skills = shared_skills

    async def execute(self, **kwargs) -> str:
        agent_name = kwargs.get("agent_name")
        profile, error = _profile_or_error(self.profile_loader, agent_name)
        if profile is None:
            return "错误：" + error
        name = kwargs.get("name")
        description = kwargs.get("description")
        instructions = kwargs.get("instructions")
        if not isinstance(description, str) or not description.strip():
            return "错误：description 不能为空。"
        if not isinstance(instructions, str) or not instructions.strip():
            return "错误：instructions 不能为空。"
        if name in {skill["name"] for skill in self.shared_skills.list_skills()}:
            return f"错误：私有 Skill '{name}' 与共享 Skill 重名。"
        try:
            normalized = self.assets.validate_name(name, "Skill")
            frontmatter = yaml.safe_dump(
                {"name": normalized, "description": description.strip()},
                allow_unicode=True,
                sort_keys=False,
            ).strip()
            self.assets.create_skill(
                profile.name, normalized, f"---\n{frontmatter}\n---\n\n{instructions.strip()}\n"
            )
            updated = self.profile_loader.update_profile(
                profile.name,
                {"private_skills": [*profile.private_skills, normalized]},
            )
        except (SceneSkillAssetError, AgentProfileError, OSError) as exc:
            return f"创建私有 Skill 失败：{exc}"
        return f"已为场景 Agent '{updated.name}' 创建并启用私有 Skill '{normalized}'。"


class UpdateAgentSkillTool(CreateAgentSkillTool):
    """Update an existing private Skill without changing its assignment."""

    name = "update_agent_skill"
    description = "更新已存在场景 Agent 私有 Skill 的描述和正文。"

    async def execute(self, **kwargs) -> str:
        agent_name = kwargs.get("agent_name")
        profile, error = _profile_or_error(self.profile_loader, agent_name)
        if profile is None:
            return "错误：" + error
        name = kwargs.get("name")
        description = kwargs.get("description")
        instructions = kwargs.get("instructions")
        if name not in profile.private_skills:
            return f"错误：私有 Skill '{name}' 未配置给场景 Agent '{profile.name}'。"
        if not isinstance(description, str) or not description.strip():
            return "错误：description 不能为空。"
        if not isinstance(instructions, str) or not instructions.strip():
            return "错误：instructions 不能为空。"
        try:
            normalized = self.assets.validate_name(name, "Skill")
            frontmatter = yaml.safe_dump(
                {"name": normalized, "description": description.strip()},
                allow_unicode=True,
                sort_keys=False,
            ).strip()
            self.assets.update_skill(
                profile.name, normalized, f"---\n{frontmatter}\n---\n\n{instructions.strip()}\n"
            )
        except (SceneSkillAssetError, OSError) as exc:
            return f"更新私有 Skill 失败：{exc}"
        return f"已更新场景 Agent '{profile.name}' 的私有 Skill '{normalized}'。"


class CreateAgentPrivateTool(Tool):
    """Create one reviewed-factory manifest and attach it to a Profile."""

    name = "create_agent_tool"
    description = "为场景 Agent 创建受控私有工具配置；只允许代码中已注册的工具工厂。"
    parameters = {
        "type": "object",
        "properties": {
            "agent_name": {"type": "string"},
            "name": {"type": "string"},
            "factory": {"type": "string"},
            "config": {"type": "object"},
        },
        "required": ["agent_name", "name", "factory", "config"],
    }

    def __init__(
        self,
        profile_loader: AgentProfileLoader,
        assets: SceneToolAssets,
        factories: ToolFactoryRegistry,
        tools_registry: ToolRegistry | None = None,
    ) -> None:
        self.profile_loader = profile_loader
        self.assets = assets
        self.factories = factories
        self.tools_registry = tools_registry

    async def execute(self, **kwargs) -> str:
        agent_name = kwargs.get("agent_name")
        profile, error = _profile_or_error(self.profile_loader, agent_name)
        if profile is None:
            return "错误：" + error
        manifest = {
            "name": kwargs.get("name"),
            "factory": kwargs.get("factory"),
            "config": kwargs.get("config"),
        }
        if manifest["name"] in SCENE_FORBIDDEN_TOOLS:
            return f"错误：私有工具名称 '{manifest['name']}' 属于场景 Agent 禁用能力。"
        if (
            self.tools_registry is not None
            and isinstance(manifest["name"], str)
            and self.tools_registry.get(manifest["name"]) is not None
        ):
            return f"错误：私有工具名称 '{manifest['name']}' 与主工具注册表冲突。"
        try:
            normalized = self.assets.validate_manifest(manifest)
            self.factories.validate_manifest(normalized)
            self.assets.create_tool(profile.name, normalized)
            updated = self.profile_loader.update_profile(
                profile.name,
                {"private_tools": [*profile.private_tools, normalized["name"]]},
            )
        except (SceneSkillAssetError, ToolFactoryError, AgentProfileError, OSError) as exc:
            return f"创建私有工具失败：{exc}"
        return (
            f"已为场景 Agent '{updated.name}' 创建并启用私有工具 "
            f"'{normalized['name']}'（factory={normalized['factory']}）。"
        )


class UpdateAgentPrivateTool(CreateAgentPrivateTool):
    """Validate and atomically replace an existing private-tool manifest."""

    name = "update_agent_tool"
    description = "更新场景 Agent 已存在的受控私有工具配置。"

    async def execute(self, **kwargs) -> str:
        agent_name = kwargs.get("agent_name")
        profile, error = _profile_or_error(self.profile_loader, agent_name)
        if profile is None:
            return "错误：" + error
        manifest = {
            "name": kwargs.get("name"),
            "factory": kwargs.get("factory"),
            "config": kwargs.get("config"),
        }
        if manifest["name"] not in profile.private_tools:
            return f"错误：私有工具 '{manifest['name']}' 未配置给场景 Agent '{profile.name}'。"
        if (
            self.tools_registry is not None
            and isinstance(manifest["name"], str)
            and self.tools_registry.get(manifest["name"]) is not None
        ):
            return f"错误：私有工具名称 '{manifest['name']}' 与主工具注册表冲突。"
        try:
            normalized = self.assets.validate_manifest(manifest)
            self.factories.validate_manifest(normalized)
            self.assets.update_tool(profile.name, normalized)
        except (SceneSkillAssetError, ToolFactoryError, OSError) as exc:
            return f"更新私有工具失败：{exc}"
        return f"已更新场景 Agent '{profile.name}' 的私有工具 '{normalized['name']}'。"


class ListAgentAssetsTool(Tool):
    """Show capability names without exposing private configuration or content."""

    name = "list_agent_assets"
    description = "列出一个场景 Agent 的共享/私有能力名称及当前已注册私有工具工厂。"
    parameters = {
        "type": "object",
        "properties": {"agent_name": {"type": "string"}},
        "required": ["agent_name"],
    }

    def __init__(
        self, profile_loader: AgentProfileLoader, factories: ToolFactoryRegistry
    ) -> None:
        self.profile_loader = profile_loader
        self.factories = factories

    async def execute(self, **kwargs) -> str:
        profile, error = _profile_or_error(
            self.profile_loader, kwargs.get("agent_name")
        )
        if profile is None:
            return "错误：" + error
        value = lambda items: ", ".join(items) if items else "（无）"
        return "\n".join(
            [
                f"场景 Agent：{profile.name}",
                f"共享工具：{value(profile.tools)}",
                f"共享 Skill：{value(profile.skills)}",
                f"私有工具：{value(profile.private_tools)}",
                f"私有 Skill：{value(profile.private_skills)}",
                f"可用私有工具工厂：{value(self.factories.list_factories())}",
            ]
        )
