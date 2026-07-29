# NC-WEIXIN-REMINDERS-V1：微信主动提醒

## 任务卡

- 状态：换绑补充已完成并通过负责人验收
- 负责人：当前 Codex 主任务（唯一项目/技术与集成负责人）
- 执行子 Agent：`reminder_target_core`、`reminder_scheduler_defer`、
  `weixin_reminder_binding`；换绑补充由 `reminder_rebind_repository`、
  `reminder_rebind_service` 执行（均为 gpt-5.6-terra / medium）
- 基线 commit / 分支：`7c3649e` / detached worktree；本地主干基线 `32b9687`
- 换绑补充基线：任务 worktree `95272eb`；本地主干 `3ae87f2`
- 依赖任务：`NC-REMINDERS-V1`、`NC-WEIXIN-V1`

### 目标

在不复制调度器或 Agent 的前提下，让单账号微信私聊成为现有主动提醒系统的可靠
目标：支持绑定/解绑、静态与动态任务、持久调度、重启恢复、稳定发送关联 ID、可靠
回执，以及微信会话失效后的暂停与恢复。

### 产品边界

- 一个提醒数据库同一时刻只有一个目标，飞书与微信二选一。
- 用户主动 `/unbind-reminders` 会释放目标所有权但保留任务；下一次显式绑定可以切换
  渠道或 owner，历史 target UUID、任务和 execution 原位迁移到新目标。
- `session_expired`、`context_missing` 等系统暂停不释放所有权；只允许原渠道和 owner
  恢复，避免掉线期间被其它 allowlist 用户接管。
- 双投递、群聊、多账号、提醒图片/语音/文件不属于 V1。
- 工具不接受 channel、chat/user ID 或 token；Web/CLI/任意聊天会话仍统一操作唯一目标。
- 不新增配置项；微信绑定继续受 `weixin.allowed_user_ids` 约束。

### 共享目标契约

- 领域字段：`channel`、`recipient_id`、`owner_id`、`active` 和持久化的显式释放状态；
  稳定公开 ID 仍为 UUID `target_id`，任务外键不变。
- Feishu：`recipient_id=chat_id`，`owner_id=open_id`。
- Weixin：`recipient_id=owner_id=encode_weixin_target(account_id, user_id)`。
- context token、bot token、cursor 和去重状态继续只属于 Node Bridge，不进入提醒 SQLite、
  Bus、配置或日志。
- SQLite 使用显式 `PRAGMA user_version` 迁移旧飞书库；历史 target UUID、任务和 execution
  必须原样保留。全局 partial unique index 保证最多一个 active target。

### Repository / Service API

- `bind_target(channel, recipient_id, owner_id, now_utc)`：首次建立 owner；活动/系统暂停
  状态仅相同 channel + owner 可重绑，显式释放后可原位更新到新 channel + owner。
- `unbind_target(channel, owner_id, now_utc)`：仅当前所有者可显式释放。
- `get_active_target()`：返回唯一 active target，不硬编码渠道。
- `get_target_by_public_id(target_id, *, active_only=True)`：投递按 execution 保存的目标
  精确解析，禁止错误投向后来出现的其它目标。
- `release_claim_for_inactive_target(execution_id, lease_owner, now_utc)`：解绑/会话失效
  竞争时把 execution 放回可恢复状态，不计一次发送失败；inactive target 不再被 claim。
- `suspend_target(target_id, now_utc, expected_binding_revision=...)`：微信
  `session_expired/context_missing` 时仅在绑定代次仍匹配时暂停目标；不开放换绑，用户
  重新扫码并以同一身份发送 `/bind-reminders` 后恢复并唤醒 Scheduler。

### 2026-07-29 换绑补充验收

- [x] 飞书 owner 主动解绑后可由微信 owner 绑定，反向使用同一渠道无关契约。
- [x] 换绑复用同一 target UUID，历史 task/execution 不丢失且后续投递走新渠道。
- [x] 非 owner 无法解绑；活动或系统暂停状态下其它渠道/owner 无法抢占。
- [x] 系统暂停后原 owner 可恢复，也可直接主动解绑再显式释放。
- [x] SQLite v0/v1 自动迁移到新版本；旧 inactive 目标默认不开放接管。
- [x] 显式释放后的并发绑定只有一个成功者，后到者不能覆盖已成功的新 owner。
- [x] 换绑后迟到的旧渠道失败回执不能暂停新渠道目标。
- [x] 服务提示、README、架构与决策文档准确区分“主动释放”和“异常暂停”。

### 渠道与投递契约

- WeixinChannel 在 allowlist 之后、图片合并和 Agent 之前精确拦截纯文本
  `/bind-reminders`、`/unbind-reminders`；命令不进会话历史，也不作为待合并图片说明。
- `session_expired` 通过可选 callback 暂停微信提醒目标；回调失败不得泄露秘密或杀死渠道。
- 到点投递使用 `target.channel` 和 `target.recipient_id` 构造 OutboundMessage。
- 微信提醒固定 `correlation_id = reminder:<execution_id>`；同一 execution 的 Bridge 内重试、
  Scheduler 重试与进程重启均复用该值。
- `timeout/network_error/bridge_error` 沿用 Scheduler 持久重试；`access_denied/invalid_target`
  为永久失败；`target_unbound/context_missing/session_expired` 暂停而不是耗尽三次后失败。
