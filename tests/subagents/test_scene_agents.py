import os
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from agent.context import ContextBuilder
from agent.profiles import AgentProfileLoader
from agent.scene_assets import SceneSkillAssets, SceneToolAssets
from agent.skills import SkillsLoader
from agent.tool_factories import ToolFactoryRegistry
from agent.tools.agent_profiles import (
    CreateAgentPrivateTool,
    CreateAgentSkillTool,
    CreateAgentTool,
    ListAgentAssetsTool,
    ListAgentsTool,
)
from agent.tools.base import Tool
from agent.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
from agent.tools.mcp import MCPTool
from agent.tools.registry import ToolRegistry
from agent.tools.skills_tools import ListSkillsTool, LoadSkillTool
from agent.tools.spawn import SpawnSubagentTool


class NamedTool(Tool):
    description = "test tool"
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, name):
        self._name = name

    @property
    def name(self):
        return self._name

    async def execute(self, **kwargs):
        return self.name


class FakeAgentLoop:
    instances = []

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.__class__.instances.append(self)

    async def run(self, task):
        self.task = task
        return "child-result"


def write_skill(root: str, name: str, body: str) -> None:
    directory = os.path.join(root, name)
    os.makedirs(directory)
    with open(os.path.join(directory, "SKILL.md"), "w", encoding="utf-8") as file:
        file.write(f"---\nname: {name}\ndescription: {name} desc\n---\n\n{body}\n")


class SceneAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.skills_dir = os.path.join(self.tempdir.name, "skills")
        write_skill(self.skills_dir, "allowed-skill", "ALLOWED BODY")
        write_skill(self.skills_dir, "hidden-skill", "SECRET BODY")
        self.skills_loader = SkillsLoader(self.skills_dir)
        self.profile_loader = AgentProfileLoader(
            os.path.join(self.tempdir.name, "workspace", "agents")
        )
        self.scene_skill_assets = SceneSkillAssets(self.tempdir.name)
        self.scene_tool_assets = SceneToolAssets(self.tempdir.name)
        self.tool_factories = ToolFactoryRegistry()

        def validate_named(config):
            if config:
                raise ValueError("config")
            return {}

        self.tool_factories.register(
            "named",
            lambda name, config: NamedTool(name),
            config_validator=validate_named,
        )
        self.registry = ToolRegistry()
        for name in ("read_file", "write_file", "web_search", "web_fetch", "exec"):
            self.registry.register(NamedTool(name))
        self.root_list_skills = ListSkillsTool(self.skills_loader)
        self.root_load_skill = LoadSkillTool(self.skills_loader)
        self.registry.register(self.root_list_skills)
        self.registry.register(self.root_load_skill)
        self.models = []
        self.config = SimpleNamespace(
            subagent_model="sub-model",
            model="main-model",
            max_iterations=9,
            turn_timeout_sec=23,
        )

        def provider_factory(model):
            self.models.append(model)
            return object()

        self.spawn = SpawnSubagentTool(
            provider_factory=provider_factory,
            tools_registry=self.registry,
            skills_loader=self.skills_loader,
            profile_loader=self.profile_loader,
            scene_skill_assets=self.scene_skill_assets,
            scene_tool_assets=self.scene_tool_assets,
            tool_factories=self.tool_factories,
            workspace=self.tempdir.name,
            config=self.config,
        )
        self.registry.register(self.spawn)
        FakeAgentLoop.instances = []

    async def test_filtered_skills_do_not_disclose_hidden_skill(self):
        filtered = self.skills_loader.filtered(["allowed-skill"])

        self.assertEqual([item["name"] for item in filtered.list_skills()], ["allowed-skill"])
        self.assertEqual(filtered.load_skill("allowed-skill"), "ALLOWED BODY")
        self.assertIsNone(filtered.load_skill("hidden-skill"))
        self.assertNotIn("hidden-skill", filtered.build_skills_summary())

    async def test_registry_public_lookup_methods_preserve_order(self):
        self.assertEqual(self.registry.get("exec").name, "exec")
        self.assertIsNone(self.registry.get("missing"))
        self.assertEqual(
            [tool.name for tool in self.registry.get_many(["web_fetch", "missing", "exec"])],
            ["web_fetch", "exec"],
        )
        self.assertEqual(
            [tool.name for tool in self.registry.iter_tools()], self.registry.list_tools()
        )

    async def test_create_and_list_agent_validate_real_capabilities(self):
        create = CreateAgentTool(self.profile_loader, self.registry, self.skills_loader)
        self.registry.register(create)
        listing = ListAgentsTool(self.profile_loader)

        result = await create.execute(
            name="xiaohongshu",
            description="负责小红书内容创作",
            system_prompt="你是小红书内容策划。",
            tools=["web_search", "web_fetch", "read_file", "write_file"],
            skills=["allowed-skill"],
            model="",
        )

        self.assertIn("已创建场景 Agent 'xiaohongshu'", result)
        self.assertIn("xiaohongshu：负责小红书内容创作", await listing.execute())
        unknown = await create.execute(
            name="bad",
            description="bad",
            system_prompt="bad",
            tools=["not-real"],
            skills=[],
        )
        self.assertIn("工具不存在", unknown)
        self.assertIsNone(self.profile_loader.get_profile("bad"))
        unknown_skill = await create.execute(
            name="bad-skill",
            description="bad",
            system_prompt="bad",
            tools=[],
            skills=["not-real"],
        )
        self.assertIn("Skill 不存在", unknown_skill)
        self.assertIsNone(self.profile_loader.get_profile("bad-skill"))

    async def test_create_agent_allows_powerful_tools_but_rejects_control_plane(self):
        create = CreateAgentTool(self.profile_loader, self.registry, self.skills_loader)
        self.registry.register(create)
        powerful = ["exec", "memory_search", "ask_image", "generate_image", "list_agents"]
        for tool_name in powerful[1:]:
            self.registry.register(NamedTool(tool_name))
        result = await create.execute(
            name="ops",
            description="明确执行命令",
            system_prompt="只执行明确任务。",
            tools=powerful,
            skills=[],
        )
        self.assertIn("已创建场景 Agent 'ops'", result)
        self.assertEqual(self.profile_loader.get_profile("ops").tools, powerful)

        rejected = await create.execute(
            name="blocked-control",
            description="blocked",
            system_prompt="BLOCKED",
            tools=["create_agent"],
            skills=[],
        )
        self.assertIn("禁止直接使用", rejected)

    async def test_create_and_spawn_allow_global_mcp_tool(self):
        mcp_tool = MCPTool("remote", "danger", "danger", {}, object())
        self.registry.register(mcp_tool)
        create = CreateAgentTool(self.profile_loader, self.registry, self.skills_loader)
        created = await create.execute(
            name="mcp",
            description="mcp",
            system_prompt="MCP",
            tools=[mcp_tool.name],
            skills=[],
        )
        self.assertIn("已创建", created)
        with patch("agent.tools.spawn.AgentLoop", FakeAgentLoop):
            result = await self.spawn.execute(agent_name="mcp", task="use mcp")
        self.assertEqual(result, "child-result")
        self.assertIn(mcp_tool.name, FakeAgentLoop.instances[-1].tools.list_tools())

    async def test_generic_spawn_inherits_tools_skills_and_recursion(self):
        with patch("agent.tools.spawn.AgentLoop", FakeAgentLoop):
            result = await self.spawn.execute(task="分析复杂问题")

        self.assertEqual(result, "child-result")
        child = FakeAgentLoop.instances[-1]
        self.assertIn("exec", child.tools.list_tools())
        self.assertIn("spawn_subagent", child.tools.list_tools())
        self.assertIn("allowed-skill", child.context.build_system_prompt())
        self.assertEqual(self.models[-1], "sub-model")
        self.assertEqual(child.max_iterations, 9)
        self.assertEqual(child.turn_timeout, 23)

    async def test_scene_spawn_isolates_tools_skills_prompt_and_model(self):
        self.profile_loader.create_profile(
            {
                "name": "xiaohongshu",
                "description": "内容创作",
                "system_prompt": "SCENE PROMPT",
                "model": "profile-model",
                "tools": ["read_file"],
                "skills": ["allowed-skill"],
            }
        )
        with patch("agent.tools.spawn.AgentLoop", FakeAgentLoop):
            result = await self.spawn.execute(
                agent_name="xiaohongshu", task="写内容"
            )

        self.assertEqual(result, "child-result")
        child = FakeAgentLoop.instances[-1]
        self.assertEqual(
            child.tools.list_tools(),
            ["read_file", "list_skills", "load_skill", "read_skill_resource"],
        )
        self.assertNotIn("exec", child.tools.list_tools())
        self.assertNotIn("spawn_subagent", child.tools.list_tools())
        self.assertIsNot(child.tools.get("list_skills"), self.root_list_skills)
        prompt = child.context.build_system_prompt()
        self.assertIn("SCENE PROMPT", prompt)
        self.assertIn("allowed-skill", prompt)
        self.assertNotIn("hidden-skill", prompt)
        self.assertEqual(self.models[-1], "profile-model")

        load_hidden = await child.tools.execute("load_skill", {"name": "hidden-skill"})
        self.assertIn("未找到技能", load_hidden)
        self.assertNotIn("SECRET BODY", load_hidden)
        direct_hidden = await child.tools.execute(
            "read_file", {"file_path": "skills/hidden-skill/SKILL.md"}
        )
        self.assertIn("无权访问未授权", direct_hidden)
        direct_allowed = await child.tools.execute(
            "read_file", {"file_path": "skills/allowed-skill/SKILL.md"}
        )
        self.assertEqual(direct_allowed, "read_file")

    async def test_private_skill_and_tool_are_only_mounted_for_owner(self):
        self.profile_loader.create_profile(
            {
                "name": "writer",
                "description": "writer",
                "system_prompt": "PRIVATE",
                "model": "",
                "tools": [],
                "skills": [],
            }
        )
        skill_admin = CreateAgentSkillTool(
            self.profile_loader, self.scene_skill_assets, self.skills_loader
        )
        tool_admin = CreateAgentPrivateTool(
            self.profile_loader,
            self.scene_tool_assets,
            self.tool_factories,
            tools_registry=self.registry,
        )
        conflict = await tool_admin.execute(
            agent_name="writer", name="read_file", factory="named", config={}
        )
        self.assertIn("主工具注册表冲突", conflict)
        self.assertIn(
            "创建并启用",
            await skill_admin.execute(
                agent_name="writer",
                name="brand-style",
                description="brand",
                instructions="PRIVATE SKILL BODY",
            ),
        )
        self.assertIn(
            "创建并启用",
            await tool_admin.execute(
                agent_name="writer",
                name="private_lookup",
                factory="named",
                config={},
            ),
        )
        self.scene_skill_assets.create_skill(
            "writer",
            "unassigned",
            "---\nname: unassigned\ndescription: hidden\n---\nHIDDEN PRIVATE SKILL",
        )
        self.scene_tool_assets.create_tool(
            "writer", {"name": "unassigned_tool", "factory": "named", "config": {}}
        )
        with patch("agent.tools.spawn.AgentLoop", FakeAgentLoop):
            result = await self.spawn.execute(agent_name="writer", task="work")
        self.assertEqual(result, "child-result")
        child = FakeAgentLoop.instances[-1]
        self.assertIn("private_lookup", child.tools.list_tools())
        prompt = child.context.build_system_prompt()
        self.assertIn("brand-style", prompt)
        self.assertNotIn("unassigned", prompt)
        self.assertNotIn("unassigned_tool", child.tools.list_tools())
        self.assertIn("PRIVATE SKILL BODY", await child.tools.execute("load_skill", {"name": "brand-style"}))
        first_private_tool = child.tools.get("private_lookup")
        with patch("agent.tools.spawn.AgentLoop", FakeAgentLoop):
            await self.spawn.execute(agent_name="writer", task="again")
        self.assertIsNot(
            first_private_tool,
            FakeAgentLoop.instances[-1].tools.get("private_lookup"),
        )
        assets = ListAgentAssetsTool(self.profile_loader, self.tool_factories)
        listing = await assets.execute(agent_name="writer")
        self.assertIn("private_lookup", listing)
        self.assertIn("brand-style", listing)

    async def test_scene_delegation_is_bound_to_filtered_parent_registry(self):
        self.profile_loader.create_profile(
            {
                "name": "delegator",
                "description": "delegator",
                "system_prompt": "DELEGATE",
                "model": "",
                "tools": ["exec", "spawn_subagent"],
                "skills": [],
            }
        )
        with patch("agent.tools.spawn.AgentLoop", FakeAgentLoop):
            result = await self.spawn.execute(agent_name="delegator", task="delegate")
        self.assertEqual(result, "child-result")
        child = FakeAgentLoop.instances[-1]
        self.assertIn("exec", child.tools.list_tools())
        self.assertIn("spawn_subagent", child.tools.list_tools())
        bound_spawn = child.tools.get("spawn_subagent")
        self.assertIsNot(bound_spawn, self.spawn)
        self.assertIs(bound_spawn.tools_registry, child.tools)
        with patch("agent.tools.spawn.AgentLoop", FakeAgentLoop):
            nested = await bound_spawn.execute(task="nested")
        self.assertEqual(nested, "child-result")
        nested_child = FakeAgentLoop.instances[-1]
        self.assertIn("exec", nested_child.tools.list_tools())
        self.assertNotIn("create_agent", nested_child.tools.list_tools())

    async def test_forged_profile_cannot_grant_control_plane_tool(self):
        create = CreateAgentTool(self.profile_loader, self.registry, self.skills_loader)
        self.registry.register(create)
        self.profile_loader.create_profile(
            {
                "name": "forged",
                "description": "forged",
                "system_prompt": "FORGED",
                "model": "",
                "tools": ["create_agent"],
                "skills": [],
            }
        )
        result = await self.spawn.execute(agent_name="forged", task="escape")
        self.assertIn("禁止直接使用", result)

    async def test_real_filesystem_cannot_read_or_write_agent_assets_or_escape_symlink(self):
        registry = ToolRegistry()
        registry.register(ReadFileTool(self.tempdir.name))
        registry.register(WriteFileTool(self.tempdir.name))
        registry.register(ListDirTool(self.tempdir.name))
        spawn = SpawnSubagentTool(
            provider_factory=lambda model: object(),
            tools_registry=registry,
            workspace=self.tempdir.name,
            skills_loader=self.skills_loader,
            profile_loader=self.profile_loader,
            scene_skill_assets=self.scene_skill_assets,
            scene_tool_assets=self.scene_tool_assets,
            tool_factories=self.tool_factories,
            config=self.config,
        )
        self.profile_loader.create_profile(
            {
                "name": "files",
                "description": "files",
                "system_prompt": "FILES",
                "model": "",
                "tools": ["read_file", "write_file", "list_dir"],
                "skills": [],
            }
        )
        outside = os.path.join(
            os.path.dirname(self.tempdir.name),
            os.path.basename(self.tempdir.name) + "-outside-scene-secret.txt",
        )
        with open(outside, "w", encoding="utf-8") as file:
            file.write("SECRET")
        os.symlink(outside, os.path.join(self.tempdir.name, "escape-link"))
        self.addCleanup(lambda: os.path.exists(outside) and os.unlink(outside))
        with patch("agent.tools.spawn.AgentLoop", FakeAgentLoop):
            await spawn.execute(agent_name="files", task="work")
        child = FakeAgentLoop.instances[-1]
        blocked_profile = await child.tools.execute(
            "read_file", {"file_path": "workspace/agents/files/profile.json"}
        )
        blocked_write = await child.tools.execute(
            "write_file",
            {"file_path": "workspace/agents/files/new.json", "content": "x"},
        )
        blocked_link = await child.tools.execute(
            "read_file", {"file_path": "escape-link"}
        )
        self.assertIn("无权访问", blocked_profile)
        self.assertIn("无权访问", blocked_write)
        self.assertIn("越过工作区", blocked_link)

    async def test_explicit_model_wins_and_missing_agent_lists_available(self):
        self.profile_loader.create_profile(
            {
                "name": "coding",
                "description": "编程分析",
                "system_prompt": "CODING",
                "model": "profile-model",
                "tools": [],
                "skills": [],
            }
        )
        with patch("agent.tools.spawn.AgentLoop", FakeAgentLoop):
            await self.spawn.execute(agent_name="coding", task="task", model="override")
        self.assertEqual(self.models[-1], "override")

        missing = await self.spawn.execute(agent_name="not_exists", task="测试")
        self.assertIn("不存在", missing)
        self.assertIn("coding：编程分析", missing)

    async def test_main_context_contains_only_agent_name_and_description(self):
        summary = "- coding：编程分析"
        context = ContextBuilder(self.tempdir.name, agents_summary=summary)
        prompt = context.build_system_prompt()
        self.assertIn(summary, prompt)
        self.assertIn("spawn_subagent(task)", prompt)
        self.assertNotIn("CODING SECRET PROMPT", prompt)

        current = {"summary": summary}
        dynamic = ContextBuilder(
            self.tempdir.name,
            agents_summary_provider=lambda: current["summary"],
        )
        self.assertIn("coding：编程分析", dynamic.build_system_prompt())
        current["summary"] = "- writing：内容创作"
        self.assertIn("writing：内容创作", dynamic.build_system_prompt())


if __name__ == "__main__":
    unittest.main()
