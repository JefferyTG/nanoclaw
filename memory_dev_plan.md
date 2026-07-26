# NanoClaw 记忆系统开发计划（v2 · 已确认决策）

> 基于 `memory_development.md` 制定。**v2 已纳入用户对 6 个决策点的确认，审核通过后进入开发。**

---

## 一、项目架构理解（任务一产出）

### 1.1 整体定位

NanoClaw 是一个**本地优先、多渠道、可扩展**的个人 AI Agent 网关。核心设计理念：

- **解耦**：模型推理与消息渠道分离，靠一条 `MessageBus` 串联。
- **无全局单例**：单台设备可跑多个独立实例（每人/每场景一个进程），配置与运行时状态都在实例内部。
- **本地优先**：所有数据落盘（JSONL/Markdown），不依赖外部数据库或云服务。

### 1.2 技术栈与依赖

- **语言/运行时**：Python 3.13+，统一用 `uv` 管理环境（强制约定）。
- **核心依赖**（`pyproject.toml`）：`aiohttp`（网页渠道）、`lark-oapi`（飞书）、`openai`（OpenAI 兼容 Provider）、`mcp`（MCP Server 接入）、`httpx`、`pyyaml`（技能 frontmatter 解析）、`ddgs`/`html2text`（搜索/抓取）。
- **无数据库依赖**：当前完全基于文件（JSONL + Markdown）。

### 1.3 核心模块与交互方式

```
渠道(飞书/网页/CLI) ──▶ MessageBus ──▶ Gateway ──▶ AgentLoop ──▶ Provider(模型)
                          ▲                   │           │
                          │                   │           ├──▶ Tools(内置+MCP)
                          └─── 出站回复 ◀──────┘           └──▶ ContextBuilder(System Prompt)
```

| 模块 | 职责 | 关键文件 |
|---|---|---|
| `MessageBus` | 入站/出站/流式事件三条 `asyncio.Queue`，渠道与网关解耦 | `bus/queue.py` |
| `Gateway` | 按 `session_key` 路由消息、惰性创建并缓存 `AgentLoop`、每会话一把锁实现串行+跨会话并发 | `gateway.py` |
| `AgentLoop` | ReAct 主循环：System Prompt → 模型 → 工具调用 → 回填 → 直到给出最终回答；带熔断(≥20次)/预警(≥10次)/墙钟超时 | `agent/loop.py` |
| `ContextBuilder` | 拼装 System Prompt：`人设 → 工作区 → 长期记忆(MEMORY.md) → 记忆指引 → 技能摘要 → 当前时间(末尾，保 Prompt Cache)` | `agent/context.py` |
| `MemoryConsolidation` | token 预算压缩：超 192k 时把中间旧消息压成摘要，写入 `workspace/memory/HISTORY.md` | `agent/memory.py` |
| `SessionManager` | 会话 JSONL 持久化（追加/覆盖写回/历史恢复/自愈补全缺失 tool 消息） | `session/manager.py` |
| `ToolRegistry` + `Tool` 基类 | 工具注册与异步调度，统一生成 OpenAI function-calling 定义 | `agent/tools/registry.py`、`base.py` |
| `SkillsLoader` | 扫描 `skills/<name>/SKILL.md`，frontmatter 提取元信息，摘要注入 System Prompt | `agent/skills.py` |
| `MCPClientManager` | 把外部 MCP Server 工具以 `{server}__{tool}` 命名注入注册表 | `agent/tools/mcp.py` |

### 1.4 代码组织方式

- **装配在 `main.py`**：`build_shared()` 创建跨会话共享组件，`make_agent_factory()` 惰性创建 `AgentLoop`，`amain()` 装配总线/渠道/网关并驱动。
- **工具按文件分**：每个内置工具一个文件，继承 `Tool` 基类，在 `main.py` 显式 `register`。
- **数据落点**：会话 JSONL 在 `workspace/sessions/`，记忆在 `workspace/memory/`，技能在 `skills/`，人设在 `workspace/identity.md`。

---

## 二、现有记忆能力盘点 & 差距分析（任务二产出）

### 2.1 现有能力

