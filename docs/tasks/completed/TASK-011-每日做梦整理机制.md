# TASK-011：每日做梦整理机制（定时整理 daily + 去重 + 砍 HISTORY）

> 状态：✅ 已完成（2026-08-05 验收归档）
> 创建：2026-08-05 ｜ 负责人：code-master（实现）＋ 小奈（设计/验收）
> 基线 commit：`857b003`（main，TASK-010 归档后）
> 归档 commit：待用户确认后提交（未 commit）

## 目标

给 NanoClaw 补上「每日做梦整理」：每天定时把当天发生的重要事件整理成**固定结构**写入 daily（含去重）；定时时刻实例未启动时，**下次启动补做前一天**；同时**移除 HISTORY.md 审计文件**（乖宝拍板用处不大）。

## 背景

- TASK-006 起压缩不再写 daily，daily 触发点只剩 `/clear`——压缩丢的事件在「每日整理/做梦机制」落地前不再留痕（DECISIONS.md 已记录该代价，乖宝当时确认）。
- GPT 记忆方案提出「每日整理」第三阶段：即时事件 + 会话检查点 + 每日整理，本次落地其核心（定时整理 + daily 固定结构 + 去重）。
- 乖宝 2026-08-05 拍板：
  1. **HISTORY.md 去掉**（用处不大；压缩摘要仍进上下文，只是不再落 HISTORY 文件）。
  2. **做梦 = 每天定时**整理当天内容 → 写 daily。
  3. **定时没跑就补做**：若定时时刻实例没启动（关机/没开），下次启动时补整理前一天。
  4. **去重**：做梦整理时做去重（同一事实不重复写入）。
- 当前 daily 为 append-only，`/clear` 时 `summarize_messages_to_daily` 以「压缩前保存」分类追加，分类标题可重复；无固定结构、无去重。

## 范围

- `agent/memory.py`：`ContextCompactor._save_to_history` 移除（不再写 HISTORY.md）；模块 docstring 与相关注释同步。
- `agent/loop.py`：压缩后写 HISTORY 的调用点移除（如有）。
- `agent/daily.py`：新增「做梦整理」能力：
  - **固定结构**：daily 文件按 GPT 建议改为固定分类（`## 用户变化` / `## 项目进展` / `## 会话总结` 等，可配置），不再无限追加重复分类标题。
  - **去重**：整理写入前对已存在内容做去重（按行/按事实哈希），同一事实不重复落盘。
  - 保留 `/clear` 触发写入（现有行为），与做梦整理共存。
- `main.py`：
  - 注册**每日定时做梦任务**（复用或仿照 ReminderScheduler 的调度循环，`asyncio` 内独立 task）。
  - **启动补做检查**：启动时判断「昨天是否有做梦整理记录」，没有则补整理前一天（读昨天 daily 已有内容 + 昨天各会话关键事件）。
  - 做梦时间可配置（config.json 新增字段，如 `dream_time`，默认一个合理时刻如 02:00）。
- `config.json` / `config.example.json`：新增做梦时间配置字段（若需新增）。
- 文档：PROJECT.md（能力矩阵/模块描述）、DECISIONS.md（做梦机制决策 + HISTORY 移除决策）、ARCHITECTURE.md（如涉及数据流）、agent/daily.py docstring、README 相关段。

## 非目标

- ❌ 不做候选记忆缓冲层 / MemoryCandidateStore（乖宝确认保留「主 Agent 自己判断直接写 USER/MEMORY」现状）。
- ❌ 不改变压缩算法本身（TASK-009 已定型）。
- ❌ 不改变 USER/MEMORY 跨会话补丁同步机制（TASK-004/007 行为不变）。
- ❌ 不做「定时调度器」通用化改造，只新增做梦这一个定时任务（复用/仿照现有 scheduler 循环即可，不重构 reminders 模块）。
- ❌ 不清理历史遗留的 `workspace/memory/HISTORY.md` 文件内容（只停止新写入；文件保留与否乖宝再定，默认保留不动）。

## 验收标准

