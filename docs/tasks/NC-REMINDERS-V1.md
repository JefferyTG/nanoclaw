# NC-REMINDERS-V1：主动提醒与定时 Agent 任务

> 本任务记录的是最初仅飞书投递的 V1；后续微信单目标扩展与通用目标迁移见
> `docs/tasks/NC-WEIXIN-REMINDERS-V1.md`。

## 任务卡

- 状态：实现与验证完成，待用户决定是否提交
- 负责人：当前 Codex 主任务（唯一项目/技术负责人）
- 基线 commit / 分支：`c0fd5e0` / `codex/nc-reminders-v1`
- 外部服务：全部使用 fake provider / fake Feishu SDK；未经授权不调用真实模型或飞书

### 目标

NanoClaw 的 Web、CLI、飞书会话均可通过 Agent 工具创建、查询、取消定时任务；
任务只投递到显式绑定的飞书私聊。支持预生成静态消息和到点运行现有 AgentLoop
的动态任务，支持 RFC 5545 RRULE、时区、重试、崩溃恢复和可靠发送回执。

### 第一版范围

- 管理能力：创建、查询、取消；不实现暂停、恢复、修改。
- 目标绑定：飞书私聊命令 `/bind-reminders`、`/unbind-reminders`；一个实例一个
  默认目标，持久化 `chat_id`、`open_id`、`target_id`，仅同一 `open_id` 可重绑/解绑。
  解绑只停用目标，不删除任务；同一 open_id 重新绑定时复用 target_id、更新 chat_id
  并唤醒调度器。目标停用期间任务不 claim，不把它们误判为永久发送失败。
- 任务类型：
  - `message`：保存 `subject` 与创建时由当前 Agent 生成人设化最终文本
    `delivery_text`，到点不调用模型。
  - `agent`：保存 `agent_prompt`，到点复用现有 AgentLoop/工具生成实时文本。
- 发送目标只能由已绑定 target_id 解析，工具和模型均不得传任意 chat_id。
- 仅飞书发送；不扩展群聊、Web 主动推送、邮件、TTS。

### 共享协议

#### 调度数据

- 所有持久时间均为带时区 UTC；SQLite 文本使用可排序的规范 ISO-8601。
- 任务保存 `dtstart_local`、IANA `timezone`、规范 RRULE、`next_run_at_utc`。
- 一次性任务也表达为 `FREQ=DAILY;COUNT=1`，避免数据库使用 once/daily 特例枚举。
- 状态：`active`、`running_agent`、`sending`、`retry_wait`、`completed`、
  `cancelled`、`failed`。
- 每个 occurrence 只有一条 execution，唯一键 `(task_id, scheduled_for_utc)`；执行记录
  保存 Agent 输出、发送尝试数、最后 DeliveryResult 和完成时间。
- 原子 claim 使用 lease token + 到期时间；启动时回收过期 lease。

#### 周期输入

工具接收结构化字段：`start_at`（本地 ISO 时间）、`timezone`、`frequency`
（`HOURLY|DAILY|WEEKLY|MONTHLY`）、`interval`、可选 `by_weekday`、
`by_monthday`、`count`、`until`。服务层严格校验并生成规范 RRULE；
`python-dateutil` 是本地锁定依赖。创建成功必须返回未来最多三次执行时间。

“每隔一天/每隔两天”等语义歧义由对话 Agent 在调用工具前确认；工具拒绝缺失或
矛盾参数，不猜测。DST 的不存在/重叠本地时间必须有确定行为并覆盖测试。

#### 调度与恢复

- 单一 ReminderScheduler：查询最早唤醒时间，以 `asyncio.Event` + 动态 timeout
  等待；创建、取消、重试、重绑均唤醒。允许低频安全校时上限，不做秒级扫描。
- 下一次 occurrence 从原计划 occurrence/规则推导，不使用完成时间。
- 一次性任务离线超过 1 小时标为 failed；窗口内补发。
- 周期任务恢复时只执行最近一次到期 occurrence，跳过更早历史，随后直接推进到
  当前时间之后的下次 occurrence（COUNT/UNTIL 耗尽则 completed）。
- Agent 使用 `scheduled:<task_id>:<execution_id>` 临时独立会话。生成成功后先把文本
  落执行记录，再清理 SessionManager/ImageStore 临时数据并进入 sending；发送失败
  只重发已保存文本，绝不再次调用 Agent。取消时清理相应临时会话。
