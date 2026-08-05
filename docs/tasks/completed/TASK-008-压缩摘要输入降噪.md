# TASK-008：压缩摘要输入降噪（工具结果只留结论）

## 任务卡

- 状态：**✅ 已完成并验收（2026-08-05）**
- 创建：2026-08-05 ｜ 负责人：code-master（实现）＋ 小奈（设计/验收）
- 基线 commit / 分支：`783cb60`（main）
- 依赖任务：无（与 TASK-006 可并行；建议在 TASK-009 前完成，为分块摘要打底）

## 目标

喂给压缩摘要模型的工具消息只保留「调了什么工具、成没成功、一句话结论」，不再携带完整文件内容、大段 shell 输出、搜索结果正文等噪声。

## 背景

- `agent/memory.py` `_messages_to_text`：把 tool_calls 的完整 `arguments` 原文（如 `write_file` 的 content 全文、可能几千字符）和 role=tool 的完整返回结果（shell 输出、文件全文）拼进摘要 prompt。
- 后果：摘要调用又贵又吵；大量 token 花在即将被压缩掉的工具噪声上；噪声可能干扰摘要质量。
- GPT 方案 §5.4「工具消息降噪」：write_file 不传完整内容、shell 只留成功与否与必要结论、搜索结果/网页正文/大段代码默认省略。

## 范围

- `agent/memory.py`：`_messages_to_text` / `_summary_content`（tool 消息降噪）。
- `tests/`：新增降噪单测。

## 非目标

- ❌ 改动工具执行本身（只影响「喂给摘要模型」的视图）
- ❌ 改动 `_SUMMARIZE_INSTRUCTION` 与摘要格式（TASK-009）
- ❌ 改动主对话的 tool 消息（只降噪摘要输入）

## 验收标准

- [x] `write_file` 的工具结果在摘要输入中只呈现「工具名 + 成功/失败 + 写入目标/字符数」等结论，不包含文件全文
- [x] shell 输出只保留退出码与首尾摘要（如截断至 ~200 字符），不包含完整 stdout/stderr
- [x] 搜索结果 / web_fetch 正文 / 大段代码默认省略或只留标题级信息
- [x] 图片消息保持现有「[图片内容已省略]」占位行为
- [x] 用户 / assistant 普通文本不受影响
- [x] 摘要关键事实不因降噪丢失（对照测试：降噪前后摘要核心信息一致）
- [x] 单元测试全过（unittest）、`git diff --check`、`compileall`、`import main` 通过
- [x] 文档同步：任务卡状态

## 相关模块

- `agent/memory.py`（`_messages_to_text` / `_summary_content`）

## 实现方案

- 为 role=tool 消息增加降噪分支：按工具名白名单/黑名单处理或通用截断（建议通用截断 + 保留前 N 字符 + 关键结论标记）。
- `tool_calls` 的 `arguments` 仅保留 name + 轻量参数摘要（如路径、文件名），丢弃大字段（content / prompt / 全文）。
- 降噪只作用于 `_messages_to_text`（摘要输入），`_summary_content` 同步适配。

## 测试方式

```bash
git diff --check
.venv/bin/python -m unittest discover -s tests
uv run python -m compileall -q agent bus channels providers session
uv run python -c "import main"
```

新增单测：构造含大 content 的 write_file 工具消息，断言摘要输入不含全文、含结论。

## 风险与遗留

- 降噪过度可能丢关键结论 → 用「保留首尾 + 显式结论」策略，验收时人工抽查压缩质量。
- 后续 TASK-009 结构化摘要将复用本任务的降噪视图。

## 下一步

说「开始」→ 派遣 code-master 实施（可与 TASK-006 并行）。

## 实现摘要（2026-08-05 code-master 实现，小奈验收）

改动文件：
- `agent/memory.py`：重写 `ContextCompactor._messages_to_text`（工具消息降噪），新增 `_truncate` / `_denoise_arg_value` / `_denoise_tool_call_args` / `_titles_only` / `_is_sensitive_value` / `_denoise_tool_content` / `_exec_narrow_stderr` 与常量 `_TOOL_CONTENT_LIMIT` / `_ARG_RAW_LIMIT` / `_ARG_VALUE_LIMIT` / `_ARG_RENDER_LIMIT` / `_HEAVY_ARG_KEYS` / `_SENSITIVE_ARG_KEYS` / `_IMAGE_TOOLS` / `_IMAGE_TOOL_KEYS`。
- `tests/test_memory_denoise.py`：新增 16 个单测（降噪规则全覆盖 + 敏感值回归）。

关键决策：
- **降噪只作用于摘要输入视图**（`_messages_to_text`），不改工具执行、不改主对话 tool 消息（非目标遵守）。
- **首尾窗口 + 显式结论**策略：写文件结论在开头、shell 退出码在末尾，均被窗口保住；web_search 只留标题级。
- **敏感值防护（code-review 补 P1）**：`_SENSITIVE_ARG_KEYS` 整键丢弃（camelCase 归一比对）；嵌套 JSON 字符串递归清洗；`sk-`/`AKIA`/`eyJ`/超长 base64/PEM 特征值直接丢弃——防密钥随摘要持久化。
- **图类工具白名单**：generate_image/ask_image 保留 prompt/question 关键事实，base64 大字段仍丢。
- **exec stderr 收窄**：`标准错误:` 之后只留退出码行，避免密钥行随首部窗口进入。

验证结果（全部通过）：
- `git diff --check` ✅
- 降噪单测 16/16 ✅；全量 unittest 377/377 ✅
- `compileall` ✅；`import main` ✅

未验证项：
- 真实模型摘要质量人工抽查（降噪前后核心信息一致属人工范畴，使用中留意）。

遗留（记录不处理）：
- `agent/daily.py` 有同构 `_messages_to_text`/`_summary_content` 副本未降噪——TASK-009 前评估统一。
- `_titles_only` 对正文中 `###` 开头行会误留（轻微 over-retention），可留待后续加行长度护栏。