- [x] 移除 HISTORY.md 写入：压缩后不再产生 HISTORY.md 新块；`grep -rn "HISTORY" agent/` 仅剩 docstring 说明（或已清理），`_save_to_history` 删除或不再被调用
- [x] daily 固定结构：做梦整理写出的 daily 使用固定分类（`## 用户变化` / `## 项目进展` 等），不再出现重复分类标题堆叠
- [x] 去重生效：同一事实重复出现在输入时，daily 只写入一次（单测覆盖：相同事件两次整理不重复落盘）
- [x] 定时触发：到配置的做梦时间自动执行整理（单测 mock 时钟推进验证；实机观察日志）
- [x] 启动补做：模拟「昨日做梦时刻实例未运行」→ 启动后自动补整理前一天（单测覆盖；实机验证看 daily 出现昨日整理块）
- [x] `/clear` 写 daily 行为不变（回归测试通过）
- [x] 文档同步：PROJECT.md / DECISIONS.md / ARCHITECTURE.md / daily.py docstring / README 均更新
- [x] 测试：`.venv/bin/python -m unittest discover -s tests` 全过；`git diff --check`、`compileall`、`import main` 通过

## 相关模块

- `agent/memory.py`（ContextCompactor / _save_to_history）
- `agent/loop.py`（压缩落盘路径）
- `agent/daily.py`（DailyMemory / summarize_messages_to_daily / 新增做梦整理）
- `main.py`（装配、定时任务注册、启动补做钩子）
- `config.py` / `config.json`（做梦时间配置）
- `reminders/scheduler.py`（参考其调度循环实现，不重构）
- `docs/DECISIONS.md`、`docs/ARCHITECTURE.md`、`PROJECT.md`、`README.md`

## 实现方案

1. **砍 HISTORY**：`agent/memory.py` 删除 `_save_to_history`（或改为 no-op），压缩摘要不再写 `workspace/memory/HISTORY.md`；同步清理模块头 docstring 中「同一份摘要同步写入 HISTORY.md」的描述。
2. **做梦整理函数**（`agent/daily.py` 或新 `agent/dream.py`）：
   - 输入：日期 + 该日期相关事件源（当天 daily 已有内容 + 当天各会话关键消息，经 `summarize_messages_to_daily` 类似方式用模型提取）。
   - 输出：按固定分类组织的事实列表，写入当天 daily（若当天 daily 已存在则合并更新而非纯追加）。
   - 去重：写入前读取已存在内容，按「规范化行内容哈希」去重；模型输出与已存在事实语义重复时由模型判断合并（提示词加「不要重复已记录内容」指令，兜底用行哈希）。
3. **定时调度**：在 `main.py` 仿照 `ReminderScheduler.start()` 建一个 asyncio 后台 task，每天到点（`dream_time`）跑一次做梦整理；实现要可被单测（注入时钟）。
4. **启动补做**：维护一个状态标记（如 `workspace/memory/dream_state.json` 记录 `last_dream_date`）；启动时若 `last_dream_date < 昨天`，则补做缺失日期（可补最近 1~N 天，默认补最近 1 天，乖宝拍板「前一天」）。补做完成更新状态标记。
5. **配置**：config.json 新增 `dream_time`（如 `"02:00"`），缺省时给默认值不报错（与 `context_budget_tokens` 同款容错模式）。
6. **文档同步**：每完成一步同步更新任务卡与相关文档（文档同步铁律）。

## 测试方式

```bash
.venv/bin/python -m unittest discover -s tests          # 全量单测
git diff --check                                        # 协作最低检查
uv run python -m compileall -q agent bus channels providers session
uv run python -c "import main"                          # 导入冒烟
```

新增单测建议：
- `test_dream_consolidate_writes_fixed_structure`：做梦整理写入固定分类结构
- `test_dream_dedup_same_fact_once`：同一事实两次整理只落一条
- `test_dream_scheduled_runs_at_time`：mock 时钟推进到 dream_time 触发执行
- `test_dream_catchup_on_startup`：last_dream_date 落后时启动补做
- `test_clear_still_writes_daily`：/clear 写 daily 回归