| 能力 | 现状 | 与目标的关系 |
|---|---|---|
| 长期记忆注入 | `ContextBuilder` 读取 `workspace/memory/MEMORY.md` 注入 System Prompt，并附"记忆管理指引"让 Agent 用 `write_file` 工具直接改写 | ✅ 对应 Phase 1 的 MEMORY.md 注入；✅ 写入方式（write_file）已符合 v2 决策 |
| 会话压缩 | `MemoryConsolidation` 超 token 预算时压摘要，落 `HISTORY.md` | 部分对应 Phase 3"Context 压缩前保存重要信息"的触发点，但不写 daily |
| 会话持久化 | `SessionManager` JSONL 全量保存 | ✅ 对应 Phase 4 Session History 索引源 |
| USER.md | **不存在** | ❌ Phase 1 缺失 |
| Daily Memory | **不存在** | ❌ Phase 3 缺失 |
| 搜索 | **无**（只能靠模型读整个文件） | ❌ Phase 4 缺失 |
| Memory Cue | **无** | ❌ Phase 5 缺失 |
| Follow Up | **无** | ❌ Phase 6 缺失 |

### 2.2 关键差距点

1. **USER.md 未注入**：现有 `ContextBuilder` 只读 MEMORY.md，Phase 1 要求 USER.md 也注入 System Prompt。
2. **SQLite FTS5 可用性**：Python 标准库 `sqlite3` 是否启用 FTS5 取决于构建，需先验证；不可用则降级。
3. **现有"记忆管理指引"需强化**：`ContextBuilder._memory_instructions()` 当前指引较简略，v2 需扩展为更完整的"何时写/何时不写/如何删/不再提"指引（仍走 write_file，符合大道至简）。

---

## 三、已确认的设计决策（用户 2026-07-26 确认）

| # | 决策点 | 用户选择 | 对方案的影响 |
|---|---|---|---|
| 1 | 记忆根目录基准 | **同意** 用 `<workspace>/workspace/memory/` | 所有记忆文件落此目录，与现有 `HISTORY.md` 同级 |
| 2 | 写入记忆方式 | **不新建 memory_add 等工具，让模型直接用 `write_file`** | 推翻原 Phase 1/2 的工具化方案；写入控制靠 prompt/skill 指引，不搞 Memory Operation 协议与验证层 |
| 3 | daily 整理触发点 | **同意** 用 `/clear` + 压缩前 | 不加新命令；daily_append 作为程序内部方法，不作为工具 |
| 4 | 是否硬拦 write_file | **不拦截**；用户要求"不再提某些记忆"时模型自行改文件 | 不改 `WriteFileTool`；靠指引让模型自主增删改记忆 |
| 5 | Cue 注入时机 | **大模型自己判断** | 不做自动关键词触发；memory_search 返回原始结果，由模型按指引自然引用 |
| 6 | 交付节奏 | **可以** 分三单元验收 | A+B / C+D / E+F |

### 核心设计原则：大道至简

> 用户明确：**不引入与现有工具功能重复的工具**。`memory_add`/`memory_update` 这类与 `write_file` 高度重合的工具是多余抽象。行为约束靠 prompt/skill，而非堆工具。**唯一例外是真正的检索能力**——跨文件全文检索（FTS5）是 `read_file` 做不到的，可建 `memory_search` / `session_search`。

此原则已记入项目长期笔记（`.workbuddy/memory/MEMORY.md` "工具设计原则"节），适用于后续所有工具设计。

---

## 四、需求拆解（子任务 · v2）

### Phase 1：基础 Memory 文件系统（注入为主，写入靠 write_file）

| ID | 子任务 | 说明 |
|---|---|---|
| P1-1 | 统一记忆目录基准 | 所有记忆文件落 `<workspace>/workspace/memory/`，与现有 `HISTORY.md` 同级 |
| P1-2 | `ContextBuilder` 注入 USER.md | 扩展 `ContextBuilder`，读取 USER.md 注入 System Prompt（与 MEMORY.md 并列）；文件不存在时返回空串不报错 |
| P1-3 | 创建 USER.md / MEMORY.md 初始模板 | 提供初始结构（Basic/Interest/Preference 等），用户可手改 |
| P1-4 | 长度提示 | USER.md ≤3000、MEMORY.md ≤5000 字符；超限时由模型按指引自行合并/删低价值内容（不强制程序拦截） |

