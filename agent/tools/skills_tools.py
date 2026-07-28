"""技能相关工具：让模型能主动枚举与读取技能。

本模块把 ``agent.skills.SkillsLoader`` 的两个查询能力封装成 Tool，
注册进 ToolRegistry 后，模型在对话中即可主动调用：

- ``ListSkillsTool``：列出当前所有已发现的技能（名称、描述、路径）。
- ``LoadSkillTool``：读取指定技能 SKILL.md 的完整正文（详细操作指南）。

两个工具都持有构造时注入的 ``SkillsLoader``：主 Agent 使用共享 Loader，
场景 Agent 使用独立的白名单 Loader，因此清单、正文和 System Prompt 摘要一致。
"""

from typing import Optional

from agent.skills import SkillsLoader
from agent.tools.base import Tool


class ListSkillsTool(Tool):
    """列出当前所有可用技能，供模型主动了解技能清单。"""

    name = "list_skills"
    description = (
        "列出当前所有可用技能（名称、描述、SKILL.md 路径）。"
        "当系统提示里的技能摘要不够新、你想确认有哪些技能可调，或想获取技能的完整路径时调用。"
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, loader: SkillsLoader) -> None:
        """持有共享的 SkillsLoader 实例。"""
        self.loader = loader

    async def execute(self, **kwargs) -> str:
        skills = self.loader.list_skills()
        if not skills:
            return "当前没有任何已发现的技能。"

        lines = []
        for s in skills:
            lines.append(f"- {s['name']}：{s['description']}\n  路径：{s['path']}")
        return "可用技能：\n" + "\n".join(lines)


class LoadSkillTool(Tool):
    """读取指定技能的完整正文，供模型获取详细操作指南。"""

    name = "load_skill"
    description = (
        "读取指定技能的完整正文内容（SKILL.md 的详细操作指南）。"
        "当你决定使用某项技能、需要先获取其详细步骤与规范时调用，参数为技能名称 name。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "技能名称，即 skills 目录下对应的子目录名（也是 list_skills 返回中的 name）",
            }
        },
        "required": ["name"],
    }

    def __init__(self, loader: SkillsLoader) -> None:
        """持有共享的 SkillsLoader 实例。"""
        self.loader = loader

    async def execute(self, **kwargs) -> str:
        name = kwargs.get("name", "")
        if not name:
            return "错误：缺少参数 name（技能名称）。请传入要读取的技能名称。"

        content = self.loader.load_skill(name)
        if content is None:
            return f"错误：未找到技能 '{name}'。可用技能请调用 list_skills 查询。"

        return content


class ReadSkillResourceTool(Tool):
    """Read a text resource from an allowed shared/private Skill, read-only."""

    name = "read_skill_resource"
    description = (
        "读取当前 Agent 已授权 Skill 目录中的 UTF-8 文本资源。"
        "不能访问未授权 Skill、绝对路径或越过 Skill 目录。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "已授权的 Skill 名称。"},
            "resource_path": {
                "type": "string",
                "description": "相对于该 Skill 目录的资源路径。",
            },
        },
        "required": ["name", "resource_path"],
    }

    def __init__(self, loader: SkillsLoader) -> None:
        self.loader = loader

    async def execute(self, **kwargs) -> str:
        name = kwargs.get("name", "")
        resource_path = kwargs.get("resource_path", "")
        try:
            content = self.loader.load_skill_resource(name, resource_path)
        except ValueError as exc:
            return f"错误：{exc}"
        if content is None:
            return "错误：资源不存在、不是 UTF-8 文本，或不在已授权 Skill 目录内。"
        return content
