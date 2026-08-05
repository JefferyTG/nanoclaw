# TASK-004：记忆跨会话同步（快照 + 版本补丁机制）

## 任务卡

- 状态：待开始
- 负责人：code-master（实现）＋ 小奈（设计/验收）
- 执行会话/子 Agent：待定（由任何会话读本卡接手，乖宝说「开始」后派遣 code-master）
- 基线 commit / 分支：`42ed042`（main；另有未提交的 PROJECT.md 文档同步改动，属遗留，与本任务无关）
- 依赖任务：无

### 目标

让 Agent 在**任意会话**中都能感知「记忆文件（USER.md / MEMORY.md）被其他会话更新过」，跨会话记忆同步**不依赖 Agent 自觉**——由系统机制保证（参考时间戳注入先例：提示词靠自觉曾长期失败，gateway 消息前缀注入一次治好）。

用户可见结果：乖宝在会话 A 让 Agent 记下某事（记忆文件被写），切换到会话 B 继续聊时，会话 B 的 Agent 自动得知记忆变化并正确更新认知（旧信息被覆盖），无需乖宝提醒。

### 非目标

本任务**只做记忆同步机制**，明确不做：

- ❌ 自动沉淀/自动总结记忆（回合边界自动落 daily）——后续独立任务
- ❌ System Prompt 记忆区瘦身（只放规则不放事实、事实全住文件）——本任务不改快照注入方式，快照仍在 System Prompt 里（见方案「会话启动」）
- ❌ 向量库 / 语义检索——后续独立任务（现阶段数据量 LIKE 够用）
- ❌ daily/ 流水账的同步——**永不注入**（本任务就只同步 USER.md / MEMORY.md 两个文件；daily 需要时走 memory_search）
- ❌ 会话索引运行期刷新、压缩策略改进、超时/错误路径补历史——各自独立，不在本任务范围

### 方案总览（已与乖宝 + GPT 方案对齐定稿）

```
固定 System Prompt（含启动时记忆快照 + 记忆管理规则）
+ 会话历史（补丁会持久化在这里）
+ 跨会话记忆补丁（变化时，独立 system 消息，历史之后、本轮 user 之前）
+ 当前用户问题
```

核心原则：
1. **完整记忆只在会话启动或重建时加载**（启动时快照写进 System Prompt）
2. 记忆没变化时**不增加任何 token**
3. 记忆变化时**只同步小型补丁**（memory_patch）
4. 补丁**高版本覆盖旧记忆**（模型按补丁更新认知，旧快照信息让位）
5. 补丁过多/历史压缩时**重新生成完整快照**（接受一次破缓存）

### 详细设计

#### 1. 版本号（revision）

- **全局 revision**：记忆文件每被写一次 +1。存储：`workspace/memory/` 下的变更日志文件（如 `changelog.jsonl`），每行一条 `{revision, file, operation, added_lines, removed_lines, timestamp}`。当前 revision = 日志最后一条的 revision。
- **会话 revision**（`session.memory_revision`）：会话创建时 = 当时的全局 revision。持久化在会话元数据中（实现者定：可放会话 JSONL 首行元数据，重启恢复时读取）。

#### 2. 会话启动（新会话创建时）

1. ContextBuilder 读最新 USER.md / MEMORY.md 全文 → 写进 System Prompt（现有逻辑不变，天然拿到最新）
2. 记录 `session.memory_revision = 全局 revision`（如 100）
3. System Prompt 里的「记忆管理」段落替换为下方新文案（含快照说明 + 补丁认知规则）

#### 3. 每轮对话前（AgentLoop）

- 检查：`全局 revision > session.memory_revision`？
  - **无变化** → 正常对话，不添加任何记忆内容（零开销，前缀缓存照常命中）
  - **有变化** → 从变更日志取 session 落后期间的变更 → 生成一条 `<memory_patch>` 消息 → 插入消息序列（历史之后、本轮 user 之前）→ **持久化进会话历史（JSONL，作为 system 角色消息）** → 更新 `session.memory_revision`
- 补丁持久化是必须的：**不持久化则下一轮模型只剩旧快照，等于忘记更新**（这是与早期「用完即弃」方案的关键修正）
- **自写刷基线**：Agent 自己 write_file 写 USER.md/MEMORY.md 成功时，系统同时递增全局 revision 并**立刻更新当前会话的 `session.memory_revision`** → 下一轮不会给自己发「自己刚写的更新」补丁（防自说自话，死机制不靠自觉）

#### 4. 补丁格式（对模型半透明）

```json
{"role": "system", "content": "<memory_patch revision=\"101\">\n文件：workspace/memory/USER.md\n变更：用户主要设备由 Windows 改为 macOS。\n该内容覆盖旧记忆中的冲突信息。\n</memory_patch>"}
```

