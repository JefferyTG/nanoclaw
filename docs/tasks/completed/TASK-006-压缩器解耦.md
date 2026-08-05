# TASK-006：压缩器解耦（每会话独立实例 + 结果显式返回）

## 任务卡

- 状态：**实现完成，待乖宝验收**（2026-08-05 code-master 完成实现与验证）
- 创建：2026-08-05 ｜ 负责人：code-master（实现）＋ 小奈（设计/验收）
- 基线 commit / 分支：`783cb60`（main）
- 依赖任务：无（TASK-004/005 已归档，本任务与其无耦合）
- 提交：**未 commit**（按协作规则，提交前需乖宝确认）

## 目标

消除 `MemoryConsolidation` 的跨会话共享状态，让「当前会话上下文压缩」与「长期记忆副作用」彻底解耦：

1. **每会话独立压缩器实例**：`main.py` 不再创建全局单例，改为在 `make_agent_factory()` 中为每个 session 创建独立 `ContextCompactor`（已按推荐方案改名，见下）。
2. **压缩结果显式返回**：`maybe_consolidate` 已改为 `maybe_compact`，返回结构化 `CompactionResult` dataclass（messages / compacted / estimated_tokens / token_budget / summarized_messages / preserved_tail_messages），已删除共享可变字段 `last_estimate` / `last_consolidation`。
3. **移除压缩器的 daily 依赖**：压缩流程已不再调用 `summarize_messages_to_daily`；压缩只生成当前会话摘要并写 HISTORY.md，不再产生任何长期记忆副作用。**乖宝已确认此决策**（压缩不再写 daily；daily 触发点只剩 `/clear`）。

## 背景

- 当前 `main.py:512` 创建**单个** `MemoryConsolidation` 实例注入所有 AgentLoop，`last_consolidation` / `last_estimate` 是共享可变字段 → 会话 A 压缩会污染会话 B 的判断。
- 压缩前调用 `summarize_messages_to_daily` 写 daily（`memory.py` `maybe_consolidate`），压缩与长期记忆耦合——这是 GPT 方案列出的首要痛点。
- GPT 方案目标：「当前模型需要看到多少上下文」与「系统以后应该记住什么」彻底分离。

## 范围

- `agent/memory.py`：`maybe_consolidate` 返回值改造；移除 daily 调用；新增 `CompactionResult`；类改名 `ContextCompactor`。
- `agent/loop.py`：读取压缩结果的方式改为返回值；`get_context_usage()` 与压缩后落盘逻辑适配。
- `main.py`：`make_agent_factory()` 内每会话创建独立压缩器；删除全局单例与 daily_memory 装配。
- `tests/`：新增/适配压缩器单元测试。
- 文档：本任务卡、PROJECT.md、DECISIONS.md、ARCHITECTURE.md、agent/daily.py 模块 docstring（同步类名与 daily 触发点）。

## 非目标

- ❌ 改变压缩算法本身（分块/结构化摘要 → TASK-009）
- ❌ 修改记忆快照/补丁机制（TASK-004 行为，TASK-007 另行处理）
- ❌ 增加候选记忆层、每日整理器（后续阶段）
- ❌ 修改 daily 文件格式与 `/clear` 写 daily 的行为（仅摘掉「压缩时写 daily」）

## 验收标准（实现核对）

- [x] `CompactionResult` 返回 messages / compacted / estimated_tokens / token_budget（另有 summarized_messages / preserved_tail_messages）；loop.py 不再读 `last_consolidation`（grep 确认 0 残留）
- [x] 两个不同 session 的压缩互不影响（各自独立实例，压缩 A 不改变 B 的任何状态）——新增单测 `test_independent_instances_do_not_interfere` / `test_same_instance_no_cross_turn_state_leak`
- [x] 压缩流程不再调用 `summarize_messages_to_daily`（`agent/memory.py` 无 daily import/参数/调用；新增单测 mock DailyMemory 断言未被调用 + 磁盘无 daily 目录）
- [x] 压缩行为与压缩前一致：预算内原样返回；超预算压成摘要；摘要失败保留原历史；HISTORY.md 仍写（既有边界测试全部适配通过）
- [x] `/context` 命令与 Web 进度条仍正常显示占用（`get_context_usage` / `_build_usage_event` / `_build_turn_event` 的 budget 读取改为 `self.compactor.token_budget`；test_context_budget 全过）
- [x] 单元测试全过（unittest 359 个 OK）、`git diff --check`、`compileall`、`import main` 通过
- [x] 文档同步：本任务卡、DECISIONS.md（压缩不再写 daily）、PROJECT.md 模块描述/能力矩阵、ARCHITECTURE.md 数据流、daily.py docstring

