# TASK-015-记忆小尾巴修复

> 状态：已完成（2026-08-06 乖宝验收；摘要抽查暂不执行，脚本保留在 scripts/，git 不跟踪）
> 创建：2026-08-06 ｜ 负责人：code-master ｜ 基线 commit：fbd5dd9

## 目标
修掉记忆体系的三个遗留小尾巴（压缩改写时间戳、write_dream 无文件锁、定时整理失败当日不自动重试），并补一次真实模型摘要质量抽查验证。

## 背景
TASK-006~014 记忆体系已全部验收归档，但 DECISIONS.md 遗留清单里还剩几个记忆相关的边角问题：
- **NC-MEM-002**：压缩触发后 `save_messages` 整段覆盖写回，`session/manager.py` 每条消息重新写 `timestamp = datetime.now()`，导致会话文件里所有消息时间戳变成压缩时刻（内容不受影响，TASK-010 已保证内存=磁盘一致；风险仅在时间戳真实性，未来依赖时间戳审计时会有问题）。
- **TASK-011 遗留**：`DailyMemory.write_dream` 是「读-合并-写回」，无文件锁，假定做梦时段（02:00）无并发 `/clear` 写入；单进程风险低但属竞态隐患。
- **TASK-011 遗留**：做梦整理失败静默，`run_dream_for_date` 返回 False 后当日不自动重试，只能等下次启动补做（停机场景才会补）。
- **TASK-009 遗留**：分块结构化摘要（map-reduce）用真实模型的质量从未人工抽查过（当前无真实链路验证）。

乖宝 2026-08-06 拍板：把记忆小尾巴修掉。

## 范围
1. **NC-MEM-002**：`session/manager.py::save_messages` 增加保留原始 timestamp 的能力（已有 timestamp 的消息不覆盖，缺失才补当前时间）；`agent/loop.py` 压缩写回时使用保留模式。检查其它调用点（loop.py:677 取消补历史、spawn.py:65 子 Agent 落盘）语义是否需要变。
2. **write_dream 文件锁**：`agent/daily.py::DailyMemory.write_dream` 增加进程内写锁（threading.Lock 或 asyncio 锁，按项目现有风格），或原子写（临时文件+rename），防止与 `/clear` 的 append 并发丢数据。评估 append（/clear 场景）是否也需要同步保护。
3. **定时整理失败重试**：`main.py` DreamScheduler / run_dream_for_date 失败后当日自动重试（如 30 分钟后重试，有限次数，避免无限循环），`last_dream_date` 仍只推进到真正完成。
4. **摘要质量抽查**：写一个可复跑的抽查脚本（用真实模型跑 1~2 个样例会话的分块结构化摘要），产出质量报告供乖宝人工确认；**不自动跑真实 API**（花 token），脚本就绪后等乖宝确认再执行。

## 非目标
- 不做 NC-MEM-001（记忆软规则机制化——Cue/14 天冷却/Follow Up 依赖模型自律，大改）。
- 不做首启全扫描性能优化（TASK-013 遗留，当前数据量可接受）。
- 不处理时区假设（当前部署 CST==Asia/Shanghai 自洽）。
- 不做重启当天重复整理的安全冗余消除（TASK-013 明确属安全方向）。
- 不做 write_dream 的「临时文件 + os.replace」原子写升级（本任务用进程内锁满足验收；原子写留作后续可选加固）。

## 验收标准
- [x] `save_messages` 保留模式：已有 timestamp 的消息写回后 timestamp 不变；无 timestamp 的消息补当前时间
- [x] 压缩写回走保留模式；取消补历史、子 Agent 落盘的语义不变（新增测试覆盖）
- [x] `write_dream` 并发调用不丢数据（新增并发/顺序测试），append 竞态同步方案确定
- [x] 做梦整理失败后当日自动重试（可注入时钟/可控次数的测试），`last_dream_date` 不提前推进
- [x] 抽查脚本就绪（`scripts/` 下，可复跑，输出质量报告），待乖宝确认后执行
- [x] 全部测试通过：`.venv/bin/python -m unittest discover -s tests`（484 个，含新增 15 个）
- [ ] 文档同步：DECISIONS.md 遗留清单更新、PROJECT.md 能力矩阵/里程碑、MEMORY.md 指针、任务卡归档（本卡已更新，MEMORY.md 指针待归档时同步——`.workbuddy/` 不在当前工作区、已被 gitignore）

## 相关模块
- `session/manager.py`（save_messages 时间戳）
- `agent/loop.py`（压缩写回调用点、取消补历史）
- `agent/tools/spawn.py`（子 Agent 落盘调用点）
- `agent/daily.py`（write_dream / append 并发）
- `main.py`（DreamScheduler 重试逻辑）
- `tests/`（新增测试）

