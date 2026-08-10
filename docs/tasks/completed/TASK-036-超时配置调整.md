# TASK-036：超时配置调整（Shell 可配 + 单轮墙钟放宽）

## 任务卡

- 状态：已完成
- 负责人：小奈
- 执行会话/子 Agent：本会话
- 基线 commit / 分支：`5302c36`（main，TASK-035 测试目录拆分）
- 依赖任务：无

### 背景

派 code-master 等子 Agent 跑长任务时经常「跑一半停」，需要用户说「继续」才接着跑。排查后确定两个主要限制：

1. **Shell 命令超时硬编码 60 秒**——全量测试等长命令经常触顶（`命令执行超时（60秒）`）；
2. **单轮对话墙钟 `turn_timeout_sec` 过紧**——当前实例 1200s（20 分钟），长任务回合容易超时被强杀。

乖宝 2026-08-10 拍板：Shell 60s→300s 并配置化；单轮 20 分钟→40 分钟。

### 目标

- Shell 命令超时从硬编码 `60` 改为配置项 `shell_timeout_sec`（默认 300s），启动期注入 ExecTool，子 Agent 复用同一实例自动继承
- 当前实例 `turn_timeout_sec` 1200 → 2400（40 分钟）
- Web 配置页可编辑 `shell_timeout_sec`
- 文档同步

### 非目标

- 不动 `max_iterations`（200，已够大）
- 不动普通工具 180s 兜底、生图 600s、视频 30s 等专项超时
- 不引入新的测试框架或 CI

### 允许修改

- `config.py`（新增字段 + 白名单）
- `config.example.json`
- `agent/tools/shell.py`（超时参数化）
- `main.py`（装配时注入 `config.shell_timeout_sec`）
- `webui/index.html`（配置字段）
- `PROJECT.md` / `docs/ARCHITECTURE.md` / `docs/DECISIONS.md`
- 本机 `config.json`（gitignore，不提交）

### 禁止修改

- 不改业务逻辑语义（只放宽超时上限）
- 不改 `max_iterations` / 其它专项超时
- 不提交 `config.json`（本机敏感配置）

### 验收标准

- [x] `shell_timeout_sec` 可配置且默认 300s，`config.py` 白名单 + `config.example.json` 同步
- [x] ExecTool 超时读取配置（默认 300），超时错误信息动态显示秒数
- [x] `main.py` 装配注入 `config.shell_timeout_sec`
- [x] Web 配置页含 `shell_timeout_sec` 字段
- [x] 本机 `config.json`：`turn_timeout_sec=2400`、`shell_timeout_sec=300`
- [x] `import main` / `py_compile` / tools 测试通过
- [x] 文档（PROJECT.md / ARCHITECTURE.md / DECISIONS.md）同步
- [x] 实例已重启，配置生效（乖宝 2026-08-10 确认）

### 必须执行的验证

```bash
.venv/bin/python -m py_compile config.py agent/tools/shell.py main.py
.venv/bin/python -c "import main"
.venv/bin/python -c "from config import load_config; c=load_config(); print(c.turn_timeout_sec, c.shell_timeout_sec)"
.venv/bin/python -m unittest discover -s tests/tools -t .
git diff --check
```

## 执行交接

- 状态：实现完成（乖宝已重启实例生效）
- 改动文件：
  - `config.py`：新增 `shell_timeout_sec: int = 300` + `_CONFIG_FIELDS` 白名单
  - `config.example.json`：`"shell_timeout_sec": 300`
  - `agent/tools/shell.py`：`ExecTool.__init__(workspace, timeout=300)`，超时用 `self.timeout`，错误信息动态
  - `main.py`：`ExecTool(config.workspace, timeout=config.shell_timeout_sec)`
  - `webui/index.html`：配置字段 + FIELDS 白名单
  - `PROJECT.md` / `docs/ARCHITECTURE.md` / `docs/DECISIONS.md`：Shell 超时描述改为可配置
  - 本机 `config.json`（gitignore）：`turn_timeout_sec=2400`、`shell_timeout_sec=300`
- 关键决策：
  - Shell 超时做成启动期配置（工具注册时注入），子 Agent 复用同一 ExecTool 实例自动继承
  - `turn_timeout_sec` 是 AgentLoop 创建时读取，新会话生效；`shell_timeout_sec` 需重启
- 验证结果（本会话实测）：
  - `py_compile` 通过、`import main` 通过
  - `load_config()`：turn_timeout_sec=2400、shell_timeout_sec=300
  - `tests/tools` 通过（ExecTool 危险命令正则测试不受影响）
  - `git diff --check` 通过
- 未验证项：真实长命令触顶验证（依赖运行环境，未刻意触发）
- 风险/遗留：无
- 建议下一步：负责人验收后提交（commit message：`feat: Shell 超时可配置(默认300s)，单轮墙钟调至40分钟（TASK-036）`）

## 负责人验收

- [x] 检查 diff 与授权范围（config/shell/main/webui/example + 3 份文档 + 任务卡，无越界；`config.json` 未纳入）
- [x] 独立复跑关键验证（`py_compile`、`import main`、`load_config` 输出 2400/300、`tests/tools` 通过、`git diff --check` 通过）
- [x] 检查秘密/个人数据/运行产物（无；config.json 本机配置不提交）
- [x] 检查文档与配置一致性（PROJECT.md / ARCHITECTURE.md / DECISIONS.md 已同步）
- [x] 更新 `docs/DECISIONS.md` 中相关状态（Shell 超时已配置化描述已更新）
- 验收结论：通过
- 证据与备注：乖宝已重启实例，配置生效