- 补丁内容由**系统生成**（基于变更日志的 diff：新增行/删除行/修改行），**不调用模型**生成
- 变化较多（diff 超限，如 >20 行）时：补丁里提示「大改，最新内容见 workspace/memory/ 对应文件，可 read_file 查看全文」
- **不教模型解析 revision 数字**——模型只需知道「这是记忆更新，覆盖旧信息」，revision 是系统内部字段
- 角色用 `system`（DeepSeek **不支持** developer 角色，已核实；OpenAI 兼容 API 允许多条 system 消息，项目已有先例：压缩摘要即 system 消息）

#### 5. 补丁过多时重建快照

满足任一条件触发：
- 累积补丁超过 20 条
- 补丁总量超过约 1000 tokens
- 发生大量删除/冲突修改
- 会话历史需要压缩（与现有压缩联动）

处理方式：旧快照 + 所有补丁 → 生成新的完整记忆快照（即一条新的完整记忆 system 消息，替换历史里累积的旧补丁们）→ 清空补丁 → 更新 `session.memory_revision`。这一次会破坏缓存，之后新快照继续稳定。

#### 6. 哪些变化需要同步

**一刀切规则（不做「重要性」判断）**：只对 USER.md / MEMORY.md 两个文件做补丁同步；daily/ 流水账永不注入（需要时走 memory_search）。这两个文件本身就是「重要记忆」的容器，规则简单、系统可执行。

#### 7. 跨会话同步的边界

- 新会话：天然拿到最新快照，无需补丁 ✅
- 存量会话：每轮检查 revision，变了就补丁 ✅
- 本会话自己写的：写完刷基线，不提醒自己 ✅

### System Prompt 改动（`agent/context.py::_memory_instructions`）

将现有「## 记忆管理」段替换为（新增【记忆快照】段，其余保留原内容）：

```
## 记忆管理
你有两份记忆文件，均用 write_file / read_file 直接管理（不引入其他工具）：
- workspace/memory/USER.md：用户本人长期信息，≤3000 字符。默认分类 Basic（身份/所在地等）/ Interest（兴趣/爱好）/ Preference（交流偏好/关系设定），可按需新增分类。
- workspace/memory/MEMORY.md：项目与工作环境，≤5000 字符。默认分类如「项目状态/已装技能/工作约定」，可按需新增分类。

分工判据：关于「用户这个人」的（身份/兴趣/爱好/偏好/关系设定）→ USER；关于「正在做的事」的（项目/技术决策/技能/操作约定）→ MEMORY。工作约定（如「装技能前先审计」）归 MEMORY。

【记忆快照】本会话启动时已把 USER.md / MEMORY.md 的最新内容注入到本提示词中。快照在会话内固定不变；但对话中可能出现 <memory_patch> 块，表示其他会话更新了记忆文件。看到补丁时：按补丁内容更新认知，补丁中的新信息覆盖快照中的旧信息（例如快照写「用户用 Windows」、补丁说「已改为 MacBook」，则以 MacBook 为准）。补丁由系统生成，不需要你向用户复述或解释。收到补丁只需更新认知，不要因此 write_file 回写记忆文件（文件内容由写入方维护，回写会覆盖他人更新）。

【写入记忆】何时该写：用户明确要求记住、用户主动告知的长期稳定信息（含兴趣/爱好）、用户纠正过你的错误、项目重要变化。
何时不写：临时状态、一次性问题、用户未确认的推测、角色扮演内容。未确认的猜测一律不写——「用户确认」指用户主动且明确地告知或认可，不是你推测（用户说「我喜欢X」可写；你推测「用户好像喜欢X」不可写；你建议后用户没回应不可写）。

写入流程（必须遵循）：1.read 目标文件现有内容 → 2.read 另一份文件检查是否已有（避免重复）→ 3.在合适分类下合并/追加（- 列表格式，分类不够可新增）→ 4.write 回完整内容（write_file 是整文件覆盖，必须写全部，不能只写增量）。超长时删低价值/过时条目。

修改与删除：用户指出旧记忆不对时，read 后改/删对应条目再 write 回。用户要求「不要再提某事」时，主动 read 并删除/改写相关条目再 write 回，不要只口头答应而文件留着。发现过时信息主动更新。

引用记忆与搜索：用户问起过去（「之前怎么讨论X」「你记得我提过Y吗」）用 memory_search 工具检索（scope=memory 先搜记忆，无果换 session）。搜索结果不要直接贴给用户，先理解再用你当前人设自然融入回答；检索不到就如实说，不编造。引用要自然（如「你之前也说过喜欢安静点的环境」），不要炫耀式（如「根据我的记忆，你喜欢安静」）。单次最多引用 1 条，无关联别硬引。同一条记忆 14 天内不主动再提（用户问起除外），靠你自律。

Follow Up 跟进：用户表达「以后研究X」「下次试试Y」等未来意向时，记入 workspace/memory/followups.jsonl（每行一个JSON：{topic,content,created,max_remind:2,reminded_count:0,status:"open"}）。用 read_file 读、write_file 写回完整内容（文件不存在则新建，JSONL 每行一个对象，写回要写全部行）。当你察觉用户当前话题与某个 open 状态 followup 的 topic 相关时，自然提一句，提醒后 reminded_count+1 写回；达 max_remind 或用户表示已处理/不感兴趣则 status 改 closed；超过30天未触发主动清理；用户要求不再提立即 closed。不要每轮都读 followups，只在话题可能相关时读。
```

