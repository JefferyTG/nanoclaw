# TASK-047-dsh会话编排工具

> 状态：已完成（2026-08-15 归档）
> 创建：2026-08-14 ｜ 负责人：小奈（实现）/ 乖宝（验收）
> 基线 commit：`b2612f0`（TASK-046 提交）
> 备注：工作区存在 `config.example.json` 未提交修改（乖宝 08-14 的改动），本任务不触碰该文件

## 实现摘要（归档）

**改动文件**：`agent/tools/dsh_session.py`（新增，DshSessionTool）、`main.py`（+3 行注册）、`skills/dsh-coder/SKILL.md`（新增）、`tests/tools/test_dsh_session.py`（新增，17 单测）、`PROJECT.md`（能力矩阵+1 行）、`docs/DECISIONS.md`（+1 决策行）、本任务卡（归档）。

**关键决策**：
- 走 DSH 官方 `/api` 直连（本机 127.0.0.1:3080），不引入社区 MCP/ACP 包（太新、star≈0）
- 工具无状态设计：增量轮询的游标（before_seq）由调用方持有，跨回合/并发安全
- workspace 构造时 `os.path.abspath` 规范化（config.workspace 默认相对路径 "."，DSH 要求绝对路径）
- SKILL.md 只写行为机制，不含实例人设（AGENTS.md 约定）

**验证结果**：全量 1001 tests OK（08-14 修复后）；17 个 dsh_session 单测（mock HTTP）；真机冒烟 3 连（list/prompt/read/cancel 对运行中 3080）；启动冒烟通过（临时配置关渠道，`dsh_session` 出现在注册工具列表，优雅退出）。

**遗留问题**：长任务跨回合轮询（reminders 闹钟接力）未做，留候选任务；`~/.dsh/sessions/` 无自动清理，需人工/脚本清理（skill 已记录）。

**补丁（2026-08-15 真机发现）——DSH 审批挂起**：DSH Agent 越界操作（写项目外路径等）会发 `approval/asked` 挂起（策略 ask、应答者=Web 界面用户），宿主 Agent 无法通过 /api 批准（无审批方法）→ read 永远"还在干活"。修复：① 工具 read 检测 `approval/asked` 事件并明确提示（工具名/原因/三种处理方式），18 单测；② DSH 侧治本方案（审批策略改 never，越界确定性拒绝不挂起）待乖宝确认后改 `~/.dsh/profiles/web/cordis.patch.yml`。全量 1002 tests OK。

**补丁 2（2026-08-15，乖宝需求）——远程审批应答**：需求场景=用户不在家时经小奈远程干活，DSH 要权限时由用户经小奈批准/拒绝。实测打通 DSH 审批应答协议：下行 WS `/api/events.mux` 推送 `approval/requested` 帧（server-request 信封，含 rpcId+approvalId，**只推送一次不重放**）；应答 = POST `/api/respond`（client-response 信封，rpcId 回填帧值，value={sessionId, approvalId, outcome:"allowed-once"|"rejected"}）→ 返回 `{"accepted":true}`（**无 result 包装**）。实现：工具常驻 WS 监听（懒启动 asyncio 后台任务，断线重连，pending 表 approvalId→rpcId）+ 新 action `approve`/`reject`（先问用户再应答）+ read 审批提示带审批 ID。6 新单测（24 总），真机闭环验证：审批挂起 → read 提示 → approve → Agent 继续并成功写入。全量 1008 tests OK。测试残留文件已清理。

## 实现进展（2026-08-14）

- [x] `agent/tools/dsh_session.py` 完成（DshSessionTool：list/prompt/read/cancel，RPC 信封 + 本地增量过滤）
- [x] `main.py` 注册（import + `tools.register(DshSessionTool(config.workspace))`）
- [x] `skills/dsh-coder/SKILL.md` 完成（仿 codebuddy-coder 骨架）
- [x] `tests/tools/test_dsh_session.py` 16 个单测全过（mock HTTP，不发真实请求）
- [x] 真机冒烟通过（对运行中的 3080）：
  - list：本项目会话排前、标题/状态正确显示
  - prompt 自动建会话 → accepted；read 第 2 次轮询拿到真实回复（状态"已完成"）
  - cancel 兜底正常
  - 踩坑记录：**DSH 的 `beforeSeq` 参数是"往前翻页"语义（取 seq < beforeSeq 的旧事件），不是增量起点**——增量过滤改为本地按 seq 做（before_seq 仅作过滤起点，不传给 API）。冒烟时 `beforeSeq=0` 曾导致返回空事件
