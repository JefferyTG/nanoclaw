# TASK-010：会话中断回合上下文丢失（Agent「失忆」）

## 任务卡

- 状态：✅ 已完成（2026-08-05 验收归档）
- 创建：2026-08-05 ｜ 负责人：code-master 排查 + 小奈设计/验收
- 基线 commit / 分支：`573019c`（main，TASK-009 归档后）
- 依赖任务：无

## 现象

**Agent 在「回合中途中断」后重启/接回，中断回合及之前的消息没有进入新实例上下文，导致 Agent「失忆」——不知道自己在中断前做了什么，回复内容与工作区实际状态对不上。**

2026-08-05 实证案例（乖宝已多次遇到「没回消息」）：
- 17:54:06 当前会话收到 code-review 报告（tool 消息）后，assistant **未生成任何最终回复文本**即中断（无文本、未发送、未写入）；
- 18:09 乖宝重启/重连后追问，新实例的上下文**只到 17:32**（提交完 TASK-008），**缺失 17:33~17:54 约 20 分钟的全部消息**（TASK-009 实施、code-master 派遣、验收、审查全过程）；
- 新实例看到工作区有 TASK-009 的未提交代码，误判为「另一个会话做的」，实际上**就是自己做的**；
- 会话文件 `workspace/sessions/web_ws-7d68c6ce296b_1.jsonl` 中 17:33~17:54 的消息**落盘完整**，说明**落盘正常，是接回时未拼进上下文**。

## 目标

- 排查并修复「中断回合 + 重启接回时上下文缺失」的问题，保证 Agent 重启后能看到中断前同一会话的完整历史。
- 明确「会话文件（JSONL）↔ 当前上下文」的恢复契约：恢复什么、恢复多少、中断回合如何处理。

## 排查结论（2026-08-05 code-master 完成）

### 根因（一句话）

**不是「加载错文件」，也不是「读取时被裁剪」，而是「中断回合处理」缺陷：回合被 `turn_timeout` 强杀时，中断回合的消息只写进磁盘 JSONL、从未同步进内存 `_session_history`；同一 AgentLoop 实例存活复用（案例中「重启」实为浏览器 WS 重连，后端实例未死），下一轮用旧的内存历史拼上下文 →「只到 17:32」。**

### 精确到代码路径

| 环节 | 位置 | 行为 |
|---|---|---|
| 内存历史唯一更新点 | `agent/loop.py:921` `_save_to_history(messages)`（仅正常完成分支）+ `:1054`（仅熔断分支）+ `:314` `_record_cancelled_turn`（仅网页「停止」CancelledError） | **回合未正常完成时，`_session_history` 永远停在上一完整回合** |
| 磁盘增量写 | `agent/loop.py:782` `_persist(user_record)`；`:1155-1188` `_persist_tool_exchange`（assistant(tool_calls) + tool） | 每步都落盘，但**不更新** `_session_history` |
| 模型输入来源 | `agent/loop.py:728` `history_clean = [... for m in self._session_history]` | 下一轮上下文只来自内存 `_session_history`，**与磁盘无关** |
| 本次中断触发点 | `agent/loop.py:790-802` 整轮超时：`turn_start=time.monotonic()`（:790），迭代顶部 `if time.monotonic()-turn_start > self.turn_timeout: return msg`（:796-802） | 返回**不带任何历史同步** |

### 任务卡事实修正（2026-08-05）

- 「170 条」不实：17:33~17:54 窗口实际约 10 条（全文件 187 行）。「170 条」应是浏览器里子 Agent 流式事件的观感，父会话只落盘子 Agent 边界消息。
- 「重启」实为 WS 重连：`channels/web.py` 每连接生成新 `conn_id`，前端重连后 `{ctl:true,type:'open'}` 重开原 `session_key` → 同一实例续写，非新建 AgentLoop。
- **真进程重启不会复现本 bug**：新建 AgentLoop 时 `get_history` 读磁盘全量返回，canonicalize 全保留。已用模拟脚本验证。

### 关键排除项

- `canonicalize_history` 对「assistant(tool_calls)→tool→流结束」**保留** tool 结果（实测），不丢；孤立 tool 才丢弃。canonicalize 不是丢消息原因。
- 不存在「最后一条 user 之后只恢复闭合 assistant」的显式逻辑；真正边界是「最后一个**完成**的回合」。
- 网页历史接回与 CLI/微信是同一路径（`gateway.py` → `agent_factory(session_key)` → `AgentLoop.__init__` → `get_history`）。
- 压缩未触发（token 预算 524288，115 条估算远低于预算）；文件时间戳均原始值，从未被 `save_messages` 覆盖写回。**但属潜在关联风险**：若中断后下一轮触发压缩，会用旧内存历史 + `save_messages` 覆盖磁盘抹掉中断内容——修 bug 时需考虑（压缩发生在 `_run` 开头 loop.py:753-755）。

## 修复方案（已实现）

