# TASK-046：webui 断线重连续流 + 断网结果补拉 + 主界面任务横幅

## 任务卡

- 状态：**已完成**（08-14 小奈实现 + 乖宝真机多轮验收通过；984 tests 全绿；已提交推送）
- 负责人：乖宝（验收）/ 小奈（实现）
- 依赖任务：TASK-045（会话活跃状态，/api/sessions active 字段 + 6s 轮询）；与 TASK-040（多端历史同步）共享「事件按会话广播」核心

## 背景与痛点

乖宝用手机 webui 干活，网络断了再连回来，任务其实还在 Mac 上跑，但体验差：
1. 断线瞬间前端 `cur = null` 清空进行中状态，聊天界面无交代
2. 重连后任务后续事件仍发给旧连接（已断）→ 丢弃，**续不上流**
3. 断网期间任务跑完的结果看不到（除非手动切会话/刷新）
4. 侧边栏「⏳ 执行中」徽标（TASK-045）不显眼，手机端要划开抽屉才看得到

## 根因（已排查 08-14 11:0x）

- `gateway._make_stream_sink`（gateway.py:320）构造 `StreamEvent` 只带 `chat_id`（= conn_id），**无会话 key** → `_dispatch_stream` 按 conn_id 单发（gateway.py:338-350）
- `channels/web.py.stream_event`（L1083）按 conn_id 找连接，找不到就丢弃；连接断开时 `_conns/_sessions` 被 pop（L807-808）→ 重连的新 conn_id 收不到旧任务的后续事件
- 前端 `connectWS` onopen 只 send open 控制消息，**没有 fetch /api/session 补拉历史**（openSession 里才有 fetch，重连路径没有）
- 前端 ws.onclose 直接 `cur = null`（L1006），进行中 UI 清空

## 方案（事件按会话广播）

1. **bus/queue.py**：`StreamEvent` / `OutboundMessage` 加可选 `session_key: Optional[str] = None`（向后兼容）
2. **gateway.py**：`_make_stream_sink` 构造 StreamEvent 带 `session_key=msg.sender_id`（web 渠道 sender_id = 会话 local key，已确认）；`outbound_safe` 同理
3. **channels/web.py**：
   - `__init__` 维护 `self._key_conns: dict[str, set[str]]`（local key → conn_ids 集合）
   - `new`/`open` 控制消息：conn_id 移入新 key 集合（先从旧集合移除）
   - 连接断开：从所有集合移除该 conn_id
   - `stream_event(conn_id, event, session_key=None)`：有 session_key 时广播给该 key 集合内所有**存活**连接；无则退回按 conn_id 单发
   - `send(message)`：原 chat_id 连接不在时，按 `message.session_key` 找当前活跃连接回投
4. **webui/index.html**：
   - `ws.onclose`：**不清空聊天区**，显示「已断开 · 任务可能仍在执行」横幅（复用系统消息样式）；停止 TTS/清计时；cur 置空防污染
   - `ws.onopen`：open 原 key + **fetch('/api/session?key=...') 补拉历史**（断网期间完成的消息渲染出来）+ loadSessions
   - 主界面横幅：6s 轮询（已有）拿到当前会话 `active`/`active_since` → 聊天区顶部显示「⏳ 任务进行中 · 已运行 N 分钟」，inactive 隐藏
5. **测试**：广播/接管/补拉单测 + 全量回归

## 附带收益

- TASK-040 核心（事件按会话广播）一并打通，多端同步基础就位
- 手机断网重连 = 自动接管原会话，任务结果不丢

## 约束

- 改动最小化，复用现有机制；不动 gateway 并发/锁语义
- 默认主工作区直接改；git 提交须乖宝点头
- 前端注意移动端适配（TASK-039 布局）与「正在输入不被打断」
- 不碰 workspace/memory/、skills/

## 验收标准

- [ ] 全量 `unittest discover -s tests -t .` 通过
- [ ] 导入冒烟 `uv run python -c "import main"` + 实际启动几秒看日志
- [ ] 手机真机：发长任务 → 断网 → 聊天区保留 + 显示「已断开」；重连 → 事件自动续流（后续进展不用刷新）；若断网期间已跑完，重连自动补拉结果
- [ ] 主界面横幅显示「任务进行中 · 已运行 N 分钟」，完成后消失
- [ ] 单端体验无回归（桌面/移动布局、实时通话、按住说话）
- [ ] 文档同步：ARCHITECTURE.md / DECISIONS.md 如涉及则更新；任务卡归档时同步

## 必须执行的验证

```bash
git diff --check
uv run python -c "import main"
unittest discover -s tests -t .
```

## 执行日志

- 08-14 11:1x 小奈实现完成：
  - `bus/queue.py`：`StreamEvent`/`OutboundMessage` 加可选 `session_key` 字段（向后兼容）
  - `gateway.py`：`_make_stream_sink`/`outbound_safe` 构造时带 `session_key=msg.sender_id`（web 渠道 sender_id 即会话 local key）；`_dispatch_stream` 传第三参
  - `channels/web.py`：新增 `_key_conns`（local key -> {conn_id}）会话连接集合 + `_bind_conn_to_key`/`_unbind_conn`；初始连接/ctl new/open 时绑定、断开时解绑；`stream_event` 带 session_key 时按会话**广播**给所有存活连接（查不到退回单发）；`send` 发起连接断开时按 session_key 转发给接管连接
  - `webui/index.html`：断线保留聊天区+只提示一次「已断开·任务可能仍在执行」；重连自动 open 原会话 + fetch `/api/session` 补拉历史（断网期间完成的结果一次看全）；主界面顶部「⏳ 任务进行中·已运行N分钟」横幅（随 6s 轮询刷新）
  - 测试：新增 `tests/channels/test_web_reconnect_broadcast.py` 13 用例（绑定/解绑/广播/接管/转发/降级）；全量 **984 tests OK**（971+13）；`import main` OK；前端 JS `node --check` OK
- ⏳ 待办：重启实例加载新代码（当前实例为乖宝 08-13 手动启动旧代码）→ 乖宝真机验收（发长任务→断网→重连→续流/补拉/横幅）→ 文档同步（ARCHITECTURE §web 分发/DECISIONS）→ git 提交须乖宝点头

## 验收记录

- 08-14 乖宝真机多轮验收（电脑发起任务→关页面→手机打开）：
  1. 首轮：任务进行中横幅 ✅；重连后看不到工具结果（tool_result 静默丢弃）→ 修复：前端 tool_result 兜底卡片
  2. 二轮：exec 卡片出现但展开为空 → 根因：兜底卡片漏 `has-result` 类（CSS 隐藏结果区）→ 修复
  3. 三轮：实时结果可见 ✅；但「已输出的内容重连后消失」→ 根因：重连补拉用清空重渲染，进行中内容未落盘 → 修复：任务仍在跑时保留现有渲染不重渲染
  4. 四轮：内容保留 ✅；但手机新开页面看不到中间过程 → 乖宝拍板 B 方案（中间过程省略，自动接管+横幅+结果自动出现）→ 实现 autoTakeoverActive
  5. 五轮：自动接管生效 ✅ 收尾
- 08-14 提交：commit 见 git log（含 PROJECT/ARCHITECTURE/DECISIONS 文档同步 + 任务卡归档）
