# TASK-007：压缩触发 → 无条件重建记忆快照（修洞）

## 任务卡

- 状态：**实现完成，待乖宝验收**（2026-08-05 code-master 完成实现与验证）
- 创建：2026-08-05 ｜ 负责人：code-master（实现）＋ 小奈（设计/验收）
- 基线 commit / 分支：`783cb60`（main）
- 依赖任务：TASK-006（需基于 `CompactionResult` 返回值判断本轮是否压缩过）

## 目标

**压缩发生 = 前缀缓存已破 = 直接把最新完整记忆快照更新进上下文**，无论是否有落后期间的记忆变更。

修复现有「压缩会吞掉历史里记忆补丁/快照消息，但重建条件依赖 entries 非空」的洞：

> 当记忆是**当前会话自己刚写的**（revision 已刷新到最新），触发压缩后，历史里的旧快照/补丁被压成摘要 → `_sync_memory_patch` 因无落后 entries 直接 return → 模型上下文里的完整记忆只剩摘要残影，丢失磁盘最新内容。

## 背景

- `agent/loop.py` `_should_rebuild_memory(entries, consolidated)`：`consolidated` 只是 OR 条件之一，且 `_sync_memory_patch` 开头有 `if global_rev <= self._memory_revision: return`（无落后 entries 时直接跳过，根本不进重建分支）。
- GPT 方案 §4.5 建议「压缩不参与快照重建判断」——**本项目不采纳**，反向强化：压缩时必须重建（原因：压缩后 messages 结构已变、缓存必然失效，塞完整快照最划算；且补丁消息可能已被摘要吞掉）。

## 范围

- `agent/loop.py`：`_sync_memory_patch` / `_should_rebuild_memory` / `_rebuild_memory_snapshot` 联动逻辑。
- `tests/`：新增压缩后快照重建的测试。

## 非目标

- ❌ 修改补丁机制本身（TASK-004 行为不变：未压缩时仍按 entries 判断补丁 vs 快照）
- ❌ 修改快照消息格式 / `build_snapshot_message`
- ❌ 压缩算法改造（TASK-006/008/009）

## 验收标准（实现核对）

- [x] 触发压缩的那一轮，无论有无新 entries，都执行 `_rebuild_memory_snapshot`（读磁盘最新 USER.md/MEMORY.md → 生成快照消息 → 替换历史里旧补丁）——`_sync_memory_patch` 将 `consolidated` 判断提前到 early-return 之前，压缩过即走重建分支
- [x] 压缩后模型上下文中的记忆快照 = 磁盘最新完整版，无旧快照残影（构造「本会话刚写记忆 + 立即压缩」场景验证）——新单测 `test_compaction_rebuilds_snapshot_even_when_revision_latest`，修复前该测试失败（0 快照）、修复后通过（1 快照，含 MacBook）
- [x] 未压缩时行为与现状一致（补丁 / 快照按 entries 判断，零注入缓存命中不受影响）——`consolidated=False` 分支与既有逻辑逐字相同；既有零注入/补丁/自写刷基线测试全过
- [x] 重建失败仍回退补丁模式 / 静默降级（现有降级路径保留）——`_rebuild_memory_snapshot` 的 try/except 与回退逻辑未动
- [x] 单元测试全过（unittest 361 个 OK）、`git diff --check`、`compileall`、`import main` 通过
- [x] 文档同步：本任务卡状态、DECISIONS.md 已记录决策（「压缩→无条件重建快照」与 GPT 方案 §4.5 相反的决策记录）、PROJECT.md 能力矩阵「记忆体系」行已同步

## 相关模块

- `agent/loop.py`（`_sync_memory_patch` / `_should_rebuild_memory` / `_rebuild_memory_snapshot`）
- `agent/memory_sync.py`（只读复用 `build_snapshot_message` / `read_memory_files`，不改）

## 实现方案

- `_run` 中压缩段拿到 `result.compacted == True` 后，把该标志传给 `_sync_memory_patch`（或在其内部由 compactor 状态判断）。
- `_should_rebuild_memory`：`consolidated` 提升为**决定性条件**——本轮压缩过则直接重建（无需再看 entries / patch_count / tokens / removed）。
- 重建走现有 `_rebuild_memory_snapshot`：读最新文件 → 生成快照 → `canonicalize_history` 剔除旧补丁 → 落盘 → `_advance_memory_revision(global_rev)` → 重装 messages。失败回退补丁模式的现有路径保留。

## 测试方式（2026-08-05，code-master 实际执行）

```bash
git diff --check
.venv/bin/python -m unittest discover -s tests
uv run python -m compileall -q agent bus channels providers session
uv run python -c "import main"
```

实际结果：

```text
git diff --check
（无输出，通过）

.venv/bin/python -m unittest discover -s tests
Ran 361 tests in 37.974s
OK

uv run python -m compileall -q agent bus channels providers session
（无输出，通过）

uv run python -c "import main"
（无输出，通过）
```

新增单测（tests/test_memory_sync.py）：
- `test_compaction_rebuilds_snapshot_even_when_revision_latest`：真实 ContextCompactor（token_budget=10）走完整链路——回合 1 本会话自写 USER.md（全局/会话 revision 刷到 1）→ 回合 2 强制压缩 → 断言主 ReAct 上下文出现且仅出现一条 `<memory_snapshot>`（含磁盘最新内容 MacBook）、无 `<memory_patch>`、磁盘会话历史同步重建。**回归验证：临时还原 loop.py 修复后该测试失败（0 快照，`AssertionError: 0 != 1`），证明其能复现原始洞**。
- `test_should_rebuild_consolidated_is_decisive`：consolidated=True 时无条件重建（entries 再少也 True）；未压缩时按原阈值判断（1 条小补丁 → False）。

适配既有测试（tests/test_prompt_cache_loop.py）：`test_consolidation_call_is_included_in_weighted_turn_metrics` 的 `metric.history_messages` 由 6 改为 7——压缩回合现在会无条件注入一条快照进历史，主 ReAct 历史数比纯压缩结果多 1（快照），属 TASK-007 预期行为。

## 风险与遗留

- 压缩每轮最多触发一次，重建快照是既有路径，成本可控。
- 需确认压缩段与 `_sync_memory_patch` 的调用顺序（当前压缩在前、同步在后），保证标志传递正确。

## 下一步

乖宝验收通过后：commit（原子、仅含本任务文件）→ 任务卡移入 `docs/tasks/completed/`。

## 实现摘要（2026-08-05 code-master）

改动文件：
- `agent/loop.py`：`_sync_memory_patch` 将 `consolidated` 判断提前到 `global_rev <= memory_revision` early-return 之前，压缩过即 `_rebuild_memory_snapshot`（修复早退洞）；`_should_rebuild_memory` 的 `consolidated` 提升为决定性条件（无条件返回 True）。
- `tests/test_memory_sync.py`：新增上述 2 个单测。
- `tests/test_prompt_cache_loop.py`：适配压缩回合 history_messages 断言（6→7）。
- `docs/`：本任务卡、DECISIONS.md、PROJECT.md 能力矩阵。

关键决策：压缩→无条件重建快照（与 GPT 方案 §4.5「压缩不参与重建判断」相反）。理由：压缩发生 = 前缀缓存已破，塞一条完整快照是免费增量；补丁消息可能已被摘要吞掉，靠 entries 判断会漏。重建读磁盘最新内容、不依赖 entries，revision 已最新也安全（_advance_memory_revision 幂等）。
