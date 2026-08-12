# TASK-040：webui 多端历史同步

## 任务卡

- 状态：待开始
- 负责人：乖宝（验收）/ code-master（实现）
- 执行会话/子 Agent：code-master
- 基线 commit / 分支：待定（依赖 TASK-039 先验收归档；当前工作区含 039 未提交改动）
- 依赖任务：TASK-039（webui 移动端适配，同一文件 `webui/index.html`，需 039 先落地/归档避免文件所有权冲突）

### 目标

让网页端（webui）在**多个连接同时打开**（如手机 + 电脑、多标签页）时，聊天历史与会话列表保持一致：
- 一端发送消息并收到回复 → 另一端**自动**看到（不用手动刷新）
- 一端新建/切换/删除会话 → 另一端的侧边栏**自动**更新
- 不打断正在输入的一方（不强行重载他人正在编辑的输入框）

### 背景与根因（已排查，2026-08-12 11:1x 核实代码）

TASK-039 移动端适配后，手机+电脑多端同时使用成为常态，问题浮现：

1. **后端不回广播**：`channels/web.py` 的 `send()`（出站回包）与 `stream_event()`（流式事件 thinking/token/done）都只按 `message.chat_id`（conn_id）推给**单个**连接（`self._conns.get(conn_id)`），没有按会话 key 广播 → 只有发消息的那一端收到回复。
2. **前端无同步机制**：`loadSessions()` 只在首屏 / 自己发完消息后 / 新建会话后调用；无定时轮询、无「他人更新」通知事件 → 另一端侧边栏与当前会话历史静止不动。
3. **会话状态每连接独立**：`self._sessions[conn_id] = {seq, current_key}`，各连接 current_key 各自维护，一端切换会话另一端不知道。

### 非目标

- ❌ 实时语音/HTTPS/PWA 等（见 TASK-039 后续任务）
- ❌ 修改会话管理/Agent 循环/网关核心逻辑（不动 gateway 的并发/锁语义）
- ❌ 跨渠道同步（web 与飞书/微信会话一致——渠道模型本就独立，不在本任务）
- ❌ 做「多端协同编辑」（同一时刻多人同时编辑同一输入的冲突解决）

### 允许修改

- `channels/web.py`：**多端同步必须后端配合**，这是本任务与 039 的边界差异——需要在网关出站/流事件分发处按会话广播，或新增轻量 sync 事件
- `webui/index.html`：前端监听 sync 事件 → 刷新侧边栏；当前会话被他人更新且本端未在输入时重载历史

### 禁止修改

- `main.py`、`gateway.py`、`config.json`、其它渠道代码（feishu/weixin/voice/realtime/cli）
- 会话持久化、Agent 循环、工具注册等业务逻辑
- 未授权的 git commit / push

### 上下文与约束

- **相关代码入口**：
  - `channels/web.py`：`send()`（出站回包，L~640）、`stream_event()`（流式事件，L~663）、`_conns` 连接表（conn_id → WS）、`_sessions`（conn_id → {seq, current_key}）
  - `webui/index.html`：`ws.onmessage`（L931）、`loadSessions()`（L1358）、`openSession()`（L1392）、`newSession`（L1418）
  - 事件协议：前端收 `{"event": {...}}`，事件 type 有 hello/session_changed/thinking/token/tool_call/done 等
- **会话标识**：`session_changed` 事件带 `key: "web:<key>"` 格式（`self.name:current_key`）；广播可按此 key 关联连接
- **已确认行为**：`send()` 在 `message.streamed=True` 时跳过（流事件已覆盖）；连接断开时丢弃事件不影响其它连接
- **风险**：
  - 广播时不能给「发消息方」重复推送（流事件已覆盖回包），需去重或保持现有单发路径不动、只给**其它**同会话连接补发
  - 前端重载当前会话历史时若本端正在输入，不能清掉输入框内容
  - 不得影响桌面单端体验与 TASK-039 已适配的移动端布局