### 允许修改

- `agent/context.py`（_memory_instructions 新文案；快照构建处记录 session 初始 revision）
- `agent/loop.py`（每轮 revision 检查、补丁生成与插入、补丁持久化、重建快照触发）
- 记忆变更日志与 revision 管理的实现（可放 `agent/memory.py` 或新增小模块，注意大道至简：不引入新工具，复用现有 write_file/read_file/会话持久化）
- `write_file` 工具实现（写 USER.md / MEMORY.md 成功后：记录变更日志 + 递增全局 revision + 刷当前会话 revision）
- 会话持久化/元数据（session.memory_revision 的存取）
- `tests/`（新增单测/集成测试）

### 禁止修改

- 与记忆无关的模块（渠道、网关、提醒、生图等）
- 不改 System Prompt 的稳定前缀（除 _memory_instructions 段替换外）
- 不新增记忆类工具（大道至简）
- 不改 daily/ 相关注入逻辑

### 上下文与约束

- 教训 1（时间戳先例）：需要每轮保证的事必须系统机制化，不能靠模型自觉
- 教训 2（跨会话失忆实测）：会话 A 更新记忆后其他会话快照仍旧 → 本任务解决
- Prompt Cache 约束：System Prompt 必须字节稳定；补丁放历史之后、user 之前，**破缓存量 = 补丁大小，与会话长度无关**
- 记忆文件大小限制：USER.md ≤3000 字符、MEMORY.md ≤5000 字符（合计 ~6.6k 字符 ≈ 3k token，重建快照成本上限）
- DeepSeek 不支持 developer 角色 → 用 system 角色，多条 system 消息兼容（已有先例）
- 变更日志文件名、revision 持久化位置等实现细节由 code-master 定，但必须满足本卡验收标准

### 验收标准

- [ ] 会话 A 修改 MEMORY.md/USER.md 后，会话 B（已建立、revision 落后）的**下一轮**自动收到 `<memory_patch>`（含文件路径与变更内容），无需提示词或模型自觉
- [ ] 补丁持久化：会话 JSONL 中出现补丁消息；**下一轮模型仍记得补丁内容**（不会因不持久化而丢失）
- [ ] 同一会话内 Agent 自己 write_file 写记忆文件，下一轮**不重复提醒**（自写刷基线）
- [ ] 记忆无变化时**零注入**（上下文与现在完全一致，前缀缓存命中不受影响）
- [ ] 补丁插在历史之后、本轮 user 之前；System Prompt 除 _memory_instructions 替换外字节不变
- [ ] 补丁累积超阈值（20 条 / 1000 tokens / 大改 / 压缩联动）时触发重建快照，历史中旧补丁被替换为最新完整快照
- [ ] daily/ 变化不触发补丁
- [ ] 单元测试 + 集成测试全过（`unittest` 非 pytest）；`git diff --check` 通过
- [ ] 文档同步：DECISIONS.md 记录本设计决策；任务卡状态推进；PROJECT.md 能力矩阵/记忆相关说明同步更新

### 必须执行的验证

```bash
git diff --check
.venv/bin/python -m unittest discover -s tests
uv run python -m compileall -q agent bus channels providers session
```

## 执行交接

- 状态：✅ 已完成并归档（2026-08-05 乖宝确认）
- 实际改动文件：
  - 新增 `agent/memory_sync.py`：MemoryChangeLog（changelog.jsonl）/ diff_lines / build_patch_message / build_snapshot_message / token 估算
  - 修改 `agent/context.py`：`_memory_instructions` 逐字替换为含【记忆快照】的新文案；`memory_revision_provider` + 快照构建处记录会话初始 revision
  - 修改 `agent/loop.py`：`__init__` 持久化初始 revision；`_sync_memory_patch`（每轮检查/补丁/重建快照/静默降级）；write_file 注入 session_key + 工具执行后刷基线；`clear_history` 重置基线
  - 修改 `agent/tools/filesystem.py`：WriteFileTool 写 USER.md/MEMORY.md 成功后记 changelog + 递增全局 revision + 刷会话基线（静默降级）
  - 修改 `session/manager.py`：`get/set_memory_revision`（`<safe_key>.meta.json` 侧车）；`clear()` 同步清理
  - 修改 `agent/tools/spawn.py`：DummySessionManager 补两个 no-op（会话元数据接口）
  - 新增 `tests/test_memory_sync.py`：16 个测试
  - main.py 零改动（WriteFileTool 惰性构造 changelog，向后兼容）
