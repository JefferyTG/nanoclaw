# TASK-005：上下文预算动态配置 + 占用显示（Web 进度条 / 渠道命令）

## 任务卡

- 状态：已实现（TASK-005 代码+单测完成，待项目负责人验收归档）
- 负责人：code-master（实现）＋ 小奈（设计/验收）
- 基线 commit / 分支：`eece4ab`（main，TASK-004 已归档）
- 依赖任务：无（TASK-004 已完成的 memory_sync 与本任务无耦合）

### 背景

- 当前 `MemoryConsolidation` 压缩阈值硬编码 `192_000`（main.py:510），而 deepseek-v4-flash 原生支持 **1M token 上下文**，仅用 ~19%。
- 乖宝决策：预算扩到 **512k**，且改为**配置文件动态可调**。
- 上下文占用与缓存命中数据已存在（loop.py:640 `response.cache_usage` → `last_cache_metrics`），但未展示给用户。
- 需求：Web 端显示占用进度条 + 缓存命中率；微信/飞书/CLI 增加命令查看当前会话占用。

### 目标

1. **动态配置**：`config.json` 新增 `context_budget_tokens`（默认 524288 = 512k），main.py 读配置传给 `MemoryConsolidation`，替换硬编码 192k。修改配置重启生效（与 asr_model 等启动期配置一致）。
2. **Web 占用显示**：AgentLoop 每次模型调用后，把真实 usage（`input_tokens` / `cached_input_tokens` / `uncached_input_tokens` / 预算 / 占用比 / 缓存命中率）通过 `stream_sink` 推送 `{type: "usage", ...}` 事件；`webui/index.html` 在输入框上方渲染进度条 + 小字（含缓存命中率），随会话切换各自显示。
3. **渠道命令**：新增 `/context` 命令，web / feishu / weixin / cli 识别后直接从 AgentLoop 查询当前会话占用并**直接回复文本（不经过模型）**。内容含：历史/系统估算 token、上次真实 input_tokens、预算、占用百分比、缓存命中率。

### 非目标

- ❌ Web 配置页热更新 `context_budget_tokens`（启动期配置，需重启）
- ❌ 修改 TASK-004 记忆同步机制
- ❌ 精确 tokenizer（沿用现有 CJK 估算；真实值以 provider 返回 usage 为准）
- ❌ 多实例共享预算（每实例独立配置/workspace 不变）

### 方案要点

- **配置**：`config.py` 增加 `context_budget_tokens: int = 524288`（白名单字段）；`config.json` 写入该字段；main.py 装配 `MemoryConsolidation(token_budget=config.context_budget_tokens)`。旧 config 缺字段时用默认值，向后兼容。
- **usage 事件**：`agent/loop.py` 在每次模型响应拿到 `cache_usage` 后，若 `stream_sink` 存在则推 `{"type":"usage","input_tokens":...,"cached":...,"uncached":...,"budget":...,"ratio":...,"cache_ratio":...}`。Web 前端 `webui/index.html` 处理该事件渲染进度条。
- **占用查询接口**：`AgentLoop` 新增公开方法（如 `get_context_usage() -> dict`），返回：估算的 System 段 token、估算的历史 token、上次真实 `input_tokens`/`cached`（无则 None）、`token_budget`、占用比、缓存命中率。命令回调由此获取数据。
- **命令分发**：各渠道在既有内置命令解析处新增 `/context` 分支：
  - `channels/web.py::_parse_command`
  - `channels/feishu.py::_try_handle_command`
  - `channels/weixin.py`（微信命令处理处）
  - `channels/cli.py`（内置命令处）
  - 渠道需持有/注入一个「占用查询回调」（与现有 `_clear_callback` 同构），由 main.py 装配时绑定到 AgentLoop。
- **回复格式**（示例）：
  ```
  当前会话上下文占用：
  · 预算 512k（524288 tokens）
  · 上次请求 input_tokens：45,230（缓存命中 82%）
  · 估算：System ~2.1k + 历史 ~41k
  · 占用比：约 8.8%
  ```

### 允许修改

- `config.py`（新增配置项 + 白名单）
- `config.json`（写入默认 524288）
- `main.py`（读配置传给 MemoryConsolidation；绑定占用查询回调到各渠道）
- `agent/loop.py`（usage 事件推送 + `get_context_usage()`）
- `channels/web.py` / `channels/feishu.py` / `channels/weixin.py` / `channels/cli.py`（/context 命令）
- `webui/index.html`（进度条渲染）
- `tests/`（新增单测：配置读取、usage 事件、命令回复、get_context_usage）

### 禁止修改

- 与记忆同步相关的模块行为（memory_sync / 补丁机制）
- 提醒、生图、视频、MCP 等无关模块
- System Prompt 稳定前缀

### 验收标准

- [x] `config.json` 设 `context_budget_tokens: 524288` 后，`MemoryConsolidation.token_budget == 524288`；删除配置项时回退默认值且不报错（向后兼容）
- [x] 每轮模型调用后，Web 收到 `{type:"usage"}` 流事件，前端进度条与缓存命中率更新
- [x] `/context` 命令在 web/feishu/weixin/cli 四渠道均直接回复占用文本（不经过模型）；命令在会话内显示当前会话占用
- [x] 无 stream_sink / 无 usage 数据时优雅降级（进度条不显示或显示估算，不报错）
- [x] 单元测试 + 集成测试全过（unittest，350 通过）；`git diff --check` 通过；`compileall` 通过；`import main` 通过
- [x] 文档同步：DECISIONS.md 记录决策；任务卡状态推进；PROJECT.md 能力矩阵/配置速查同步更新

### 必须执行的验证

```bash
git diff --check
.venv/bin/python -m unittest discover -s tests
uv run python -m compileall -q agent bus channels providers session
uv run python -c "import main"
```

### 风险与遗留

- 预算调大后单次请求的上下文更长（flash 成本低，风险可控）；压缩触发点后移，历史更长时首次压缩可能更慢
- Web 进度条需处理「估算值 vs 真实值」两级刷新；真实 usage 缺失（provider 未返回）时回退估算
- 微信命令需与 Bridge 侧文本命令识别对齐（确认微信渠道命令已能直通文本，不经过模型）
- `context_budget_tokens` 属启动期配置，改后需重启（已在非目标注明）

### 建议下一步

乖宝确认命令名（默认 `/context`）与预算默认值（默认 524288）后，说「开始」→ 派遣 code-master 实施。