- 发送超时 30 秒；由 Scheduler 统一控制最多 3 次逻辑尝试，Channel 内不再嵌套重试。
  网络、超时、限流/服务端错误可重试；权限、
  无效目标等永久错误停止重试。第一版为 at-least-once，记录进程在飞书接受后、
  SQLite 提交前崩溃可能产生极小概率重复。

#### 出站回执

- `OutboundMessage` 增加可选回执句柄；普通回复不创建、不等待回执。
- `DeliveryResult` 至少包含 `success`、`retryable`、`code`、`message`、
  可选 provider message id。
- Gateway 无论渠道缺失、`Channel.send` 返回、抛错或取消都必须恰好一次完成可选回执；
  无回执普通回复保持现有行为。
- FeishuChannel 将所有文本分片/图片视为一次逻辑发送；任一分片失败即失败，成功仅指
  飞书 API 接受。错误分类必须离线可测。

### 文件所有权

1. `NC-REM-CORE`（独立开发任务）
   - 独占：`reminders/__init__.py`、`reminders/models.py`、
     `reminders/schedule.py`、`reminders/repository.py`、
     `tests/reminders/test_schedule.py`、`tests/reminders/test_repository.py`、
     `pyproject.toml`、`uv.lock`。
2. `NC-REM-SCHED`（独立开发任务）
   - 独占：`reminders/scheduler.py`、`tests/reminders/test_scheduler.py`。
3. `NC-REM-DELIVERY`（独立开发任务）
   - 独占：`bus/queue.py`、`channels/base.py`、`gateway.py`、
     `channels/feishu.py`、`tests/reminders/test_delivery.py`、
     `tests/reminders/test_feishu_binding.py`。
4. 主任务集成专属
   - `reminders/service.py`、`agent/tools/reminders.py`、`agent/tools/__init__.py`、
     `agent/context.py`、`main.py`、`config.py`、`config.example.json`、其余集成测试、
     README 与 docs。

各开发任务不得修改任务卡、他人独占文件、`config.json`、`.workbuddy/`、
`workspace/`、`sessions/`、图片、`deploy/`、`scripts/` 或运行实例；不得 commit、push、
部署或调用真实外部服务。范围冲突立即停止并报告主任务。

### 验收标准

- [x] 未绑定时所有渠道创建任务均返回 `/bind-reminders` 指引。
- [x] 绑定只接受 p2p，且只有相同 open_id 可重绑/解绑。
- [x] 静态/动态、完整周期、DST、COUNT/UNTIL、未来三次预览有自动化测试。
- [x] 动态 Agent 成功后发送失败只重发相同落库文本。
- [x] 动态唤醒、取消竞态、lease、重启/休眠恢复和离线策略有 fake-clock 测试。
- [x] 飞书回执成功、超时、可重试/永久错误和普通回复兼容有 SDK mock 测试。
- [x] 配置、工具、生命周期、文档同步；不读取或提交敏感/运行产物。
- [x] 全量测试、`git diff --check`、Python compile/import 冒烟通过。

### 最低验证

```bash
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q agent bus channels providers reminders session tests
uv run python -c "import main"
git diff --check
git status --short --branch
```

## 执行交接

每个开发任务必须回传：状态、基线、实际改动文件、实现摘要、关键决策、实际验证
命令与结果、未验证项、风险、当前 git status、建议下一步。只有主任务可以确认完成。

## 完成记录（2026-07-29）

- 基线：`c0fd5e0`；工作分支：`codex/nc-reminders-v1`；未创建提交。
- 新增 `python-dateutil` 并同步 `uv.lock`；提醒数据库默认为
  `<config.workspace>/workspace/reminders.db`，与可重建记忆索引分离。
- 集成审查额外修复了 `retry_wait` 丢失 task 唤醒锚点的问题；真实 SQLite 测试确认
  Agent 结果只生成一次，两次发送尝试复用完全相同的落库文本。
- 验证：`uv run python -m unittest discover -s tests -v` 共 118 项通过；
  `uv run python -m unittest discover -s tests/reminders -q` 共 43 项通过；compileall、
  `uv run python -c "import main"`、`git diff --check` 均通过。
- 未执行：真实付费模型、真实飞书 API、部署、运行中实例修改、提交或推送。
- 已知语义：飞书成功只表示 API 接受；第一版 at-least-once，接受飞书接受后、SQLite
  提交前崩溃导致的极小概率重复发送。
