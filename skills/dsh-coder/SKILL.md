---
name: dsh-coder
description: "通过 dsh_session 工具对话式调用本机 DeepSeek Harness（dsh）编码 Agent 执行编程任务：代码生成、重构、修复、测试、代码审查、批量文件操作。当用户要求用 dsh / DeepSeek Harness 写代码，或 codebuddy 不可用/超额时需要把编码任务委托给独立 Agent 时使用。与 codebuddy-coder 的区别：本技能是"对话式编排"（同一会话多轮追问纠偏），不是无头单发。"
---

# DeepSeek Harness 编码外援调用指南

本技能说明如何用 `dsh_session` 工具调用本机 DeepSeek Harness（DSH，命令 `dsh`）的编码 Agent：派活、增量读结果、多轮追问纠偏、取消、验收。DSH 是常驻的编码 Agent（web profile 跑在 `127.0.0.1:3080`，本机专用、无认证），会话持久（JSONL 落 `~/.dsh/sessions/`），**同一会话 ID 反复 `prompt` 就是对话**——它记得自己读过什么、改过什么。

## 使用前必读

- **每次使用前先 `load_skill` 本技能**，不凭记忆调用
- **DSH 服务必须已启动**：`dsh web`（默认 127.0.0.1:3080）。未启动时工具返回「DSH 服务未连接」，此时请用户先启动
- API 地址可用环境变量 `DSH_API_BASE` 覆盖（默认 `http://127.0.0.1:3080`）
- DSH 为 rc 版本，接口可能变动；报错时以工具返回的原始信息为准

## 核心用法：四个动作的对话循环

```json
dsh_session(action="list")                          // 看有哪些会话（本项目的排前面）
dsh_session(action="prompt", message="...")          // 派活（自动新建项目会话）→ 返回会话ID
dsh_session(action="prompt", session_id="session-xxx", message="...")  // 追问/纠偏（同一会话）
dsh_session(action="read", session_id="session-xxx", before_seq=N)     // 增量读结果
dsh_session(action="approve", approval_id="...")     // 批准 DSH 的权限审批（仅本次）
dsh_session(action="reject", approval_id="...")      // 拒绝 DSH 的权限审批
dsh_session(action="cancel", session_id="session-xxx")                 // 打断当前回合
```

## 权限审批的远程应答（2026-08-15 新增）

DSH Agent 做越界操作（写项目外路径、沙箱升级等）时会**挂起等待审批**（审批策略 ask，默认应答者是 Web 界面用户）。远程场景（用户不在电脑前）下，宿主 Agent 就是应答者：

1. `read` 检测到挂起时会明确提示「⚠️ DSH 正在等待权限审批」+ 工具名 + 原因 + **审批 ID**
2. **先问用户**：「DSH 请求权限：<原因>，批准还是拒绝？」
3. 用户说批准 → `dsh_session(action="approve", approval_id="<审批ID>")`；拒绝 → `action="reject"`
4. 应答后 DSH Agent 继续干活，照常 `read` 轮询

技术说明（供排障）：工具常驻监听 DSH 的 `/api/events.mux` WebSocket，审批帧只在发生时推送一次（不重放），监听错过（DSH 重启/工具刚启动）时 approve 会报「未监听到该审批请求」——此时只能 `cancel` 回合重派，或让用户到 Web 界面处理。

安全边界：**批准 = 授权 DSH 执行该次越界操作（allowed-once，仅本次）**。必须经用户明确同意才 approve；用户不在/不确定时一律 reject 或等待。

## 调用工作流（标准姿势）

1. **确认服务在线**：不确定时先 `dsh_session(action="list")`；报连接错误就让用户起 `dsh web`
2. **建会话/复用会话**：新任务用 `prompt` 不带 session_id（自动建项目会话）；跨任务续跑用 `list` 找到旧会话（按标题/cwd 判断）再 `prompt` 接上——DSH 记得上次上下文
3. **派活（plan 先行，2026-08-14 约定）**：需求模糊/大任务，第一条消息让 DSH **只出方案不写代码**（明确说「不要改任何文件，先给实现方案」）；审方案、与用户对齐后，第二条消息再让它实现
4. **轮询**：`prompt` 后循环 `read`（带 `before_seq` 增量），每次间隔由回合节奏决定；DSH 还在干活时 read 会明确说「还在干活」；回合完成会标「已完成」
5. **多轮纠偏**：对方案/实现不满意，直接 `prompt` 同一会话：「第 X 处有问题，改成 YYY」——这是本技能的核心价值，不要重开会话
6. **验收（铁律）**：DSH 报完成 ≠ 完成。必须自己 `git diff` 看改动 + 跑相关测试复核；有问题回到第 5 步，或自行修复
7. **收尾**：任务完成向用户汇报（改了什么、测试结果）；会话保留在 DSH（可复盘、可续跑），不删除

