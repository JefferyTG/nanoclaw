# TASK-012：会话索引实时性（新增会话进 memory_search）

> 状态：**已完成（2026-08-06 验收归档）**
> 创建：2026-08-05 ｜ 负责人：code-master（实现）＋ 小奈（设计/验收）
> 基线 commit：`857b003`（main，TASK-010 归档后）｜ 实现提交：待验收后提交

## 目标

修复 memory_search 的会话索引新鲜度问题：**新增会话/新消息实时进索引**，不再等到下次重启才能搜到最近对话。

## 背景

- `agent/search.py` 现状：启动时 `rebuild_all()` 全量建索引一次；每次 `search()` 前只 `refresh_memory()`（重建 USER/MEMORY/daily 记忆文件部分），**sessions 部分启动后永不刷新**（注释理由：「量大；当前会话的新消息滞后可接受」）。
- 实测（2026-08-05）：`sessions/` 有 126 个会话文件，索引库里 session 消息 3029 条，indexed_at 最新只到 `cli:direct` 19:33:52——**当前会话 web_ws-b230c3dcd881_0 不在索引里**。越新的对话越搜不到，而「最近聊了什么」恰恰是搜历史最高频场景。
- 乖宝 2026-08-05 拍板：采用**方案 A（增量扫）**——每次搜索前除 refresh_memory 外，也增量扫 sessions 目录，只补新增，不重抄旧的。

## 范围

- `agent/search.py`：新增「会话部分增量刷新」逻辑；`search()` 前除 `refresh_memory()` 外调用增量刷新。
- `agent/tools/search.py`：无需改动（透传）。
- `main.py`：无需改动（searcher 已持有 session_manager）。
- 文档：`agent/search.py` 模块头「新鲜度策略」注释同步；PROJECT.md 能力矩阵「记忆体系」行补充检索新鲜度描述。

## 非目标

- ❌ 不改索引存储结构（仍为 SQLite 普通表 + LIKE，不引入 FTS5/向量）。
- ❌ 不改为每次搜索全量重建 sessions（保持增量，避免不必要的开销）。
- ❌ 不增加向量/语义检索（那是 followup「memory 语义理解升级」，另行处理）。
- ⚠️ 已调整：「删除会话同步删索引」由非目标升级为**已实现**——增量刷新状态对比自然发现「已索引但文件已消失」的会话，顺手清索引（成本近零），使 `/clear` 清空会话后旧内容搜不到（原「风险」段要求，与「非目标」段措辞冲突，以风险段行为为准，2026-08-06 确认）。

## 验收标准

- [x] 新会话实时可搜：写入新会话消息后，`memory_search(scope=session)` 立即能搜到（无需重启）——`test_new_session_searchable_immediately` ✅
- [x] 既有会话新增消息可搜：老会话追加消息后，增量刷新能补上新消息——`test_appended_message_searchable` ✅
- [x] 增量而非全量：刷新只处理「有变化的会话」，不重抄未变化的会话（按文件 mtime/size 判断；单测断言未变化会话不重复插入）——`test_unchanged_session_not_reindexed` ✅
- [x] 记忆文件部分行为不变：refresh_memory() 仍每次搜索前执行，USER/MEMORY/daily 可搜（全量测试覆盖）✅
- [x] 性能可接受：126 会话 / 504 消息实测空闲 refresh 0.487ms、含 1 变化会话 0.636ms，远低于 100ms 阈值——`test_refresh_sessions_perf` ✅
- [x] `/clear` 后旧内容搜不到：`session_manager.clear()` 删文件 → 增量刷新清索引——`test_clear_removes_index` ✅
- [x] 文档同步：`agent/search.py` 模块头新鲜度策略已更新；PROJECT.md 能力矩阵已同步 ✅
- [x] 测试：`.venv/bin/python -m unittest discover -s tests` **Ran 463 tests OK**；`git diff --check`、`compileall`、`import main` 全过 ✅

## 相关模块

- `agent/search.py`（MemorySearcher / refresh_memory / **新增 refresh_sessions** / `_session_state`）
- `agent/tools/search.py`（MemorySearchTool，未动）
- `main.py`（searcher 装配，未动——session_manager 已注入）
- `session/manager.py`（list_sessions / get_session_messages，未动）
- `PROJECT.md`、`docs/ARCHITECTURE.md`（检索数据流描述）

## 实现方案（已落地）

1. **方案 A：增量扫 sessions**（乖宝确认的方向）：
   - `MemorySearcher` 新增内存状态 `_session_state: dict[stem, (st_mtime_ns, st_size)]`（stem = list_sessions 返回值）。
   - `rebuild_all()` 重置 `_session_state`（以磁盘为准），`_index_sessions()` 逐会话填充状态。
   - 新增 `refresh_sessions() -> int`（返回本次新索引文档数）：
     - 遍历 list_sessions：`(mtime_ns, size)` 与状态不同或首次见 → 整会话重索引（DELETE 该会话旧索引 + 逐条 INSERT，复用与 `_index_sessions` 相同的过滤：空 content / role=='tool' 跳过）；未变化跳过。
     - 状态中存在但 list_sessions 已无此 stem（文件被删，如 `/clear`）→ 清索引 + 移除状态。
     - 统一 commit；`get_session_messages` 自带 JSONDecodeError 容错，半截行跳过不崩。
   - `search()` 调用顺序：`refresh_memory()` → `refresh_sessions()` → LIKE 查询。
   - 状态仅存内存：重启由 `rebuild_all()` 兜底重建，符合方案 A 设计。
2. **测试**（`tests/test_search_freshness.py`，5 用例）：
   - 新会话立即可搜 / 老会话追加可搜 / 未变化幂等（行数不变）/ `/clear` 清索引 / 126 会话刷新 <100ms。

## 测试方式

```bash
.venv/bin/python -m unittest discover -s tests          # 全量单测（463 OK）
.venv/bin/python -m unittest tests.test_search_freshness -v  # 新增测试（5 OK）
git diff --check                                        # 通过
uv run python -m compileall -q agent bus channels providers session   # 通过
uv run python -c "import main"                          # 通过
```

## 风险

- 「整会话重索引」在会话超大（上万条）时单次稍慢：个人助手场景会话一般数百~数千条，可接受；如未来会话巨大再优化为偏移续读。
- 会话文件正在写入时读（并发）：`get_session_messages` 已有 JSONDecodeError 容错，不崩。
- `/clear` 后行为：已实现「文件消失 → 清索引」，清空会话后旧内容搜不到 ✅。
- `stem.replace("_", ":")` 还原对原始 key 含下划线的会话有歧义（`cli:a_b`→`cli:a:b`）：既有约定（list_sessions_detailed / 原 _index_sessions 同款），本次保持一致，未引入新问题；记候选后续统一处理。
- 索引库与真实文件状态漂移（mtime 被外部改动漏检）：可接受，重启 rebuild_all 兜底。

## 归档记录

- 2026-08-06 乖宝验收通过，按 project-manager 清单完成归档：全量单测 463 OK、`git diff --check` / `compileall` / `import main` 全过；任务卡移至 `docs/tasks/completed/`；PROJECT.md 能力矩阵与里程碑、ARCHITECTURE.md 检索数据流、DECISIONS.md 决策表、MEMORY.md 指针已同步。