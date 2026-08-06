# TASK-014：单进程多日睡眠唤醒后补做梦整理（睡眠唤醒补做）

> 状态：待开工
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
- ❌ 不处理 `write_dream` 文件锁（遗留 NC-MEM-002，另行处理）。
- ❌ 不改变 daily 文件结构/固定分类/去重机制。

## 验收标准

- [ ] mock 时钟：进程 8-9 起运行、睡眠到 8-13 唤醒（同一 scheduler 实例，时钟直接跳到 8-13）→ 触发一次补做，目标为「最后一个有消息的日期」（如 8-9），不是 8-12。
- [ ] 正常每日连续运行不受影响：每天到点仍只整理昨天一次，不额外补做（跨日 == 1 不触发补做）。
- [ ] 与定时到点/重启补做共用去重（`_done_this_run` + 锁），不重复整理同一日期。
- [ ] 无消息日期不推进 `last_dream_date`（沿用 TASK-013 契约）。
- [ ] 测试：`.venv/bin/python -m unittest discover -s tests` 全过（含更新后旧用例）。
- [ ] 验证：`python -m compileall main.py` 无语法错误；`import main` 正常。

## 相关模块

- `main.py`：DreamScheduler.run（唤醒路径）、build_dream_components（补做回调注入）、find_last_active_date（复用）
- `tests/test_dream_scheduler.py`：单测

## 实现方案

1. `build_dream_components` 把 `catch_up_yesterday` 也暴露给 `DreamScheduler`（或注入一个「唤醒回调」）。
2. `DreamScheduler.run()` 在跨日唤醒分支：计算跨日天数，>1 时在 `consolidate_today` 之外再调一次补做（可先 consolidate 后 catch_up，或先 catch_up 后 consolidate，以「最后有消息日期」去重集合兜底不重复）。
3. 具体细节由 code-master 定，以验收标准为准。

## 测试方式

- 单测：mock 时钟 + 假 wait，模拟「运行→睡眠→唤醒」循环，断言唤醒后补做目标与次数。
- 冒烟：compileall + import main。

## 风险

- `DreamScheduler.run()` 当前逻辑按「每天到点执行一次」设计，加唤醒补做分支需小心不破坏正常到点路径（区分跨日 == 1 与 >1）。
- 与 TASK-013 的 `_done_this_run` 集合交互：补做回调需走同一去重入口，避免与定时整理重复调模型。

## 下一步

- 乖宝确认「开始 TASK-014」后：派 code-master 实现 → 按「完成任务」清单走（验收标准逐项、测试真实结果、文档同步、归档、提交前确认）。
