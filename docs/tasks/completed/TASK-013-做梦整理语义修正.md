# TASK-013：每日做梦整理语义修正（定时整理昨天 + 补做最后有消息日期 + 竞态修复）

> 状态：已完成（2026-08-06 验收归档）
> 创建：2026-08-06 ｜ 负责人：code-master（实现）＋ 小奈（设计/验收）
> 基线 commit：以当前 main 最新提交为准（git log 核对）

## 目标

修正每日做梦整理的两处语义缺陷：**定时到点整理「前一天完整 24h」**（8-10 02:00 → 整理 8-9），**启动补做目标改为「最后一个有消息的日期」**（停机多天后补真正有内容的那天），并修复「当天补跑顶掉昨天补做」的竞态，保证**任何情况下每天内容都不漏整理、不做无意义整理**。

## 背景

2026-08-06 发现 08-05 全天 1356 条消息从未被做梦整理（daily 只有 /clear 的「压缩前保存」，无「用户变化/项目进展/会话总结」分类），手动补做才捞回。根因（对照 TASK-011 实现）：

1. **定时到点整理「当天」而非「昨天」**：`DreamScheduler` 到点调 `consolidate_today()` → `run_dream(_today_date(...))`。8-10 02:00 时整理的是 8-10 当天 00:00~02:00（基本空转），却把 `last_dream_date` 推进到 8-10；8-9 白天的消息在 8-9 02:00 定时时尚未发生、在 8-10 02:00 定时时又没人整理 → **昨天全天内容系统性悬空**（除非当天 /clear 或重启补做）。
2. **启动补做只补「机械的昨天」**：`should_catch_up` 为 `last < 昨天 → 补昨天`。停机多天场景（8-9 停机 → 8-13 启动），8-10/11/12 无会话无消息，补 8-12 没有意义，还把 last 推进到 8-12，让 8-9 内容永久漏掉。**应往前找「最后一个有消息的日期」（8-9）补它，就补一天**。
3. **晚启动竞态**：进程晚启动（已过到点）时「当天补跑」先把 last 推进到当天（当天无消息也算完成），随后 `catch_up_yesterday` 看到 `last ≥ 昨天` 即跳过 → 昨天补做被吞（今天早上 08-05 正是被此吞掉）。

乖宝 2026-08-06 确认正确语义：
- **定时做梦 = 整理「上次做梦以来的完整一天」**：8-10 02:00 应整理 8-9 02:00 ~ 8-10 02:00 这 24 小时的内容（即昨天全天，含凌晨尾巴）。
- **启动补做 = 补「最后一个有消息的日期」**：停机多天只补有消息的那一天（如 8-9），无消息的日期不做无用功、也不推进状态。

## 范围

- `main.py`：
  - `DreamScheduler` 定时语义：`consolidate_today()` 目标日期改为「昨天」（today−1，收集昨天全天消息）。
  - `catch_up_yesterday` / `should_catch_up`：改为「从昨天往前找最后一个有消息的日期」；无消息则不做、不推进。
  - 竞态修复：启动补做与当天补跑目标计算统一，杜绝「当天补跑先把 last 推进到当天 → 昨天补做被跳过」。
  - `collect_messages_for_date` 复用（如需按窗口收集 24h 则调整参数）；可能需要新增「找最后有消息日期」的辅助函数（复用现有 memory_patch/snapshot 过滤）。
  - `last_dream_date` 语义：只推进到**真正整理过的日期**；无消息日期不推进。
- `agent/daily.py`：`dream_consolidate` 已是「date_str + messages」通用接口，预计无需大改；如需跨日窗口参数则小改。
- `tests/test_dream_scheduler.py`、`tests/test_daily_dream.py`：更新旧语义用例 + 新增用例（见验收标准）。
- 文档同步：`PROJECT.md` 能力矩阵「每日做梦整理」行描述更新；`docs/DECISIONS.md` 记录语义决策；MEMORY 指针更新（TASK-011 归档描述中「补做前一天」表述修正）。

