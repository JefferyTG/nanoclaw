# DeepSeek Harness（dsh）深度调研报告

> 调研时间：2026-08-13 开源当日（v0.1 开发者预览版，MIT 协议）
> 信息来源：GitHub 仓库（README、docs/architecture.md、docs/subsystems/*、packages/README.md）、机器之心《刚刚，DeepSeek Harness震撼开源：一切皆插件》、量子位《深度体验DeepSeek Harness，我原谅它涨价了》、智东西《实测DeepSeek Harness！梁文锋憋的"黑色鲸鱼"大招，有惊喜》、DeepTech《DeepSeek正式开源Harness：它终于有了自己的Vibe Coding入口》及 Hacker News / 社区反馈。
> 仓库状态（抓取时）：49.8k Star / 4k Fork / 12,293 commits / 230+ workspace 成员。

---

## 一、架构总览

### 1.1 定位

DeepSeek Harness（`dsh`）不是新模型，也不是 API 客户端，而是"把模型变成智能体的工具"（官方公式：**Agent = Model + Harness**）。它负责调度上下文、工具、任务状态、反馈与边界，把模型接入文件系统、Shell、终端、网页、LSP、其他 Agent，完成从理解需求到交付结果的闭环。产品定位直接对标 OpenAI Codex 与 Anthropic Claude Code，但它的野心更底层——官方原话是"一切皆插件（Everything is a plugin）"，本质是一套**组装 Agent 的 SDK 与运行时框架**，默认应用（Web/TUI/Headless）只是这套 SDK 的"第一位客户"。

### 1.2 仓库结构（monorepo，Node.js / TypeScript，pnpm）

- **packages/**：全部能力包，npm scope `@deepseek-ai/dsh-*`，按 30+ 个 group 组织（见 1.4）。
- **apps/**：成品应用壳——`apps/cli`（`dsh` 命令行）与 `apps/web`（Web UI 宿主）。
- **examples/**：演示 bundle（agent-spine demo、CLI/ACP/JSON-RPC 可执行入口、Code Mode、自指 Cordis、MCP 记忆服务等）。
- **python/**：Python SDK（驱动 JSON-RPC 运行时，不内嵌 Node 内核）。
- **native/**：本地原生代码（沙箱/进程约束等底层实现）。
- **vendor/**：vendored 的 Cordis 微内核源码（`vendor/README.md` 说明同步流程）。
- **docs/**：架构文档（architecture.md、subsystems/*、cordis-primer、cookbook 等，体量惊人、极其工程化）。
- **patches/、scripts/、website/、assets/**：依赖补丁、生成脚本（模块图/事件目录/config 目录）、官网与素材。
- **.agents/notes/**：全部设计决策以"Agent Note"形式留存（如 parallel-tool-call、capability-seams、agent-scope-runtime-design 等），是理解设计意图的第一手资料。

### 1.3 Cordis 微内核：为什么选它

Cordis 是 DeepSeek 与北京大学联合论文《A Programming Paradigm for Spatiotemporal Composability》（作者 Yifan Shi、Wei Zhang、Tianyi Cui）对应的插件框架，论文提出"时空可组合性"（Spatiotemporal Composability）：

- **时间可组合性**：组件卸载后能撤销自身对系统造成的所有状态修改（副作用可逆）。
- **空间可组合性**：自动管理组件间依赖——当某组件依赖的服务出现、消失或被替换时，系统自动协调相关组件。
- 传统插件系统要求开发者手写卸载/清理逻辑；Cordis 会**追踪组件通过 Context 产生的副作用（effect），并在卸载时自动执行恢复操作**，同时按依赖关系协调加载与退出。

为什么对 Agent 重要：未来 Agent 需要支持"自我演化"——AI 自己生成、部署、替换组件。若每次改插件都重启整个 Agent，会丢状态、中断任务；若改错，要能恢复到此前状态。Cordis 让组件在运行中"拆掉、换掉、再装回来"而不影响系统。论文把"自我演化 Agent Harness"列为未来重点验证方向，当前版本尚未声称实现自主改自己。

Cordis 核心五概念：插件实现 Service；Context 是服务仓库（`ctx.tools`/`ctx.llm`/`ctx.sessions`…）；`inject` 声明服务依赖（用服务需求表达加载顺序）；类型化事件（emit/waterfall/parallel/serial 四种派发模式）；**注册即可逆副作用**（`ctx.effect()`/`ctx.on()` 安装，卸载时按序解绑）。

### 1.4 "一切皆插件"的实现方式

**运行中的 Harness 本质上是一个 Cordis Context**：模型适配器、工具注册表、Session Log、Agent Loop、沙箱、审批策略、存储、UI 全部是插件，在配置层自由组合；没有"需要打补丁的特权核心"。不同包向 Context 注册服务、事件与能力。

**core 包（packages/core/，即系统的"脊柱"）**，每个都是可替换插件：

| 包 | 职责 | ctx 键 |
|---|---|---|
| `core/session` | 仅追加 `SessionEvent` 日志 + 内存存储（唯一权威来源） | `ctx.sessions` |
| `core/system-prompt` | 提示词分节（prompt section）与工具 schema 组装 | `ctx.systemPrompt` |
| `core/tools` | 作用域化工具注册表 + 受守卫的执行流水线 | `ctx.tools` |
| `core/agent` | `Agent` 接口、活跃注册表、发起者作用域、`agent/*` 事件 | `ctx.agents` |
| `core/agent-loop` | 实现 `Agent` 契约的默认驱动（**循环本身可换**） | `ctx.agentLoop` |
| `core/scope` | 每个 Agent 的作用域化注册原语（无 ctx 键的纯库） | — |

关键设计：扩展插件只依赖 `dsh-agent`（接口），**从不直接依赖 `dsh-agent-loop`（实现）**，所以循环保持可替换。UI、hooks、编排器都通过 `ctx.agents` 编程，不触碰具体循环。

**能力包划分（packages/README.md 的 group 总表）**：

- 执行类：`shell/`（一次性命令：executor 接缝 + 本地实现 + 面向模型的工具）、`subprocess/`（进程树）、`terminal/`（持久 PTY，owner 作用域会话）、`code-runtime/`（模型写程序执行：Service Def + worker-thread 后端 + Code Mode 消费者）、`sandbox/`（进程约束接缝：bwrap/Landlock/Seatbelt 后端）、`fs/`（文件读写/编辑/搜索 + 策略限制）、`lsp/`（语言服务器，语义级代码导航）、`web/`（搜索与网页抓取）、`e2b/`（POC）。
- Agent 类：`skill/`（可复用技能：注册表 + 本地 provider + 目录/加载工具）、`subagent/`（子 Agent：provider 注册表 + 委派工具）、`jobs/`（通用后台任务 + `job_*` 控制工具）、`workflow/`（脚本驱动编排：接缝 + worker-thread 引擎 + `workflow`/`ralph` 工具）。
- 会话/状态类：`session/`（持久化接缝 + JSONL/SQLite 后端、投影接缝、日志标题）、`session-query/`（会话检索：逻辑语料、行系、SQLite 全文检索）、`goal/`（同会话目标）、`schedule/`（会话内定时 follow-up）、`feedback/`（人工反馈）、`todo/`（`todo_write` 工具）、`plan/`（计划协作状态）、`context/`（模型可见请求上下文：工作区指令、时间上下文）、`compaction/`（上下文压缩）、`spill/`（工具结果 spill 策略）、`attachment/`（附件身份与内容寻址存储）。
- 接入/交互类：`llm/`（抽象服务 + provider 适配器）、`interaction/`（审批/交互接缝、权限预设、命令、ask-user 工具）、`hooks/`（Claude Code / Codex 线协议桥）、`credentials/`（凭据引用接缝）、`settings/`、`workspace/`、`acp/`（Agent Client Protocol 服务端）、`sdk/`（JSON-RPC 运行时 SDK）、`api/`（远程 BFF + Typert RPC 网关）、`host/`（Web-GUI 宿主半侧）、`client/`（Web-GUI 浏览器半侧，含 `ui-*` 插件）、`preset/`（按 preset 组装会话）、`bundle/`（可安装 profile 补丁层）、`extensions/`（**自指 Cordis 工具集**：运行时自我修改）、`guard/`（循环卫生守卫：重复调用提醒 + 执行期限强制）。

**Seam（接缝）模式**：典型能力拆成三层——Service Definition（声明接口）/ Service Provider（实现）/ Consumer（消费方，通常是面向模型的工具）。以 Bash 为例：接口定义"执行命令是什么"，本地实现创建进程，模型工具包把它变成模型可理解的 schema/结果。将来换远程容器/云沙箱只需替换实现层。文件系统与子进程共享同一执行世界，指向远程沙箱即可连带移动 Bash/PTY/LSP。**谁拥有接口、谁负责实现、谁把能力呈现给模型，严格不混。**

### 1.5 组装：profile / bundle / cordis.yml

- **Profile**：命名组合，存于 Harness home；列出叠加的 bundle、外部插件、用户的 `cordis.patch.yml`。`web`、`headless` 是自带模板。
- **Bundle**：Cordis 配置行 + 代码的分发格式，`dsh-base` 是第一层（模型适配器、工具、持久化、沙箱、审批、设置、凭据、遥测）；`dsh-web-app` 叠加浏览器应用；`dsh-headless` 是无服务器的 one-shot runner。
- 分层顺序：profile 列出的 bundle 依次 → profile 的 `cordis.patch.yml` → home 级 → `--patch` 覆盖。`dsh --profile web --dump-config` 可打印实际启动的插件树，任何一行都能被自己的 patch 替换。
- 界面本身也是"第二棵插件树"：页面声明侧边栏/对话区/输入区/设置区等挂载位，UI 插件把组件注册进去（`ConversationNodeDefinition` + keyed renderer）。
- 同一套代码可组装成：终端编程 Agent（LLM+fs+Bash+TUI）、浏览器应用（换 Web 插件）、CI 脚本（Headless 一次跑完退出）、自动化服务（ACP / JSON-RPC 前门）。

---

## 二、创新点清单（每条 = 解决什么问题 + 实现方式）

### 1. Agent Loop 生命周期：Turn/Step 拆分

**解决什么问题**：早期 Agent 核心就是"发消息→执行工具→发回去"三行代码，长任务中无法回答"模型到底在哪一步看了什么、干了什么、为什么停下"。

**实现方式**：
- **Step = 一次模型请求 + 其调用的工具执行**；**Turn = 零或多个 Step**，在首个输入被认领前打开，直到无欠账（nothing owed）才关闭。
- 完整流程（架构文档给出的状态机）：`turn/start` → 认领 next-step 输入 + 一条排队消息 → 组装 prompt section + 工具 schema → `agent/pre-step`（可改写或拒绝输入；被拒绝/空的首个 claim 仍会关闭一个"未产生 step 的持久 turn"，日志如实记录尝试）→ `step/start` → 追加 user/message → 从日志派生模型历史 → `agent/request` → `llm/stream` → `assistant/chunk*` → `assistant/message` → `tool/call*` → 工具流水线 → `tool/result*` → `step/end` → 工具欠请求或有 next-step 输入则继续，否则 `agent/turn-stopping` → `turn/end`。
- 事件分三层：`turn/*`、`step/*`、`user/message`、`assistant/*`、`tool/*` 是**持久 session 事件**；`agent/pre-step`、`agent/request`、`llm/stream`、三个 `tools/*` 是 **waterfall 实时扩展点**（监听者必须 `next()` 委派）。
- 实测体量：机器之心让 dsh（V4-Flash）从零做第一人称丧尸射击游戏，"3 turn、127 step、30+ 分钟、5 个并行子 Agent"。

### 2. 工具调用流水线：前置策略 / 安全守卫 / 后置处理 / 内容整理 / 结果通知

**解决什么问题**：工具调用不能"拿到函数名就执行"；允许/拒绝、超时、重试、指标、附加上下文都应从固定位置介入，且安全决策必须单调不可被绕过。

**实现方式**（`ctx.tools.execute()`）：
`tools/pre-execute`（可重排的 allow/deny/ask 瀑布）→ **单调安全守卫（monotonic guards）** → `tools/execute`（around-dispatch 包装）→ `tools/post-execute`（检查/替换结果）→ 工具自有的 `finalizeContent`（同步最后一步变换，对每条归一化结果恰好调用一次，含绕过 post-execute 的失败路径）→ `tools/result`（不可变权威结果事件）。
- 只有 `tools/execute` 视图能替换取消 signal。
- **被守卫拒绝的操作不能被后续插件重新放行**（单调性）；需扩大权限的命令必须说明原因并通过审批重试。
- `ToolDefinition` 还声明 `output.schema`（规范 JSON 输出契约）、`timeoutMs`（协作式超时预算，永不发给模型）、`isConcurrencySafe(args)`（纯同步分类器，仅 `true` 才参与并行，且不得突变父级状态）、`presentCall/presentResult`（纯函数 UI 呈现，可回放）。

### 3. 并发安全调度：只读并行，改状态做屏障

**解决什么问题**：Agent 同时搜索十个文件、跑测试、接收用户追加指令、随时可取消时，无约束并发会互相踩踏或污染状态。

**实现方式**：调度器依据 `isConcurrencySafe()` 声明分组——连续的只读任务**并行**执行；遇到修改状态或无法确认安全的调用，当作**屏障（barrier）**，等待前面的任务结束后独占执行。安全元数据永远不发给模型（`schemas()` 白名单只放 name/description/parameters）。

### 4. 运行中用户消息：排队消息 vs 注入上下文 vs Steering

**解决什么问题**：用户在工作流中途发来的内容，可能是下一轮任务、对当前工作的转向指令、或仅供参考的上下文。系统不只关心"消息收到了"，还关心"模型究竟在哪一步看到了它"。

**实现方式**（`Agent` 句柄的 inbox 路由）：
- **inbox 分两个有序队列**：`next-turn`（下一轮普通消息）与 `next-step`（下一 step 边界消费的转向消息），每个待处理项是带身份（MessageId）的 `UserMessage`。
- `followup()`：排队普通 follow-up 并唤醒驱动；成为自己 turn 的唯一普通消息。
- `steer()`：为最近 step 提交转向指令；驱动运行中在下一 step 边界消费；被拒的 step 会把转向留在 inbox 等下次唤醒；取消/销毁会丢弃待处理转向。
- `inject()`：排队面向模型的上下文，**不唤醒驱动**；空闲驱动挂起直到有 follow-up/steer 唤醒；可能错过 pre-step 已认领的批次。
- `agent/pre-step` 是"模型看到什么"的最后裁决点；消息来源（MessageSource）只记录身份、不授予权限。通过回执（MessageId 进入日志的插入/认领/丢弃事实）确认某条转向是否真正进入某次模型请求。

### 5. Session Log 作为唯一权威来源（模型可见 = 已记录）

**解决什么问题**：任务出错时，系统往往只知道"最终聊天文本"，不知道模型请求前注入了工作区状态、工具结果被裁剪过、模型路由被切换、用户中途改了方向。界面、持久化、恢复、Fork、遥测、回放各自维护一份"差不多正确"的状态会互相漂移。

**实现方式**：
- `Session` 是**仅追加（append-only）的 `SessionEvent` 类型化日志**，是会话交互历史的唯一事实源；LLM 消息历史**从日志派生（`deriveMessages()`）**，绝不单独存储；回放就是对同一事件流重新派生。
- **运行时不变量：凡是模型请求能看到的，必须能从日志重建**（model-visible means logged）；新增模型可见输入必须扩展 `SessionEventMap` 并实现从日志渲染。
- 事件覆盖：`turn/start|end`、`step/start|end`、`user/message`（直接提示/注入上下文/目标续轮，靠 `source` 区分）、`assistant/chunk`（**保留原始流式 chunk，token 级回放保真**）、`assistant/message`（携带 usage，输出与计费同行）、`tool/call`（参数是模型产出的**未解析原始 JSON 字符串**）、`tool/result`（含可选的错误身份与工具私有 meta）、`todo/write`（全列表快照）、`request/header`（**每次请求的信封：provider/model/推理强度/system prompt/工具 schema 全部落日志**，使每次请求成为日志的纯函数）、`request/context`（路由容量）、`session/end-seed`（种子边界）。
- 事件全部是 lossless JSON、seq 连续；`Session.append` 运行时校验 `isJsonValue`，非序列化数据在源头被拒。
- **持久化后端是插件**：`ctx.sessionPersistence` 接缝，JSONL（每会话一个 artifact，`locate()` 返回绝对路径）与 SQLite（共享单库，支持全文检索）两个后端可互换；查询优先访问实时会话。
- **Resume / Fork**：`ctx.agents.resume({resumeSessionId})` 沿用原会话续跑；`ctx.sessions.fork(source, boundary?, childSessionId?)` 从确定历史边界派生新会话；seed 边界由 `SessionHeader.seedLength` 持久化，区分父历史与子工作。
- **崩溃恢复**：后端加载到崩溃中断的 turn（有 `turn/start` 无 `turn/end`）**不截断**，而是补一条合成 `turn/end {reason:'interrupted'}` 保持收支平衡；格式版本不符的日志明确拒绝（报"新版本写的，请升级"而非"损坏"）。
- 权限切换、审批请求、取消原因都进日志，审计与复现共用同一事件流。

### 6. 子 Agent 作用域设计 + ACP 外部子进程

**解决什么问题**：多 Agent 体系里，子 Agent 该看到哪些工具/提示词/命令必须隔离，且隔离不能靠"全局命名约定"这种脆弱机制；生命周期结束后的残留也必须自动清理。

**实现方式**：
- `core/scope` 提供 `ScopeKey`（不透明对象身份，随 Agent 生命周期）：**每个 Agent 拥有自己的上下文层（agent.ctx）**，注册在其中的工具/提示词/命令对该 Agent 可见；作用域化注册是 Cordis effect，**随 Agent 销毁自动 unwind**，无需手动清理。`ToolRestriction`（allow/deny 列表）作为作用域对继承工具的动态过滤；作用域自身注册的工具豁免于过滤（委派子 Agent 保留它应答用的工具）。
- 子 Agent 创建三路径：全新实例、从已有会话完成边界 **Fork**、**通过 ACP 连接外部子进程**（Agent Client Protocol 服务，自动化专用）。
- 子 Agent 能力契约：`outputSchema`（结构化输出）、`depthLimit`（绝对委派深度上限）、`toolFilter`（工具过滤，既从 prompt 消失也拒绝执行——"可见性即权威"）、`persona`（子级专属 persona，shadow 全局）；**不支持的能力在 start 前 loud reject**（fail loud, no silent degradation）。
- **Continuable 后台子 Agent**：一个持久化子 Session 至多一个进程内 Activation（重建后常驻的 Agent 实例）；Activation 可执行多个 FIFO turn，并保持在其派生的子 Activation 全部销毁后才 settle/dispose；`followup()` 按 Activation 状态路由（running→同实例排队 / waiting→唤醒 / 无 Activation→cold resume）。
- 子 Agent provider 本身也是可插拔接缝（`ctx.subagents` 注册表，多 provider 并存）：`spawn-in-process`、`fork`、`acp`、`codex`、`claude-code`、`dsh-sdk`。

### 7. 工作流（Workflow）：脚本驱动多智能体编排

**解决什么问题**：一问一答之外的"长任务、并行调查、自动化运行、外部系统协调"需要确定性的编排机制，而不是让模型在上下文里即兴指挥。

**实现方式**：
- **模型写编排脚本**（普通 JS、允许 top-level await、以 `return` 结束），`agent()` 调用派生子 Agent，`parallel()` / `pipeline()` 组合器管理并发与流水线；`meta`（name/description/whenToUse/phases）是纯 JSON 数据，**运行前严格校验，绝不 eval 脚本去取 meta**。
- 引擎是 `node:worker_threads`（每次 run 一个 worker，脚本的 vm context 在其内），换引擎也是插件（每 Context 仅一个 `ctx.workflowEngine`，与 bash 同规则）。
- 故障纪律：脚本 hook 误用抛 `WorkflowError {fatal:true}`，组合器**重抛而非映射成 null**（打错的选项必须大声杀死脚本，不能溶解成普通子失败）；`null` 只留给子运行失败。`WorkflowRun.cancel()` 有界沉降（engine force-settle `cancelled` 后终止 worker），`dispose()` 永不悬挂在卡死的脚本上。
- **四种"协作状态"不是四个同名 UI 小组件，而是不同生命周期的持久化状态**：`plan/`（计划模式：记录当前协作阶段，直接入口命令 + 评审后退出）、`goal/`（目标：跨同一会话持续存在）、`todo/`（待办：模型轻量任务清单，全列表快照 last-write-wins，刻意无 id/优先级）、`jobs/`（后台任务：管理仍在运行的实际工作）。机器之心点评："框架先把方向盘、仪表盘和刹车做了出来。"

### 8. 四种 Agent 预设

**解决什么问题**：同一套 Harness 宿主要服务"日常开发、复杂调用链、基准测试、自改装研究"四种截然不同的场景；预设不应是四套分叉的 Agent，而应是对同一宿主的不同插件装配。

**实现方式**（不是改 prompt 风格，而是装入不同工具/提示词/运行时能力）：
- **标准模式**：功能最全的通用编码 Agent（文件编辑、Shell、文件/网页检索、Skills、计划、目标、子 Agent、工作流）。
- **PTC 模式（Programmatic Tool Calling）**：标准能力之上，通过 **Code Mode SDK** 向模型呈现工具——模型写一段 TypeScript 程序，在**一次 `run_code` 中组合多步操作**，中间数据留在运行环境，只有最终结果进模型上下文，大幅降低长调用链的往返与 token 开销。
- **极简模式**：仅保留持久 Bash + `str_replace_editor` 两个工具，专为模型基准测试与最小化复现（V4-Flash 发布前就是用 Harness 极简模式跑 Code Agent benchmark 的）。
- **创造模式**：标准能力之上加 Cordis 运行时检查、临时插件实验与 preset 创作指导——**Agent 能检查运行时插件树、动态挂载/卸载临时插件**，甚至可以给自己做一个官方没有的"三栏模式" UI。因能运行模型写的插件代码，是面向高级用户的高信任模式，**默认不开放**。
- 预设组合持久化在 `SessionHeader.agentPreset`——resume 若换了装配，会重放模型已无法执行的历史，所以预设必须随会话持久化。

### 9. cordis.yml 配置系统

**解决什么问题**：声明式组合插件、环境差异覆盖、密钥不进仓库不进日志。

**实现方式**：
- 配置列出插件名、稳定 ID、参数；覆盖层（bundle → profile patch → home patch → `--patch`）让 TUI/Web 共享基础配置再叠加各自界面。
- **`!!js` 读取环境变量与运行时表达式**（如 `DEEPSEEK_API_KEY`）；`@deepseek-ai/cordis-plugin-include` 解析成表达式节点。
- **凭据管理**：配置只引用凭据名，实际调用时解析；Web UI 把密钥写入 `$DSH_HOME/.credentials.yaml`，环境变量 / `.env` 作为自动化与本地开发回退；密钥不得写入 cordis.yml 或会话日志。

### 10. 上下文管理：压缩接缝 + 512k/百万 token 策略

**解决什么问题**：长会话上下文爆炸；且不同模型路由容量不同（V4 Pro 100 万 token），压缩策略必须感知模型容量、可回放、可审计、崩溃可恢复。

**实现方式**：
- **Compaction 是可选能力接缝**（不在循环脊柱上）：`ctx.compaction`（Service Def）+ `compaction-basic`（后端）+ `dsh-command-compact`（人工消费者）。自动触发分两种：`pressure`（常规压力，依据最新持久化路由请求）与 **`context-overflow`（provider 确认的容量溢出，允许低于常规阈值也强制做一次有效均衡缩减）**——路由容量通过 `request/context` 事件记录 `contextWindow`（如 1M token），这就是"512k/百万 token 下的处理策略"入口。
- 摘要压缩把选中的表面区间替换为一个 summary 节点，承载方式是 `user/message` + `surfaceOp:{op:'replace',start,end}`（唯一的表面变异操作）；`compaction/start`/`summary`/`end` 三个日志事件构成**锁括号**（start 最后 append end，崩溃留下"孤儿锁"可检测），保证并发压缩互斥。
- 可选 `ctx.toolResultPruner`：把超大工具结果替换为裁剪版（按 Unicode 码点计量），工具调用/结果配对边界严格校验。
- 回放保真：原始 chunk 与压缩事件都进日志，界面回放与实时运行一致；token 估算由独立 `ctx.tokenMeter` 负责（量子位实测：DSH 底部有 token 仪表盘，缓存命中率大多 99%，偶有 100%）。

### 11. 安全策略（贯穿配置→执行→审批→日志→恢复）

**解决什么问题**：编程 Agent 拿到 fs+Shell 权限后，可改代码、装依赖、起进程、触碰工作区外主机；"界面加个确认弹窗"不是安全架构。

**实现方式**：默认 `workspace-write`（限制在当前工作区+允许的临时目录），配合 `ask` 审批；`danger-full-access` 必须显式选择，不包装成无害兼容项。**失败关闭（fail-closed）**：无法确认隔离生效就拒绝执行，绝不静默退化。文件系统、Bash、子进程共享同一沙箱策略，避免"命令受限但文件工具能绕过去"的割裂边界；所有权限切换/审批/参数/结果/取消原因进 Session Log。

---

## 三、已知缺点 / 坑（来自实测文章与社区）

1. **开发者预览版，官方明示破坏性变更**：v0.1 正在快速迭代，"THERE WILL BE COMPATIBILITY-BREAKING CHANGES"；团队负责人崔添翼自评"很不完善，多提意见"。
2. **安装/构建门槛高**：monorepo 230+ workspace 成员、pnpm 全套、文档海量；量子位吐槽"没有 Electron APP，得靠浏览器 Web UI"。
3. **UI 与交互细节落后 Codex**：量子位点名"经典 Agent 三列表右侧栏还没做出来"、内置浏览器/文件管理/预览/视频播放等缺失；Web UI 极简（智东西："干净但什么都没有"）。
4. **多模态缺口**：V4 系列是"瞎子"（无视觉），图片附件需另配多模态模型，实测场景受限。
5. **配置 patch 是整体替换而非深合并**：补丁替换目标插件的整个 config，只写一个新字段会把原有 API Key / 基础地址一起丢掉——"很明确但不符直觉"（机器之心点名）。
6. **插件生态尚早期**：官方 100+ 插件，Plugin Store 只是预留；量子位明说"对普通用户暂时没太大帮助，毕竟是吃生态的 feature"。Hacker News 尖锐质疑："所有依赖社区插件提供功能的产品，头六个月很顺，之后就是不兼容、过时、缺乏一致性和治理的噩梦"——Cordis 有形式化卸载保证，但 Koishi 4000 插件与全球社区的量级不可同日而语。
7. **定位尴尬：更像框架而非产品**：智东西实测后结论"不是面向 C 端用户的产品"；社区亦有"这更像开发框架而非 Coding Agent"的声音。
8. **实测暴露的行为差异**：极简模式下贪吃蛇没写成 HTML（需 python 跑）；一次任务可能"猛跑 10 小时"（社区反馈最长 10h），token 仪表盘很必要但失控成本风险真实存在；不同 Harness 对同一模型任务完成质量差异巨大（36氪：同权重换 Harness，CORE-Bench Hard 从 42% 到 95%；同任务 Claude Code 平均 70 次工具调用 vs OpenCode 22 次）。
9. **成本侧压力**：开源当天伴随 V4-Pro 发布与 API 涨价（含峰谷定价），"用自家 Harness 省 token"与"API 涨价"之间需要算账。
10. **自修改（创造模式）风险**：Agent 运行模型写的插件代码属于高信任操作，默认关闭；论文也仅把"自我演化"列为未来方向，当前"自主改自己"不可靠。

---

## 四、对"Python 个人陪伴型 Agent"（webui / 多渠道 / 实时通话 / 语音唤醒 / 主+子 Agent）的可借鉴启发

### 4.1 用事件溯源会话日志取代"聊天记录 + 散落状态"

- **把 `model-visible = logged` 当运行时不变量**：模型看到的每个 token（系统提示词、注入的工作区/记忆上下文、工具结果裁剪前后、路由切换）都必须能从日志重建。陪伴型 Agent 最怕"说不清自己为什么这么回应"——事件日志天然支持审计、复现、用户投诉排查。
- 实现建议（Python）：先上 JSONL 追加式事件存储（typed events：`user_message`/`assistant_chunk`/`assistant_message`/`tool_call`/`tool_result`/`context_inject`/`compaction_summary`），模型历史一律 `derive_messages()` 从日志投影，绝不另存一份"会话历史"；成熟后加 SQLite 后端 + FTS。
- **Resume/Fork 免费获得**：记录 `seed_length`/`parent_session` 边界，可支持"从某轮记忆分叉新会话"（陪伴场景：用户想从某天的话题重新展开）。
- 原始流式 chunk 也落盘，保证多渠道（webui/IM/电话）回放与实时一致。

### 4.2 Turn/Step 生命周期 + 三通道消息路由（实时场景刚需）

- 陪伴 Agent 的痛点：语音唤醒/实时通话中，用户在 Agent 正在说话/执行时插话。把输入分成 **followup（下一轮）/ steer（立即转向）/ inject（仅供参考，不打断）** 三通道，并回执"这条转向已进入第 N 次模型请求"——这正是通话场景需要的语义（"停一下"必须是 steer 且确认生效，而不是排队到讲完）。
- 用 `pre-step` 钩子做"模型本次到底看到什么"的最后裁决点，配合白名单实现"敏感话题不进上下文"。

### 4.3 工具流水线与单调守卫 → 陪伴 Agent 的安全边界

- 陪伴型 Agent 的工具少而敏感（发消息、读日历、联网、语音合成、可能操作文件/系统）。把工具调用做成 `pre-policy → guard → execute → post → finalize → result` 六段流水线，**被守卫拒绝的操作不可被后续环节放行**（单调性）。
- `isConcurrencySafe` 式声明 + 屏障调度：并行读（多条天气/新闻/搜索）没问题，一旦触碰状态（发消息、改设置、拨电话）做独占屏障——避免语音通道与 webui 同时触发"发送"造成重复动作。
- 安全默认：陪伴 Agent 默认"只读+受控回复"，写文件/外呼等需审批或显式授权；**失败关闭**（隔离机制失效即拒绝，不静默降级）。

### 4.4 作用域化子 Agent（主+子体系的正解）

- 我们已有主 Agent + 子 Agent：照抄 `scope` 思路——**每个子 Agent 一个独立上下文层（agent.ctx）**，工具/提示词/命令注册其中，随子 Agent 生命周期自动清理（Python 可用 context manager / DI 容器实现可逆注册），杜绝"全局命名约定隔离"。
- 子 Agent 能力契约用 capability 位图（`outputSchema/depthLimit/toolFilter/persona`），不支持就 start 前 loud reject——例如"记忆检索子 Agent"只给只读工具，"外呼子 Agent"只给电话 API。
- **Continuable 子 Agent（Activation 常驻 + FIFO turn）** 很适合陪伴场景的"长期记忆整理子 Agent"：主 Agent 空闲时把当日对话交给它压缩沉淀，随生命周期自动收尾。

### 4.5 四种协作状态 → 长任务与记忆分层

- 用 `plan（当前阶段）/ goal（跨会话目标，如"帮用户减重"）/ todo（轻量清单）/ jobs（后台任务，如定时问候、音乐播放进程管理）` 四种**不同生命周期**的状态，而不是一个万能的"任务表"。
- 陪伴场景直接受益：语音唤醒的定时提醒是 `schedule`（会话内定时 follow-up），长期陪伴目标是 `goal`，正在放的音乐/直播是 `jobs`。

### 4.6 上下文压缩作为"记忆管线"而非事后补救

- 把压缩做成**独立接缝**（Service Def / Provider / Consumer 三层），自动触发分 `pressure` 与 `context-overflow`，并感知模型容量（contextWindow）——陪伴型 Agent 的对话无限增长，摘要压缩 + 工具结果裁剪（pruner）要持续把"旧聊天"折叠成 `compaction_summary` 事件，**折叠不可逆地留在日志里**，需要时可展开原始 chunk。
- 更妙的是把"长期记忆"插件化：像 ifeng 报道里的社区记忆插件那样，用"本地文件持久化 + 分层上下文注入 + LLM 自我整理"，而不是一上来就上向量库 RAG。

### 4.7 配置与凭据纪律

- 照抄 `cordis.yml` 分层：bundle 基础层 → 渠道层（webui / IM / 电话各叠加自己 UI 与工具）→ 个人覆盖层；用 `!!js` 风格读取环境变量。
- 凭据独立于配置：`$HOME/.companion/.credentials.yaml` + env 回退，**密钥永不进日志**（陪伴 Agent 要接语音服务、日历、支付等敏感凭据，这条直接是合规底线）。
- 注意坑：补丁是整段替换而非深合并——设计自己的配置覆盖时明确"覆盖即替换"语义，避免用户丢配置。

### 4.8 可热替换的插件生命周期（24/7 服务不重启）

- 陪伴 Agent 要常驻、要 24/7 在线，最忌讳"改个技能就要重启"。用可逆 effect 注册（register/unregister 成对、可撤销副作用），让技能/渠道/记忆模块支持热挂载卸载——这正是 Cordis"时空可组合性"解决的问题，也是自进化 Agent 的地基。创造模式（高信任自改）可留作高级开关，默认关闭。

### 4.9 编排层：脚本驱动多 Agent（Python 版可用 asyncio 实现）

- Workflow 的思路（模型写脚本 + `agent()`/`parallel()`/`pipeline()` + fatal 重抛纪律 + 有界取消沉降）可以在 Python 侧用 asyncio 复刻成"任务图"DSL；关键是**运行前校验 meta、失败 loud、取消有界不悬挂**——陪伴场景的"今晚的日程编排"（查日历→定提醒→订外卖→播报）就是一个天然 workflow。

### 4.10 可观测性：轨迹视图 + token 仪表盘

- 为用户（和开发者自己）提供"模型到底干了什么"的回放窗口（Trajectory）与 token/缓存成本仪表盘——陪伴型产品做商业化时，这是解释"为什么这个月 API 账单涨了"和"为什么 Agent 这么回复"的必备件；对语音场景尤其重要（用户只听到结果，看不到过程）。

---

### 附：信息来源与关键原文

- 官方 README / 架构文档：`github.com/deepseek-ai/deepseek-harness`（docs/architecture.md、docs/cordis-primer.md、docs/subsystems/{core,tools,session,scope,compaction,subagent,workflow,persistence,llm-streaming}.md、packages/README.md）
- Cordis 论文：`github.com/cordiverse/paper`《A Programming Paradigm for Spatiotemporal Composability》（北大+DeepSeek：Yifan Shi、Wei Zhang、Tianyi Cui）
- 机器之心《刚刚，DeepSeek Harness震撼开源：一切皆插件》（2026-08-13，新浪财经转载）
- 量子位《深度体验DeepSeek Harness，我原谅它涨价了》（2026-08-14，qbitai.com/2026/08/472208.html）
- 智东西《实测DeepSeek Harness！梁文锋憋的"黑色鲸鱼"大招，有惊喜》（2026-08-14，zhidx.com/p/584897.html）
- DeepTech深科技《DeepSeek正式开源Harness：它终于有了自己的Vibe Coding入口》（2026-08-13）
- 36氪《在做Harness这件事上，DeepSeek更信搞量化的》（Harness 差异带来的任务质量/成本差异）
- Hacker News 对"社区插件长期可维护性"的质疑（"6 个月后是噩梦"论）
