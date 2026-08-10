# TASK-035：测试目录按模块重构

## 任务卡

- 状态：已完成
- 负责人：小奈
- 执行会话/子 Agent：本会话
- 基线 commit / 分支：`b3a7f3f`（main，docs: PROJECT.md 里程碑更新至 TASK-034）
- 依赖任务：TASK-034（日志系统，已归档）

### 目标

把当前平铺的 `tests/` 目录按业务模块拆分为子目录结构，让日常开发可以只跑相关模块测试，不必每次都被 voice 测试外放声音，也便于后续持续补充各模块回归测试。

用户可观察结果：
- 测试文件按模块分到不同子目录
- 常用模块可以单独跑测试（如 `tests/agent/`、`tests/channels/voice/`）
- `unittest discover -s tests -t .` 仍能发现全部测试
- 全量测试行为不变，仍可作为回归基线
- 文档中记录按模块跑测试的命令

### 非目标

- 不修改任何业务代码
- 不改测试逻辑本身（只移动文件、调整导入路径）
- 不增加新测试用例（移动整理为主）
- 不引入 pytest 等新测试框架
- 不改动 `.gitignore` 已有规则之外的内容

### 允许修改

- `tests/` 目录下所有测试文件的物理位置
- 测试文件内部的相对/绝对导入路径
- `docs/DEVELOPMENT.md` 验证矩阵与命令速查
- `PROJECT.md` 命令速查（如有必要）
- 新增空的 `tests/*/__init__.py` 保证包发现

### 禁止修改

- 不动 `agent/`、`channels/`、`gateway.py` 等业务代码
- 不改 `pyproject.toml` 测试相关配置（当前无 pytest 等配置）
- 不删除任何既有测试
- 不破坏 `unittest discover -s tests -t .` 的全量发现能力

### 上下文与约束

- 当前 `tests/` 下约 60+ 个测试文件平铺，命名带模块前缀（`test_voice_*.py`、`test_weixin_*.py`、`test_memory_*.py` 等）
- 项目使用标准库 `unittest`，命令为 `.venv/bin/python -m unittest discover -s tests -t .`（TASK-035 起统一加 `-t .`：测试子目录与生产包/模块同名，以项目根为 top-level 避免遮蔽）
- voice 相关测试会调用 `play_audio()`，跑全量时会外放声音，日常开发较吵
- `tests/__init__.py` 已存在，子目录需要各自的 `__init__.py`
- 测试文件内部使用顶层导入（如 `from channels.voice import VoiceChannel`），移动后路径不变，一般无需改动
- 相关历史决策：NC-TEST-001（无格式化/静态检查基线）

### 建议目录结构

```text
tests/
├── __init__.py
├── agent/                 # AgentLoop、记忆、上下文、Prompt Cache 等
│   ├── __init__.py
│   ├── test_agent_cancel_history.py
│   ├── test_cache_observability.py
│   ├── test_context_budget.py
│   ├── test_daily_dream.py
│   ├── test_dream_retry.py
│   ├── test_dream_scheduler.py
│   ├── test_identity_bootstrap.py
│   ├── test_memory_cache_boundary.py
│   ├── test_memory_denoise.py
│   ├── test_memory_structured_summary.py
│   ├── test_memory_sync.py
│   ├── test_prompt_cache_context.py
│   ├── test_prompt_cache_history.py
│   ├── test_prompt_cache_loop.py
│   ├── test_prompt_cache_usage.py
│   ├── test_save_messages_timestamps.py
│   └── test_search_freshness.py
├── channels/              # 各渠道测试
│   ├── __init__.py
│   ├── voice/             # 会外放声音的 voice 测试集中放这里
│   │   ├── __init__.py
│   │   ├── test_voice_channel.py
│   │   ├── test_voice_continuous.py
│   │   ├── test_voice_idle_split.py
│   │   ├── test_voice_incremental_segments.py
│   │   ├── test_voice_kws.py
│   │   ├── test_voice_main.py
│   │   ├── test_voice_normalize.py
│   │   ├── test_voice_prune.py
│   │   ├── test_voice_sanitize.py
│   │   ├── test_voice_segment_playback.py
│   │   ├── test_voice_segments.py
│   │   ├── test_voice_streaming.py
│   │   ├── test_voice_tts_reply.py
│   │   ├── test_voice_vad.py
│   │   ├── test_voice_wake_cache.py
│   │   ├── test_voice_wake_idle_split.py
│   │   └── test_voice_wake_reply.py
│   ├── feishu/
│   │   ├── __init__.py
│   │   ├── test_feishu_audio.py
│   │   ├── test_feishu_images.py
│   │   └── test_feishu_voice_reply.py
│   └── weixin/
│       ├── __init__.py
│       ├── test_weixin_channel.py
│       ├── test_weixin_config.py
│       ├── test_weixin_files.py
│       ├── test_weixin_main.py
│       └── test_weixin_typing.py
├── gateway/               # Gateway 相关
│   ├── __init__.py
│   ├── test_gateway_cancel.py
│   ├── test_gateway_timestamp.py
│   ├── test_gateway_voice_stream.py
│   └── test_outbound_images.py
├── tools/                 # 工具、Shell、生图、视频等
│   ├── __init__.py
│   ├── test_current_time_tool.py
│   ├── test_filestore.py
│   ├── test_shell_tool.py
│   ├── test_tool_history.py
│   ├── test_tool_schema_cache.py
│   ├── test_video_tool.py
│   ├── test_web_fetch.py
│   └── test_web_search.py
├── config/                # 配置相关
│   ├── __init__.py
│   ├── test_channel_context.py
│   ├── test_cli_shutdown.py
│   ├── test_linux_control.py
│   ├── test_tavily_config.py
│   ├── test_timezone_config.py
│   └── test_weixin_config.py
├── reminders/             # 提醒调度
│   ├── __init__.py
│   └── （已存在）
├── subagents/             # 子 Agent 相关（已存在，保留）
│   └── ...
├── integration/           # 跨模块集成
│   ├── __init__.py
│   ├── test_interrupted_turn_resume.py
│   ├── test_mcp_cache_boundary.py
│   └── test_feishu_voice_reply.py
├── system/                # 日志、全量基线等横向基础设施
│   ├── __init__.py
│   └── test_logging.py
└── voice/                 # ASR/TTS/Web 语音核心（已存在，保留）
    ├── __init__.py
    └── ...
```

