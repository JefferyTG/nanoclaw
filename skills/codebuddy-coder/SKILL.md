---
name: codebuddy-coder
description: "通过 exec 调用腾讯 CodeBuddy Code CLI（命令 codebuddy / cbc）执行编程任务：代码生成、重构、修复、测试、代码审查、批量文件操作。当用户要求用 codebuddy / CodeBuddy CLI 写代码，或需要把编码任务委托给独立的 CLI 编程 Agent 执行时使用。也用于处理 codebuddy 的安装、登录、配置、会话管理与权限设置。"
---

# CodeBuddy Code CLI 调用指南

本技能说明如何通过 `exec` 工具调用腾讯 CodeBuddy Code CLI（npm 包 `@tencent-ai/codebuddy-code`，命令 `codebuddy`，别名 `cbc`）完成编程任务。CodeBuddy 是自主编排的编程 Agent：给定自然语言任务，它能读写文件、执行命令、运行测试、修 bug。

## 使用前必读

- **每次使用前先 `load_skill` 本技能**，不凭记忆调用命令
- **命令以实际 `codebuddy --help` 输出为准**（版本迭代快，参数可能变化）；本技能整理了常用稳定用法
- 环境状态（是否安装、登录与否、版本号）属于工作记忆，不在本技能内；调用前若不确定，先跑 `codebuddy --version` 与一次最小调用验证

## 核心用法：无头模式（非交互）


> **默认模型**：通过环境变量 `CODEBUDDY_MODEL` 或 `--model <id>` 指定；调用前可用 `echo $CODEBUDDY_MODEL` 确认当前默认。本环境默认 hy3（限时免费模型，见工作记忆），Agent 调用建议显式携带：`CODEBUDDY_MODEL=hy3 codebuddy -p "..."` 或 `codebuddy -p "..." --model hy3`。
```bash
# 一次性任务（text 输出，适合简单查询）
codebuddy -p "审查 src/web.py 的并发安全"

# 结构化输出（便于解析）
codebuddy -p "列出所有 API 端点" --output-format=json

# 流式输出（长任务实时进度）
codebuddy -p "重构 xxx" --output-format=stream-json

# 结构化输出 schema 校验
codebuddy -p "..." --output-format=json --json-schema='{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}'
```

关键参数：
- `-p / --print`：打印响应并退出（非交互，Agent 调用默认姿势）
- `--output-format`：`text`（默认）/ `json`（单结果）/ `stream-json`（实时流）
- `--input-format`：`text`（默认）/ `stream-json`（流式输入）
- `--include-partial-messages`：stream-json 时输出模型原始增量消息
- `--max-turns N`：限制 agentic 轮数，防止无限折腾（长任务建议设上限）
- `--effort minimal|low|medium|high|xhigh|max`：推理力度
- 无参数运行则进入交互式会话（用于登录等人工操作）

## 权限模式（安全核心）

`--permission-mode <mode>`（choices: acceptEdits / bypassPermissions / default / plan / dontAsk / auto）

| 模式 | 行为 | 建议 |
|---|---|---|
| `default` | 每个操作询问确认 | 交互式适用，无头场景会卡住 |
| `acceptEdits` | 自动接受文件编辑，命令仍确认 | **无头改文件的默认选择** |
| `bypassPermissions` | 全部跳过（等价 `-y`） | 仅沙箱/临时目录，常规绝不使用 |
| `plan` | 只规划不执行 | 先要方案、审查计划时用 |
| `dontAsk` | 不询问直接执行 | 慎用 |
| `auto` | 自动分类器判断 | 新特性，可试 |

附加安全参数（无头模式建议组合使用）：
- `--tools "Bash,Edit,Read"`：限制内置工具白名单（`""` 全禁、`default` 全开、逗号分隔）
- `--allowedTools "Bash(git:*) Edit"` / `--disallowedTools "Bash(rm:*)"`：细粒度允许/禁止
- `--add-dir <dir>`：额外授权访问的目录
- `-y / --dangerously-skip-permissions`：跳过全部权限（仅限无网沙箱）

## 会话管理

```bash
codebuddy -p "..." -c                    # 继续最近会话（上下文延续）
codebuddy -p "..." -r [sessionId]        # 恢复指定会话
codebuddy --session-id <uuid> -p "..."   # 指定会话 ID
codebuddy --no-session-persistence -p "..."  # 不落盘（临时任务）
codebuddy -p "..." --fork-session        # 从历史会话 fork 新会话
```

跨任务延续时用 `-c` 接上，不必重头解释项目背景。

## 会话生命周期与堆积控制

- **默认每次 `-p` 调用都是新会话**（新 session 落盘到 `~/.codebuddy/projects/<项目>/<uuid>.jsonl`）；`-c` 继续最近、`-r` 恢复指定、`--session-id` 指定 ID 才不会新开
- **一次性小任务（查询/审查/单点修改）加 `--no-session-persistence`**：不落盘、零堆积（2026-08-13 约定）
- **正式任务/要续跑/要复盘的才允许落盘**，且只在 `~/.codebuddy/` 下（不混入项目）
- 自动兜底：`cleanupPeriodDays` 默认 30（按最后活动日期保留本地聊天记录，`codebuddy config set -g cleanupPeriodDays N` 可调）；堆积明显时手动清理 `~/.codebuddy/projects/` 下旧 jsonl