- Node Bridge IPC 不新增方法或秘密字段；复用现有 `send_text` 与 `delivery_result`。

### 文件所有权

1. `reminder_target_core` 独占：
   `reminders/models.py`、`reminders/repository.py`、`tests/reminders/test_repository.py`。
2. `reminder_scheduler_defer` 独占：
   `reminders/scheduler.py`、`tests/reminders/test_scheduler.py`。
3. `weixin_reminder_binding` 独占：
   `channels/weixin.py`、`tests/test_weixin_channel.py`。
4. 主任务独占：
   `reminders/service.py`、`main.py`、`channels/feishu.py`、`agent/tools/reminders.py`、
   `agent/context.py`、其它提醒/集成测试、README、架构/决策文档与本任务卡。

所有子 Agent 禁止修改他人文件、共享任务卡、`config.json`、`.workbuddy/`、workspace、
sessions、identity、日志、图片、`deploy/`、`scripts/`、运行实例；禁止 commit、push、部署
或真实外部调用。

### 验收标准

- [x] 首次微信绑定、同 owner 重绑、解绑、跨身份/跨渠道抢占拒绝均有测试。
- [x] 旧飞书 SQLite 自动迁移且 target/task/execution 不丢失。
- [x] 静态与动态提醒均按目标渠道投递，微信重试复用稳定 correlation ID。
- [x] 解绑、`session_expired`、`context_missing` 暂停任务，重绑后恢复而非终态失败。
- [x] 微信绑定命令不进入 Agent/图片合并，allowlist 与停止/错误路径有覆盖。
- [x] 不修改 Node IPC；fake Bridge/clock/SQLite/Bus 完成跨层重启验证。
- [x] 文档同步且无新增配置项。

### 必须执行的验证

```bash
git diff --check
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q agent bus channels providers reminders session tests
uv run python -c "import main"
cd integrations/weixin_bridge && npm test && npm run build
```

真实微信扫码、消息、付费模型、push 和部署不在自动验收中；只在用户后续明确执行
手工测试时进行。

## 执行交接

- 状态：实现和自动化验收完成
- 实际改动文件：`README.md`、`agent/context.py`、`agent/tools/reminders.py`、
  `channels/weixin.py`、`main.py`、`reminders/{models,repository,scheduler,service}.py`、
  提醒/微信相关测试，以及本任务卡和架构、决策、历史提醒任务文档。
- 实现摘要：提醒目标改为渠道无关的 recipient/owner 模型；旧飞书 SQLite 原位迁移；
  微信私聊支持绑定、解绑、稳定主动投递回执及登录/context 失效后的暂停恢复；复用现有
  Scheduler、Bus、Gateway 和 Bridge，没有新增 Node IPC 或配置项。
- 关键决策与假设：原实现首次成功绑定会永久锁定渠道与 owner；2026-07-29 换绑补充
  已将其替代为“主动解绑可释放、系统暂停不释放”的状态契约。
- 验证命令与结果：`git diff --check` 通过；Python 全量 168 项通过；Python compileall
  与 `import main` 通过；Bridge 35 项测试和 TypeScript build 通过。
- 未验证项：真实微信端点与真实模型
- 风险与遗留问题：第一版不支持双投递；渠道 API 接受仍不等于用户已读，
  at-least-once 崩溃窗口仍可能造成极低概率重复发送。
- commit：实现提交 `bd02252`，已按用户授权 cherry-pick 为本地主干 `d731920`；最终验收
  记录由后续纯文档提交补齐
- 当前 `git status --short --branch`：实现提交后任务 worktree 无代码改动；本地主干仅有
  用户原有未跟踪图片和 `output/`，均未读取或改动
- 建议下一步：由用户在本地主干重启实例，完成真实微信绑定与到点发送手工验收

## 负责人验收

- [x] 检查 diff 与授权范围
- [x] 独立复跑关键验证
- [x] 检查秘密/个人数据/运行产物
- [x] 检查文档与配置一致性
- [x] 更新 `docs/DECISIONS.md` 中相关状态
- 验收结论：通过；只读审查无 blocker / P1
- 证据与备注：Python 168/168、Bridge 35/35、Bridge build、compileall、入口导入和
  `git diff --check` 均通过；未调用真实微信、真实模型、push 或部署。

## 2026-07-29 换绑补充交接

- 状态：实现、负责人复验与只读审查完成
- 改动文件：`reminders/models.py`、`reminders/repository.py`、`reminders/service.py`、
  `tests/reminders/test_repository.py`、`tests/reminders/test_integration.py`、`agent/context.py`、
  README、架构/决策文档与本任务卡
- 实现摘要：SQLite 升级到 `user_version=2` 并新增 `rebind_released` 与递增的
  `binding_revision`；原 owner 的明确解绑可在活动或系统暂停状态释放，下一次绑定原位
  更新同一 target；自动暂停保持锁定；绑定事务串行化且旧渠道回执按绑定代次条件暂停
- 验证：Python 全量 168/168；Bridge 35/35 及 build；compileall、`import main`、
  `git diff --check` 均通过
- 未验证：真实飞书→微信换绑、真实微信主动投递、真实模型
- 审查：初审发现并发抢绑和旧微信回执误暂停新目标两个 P1；修复后复审无 blocker、
  P1 或 P2
- 验收时 Git：detached worktree 有 11 个授权文件待提交；未读取或改动敏感运行产物