## 风险

- 做梦整理会调模型（成本 + 耗时）：整理需异步执行、失败静默（与 daily 现有 nice-to-have 原则一致），不得阻塞聊天或启动。
- 补做可能跨多天：默认只补最近 1 天，超期不回溯（防止一次补一堆消耗）。
- 定时任务与提醒调度器并存：实现上独立 task，避免互相影响；注意 asyncio 生命周期（优雅关闭）。
- 去重靠模型判断 + 行哈希兜底：语义级去重可能不完美，先接受近似；如实际重复多再加强。
- 砍 HISTORY 后「压缩审计」能力消失：确认乖宝接受（已确认「用处不大」）；如后续想要可改为可配置开关。

## 下一步

（已全部完成）按实现方案逐步实现：先砍 HISTORY → 再做做梦整理函数 → 定时 → 补做 → 配置 → 文档。


## 实现进展（第一阶段：砍 HISTORY + 做梦整理函数）

> 记录时间：2026-08-05 ｜ 由 code-master 实现 ｜ 基线 commit `857b003`

### 已完成（对应任务卡「实现方案」第 1、2 步）

1. **砍 HISTORY（agent/memory.py）**
   - 删除 `ContextCompactor._save_to_history` 方法（原追加写入 `workspace/memory/HISTORY.md`）。
   - 移除 `maybe_compact` 内的调用点 `self._save_to_history(summary, len(old_messages))`。
   - 同步清理：模块 docstring（核心思路/数据落点）、`maybe_compact` docstring 中提及 HISTORY.md 的描述；
     清理因此不再使用的 `import os` 与 `from datetime import datetime`。
   - `grep -rn "HISTORY" agent/` 仅剩 2 处说明性注释（记录 HISTORY 已移除），无任何调用点。
   - **agent/loop.py 无需改动**：其 `_save_to_history(messages_snapshot)` 是「跨轮会话历史」持久化
     （写 session history / JSONL），与 HISTORY.md 无关；经核对 loop.py 压缩落盘路径无任何写
     HISTORY.md 的调用点。

2. **做梦整理函数（agent/daily.py，复用现有结构，未新建 agent/dream.py）**
   - 新增固定分类常量 `DREAM_CATEGORIES = ("用户变化", "项目进展", "会话总结")`（可配置，顺序即写入顺序）。
   - 新增 `DailyMemory._path_for(date_str)` / `read(date_str)`（支持任意日期读写，为第二阶段启动补做铺路）；
     `_path()` 改为委托 `_path_for(datetime.now()...)`，`append()`（/clear 路径）行为不变。
   - 新增 `DailyMemory.write_dream(date_str, sections, categories)`：固定结构（每个分类至多一个
     `## 标题`，不再无限追加）+ 行哈希去重（跨分类全局）+ 合并更新（已有内容保留、非固定分类原样保留在后）。
   - 新增模块函数：`_parse_daily_sections`（解析已有 daily）、`_fact_hash`（规范化行内容哈希，
     复用 `stable_text_hash`）、`_try_json_sections` / `_parse_dream_sections`（解析模型输出，JSON 优先、
     Markdown 小节目录兜底）。
   - 新增异步入口 `dream_consolidate(provider, daily, date_str, messages, categories, cache_turn)`：
     复用 daily.py 现有 Provider.chat 调用方式与 `_messages_to_text`；提示词含「不要重复已记录内容」
     指令（模型语义去重）+ 兜底行哈希；任何环节异常静默返回（nice-to-have，不阻塞主流程）。
   - `summarize_messages_to_daily`（/clear 路径）未改动，与做梦整理共存。

