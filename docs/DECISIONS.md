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
| SQLite LIKE 代替 FTS5 | 有效 |
| 会话索引实时增量刷新（TASK-012 决策） | 有效 | `memory_search(scope=session)` 每次搜索前增量扫 sessions：按会话文件 mtime/size 与内存状态 `_session_state` 对比，只对变化/新增会话整会话重索引（DELETE+INSERT，复用 `_index_sessions` 过滤：空 content/tool 角色跳过），未变化跳过，实测 126 会话空闲刷新 0.487ms；`/clear`（删文件）后同步清索引，保证清空会话旧内容搜不到（原「非目标」措辞相应调整，2026-08-06 确认——「文件消失即清索引」是增量状态对比的自然产物，成本近零）；状态仅存内存，重启 `rebuild_all()` 兜底。取舍：超大（上万条）会话整会话重索引稍慢，个人助手场景可接受，未来可优化为偏移续读 |
 中文短词在 unicode61/trigram 下不可靠；当前个人数据量足够小 |
| Daily Memory 是 best-effort 内部机制 | 有效 | `/clear` 不能被摘要失败阻塞；TASK-006 起压缩不再写 daily（见「压缩不再写 daily」决策行） |
| 压缩不再写 daily（TASK-006 决策） | 有效 | 压缩与长期记忆彻底解耦：`ContextCompactor` 只生成当前会话摘要并写 HISTORY.md（审计轨迹），不再调用 `summarize_messages_to_daily`；daily 触发点只剩 `/clear`。代价：在「每日整理/做梦机制」落地前，压缩丢的事件不再留痕 daily（乖宝确认）。压缩结果经 `CompactionResult` 显式返回，压缩器每会话独立实例（2026-08-05）。TASK-011 起压缩摘要也不再写 HISTORY.md（见「HISTORY.md 移除」决策行），daily 触发点变为 `/clear` + 每日做梦整理 |
| HISTORY.md 移除（TASK-011 决策） | 有效 | 压缩审计文件用处不大（乖宝 2026-08-05 拍板）：`ContextCompactor._save_to_history` 删除，压缩摘要仍以 system 消息进上下文，但不再落 `workspace/memory/HISTORY.md`；历史遗留旧文件保留不清理（清理与否乖宝再定）。代价：压缩审计轨迹能力消失，如后续需要可改为可配置开关 |
| 每日做梦整理（TASK-011 决策） | 有效 | 每天 `dream_time`（默认 02:00）定时把当天各会话关键消息经模型按固定分类（`## 用户变化 / ## 项目进展 / ## 会话总结`，可配置）整理，合并更新写入当天 daily：写入前行哈希去重（模型语义去重 + 行哈希兜底）、固定分类标题不再无限堆叠；定时时刻实例未启动则下次启动补做前一天（`workspace/memory/dream_state.json` 记 `last_dream_date`，只补最近 1 天、超期不回溯）；整理异步执行、失败静默，不阻塞聊天/启动；独立 asyncio task 与 ReminderScheduler 并存互不影响；`dream_time` 属启动期配置，缺省回退默认值（与 `context_budget_tokens` 同款容错） |
| 每日做梦整理语义修正（TASK-013 决策） | 有效 | 三处语义修正（2026-08-06，08-05 全天 1356 条消息漏整理教训）：① 定时到点整理「昨天完整自然日」而非「当天」——8-10 02:00 整理 8-9 全天（含凌晨尾巴），由 `build_dream_components.consolidate_today` 计算 today−1；② 启动补做目标改为「最后一个有消息的日期」——`find_last_active_date` 从昨天往前倒序扫，只补一天、无消息不补不推进；③ 晚启动竞态修复——调度器晚启动立即补跑的也是「昨天」（不再是当天），从根上消除「当天补跑先推进 last → 昨天补做被跳过」；定时补跑与启动补做经 `_done_this_run` 集合 + 串行锁去重，只整理一次。`last_dream_date` 只推进到真正整理完成的日期（无消息/模型失败不推进），只前进不后退。取舍：停机多日有多个活跃日时只补最后一天，更早的活跃日不进 daily（任务卡非目标明确不做全量回溯）。遗留：单进程多日睡眠唤醒不触发 catch_up（仅重启触发）；消息时间戳按机器本地记、做梦归日按 config.timezone，二者需一致（当前部署 CST==Asia/Shanghai 自洽）；mtime 剪枝对「回填历史时间戳」场景不安全（当前无回填） |
| 睡眠唤醒补做（TASK-014 决策） | 有效 | 单进程多日睡眠/挂起后唤醒（同一实例跨日 >1 天，如 8-9 → 8-13）时，除整理昨天外再补做一次「最后有消息的日期」：`DreamScheduler.__init__` 新增可选 `on_wake_catch_up` 回调（默认 None 向后兼容），`run()` 跨日分支计算 `wake_gap = (local_today - last_date).days`，`last_date` 非 None 且 `wake_gap > 1` 时在 `_safe_consolidate` 之外调 `_safe_wake_catch_up`；注入的正是 `catch_up_yesterday` 闭包，天然复用 `find_last_active_date` + `_done_this_run` + 串行锁去重，不重复调模型；触发顺序先 consolidate 后 catch_up（昨天有消息时 last 先推进，扫描区间自动排除昨天）。首启（last_date is None）不触发（启动 catch_up task 已覆盖）；跨日==1 不触发。取舍：真实系统休眠时 asyncio 挂起/恢复未端到端验证（mock 时钟覆盖逻辑层）；反复唤醒多一次只读日期扫描可接受 |
| 压缩→无条件重建完整快照（TASK-007 决策） | 有效 | 压缩发生 = 前缀缓存已破，`_sync_memory_patch` 不再因「revision 已最新（本会话自写已刷基线）」或「无落后 entries」提前 return，无条件执行 `_rebuild_memory_snapshot`：读磁盘最新 USER.md/MEMORY.md 生成 <memory_snapshot> 替换历史里旧补丁，模型上下文始终有完整记忆。**与 GPT 方案 §4.5「压缩不参与快照重建判断」相反**：压缩后补丁消息可能已被摘要吞掉，靠 entries 判断会漏（模型只剩摘要残影）；塞一条完整快照是破缓存后的免费增量。重建读磁盘不依赖 entries，revision 不落后也安全（`_advance_memory_revision` 幂等）。未压缩时行为不变（零注入/补丁/快照按原阈值）。`_should_rebuild_memory` 中 `consolidated` 提升为决定性条件（2026-08-05，TASK-007，待乖宝验收） |
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
| 微信原生 typing 由 Bridge 管理 activity 生命周期 | 有效 | Python/Gateway 只传稳定 target 与不含秘密的 activity ID；Bridge 在内存中按 target 计数、用 getconfig/sendtyping 续期，最终回复被渠道接受后才 cancel，失败/取消/shutdown/session expired 均 best-effort 且不阻塞回答 |
| 微信入站采用 ack 后批次提交 cursor | 有效 | context 先落盘，Python 投递成功后 ack，整批完成才提交去重与 cursor；崩溃允许重复、不允许静默丢失 |
| 微信 allowlist 默认 deny-all | 有效 | 单账号私聊仍是外部不可信入口；空列表不放行，`*` 必须由用户显式选择 |
| 微信图片等待窗口与会话键对齐 Gateway | 有效 | 默认等待 10 秒合并同一用户的后续图文，接收图片先原子落盘再 ack，MessageBus 消费确认后才删除；ImageStore 使用完整 `weixin:<target>`，保证 `ask_image` 可按 image_id 解析 |
| 微信出站图片 AES 密钥采用腾讯线格式 | 有效 | `getuploadurl.aeskey` 使用 32 位十六进制；消息 `media.aes_key` 是该 ASCII 十六进制字符串的 Base64，不是原始 16 字节密钥的 Base64，否则微信端无法解密并显示“图片已过期或已被清理” |
| 微信发送回执同时检查 HTTP 和 JSON | 有效 | 要求 HTTP 成功和有效 JSON；腾讯成功响应可省略 `ret/errcode`，任一存在且非零或非数值都失败，correlation/client ID 在重试间稳定 |
| 微信 `-14` 切换凭据代次 | 有效 | 清除 account/cursor/context/去重后重新扫码；旧 context 不跨认证代次复用，避免稳定的主动发送失败 |
| 微信语音直接用腾讯 STT（`voice_item.text`），不落地本地 ASR | 有效 | 腾讯已提供服务端转写，零成本零延迟；实测语音为 silk 编码，本地 ASR 需额外解码器。TEXT 项在前、VOICE 转写追加在后换行合并；腾讯 text 为空时保持忽略，不做本地 fallback |
| 微信文件按月归档+引用式传递，不按会话分散 | 有效 | 文件是长期资产（非一次性查看），统一按月目录 `workspace/files/YYYY-MM/` 便于用户查找；发模型只带「文件名+路径+大小」引用（不读内容、不花 token），乖宝说「帮我看看」时 Agent 用 `read_file` 按需读取。文件名消毒+重名加后缀+50MB 上限；`/clear` 不删文件 |
| 微信收到文件 Agent 不主动读内容（行为约束走渠道快照） | 有效 | 代码层只保证「引用式传递」，但模型可能拿到引用后自行 read_file/exec 读取，违背「乖宝说帮我看看再读」的设计。约束入 `agent/context.py::_channel_section`：微信渠道收到 📎 文件引用时只确认收到，不主动读取内容（含 read_file / exec 等任何方式），等用户明确指令后再读。TASK-003 验收实机发现后补充（2026-08-05） |
| 渠道感知走会话级快照（System Prompt）而非每轮前缀注入 | 有效 |
| 跨会话记忆同步走「快照+版本补丁」机制 | 有效 | 记忆文件（USER.md/MEMORY.md）被其他会话写后，本会话经 <memory_patch> 补丁感知，不靠模型自觉。全局 revision 存 workspace/memory/changelog.jsonl（每写一次 +1），会话 revision 存 <safe_key>.meta.json 侧车；每轮比对，变了才把补丁（system 角色）插在历史之后、本轮 user 之前并持久化进 JSONL；无变化零注入（不破坏前缀缓存）。补丁是认知更新，模型不得将其回写记忆文件（实测 Agent 收到补丁后误以为要同步文件而覆盖写盘，已加行为约束） |
| 记忆补丁必须持久化进会话历史 | 有效 | 补丁不落盘则下一轮只剩旧快照，等于忘记更新（早期「用完即弃」方案的关键修正） |
| Agent 自写记忆即刷基线 | 有效 | write_file 写 USER.md/MEMORY.md 成功后递增全局 revision 并立即更新本会话 memory_revision，下一轮不给自己发自己刚写的补丁（死机制不靠自觉） |
| 补丁过多时重建完整记忆快照 | 有效 | 累积补丁 >20 条 / 总量 >约1000 tokens / 大量删除时，用当前文件全文生成 <memory_snapshot> system 消息替换旧补丁们；一次破缓存后继续稳定。压缩触发重建已提升为独立决定性条件（见「压缩→无条件重建完整快照」决策行，TASK-007） |
| 记忆同步一刀切只同步 USER.md/MEMORY.md | 有效 | 不做「重要性」判断；daily/ 永不注入（需要时走 memory_search），规则简单、系统可执行 |
| 记忆补丁角色用 system 而非 developer | 有效 | DeepSeek 不支持 developer 角色（已核实）；OpenAI 兼容 API 允许多条 system 消息（压缩摘要已有先例） | 渠道在会话内恒定，每轮注入是冗余的；快照零每轮 token、System Prompt 前缀稳定（Prompt Cache 友好）。渠道+用户已天然编码在 session_key（`channel:sender_id`）中，`make_agent_factory` 解析后注入 ContextBuilder，与 identity/USER/MEMORY 同构；时间戳因每轮变化仍走每轮前缀注入 |

