# TASK-045：webui 会话活跃状态显示

## 任务卡

- 状态：**已完成**（08-13 codebuddy 实现 + 小奈验证 + 乖宝真机验收通过）
- 负责人：乖宝（验收）/ codebuddy（实现）/ 小奈（指挥官）
- 执行会话/子 Agent：codebuddy（无头模式 acceptEdits）
- 基线 commit：`5ebf36e`（docs 漂移修复 + CodeBuddy 集成已推送）
- 依赖任务：TASK-039~044（webui 移动端适配/豆包化/HTTPS/语音/实时通话，均已归档）

## 背景与痛点

乖宝在外面用手机 webui 工作，网络会断。断线重连后**能看到历史**（刷新即可），但**不知道任务是否还在执行、跑到哪了**。需要会话级「活跃状态」展示。

## 现状（已排查）

- `gateway._active_tasks: Dict[str, asyncio.Task]` —— 按 session_key 记录正在执行的任务（已存在）
- `AgentLoop.last_run_status` —— running/completed/cancelled/timed_out/error（已存在）
- `/api/sessions`（channels/web.py `_handle_list_sessions`）返回 `list_sessions_detailed(prefix="web:")`，**未携带活跃状态**
- 前端侧边栏 `loadSessions()` 渲染列表，无状态徽标、无轮询

## 方案

1. **后端**：gateway 暴露活跃会话查询（基于 `_active_tasks`，过滤 done）；web `/api/sessions` 每项加 `active` + `active_since`；注入方式参考现有 `_context_callback` 回调姿势（main.py 装配）
2. **前端**：侧边栏列表项加状态徽标（「⏳ 执行中」+ 开始时间，呼吸动画）；轻量轮询（5~8s）刷 `/api/sessions` 更新列表与徽标，不打断输入、不干扰滚动
3. **测试**：为 `/api/sessions` 的 active 字段补 unittest；全量回归必须全绿

## 约束

- 改动最小化，复用现有机制，不重构大文件
- 默认主工作区直接改（不用 worktree/--bg）
- 不动 git 提交（小奈负责提交）；不碰 workspace/memory/、skills/
- 前端注意移动端适配（TASK-039 布局）

## 验收标准

- 全量 `unittest discover -s tests -t .` 通过
- 手机真机：发长任务 → 断网重连 → 侧边栏看到该会话「⏳ 执行中」徽标；任务完成后徽标消失、预览更新
- 汇报：改动文件清单、每处说明、测试结果、遗留问题

## 执行日志

- 08-13 codebuddy 实现完成（改动最小化，复用现有机制）：
  - `gateway.py`：`_active_tasks` 增加并行 `_active_tasks_started` 记录回合开始时刻（本地时间，与 `updated_at` 同源）；新增 `get_active_sessions()` 返回未 done 的 `session_key -> ISO` 映射（已 done 过滤）。
  - `channels/web.py`：`_handle_list_sessions` 为每项补 `active`/`active_since`（duck-typed 回调注入，异常/未注入/非 dict 均降级为全非活跃）；新增 `self._active_sessions_callback` 属性。
  - `main.py`：`web_channel._active_sessions_callback = getattr(gw, "get_active_sessions", None)` 装配。
  - `webui/index.html`：`loadSessions` 渲染「⏳ 执行中」呼吸徽标（含开始时间），加重渲染滚动位置保护 + 在途防抖；新增 6s 轻量轮询 `setInterval(loadSessions, 6000)`；新增 `.sb-active` 样式与 `@keyframes sbBreath`。
  - 测试：新增 `tests/channels/test_web_sessions_active.py`（/api/sessions 字段 + Gateway 过滤逻辑）。

## 执行交接

- 状态：实现完成（待小奈提交、待乖宝真机验收）
- 实际改动文件：gateway.py、channels/web.py、main.py、webui/index.html、tests/channels/test_web_sessions_active.py（新增）
- 与任务卡描述差异：任务卡称 `_context_callback` 为「WebChannel 构造参数」，实际代码是构造后属性赋值（`web_channel._context_callback = ...`），本实现沿用同一属性赋值姿势，未新增构造参数。
- 测试命令（本环境 Bash 受权限限制未执行，需人工跑）：
  - 相关：`.venv/bin/python -m unittest tests.channels.test_web_sessions_active -v`
  - 全量：`.venv/bin/python -m unittest discover -s tests -t .`
- 遗留/风险：① 轮询复用 `loadSessions` 整体重渲染，已用 `scrollTop` 保护与在途防抖降低干扰；若后续列表项需更细粒度更新可改为按 key 就地打补丁。② 开始时间取 `datetime.now()`（本地、naive），与 `updated_at` 同源，前端 `relTime` 直接解析；若未来要统一时区展示可改 UTC+offset。③ 仅 web 渠道会话有徽标（Gateway 按 `web:xxx` 记录，与 `list_sessions_detailed` 前缀对齐）。
