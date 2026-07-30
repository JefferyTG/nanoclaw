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
| 当前时间不进入 System Prompt，按需使用专用工具 | 有效 | 动态时间即使位于单个 System 文本末尾，也会阻断后续历史的精确前缀；仅时间/日期/提醒相关任务调用 `get_current_time` |
| 慢变上下文采用显式快照 | 有效 | identity、USER、MEMORY、Agent 摘要在新会话或 `/clear` 刷新；Skill 摘要为启动快照，需重启；避免每轮文件扫描与前缀抖动 |
| 工具 Schema 在 MCP 装配后确定性冻结 | 有效 | 按名称排序并 hash；成功连接集合变化是显式 cache boundary，不假设供应商一定缓存工具定义 |
| 跨轮与重启使用 canonical API 历史 | 有效 | 工具顺序自愈；顶层 reasoning 在当前工具循环、落盘和恢复中一致，确保只追加时维持精确前缀 |
| 缓存观测只记录结构元数据 | 有效 | 调用/回合记录 token、hash、消息数和迭代；不记录 prompt/记忆/参数/密钥，回合比率按 token 总和加权 |
| SQLite LIKE 代替 FTS5 | 有效 | 中文短词在 unicode61/trigram 下不可靠；当前个人数据量足够小 |
| Daily Memory 是 best-effort 内部机制 | 有效 | `/clear` 和压缩不能被摘要失败阻塞 |
| Skill 与人设解耦 | 有效 | Skill 写行为机制，具体口吻由 identity 决定 |
| 视觉双路径 | 有效 | 基础模型多模态则直传；否则用 `ask_image` 调独立视觉模型 |
| 生图服务完全配置化 | 有效 | 不在代码绑定特定服务商/模型；一个工具覆盖文生图、图生图、多源图 |
| 飞书图片复用 ImageRef 消息协议 | 有效 | 入站图片进入共享 ImageStore/视觉链路；出站图片由 Gateway 解析后交渠道上传，不复制 Agent 逻辑 |
| 飞书图片采用短窗口图文合并 | 有效 | 图片事件缺少用户说明；默认等待 10 秒，同用户连续图片重置计时，文字到达后只触发一次 Agent |
| Web 前端无构建步骤 | 有效 | 单文件 HTML/CSS/JS，配合 no-cache 和页面版本握手 |
| Web ASR 只向核心投递转写文本 | 有效 | 音频在渠道边界临时处理；Bus、Gateway、AgentLoop 与会话协议保持文本语义 |
| ASR 与主模型配置/凭证分离 | 有效 | Chat Completions 兼容不代表支持 Audio Transcriptions；便于替换供应商并隔离权限与账单 |
| Web TTS 是可取消的附加能力 | 有效 | 只消费实时 Agent 新回复，不进入 Bus/会话；失败、关闭或浏览器拒播不得影响文字聊天 |
| TTS 采用分句流水线 | 有效 | 强标点优先切分、弱标点达到长度再切；合成与播放并行，避免等待完整长回复和逐 token 请求风暴 |
| 子 Agent Web 事件必须命名空间化 | 有效 | 子层级的 token/done 不能污染父回复或触发 TTS；父会话只持久化有界回放摘要和图片 ID |
| 缺失人设采用 Gateway 级首次引导 | 有效 | 三种渠道共用同一入口；引导不调用模型、不进入会话历史，用户描述原子写入后每轮自动重读 |
| Linux 控制脚本使用独立进程组 | 有效 | start/stop/restart/status 无需固定安装路径；SIGTERM 先触发应用清理，超时才强制结束整个进程组 |
| 提醒目标必须显式绑定且单实例二选一 | 有效 | 工具不接受 channel/chat/user ID；飞书/微信同一时刻只允许一个目标。原 owner 主动解绑后可换渠道或用户，历史任务原位迁移；绑定事务串行化，旧回执按 binding revision 隔离；登录/context 异常只暂停且不释放所有权 |
| 提醒周期统一使用 DTSTART + timezone + RRULE | 有效 | 避免 once/daily 特例；next occurrence 从原计划计算并覆盖 DST、COUNT、UNTIL |
| 提醒调度以独立 SQLite 为事实源 | 有效 | 单 Scheduler 动态等待，WAL + 原子 claim/lease 支持重启恢复；不混入可重建的 memory/index.db |
| 动态任务先固化 Agent 输出再发送 | 有效 | scheduled 独立会话不污染日常聊天；发送重试复用相同文本，第一版明确为 at-least-once |
| 提醒回执沿用 Bus/Gateway/Channel 链路 | 有效 | OutboundMessage 仅为可靠发送附可选 future，普通聊天继续 fire-and-forget；目标 channel 由持久 target 决定 |
| 微信提醒复用现有 Scheduler 与 Bridge 主动发送 | 有效 | SQLite 按稳定 target 精确路由，execution correlation ID 跨重试/重启稳定；会话过期/context 缺失暂停目标并释放 claim，不复制调度器或保存 token |
| 微信采用固定 Node Bridge 而非重写协议或运行 OpenClaw | 有效 | vendor `wechat-ilink-client` 固定提交，Python 只做 Channel/JSONL/生命周期，Agent 逻辑继续复用 NanoClaw |
| 微信稳定身份是 account_id + user_id | 有效 | 会话和未来主动提醒不依赖临时 context token；target 使用可逆编码避免分隔符碰撞 |
| 微信秘密和同步状态由 Bridge 独占 | 有效 | token/cursor/context/去重只写入被忽略的 0700/0600 状态目录，不进入 config、Bus 或普通日志 |
| 微信入站采用 ack 后批次提交 cursor | 有效 | context 先落盘，Python 投递成功后 ack，整批完成才提交去重与 cursor；崩溃允许重复、不允许静默丢失 |
| 微信 allowlist 默认 deny-all | 有效 | 单账号私聊仍是外部不可信入口；空列表不放行，`*` 必须由用户显式选择 |
| 微信图片等待窗口与会话键对齐 Gateway | 有效 | 默认等待 10 秒合并同一用户的后续图文，接收图片先原子落盘再 ack，MessageBus 消费确认后才删除；ImageStore 使用完整 `weixin:<target>`，保证 `ask_image` 可按 image_id 解析 |
| 微信出站图片 AES 密钥采用腾讯线格式 | 有效 | `getuploadurl.aeskey` 使用 32 位十六进制；消息 `media.aes_key` 是该 ASCII 十六进制字符串的 Base64，不是原始 16 字节密钥的 Base64，否则微信端无法解密并显示“图片已过期或已被清理” |
| 微信发送回执同时检查 HTTP 和 JSON | 有效 | 要求 HTTP 成功和有效 JSON；腾讯成功响应可省略 `ret/errcode`，任一存在且非零或非数值都失败，correlation/client ID 在重试间稳定 |
| 微信 `-14` 切换凭据代次 | 有效 | 清除 account/cursor/context/去重后重新扫码；旧 context 不跨认证代次复用，避免稳定的主动发送失败 |