- [x] 全量测试：`.venv/bin/python -m unittest discover -s tests -t .` → Ran 1000 tests OK（51s，含新增 16 个）
- [x] `git diff --check` 通过；`compileall -q agent` 通过；`import main` 通过
- [x] **Bug 修复（2026-08-14 乖宝真机发现）**：`config.workspace` 默认相对路径 `"."`，而 DSH `session.create` 校验 cwd 必须绝对路径（`isAbsolute` 否则报 `session header cwd must be an absolute path`）→ prompt 自动建会话挂、list 不受影响。修复：工具构造时 `os.path.abspath(workspace)` 规范化；新增回归单测 `test_prompt_normalizes_relative_workspace_to_absolute`（17 个单测）。真机复验：`workspace='.'` 构造 → 建会话成功 → read 拿到回复。全量 Ran 1001 tests OK
  - 复盘：首轮冒烟手写绝对路径、未按 main.py 真实装配（`config.workspace`）验证，属冒烟覆盖缺口——验收时应以真实配置路径复跑

## 目标

让小奈获得「对话式编排 DeepSeek Harness」的编码外援能力：新增 `dsh_session` 工具 + `dsh-coder` 技能，小奈可对本机常驻的 DSH web（`http://127.0.0.1:3080/api`）发消息、增量读回复、多轮追问纠偏，替代/并行 codebuddy 的编码委托。

## 背景

- 用户（乖宝）调研了 codebuddy → dsh 切换可行性（2026-08-14 会话），结论：**DSH 官方无 MCP server 端、无 ACP，但 web profile 自带本机 HTTP RPC API（`/api`）已实测可用**（session.create/prompt/history/cancel/fork 等，无认证、绑定 127.0.0.1）。
- 社区已有 MCP/ACP 包装（`dsh-harness-mcp-server`、`deepseek-harness-acp`、`deepseek-harness-for-codex`），但均为数天前发布、star/下载量≈0，不作为主方案。
- 最小化改造走**官方 /api 直连**：1 个工具 + 1 个 skill + 1 行注册 + 单测。不做闹钟/reminders 联动（长任务轮询接力列为后续候选任务）。
- 实测协议事实（2026-08-14 对正在运行的 3080 验证）：
  - 信封：`POST /api/<method>`，body `{"type":"client-request","rpcId":"<uuid>","method":"...","payload":{...}}`
  - `session.list` → `result.value.items[]`（含 sessionId/cwd/title/running）
  - `session.create` → `{cwd}`（或 workspaceId 二选一）→ `value.sessionId`
  - `session.prompt` → `{sessionId, mode:"queue"|"steer", content:[{type:"text",text}]}` → `{accepted:true}`
  - `session.history` → `value.events[]`（`assistant/message` 文本在 `event.data.message.content[].text`；`turn/end` 表示回合完成；事件带单调 `seq`）
  - 增量读取：客户端记 `before_seq`，只取 `seq > before_seq` 的事件（工具保持无状态）
- 演示会话 `session-4f841669-...`（标题"阅读项目文档并概括"）已完整验证多轮往返 + 上下文延续（DSH 记得上一轮读过什么）。

## 范围

- **新增** `agent/tools/dsh_session.py`：`DshSessionTool`（继承 `agent.tools.base.Tool`，照 ExecTool 骨架），action 四合一：`list` / `prompt` / `read` / `cancel`。
- **新增** `skills/dsh-coder/SKILL.md`：仿 `skills/codebuddy-coder/SKILL.md` 骨架（触发方式 / 多轮对话姿势 / 安全边界 / 验收纪律 / 排障 / 环境坑点）。
- **修改** `main.py`：工具装配区（`tools.register(ExecTool(...))` 附近）新增 1 行注册 + 1 个 import。
- **新增** `tests/tools/test_dsh_session.py`：mock HTTP 的单测（信封格式、events 解析、增量过滤、错误分支）。
- **文档同步**：`PROJECT.md` 能力矩阵加一行「DSH 编码编排」；`docs/DEVELOPMENT.md` 验证矩阵如无对应项则补（按文档同步铁律，每步同步）。

## 非目标

- ❌ 不做闹钟/定时轮询接力（reminders 联动）——长任务跨回合轮询留作后续候选任务（TASK-047 只做回合内 read 循环）。
- ❌ 不接入社区 MCP/ACP 包（`dsh-harness-mcp-server` 等）——仅记录备选。
- ❌ 不改 `config.py`（base_url 默认 `http://127.0.0.1:3080`，允许环境变量 `DSH_API_BASE` 覆盖即可，不加配置白名单字段）。
- ❌ 不改 DSH 侧任何配置/文件（web profile 零改动）。
- ❌ 不碰 reminders/scheduler、渠道、总线、`config.example.json`。

## 验收标准