**核心思路：让内存 `_session_history` 与磁盘始终保持一致（磁盘是唯一事实源），把「回合完成时才同步」改为「每落盘一条就同步一条」，则任意中断路径（超时/error/异常/取消/崩溃）后内存都不丢。**

实现（`agent/loop.py`）：

1. **`_persist` 改为「落盘即同步内存」**：`save_message` 写盘后，把同一条消息经 `canonicalize_history_message` 清洗后**增量追加**进 `_session_history`。磁盘是唯一事实源，任意中断路径内存都不丢。
   - **刻意不做整段 canonicalize**：工具交换按 `assistant(tool_calls) → tool` 协议顺序逐条落盘（`_persist_tool_exchange`，assistant 在前）；若在 assistant(tool_calls) 落盘后立即整段 canonicalize，尚未落盘的 tool 结果会被 `close_pending` 补成占位符，随后真正的 tool 结果又被当作孤立 tool 丢弃——真实工具结果丢失。因此保持「磁盘写入顺序」增量追加（该顺序本身即 canonicalize 期望的合法顺序），由读取 / 收尾边界（`_save_to_history` / `_record_cancelled_turn` / 记忆补丁 / 压缩）统一 canonicalize 兜底，保证内存始终等价于磁盘。
2. **`_record_cancelled_turn` 去重调整**：`_persist` 已把 user / 中断记录追加进内存历史，取消分支**不再手动追加**（`records_to_append` 删除），只做幂等补写 + 整段 canonicalize 兜底，避免同一回合出现重复消息。工具执行中取消时 `_execute_tools` 已落盘的中断记录（`self._interrupt_record`）复用逻辑不变。

### 边界情况（实现后行为）

| 场景 | 修复后行为 |
|---|---|
| 正常回合 | `_persist` 增量追加 + 结束时 `_save_to_history` 整段重建，一致 |
| 取消回合（web 停止） | 内存=磁盘=[…, user, 中断占位 assistant]，「继续」可见（测试断言不重复、幂等） |
| 超时回合（本次案例） | 内存=磁盘=[…, user, assistant(tc), tool]，下一轮可见完整中断回合 |
| 模型 error / provider 异常 | 同上（`_persist` 已同步） |
| 压缩后接回 | 压缩在 `_run` 开头改写 `_session_history`+磁盘（:753-755），之后 `_persist` 增量追加 → 一致 |
| 进程崩溃重启 | 新建 AgentLoop 从磁盘全量恢复（现状已正确），修复后磁盘仍完整 |

## 范围

- `agent/loop.py`：`_persist` 单点同步内存历史（+`canonicalize_history_message` 导入）；`_record_cancelled_turn` 去重调整
- `tests/test_interrupted_turn_resume.py`：新增「中断回合后接回上下文完整」回归测试（7 用例）

## 非目标

- ❌ 改记忆体系（USER/MEMORY 快照机制不动）
- ❌ 改提醒/定时任务链路
- ❌ 处理「压缩 `save_messages` 改写时间戳」潜在风险本身（记为遗留风险，见 DECISIONS NC-MEM-002）

## 验收标准（2026-08-05 全部 ✅）

- [x] 构造「assistant tool_calls → tool 结果返回 → 无最终文本（超时/error）」的回合，同一 AgentLoop 实例下一轮追问，模型输入含中断回合全部消息 → `test_timeout_turn_kept_in_memory_and_disk_then_resumed` / `test_error_turn_kept_in_memory_and_disk_then_resumed`
- [x] 取消路径（web 停止）不回归、不重复（复用 test_agent_cancel_history 场景）→ `test_cancel_after_fix_has_no_duplicate_records` / `test_cancel_during_tool_keeps_single_interrupt_record`
- [x] 真重启路径守护：中断后新建 AgentLoop，get_history 全量恢复（现有正确行为不破坏）→ `test_restart_recovers_interrupted_turn_from_disk`
- [x] 新增回归测试全绿：`unittest discover -s tests`（408 tests OK）、`git diff --check`、`compileall`、`import main` 全部通过

## 实现摘要（2026-08-05 归档）

- **改动文件**：`agent/loop.py`（+33/-23，`_persist` 落盘即同步内存 + `_record_cancelled_turn` 去重）；`tests/test_interrupted_turn_resume.py`（新增 7 用例）
- **关键决策**：磁盘是唯一事实源；`_persist` 按磁盘顺序增量追加（不整段 canonicalize，避免工具交换被 close_pending 补成占位符导致真实 tool 结果丢失）；收尾边界统一 canonicalize 兜底
- **验证结果**：全量 408 tests OK（38.9s）、compileall OK、import main OK、git diff --check OK；`_HangThenRespondProvider`/`_ScriptedResponsesProvider`/`_BlockingProvider` mock 覆盖超时/error/取消/重启/压缩 6 类路径
- **遗留问题**：压缩 `save_messages` 改写会话文件时间戳的潜在风险（NC-MEM-002，暂不处理）

## 下一步

无（已验收归档）。关联遗留：NC-MEM-002。
