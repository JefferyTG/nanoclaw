# 历史决策与遗留问题

## 1. 阅读规则

本文件把 `.workbuddy/memory/` 的历史记录与当前代码交叉核对后整理为可移植结论。状态含义：

- **有效**：仍是当前实现或长期约定。
- **已替代**：历史上成立，之后被新实现替代。
- **待处理**：当前代码或运行边界仍存在。
- **待验证**：缺少真实环境或长期测试证据，不能宣称完成。

若本文件与代码冲突，以代码和可重复验证为准，并在任务完成后更新本文件。

## 2. 已确认的长期决策

| 决策 | 状态 | 原因与影响 |
|---|---|---|
| Python 3.13 + `uv` + `uv.lock` | 有效 | 保证环境可复现；禁止用全局 pip 改项目依赖 |
| `main.py` 作为直接运行入口和装配根 | 有效 | 使用顶层 `agent.*`/`session.*` 导入；不引入 DI 框架 |
| Channel、Bus、Gateway、Agent 解耦 | 有效 | 新增渠道不应修改 Agent 核心协议以外逻辑 |
| 同会话串行、跨会话并发 | 有效 | 避免历史竞态，同时不让慢会话阻塞全局 |
| 一人/场景一独立进程实例 | 有效 | 配置、workspace、端口和飞书 App 需实例隔离；进程内 Mailbox 平台不是当前目标 |
| 工具优先复用，避免重复抽象 | 有效 | 记忆写入复用 read/write，仅检索增加 `memory_search` |
| 可靠行为规则完整注入 System Prompt | 有效 | 实测依赖模型主动 load Skill 不可靠；固定前缀可利用 Prompt Cache |
| 当前时间置于 System Prompt 最后 | 有效 | 避免动态时间破坏固定前缀缓存 |
| SQLite LIKE 代替 FTS5 | 有效 | 中文短词在 unicode61/trigram 下不可靠；当前个人数据量足够小 |
| Daily Memory 是 best-effort 内部机制 | 有效 | `/clear` 和压缩不能被摘要失败阻塞 |
| Skill 与人设解耦 | 有效 | Skill 写行为机制，具体口吻由 identity 决定 |
| 视觉双路径 | 有效 | 基础模型多模态则直传；否则用 `ask_image` 调独立视觉模型 |
| 生图服务完全配置化 | 有效 | 不在代码绑定特定服务商/模型；一个工具覆盖文生图、图生图、多源图 |
| Web 前端无构建步骤 | 有效 | 单文件 HTML/CSS/JS，配合 no-cache 和页面版本握手 |
| Web ASR 只向核心投递转写文本 | 有效 | 音频在渠道边界临时处理；Bus、Gateway、AgentLoop 与会话协议保持文本语义 |
| ASR 与主模型配置/凭证分离 | 有效 | Chat Completions 兼容不代表支持 Audio Transcriptions；便于替换供应商并隔离权限与账单 |
| Web TTS 是可取消的附加能力 | 有效 | 只消费实时 Agent 新回复，不进入 Bus/会话；失败、关闭或浏览器拒播不得影响文字聊天 |
| TTS 采用分句流水线 | 有效 | 强标点优先切分、弱标点达到长度再切；合成与播放并行，避免等待完整长回复和逐 token 请求风暴 |
| 子 Agent Web 事件必须命名空间化 | 有效 | 子层级的 token/done 不能污染父回复或触发 TTS；父会话只持久化有界回放摘要和图片 ID |
| 缺失人设采用 Gateway 级首次引导 | 有效 | 三种渠道共用同一入口；引导不调用模型、不进入会话历史，用户描述原子写入后每轮自动重读 |
| Linux 控制脚本使用独立进程组 | 有效 | start/stop/restart/status 无需固定安装路径；SIGTERM 先触发应用清理，超时才强制结束整个进程组 |

这些约定来自 `.workbuddy/memory/MEMORY.md` 和 2026-07-22 至 2026-07-26 的开发日志；原日志被 Git 忽略，因此本表是跨会话的正式摘录。

## 3. 演进时间线