> **不新建 memory_add/update/remove 工具**——模型用现有 `write_file` / `read_file` 自主管理记忆文件。

### Phase 2：记忆写入行为指引（prompt/skill 强化，非工具化）

| ID | 子任务 | 说明 |
|---|---|---|
| P2-1 | 强化 `ContextBuilder` 记忆指引 | 扩展 `_memory_instructions()`：明确何时该写（用户身份/长期兴趣/偏好/明确要求记住/纠正过的错误）、何时不写（临时状态/一次性问题/未确认的推测/角色扮演）、如何写（read 现有 → 合并 → write 回） |
| P2-2 | 编写"记忆管理" skill | 在 `skills/memory/SKILL.md` 写完整指引：写入判定、用户确认语义、删除/不再提处理（用户明确要求时模型自行 write_file 改/删）、长度合并策略 |
| P2-3 | 落实"用户确认"判定 | 通过 prompt 指引模型自律：未确认的推测不写；用户明确确认后才写。不搞程序验证层 |

> 取消原 Phase 2 的 Memory Operation JSON 协议与程序验证层——按 v2 决策，靠指引而非机制。

### Phase 3：每日记忆系统（程序内部方法，非工具）

| ID | 子任务 | 说明 |
|---|---|---|
| P3-1 | 实现 `daily_append` 内部方法 | 在记忆模块内提供 `daily_append(category, content)`，追加到 `memory/daily/YYYY-MM-DD.md`，按 `## 分类` 组织。模型也可通过 read+write 自行整理 daily |
| P3-2 | `/clear` 触发整理 | 在 `/clear` 回调里触发：调模型总结当前会话重要事件/项目变化/用户新偏好，写入 daily |
| P3-3 | 压缩前保存 | 复用 `MemoryConsolidation.maybe_consolidate` 触发点，压缩前从待压缩消息提取重要事件落 daily |

### Phase 4：SQLite FTS5 搜索系统（唯一新增工具）