## 登录

- 登录必须人工操作：运行 `codebuddy` 进入交互会话 → 输入 `/login` → 扫码
- 国内版：微信扫码；国际版：Google / GitHub 登录
- 具体可用的模型列表随账号与版本动态变化，**以 `codebuddy --help` 中 --model 参数列出的为准**（当前国内版含 hy3 / glm-5.2 / kimi-k2.7 / deepseek-v4 等，不写死）
- 未登录时所有调用返回 `Authentication required`；登录过期同样需要重新登录
- 登录属于敏感人工步骤：Agent 不代客登录，提示用户完成

## 完整参数速查（v2.135.0 --help 实测）

### 常用 Options
- `-V, --version` 版本；`-d, --debug [filter]` 调试；`--verbose` 详细
- `-p, --print` 非交互打印；`--output-format` / `--input-format` / `--json-schema` / `--include-partial-messages`
- `-y, --dangerously-skip-permissions` 跳过全部权限
- `--permission-mode` / `--permission-mode-before-plan` / `--subagent-permission-mode`
- `--tools` / `--allowedTools` / `--disallowedTools` 工具白/黑名单
- `--mcp-config <fileOrString>` 加载 MCP；`--strict-mcp-config` 只用指定 MCP
- `-c, --continue` / `-r, --resume [sessionId]` 会话延续
- `-w, --worktree [name]` git worktree 隔离干活；`--worktree-branch`；`--tmux`
- `--model <model>` 指定模型（列表以 --help 动态输出为准）；`--text-to-image-model` / `--image-to-image-model` / `--fallback-model`
- `--add-dir <dirs>` 额外授权目录
- `--ide` 自动连 IDE
- `--session-id <uuid>` / `--no-session-persistence` / `--fork-session`
- `-H, --header "K: V"` 自定义请求头
- `--serve` 起 HTTP 服务；`--open` 自动开浏览器；`--port` / `--host`（默认 127.0.0.1）/ `--auth password|none`
- `--acp` ACP 模式（stdin/stdout ndJsonStream）；`--acp-transport stdio|streamable-http`
- `--sandbox [url]` 沙箱（`container`=Docker/Podman 或 E2B URL）；`--sandbox-upload-dir` / `--sandbox-new` / `--sandbox-id` / `--sandbox-kill`
- `--max-turns` / `--effort`
- `--system-prompt <p>` / `--system-prompt-file <path>` / `--append-system-prompt <p>`
- `--prompt-vars-file` 模板变量
- `--agent <agent>` / `--agents <json>` 自定义 Agent
- `--settings <file-or-json>` / `--setting-sources user,project,local`
- `--remote-control [client]` 远程控制
- `--plugin-dir <dirs>` 本地插件目录
- `--bg / --background` 后台运行；`--name`；`--exec <command>` 后台跑 shell 命令
- `--swarm` 群组模式

### 子命令 Commands
| 命令 | 用途 |
|---|---|
| `config` | 配置管理（如 `config set -g theme dark`） |
| `mcp` | 管理 MCP 服务器 |
| `sandbox` | 管理沙箱 |
| `plugin` | 管理插件 |
| `doctor` | 健康诊断 |
| `update` | 检查并安装更新 |
| `install [target]` | 安装原生构建 |
| `daemon` | 守护进程管理 |
| `ps` | 列出活动会话（交互/后台/daemon） |
| `logs <pidOrName>` | 查看后台会话日志 |
| `attach <pidOrName>` | 附加到后台会话 |
| `kill / stop <pidOrName>` | 终止后台会话 |
| `rm <idOrName>` | 移除后台会话视图（transcript 保留） |
| `respawn [idOrName]` | 重启后台会话且对话保留 |
| `agents` | 列出/管理 agents（需 `--json`） |
| `auto-mode` | 检查 auto mode 分类器配置 |

## 调用工作流

1. **拆解任务**：把用户需求拆成 codebuddy 能一次完成的清晰 prompt；明确范围（文件/函数）、目标、约束、验收方式
2. **选择姿势**：
   - 简单查询/单文件：`codebuddy -p "..." --output-format=json`
   - 需要改文件：加 `--permission-mode=acceptEdits`
   - 只给方案不动手：`--permission-mode=plan`
   - 大工程/长任务：`--max-turns 20` 设上限；必要时 `--bg` 后台 + `ps`/`logs` 轮询
   - **需求模糊/大任务 → plan 先行（标准流程，2026-08-13 乖宝确认）**：第 1 步 `--permission-mode=plan` 让 codebuddy 出实现方案（不写代码）→ 审方案、与用户对齐 → 第 2 步再按方案实现。理解偏差在写代码前拦截，不等它闷头改完才发现方向错
