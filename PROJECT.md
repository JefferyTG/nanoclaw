# NanoClaw 项目总览

> 本文件是项目**总览入口**，供任何新会话快速理解项目全貌与当前状态。
> 细节与流程请按「文档地图」跳转对应文档；内容以当前代码为最高事实源。

## 项目定位

**NanoClaw：一个本地优先、单进程、多渠道的个人 AI Agent 网关。**
把「大模型推理」与「消息渠道」通过进程内 asyncio 消息总线解耦；渠道只负责收发，Agent 在本地 ReAct 循环中思考、调工具、接 MCP。
默认部署形态：一台设备一个/多个独立进程实例，每人/每场景一实例，无全局单例。

## 当前能力矩阵

> 状态说明：✅ 已实现（代码+文档双重确认）｜🔶 待确认（需真实环境验证）｜⚠️ 遗留风险（见 DECISIONS.md）

| 能力 | 状态 | 说明 |
|---|---|---|
| 多渠道 | ✅ | CLI / 飞书 WS / 微信 iLink(Node Bridge) / 网页 WS |
| 渠道感知 | ✅ | Agent 经 System Prompt 会话级快照感知渠道（feishu/weixin/web/cli）与用户标识（sender_id），会话内恒定，可做渠道专属行为 |
| 微信深度集成 | ✅ | 扫码登录、图文收发、断线恢复、原生 typing、`/bind-reminders`、图片等待窗口合并 |
| 微信语音转写 | ✅ | 语音经腾讯 STT 转写（`voice_item.text`）进入 Agent，不落地本地 ASR |
| 微信文件接收 | ✅ | 文件按月归档 `workspace/files/YYYY-MM/`（消毒名+重名加后缀+50MB 上限）；发模型只带「文件名+路径+大小」引用、不读内容不花 token；乖宝说「帮我看看」时 Agent 用 `read_file` 按需读取 |
| 网页端 | ✅ | 流式思考/逐字输出、会话侧边栏、历史接回/删除、断线重连、「⏹ 停止」回合取消 |
| 语音输入 ASR | ✅ | OpenAI-compatible，FFmpeg 归一化，仅 Web（`asr_model` 配置启用） |
| 语音朗读 TTS | ✅ | edge-tts 分句流水线，可取消，默认关（`tts_model` 配置启用） |
| ReAct 工具循环 | ✅ | `max_iterations`、`turn_timeout_sec` 墙钟、重复工具防爆、180s 工具兜底 / Shell 60s |
| 内置工具 | ✅ | 26 个（含 AskImage 条件注册）+ MCP 扩展（`{server}__{tool}`） |
| 场景 Agent | ✅ | Profile 驱动：独立 System Prompt、白名单、私有 Skill/受控工具 |
| 记忆体系 | ✅ | USER/MEMORY/daily、SQLite LIKE 检索、上下文预算动态配置（`context_budget_tokens`，默认 512k）超预算压缩（`ContextCompactor`，每会话独立实例、压缩不写 daily，TASK-006）、跨会话记忆同步（全局/会话 revision + `<memory_patch>` 补丁注入与持久化；压缩后无条件重建完整快照，TASK-007；摘要输入降噪——工具结果只留结论，TASK-008；分块结构化摘要——map-reduce 按字段组织，TASK-009） |
| 每日做梦整理 | ✅ | `dream_time`（默认 02:00）定时把当天各会话事件按固定分类（用户变化/项目进展/会话总结）合并更新写入 daily，写入前行哈希去重；定时时刻未启动则下次启动补做前一天（`dream_state.json` 记 `last_dream_date`）；压缩摘要不再写 HISTORY.md（TASK-011） |
| 上下文占用显示 | ✅ | Web 进度条 + 缓存命中率（usage 流事件）；web/feishu/weixin/cli 四渠道 `/context` 命令直接回复占用（不经模型） |
| 主动提醒 | ✅ | SQLite + RRULE、显式绑定单目标、静态/动态 agent 任务、lease/回执、重启恢复 |
| 生图 / 视觉 | ✅ | `generate_image`（文生图/图生图/多源，全配置化）、`ask_image`（双路径） |
| 视频生成 | ✅ | 异步任务式，多服务商适配 |
| 技能系统 | ✅ | SKILL.md 扫描、摘要注入、ListSkills/LoadSkill |
| Prompt Cache 友好 | ✅ | System 无墙钟、按需时间工具、工具 Schema 冻结、隐私安全观测 |
| 会话持久化 | ✅ | 一会话一 JSONL、重启接回、图片只存引用；中断回合落盘即同步内存、任意中断路径不丢上下文（TASK-010） |
| Linux 后台管理 | ✅ | `bin/nanoclawctl`（setsid 独立进程组） |
| 微信真实收发 | 🔶 | 需真实端点 + 扫码授权（NC-WEIXIN-001） |
| 生图/视频/ASR 真实链路 | 🔶 | 本地仅验证到请求构造（NC-TEST-002） |
| Web 免认证泄露 | ⚠️ | NC-BUG-001（P0，优先绑定 127.0.0.1） |
| workspace 非真实沙箱 | ⚠️ | NC-SEC-001（symlink/绝对路径/网络可越界） |

