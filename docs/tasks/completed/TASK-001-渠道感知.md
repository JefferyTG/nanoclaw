# TASK-001：渠道感知（Agent 感知当前渠道与用户）

## 任务卡

- 状态：完成 ✅（2026-08-04 乖宝验收通过）
- 负责人：乖宝（验收）
- 执行会话/子 Agent：code-master（实现）
- 基线 commit / 分支：main @ 0cd50de
- 依赖任务：无

### 目标

让 Agent 能感知**当前会话所在渠道**（feishu / weixin / web / cli）与**用户标识**（sender_id），从而支持渠道专属行为与 feishu-sticker 表情包等下游功能。**感知方式为会话级快照（System Prompt 注入），不是每轮消息前缀**——渠道信息在会话内恒定，每轮注入是冗余的。

### 非目标

- 不实现渠道专属行为逻辑本身（如微信/飞书不同口吻、表情包调用）
- 不实现跨渠道会话迁移/识别同一用户
- 不注入 chat_id（v1 不需要，下游需求明确时再补）
- 不安装 feishu-sticker 技能（依赖本能力，另开任务）
- 不改动现有每轮时间戳前缀机制（时间戳每轮变化，必须按轮注入；渠道恒定，走快照）

### 允许修改

- `main.py`（`make_agent_factory`：解析 session_key 得 channel/sender_id，传给 ContextBuilder）
- `agent/context.py`（ContextBuilder 新增渠道快照参数 + System Prompt 新增「当前渠道」section）
- 新增 `tests/test_channel_context.py`（专项测试：不同 session_key → System Prompt 含正确渠道/用户）
- 相应文档（PROJECT.md 能力矩阵 / DECISIONS.md / ARCHITECTURE.md 如有必要）

### 禁止修改

- 渠道适配器（`channels/*`）行为
- `bus/queue.py` 的 DTO 结构
- `gateway.py` 的时间戳注入逻辑
- config 结构

### 上下文与约束

- 相关代码入口：
  - `gateway.py`：`session_key = f"{msg.channel}:{msg.sender_id}"`（渠道+用户已在会话键中）
  - `main.py::make_agent_factory`：`factory(session_key)` 惰性创建 AgentLoop，可解析 session_key
  - `agent/context.py::ContextBuilder`：会话级快照（identity/USER/MEMORY/skills/agents），新增渠道快照完全同构；`build_system_prompt` 拼装 section
  - `tests/test_prompt_cache_context.py`：验证 System Prompt 变更边界，渠道快照不得破坏前缀稳定
- 相关架构/历史决策：
  - DECISIONS.md：Channel/Bus/Gateway/Agent 解耦；「慢变上下文采用显式快照」——identity、USER、MEMORY 都是会话级快照，渠道信息（会话内恒定）属于同类，走快照最合适
  - 时间戳走每轮注入是因为它**每轮变化**，放 System 会过期且破坏缓存；渠道**恒定**，放 System Prompt 完美契合（同一 Agent 的 System Prompt 永不变化 → Prompt Cache 前缀稳定）
  - 渠道感知 = 单向环境元数据注入（Channel → Bus → Gateway → Agent），不产生反向依赖，不破坏解耦
- 已知风险：
  - session_key 分隔符是 `:`，若 sender_id 本身含 `:` 需用 `split(":", 1)` 只切第一刀（参考微信 target 可逆编码的教训）
  - /clear 或新会话会重建 Agent → 快照自动重建，无遗留问题
  - 提醒/定时任务触发的会话（scheduled session）也要有渠道快照（沿用现有 session_key 体系）

### 验收标准

- [x] 新建会话时 System Prompt 含「当前渠道」section（如 `渠道：weixin；用户：<sender_id>`），源自 session_key 解析
- [x] 不同渠道（feishu/weixin/web/cli）各自正确；sender_id 含 `:` 时解析不裂
- [x] System Prompt 在会话内恒定（不破坏 Prompt Cache 前缀稳定性，复用 test_prompt_cache_context 范式）
- [x] 每轮消息头仍只有时间戳前缀（无渠道冗余）
- [x] 新增专项测试覆盖上述场景；全量 unittest 通过
- [x] 文档同步：PROJECT.md 能力矩阵标 ✅、DECISIONS.md 记录决策

### 必须执行的验证

```bash
git diff --check
.venv/bin/python -m unittest discover -s tests
uv run python -m compileall -q gateway agent main
uv run python -c "import main"
```

## 执行交接

- 状态：完成 ✅
- 实际改动文件：`agent/context.py`、`main.py`、`tests/test_channel_context.py`（新增）、`PROJECT.md`、`docs/DECISIONS.md`
- 实现摘要：`ContextBuilder` 新增 `channel`/`sender_id` 构造参数（默认空串，向后兼容），纳入 `refresh_snapshot` 快照并在 `build_system_prompt` 注入「## 当前渠道」section（渠道会话内恒定，System Prompt 前缀稳定）；`make_agent_factory` 的 `factory` 用 `session_key.split(":", 1)` 只切第一刀解析渠道与用户并传入 ContextBuilder；新增 6 个专项测试直接驱动真实 factory 链路
- 关键决策与假设：渠道走会话级快照而非每轮前缀注入（零每轮 token、Prompt Cache 前缀稳定，与 identity/USER/MEMORY 同构）；scheduled 会话（`scheduled:task:exec`）渠道名解析为 `scheduled`，自动获得快照；未配置渠道信息（如共享上下文构建器）不注入 section，避免多余文本
- 验证命令与结果：`git diff --check` ✅（无输出，exit 0）；`.venv/bin/python -m unittest discover -s tests` ✅ 290 tests OK；`uv run python -m compileall -q gateway agent main` ✅ exit 0；`uv run python -c "import main"` ✅ 无输出
- 未验证项：真实渠道端到端（需真实模型/渠道环境）；web/feishu/weixin 各渠道实机会话 System Prompt 内容
- 风险与遗留问题：低。若未来 session_key 格式变化（如引入 chat_id 分隔符）需同步解析逻辑；子 Agent（`_TaskContextBuilder`）未继承渠道快照（任务卡未要求，其 System Prompt 为固定任务提示词）
- commit（仅在获授权时）：未提交（未获授权）
- 当前 `git status --short --branch`：`## main...origin/main` + 未跟踪 `docs/tasks/active/TASK-001-渠道感知.md`、`tests/test_channel_context.py`、`kb-testset/`（新增文件均未跟踪，未做任何提交）
- 建议下一步：负责人验收 diff 与授权范围；确认后由负责人更新中央任务状态；下游 feishu-sticker 技能可基于本能力另开任务

## 负责人验收

- [x] 检查 diff 与授权范围
- [x] 独立复跑关键验证
- [x] 检查秘密/个人数据/运行产物
- [x] 检查文档与配置一致性
- [x] 更新 `docs/DECISIONS.md` 中相关状态
- 验收结论：**通过** ✅（乖宝 2026-08-04 实机验证：渠道感知生效）
- 证据与备注：290 tests OK；专项 6 用例全过；PROJECT.md 能力矩阵 + DECISIONS.md 决策已同步；实机新会话可回答「当前渠道」
