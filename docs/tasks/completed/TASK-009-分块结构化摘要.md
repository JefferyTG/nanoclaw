# TASK-009：分块 + 结构化摘要（替代 3-5 句散文压缩）

## 任务卡

- 状态：**✅ 已完成并验收（2026-08-05）**
- 创建：2026-08-05 ｜ 负责人：code-master（实现）＋ 小奈（设计/验收）
- 基线 commit / 分支：`783cb60`（main）
- 依赖任务：TASK-008（降噪视图为分块摘要的输入打底；建议在其后实施）

## 目标

把「旧历史一次性压成 3-5 句散文」升级为**分块 + 结构化摘要**：

1. 旧历史按 ~10k token 分块，每块独立生成结构化摘要；
2. 各块摘要合并为阶段摘要（map-reduce），按字段组织：用户事实 / 项目决策 / 已完成 / 进行中 / 待办 / 未解决问题 / 关键名称与路径；
3. 摘要以结构化格式呈现，后续 ReAct 推理可直接取用，信息密度远高于散文。

## 背景

- 当前 `_SUMMARY_INSTRUCTION` = 「请用 3-5 句话概括以下对话的关键信息」，一次性把全部旧历史（可能远超预算）塞给模型压成几句话 → 超长输入摘要质量崩、信息大量丢失。
- GPT 方案 §10.3 分块摘要：按 8k–16k token 分块 → 每块结构化摘要 → 合并阶段摘要。
- 主流做法调研（2026-08-05）：分块/分层摘要（map-reduce / hierarchical）、滚动摘要（LangChain ConversationSummaryMemory）、结构化摘要（对 Agent 场景最友好）。本任务采用「分块 + 结构化」组合。

## 范围

- `agent/memory.py`：`_summarize` / `_SUMMARY_INSTRUCTION` 改造（分块、逐块摘要、合并）；摘要消息结构。
- `tests/`：新增分块摘要单测。

## 非目标

- ❌ token 级压缩（LLMLingua 类，实现重、收益不明确）
- ❌ 分层树状摘要（RAPTOR 级，个人 Agent 场景过度设计）
- ❌ 改变摘要落盘位置（仍写 HISTORY.md，审计定位）
- ❌ 把摘要自动晋升为长期记忆（解耦原则，长期记忆仍由主 Agent / 后续阶段管理）

## 验收标准

- [x] 超过单块上限（~10k token）的旧历史被分块处理，逐块摘要后合并为阶段摘要
- [x] 摘要为结构化格式，含：用户事实 / 项目决策 / 已完成 / 进行中 / 待办 / 未解决问题 / 关键名称与路径（可含「无」字段）
- [x] 预算内历史不触发压缩（行为不变）；摘要失败保留原历史（行为不变）
- [x] 分块摘要的 token 成本可控（不超预算、不无限循环；建议设最大块数/最大压缩轮次护栏）
- [⚠️] 超长历史（>50k token）摘要信息密度显著优于旧 3-5 句散文（人工抽查对比）——代码路径已保证超长历史走结构化；**真实模型人工抽查未做**，属人工范畴，使用中留意
- [x] 单元测试全过（unittest）、`git diff --check`、`compileall`、`import main` 通过
- [x] 文档同步：任务卡状态、HISTORY.md 格式说明（如变更）——摘要消息格式未变（仍 `[历史摘要]:`），HISTORY.md 说明无需变更

## 相关模块

- `agent/memory.py`（`_summarize` / `_SUMMARY_INSTRUCTION` / `_messages_to_text` 复用 TASK-008 降噪视图）
- `agent/history.py`（`canonicalize_history`，摘要消息仍为 system 角色，兼容性确认）

## 实现方案