3. **测试**
   - 新增 `tests/test_daily_dream.py`（15 个用例）：固定结构写入、同一事实两次整理去重、单次输出内部
     去重、与已有内容合并且保留非固定分类、模型提示携带已有内容、Provider 异常/空响应静默、daily 为
     None / messages 为空不调模型、`/clear` 回归、JSON/代码块围栏/Markdown 兜底解析。
   - 修改 `tests/test_memory_cache_boundary.py`：`test_compaction_never_writes_daily` 中原断言
     「压缩后 HISTORY.md 存在」改为「HISTORY.md 不存在」（TASK-011 移除 HISTORY 的直接行为断言）。
     ⚠️ 该测试文件不在授权文件清单内，但断言的是本次被移除的行为，不改则全量单测必红；已最小改动
     并在本备注记录（见下「遗留问题」）。

### 验证结果（真实输出）

```
$ git diff --check                                   → 通过（无空白错误）
$ uv run python -m compileall -q agent              → 通过
$ uv run python -c "import main"                    → 通过
$ grep -rn "HISTORY" agent/                         → 仅 2 处说明性注释（memory.py:20 / daily.py:12），无调用点
$ .venv/bin/python -m unittest discover -s tests    → Ran 423 tests in 38.824s  OK（基线 408 + 新增 15）
```

### 遗留问题 / 备注（记录但不扩大实现）

- **定时调度 / 启动补做 / dream_time 配置 / 文档同步（PROJECT/DECISIONS/ARCHITECTURE/README）**：
  属「实现方案」第 3~6 步，本次任务范围只含第 1、2 步，留待第二阶段。
- **`tests/test_memory_cache_boundary.py` 越权说明**：授权文件清单不含测试文件，但该测试断言
  「压缩写 HISTORY.md」与本次移除目标直接冲突，不做最小修正则验收标准「全量单测全过」无法达成；
  已做最小改动（一行断言 + 注释），请验收方知悉。
- **`write_dream` 为「读-合并-写回」**：假定做梦时段（如 02:00）无并发 /clear 写入；若后续出现并发
  写 daily 场景，需加文件锁（当前单进程架构风险低，第二阶段可评估）。
- **去重近似性**：语义级去重依赖模型判断（提示词指令），行哈希兜底只能拦「规范化后完全一致」的重复；
  跨分类语义近似重复可能残留，符合任务卡「先接受近似」的约定。
- **`dream_consolidate` 的 `cache_turn` 记录用 `tool_iteration=-3`**（与 daily 的 -2、压缩的 -1 区分），
  如后续统一观测口径需一并调整。


## 实现进展（第二阶段：定时调度 + 启动补做 + dream_time 配置）

> 记录时间：2026-08-05 ｜ 由 code-master 实现 ｜ 基线 commit `857b003`（同第一阶段）
> 对应任务卡「实现方案」第 3、4、5 步；授权文件：main.py / config.py / config.json /
> config.example.json / agent/daily.py（小改接口）/ tests/ 新增单测。

### 已完成

1. **配置：`dream_time`（config.py / config.json / config.example.json）**
   - `NanoClawConfig` 新增 `dream_time: str = "02:00"`（HH:MM，实例时区）；`_CONFIG_FIELDS`
     同步加入，缺省/缺失不报错（与 `context_budget_tokens` 同款容错模式），非法值由调度器
     回退默认 02:00。
   - `config.json` 追加 `"dream_time": "02:00"`（仅此一个字段，其余不动；该文件在 .gitignore 内，
     属本机配置，改动不进入版本库）；`config.example.json` 在 timezone 后同步加字段。

2. **定时调度（main.py）**
   - 新增 `DreamScheduler`：仿照 `ReminderScheduler` 的「动态等待 + 可注入时钟」循环，独立
     asyncio 后台 task（name="dream-scheduler"），每个本地日到 `dream_time` 执行一次
     `consolidate_today()`（整理当天）；进程晚启动（已过到点）立即补跑当天一次。
   - 可测性：注入 `clock`（now() -> datetime）与 `wait`（event, timeout），单测用假时钟推进、
     假 wait 直接跳到到点时刻，无需真实 sleep。
   - 失败静默：`consolidate_today` 内部吞异常，`_safe_consolidate` 再兜底一层，异常绝不影响
     调度循环；不阻塞聊天/启动。
   - 优雅关闭：`stop()` 置停止标志 + 唤醒事件 + 取消 task，`amain()` 收尾段与 ReminderScheduler
     并列处理，`dream_scheduler_task` 加入 watched 与最终 gather；启动补做任务单独跟踪并取消。
   - 辅助函数：`_parse_dream_time`（HH:MM 解析 + 容错）、`_next_dream_run`（下一次到点时刻）、
     `_today_date`（实例时区今天）、`_as_aware`（naive 时钟按 UTC 归一）、`_dream_default_wait`。