| 日期 | 主要变化 | 仍然重要的经验 |
|---|---|---|
| 07-22 | Tool、Provider、Context、AgentLoop、文件/Shell/Web 工具 | 工具异常要归一化；路径和进程执行必须有边界与超时 |
| 07-23 | MessageBus、技能、JSONL 会话、历史压缩 | OpenAI tool-call 消息顺序必须自洽；压缩必须同步写回磁盘 |
| 07-24 | CLI/飞书/Web 多会话、Web 流式、并发锁、历史侧边栏 | 所有流式错误/超时路径都要发 `done`；同会话串行 |
| 07-25 | Web 重连、MCP、Prompt Cache 排序、常驻运行探索 | 后台 WS 不等于消息持久队列；睡眠期间可能丢消息 |
| 07-26 | 记忆、图片/视觉、生图、工具耗时 | 配置/消息/持久化是跨层协议；临时测试不应再删除 |

## 4. 已替代或已核销的旧记录

- “暂不支持多会话”已被 CLI、飞书和 Web 的多会话实现替代。
- MemoryConsolidation “尚未接入 AgentLoop”和 token 估算 TODO 均已完成。
- Web 旧页显示 JSON、断线不重连、`web:web:` 双前缀、流式挂起无超时等问题已有对应修复，不能仅凭旧日志当作当前 Bug。
- 历史日志对统一工具超时互相冲突；当前普通工具由 `ToolRegistry.execute()` 使用 180 秒兜底，Shell 另有 60 秒超时。子 Agent 由自身回合上限管理，生图由单请求超时和整次任务预算管理，避免普通工具上限提前截断长任务和重试。
- 历史日志称最后提交无法 push；当前 Git 实测 `HEAD` 与 `origin/main` 均为 `0daefc4`，领先/落后为 `0/0`，此项已核销。

## 5. 当前遗留问题清单

### P0：应优先修复

#### NC-BUG-001 Web 管理面泄露敏感配置且无认证

- **现状**：WebChannel 明确免登录；`GET /api/config` 按 `_CONFIG_FIELDS` 原样返回配置，其中包括主 API Key、飞书 Secret、视觉/生图 Key；POST 还能修改并写回配置。启用时默认 host 为 `0.0.0.0`。
- **影响**：同网段非可信访问者可读取密钥、会话与图片，并修改配置。README 的“可信局域网”提示不足以防止误暴露。
- **建议验收**：默认只绑定 loopback，或加入认证；秘密字段只返回是否已配置，空值不得覆盖已有秘密；对会话/图片/配置端点统一鉴权。
- **证据**：`channels/web.py` 的 `_handle_get_config/_handle_post_config`，`config.py` 的 `_CONFIG_FIELDS` 和 `web_host`。

#### NC-BUG-002 工具调用持久化顺序回归

- **现状**：`AgentLoop._execute_tools()` 先 `_persist(tool_msg)`，循环结束后才持久化 `assistant(tool_calls)`。磁盘顺序成为 `user → tool → assistant`，不符合 OpenAI 要求的 `assistant(tool_calls) → tool`。
- **复现**：2026-07-26 离线调用 `_execute_tools`，得到 `disk_roles=['tool','assistant']`；`SessionManager.get_history()` 自愈后成为 `['tool','assistant','tool']`，同一 `tool_call_id` 重复两次。
- **影响**：包含工具调用的会话在重启/续接后可能 API 400，且当前自愈会留下前置孤儿 tool 消息。
- **建议验收**：保留生成图片元数据能力的同时原子地按协议顺序落盘；为普通工具、多个工具、生图、执行中断和坏历史各留回归测试。
- **证据**：`agent/loop.py::_execute_tools`、`session/manager.py::get_history`。

#### NC-SEC-001 workspace 边界不是真实沙箱

- **现状**：文件工具用 `abspath` 前缀判断，工作区内符号链接可指向外部；ExecTool 只设置 cwd 和正则黑名单，仍可用绝对路径、`cd ..`、解释器和网络访问任意目标。
- **影响**：不能把“工具以 workspace 为边界”的注释当作安全保证。在不可信渠道或 prompt injection 下可读写工作区外数据。
- **建议验收**：明确威胁模型；文件路径用 `realpath`/symlink 策略；Shell 若要真正隔离应使用 OS 沙箱/容器和 allowlist，而不是继续扩充黑名单。
- **证据**：`agent/tools/filesystem.py::_safe_path`、`agent/tools/shell.py::execute`。
- **局部缓解**：场景 Agent 的普通文件工具现已额外使用 `realpath/commonpath`，并
  隐藏 `workspace/agents` 控制面。但场景可以按 Profile 获得原始 `exec`，它会绕过
  应用层文件保护；主 Agent 和通用临时子 Agent 也保留原有边界，因此本问题尚未核销。

