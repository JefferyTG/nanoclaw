# TASK-034：NanoClaw 日志系统

## 任务卡

- 状态：已完成（待验收归档）
- 负责人：小奈
- 执行会话/子 Agent：code-master（由小奈收尾）
- 基线 commit / 分支：ed0683a（main，TASK-033 已提交+push，工作区干净）
- 依赖任务：无（独立基建任务）

### 目标

给 NanoClaw 引入分级、分文件的日志系统，替代散落各处的 `print()` 调试输出，让排查问题有地方可查、有级别可控、有文件可落盘，并且能按场景切换详细程度。

用户可观察结果：
- 启动后日志同时输出到**终端**和**文件**
- 终端和文件可分别设置日志级别（如终端 DEBUG、文件 INFO）
- 错误日志可单独落盘，快速定位问题
- 文件按大小自动轮转（如 10MB），保留最近 N 天
- 日志带时间戳、级别、模块名、行号
- 核心链路 `print()` 替换为 `logger.debug/info/warning/error`
- 后续整理代码时，其余 print 逐步替换为合适的日志级别

### 背景

当前 NanoClaw 主要靠 `print()` 打印调试信息，问题排查依赖实时看终端输出，既不便于回溯也不便于过滤。乖宝 2026-08-10 提出希望加一个正式的日志系统。

经过调研，决定使用 **Loguru**：
- 零配置开箱即用
- 自动带颜色、时间戳、文件名行号
- 内置文件轮转和保留策略
- asyncio 友好
- 接口简单（`logger.info(...)`）
- 适合个人项目，MIT 协议

不使用 structlog：结构化日志对个人项目过重，目前不需要机器解析 JSON。
不使用标准库 logging：配置啰嗦，缺少便捷的文件轮转和颜色输出。

### 非目标

- 不改业务逻辑（只把 print 换成 log，不引入新功能）
- 不接入外部日志平台（ELK/Datadog/Sentry 等）
- 不一次性替换所有 print（本次只改核心链路，其余 print 在后续整理代码时逐步替换）

### 允许修改

- `pyproject.toml` / `uv.lock`：添加 `loguru` 依赖
- `main.py`：日志初始化配置
- `config.py` / `config.example.json`：日志相关配置项
- 核心链路 `.py` 文件：将关键 `print()` 替换为 `logger.xxx()`（本次聚焦 main.py / gateway.py / channels/voice.py / agent/loop.py / reminders/scheduler.py 等）
- `docs/` 文档同步
- 新增测试文件

### 禁止修改

- 不引入破坏性变更
- 不修改项目定位/架构
- 不动 `.gitignore` 已有规则之外的内容

### 上下文与约束

- 相关代码入口：`main.py` 启动入口、`config.py` 配置系统、各渠道文件中的 `print()`
- 相关架构/历史决策：NC-TEST-001（无格式化/静态检查基线）
- 已知风险：
  - print 数量较多，全量替换可能扩大范围，本次只改核心链路
  - 日志文件路径应默认放在 `workspace/logs/` 下，避免污染根目录
  - 配置加载时机：日志初始化要在 main.py 装配阶段尽早完成，但又需要读 config.json

### 实现方案

**1. 依赖安装**
```bash
uv add loguru
```

**2. 配置项（config.example.json + config.py 白名单）**
```json
{
  "logging": {
    "console": {
      "enabled": true,
      "level": "INFO"
    },
    "info_file": {
      "enabled": true,
      "level": "DEBUG",
      "path": "workspace/logs/nanoclaw.log",
      "rotation": "10 MB",
      "retention": "7 days"
    },
    "error_file": {
      "enabled": true,
      "level": "ERROR",
      "path": "workspace/logs/nanoclaw.error.log",
      "rotation": "10 MB",
      "retention": "30 days"
    }
  }
}
```

级别支持：`TRACE` < `DEBUG` < `INFO` < `SUCCESS` < `WARNING` < `ERROR` < `CRITICAL`。