## 主要模块

| 模块 | 职责 | 关键文件 |
|---|---|---|
| `main.py` | 唯一 composition root：装配共享对象、渠道、生命周期、信号处理；DreamScheduler/DreamState（每日做梦整理）装配与启动补做 | main.py |
| `gateway.py` | 入站调度、同会话锁/跨会话并发、出站/流事件路由、停止控制 | gateway.py |
| `bus/queue.py` | 三队列消息总线 + DTO（Inbound/Outbound/Stream/ImageRef） | bus/queue.py |
| `config.py` | 配置：默认值 < config.json < 环境变量，白名单读写 | config.py |
| `channels/` | 渠道适配器（仅收发，不含业务） | base/cli/feishu/web/weixin.py |
| `agent/loop.py` | AgentLoop：ReAct 循环、流式事件、持久化、取消补历史 | agent/loop.py |
| `agent/tools/` | Tool 抽象、Registry、内置工具、MCP 包装 | registry.py、mcp.py、各工具 |
| `agent/daily.py` | 每日记忆：/clear 摘要 append + 每日做梦整理（固定分类/去重/合并更新） | agent/daily.py |
| `providers/` | LLMProvider 抽象 + OpenAI 兼容实现 | base.py、openai_compat.py |
| `session/manager.py` | 一会话一 JSONL，恢复与自愈 | session/manager.py |
| `reminders/` | 提醒 DTO、SQLite 仓储、RRULE、调度器、应用服务 | models/repository/schedule/scheduler/service.py |
| `voice/` | 音频归一化、ASR/TTS 抽象与 Provider | asr/、tts/、media.py |
| `integrations/weixin_bridge/` | 固定上游 Node Bridge（iLink/CDN AES/凭据独占） | bridge.mjs、NOTICE.md |
| `webui/index.html` | 单文件前端（无构建步骤） | webui/index.html |
| `skills/` | 运行时技能目录（SKILL.md） | skills/*/SKILL.md |
| `bin/nanoclawctl` | Linux 后台 start/stop/restart/status/logs | bin/nanoclawctl |

> 完整目录树与模块边界见 `docs/ARCHITECTURE.md` §4。

## 命令速查

| 用途 | 命令 |
|---|---|
| 启动 | `uv run python main.py`（无终端时 `web_port>0` 可纯 Web 跑） |
| 测试（Python） | `.venv/bin/python -m unittest discover -s tests`（**unittest，非 pytest**） |
| 微信 Bridge 回归 | `cd integrations/weixin_bridge && npm test && npm run build` |
| 语法检查 | `uv run python -m compileall -q agent bus channels providers session` |
| 导入冒烟 | `uv run python -c "import main"` |
| 协作最低检查 | `git diff --check` |
| Linux 控制 | `./bin/nanoclawctl {start\|stop\|restart\|status\|logs}` |

> ⚠️ 当前**无**格式化/静态检查基线（pyproject.toml 无 `[tool.*]` 配置、无 CI）——见 NC-TEST-001。

## 配置速查

- 优先级：代码默认值 < `config.json` < 对应环境变量。
- Web 配置页热更新**只对新会话生效**；MCP/workspace/技能/工具注册等启动期对象需重启。
- `base_model_multimodal`、`timezone`、`asr_model`、`tts_model`、`reminders`、`weixin`、`context_budget_tokens`、`dream_time` 均属启动期配置，修改后需重启。
- `context_budget_tokens`：ContextCompactor 压缩阈值（默认 524288=512k），旧配置缺字段回退默认值不报错；`/context` 命令与 Web 进度条展示当前会话占用。
- `dream_time`：每日做梦整理时刻（"HH:MM"，默认 "02:00"），旧配置缺字段回退默认值不报错。
- 敏感字段（weixin 状态、API Key 等）不进 `config.json` 或受白名单过滤。

## 消息流转（简述）

```
Channel(start) → Bus.inbound → Gateway(会话锁) → AgentLoop.run
  → Provider.chat_stream → ToolRegistry.execute（内置工具 / MCP / ReminderService）
  → OutboundMessage → Bus.outbound → Gateway._dispatch → Channel.send
Web 附加：AgentLoop 流事件 → Bus.stream → WebChannel → WebSocket（thinking/token/tool_call/done）
提醒链路：ReminderScheduler → claim/lease(SQLite) → AgentRunner 或直接取 delivery_text → Bus.outbound → 绑定渠道
做梦链路：DreamScheduler（到 dream_time）/ 启动 catch_up → collect_messages_for_date + dream_consolidate → DailyMemory.write_dream（固定分类+去重）
```

> 完整时序图与依赖方向见 `docs/ARCHITECTURE.md` §3、§5。

## 文档地图

| 文档 | 用途 | 何时读 |
|---|---|---|
| **PROJECT.md（本文件）** | 总览入口：定位、能力、模块、命令 | 新会话第一份 |
| AGENTS.md | 协作规则（所有会话必读） | 开始任何工作前 |
| docs/ARCHITECTURE.md | 架构、模块边界、数据流、扩展点 | 改跨模块链路前 |
| docs/DECISIONS.md | 历史决策、已知限制、遗留问题（NC-*） | 做设计选择/排查 Bug 前 |
| docs/DEVELOPMENT.md | 任务拆解、会话分工、验证矩阵、完成标准 | 每个开发任务开始和交接时 |
| docs/tasks/ | 任务卡（active/ 进行中、completed/ 已归档） | 恢复/交接任务时 |
| docs/decisions/ | 单条决策记录（ADR 风格，可选） | 记录重大架构决策时 |
| docs/TTS.md / ASR.md / VOICE_WAKE_KWS.md / SCENE_AGENTS.md | 专项能力说明 | 修改对应模块前 |

事实冲突时优先级：**当前代码与配置契约 > 当前 Git 状态 > docs/ 已确认文档 > 旧方案草稿**。

## Git 状态（指针式）

> **唯一事实源是 git 本身**（`git log` / `git status` / `git diff`）。本段只留指针与稳定约定，**不复制任何瞬时状态**——hash 列表、领先/落后数量、未跟踪清单都会过期，一律不写，需要时直接查 git。

- 当前里程碑：TASK-001~011 已完成并归档（任务卡见 `docs/tasks/completed/`）；active：TASK-012 会话索引实时性（待开工，任务卡见 `docs/tasks/active/`）。
- 最新提交、分支、领先/落后、工作区状态：`git log` / `git status`。
- 稳定约定：`kb-testset/`（个人知识库测试资产）已在 `.gitignore` 中不追踪；存在 codex 外部 worktree → 多会话并行开发时严格遵守 AGENTS.md 文件所有权规则。