3. **启动补做（main.py + 状态文件）**
   - 新增 `DreamState`：维护 `workspace/memory/dream_state.json`（`{"last_dream_date": "YYYY-MM-DD"}`，
     缩进 2，与任务卡建议格式一致）；缺失/损坏按「无记录」、写失败静默（运行时产物不阻断启动）；
     `write_last_dream_date` **只前进不后退**（已有更晚记录则跳过），消除「补做昨天」与「定时写今天」
     并发更新时把状态回退的竞态。
   - 新增 `should_catch_up(last_dream_date, today)`：首次无状态文件 → 补昨天一次；
     `last_dream_date < 昨天` → 只补最近 1 天（昨天），超期不回溯；已覆盖昨天 → 不补。
   - `amain()` 启动时 `asyncio.create_task(catch_up_yesterday())` 异步后台执行，不阻塞启动。
   - 定时到点整理当天后同样更新状态（避免下次启动重复补做昨天）；模型调用失败时**不**更新
     状态，保证下次启动可重试该失败日期。

4. **消息源接入（main.py）**
   - 新增 `collect_messages_for_date(session_manager, date_str)`：枚举所有会话 JSONL，取
     `timestamp` 命中该日期的消息（沿用 list_sessions + get_session_messages 路径，key 还原与
     `list_sessions_detailed` 一致——`stem.replace("_", ":")` 经 `_get_session_path` 恒映射回原文件，
     无丢失）；过滤系统内部记忆补丁/快照消息（`<memory_patch>/<memory_snapshot>`，非用户事件）。
   - 数据源=当天各会话关键消息 + 当天 daily 已有内容（`dream_consolidate` 内部读取并让模型去重）。
   - 新增 `run_dream_for_date(provider, daily, session_manager, dream_state, date_str)`：定时与补做
     共用的整理入口，失败静默。

5. **小改接口（agent/daily.py）**
   - `dream_consolidate` 返回值 `None -> bool`：`True`=本次整理已完成（含无事可做：daily 未启用、
     无消息、空内容、模型输出无新事实、成功写盘）；`False`=模型调用失败或返回 error/空内容
     （调用方不把该日期标记为已整理，下次启动补做可重试）。向后兼容：既有调用方忽略返回值。

6. **测试（tests/test_dream_scheduler.py，25 个用例）**
   - `DreamTimeConfigTests`：dream_time 默认值 / 配置文件覆盖 / 缺省回退不报错；
   - `DreamStateTests`：状态文件读写格式、缺失/损坏按无记录、写失败静默；
   - `ShouldCatchUpTests`：首次补昨天、已整理不重复补、超期只补最近 1 天、当天已标记不补；
   - `CollectMessagesTests`：按日期过滤 + 过滤记忆补丁/快照、空会话目录返回空；
   - `DreamSchedulerTests`：mock 时钟推进到点触发（test_dream_scheduled_runs_at_time）、晚启动
     立即补跑当天、实例时区换算（02:00 Asia/Shanghai=UTC 前一日 18:00）、等待中 stop 不执行、
     整理异常不影响调度循环、start/stop 生命周期、与 ReminderScheduler 独立 task 互不影响；
   - `CatchUpConsolidationTests`：补做写 daily + 更新状态、模型失败不更新状态、无消息不调模型
     但标记已整理、`dream_consolidate` 返回值契约。

### 验证结果（真实输出）