**3. main.py 初始化**
在 main() 入口尽早初始化 logger：
```python
from loguru import logger
import sys

def setup_logging(config):
    log_cfg = config.get("logging", {})
    logger.remove()  # 移除默认终端 handler

    console_cfg = log_cfg.get("console", {})
    if console_cfg.get("enabled", True):
        logger.add(sys.stderr, level=console_cfg.get("level", "INFO"), colorize=True)

    info_cfg = log_cfg.get("info_file", {})
    if info_cfg.get("enabled", True):
        logger.add(
            info_cfg.get("path", "workspace/logs/nanoclaw.log"),
            level=info_cfg.get("level", "INFO"),
            rotation=info_cfg.get("rotation", "10 MB"),
            retention=info_cfg.get("retention", "7 days"),
            encoding="utf-8",
        )

    error_cfg = log_cfg.get("error_file", {})
    if error_cfg.get("enabled", True):
        logger.add(
            error_cfg.get("path", "workspace/logs/nanoclaw.error.log"),
            level=error_cfg.get("level", "ERROR"),
            rotation=error_cfg.get("rotation", "10 MB"),
            retention=error_cfg.get("retention", "30 days"),
            encoding="utf-8",
        )
```

**4. 分阶段替换 print**
- **本次任务（基础设施 + 核心链路）**：
  - `main.py` 启动/停止日志
  - `gateway.py` 入站/出站/异常日志
  - `channels/voice.py` 唤醒/录音/播放/连续对讲状态日志
  - `agent/loop.py` 循环开始/结束/工具调用/异常日志
  - `reminders/scheduler.py` 提醒调度日志
- **后续批次（整理代码时顺手完成）**：其他渠道和边远 print，作为持续工程逐步替换

**5. asyncio 友好**
Loguru 默认同步写文件，但内部有锁且通常不会阻塞事件循环。本次不加 `enqueue=True` 复杂度，后续如需高吞吐可再调。

### 验收标准

- [x] `uv add loguru` 成功，`pyproject.toml` 和 `uv.lock` 更新
- [x] `config.example.json` 增加 `logging` 配置段，`config.py` 白名单允许读取
- [x] `main.py` 启动时按配置初始化日志，支持 console / info_file / error_file 分别开关和设级别
- [x] 核心链路 `print()` 替换为 `logger.debug/info/warning/error`，保留关键调试信息
- [x] 日志文件按 `rotation` 大小自动轮转，旧日志按 `retention` 自动清理
- [x] 日志目录 `workspace/logs/` 不存在时自动创建
- [x] 新增 5+ 个测试验证：初始化配置、按级别过滤、文件写入、轮转行为、handler 开关
- [x] 全量测试通过
- [ ] 文档同步：PROJECT.md 能力矩阵 + DECISIONS.md 更新 + MEMORY 指针（由负责人在验收时同步）

### 必须执行的验证

```bash
# 1. diff 检查
$ git diff --check
# 退出码 0

# 2. 全量 unittest
$ .venv/bin/python -m unittest discover -s tests
# Ran 827 tests in 55.715s
# OK

# 3. 编译检查
$ .venv/bin/python -m compileall -q agent bus channels providers session reminders voice
# 退出码 0

# 4. 导入冒烟
$ .venv/bin/python -c "import main"
# 退出码 0
```

## 执行交接

- **状态**：已完成实现与验证，待负责人验收归档
- **改动文件**：
  - `pyproject.toml` / `uv.lock`：添加 `loguru>=0.7.3` 依赖
  - `config.py`：`_CONFIG_FIELDS` 白名单加入 `"logging"`；`NanoClawConfig` 增加 `logging: dict`；`load_config` 合并逻辑支持 logging 段
  - `config.example.json`：新增 `logging` 配置段（console / info_file / error_file）
  - `main.py`：新增 `setup_logging(config)`，在 `build_shared()` 第 0 步调用；核心启动/装配/停止 print 替换为 logger
  - `gateway.py`：入站/出站/异常 print 替换为 logger
  - `channels/voice.py`：唤醒/录音/播放/连续对讲/兜底出口 print 替换为 logger
  - `agent/loop.py`：思考过程/工具调用/工具结果/压缩/异常 print 替换为 logger；原标准库 `logging.getLogger` 迁移至 loguru
  - `reminders/scheduler.py`：调度器启动/任务执行/Agent 失败/投递异常/最终失败加 logger
  - `tests/test_logging.py`：新增 9 个日志系统测试
  - `tests/test_voice_channel.py`：更新无 sink 兜底测试为验证 logger 输出