| 压缩/快照重写保留原始时间戳（TASK-015 / NC-MEM-002） | 有效 | `save_messages(preserve_timestamps=True)`：覆盖写回前按「规范化身份」依次取输入消息自带 timestamp → 原文件同身份消息 timestamp → 补当前时间。压缩写回与压缩后「无条件重建完整快照」（TASK-007）都走保留模式——尾部原样保留的消息保持原始时间戳（内容不变则时间戳真实性应保持），仅新生成的摘要/快照消息取当前时刻。取舍：身份匹配用 role/tool_call_id/content/tool_calls 稳定序列化，canonicalize 前后一致；带图片的 user 消息经 `_history_item_to_api` 转多模态 content 后与磁盘原文身份不同，此类消息时间戳退化为当前时刻（罕见边缘）；默认 False 行为不变（取消补历史走 `_persist`/`save_message`、子 Agent 走 no-op，均不受影响） |
| write_dream 进程内写锁（TASK-015 / TASK-011 遗留） | 有效 | `DailyMemory` 实例级 `threading.Lock`：`write_dream`（读-合并-写回）整体持锁，`append` 共享同一把锁（头部「文件不存在时建」是先查后写，也需串行）。asyncio 单线程事件循环内锁从不阻塞（临界区无 await，天然原子），跨线程时由锁串行。append 本身是单次 open 追加、无读-改-写，不加锁也不会丢数据，但与 write_dream 同锁可防「先查后写」竞态。取舍：仍非崩溃安全（进程崩溃可能留半截文件），如需更强可升级「临时文件 + os.replace」原子写（本任务未做） |
| 定时整理失败当日自动重试（TASK-015） | 有效 | `run_dream_for_date` 返回 bool（True=完成/无事可做，False=模型失败/空响应/异常）。DreamScheduler 失败后在当日稍后重试：最多 `dream_max_retries` 次（默认 3）、间隔 `dream_retry_interval` 秒（默认 1800=30 分钟，可注入时钟/wait 单测），重试等待跨日即放弃；`last_dream_date` 仍只推进到真正整理完成的日期，失败不提前标记（下次启动补做兜底）。与 `_done_this_run` + 串行锁去重配合：失败日期不入去重集合可重试，成功入集合不重复调模型。None（旧式回调）视为成功向后兼容 |
| 上下文预算动态配置 + 占用显示（TASK-005） | 有效 | `config.json` 的 `context_budget_tokens`（默认 524288=512k）作为 ContextCompactor 压缩阈值，替换硬编码 192k；旧配置缺字段回退默认值（向后兼容）。AgentLoop 每次模型响应后经 stream_sink 推 `{type:"usage",...}`（input/cached/uncached/budget/ratio/cache_ratio，无真实 usage 时回退估算）；新增公开方法 `get_context_usage()` 供 web/feishu/weixin/cli 的 `/context` 命令直接回复占用文本（不经模型）。Web 输入框上方渲染进度条 + 缓存命中率，随会话切换各自显示；无数据优雅降级。预算属启动期配置，改后需重启（2026-08-05） |

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
| 08-05 | TASK-004 记忆跨会话同步：全局/会话 revision + changelog.jsonl + <memory_patch> 补丁注入与持久化 + 自写刷基线 + 快照重建 | 每轮必须保证的事要系统机制化，不能靠模型自觉（时间戳先例同理）；补丁插在历史之后、user 之前，破缓存量=补丁大小 |
| 08-05 | TASK-003 实机验收补充：微信端 Agent 收到文件引用后自行 read_file/exec 读取，违背按需读取设计；行为约束补入渠道快照（`agent/context.py::_channel_section` 微信分支） | 代码层只能保证引用式传递，管不住模型行为；行为规则应放 System Prompt 渠道专属 section |
| 08-05 | TASK-006 压缩器解耦：`MemoryConsolidation` 改名 `ContextCompactor`；压缩器改为每会话独立实例（无共享 `last_consolidation`/`last_estimate`）；`maybe_compact` 返回 `CompactionResult`；压缩不再写 daily（触发点只剩 `/clear`） | 共享可变状态是跨会话 Bug 之源，改显式返回值；「当前会话需要看多少上下文」与「系统以后该记住什么」彻底分离 |
| 08-05 | TASK-007 压缩触发→无条件重建记忆快照：`_sync_memory_patch` 将 `consolidated` 判断提前到 early-return 之前，压缩过即重建（修「本会话自写记忆 + 压缩后完整记忆只剩摘要残影」洞）；`_should_rebuild_memory` 的 `consolidated` 提升为决定性条件（与 GPT 方案 §4.5 相反） | 压缩=缓存已破=塞完整快照最划算；重建读磁盘不依赖 entries，revision 已最新也安全；未压缩路径逐字不变，前缀缓存命中不受影响 |
| 08-05 | TASK-010 会话中断回合上下文丢失修复（Agent「失忆」bug）：`_persist` 改为「落盘即同步内存」——每写一条磁盘就按磁盘顺序增量追加进 `_session_history`，磁盘是唯一事实源；`_record_cancelled_turn` 去重不再手动追加；刻意不做整段 canonicalize（避免工具交换被 close_pending 补占位符导致真实 tool 结果丢失），收尾边界统一 canonicalize 兜底 | 「回合完成时才同步内存」是失忆根因；任意中断路径（超时/error/异常/取消/崩溃）内存都不丢；真进程重启本就从磁盘全量恢复（不受影响）；遗留：压缩 `save_messages` 改写时间戳潜在风险（NC-MEM-002） |
| 08-06 | TASK-013 每日做梦整理语义修正：定时整理「昨天完整自然日」而非「当天」（`consolidate_today` 目标=today−1）；启动补做改为 `find_last_active_date`（从昨天往前找最后有消息日期，只补一天、无消息不推进）；晚启动竞态修复（调度器立即补跑目标也是昨天）+ `_done_this_run`/锁去重；`run_dream_for_date` 无消息早退不推进 last；`collect_messages_for_date` 增加 mtime 剪枝与 stop_at_first | 定时与补做目标统一为「最后有消息日期」才能从根上消除竞态；无消息不推进是「不漏整理」的关键；mtime 剪枝只跳过「未被该日修改过」的文件（安全方向）；与 TASK-011 冲突处以本卡为准 |
| 08-06 | TASK-015 记忆小尾巴修复：① NC-MEM-002——`save_messages` 新增 `preserve_timestamps`（保留模式按规范化身份依次取：输入自带 → 原文件同身份 → 补当前时间）；压缩写回与压缩后「无条件重建快照」（TASK-007）都走保留模式，尾部保留消息时间戳不再被改写成压缩时刻；② `DailyMemory.write_dream`/`append` 加进程内 threading.Lock（读-合并-写回与头部「先查后写」串行，append 单次 open 追加本无读-改-写）；③ DreamScheduler 失败当日自动重试（`run_dream_for_date` 改返回 bool；最多 3 次、间隔 30 分钟可注入，跨日放弃，`last_dream_date` 仍只推进到真正完成）；④ 摘要质量抽查脚本 `scripts/dream_summary_spotcheck.py` 就绪（读会话 JSONL + 真实 provider 跑 ContextCompactor 分块结构化摘要，`--dry-run` 不花 token，待乖宝确认后执行） | 压缩后重建快照是压缩链路的下一环，不改它时间戳照样被立刻改写（集成测试证实）；保留时间戳靠「原文件按身份找回」而非输入自带（canonicalize 剥离 timestamp）；重试成功才推进 last 与 `_done_this_run`，失败日期可被重试/下次启动补做再次进入；scripts/ 目录被 .gitignore 忽略属项目既有约定 |
| 08-05 | TASK-011 每日做梦整理：砍 HISTORY 写入（`_save_to_history` 删除，压缩摘要不再落盘）；`agent/daily.py` 新增 `dream_consolidate`（固定分类 + 行哈希去重 + 合并更新）；`main.py` 新增 DreamScheduler（每日定时）+ DreamState（启动补做前一天）+ `collect_messages_for_date` + `dream_time` 配置 | 定时整理要能注入时钟可单测；补做状态单调前进、模型失败不标记（重启可重试）；整理失败静默符合 daily nice-to-have 原则；与 ReminderScheduler 独立 task 互不影响 |
| 08-04 | 微信文件接收（TASK-003）：Bridge FILE 入站下载 → FileStore 按月归档 → content 只带引用 | 文件名不可信需消毒防穿越；文件大小需设上限防撑爆磁盘；命令消息保持纯文本判断不变；批量持久化需兼容旧批次缺 `files` 字段 |