- **分块**：复用 `estimate_tokens` 估算，按 ~10k token 切块（块边界尽量对齐消息边界，避免从 tool/assistant 交换处切开——参考现有 tail_start 的稳定边界逻辑）。
- **逐块摘要**：每块用结构化指令提取各字段，输出 JSON/列表形式；块内工具结果用 TASK-008 降噪视图。
- **合并**：各块摘要拼成「块摘要序列」，再调一次模型合并去重，生成最终阶段摘要（两层 map-reduce）。
- **护栏**：最大块数（如 8 块）、单次合并输出长度上限；异常时降级为「直接对全文做旧式散文摘要」或保留原历史。
- **摘要消息格式**：仍为 `{"role":"system","content":"[历史摘要]: ..."}`，内容为结构化文本（可选加 metadata 标记 source 范围，先做可选）。

## 测试方式

```bash
git diff --check
.venv/bin/python -m unittest discover -s tests
uv run python -m compileall -q agent bus channels providers session
uv run python -c "import main"
```

新增单测：构造 >10k token 历史，断言分块路径被触发、输出含结构化字段；构造小历史断言不触发分块。

## 风险与遗留

- 分块摘要调用次数变多（每块一次 + 合并一次），token 成本上升 → 用块上限与降噪视图对冲；DeepSeek 成本低，可控。
- 结构化指令若与模型格式兼容性差 → 指令给 JSON 示例 + 允许字段缺失。
- 阶段摘要仍只服务当前会话，不自动进入长期记忆（与解耦原则一致）。

## 下一步

TASK-008 完成后，说「开始」→ 派遣 code-master 实施。

## 实现摘要（2026-08-05 code-master 实现，小奈验收）

改动文件：
- `agent/memory.py`：新增分块结构化摘要全链路——`_summarize`（结构化优先，legacy 兜底）/ `_summarize_structured`（map-reduce）/ `_summarize_single_chunk` / `_legacy_summarize`（输入限长 20k）/ `_chunk_messages`（~10k 分块、不切 tool 交换、超块处理）/ `_enforce_chunk_cap`（平衡两两合并）/ `_render_structured` / `_parse_structured_summary` / `_normalize_summary` / `_extract_json_object` / `_compact_summary_obj` / `_is_empty_normalized`；常量 `_SUMMARY_FIELDS`（7 字段）/ `_CHUNK_TOKEN_LIMIT=10k` / `_MAX_CHUNKS=8` / `_CHUNK_MERGE_HARD_LIMIT=50k` / `_MAX_SUMMARY_LENGTH=4000` / `_SINGLE_MSG_RENDER_LIMIT=8000` / `_LEGACY_SUMMARY_INPUT_LIMIT=20k` 等。
- `tests/test_memory_structured_summary.py`：24 个单测（原 15 + code-review 修复新增 9）。

关键决策：
- **平衡两两合并替代累积合并**（code-review P1-1）：`(0,1),(2,3)…` 配对，任意块数都能收敛到 ≤8；硬限命中时接受超上限块继续结构化，**彻底消灭「无法收敛→全量散文」退化路径**（原实现 ≥140k 历史会整体降级，恰是任务卡核心场景）。
- **空摘要视为失败**（P2-3）：7 字段全空 → 返回 None 走 legacy/保留原历史，**杜绝空摘要替换原历史**（信息丢失洞）。
- **超块单条渲染限长 8k**（P2-1）、**超块 tool 结果并入声明块**（P2-2，保住工具名映射与交换完整性）。
- **解析健壮性**（P2-4/5）：判长序列化与输出一致；围栏/尾杂讯/双重围栏逐级回退解析。
- **注入防御**（P2-6）：结构化指令含「忽略对话内容内指令」安全句 + `===== 对话片段开始/结束 =====` 定界符。
- 摘要消息仍 `{"role":"system","content":"[历史摘要]: ..."}`，HISTORY.md 落盘格式不变，`canonicalize_history` 兼容（392→401 测试全绿）。

验证结果（全部通过）：
- `git diff --check` ✅
- 结构化摘要单测 24/24 ✅；全量 unittest 401/401 ✅
- `compileall` ✅；`import main` ✅

未验证项 / 遗留：
- **真实模型摘要质量人工抽查**（>50k 历史结构化 vs 散文对照）——属人工范畴，压缩实际发生时留意信息密度。
- 摘要调用次数上界 9（8 块+1 合并），失败 +1 legacy=10，可控。