## 实现方案
1. `save_messages` 增加 `preserve_timestamps: bool = False` 参数；`record["timestamp"]` 仅在缺失或未保留时写入当前时间。
2. `write_dream` 增加模块级/实例级写锁（线程锁即可，asyncio 单事件循环内串行）；`append` 评估是否同锁；或改为原子写。
3. DreamScheduler：失败重试用「下一次计划时间 + 重试间隔」或独立重试循环；重试次数上限（如 3 次）、间隔（如 30 分钟），全部失败后当日放弃（下次启动补做兜底）；`last_dream_date` 推进逻辑不变。
4. 抽查脚本 `scripts/dream_summary_spotcheck.py`：读指定会话 JSONL → 调真实 provider 跑 `ContextCompactor` 分块摘要 → 输出摘要文本+统计到报告文件。
5. 测试：新增 `tests/test_save_messages_timestamps.py`、`tests/test_dream_retry.py`、write_dream 并发测试（在现有 daily 测试中加）。

### 实际实现与任务卡的差异（如实记录）
1. **NC-MEM-002 实际不止「压缩写回」一处**：TASK-007 规定压缩后 `_sync_memory_patch` **无条件执行 `_rebuild_memory_snapshot`**（压缩→重建完整快照），其内部 `save_messages`（默认模式）会在压缩写回后**同一回合内**立刻把时间戳再改写掉。仅改压缩写回无法通过验收（集成测试证明）。因此 `_rebuild_memory_snapshot` 的覆盖写回**也必须走保留模式**——这是同一 NC-MEM-002 缺陷在压缩链路下一环的必然延伸，属本任务必要范围（任务卡原「只改压缩场景」措辞据此修订）。
2. **任务卡对 loop.py:677 的标注有误**：该行实为 `_rebuild_memory_snapshot`（快照重建），**不是**「取消补历史」。取消补历史走 `_record_cancelled_turn` → `_persist` → `save_message`（单条追加），不经过 `save_messages`，天然不受影响。
3. **`save_messages` 保留模式扩展为「可从原文件找回原始时间戳」**：压缩/快照重建的输入消息本身不带 timestamp（`get_history`/canonicalize 会剥离），仅靠「输入自带 timestamp」无法找回。保留模式现按「规范化身份」依次取：输入消息自带 timestamp → 原文件同身份消息的 timestamp → 补当前时间。这是 NC-MEM-002 修复的关键，已在 docstring 与测试中明确。
4. **`scripts/` 目录被 `.gitignore` 忽略**：抽查脚本按要求落在 `scripts/dream_summary_spotcheck.py`，但 git 不跟踪（与现有 `run_resident.sh`/`send2wsl.sh` 一致，项目既有约定）。

### 范围外发现（记入本卡，不顺手改）
- `_rebuild_memory_snapshot` 在**非压缩**路径（累积补丁过多）触发时，历史里除快照外的消息按「身份与旧文件匹配」保留时间戳；若旧文件内容与 `_session_history` 长期不一致（如磁盘写失败静默），可能匹配到同内容但不同时间戳的消息——属罕见边缘，不影响内容正确性。
- write_dream 采用进程内锁而非「临时文件 + os.replace」原子写：崩溃中途仍可能留下半截 daily 文件（`os.replace` 可避免）。当前 single-writer + 锁已满足验收；如需更强持久性可后续升级为原子写。
- `DummySessionManager.save_messages` 签名已扩展以兼容 `preserve_timestamps`；子 Agent 仍完全不落盘（no-op，语义不变）。

## 测试方式
- `.venv/bin/python -m unittest discover -s tests`（484 个全部通过）
- `uv run python -m compileall -q agent bus channels providers session`（通过）
- `uv run python -c "import main"`（通过）
- `git diff --check`（通过）

## 风险
- 真实模型摘要抽查会花 token，脚本就绪后必须等乖宝确认再执行。
- 重试逻辑若实现不当可能造成重复整理；用 `_done_this_run` + 锁去重。
- `save_messages` 三个调用点语义不同，改动需逐个核对测试。

## 下一步
- 等乖宝确认后运行 `scripts/dream_summary_spotcheck.py`（先 `--dry-run` 验证输入，再真实执行抽查样例会话）并人工确认摘要质量。
- 归档本任务卡到 `docs/tasks/completed/`，同步 DECISIONS.md 遗留清单、PROJECT.md 能力矩阵与里程碑、MEMORY.md 指针（`.workbuddy/` 在归档环境补齐）。