## 4. 已替代或已核销的旧记录

- “暂不支持多会话”已被 CLI、飞书和 Web 的多会话实现替代。
- MemoryConsolidation（TASK-006 起改名 `ContextCompactor`）“尚未接入 AgentLoop”和 token 估算 TODO 均已完成。
- Web 旧页显示 JSON、断线不重连、`web:web:` 双前缀、流式挂起无超时等问题已有对应修复，不能仅凭旧日志当作当前 Bug。
- 历史日志对统一工具超时互相冲突；当前普通工具由 `ToolRegistry.execute()` 使用 180 秒兜底，Shell 另有 60 秒超时。子 Agent 由自身回合上限管理，生图由单请求超时和整次任务预算管理，避免普通工具上限提前截断长任务和重试。
- 历史日志称最后提交无法 push；当前 Git 实测 `HEAD` 与 `origin/main` 均为 `0daefc4`，领先/落后为 `0/0`，此项已核销。
- “飞书不支持图片”已核销：当前支持私聊图片入站，以及 Agent/子 Agent 生成图片出站；群聊仍要求事件包含 @ 提醒。
- NC-BUG-005 已核销（TASK-012 会话索引实时性）：`memory_search(scope=session)` 每次搜索前增量扫 sessions（mtime/size 对比只重索引变化/新增），`/clear` 后同步清索引；重启 `rebuild_all()` 兜底，新会话内容无需重启即可搜到。
- 工具消息重启后可能触发 400 已核销：新记录按 `assistant(tool_calls) → tool` 落盘；读取旧会话时会重排历史前置 tool、补齐缺失结果并丢弃孤立 tool。飞书因稳定复用 `chat_id` 更容易暴露旧问题，Web 的新连接默认产生新 ID，但接回旧会话时同样受修复保护。
- NC-MEM-002 已核销（TASK-015）：压缩 `save_messages` 覆盖写回改写会话文件时间戳的问题已修复——`save_messages` 新增 `preserve_timestamps` 参数（保留模式从原文件按身份找回原始时间戳），压缩写回与压缩后「无条件重建快照」均走保留模式；时间戳真实性不再被压缩改写。
- 「（未编号·TASK-011 遗留）`write_dream` 读-合并-写回无文件锁」已核销（TASK-015）：`DailyMemory` 增加实例级 `threading.Lock`，`write_dream` 与 `append` 共享锁串行写，并发不丢数据（新增多线程并发测试）。
- 「定时整理失败当日不自动重试」已核销（TASK-015）：`run_dream_for_date` 返回 bool，DreamScheduler 失败后当日自动重试（最多 3 次、间隔 30 分钟可注入），`last_dream_date` 仍只推进到真正整理完成的日期。

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
| ~~NC-MEM-002~~（TASK-015 已核销） | 压缩 `save_messages` 覆盖写回会改写会话文件时间戳 | 已修复：`save_messages(preserve_timestamps=True)` 从原文件按身份找回原始时间戳，压缩写回与压缩后无条件重建快照均走保留模式，详见 §4 核销记录 |
| NC-LICENSE-001 | 微信社区基础缺少独立 LICENSE | 上游 `package.json` 声明 MIT，但仓库没有 LICENSE 文件；当前已固定来源/NOTICE，正式分发前仍需维护者或法律复核 |
| NC-WEIXIN-001 | 微信真实端点未验收 | 自动化使用 fake iLink HTTP/CDN/clock/process；真实扫码、长轮询、图片和主动发送需用户授权后受控手工验收 |
| NC-WEIXIN-002 | 微信主动发送需要历史 context token | 对端至少入站交互一次后才可发送；Bridge 按 account/user 持久化，V1 不绕过服务端这项协议约束 |
| ~~（未编号·TASK-011 遗留）~~（TASK-015 已核销） | `write_dream` 读-合并-写回无文件锁 | 已修复：`DailyMemory` 实例级 `threading.Lock` 串行 write_dream/append，详见 §4 核销记录 |
| NC-CACHE-001 | 工具 Schema 与图片缓存存在供应商差异 | 本地只保证请求表示稳定并记录 hash；供应商可能不缓存工具或图片。图片缺失/变化与 ContextCompactor 摘要替换都会形成前缀断点 |