这些约定来自 `.workbuddy/memory/MEMORY.md` 和 2026-07-22 至 2026-07-26 的开发日志；原日志被 Git 忽略，因此本表是跨会话的正式摘录。

## 3. 演进时间线

| 日期 | 主要变化 | 仍然重要的经验 |
|---|---|---|
| 07-22 | Tool、Provider、Context、AgentLoop、文件/Shell/Web 工具 | 工具异常要归一化；路径和进程执行必须有边界与超时 |
| 07-23 | MessageBus、技能、JSONL 会话、历史压缩 | OpenAI tool-call 消息顺序必须自洽；压缩必须同步写回磁盘 |
| 07-24 | CLI/飞书/Web 多会话、Web 流式、并发锁、历史侧边栏 | 所有流式错误/超时路径都要发 `done`；同会话串行 |
| 07-25 | Web 重连、MCP、Prompt Cache 排序、常驻运行探索 | 后台 WS 不等于消息持久队列；睡眠期间可能丢消息 |
| 07-26 | 记忆、图片/视觉、生图、工具耗时 | 配置/消息/持久化是跨层协议；临时测试不应再删除 |
| 07-29 | 主动提醒、RRULE 调度、飞书绑定与可靠回执 | 持久状态机与副作用分离；Agent 输出必须先落库再发送 |
| 07-29 | 微信私聊 V1、Node Bridge、扫码/图片/状态恢复 | 跨进程协议先定契约；cursor 必须在消费确认后推进；fake 服务不能与错误实现共同自洽 |
| 07-30 | Prompt Cache 稳定前缀、时间工具、usage 观测 | 动态字段不能放在历史前；缓存缺失字段不能当作 0 命中；工具/图片缓存能力属于供应商边界 |

