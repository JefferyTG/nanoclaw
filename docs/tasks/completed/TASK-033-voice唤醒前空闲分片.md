# TASK-033：voice 渠道唤醒前空闲分片检查

## 任务卡

- 状态：✅ 已完成（2026-08-10 验收归档）
- 负责人：小奈
- 执行会话/子 Agent：code-master
- 基线 commit / 分支：cde48d6（main，TASK-032 已提交+push，工作区干净）
- 依赖任务：TASK-024（voice 骨架）、TASK-025（唤醒→录音→ASR 闭环）、TASK-027（连续对讲）

### 目标

voice 渠道唤醒时，若距上次交互超过 `idle_ttl_sec`（默认 30 分钟），先自动开新会话（seq+1）再进入连续对讲——恢复 TASK-024/026 约定的空闲分片行为。

用户可观察结果：隔 30 分钟以上再喊「小奈小奈」，voice 渠道会先开新会话再开始对话，旧会话保留可 `/switch` 切回。

### 背景与根因

TASK-024 引入空闲分片：`inject_text()` 里调 `_maybe_split_session()` 检查距上次活动是否超 `idle_ttl_sec`，超了就 `_create_session()`。

TASK-027 引入连续对讲模式：`_handle_wake()` 里 `_enter_continuous()` 设 `_continuous=True`，此后 `inject_text()` 的 `_maybe_split_session()` 第一行 `if self._continuous: return` 直接短路，分片检查变成死代码。

根因：`_continuous` 在 `_handle_wake` 中、在 `inject_text` 之前就被设成 True，且连续对讲期间一直为 True，只在退出时（静默/[END]）清成 False。下次唤醒又立刻设回 True。所以 `_maybe_split_session` 里的 `_continuous` 检查永远命中，30 分钟空闲分片逻辑永远跑不到。

### 非目标

- 不改 web/feishu/weixin/cli 渠道的分片逻辑（本任务仅修 voice）
- 不改连续对讲期间的行为（对讲中不分片是对的）
- 不改 `/new` 手动新建会话逻辑
- 不调 `idle_ttl_sec` 默认值

### 允许修改

- `channels/voice.py`

### 禁止修改

- `agent/loop.py`、`agent/providers/`、`config.py`、`config.json`、`gateway.py`、其他渠道文件、`voice/kws/*`

### 验收标准

- [x] 唤醒时隔超过 idle_ttl_sec → 唤醒后自动开新会话（seq+1），旧会话保留
- [x] 唤醒时隔未超 idle_ttl_sec → 沿用当前会话，不分片
- [x] 连续对讲期间不分片（`_continuous` 仍为 True 时 `inject_text` 里的 `_maybe_split_session` 短路）
- [x] `/new` 手动新建不受影响
- [x] 其他渠道（web/feishu/weixin/cli）行为不变
- [x] 专项测试覆盖：唤醒前分片/唤醒前不分片/连续对讲中不分片
- [x] 全量测试通过（818 tests OK）
- [x] 文档同步：任务卡归档 + MEMORY 指针

### 实现摘要

**改动**（1 源文件 +5 行 + 1 新测试文件 3 条）：

`channels/voice.py` `_handle_wake()` 中，`await self._play_wake_reply()` 之后、`self._enter_continuous()` 之前，插入：
```python
self._maybe_split_session()   # 唤醒前检查空闲分片（此时 _continuous 还是 False）
self._bump_activity()          # 更新活动时间
```

此时 `_continuous` 为 False（还没 enter），`_maybe_split_session` 的连续对讲短路不命中，分片检查正常执行。`_bump_activity()` 紧跟其后，将唤醒计入最近交互，避免紧接着的 `inject_text` 再次分片。`inject_text()` 里现有的 `_maybe_split_session()` + `_bump_activity()` 保留不动（兜底手动调用/测试场景）。

**新增测试** `tests/test_voice_wake_idle_split.py`（3 条）：
- `test_wake_after_idle_exceeded_splits_session`：隔超 idle_ttl_sec → seq+1
- `test_wake_within_idle_threshold_does_not_split`：隔未超 → seq 不变
- `test_continuous_mode_inject_text_does_not_split`：连续对讲中 inject_text 不分片

**验证结果**：
- `git diff --check`：✅ 无空白错误
- 全量测试：✅ 818 tests OK
- `compileall channels/voice.py`：✅
- 仅改 `channels/voice.py` +5 行，未动其他文件

### 遗留问题

无。改动极小，行为恢复至 TASK-024/026 原始设计意图。