| ID | 子任务 | 说明 |
|---|---|---|
| P4-1 | 验证 FTS5 可用性 | 检测当前 Python `sqlite3` 是否编译启用 FTS5，不可用则定降级方案（LIKE 全表扫描 / 文件搜索兜底） |
| P4-2 | 实现索引构建与增量更新 | 索引 USER.md / MEMORY.md / daily/*.md / sessions/*.jsonl；记忆文件变更时（write_file 后无法 hook，改为启动时全量 + 按需重建）增量更新 |
| P4-3 | 实现 `memory_search` 工具 | FTS5 全文检索记忆文件，返回相关片段 |
| P4-4 | 实现 `session_search` 工具 | FTS5 检索 sessions JSONL 历史对话 |
| P4-5 | 搜索策略 | 优先 `memory_search`，无结果再 `session_search`（由模型按工具描述决策） |
| P4-6 | 工具注册 | 仅这两个工具注册到 `ToolRegistry` |

### Phase 5：陪伴式 Memory Cue（模型自主判断）

| ID | 子任务 | 说明 |
|---|---|---|
| P5-1 | Cue 指引写入 skill | 在记忆 skill 里写：搜索结果不直接展示，自然融入回答；单次最多引用 1 条；不炫耀记忆；不强行关联 |
| P5-2 | 冷却状态（可选轻量） | `memory_search` 内部可选维护 `cue_state.json`（记忆指纹→上次提起时间），14 天内不重复返回同一条。**v2 倾向先不做硬冷却**，靠模型自律；若实践中重复骚扰再加 |

> 按 v2 决策，Cue 注入时机由模型自己判断，不做自动关键词触发。

### Phase 6：Follow Up 系统（read+write 管理，先不建专门工具）

| ID | 子任务 | 说明 |
|---|---|---|
| P6-1 | `followups.jsonl` 结构约定 | 字段：topic / content / created / max_remind / reminded_count / status |
| P6-2 | 用 read_file + write_file 管理 | 模型按 skill 指引读写 followups.jsonl；新增追加一行、完成时改 status |
| P6-3 | 触发与限制写进 skill | 用户聊相关主题时模型主动检查 followups；最多提醒 2 次、用户关闭立即停、超时自动失效 |
| P6-4 | （备选）轻量工具 | 若实践中 JSONL 覆盖写易出错，再考虑加 `followup_add` / `followup_complete` 工具 |

### 横向子任务

| ID | 子任务 | 说明 |
|---|---|---|
| X-1 | 配置项扩展 | `config.py` / `config.example.json` 增加记忆相关配置（记忆根目录、FTS 索引路径、是否启用各 Phase） |
| X-2 | 装配集成 | `main.py` 的 `build_shared()` 创建记忆模块实例并注入 `AgentLoop` / `ContextBuilder` |
| X-3 | 测试用例 | 按 `memory_development.md` 第 6 章 5 个测试场景 + 各 Phase 验证方法 |
| X-4 | 文档更新 | 更新 `README.md` 记忆系统章节、`features_en.md` |

---

## 五、任务优先级（开发顺序 · v2）

```
阶段 A（基础注入 + 指引，无新工具）
  └─ P1-1 统一目录基准
      ├─ P1-2 ContextBuilder 注入 USER.md
      ├─ P1-3 初始模板
      ├─ P1-4 长度提示
      ├─ P2-1 强化记忆指引
      ├─ P2-2 记忆 skill
      └─ P2-3 用户确认判定（写进 skill）
      （X-1/X-2 同步）

阶段 B（短期记忆，程序内部方法）
  └─ P3-1 daily_append 内部方法
      ├─ P3-2 /clear 触发整理
      └─ P3-3 压缩前保存

阶段 C（搜索，唯一新工具）
  └─ P4-1 FTS5 可用性验证（最先做，决定方案）
      ├─ P4-2 索引构建与增量
      ├─ P4-3 memory_search
      ├─ P4-4 session_search
      ├─ P4-5 搜索策略
      └─ P4-6 工具注册

阶段 D（陪伴引用，模型自律）
  └─ P5-1 Cue 指引写入 skill
      └─ P5-2 冷却状态（可选，先不做）

阶段 E（Follow Up，read+write 管理）
  └─ P6-1 followups.jsonl 结构
      ├─ P6-2 read+write 管理
      └─ P6-3 触发与限制写进 skill

横向：X-3 测试 / X-4 文档（每阶段增量补充）
```

**交付节奏（已确认）**：
- 单元一 = 阶段 A（基础注入 + 指引，对应测试 1/2/3）
- 单元二 = 阶段 B + C（短期记忆 + 搜索，对应测试 4/5）
- 单元三 = 阶段 D + E（陪伴引用 + Follow Up）

---

## 六、技术方案概要（聚焦"做什么" · v2）

### 6.1 记忆注入（P1）

- **做什么**：`ContextBuilder` 增加 USER.md 读取，与 MEMORY.md 并列注入 System Prompt。
- **关键点**：USER.md/MEMORY.md 不存在时返回空串不报错；注入顺序保持 `人设 → 工作区 → USER → MEMORY → 记忆指引 → 技能 → 时间(末尾)` 以保 Prompt Cache。
- **不做**：不新建 memory_add 工具，写入完全靠现有 write_file。

### 6.2 记忆指引（P2）

- **做什么**：把 `_memory_instructions()` 扩展为完整指引 + 单独写 `skills/memory/SKILL.md`。覆盖：何时写、何时不写、用户确认判定、删除/不再提、长度合并。
- **关键点**：用户明确要求"不再提某些记忆"时，指引模型自行 read + write 修改/删除记忆文件。
- **不做**：不搞 Memory Operation JSON 协议、不做程序验证层、不硬拦 write_file。

### 6.3 Daily Memory（P3）

- **做什么**：`daily_append(category, content)` 内部方法，追加到 `daily/YYYY-MM-DD.md`。在 `/clear` 和 `MemoryConsolidation` 压缩前两个触发点调用模型总结后写入。
- **关键点**：会话是长生命周期（按 session_key 缓存），用 `/clear` 作为"会话结束"语义触发点。
- **不做**：不把 daily_append 作为工具暴露给模型（模型若想整理 daily 可用 read+write）。

### 6.4 SQLite FTS5 搜索（P4 · 唯一新工具）

- **做什么**：建 FTS5 虚表索引所有记忆文本 + sessions；提供 `memory_search` / `session_search` 两个工具。
- **索引更新**：write_file 无法 hook，改为**启动时全量重建 + memory_search 调用时按需刷新变更文件**（或定时重建）。
- **FTS5 不可用降级**：`LIKE '%keyword%'` 全表扫描或文件搜索兜底。**P4-1 优先验证**。
- **索引存储**：`<memory根>/index.db`。
- **不做**：不做 embedding（列为后续可选）。

### 6.5 Memory Cue（P5 · 模型自律）

- **做什么**：在记忆 skill 里写 Cue 指引（自然融入、单次 1 条、不炫耀）。
- **冷却**：v2 先不做硬冷却，靠模型自律；若实践中重复骚扰再加 `cue_state.json` 软限制。
- **不做**：不做自动关键词触发，模型自己判断何时调 memory_search。

### 6.6 Follow Up（P6 · read+write）

- **做什么**：`followups.jsonl` 约定结构；模型按 skill 指引用 read_file + write_file 管理（新增追加一行、完成改 status）。
- **风险**：JSONL 覆盖写易出错；若实践频繁写坏再加轻量工具（P6-4 备选）。

---

## 七、风险与注意事项（v2）

| 风险 | 影响 | 应对 |
|---|---|---|
| **写入完全靠模型自律**（P2） | 模型可能写未确认/臆测内容，污染 USER.md/MEMORY.md | 强 System Prompt + 完整 skill 指引；用户可手改文件兜底；用户明确要求"不再提"时模型自行改/删 |
| **FTS5 可能不可用**（P4） | 搜索方案失效 | P4-1 最先验证；不可用降级 LIKE/文件搜索；不影响前序 Phase |
| **write_file 无法 hook 做增量索引**（P4） | 索引可能滞后 | 启动时全量重建 + search 时按需刷新；或定时重建。可接受（记忆文件不大） |
| **会话"结束"无明确事件**（P3） | daily 自动整理触发点不清 | 用 `/clear` + 压缩前两个时机（已确认） |
| **记忆文件并发写**（多实例） | 多实例同 workspace 会竞争 | 沿用现有约定：**每实例独立 workspace**；单实例内每会话串行 |
| **System Prompt 膨胀**（P1） | 注入 USER.md + MEMORY.md 增加 token | USER/MEMORY 有长度上限（模型自律合并）；监控总 token |
| **Prompt Cache 命中率**（P1） | USER.md/MEMORY.md 变动使前缀缓存失效 | 沿用现有设计：易变内容放末尾；USER/MEMORY 变动频率低，可接受 |
| **JSONL 覆盖写易出错**（P6） | Follow Up 文件可能被写坏 | 先 read+write 试；频繁出错再加轻量工具（P6-4 备选） |
| **现有 `MemoryConsolidation` 与 daily 整理边界**（P3） | 两套"整理"逻辑可能重叠 | 明确：`MemoryConsolidation` 管 token 压缩；daily 整理管事件留痕；二者在 `maybe_consolidate` 触发点协作不互相替代 |
| **测试场景需真实模型参与**（X-3） | 测试 3/4/5 涉及模型判断 | 单元测试覆盖规则层；模型行为靠手动验收 |

---

## 八、v2 相对 v1 的关键变化

1. **删除 memory_add/update/remove 工具**（用户决策 2）——写入靠现有 write_file，符合"大道至简"。
2. **Phase 2 从"协议+验证层"降为"prompt/skill 指引强化"**——不搞 Memory Operation JSON，不做程序验证。
3. **daily_append 降为程序内部方法**（用户决策 3）——不作为工具暴露。
4. **Cue 完全交给模型自律**（用户决策 5）——不做自动触发，先不做硬冷却。
5. **Follow Up 用 read+write 管理**——先不建专门工具，P6-4 作为备选。
6. **唯一新增工具收敛为 `memory_search` / `session_search`**——真正的检索新能力。

---

**v2 计划已纳入全部 6 个决策。审核通过后从阶段 A 起步实现（无新工具，仅注入 + 指引 + skill）。**