## 4. 已替代或已核销的旧记录

- “暂不支持多会话”已被 CLI、飞书和 Web 的多会话实现替代。
- MemoryConsolidation “尚未接入 AgentLoop”和 token 估算 TODO 均已完成。
- Web 旧页显示 JSON、断线不重连、`web:web:` 双前缀、流式挂起无超时等问题已有对应修复，不能仅凭旧日志当作当前 Bug。
- 历史日志对统一工具超时互相冲突；当前普通工具由 `ToolRegistry.execute()` 使用 180 秒兜底，Shell 另有 60 秒超时。子 Agent 由自身回合上限管理，生图由单请求超时和整次任务预算管理，避免普通工具上限提前截断长任务和重试。
- 历史日志称最后提交无法 push；当前 Git 实测 `HEAD` 与 `origin/main` 均为 `0daefc4`，领先/落后为 `0/0`，此项已核销。
- “飞书不支持图片”已核销：当前支持私聊图片入站，以及 Agent/子 Agent 生成图片出站；群聊仍要求事件包含 @ 提醒。
- 工具消息重启后可能触发 400 已核销：新记录按 `assistant(tool_calls) → tool` 落盘；读取旧会话时会重排历史前置 tool、补齐缺失结果并丢弃孤立 tool。飞书因稳定复用 `chat_id` 更容易暴露旧问题，Web 的新连接默认产生新 ID，但接回旧会话时同样受修复保护。

## 5. 当前遗留问题清单

### P0：应优先修复

#### NC-BUG-001 Web 管理面泄露敏感配置且无认证

- **现状**：WebChannel 明确免登录；`GET /api/config` 按 `_CONFIG_FIELDS` 原样返回配置，其中包括主 API Key、飞书 Secret、视觉/生图 Key；POST 还能修改并写回配置。启用时默认 host 为 `0.0.0.0`。
- **影响**：同网段非可信访问者可读取密钥、会话与图片，并修改配置。README 的“可信局域网”提示不足以防止误暴露。
- **建议验收**：默认只绑定 loopback，或加入认证；秘密字段只返回是否已配置，空值不得覆盖已有秘密；对会话/图片/配置端点统一鉴权。
- **证据**：`channels/web.py` 的 `_handle_get_config/_handle_post_config`，`config.py` 的 `_CONFIG_FIELDS` 和 `web_host`。

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
| NC-TEST-001 | 无 CI、lint、类型检查基线 | 当前已有 unittest 回归集，但尚未自动化运行；应建立 CI，并逐步加入静态检查与关键并发覆盖 |
| NC-BUG-003 | 工具循环硬熔断不可达 | 重复签名达到 10 次后直接返回警告且不再累计，永远到不了 20 次硬熔断；调整计数语义并测试 10/20 边界 |
| NC-BUG-004 | session_key 文件名映射有损 | `:` 写为 `_`，列表再把所有 `_` 还原成 `:`；原 key 含下划线时失真/碰撞，飞书 chat_id 正常会含 `_`；需可逆编码或显式元数据 |
| NC-BUG-005 | 当前进程的会话检索索引陈旧 | `rebuild_all()` 只在启动执行，搜索仅刷新 memory 文件；新会话内容重启前搜不到；需增量更新或刷新策略 |
| NC-ARCH-001 | 配置热更新对象分裂 | 新会话只更新部分 Provider/Context；MCP、skills、workspace、共享 memory provider、工具注册仍是启动值；UI 需明确每字段生效方式或统一重载 |
| NC-ARCH-002 | 无背压且缓存不回收 | Queue 无 maxsize，每消息无界 create_task，Agent/lock 永久缓存；需并发上限、队列策略和会话回收 |
| NC-ARCH-003 | Channel 停止不是优雅关闭 | Web/飞书后台守护线程未真正 stop；测试、热重启和嵌入式运行可能泄漏资源 |