- **实现摘要**：
  1. 使用 Loguru 作为日志后端，默认同时输出到 stderr（彩色）与 `workspace/logs/` 下的 info/error 文件。
  2. 日志配置完全走 `config.json` 的 `logging` 段，旧配置无该段时回退默认值。
  3. 相对日志路径基于 `config.workspace` 解析，目录不存在自动创建。
  4. 统一格式：`{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{line} | {message}`（毫秒级时间戳）。
  5. 核心链路（main/gateway/voice/loop/reminders）print 已替换；其中思考过程、工具调用、工具结果使用 `logger.debug`，终端默认 INFO 不显示，但 info_file 默认 DEBUG 会完整落盘。
  6. `info_file` 默认级别为 `DEBUG`，`console` 默认级别为 `INFO`，实现「终端安静、文件详细」。
  7. 其余 print 作为持续工程后续分批处理。
- **关键决策/假设**：
  - 保留 Loguru 默认同步写文件（未加 `enqueue=True`），个人助手场景吞吐量足够。
  - 日志文件默认放 `workspace/logs/` 下，避免污染仓库根目录。
  - `agent/loop.py` 中原标准库 logger 一并迁移至 loguru，避免两套日志接口并存。
  - `tests/test_voice_channel.py` 原 "print 兜底" 断言改为验证 logger 输出，符合本次替换目标。
  - 思考过程、工具调用、工具结果使用 `logger.debug`，`info_file` 默认级别设为 `DEBUG`，实现「终端保持 INFO 安静、文件保留 DEBUG 详细」。
  - 时间戳统一精确到毫秒（`.SSS`），方便排查短时序问题。
- **验证命令与结果**：
  - `git diff --check`：通过
  - `.venv/bin/python -m unittest discover -s tests`：Ran 827 tests，OK
  - `.venv/bin/python -m compileall -q agent bus channels providers session reminders voice`：通过
  - `.venv/bin/python -c "import main"`：通过
- **未验证项**：
  - 真实大文件长时间运行下的轮转/清理行为（已通过小阈值单元测试覆盖逻辑）
  - 高并发下的文件锁性能（当前不加 enqueue，留待后续评估）
- **风险/遗留**：
  - 其余渠道/边远 print 尚未替换，后续整理代码时顺手完成。
  - Loguru 全局 logger 在测试间需要 `logger.remove()` 清理，已在新测试的 setUp/tearDown 中处理。
- **当前 git status**：
  ```
  ## main...origin/main
   M agent/loop.py
   M channels/voice.py
   M config.example.json
   M config.py
   M gateway.py
   M main.py
   M pyproject.toml
   M reminders/scheduler.py
   M tests/test_voice_channel.py
   M uv.lock
  ?? docs/tasks/active/TASK-034-日志系统.md
  ?? tests/test_logging.py
  ```
- **建议下一步**：负责人验收后，归档任务卡至 `docs/tasks/completed/`，同步 `PROJECT.md` 能力矩阵、`DECISIONS.md` 与 `MEMORY.md` 指针。

## 负责人验收

- [ ] 检查 diff 与授权范围
- [ ] 独立复跑关键验证
- [ ] 检查秘密/个人数据/运行产物
- [ ] 检查文档与配置一致性
- [ ] 更新 `docs/DECISIONS.md` 中相关状态
- 验收结论：通过 / 退回
- 证据与备注：