## 相关模块

- `agent/memory.py`（ContextCompactor / CompactionResult）
- `agent/loop.py`（AgentLoop.run 压缩段、get_context_usage、_sync_memory_patch 压缩联动）
- `main.py`（make_agent_factory / shared 装配）
- `agent/tools/filesystem.py`（`_record_memory_write` 与此无关，未动）

## 实现方案（落地细节）

- **命名**：类名改为 `ContextCompactor`，**同文件改名**（保留 `agent/memory.py`，任务卡范围明确列出该文件；「新建 context_compactor.py 或同文件改名」二选一，选改动最小者）。涉及 import 一并更新：`agent/loop.py`、`main.py`、`tests/test_context_budget.py`、`tests/test_prompt_cache_loop.py`、`tests/test_channel_context.py`。
- **返回值**：新增 `@dataclass CompactionResult`（messages / compacted / estimated_tokens / token_budget / summarized_messages / preserved_tail_messages）；`maybe_consolidate` → `maybe_compact`，返回 `CompactionResult`；调用方 `result = await compactor.maybe_compact(messages, tools=...)`，`messages = result.messages`。
- **每会话实例**：`make_agent_factory()` 的 `factory(session_key)` 内 `compactor = ContextCompactor(provider, os.path.join(cfg.workspace, "workspace"), token_budget=cfg.context_budget_tokens)`，注入 `AgentLoop(compactor=compactor)`。允许共享：config / provider / tools / session_manager；必须独立：AgentLoop / compactor / token 估算状态 / 本轮压缩结果。
- **daily 移除**：构造函数删除 `daily_memory` 参数；`maybe_compact` 删除 `summarize_messages_to_daily` 调用块。`main.py` 中 `daily_memory` 仍保留装配（供 `/clear` 使用），只是不再传给压缩器。
- **loop.py 适配**：
  - 落盘判断由 `self.memory.last_consolidation.get("consolidated")` 改为 `result.compacted`；
  - `get_context_usage()` / `_build_usage_event` / `_build_turn_event` 的 budget 读取改为 `self.compactor.token_budget`；
  - `_sync_memory_patch` 的「历史压缩联动重建快照」改为读 `self._last_compaction`（每回合私有、`_run` 开头重置）；
  - `MemoryConsolidation._estimate_value` 静态方法引用改为 `ContextCompactor._estimate_value`。

## 测试结果（2026-08-05，code-master 实际执行）

```text
git diff --check
（无输出，通过）

.venv/bin/python -m unittest discover -s tests
Ran 359 tests in 37.977s
OK

uv run python -m compileall -q agent bus channels providers session
（无输出，通过）

uv run python -c "import main"
（无输出，通过）
```

新增/适配单测（tests/test_memory_cache_boundary.py 全量重构为 ContextCompactor + CompactionResult API）：
- `test_compaction_result_fields_and_stable_boundary`：CompactionResult 字段正确性（compacted/estimated_tokens/token_budget/summarized_messages/preserved_tail_messages）
- `test_within_budget_returns_same_messages_uncompacted`：预算内原样返回（同一对象）
- `test_independent_instances_do_not_interfere`：A 压缩不改变 B 任何状态
- `test_same_instance_no_cross_turn_state_leak`：同实例上一轮压缩不影响下一轮判断
- `test_compaction_never_writes_daily`：mock DailyMemory.append / summarize_messages_to_daily 断言未被调用 + 磁盘无 daily 目录（HISTORY.md 仍写）

## 风险与遗留

- **✅ 已确认决策**：移除「压缩时写 daily」后，daily 触发点只剩 `/clear` 一个；第三阶段「每日整理/做梦机制」落地前，压缩丢的事件不再留痕 daily（乖宝确认，见 DECISIONS.md 新增记录）。
- HISTORY.md 保留（压缩摘要仍写，定位为审计记录）。
- **旧类名清理（2026-08-05 乖宝确认后已处理）**：`config.py:71/132` 注释与 `README.md:283` 的 `MemoryConsolidation` 均已改为 `ContextCompactor`；`agent/daily.py` docstring 已同步（类名 + 触发点）。历史引用保留于 TASK-005 归档任务卡与 DECISIONS.md（描述改名过程本身，不属漂移）。
- **行为等价的额外保障**：`estimate_tokens` 不再写 `last_estimate`（该字段已删除），其返回值语义不变；`test_estimate_includes_multimodal_payload_and_tool_schema` 改为对比「带工具 vs 不带工具」「图片 vs 等长纯文本」的估算差，替代对 `last_estimate` 的断言。

## 下一步

乖宝验收通过后：commit（原子、仅含本任务文件）→ 任务卡移入 `docs/tasks/completed/`。
