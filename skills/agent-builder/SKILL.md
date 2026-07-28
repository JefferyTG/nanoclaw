---
name: agent-builder
description: 通过自然语言设计并创建最小权限的可复用场景 Agent Profile。
---

# 场景 Agent 创建指南

当用户希望创建一个长期复用、职责明确的场景 Agent 时使用本技能。

## 工作流程

1. 理解场景目标，生成只含字母、数字、下划线或短横线的简短名称。
2. 整理一句职责简介和简洁、可执行的 System Prompt。
3. 从当前已注册工具中选择完成任务所必需的最小白名单。
4. 调用 `list_skills` 核对共享 Skill，只选择真实存在且与场景相关的最小集合。
5. 调用 `create_agent` 保存 Profile。
6. 用户需要场景私有指导时，调用 `create_agent_skill` 创建私有 Skill；不得让场景
   Agent 自己创建或修改。
7. 用户需要场景私有执行能力时，先用 `list_agent_assets` 确认已有审核 factory，
   再调用 `create_agent_tool`。没有可用 factory 时报告需要开发并注册，不能虚构。
8. 再次调用 `list_agent_assets` 核对最终能力并告知用户。

## 约束

- 必须调用 `create_agent`，不得用 `write_file` 直接写 `workspace/agents/*.json`。
- 不得虚构工具或 Skill；不确定时先查询现有清单。
- 不默认授予全部工具，只给完成该场景所需的最小集合。
- 场景 Agent 可以按用户需求获得 `exec`、`spawn_subagent`、全局记忆、视觉/生图
  和全局 MCP；这些属于高权限或外部副作用能力，必须明确列入 Profile，不默认全给。
- 创建、修改、删除 Agent/Profile/私有 Skill/私有工具的管理 API 不授予场景
  Agent；相关动作由主 Agent 或用户完成。
- 原始 `exec` 不是沙箱，会绕过应用层文件保护。用户要求授予时必须在结果中说明
  该场景 Agent 被视为可信执行者。
- 私有工具配置不得保存 API Key、Token、密码等明文，只能使用 `*_env` 环境变量引用。
- 用户未指定模型时让 `model` 留空，以沿用默认子 Agent 模型。
- 尽量根据用户目标生成合理默认值；只有会显著改变权限或职责时才追问。

通用复杂任务无需创建 Profile，直接调用 `spawn_subagent(task=...)` 即可。
