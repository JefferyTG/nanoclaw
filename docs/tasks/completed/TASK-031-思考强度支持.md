# TASK-031：思考强度参数支持（reasoning_effort + thinking_budget）

## 任务卡

- 状态：**已完成**
- 负责人：小奈 + code-master
- 执行会话/子 Agent：`spawn_subagent` 实施
- 基线 commit / 分支：`4dbfbc0`（TASK-028）→ 当前 main
- 依赖任务：无

### 目标

为 NanoClaw 的 `OpenAICompatProvider` 增加 `reasoning_effort` 和 `thinking_budget` 参数的传递能力，使用户可通过 `config.json` 配置模型的思考强度（推理深度 / 思考 token 预算），默认先设为 `high`。

### 非目标

- 不改动子 Agent 模型的思考强度（`subagent_model` 独立配置，本次不涉及）
- 不改动无害/非主对话模型的思考强度（视觉/ASR/生图等不变）
- 不解决中转站截断 `reasoning_content` 的问题（那是中转站的事）
- 不新增 UI 端的思考强度调节界面

### 允许修改

- ✅ `config.py`：`NanoClawConfig` 新增 `reasoning_effort` 和 `thinking_budget` 字段
- ✅ `providers/openai_compat.py`：`chat()` 和 `chat_stream()` 的 `request_kwargs` 中带上这两个参数
- ✅ `providers/base.py`：`LLMProvider` 基类 `chat()` / `chat_stream()` 方法签名增加可选参数
- ✅ `config.json`：新增 `reasoning_effort`（默认 `"high"`）和 `thinking_budget`（`null`，可选）
- ✅ `main.py`：3 处 `OpenAICompatProvider` 实例化处透传配置

### 验收标准

- [x] `config.json` 可配置 `reasoning_effort` 和 `thinking_budget`
- [x] 配置 `reasoning_effort: "high"` 后，请求中包含 `"reasoning_effort": "high"`
- [x] 配置 `thinking_budget: 4096` 后，请求中包含 `"thinking_budget": 4096`
- [x] 不配置时（默认 `high` / `None`），行为同旧版（`None` 不传，`high` 默认传）
- [x] 空字符串 `""` 时不传 `reasoning_effort`（兼容无思考能力的模型）
- [x] 重启后新会话生效（启动期配置，改后需重启）
- [x] 所有现有测试通过（725 tests OK）

### 实现摘要

**改动文件：**

| 文件 | 改动 |
|---|---|
| `config.py` | `NanoClawConfig` 新增 `reasoning_effort: str = "high"` 和 `thinking_budget: Optional[int] = None`；`_CONFIG_FIELDS` 追加两项 |
| `providers/base.py` | `LLMProvider.chat()` / `chat_stream()` 签名增加 `reasoning_effort` 和 `thinking_budget` 可选参数；默认实现透传 |
| `providers/openai_compat.py` | `__init__` 接收两参数；新增 `_add_reasoning_params()` 辅助方法；`chat()` / `chat_stream()` 的条件性传参（优先方法级参数，回退实例属性） |
| `main.py` | 3 处 `OpenAICompatProvider` 构造传参（`getattr` 安全读取，兼容旧配置） |
| `config.json` | 添加 `"reasoning_effort": "high"` 和 `"thinking_budget": null` |

**关键设计决策：**
- `reasoning_effort` 默认 `"high"`（开启思考）；显式配空字符串 `""` 时不传，兼容不支持思考的模型/中转站
- `thinking_budget` 默认 `None`（不传）；配具体整数时才传
- `chat()` / `chat_stream()` 方法级参数优先于实例属性，为未来子 Agent 差异化思考强度留接口
- 测试文件中的 Mock Provider 继承签名变动自动适配，无需修改测试逻辑

**遗留问题：**
- 中转站 `https://chat.ai666.net/api/codex` 是否支持这两个参数未实测，若不支持可切回 DeepSeek 官方验证

### 必须执行的验证

```bash
✅ git diff --check
✅ .venv/bin/python -m compileall -q config.py providers/ main.py
✅ .venv/bin/python -c "import main"
✅ .venv/bin/python -m unittest discover -s tests  # 725 passed
```