## 派活 prompt 的写法（无头/会话模式的共同教训，TASK-040 复盘）

DSH Agent 的视野取决于 prompt 给的信息，第一轮消息要写清：

1. **项目全貌**：跨模块任务显式加「先读 PROJECT.md、AGENTS.md、docs/ARCHITECTURE.md（或任务相关文档）再动手」；小任务可只给局部文件
2. **范围**：明确文件/函数边界、约束、验收方式；「只改 xxx.py」「改动最小化」
3. **资源缺失处理**：「若缺少 API key/凭据/外部资源：**不得编造**，标记 TODO 继续其它部分，结束时在总结里报告缺失项」。验收时 grep diff 里的 TODO/placeholder/假 key
4. **只读要求**：要方案时明确「不要修改、创建或删除任何文件」

## 常见任务模板

```json
// 代码审查（只读）
dsh_session(action="prompt", session_id="<已有>", message="审查最近一次 git 提交的改动（git diff HEAD~1），找 bug 和安全问题，按严重程度列出，不要改文件")

// 修 bug（附报错）
dsh_session(action="prompt", session_id="<已有>", message="修复这个报错：<贴报错>。根因排查，改动最小化，先读相关代码再动手")

// 重构
dsh_session(action="prompt", session_id="<已有>", message="把 xxx.py 的 XXX 重构为 YYY 风格，保持行为不变，完成后跑相关测试")

// 只出方案
dsh_session(action="prompt", session_id="<已有>", message="分析 xxx 的架构给出优化方案，先不要改代码")
```

## 安全边界与隔离约定

- **DSH 沙箱**：默认 `workspace-write`（以启动时的 cwd 为 workspace 根）+ 审批 `ask`，但 DSH web 的 Agent 是常驻会话，**没有人工应答者时需审批的操作会 fail-closed 直接拒绝**（如 git push、写项目外路径）。派活限定在项目目录内
- **绝不**设 `DSH_PERMISSION_MODE=danger-full-access`（等同 codebuddy 的 bypassPermissions，无确认删文件风险）；要放开必须用户明确授权
- **git 提交/推送由宿主 Agent 做**：DSH 无权限也无需权限；commit/push 前照常征得用户同意
- **数据目录隔离**：DSH 的会话/凭据/配置在它自己的 `~/.dsh/`（`$DSH_HOME`），**不复制进项目、不提交 git**；`~/.dsh/sessions/` 无自动清理，堆积明显时手动清理旧会话目录（`rm -rf ~/.dsh/sessions/--<项目>--/session-<旧id>`），或先问用户
- 敏感信息（API key、凭据、个人信息）不进派活 prompt
- 演示/测试用会话可在 Web 侧边栏（127.0.0.1:3080）查看进度

## 排障

| 现象 | 处理 |
|---|---|
| 「DSH 服务未连接」 | `dsh web` 未启动 → 请用户运行（首次会要 DeepSeek API Key，Settings → Models 配置） |
| 「DSH 返回错误 [xxx]」 | 按错误码处理；`session-not-found` 说明会话被清理/不存在 → 重新 `prompt` 新建 |
| read 一直「还在干活」 | 任务较长属正常；继续轮询；确认回合卡死可 `cancel` 后重派 |
| read 提示「等待权限审批」 | 越界操作挂起；先问用户，批准用 `approve`、拒绝用 `reject`（见上文审批章节） |
| approve 报「未监听到该审批请求」 | WS 监听错过（DSH 重启/工具刚启动/审批已处理）；`cancel` 回合重派或让用户到 Web 界面处理 |
| 回复与预期不符 | 用同一会话 `prompt` 追问纠偏，不要重开（重开丢失上下文） |
| 想打断重来 | `cancel` 当前回合 → 重新 `prompt`（会话上下文保留） |
| 行为异常 | 到 Web 侧边栏查看该会话的完整工具调用记录 |

## 环境坑点（本环境实测）

- 本机 DSH web 已长期运行（3080 端口），工具直接可用
- `prompt` 是**异步入队**：返回 accepted 只代表消息进了队列，结果必须 `read` 轮询拿
- 会话标题自动生成（按第一条消息）；`list` 显示标题可用来认会话
- 一次 `read` 只返回最后一条完整 assistant 回复；多轮历史用 Web 侧边栏看