```
$ git diff --check                                              → 通过（无空白错误）
$ uv run python -m compileall -q agent                          → 通过
$ uv run python -m compileall -q main.py config.py              → 通过
$ uv run python -c "import main"                                → 通过
$ .venv/bin/python -m unittest discover -s tests                → Ran 448 tests in 39.044s  OK（基线 423 + 新增 25）
$ .venv/bin/python -m unittest tests.test_dream_scheduler -v    → Ran 25 tests  OK
$ .venv/bin/python -m unittest tests.test_daily_dream           → Ran 15 tests  OK（回归）
```

### 遗留问题 / 备注（记录但不扩大实现）

- **文档同步（PROJECT/DECISIONS/ARCHITECTURE/README）**：属任务卡「范围」第 6 步，本阶段授权文件
  不含它们；待后续阶段/验收时同步（做梦机制决策、daily.py docstring 已含做梦说明、ARCHITECTURE
  数据流可补 dreaming 节点）。
- **定时整理失败即标记当天已整理（schedule 路径）**：定时到点整理当天时，若模型调用失败
  （`dream_consolidate` 返回 False），调度器**不**更新状态；但当日不会自动重试——只有下次重启且
  「昨天未整理」时才补做。连续运行实例的某一天模型失败会被跳过（数据仍在会话 JSONL 与 daily，
  不丢，只是当天没出做梦整理块）。符合任务卡「失败静默 / nice-to-have」定位，已知可接受。
- **`collect_messages_for_date` 的 key 还原**：`stem.replace("_", ":")` 对含下划线的原始 key 是
  有损的，但经 `SessionManager._get_session_path`（":"→"_"）恒映射回同一文件路径（已推演验证），
  故读到的消息不丢；与既有 `list_sessions_detailed` 同款处理，未引入新问题。
- **`write_dream` 读-合并-写回**：第二阶段未引入文件锁（同第一阶段备注），做梦时段假定无并发
  /clear 写入；当前单进程架构风险低。
- **首次启动即补昨天**：无 dream_state.json 时按「补做一次昨天」处理；若昨天确实无任何会话消息，
  不调模型并直接标记已整理（避免每次启动重复尝试）。


## 实现进展（第三阶段：文档同步）

> 记录时间：2026-08-05 ｜ 由小奈同步 ｜ 对应任务卡「范围」第 6 步与验收标准「文档同步」项

### 已完成

1. **PROJECT.md**
   - 能力矩阵新增「每日做梦整理」行（`dream_time` 定时 + 固定分类 + 去重 + 启动补做 + 不再写 HISTORY.md）；
     「记忆体系」行描述同步（USER/MEMORY/daily，移除 HISTORY 提及）。
   - 主要模块表新增 `agent/daily.py` 行；`main.py` 行补充 DreamScheduler/DreamState 职责。
   - 配置速查新增 `dream_time` 字段说明（启动期配置，缺省回退默认值不报错）。
   - 「Git 状态（指针式）」里程碑更新：TASK-011 实现完成、待验收归档（归档后由验收人再更新为已归档）。
   - 消息流转简述补充「做梦链路」一行。

2. **README.md**
   - 特性列表新增「每日做梦整理」条目。
   - 目录结构补 `agent/daily.py`。
   - 配置说明表新增 `dream_time` 行（默认 `"02:00"`，重启后生效）。

3. **docs/DECISIONS.md**
   - 「压缩不再写 daily（TASK-006 决策）」行更新：补充 TASK-011 起压缩摘要也不再写 HISTORY.md，
     daily 触发点变为 `/clear` + 每日做梦整理。
   - 新增决策行：「HISTORY.md 移除（TASK-011 决策）」「每日做梦整理（TASK-011 决策）」。
   - 演进时间线新增 08-05 TASK-011 行。

4. **docs/ARCHITECTURE.md**
   - 总体结构图新增 DreamScheduler 节点；依赖约束补充 DreamScheduler 独立 task 说明。
   - §4 目录职责：memory.py（不再写 HISTORY.md）、daily.py（每日记忆：/clear 摘要 + 做梦整理）注释更新。
   - §5.1 启动与装配第 6 步补充 DreamScheduler 与启动补做。
   - §5.4 持久化和记忆：workspace 树补 `dream_state.json`、HISTORY.md 标注不再写入；说明行更新；
     新增「每日做梦整理（TASK-011）」小节描述数据流。
   - §6 配置生效边界补 `dream_time`。