## 非目标

- ❌ 不做「补做所有缺失日期」的全量回溯（乖宝确认只补最后一个有消息的日期，就一天）。
- ❌ 不改 reminders 调度器（独立体系，互不影响）。
- ❌ 不做 `write_dream` 文件锁（TASK-011 遗留未编号项「`write_dream` 读-合并-写回无文件锁」，另行处理；**NC-MEM-002 是「压缩改写会话文件时间戳」，与此无关，勿混淆**）。
- ❌ 不改变 daily 文件结构/固定分类/去重机制。

## 验收标准

- [ ] 定时到点语义：mock 时钟推进到 8-10 02:00 → consolidate 收到目标日期为「2026-08-09」（昨天全天）。
- [ ] 正常每日连续运行：8-10 02:00 整理 8-9、8-11 02:00 整理 8-10（昨天语义连续，不重不漏，去重生效）。
- [ ] 停机多天场景：8-9 有消息、8-10/11/12 无消息 → 8-13 启动 → 补做目标为「2026-08-09」，不是 8-12。
- [ ] 晚启动竞态修复：进程 08-06 09:25 启动（last=08-05、08-05 有消息未整理）→ 补做 08-05 成功，不被「当天补跑」顶掉。
- [ ] 无消息日期不推进 `last_dream_date`（或与「只推进到真正整理过的日期」语义一致）。
- [ ] `last_dream_date` 只前进不后退（保留现有防回退），补做/定时并发更新不产生竞态。
- [ ] 测试：`.venv/bin/python -m unittest discover -s tests` 全过（含更新后旧用例）。
- [ ] 验证：`python -m compileall main.py agent/daily.py` 无语法错误；`import main` 正常。

## 相关模块

- `main.py`：DreamScheduler / DreamState / should_catch_up / collect_messages_for_date / run_dream_for_date（组合根与做梦调度）
- `agent/daily.py`：dream_consolidate / DailyMemory.write_dream（整理函数）
- `tests/test_dream_scheduler.py`、`tests/test_daily_dream.py`：单测

## 实现方案

1. **定时做梦**：`DreamScheduler` 到点执行 `run_dream(today−1)`（昨天全天消息）。daily 已有「模型语义去重 + 行哈希兜底」，8-9 00:00~02:00 与 8-9 02:00~8-10 02:00 的重叠不会重复落盘。
2. **启动补做**：新增逻辑——从昨天往前逐日调用 `collect_messages_for_date`（复用 memory_patch/snapshot 过滤），找到「有消息且日期 > last_dream_date」的第一个日期即补做它，只补一天；找不到则不补、不推进 last。
3. **竞态修复**：把「启动补跑当天」与「补做昨天」合并为同一个目标计算（以「最后有消息日期」为准），从根上消除「当天补跑先推进 last → catch_up 跳过昨天」的路径。具体实现细节由 code-master 定，以验收标准为准。
4. **状态语义**：`last_dream_date` 只记录「真正整理完成的日期」；模型调用失败（dream_consolidate 返回 False）不推进（沿用现有契约），保证下次可重试。
5. **文档同步**：PROJECT.md 能力矩阵「每日做梦整理」行改为「定时整理前一天 24h + 启动补做最后有消息日期」；DECISIONS 记语义决策与 08-06 教训。

## 测试方式

- 单测：`.venv/bin/python -m unittest discover -s tests`（更新旧语义用例 + 新增验收标准对应用例，覆盖：定时整理昨天、停机多天补最后有消息日、无消息不推进、晚启动竞态修复、只前进不后退）。
- 冒烟：`python -m compileall main.py agent/daily.py`、`python -c "import main"`。

## 风险

