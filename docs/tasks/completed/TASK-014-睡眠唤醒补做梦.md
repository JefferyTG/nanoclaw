# TASK-014：单进程多日睡眠唤醒后补做梦整理（睡眠唤醒补做）

> 状态：✅ 已完成（2026-08-06 验收归档）
> 创建：2026-08-06 ｜ 负责人：code-master（实现）＋ 小奈（设计/验收）
> 基线 commit：以当前 main 最新提交为准（git log 核对）
> 来源：TASK-013 code-review P2-#4 记录（乖宝确认立项）

## 目标

修复「单进程多日睡眠/挂起唤醒后，跨越多天的内容不被做梦整理」的缺口：机器 8-9 睡眠到 8-13 唤醒（同一进程未重启），唤醒后应补做「最后一个有消息的日期」（如 8-9），保证多日停机变体也不漏内容。

## 背景

TASK-013 已修复「重启」路径的补做语义（启动时 `catch_up_yesterday` 从昨天往前找最后有消息日期，只补一天）。但还存在一条 TASK-011 遗留的结构性缺口：

- **启动补做只在 `amain` 启动时创建**（`asyncio.create_task(catch_up_yesterday())`）。
- `DreamScheduler.run()` 的唤醒路径**只调 `consolidate_today`**（整理昨天）：进程睡眠跨多天后唤醒，`last_date != local_today` 成立，执行 `consolidate_today`（目标 = 昨天，如 8-12）→ 8-12 无消息 → 早退不推进 last；8-9 有消息的内容**永远不会被整理**——因为 catch_up 只在启动时跑过一次，睡眠唤醒不触发。

与「重启」场景的区别：重启会重新走 `amain` → catch_up 触发；睡眠唤醒（进程活着）不重新走启动逻辑，故漏。

## 范围

- `main.py`：
  - `DreamScheduler.run()`：在「跨日唤醒」（`last_date != local_today` 且不是正常到点路径）时，额外触发一次「补做最后有消息日期」逻辑（复用 TASK-013 的 `find_last_active_date` / 去重锁，不重复实现）。
  - 注意与定时到点路径的区分：正常每天到点只整理昨天；跨日 >1 的唤醒才触发补做。
- `tests/test_dream_scheduler.py`：新增「睡眠跨多天唤醒 → 补做最后有消息日期」用例。
- 文档同步：PROJECT.md 能力矩阵「每日做梦整理」行补「睡眠唤醒补做」描述；DECISIONS 记录；MEMORY 指针。

## 非目标

- ❌ 不做「补做所有缺失日期」的全量回溯（沿用 TASK-013 决策：只补最后一个有消息的日期）。
- ❌ 不改 reminders 调度器。
- ❌ 不处理 `write_dream` 文件锁（TASK-011 遗留未编号项，另行处理；NC-MEM-002 是「压缩改写会话文件时间戳」，与此无关）。
- ❌ 不改变 daily 文件结构/固定分类/去重机制。

## 验收标准

- [x] mock 时钟：进程 8-9 起运行、睡眠到 8-13 唤醒（同一 scheduler 实例，时钟直接跳到 8-13）→ 触发一次补做，目标为「最后一个有消息的日期」（如 8-9），不是 8-12。
  - 实现：`test_wake_after_multi_day_sleep_catches_up_last_active`（管线级，断言 daily 8-9 写入、8-12 为空、last=8-9）
- [x] 正常每日连续运行不受影响：每天到点仍只整理昨天一次，不额外补做（跨日 == 1 不触发补做）。
  - 实现：`test_daily_cross_day_does_not_trigger_wake_catch_up`
- [x] 与定时到点/重启补做共用去重（`_done_this_run` + 锁），不重复整理同一日期。
  - 实现：`test_wake_catch_up_shares_dedup_with_startup`（模型只调 1 次）
- [x] 无消息日期不推进 `last_dream_date`（沿用 TASK-013 契约）。
  - 实现：管线级用例断言 8-12 无消息，last 只到 8-9
- [x] 测试：`.venv/bin/python -m unittest discover -s tests` 全过（含更新后旧用例）。
  - 实测：test_dream_scheduler 41 个全过；全量 469 个全过
- [x] 验证：`python -m compileall main.py` 无语法错误；`import main` 正常。
  - 实测：compileall -q main.py tests/test_dream_scheduler.py 通过；`uv run python -c "import main"` 正常；git diff --check 通过