> 以上为建议结构，开始任务时应根据实际文件列表调整；归类边界不明确的测试可暂放 `integration/` 或保持顶层。

### 验收标准

- [x] `tests/` 下测试文件按模块拆分到子目录
- [x] 每个子目录都有 `__init__.py`
- [x] 所有测试文件内部导入路径修正后无报错
- [x] `.venv/bin/python -m unittest discover -s tests -t .` 仍能发现全部测试（数量不减少）
- [x] 常用模块可单独跑：
  - `.venv/bin/python -m unittest discover -s tests/agent -t . -v`
  - `.venv/bin/python -m unittest discover -s tests/channels/voice -t . -v`
  - `.venv/bin/python -m unittest discover -s tests/channels/weixin -t . -v`
  - `.venv/bin/python -m unittest discover -s tests/gateway -t . -v`
  - `.venv/bin/python -m unittest discover -s tests/tools -t . -v`
  - `.venv/bin/python -m unittest discover -s tests/config -t . -v`
- [x] `git diff --check` 通过
- [x] `docs/DEVELOPMENT.md` 验证矩阵更新，增加「按模块跑测试」说明
- [x] `PROJECT.md` 命令速查如有测试相关命令则同步更新
- [x] 全量测试通过（作为最终回归）

### 必须执行的验证

```bash
# 1. diff 检查
git diff --check

# 2. 全量测试仍可发现
.venv/bin/python -m unittest discover -s tests -t . -v 2>&1 | tail -20

# 3. 各模块测试可独立跑
.venv/bin/python -m unittest discover -s tests/agent -t . -v
.venv/bin/python -m unittest discover -s tests/channels/voice -t . -v
.venv/bin/python -m unittest discover -s tests/channels/weixin -t . -v
.venv/bin/python -m unittest discover -s tests/gateway -t . -v
.venv/bin/python -m unittest discover -s tests/tools -t . -v
.venv/bin/python -m unittest discover -s tests/config -t . -v

# 4. 编译/导入检查
.venv/bin/python -m compileall -q tests
.venv/bin/python -c "import main"
```

## 执行交接

- 状态：实现完成，待全量回归与负责人验收
- 基线：`b3a7f3f`
- 改动文件：
  - 移动 68 个顶层测试文件到 `tests/{agent,channels/{voice,feishu,weixin},config,gateway,tools,integration,system}/`
  - 修正 2 处依赖文件位置的路径：`tests/agent/test_context_budget.py`、`tests/config/test_linux_control.py`（`parents[1]→parents[2]`）
  - 新增 11 个子目录 `__init__.py`（channels 及其三个子目录、gateway、config、tools、integration、system、agent、reminders）
  - `docs/DEVELOPMENT.md` §5.1 新增「按模块跑测试」；`PROJECT.md` 命令速查同步
- 关键决策：
  - 测试命令统一加 `-t .`（项目根为 top-level）：测试子目录与生产包/模块同名，`unittest` 只递归有 `__init__.py` 的目录，而测试包又必须能 import/patch 到生产代码，`-t .` 可同时满足「全量发现」与「生产模块优先」
  - 子目录 `__init__.py` 一律空包（不重导出、不扩 `__path__`，仅保证 discover 发现；原有 `tests/voice/__init__.py` 的 `__path__` 扩展保留不动）
- 验证（本会话执行）：
  - 各模块单跑全过：agent 200 / channels/voice 250 / channels/weixin 81 / channels/feishu 30 / reminders 65 / voice 71 / system 9 等
  - 全量 `discover -s tests -t .`：见「负责人验收」下方结果
  - `git diff --check` 通过
  - 待跑：`compileall`、`import main`（见验收证据）
- 风险/遗留：
  - 不再支持不带 `-t .` 的 `discover -s tests`（会产生遮蔽或漏发现），文档已统一改用带 `-t .` 命令
  - `tests/` 下 `.DS_Store` 为 macOS 产物（已被 gitignore 忽略），未纳入改动
- 建议下一步：负责人复跑验收清单后归档

## 负责人验收

- [x] 检查 diff 与授权范围（62 个 rename + 11 个 `__init__.py` + 2 处路径修正 + 2 份文档，无越界）
- [x] 独立复跑关键验证（全量 `discover -s tests -t .` = 892 OK；模块单跑 6 项 OK；`git diff --check`、`compileall`、`import main` 全过）
- [x] 检查秘密/个人数据/运行产物（无；`tests/` 下 `.DS_Store` 已被 gitignore 忽略）
- [x] 检查文档与配置一致性（`docs/DEVELOPMENT.md` §5.1、`PROJECT.md` 命令速查已同步）
- [x] 更新 `docs/DECISIONS.md` 中相关状态（无需，纯测试目录重构，无新决策/遗留）
- 验收结论：通过
- 证据与备注：892 tests OK（agent 200 / channels/voice 250 / channels/weixin 81 / channels/feishu 30 / reminders 65 / voice 71 / system 9 等）；命令统一 `-t .` 避免子目录遮蔽生产包/模块