- 现有 `test_dream_scheduler.py` 部分用例基于旧语义（should_catch_up 只补昨天、定时整理当天），需同步更新预期，否则测试失败是预期的。
- 「找最后有消息日期」需枚举所有会话 JSONL：停机多天后消息量大，首次扫描可能慢（启动一次性、可接受；可考虑按修改时间倒序提前剪枝）。
- 与 TASK-011 已归档验收结论冲突处，以本任务卡为准（TASK-011 验收的是旧语义，本卡为语义修正）。

## 验收结论（2026-08-06）

- **验收标准 8 条逐条通过**（code-review 独立核对，证据见下）：
  1. ✅ 定时到点语义：mock 时钟 8-10 02:00 → consolidate 目标 2026-08-09（`test_scheduled_targets_yesterday`）
  2. ✅ 正常每日连续运行：8-10 整理 8-9、8-11 整理 8-10（`test_consecutive_days_no_skip_no_dup`）
  3. ✅ 停机多天场景：8-9 有消息、8-10~12 无 → 8-13 启动补 8-9（`test_multi_day_downtime_targets_last_active_date` + `test_stale_catch_up_targets_last_active_date`）
  4. ✅ 晚启动竞态修复：08-06 09:25 启动（last=08-05、08-05 未整理）→ 补做 08-05 不被顶掉（`test_late_start_recovers_yesterday_not_today` + `test_legacy_last_marked_today_still_recovers_yesterday`）
  5. ✅ 无消息日期不推进 last（`test_no_messages_does_not_advance_state` + `test_no_messages_does_not_advance_in_pipeline`）
  6. ✅ last 只前进不后退、并发无竞态（`test_write_is_monotonic` + `test_late_start_no_double_consolidation` + `test_state_never_regresses_in_pipeline`）
  7. ✅ 全量测试：Ran 458 tests, OK
  8. ✅ compileall main.py agent/daily.py 无语法错误；import main 正常
- **code-review**：无 P0；P1 文档同步（已补齐）；P2 过时注释（已修）。
- **遗留/候选**：单进程多日睡眠唤醒不触发 catch_up → 已立项 **TASK-014**（任务卡 `docs/tasks/active/TASK-014-睡眠唤醒补做梦.md`）；其余 P2（重启当天重复整理昨天 1 次属安全冗余、首启全扫描性能、时区假设、命名措辞、测试代理弱化）留档不改，见本卡「实现进展」段。

## 实现进展（2026-08-06）

- **实现完成**：main.py 做梦区段重构（`find_last_active_date` 替代 `should_catch_up`、`collect_messages_for_date` 增加 mtime 剪枝与 stop_at_first、`run_dream_for_date` 无消息早退不推进、新增 `DreamComponents`/`build_dream_components` 统一组装 + `_done_this_run` 去重集合 + 串行锁、`consolidate_today` 目标改为昨天、amain 改用新组装）；agent/daily.py 未改。
- **测试**：`tests/test_dream_scheduler.py` 旧用例更新 + 新增 FindLastActiveDateTests / ScheduledTargets / Pipeline 用例；`tests/test_daily_dream.py` 两处用例目标日期改为本地今天。全量 `.venv/bin/python -m unittest discover -s tests` = **458 tests OK**；`compileall main.py agent/daily.py` OK；`import main` OK；`git diff --check` OK。
- **code-review**（双轴，独立审查）：结论「有条件通过」——8 条验收标准逐条通过，无 P0；P1=文档同步（本次已补齐）；P2 若干（过时注释已修、重启当天重复整理昨天 1 次属安全冗余、首启全扫描性能、单进程多日睡眠不触发 catch_up、时区假设、命名措辞、测试代理弱化、多进程边界）。
- **文档同步**：PROJECT.md 能力矩阵行更新；DECISIONS.md 新增 TASK-013 决策行 + 历史记录；MEMORY 指针同步（另行更新）。

## 下一步

- 乖宝确认「开始 TASK-013」后：派 code-master 实现 → 按「完成任务」清单走（验收标准逐项、测试真实结果、文档同步、归档、提交前确认）。