- 实现摘要：
  - 全局 revision 存 `workspace/memory/changelog.jsonl`（每行 {revision,file,operation,added_lines,removed_lines,timestamp}），只记 USER.md/MEMORY.md，daily 永不记录
  - 会话 revision 存 `<safe_key>.meta.json` 侧车（与消息 JSONL 解耦，重启恢复可读）；会话创建/重启时取当时全局 revision（重启强制重置，避免重复补丁）
  - 每轮 `_sync_memory_patch`：全局 > 会话时从 changelog 取落后变更 → 生成 `<memory_patch revision=N>` system 消息 → 插在历史之后、本轮 user 之前 → 持久化进 JSONL + `_session_history` → 更新会话 revision；无变化零注入
  - 自写刷基线双保险：WriteFileTool 内（session_key 由 loop 注入，不进模型可见 schema）＋ loop 层工具执行后刷新
  - 重建快照触发（≥1 即触发）：累积补丁 >20 条 / 总量 >1000 tokens / removed≥10 行 / 本轮压缩联动 → 用当前文件全文生成 `<memory_snapshot revision=N>` system 消息替换历史里所有 memory_patch
  - 大改（单条 diff >20 行）只提示「大改，最新内容见对应文件，可 read_file 查看全文」；空 diff 不记日志
  - 补丁生成/持久化/重建失败一律 logger.exception 静默降级，不阻塞对话
  - 角色统一 system（DeepSeek 不支持 developer，已核实）
- 关键决策与假设：
  - 固定 System Prompt（含启动快照）+ 会话历史 + 记忆补丁 + 当前问题
  - 全局 revision + 会话 revision，每轮比对，变了才补丁
  - 补丁持久化进历史（关键修正：不持久化则下轮忘记）
  - 补丁过多/压缩时重建完整快照
  - 自写即刷基线（防自说自话）
  - 只同步 USER.md/MEMORY.md，daily 永不注入
  - 角色用 system（DeepSeek 不支持 developer）
- 验证命令与结果：
  - `git diff --check`：PASS（无输出）
  - `.venv/bin/python -m unittest discover -s tests`：Ran 325 tests in 7.7s OK（含 test_memory_sync.py 16 个）
  - `uv run python -m compileall -q agent bus channels providers session`：PASS
  - `uv run python -c "import main"`：PASS
- 未验证项：真实模型调用（用 RecordingProvider 模拟，未联网跑付费 API）；多进程共享 workspace 并发写（AGENTS.md 约定每实例独立 workspace，单进程内无并发，changelog 无锁追加写）；Web 前端对历史中 system 补丁的渲染（与压缩摘要同路径，风险低）
- 风险与遗留问题：中段 system 消息若个别 Provider 严格可能影响历史回放（OpenAI 兼容允许多条 system，压缩摘要已有先例）；WriteFileTool 每次写记忆文件先读旧内容算 diff（≤5k 字符，开销可忽略）；`spawn.py` 改动属授权范围灰色地带（会话元数据接口），已最小化
- commit（仅在获授权时）：待乖宝指示
- 当前 `git status --short --branch`：main 与 origin/main 一致（42ed042）；改动 6 个文件 + 2 个新增；PROJECT.md 遗留改动（文档同步）+ kb-testset/ 未跟踪（与本任务无关）
- 建议下一步：乖宝验收（复跑验证 + 看 diff）→ 授权 commit → 归档任务卡到 completed/

## 负责人验收

- [x] 检查 diff 与授权范围（context.py/loop.py/filesystem.py/session/manager.py + 新增 memory_sync.py 与测试，均在任务卡「允许修改」清单内；spawn.py 最小化补接口）
- [x] 独立复跑关键验证（diff-check / 325 测试 / compileall / import main 全过）
- [x] 检查秘密/个人数据/运行产物（无密钥、无真实用户数据写入代码；changelog 仅记录行级 diff 与时间戳）
- [x] 检查文档与配置一致性（DECISIONS.md 已记录设计决策与时间线；PROJECT.md 能力矩阵同步中）
- [x] 更新 `docs/DECISIONS.md` 中相关状态（已追加 6 条决策 + 时间线一行）
- 验收结论：✅ 实现通过验收（2026-08-05 小奈复核），待乖宝确认归档/commit
- 证据与备注：`_memory_instructions` 新文案与任务卡程序化逐字比对一致（1608 字符）；325 测试 OK