### P1：近期应处理

| ID | 问题 | 当前影响 / 建议 |
|---|---|---|
| NC-TEST-001 | 无正式测试、CI、lint、类型检查 | 历史验证多为已删除临时脚本；先建立 pytest 回归基线，优先覆盖 P0、Gateway 并发、Provider 流式、路径边界 |
| NC-BUG-003 | 工具循环硬熔断不可达 | 重复签名达到 10 次后直接返回警告且不再累计，永远到不了 20 次硬熔断；调整计数语义并测试 10/20 边界 |
| NC-BUG-004 | session_key 文件名映射有损 | `:` 写为 `_`，列表再把所有 `_` 还原成 `:`；原 key 含下划线时失真/碰撞，飞书 chat_id 正常会含 `_`；需可逆编码或显式元数据 |
| NC-BUG-005 | 当前进程的会话检索索引陈旧 | `rebuild_all()` 只在启动执行，搜索仅刷新 memory 文件；新会话内容重启前搜不到；需增量更新或刷新策略 |
| NC-ARCH-001 | 配置热更新对象分裂 | 新会话只更新部分 Provider/Context；MCP、skills、workspace、共享 memory provider、工具注册仍是启动值；UI 需明确每字段生效方式或统一重载 |
| NC-ARCH-002 | 无背压且缓存不回收 | Queue 无 maxsize，每消息无界 create_task，Agent/lock 永久缓存；需并发上限、队列策略和会话回收 |
| NC-ARCH-003 | Channel 停止不是优雅关闭 | Web/飞书后台守护线程未真正 stop；测试、热重启和嵌入式运行可能泄漏资源 |
| NC-FEAT-001 | 飞书不支持图片 | `_on_message` 只处理 text；当前完整图片链路只覆盖 Web |

### P2：明确边界或后续优化

| ID | 项目 | 说明 |
|---|---|---|
| NC-OPS-001 | Mac 睡眠期间飞书消息可能丢失 | WS 不是持久队列；launchd/caffeinate 本机脚手架也不能解决纯电池合盖睡眠 |
| NC-TEST-002 | 生图真实成功链路未验 | 历史只用假 key 验证请求到服务端并得到结构化 401；需真实服务的受控集成测试 |
| NC-MEM-001 | 记忆软规则不保证执行 | Cue、14 天冷却、Follow Up 依赖模型自律；Daily 失败静默；摘要失败会丢旧上下文 |
| NC-DOC-001 | README 与实现有少量漂移 | README 称 MCP 多 Server 并行连接，当前实现为顺序 await；配置热更新描述也需更精确 |
| NC-SEC-002 | WebFetch 可访问内网地址 | 只限制 http/https 且跟随重定向；不可信输入下应评估 SSRF 防护 |
| NC-CLEAN-001 | `agent/skills/` 历史副本 | 当前运行入口使用根 `skills/`；确认无外部依赖后可移除重复副本 |

## 6. 运行和产品边界

- Web 当前只能用于严格可信网络；在 NC-BUG-001 修复前，优先绑定 `127.0.0.1`。
- 多实例必须使用不同 workspace、Web 端口和飞书 App；同一个飞书 App 不应被多个进程竞争长连接。
- 进程内长期多 Agent/Mailbox/寻址层尚未实现，也不是当前优先产品方向。
- 本地图片工具只应接受 workspace 内路径；外部目录需显式改变 workspace 或另行授权。
- `base_model_multimodal`、MCP、skills、workspace 等启动期结构变化后需要重启。

## 7. 维护方式

- 修复遗留项时在任务卡引用对应 ID。
- 合并后更新状态、复现命令和关闭提交；不要只在聊天或 `.workbuddy` 追加一条日志。
- 新发现问题必须区分“当前代码已复现”“日志记录”“合理推断”三种证据等级。