3. **执行并验收**：解析输出（json / git diff / 跑测试），有问题让 codebuddy 继续修或自行修复
4. **汇报**：做了什么、改了哪些文件、测试结果，简洁汇报给用户

## 无头模式的三个盲区（派活必看，TASK-040 复盘教训 2026-08-13）

codebuddy 无头模式 = 一次性指令执行，**没有双向沟通**。派活前必须想清楚这三点：

1. **项目全貌**：它只会按 prompt 给的文件去读，不会主动读全项目。大任务/跨模块任务，prompt 必须显式加「项目全貌指引」：先读 `PROJECT.md`、`AGENTS.md`、`docs/ARCHITECTURE.md`（或任务相关文档）再动手；小任务可只给局部文件，但要知道它视野就这么大。
2. **max-turns 不足**：turn 用尽/超时 = 任务直接结束，不会有「汇报卡在哪」。处理流程：先 `git diff` 评估已完成部分 → 用 `-c` 继续同一会话断点续跑（上下文延续）→ 或自行接手。长任务宁可先设小上限试跑，再续。
3. **干活期间缺资源（key/账号/确认）**：无头模式没有「人」可问，它只能自己决定——可能编造、跳过或失败。派活前先预判资源需求，并在 prompt 里显式声明：「若缺少 API key/凭据/外部资源：**不得编造**，标记 TODO 继续其它部分，结束后在总结里报告缺失项」。验收时 grep diff 里的 TODO/placeholder/假 key。

## 常见任务模板

```bash
# 代码审查
codebuddy -p "审查 $(git diff --name-only HEAD~1) 的改动，找出 bug 和安全隐患，按严重程度列出" --output-format=json

# 写单测
codebuddy -p "为 src/xxx.py 的 XXX 函数生成 unittest 测试用例，覆盖边界情况" --permission-mode=acceptEdits

# 修 bug（附报错）
codebuddy -p "修复这个报错：<贴报错>。根因排查，改动最小化" --permission-mode=acceptEdits

# 重构
codebuddy -p "把 src/xxx.py 的 XXX 重构为 YYY 风格，保持行为不变" --permission-mode=acceptEdits

# 只出方案
codebuddy -p "分析 xxx 的架构，给出优化方案，先不要改代码" --permission-mode=plan
```

## 环境坑点（本环境实测）

- **exec 安全机制会拦截 `format` 后跟空格**：`--output-format json` 会被拦，**必须用等号形式 `--output-format=json`**；`--json-schema` 等参数同理建议等号
- 交互式会话会阻塞等待输入，Agent 调用一律用 `-p` 无头模式
- **默认在主工作区直接改**（用户 2026-08-13 约定）：除非任务明确要求隔离，否则不用 `--bg`/`--worktree`；**注意 `--bg` 会自动创建 git worktree**（项目内 `.codebuddy/worktrees/bg-*`，分支 `worktree-bg-*`），改动落在 worktree 而非主工作区，需手动 `git diff | git apply` 合回；要用 worktree 必须先向用户说明

## 排障

| 现象 | 处理 |
|---|---|
| `Authentication required` | 未登录/过期 → 请用户运行 `codebuddy` 交互式 `/login` 扫码 |
| exec 拦截 `format` | 改用 `--output-format=json` 等号形式 |
| 输出格式异常 | 用 `--output-format=json` 拿结构化结果 |
| 命令不存在 | `codebuddy --version` 确认安装；检查 npm 全局 bin 是否在 PATH |
| 后台任务状态 | `codebuddy ps` / `logs <pid>` / `attach <pid>` |
| 行为异常 | `codebuddy doctor`；日志在 `~/.codebuddy/logs/` |

## 安全底线与隔离约定

- **绝不裸用 `-y` / `bypassPermissions`**：会无确认删文件、越权操作；仅限用户明确要求且限定沙箱/临时目录
- **授权范围**：项目目录内的代码与文档读写已获用户授权（可直接改）；git 提交、推送、删除等**仍需先征得用户同意**
- **读取与项目修改**（用户 2026-08-13 明确）：codebuddy 可读取项目内任何文件；项目相关的代码/文档可正常生成与修改，无需设限
- **数据目录隔离**（用户 2026-08-13 明确）：
  - codebuddy 自己的会话/记忆/配置/skill 等运行时数据放在它自己的目录 `~/.codebuddy/`，**不要复制或写入项目内**（尤其不要往 `workspace/memory/`、`skills/` 里塞 codebuddy 的东西，避免两边记忆混淆）
  - 项目内 codebuddy 生成物（`.codebuddy/`、`CODEBUDDY.md`）已在 `.gitignore` 中，**不提交 git**
- 敏感信息（API key、凭据、个人信息）不进 prompt
- codebuddy 完成的关键改动需复核（diff / 测试）后再交付
- **默认模型**：hy3（限时免费）；超量/不可用时**优先切换 deepseek-v4-flash**（`--model deepseek-v4-flash` 或 `--fallback-model deepseek-v4-flash`）