### P2：明确边界或后续优化

| ID | 项目 | 说明 |
|---|---|---|
| NC-OPS-001 | Mac 睡眠期间飞书消息可能丢失 | WS 不是持久队列；launchd/caffeinate 本机脚手架也不能解决纯电池合盖睡眠 |
| NC-TEST-002 | 生图真实成功链路未验 | 历史只用假 key 验证请求到服务端并得到结构化 401；需真实服务的受控集成测试 |
| NC-MEM-001 | 记忆软规则不保证执行 | Cue、14 天冷却、Follow Up 依赖模型自律；Daily 失败静默。历史压缩现已在摘要失败时保留原上下文，但会继续承受超预算风险 |
| NC-DOC-001 | README 与实现有少量漂移 | README 称 MCP 多 Server 并行连接，当前实现为顺序 await；配置热更新描述也需更精确 |
| NC-SEC-002 | WebFetch 可访问内网地址 | 只限制 http/https 且跟随重定向；不可信输入下应评估 SSRF 防护 |
| NC-CLEAN-001 | `agent/skills/` 历史副本 | 当前运行入口使用根 `skills/`；确认无外部依赖后可移除重复副本 |
| NC-LICENSE-001 | 微信社区基础缺少独立 LICENSE | 上游 `package.json` 声明 MIT，但仓库没有 LICENSE 文件；当前已固定来源/NOTICE，正式分发前仍需维护者或法律复核 |
| NC-WEIXIN-001 | 微信真实端点未验收 | 自动化使用 fake iLink HTTP/CDN/clock/process；真实扫码、长轮询、图片和主动发送需用户授权后受控手工验收 |
| NC-WEIXIN-002 | 微信主动发送需要历史 context token | 对端至少入站交互一次后才可发送；Bridge 按 account/user 持久化，V1 不绕过服务端这项协议约束 |
| NC-CACHE-001 | 工具 Schema 与图片缓存存在供应商差异 | 本地只保证请求表示稳定并记录 hash；供应商可能不缓存工具或图片。图片缺失/变化与 MemoryConsolidation 摘要替换都会形成前缀断点 |

## 6. 运行和产品边界

- Web 当前只能用于严格可信网络；在 NC-BUG-001 修复前，优先绑定 `127.0.0.1`。
- 多实例必须使用不同 workspace、Web 端口和飞书 App；同一个飞书 App 不应被多个进程竞争长连接。
- 微信 V1 每个实例只支持一个扫码账号；多实例必须使用不同 workspace/weixin 状态目录，且不应让多个进程竞争同一账号 cursor。
- 进程内长期多 Agent/Mailbox/寻址层尚未实现，也不是当前优先产品方向。
- 本地图片工具只应接受 workspace 内路径；外部目录需显式改变 workspace 或另行授权。
- `base_model_multimodal`、MCP、skills、workspace 等启动期结构变化后需要重启。

## 7. 维护方式

- 修复遗留项时在任务卡引用对应 ID。
- 合并后更新状态、复现命令和关闭提交；不要只在聊天或 `.workbuddy` 追加一条日志。
- 新发现问题必须区分“当前代码已复现”“日志记录”“合理推断”三种证据等级。
