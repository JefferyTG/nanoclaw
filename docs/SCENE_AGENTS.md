# 临时子 Agent、场景 Agent 与私有能力

NanoClaw 的子 Agent 始终是一次性的：执行一个任务、返回结果后销毁。场景 Agent
只是可复用配置，不是常驻进程，也没有独立记忆或会话历史。

## 两种派遣方式

通用任务无需 Profile：

```text
spawn_subagent(task="分析这个复杂问题")
```

通用子 Agent 保持兼容行为：继承父级大部分工具和全部共享 Skill，并在递归深度
允许时继续拆分任务。

固定职责使用场景 Agent：

```text
spawn_subagent(agent_name="xiaohongshu", task="写一篇小红书内容")
```

场景模式使用 Profile 的 System Prompt、模型和最小能力清单。模型优先级为调用
参数、Profile、全局子 Agent 模型、主模型。

`spawn_subagent` 不套用普通工具的 180 秒兜底；临时子 Agent 由自身的
`turn_timeout_sec`、`max_iterations` 和调用方取消控制生命周期。

在 Web 渠道中，子 Agent 的思考、内部工具、状态和耗时通过独立事件面板实时展示，
不会混入父 Agent 正文或触发自动朗读。运行终态、工具步骤摘要和图片 ID 会随父会话
保存；重新打开历史会话仍可查看，升级前的会话则从已有 `spawn_subagent` 工具记录
尽量恢复。完整思考、工具参数和完整内部工具结果不重复写入摘要，以限制体积和敏感信息暴露。

## Profile v2 与私有目录

新 Agent 使用以下结构：

```text
<config.workspace>/workspace/agents/<agent-name>/
├── profile.json
├── skills/
│   └── <skill-name>/
│       ├── SKILL.md
│       └── resources/...
└── tools/
    └── <tool-name>.json
```

Profile 中：

- `tools` / `skills` 是主应用拥有的共享能力白名单。
- `private_tools` / `private_skills` 是目标 Agent 私有目录中的能力白名单。
- 私有能力即使物理文件存在，没有进入 Profile 也不会装配。
- 只识别上述目录式 v2 Profile；旧 `<agents>/<name>.json` 单文件不会再加载。

## 私有 Skill

主 Agent 使用 `create_agent_skill` / `update_agent_skill` 创建和维护私有 Skill。
场景 Agent 通过独立的 `list_skills`、`load_skill` 和只读
`read_skill_resource` 使用它们，不能修改 Skill 文件。

私有 Skill 必须满足：

- Agent、Skill、目录和 frontmatter 名称合法且一致。
- `SKILL.md` 是受大小限制的 UTF-8 文本。
- 资源路径必须留在所属 Skill 目录，符号链接不能越界。
- 不能与已分配的共享 Skill 重名。

用户也可以直接维护目录和 Profile；下一次派遣重新读取，正在执行的临时 Agent
不会热变更。

## 受控私有工具

私有工具配置只是数据，不是可执行扩展：

```json
{
  "name": "publish_content",
  "factory": "content_publisher",
  "config": {
    "api_key_env": "CONTENT_API_KEY",
    "timeout_sec": 20
  }
}
```

实现者必须在应用代码中向 `ToolFactoryRegistry` 注册经过审核的 factory 和必填的
严格配置校验器（按字段白名单拒绝未知配置）；运行时不从 workspace 动态导入 Python，不支持任意 Shell 或通用 HTTP
manifest。每次派遣都会由 factory 创建新的 Tool 实例；私有工具名称不能覆盖主
工具注册表中的任何名称。

主 Agent 使用 `create_agent_tool` / `update_agent_tool` 管理 manifest；
`list_agent_assets` 只显示名称和 factory 清单，不显示私有内容或配置。密钥不能写入
manifest，敏感配置必须使用 `*_env` 引用环境变量。仓库默认不注册业务私有工具
factory；新增业务工具时应在 composition root 显式注册并补测试。

## 能力模型与控制面边界

场景 Agent 是高能力、受信任执行者。Profile 可以显式授予主注册表中已有的
`exec`、`spawn_subagent`、`memory_search`、视觉/生图、文件、Web 和全局 MCP
工具。能力仍按 Profile 白名单装配，不会因为是场景 Agent 就自动获得全部工具。

以下控制面规则同时在创建期和派遣期执行，手工伪造 Profile 也不能绕过：

- 场景 Agent 不能直接调用创建、修改、删除 Agent/Profile/私有 Skill/私有工具的
  管理 API；这些操作仍由主 Agent 或用户完成。
- `spawn_subagent` 绑定当前场景已经过滤后的 ToolRegistry，继续派生不会自动获得
  父场景没有的工具。
- `read_file`、`write_file`、`list_dir` 使用 realpath 校验，不能访问整个
  `workspace/agents` 管理根目录，不能通过符号链接逃出 workspace。
- 场景 Agent 不能修改共享 Skill；私有 Skill 只通过专用只读工具访问。
- 管理工具只注册在主 Agent 的 ToolRegistry，不下发给场景 Agent。

这些是应用层能力装配，不是操作系统沙箱。显式授予原始 `exec` 后，场景 Agent
实际上可以通过 Shell 读取或修改宿主用户有权访问的文件，也可能绕过上述文件工具
保护；全局记忆、MCP、视觉和生图也会共享主进程的数据范围与外部副作用。只应把
这些能力授予可信场景。若要运行不可信场景，必须另加容器或 OS 级沙箱。

## 创建流程

1. 主 Agent 按 `agent-builder` Skill 调用 `create_agent` 创建最小共享能力 Profile。
2. 如需私有指导，调用 `create_agent_skill`。
3. 如需私有执行能力，先确认已有审核 factory，再调用 `create_agent_tool`。
4. 使用 `list_agent_assets` 核对最终能力。
5. 通过 `spawn_subagent(agent_name=..., task=...)` 派遣并验证结果。

第一版仍不包含常驻 Agent、独立长期记忆、Agent 间通信、后台队列、网页管理页、
场景 Agent 直接调用控制面 API 自行创建能力或从 workspace 动态加载 Python 插件。