5. **agent/daily.py docstring**：已在第一阶段由 code-master 同步（模块头含做梦整理触发时机与固定分类说明）。

### 验证结果（真实输出，2026-08-05 第三阶段后全量）

```
$ git diff --check                                   → 通过（无空白错误）
$ uv run python -m compileall -q agent bus channels providers session main.py config.py → 通过
$ uv run python -c "import main"                     → 通过
$ grep -rn "HISTORY" agent/                          → 仅 1 处说明性 docstring（daily.py:12），无调用点
$ .venv/bin/python -m unittest discover -s tests     → Ran 448 tests in 39.204s  OK
```

## 实现摘要（验收归档，2026-08-05）

### 改动文件清单

| 文件 | 状态 | 说明 |
|---|---|---|
| `agent/memory.py` | 修改 | 删除 `_save_to_history` + 调用点 + docstring/import 清理 |
| `agent/daily.py` | 修改 | 新增 `dream_consolidate`/`DailyMemory.write_dream`/`read`/`_path_for` 等；docstring 更新 |
| `main.py` | 修改 | 新增 `DreamScheduler`/`DreamState`/`collect_messages_for_date`/`run_dream_for_date`；amain 装配与生命周期 |
| `config.py` | 修改 | 新增 `dream_time` 字段（默认 "02:00"，容错模式） |
| `config.json` | 修改（本机，gitignored） | 追加 `dream_time` 一个字段 |
| `config.example.json` | 修改 | 同步加 `dream_time` |
| `tests/test_daily_dream.py` | 新增 | 15 例 |
| `tests/test_dream_scheduler.py` | 新增 | 25 例 |
| `tests/test_memory_cache_boundary.py` | 修改（最小） | 断言 HISTORY.md 不再存在 |
| `docs/PROJECT.md` / `README.md` / `docs/DECISIONS.md` / `docs/ARCHITECTURE.md` | 修改 | 文档同步 |
| `docs/tasks/active/TASK-011-…md` | 修改 → 归档 | 本任务卡 |

### 关键决策

- 砍 HISTORY：压缩摘要不再落 HISTORY.md（乖宝拍板「用处不大」），旧文件保留不清理。
- 做梦 = 每天 `dream_time` 定时整理当天 → 固定分类（用户变化/项目进展/会话总结）合并更新 daily，
  行哈希去重 + 模型语义去重指令兜底。
- 定时没跑则下次启动补做前一天（`dream_state.json` 记 `last_dream_date`，只补最近 1 天、状态单调前进、
  模型失败不标记可重试）。
- 整理异步执行、失败静默（nice-to-have），独立 asyncio task 与 ReminderScheduler 并存互不影响。
- `/clear` 写 daily 行为不变，与做梦整理共存。

### 验证结果

- `.venv/bin/python -m unittest discover -s tests` → **Ran 448 tests OK**（基线 408 + 新增 40）
- `git diff --check` / `compileall` / `import main` 全部通过
- `grep -rn "HISTORY" agent/` → 仅 1 处说明性 docstring，无调用点

### 遗留问题（已知，不阻塞验收）

1. **定时整理失败当日不自动重试**：模型失败那天会被跳过，仅下次重启且昨天未整理时补做；数据不丢（仍在会话/daily）。
2. **`collect_messages_for_date` key 还原有损**：`stem.replace("_", ":")`，但恒映射回同一文件，不丢消息；与既有处理一致。
3. **`write_dream` 读-合并-写回无文件锁**：做梦时段假定无并发 /clear；单进程风险低。
4. **去重近似性**：语义去重靠模型，行哈希兜底只能拦规范化后完全一致的重复。
5. **实机观察**：定时触发与启动补做已单测覆盖，真实运行效果（daily 出现整理块、日志）待部署后观察。