- [ ] `git diff --check` 通过
- [ ] `uv run python -m compileall -q agent` 通过
- [ ] `uv run python -c "import main"` 通过
- [ ] `.venv/bin/python -m unittest discover -s tests -t .` 全量通过（含新增 test_dsh_session）
- [ ] 单测覆盖：RPC 信封构造、history events 解析、增量（before_seq）过滤、HTTP 错误/DSH 未启动的中文可读报错、cancel 分支
- [ ] 真机冒烟（可选但推荐，DSH web 已在跑）：对 3080 完成一次 list → prompt → read 完整往返
- [ ] skill 文档与工具实际行为一致（参数名、返回结构、安全边界描述）
- [ ] 归档时 PROJECT.md 能力矩阵/命令速查同步（指针式，不写 hash）

## 相关模块

- `agent/tools/`（Tool 抽象与注册模式；ARCHITECTURE.md §工具扩展）
- `main.py` 工具装配区（1027-1040 行附近，`tools.register(...)` 列表）
- `skills/`（运行时技能目录，SKILL.md frontmatter 驱动）
- 外部依赖：DSH web profile 的 `/api`（`@deepseek-ai/dsh-host-apiproxy`，HTTP RPC 协议）

## 实现方案

1. **`DshSessionTool`**（约 150 行，无状态设计）：
   - `name="dsh_session"`，`parameters`：`{action, session_id?, message?, before_seq?}`
   - 内部用 `httpx.AsyncClient`（项目已有依赖）POST `{base}/api/{method}`，RPC 信封 + uuid rpcId
   - `list`：按 cwd 过滤会话（优先找当前项目目录的），返回 标题/会话ID/running/更新时间
   - `prompt`：不传 session_id 时自动 `session.create({cwd: 项目目录})`；返回 `{session_id, accepted}`
   - `read`：传 `before_seq`（或首次 0），返回 `{reply, last_seq, running}`；reply 取 `seq > before_seq` 的最后一条非空 `assistant/message`；`turn/end` 存在且无新消息 → `running:false`
   - `cancel`：`session.cancel({sessionId})`
   - 错误全部转可读字符串（同 ExecTool 风格）：连接失败 → "DSH 服务未连接（127.0.0.1:3080），请先运行 `dsh web`"；业务错误码 → 透传 message
   - base_url 读取：`os.environ.get("DSH_API_BASE", "http://127.0.0.1:3080")`
2. **注册**：main.py 装配区 `tools.register(DshSessionTool())` + import。
3. **`skills/dsh-coder/SKILL.md`** 要点：
   - 触发：用户要求用 dsh / DeepSeek Harness 开发、或 codebuddy 不可用时
   - 多轮对话姿势：同一 session_id 反复 prompt = 对话（DSH 会话持久、记得上下文）；每轮小奈先自审（方案/diff）再决定下一步
   - 安全边界：DSH 默认 workspace-write + 审批 fail-closed（headless 无应答者）；派活限定项目内；**绝不**设 `DSH_PERMISSION_MODE=danger-full-access`；git push/commit 由小奈做（DSH 无权限）
   - 验收纪律：DSH 报完成 ≠ 完成，小奈必须 `git diff` + 跑测试复核（与 codebuddy 一致）
   - 排障：DSH 未启动 / 404 / 超时 / 会话不存在
4. **单测**：mock `httpx`（或注入 fake client），覆盖信封/解析/增量/错误。

## 测试方式

- `git diff --check`
- `uv run python -m compileall -q agent`
- `uv run python -c "import main"`
- `.venv/bin/python -m unittest discover -s tests -t .`
- 定向：`.venv/bin/python -m unittest tests.tools.test_dsh_session -v`
- 真机冒烟（可选）：对 `http://127.0.0.1:3080` 跑 list/prompt/read 一次往返（复用演示会话 `session-4f841669` 或新建）

## 风险

- DSH 为 rc.6，`/api` 协议可能变动 → skill 注明"以实际响应为准"，解析层集中在一处便于改
- `/api` 无认证（仅本机绑定）→ 不暴露远程；skill 注明安全边界
- DSH web 未运行时工具报错（已设计可读报错）——提醒用户先起 `dsh web`
- 长任务回合内 read 受 `turn_timeout_sec`（实例 2400s）限制 → 超长任务留待后续候选（闹钟接力），不在本任务范围
- 会话堆积：`~/.dsh/sessions/` 无自动清理 → skill 记录清理约定（如定期 `rm -rf` 旧会话目录）

## 下一步

- [x] 实现 + 单测 + 真机冒烟 + 全量测试（2026-08-14 完成）
- [ ] 乖宝真机验收：确认 dsh_session 工具行为与 skill 文档一致
- [ ] 文档同步检查：PROJECT.md 能力矩阵新增「DSH 编码编排」行（待同步）
- [ ] 验收通过后归档到 docs/tasks/completed/，同步 PROJECT.md git 状态段与 MEMORY 指针
- [ ] 提交前与乖宝确认 commit message