## 相关模块

- `main.py`：DreamScheduler.run（唤醒路径）、build_dream_components（补做回调注入）、find_last_active_date（复用）
- `tests/test_dream_scheduler.py`：单测

## 实现方案（code-master 落地 2026-08-06）

1. `DreamScheduler.__init__` 新增可选参数 `on_wake_catch_up=None`（默认 None，不注入时行为与 TASK-013 完全一致，向后兼容）。
2. `DreamScheduler.run()` 跨日分支：`wake_gap = (local_today - last_date).days`（`last_date is None` 记为 0，首次启动不触发——启动 catch_up task 已覆盖）；先 `_safe_consolidate()` 整理昨天，仅当 `on_wake_catch_up 非 None 且 wake_gap > 1` 时再调 `_safe_wake_catch_up()`（新增，try/except 静默，与 _safe_consolidate 同款容错）；最后 `last_date = local_today`。
3. `build_dream_components` 注入 `on_wake_catch_up=catch_up_yesterday`——唤醒补做即启动补做协程本身，天然复用 `find_last_active_date` + `_done_this_run` + 串行锁去重，未动既有逻辑。
4. 触发顺序：先 consolidate 后 catch_up。理由：先整理昨天会把 last 推进（若昨天有消息），随后 find_last_active_date 扫描区间（last < D < today）自动排除昨天，语义更干净。
5. 测试新增 6 个用例：DreamSchedulerTests 4 个（多日唤醒触发一次/跨日==1 不触发/回调异常被吞/未注入时向后兼容）+ DreamPipelineTests 2 个（真实管线 8-9→8-13 补做 8-9 非 8-12、与启动补做共用去重）。

## 测试方式

- 单测：mock 时钟 + 假 wait，模拟「运行→睡眠→唤醒」循环，断言唤醒后补做目标与次数。
- 冒烟：compileall + import main。

## 风险

- `DreamScheduler.run()` 当前逻辑按「每天到点执行一次」设计，加唤醒补做分支需小心不破坏正常到点路径（区分跨日 == 1 与 >1）。→ 已用单测覆盖。
- 与 TASK-013 的 `_done_this_run` 集合交互：补做回调需走同一去重入口，避免与定时整理重复调模型。→ 注入的正是 catch_up_yesterday 闭包，天然共用。

## 已知风险（低）

- 真实睡眠/挂起下的时钟行为未做端到端验证（测试仅 mock 时钟验证逻辑；系统休眠期间 asyncio.wait_for 挂起/恢复行为未验证）——这正是注入时钟可测性的价值，逻辑层已覆盖。
- 反复多日唤醒时 catch_up_yesterday 会重扫日期区间（find_last_active_date 倒序扫描），但 _done_this_run 保证不重复调模型/写盘，仅多一次只读扫描，可接受。

## 实现摘要（2026-08-06 归档）

- 改动文件：`main.py`（+31 行）、`tests/test_dream_scheduler.py`（+203 行，新增 6 用例）；仅这两个文件。
- 关键决策：`on_wake_catch_up` 可选回调注入 DreamScheduler（默认 None 向后兼容）；跨日 >1 天（last_date 非 None）才触发；先 consolidate 后 catch_up（昨天有消息时 last 先推进，find_last_active_date 区间自动排除昨天）；注入的正是 catch_up_yesterday 闭包，天然复用 `_done_this_run` + 锁去重；`_safe_wake_catch_up` 同款容错不杀循环。
- 验证真实结果：`test_dream_scheduler.py` 41 用例 OK；全量 `.venv/bin/python -m unittest discover -s tests` = **Ran 469 tests in 40.023s OK**；`compileall -q main.py tests/test_dream_scheduler.py` 通过；`uv run python -c "import main"` 正常；`git diff --check` 无空白错误。
- 遗留/风险：真实系统休眠时 asyncio 挂起/恢复行为未端到端验证（mock 时钟已覆盖逻辑层）；反复唤醒多一次只读日期扫描（_done_this_run 保证不重复调模型/写盘）；write_dream 文件锁属 TASK-011 遗留未编号项，另行处理（非 NC-MEM-002）。


## 下一步

- 乖宝验收：跑全量测试确认 → 按「完成任务」清单走（验收标准逐项、测试真实结果、文档同步 PROJECT.md/DECISIONS/MEMORY、归档、提交前确认）。