### P3：低风险 / 观察 / 决策待定（2026-08-06 盘点收拢，来自历次任务卡遗留）

| 来源 | 项目 | 说明 |
|---|---|---|
| TASK-003 遗留 | `read_file` 路径语义不统一 | 文件引用为 `files/YYYY-MM/name`（相对数据根 `workspace/`），`ReadFileTool` 根为 `config.workspace`（项目根）；Agent 按引用读需写 `workspace/files/...`。实测模型能自行拼对；若要让 `files/...` 直接可读需另开任务统一 read_file 根或调整路径语义 |
| TASK-003 遗留 | 50MB 大小上限两侧常量 | Python 侧 `max_inbound_file_bytes` 固定 50MB；若单独调低 Bridge env 上限，Python 仍按 50MB 读满，无实际风险 |
| TASK-004 遗留 | Web 前端对历史 system 补丁渲染未验证 | 与压缩摘要同路径，风险低 |
| TASK-008 遗留 | `agent/daily.py` 同构降噪副本未统一 | `_messages_to_text`/`_summary_content` 副本未降噪（TASK-008 只降噪压缩路径）；轻微 over-retention，TASK-009 前评估统一 |
| TASK-008 遗留 | `_titles_only` 对正文 `###` 开头行误留 | 轻微 over-retention，可加行长度护栏 |
| TASK-009 遗留 | 真实模型摘要质量人工抽查 | 分块结构化摘要的真实模型质量属人工范畴，使用中留意 |
| TASK-011 遗留 | 定时整理失败当日不自动重试 | 模型失败那天被跳过，仅下次重启且昨天未整理时补做；数据不丢（仍在会话/daily） |
| TASK-011 遗留 | `collect_messages_for_date` key 还原有损 | `stem.replace("_", ":")` 恒映射回同一文件，不丢消息；与既有处理一致 |
| TASK-011 遗留 | 去重近似性 | 语义去重靠模型，行哈希兜底只能拦规范化后完全一致的重复 |
| TASK-011 决策项 | `workspace/memory/HISTORY.md` 历史文件清理 | TASK-011 起停止新写入；旧文件保留与否待乖宝拍板，默认保留不动 |
| TASK-013 遗留 | 重启当天重复整理昨天 1 次安全冗余 / 首启全扫描性能 / 时区假设 / 测试代理弱化 | P2 留档不改，详见 TASK-013 任务卡「实现进展」段 |
| TASK-014 遗留 | 真实休眠 asyncio 挂起/恢复未端到端验证 | mock 时钟已覆盖逻辑层；系统休眠期间 `asyncio.wait_for` 挂起/恢复行为未实测，逻辑层已覆盖 |

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