### 验收标准

- [ ] 手机 + 电脑同时打开 webui：手机发消息 → 电脑**无需刷新**自动看到该消息与回复（含思考面板/工具卡片）
- [ ] 一端新建会话 → 另一端侧边栏自动出现新会话
- [ ] 一端切换会话 → 另一端当前历史自动跟随（未在输入时）
- [ ] 一端删除会话 → 另一端侧边栏自动移除
- [ ] 正在输入的一方**不被打断**：输入框内容与焦点保留，不因他人消息被清空/强刷
- [ ] 单端体验无回归：桌面、移动端布局与 TASK-039 验收结果一致
- [ ] 全量测试通过：`unittest discover -s tests -t .`
- [ ] 文档同步：ARCHITECTURE.md（web 渠道分发说明）/ DECISIONS.md（广播决策）如涉及则更新；任务卡归档时同步

### 必须执行的验证

```bash
git diff --check
unittest discover -s tests -t .          # 全量
python -m compileall -q channels         # 后端语法
# 手动验证（乖宝真机）：手机+电脑双端同时开，逐项对照验收标准
```

## 实现方案（建议，code-master 可优化）

**方案 C：轻量同步通知（推荐）**——后端广播 + 前端轻量刷新，改动适中、体验实时、不打断输入：

1. **后端（channels/web.py）**：
   - 在 `send()` 与 `stream_event()` 现有单发逻辑**保持不变**（发消息方路径零改动），另加一个「同会话广播」：当回包/流事件产生时，把同一会话 key 下的**其它**连接也推送对应事件（回复内容或 `{"type":"sync"}` 通知）
   - 或更轻：新增 `broadcast_sync(key)` 辅助——向同会话其它连接发 `{"type":"sync","key":...}` 信号，由前端拉取最新状态；回包不重复推，避免双端渲染竞态
   - 维护 key → [conn_id] 的索引（或用 `_sessions` 反查），注意锁与线程（web 事件循环 run_coroutine_threadsafe）
2. **前端（webui/index.html）**：
   - `ws.onmessage` 收到 `sync` 事件 → `loadSessions()`（刷新侧边栏）；若 `sync.key == 当前会话 key` 且本端**未在输入**（输入框无焦点或值为空）→ 重载当前会话历史
   - 若正在输入 → 只刷侧边栏 + 本地标记「他人更新过」，下次发送前再合并/刷新（避免覆盖）
3. 不引入轮询（除非广播不可行才退化为 30s 轮询）

## 后续任务（本任务不处理，仅记录）

- TASK-0??：webui 实时语音 / HTTPS / PWA（见 TASK-039 后续任务，优先级更高）
- 多端「实时双人编辑」级别的冲突解决（本任务只保证不打断输入，不做协同编辑）

## 执行交接

- 状态：待开始（2026-08-12 11:1x 建卡）
- 实际改动文件：（待填）
- 实现摘要：（待填）
- 关键决策与假设：（待填）
- 验证命令与结果：（待填）
- 未验证项：（待填）
- 风险与遗留问题：（待填）
- commit（仅在获授权时）：（待填）
- 当前 `git status --short --branch`：main；`M PROJECT.md`（039 里程碑指针）+ `M webui/index.html`（039 改动未提交）+ `?? docs/tasks/active/TASK-039-*.md`（未跟踪）
- 建议下一步：TASK-039 验收归档 → 乖宝说「开始 TASK-040」→ code-master 按本卡实现（方案 C）

## 负责人验收

- [ ] 检查 diff 与授权范围
- [ ] 独立复跑关键验证
- [ ] 检查秘密/个人数据/运行产物
- [ ] 检查文档与配置一致性
- [ ] 更新 `docs/DECISIONS.md` 中相关状态
- 验收结论：（待填）
- 证据与备注：（待填）
