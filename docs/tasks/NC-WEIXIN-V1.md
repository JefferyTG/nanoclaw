# NC-WEIXIN-V1：微信私聊渠道 V1

## 任务卡

- 状态：实现完成（离线验收通过；真实微信手工验收待授权）
- 负责人：当前 Codex 主会话（唯一项目负责人、架构与集成负责人）
- 执行子 Agent：
  - Bridge 实现：独占 `integrations/weixin_bridge/**`
  - Python Channel 实现：独占 `channels/weixin.py`、`tests/test_weixin_channel.py`
  - 安全/可靠性审查：实现结束后只读审查
- 基线 commit / 分支：`eed850f` / detached HEAD
- 依赖任务：无

### 目标

为 NanoClaw 新增一个单微信账号、仅私聊的微信渠道。V1 必须支持扫码登录、
文本和图片收发、显式访问控制、凭据/cursor/context token 持久化、断线恢复、
可靠发送回执和完全离线的自动化测试。渠道只负责协议与消息搬运，继续复用现有
`Channel → MessageBus → Gateway → AgentLoop → SessionManager/ImageStore` 链路。

特别验收场景：某个允许的 `user_id` 至少入站交互一次后，Bridge/NanoClaw
均重启，仍可仅凭稳定的 `account_id + user_id` 主动发送，并获得服务端 API
接受或拒绝的结构化回执；调用方不得保存或传入临时 `context_token`。

### 非目标

- 不实现群聊、语音、视频、普通文件或多微信账号。
- 不修改提醒业务、绑定命令或提醒数据库；只为未来主动提醒提供稳定发送基础。
- 不运行完整 OpenClaw，不复制 AgentLoop、会话或提醒调度逻辑。
- 不调用真实微信、飞书或付费模型；真实扫码和消息联调留作获授权后的手工验收。
- 不修复现有 session key 有损映射、Web 管理面或 workspace 沙箱等相邻遗留问题。

### 上游来源与许可证基线

- 社区基础：`https://github.com/photon-hq/wechat-ilink-client`，固定提交
  `b3e5944`，版本 `0.1.0`，Node `>=20`。截至 2026-07-29，仓库仍只有两个
  commit；`package.json` 声明 MIT，但仓库没有独立 `LICENSE` 文件。
- 正确性对照：`https://github.com/Tencent/openclaw-weixin`，固定核验提交
  `cef0bfc`，版本 `2.4.6`，有完整 Tencent MIT `LICENSE`。
- vendor 必须记录源 URL、固定提交、未修改/已修改文件和本地补丁说明；不得使用
  `npm latest` 或运行时 Git 拉取。不得虚构社区项目版权人或补写不存在的版权行。
- 若实现逐字复制或实质改编 Tencent 源码，必须同时保留 Tencent 的 MIT 许可证；
  仅作为行为事实源独立实现时，也要在 NOTICE 中记录对照提交。
- 社区仓库缺少独立许可证文件是发布风险：V1 可保留其 `package.json` MIT 声明与
  来源 NOTICE 继续实现，但最终发布/分发前仍应向上游确认或由维护者做法律复核。

### 架构与边界

```text
WeixinChannel (Python)
  ├─ asyncio 子进程生命周期、JSONL 请求/事件路由
  ├─ allowlist、稳定 target 编解码、ImageStore 映射
  └─ DeliveryResult 映射
          ⇅ stdin/stdout JSON Lines（stdout 禁止非协议输出）
Weixin Bridge (Node >=20)
  ├─ 固定 vendor 的 iLink client + 本项目补丁
  ├─ 登录、长轮询、文本/图片、CDN AES
  ├─ 敏感状态、context token、cursor、去重
  └─ 超时/取消、退避、错误分类、优雅停止
          ⇅ fake/real iLink HTTP + CDN
```

- Bridge 独占敏感 token、cursor、context token；Python 普通消息和日志中不得出现。
- Bridge 状态目录默认是已被 Git 忽略的 `workspace/weixin/`，由配置显式指定。
  目录权限必须为 `0700`，敏感状态文件为 `0600`，所有更新使用同目录临时文件、
  `fsync`（可用时）与原子 rename。状态目录不得落在 vendor/源码目录。
- Python 使用稳定、可逆、无分隔符碰撞的 target 编码承载
  `account_id + user_id`；`InboundMessage.sender_id` 和 `chat_id` 均使用该 target，
  因而 Gateway 会话天然按账号与对端隔离。提供公开的 target 编解码 helper，未来
  提醒只持久化稳定 target 或账号/用户二元组，不持久化 context token。
- 配置使用单个 `weixin` 字典，至少含 `enabled`、`bridge_command`、`state_dir`、
  `allowed_user_ids`、命令/登录/入站确认超时。`allowed_user_ids=[]` 为 deny-all；
  精确 user ID 或显式 `"*"` 才允许入站。出站同样必须通过 allowlist。
- `main.py` 只在 `weixin.enabled=true` 时装配渠道；Node 构建物缺失或版本不满足时
  产生明确、可操作错误，不影响未启用微信时的正常导入。

### JSONL IPC v1 契约

每行是一个完整 UTF-8 JSON 对象，协议版本固定为 `1`，最大行长有界。stdin 仅接收
命令/确认，stdout 仅输出 response/event；诊断只写 stderr 且必须脱敏。

命令：

```json
{"v":1,"type":"request","id":"req-1","method":"login","params":{"force":false}}
{"v":1,"type":"request","id":"req-2","method":"start","params":{}}
{"v":1,"type":"request","id":"req-3","method":"send_text","params":{"account_id":"...","user_id":"...","text":"...","correlation_id":"stable-id"}}
{"v":1,"type":"request","id":"req-4","method":"send_image","params":{"account_id":"...","user_id":"...","file_path":"/validated/path","caption":"...","correlation_id":"stable-id"}}
{"v":1,"type":"request","id":"req-5","method":"ack_inbound","params":{"delivery_id":"..."}}
{"v":1,"type":"request","id":"req-6","method":"stop","params":{}}
```

- 首个请求必须完成 `hello`/能力握手（可由 Bridge 启动事件或显式 `hello` 请求实现），
  核对协议版本、Bridge 版本与运行时能力。
- `login` 在过程中发 `qr_code` 事件，最终确认后才返回成功 response；已有有效凭据且
  `force=false` 时可直接返回账号信息。所有登录等待都可取消且受总截止时间约束。
- `start` 从原子状态恢复凭据/cursor/context/去重后启动一个长轮询；重复调用幂等。
- `send_*` 必须由调用方传稳定 `correlation_id`。文本分块和图片/说明子消息从它
  确定性派生 client ID，重试同一相关 ID 时不得重新随机。Bridge 自行按
  `account_id + user_id` 解析 context token。
- `file_path` 仅用于已经由 Python 校验的本地图片；Bridge 再做普通文件、大小、MIME
  和允许根目录校验。不得接受 URL、任意媒体类型或把路径写入日志/事件。
- response：

```json
{"v":1,"type":"response","id":"req-3","ok":true,"result":{"correlation_id":"stable-id","provider_message_id":"..."}}
{"v":1,"type":"response","id":"req-3","ok":false,"error":{"code":"api_rejected","message":"redacted","retryable":false,"provider_code":123}}
```

异步事件至少包含：

```text
ready
qr_code
login_status
login_success
inbound_message
delivery_result
session_expired
channel_error
stopped
```

`inbound_message.data` 至少含 `delivery_id/account_id/user_id/message_id/text/images`。
图片通过 Bridge 状态目录内的受控临时 `file_path + mime_type` 传给 Python，不把大段
base64 塞入 JSONL；Python 校验路径、读取并存入共享 ImageStore 后删除临时文件。
只有 `bus.publish_inbound()` 成功后 Python 才发送 `ack_inbound`。

`delivery_result.data` 至少含 `correlation_id/success/retryable/code/provider_code/
provider_message_id/message`。成功只表示 iLink `sendmessage` 的 HTTP 和 JSON
`ret/errcode` 均接受，不表示用户已读。命令 response 和异步回执必须一致且至多一次
完成 Python pending future。

### 持久状态模型与崩溃语义

状态 schema v1：

```json
{
  "version": 1,
  "account": {
    "account_id": "...",
    "bot_token": "secret",
    "base_url": "server supplied",
    "route_tag": "optional",
    "login_user_id": "optional",
    "cdn_base_url": "optional"
  },
  "cursor": "opaque get_updates_buf",
  "context_tokens": {
    "account_id": {"user_id": {"token":"secret","updated_at_ms":0}}
  },
  "processed_message_ids": ["bounded durable IDs"]
}
```

- 服务端在登录/重定向返回的有效 `baseurl`、`redirect_host`/route 必须成为后续请求的
  实际地址；禁止确认登录后又回退默认 host。
- 二维码状态至少处理：`wait`、`scaned`、`confirmed`、`expired`、
  `scaned_but_redirect`、`need_verifycode`、`verify_code_blocked`、
  `binded_redirect`。需要验证码时发状态事件并等待受支持输入；若 V1 暂无交互输入，
  必须明确失败而不是无限 wait。blocked/expired 有界刷新。
- 每个入站消息先原子保存该 `account_id + user_id` 的最新 context token，再发事件；
  Python ack 后才把消息 ID 加入持久去重集合；一批消息全部完成/去重后，最后原子推进
  cursor。禁止“先保存 cursor、后派发”。
- 崩溃允许重复、不允许静默丢失：事件发出但 ack/去重落盘前崩溃，重启后可重发；
  已持久去重但 cursor 未推进时，重拉批次应跳过该消息；去重集合必须有明确上限。
- `errcode/ret=-14` 立即清除当前 account、cursor、context token 与去重状态、停止
  poll、发 `session_expired`，等待重新扫码；旧 context 不跨凭据代次复用，对端需
  再次交互才能恢复主动发送。不得固定睡眠一小时后继续用失效 token。
- 网络、HTTP、JSON API 拒绝、超时、取消、会话失效、上下文缺失、媒体、协议与内部
  错误必须分类；退避可取消、带上限并可注入 fake clock/sleep 测试。

### 允许修改与文件所有权

- Bridge 子 Agent 独占：`integrations/weixin_bridge/**`。
- Python Channel 子 Agent 独占：`channels/weixin.py`、`tests/test_weixin_channel.py`。
- 项目/集成负责人独占：`bus/queue.py`、`config.py`、`main.py`、
  `config.example.json`、`README.md`、`docs/ARCHITECTURE.md`、
  `docs/DECISIONS.md`、本任务卡及任何共享协议/入口测试。
- 审查 Agent 只读，不修改任何文件。

### 禁止修改

- `config.json`、`.workbuddy/`、`workspace/`、`sessions/`、`identity*.md`、日志、
  图片、`deploy/`、`scripts/`、本地虚拟环境和用户的 `Camera_*.jpg`。
- 未经授权不 commit、push、部署、改运行实例或调用真实外部副作用链路。
- 子 Agent 不得修改另一所有者文件，不得更新任务状态或自行扩展产品范围。

### 验收标准

- [x] 单账号私聊扫码登录；全部二维码状态、服务端 base URL/route、取消/超时有测试。
- [x] 文本与图片双向映射到现有 Bus/ImageStore；非图片媒体与未授权用户被拒绝。
- [x] send API 同时校验 HTTP 与 JSON `ret/errcode`，结构化回执正确区分可重试性。
- [x] 调用方稳定 correlation/client ID、文本分块、超时/取消/退避、优雅停止有测试。
- [x] 凭据、cursor、context token 与去重按 schema 原子持久化且权限正确。
- [x] cursor 在派发/ack 后提交；崩溃窗口体现 at-least-once 且无静默丢消息。
- [x] `-14` 会话失效停止轮询、清理旧代次状态并要求重新登录。
- [x] “交互一次 → 双端重启 → account_id+user_id 主动发送 → 可靠回执”离线集成测试通过。
- [x] fake iLink HTTP/CDN/clock/process 覆盖正常、拒绝、超时、断线、重启与停止；
  自动化测试不需要真实微信、飞书或模型凭据。
- [x] 配置示例、README、架构和决策文档与实现一致，来源与许可证 NOTICE 完整。

### 必须执行的验证

```bash
git diff --check
cd integrations/weixin_bridge && npm ci
cd integrations/weixin_bridge && npm test
cd integrations/weixin_bridge && npm run build
uv run python -m py_compile channels/weixin.py config.py main.py
uv run python -c "import main"
uv run python -m unittest tests.test_weixin_channel
uv run python -m unittest discover -s tests
git status --short --branch
```

若 Node 脚本名或 Python 测试路径最终不同，交接时必须列出等价的实际命令；负责人
还要独立复跑关键验证并检查 diff 中没有敏感状态、构建产物、`node_modules` 或图片。

## 执行交接

- 状态：V1 实现与离线自动化验收完成；未调用真实微信或付费/外部副作用接口。
- 实际改动文件：
  - Python/共享入口：`channels/weixin.py`、`bus/queue.py`、`config.py`、
    `config.example.json`、`main.py`。
  - Python 测试：`tests/test_weixin_channel.py`、`tests/test_weixin_config.py`、
    `tests/test_weixin_main.py`。
  - Node Bridge：`integrations/weixin_bridge/bridge.mjs`、`lib/*.mjs`、
    `test/*.mjs`、`package*.json`、`.gitignore`、`NOTICE.md`、腾讯 MIT 文本和
    `vendor/wechat-ilink-client/**` 固定源码。
  - 文档：`README.md`、`docs/ARCHITECTURE.md`、`docs/DECISIONS.md`、本任务卡。
- 实现摘要：单账号微信私聊扫码、文本/图片双向、deny-all allowlist、稳定
  account/user target、JSONL 子进程生命周期、0700/0600 原子状态、ack 后 cursor、
  稳定 client ID、结构化成功/失败回执、`-14` 重新认证和断线恢复均已落地。
- 关键决策与假设：运行时精确依赖 `wechat-ilink-client@0.1.0` 并直接复用其协议
  常量、CDN URL 与 AES 原语；腾讯 2.4.6 作为 QR/header/状态事实源；旧 context
  不跨 `-14` 凭据代次；`binded_redirect` 仅在本地仍有凭据时作为成功恢复。
- 验证命令与结果：
  - `npm ci --ignore-scripts`：通过，3 packages，0 vulnerabilities。
  - `npm test`：32 passed。
  - `npm run build`：通过。
  - `uv run python -m unittest discover -s tests`：144 tests OK。
  - `uv run python -m py_compile channels/weixin.py config.py main.py bus/queue.py tests/test_weixin_channel.py tests/test_weixin_config.py tests/test_weixin_main.py`：通过。
  - `uv run python -c "import main"`：通过。
  - `uv run python -m json.tool config.example.json`：通过。
  - `git diff --check`：通过。
- 未验证项：真实微信扫码、真实 iLink HTTP/CDN、手机端文本/图片和主动发送均未执行；
  这些必须在用户明确授权后做受控手工验收。
- 风险与遗留问题：社区上游缺少独立 LICENSE，正式分发前仍需维护者/法律复核；
  真实端点可能暴露 fake 无法发现的兼容差异。
- commit：无（未授权）
- 当前 `git status --short --branch`：detached HEAD，只有本任务列出的未提交改动；
  `node_modules` 与状态目录保持忽略。
- 建议下一步：用户授权后用测试账号完成一次扫码、入站文本/图片、重启后主动发送
  的手工验收；通过后再由负责人决定提交/推送。

## 负责人验收

- [x] 检查 diff 与授权范围
- [x] 独立复跑关键验证
- [x] 检查秘密/个人数据/运行产物
- [x] 检查文档与配置一致性
- [x] 更新 `docs/DECISIONS.md` 中相关状态
- 验收结论：离线验收通过；真实微信验收按明确非目标保留为授权后手工步骤。
- 证据与备注：独立安全/可靠性复审提出的 QR headers、`binded_redirect`、
  `-14` context 代次及实际 vendor 使用四项 P1 均已修复并复审关闭；未发现 blocker。

## 2026-07-29 实机反馈跟进

- 问题：微信入站图片已生成无扩展名 `image_id`，但 `ask_image` 报图片未找到。
- 根因：WeixinChannel 使用 `<target>` 保存图片，Gateway/AgentLoop 却使用
  `weixin:<target>` 解析；无扩展名 ID 是 ImageStore 的正常公开标识，不是故障。
- 修复：入站图片改用 Gateway 完整会话键；新增默认 10 秒的同用户图文
  合并窗口，后续图片重置计时，`0` 表示关闭等待。
- 可靠性：等待批次先以 `0700/0600` 权限原子落盘再确认 Bridge；
  MessageBus 消费者实际取走消息后才删除恢复记录。停止会先取消未完成的
  交接和定时器，保留已落盘批次；恢复失败时 fail-closed，不启动新轮询。
- 安全：ImageStore 新建图片目录/文件显式收紧为 `0700/0600`。
- 自动验证覆盖：会话键到 `ask_image` 真实解析、图/文合并、连续图片重置、
  用户隔离、停止保留、重启恢复、重投去重、落盘失败不误 ack、Bus 交接崩溃
  窗口、恢复重试和私有权限。
- 验证：`npm test` 35 passed，`npm run build` 通过，Python 全量 164 tests OK；
  `py_compile`、`import main`、配置 JSON 校验和 `git diff --check` 均通过。最终只读
  安全/可靠性复审未发现 blocker。
- 未验证项：本跟进未调用真实微信、真实视觉模型或任何付费/外部副作用链路；
  合并后需由用户继续手工收图验收。

## 2026-07-29 微信端出站图片解密修复

- 问题：图片已上传且微信生成了消息气泡，但点开提示“图片已过期或已被清理”。
- 根因：Bridge 把原始 16 字节 AES 密钥直接做 Base64 写入 `media.aes_key`；
  腾讯当前实现要求先将密钥转成 32 位十六进制字符串，再对该 ASCII 字符串做 Base64。
- 修复：`getuploadurl.aeskey` 与 `sendmessage.media.aes_key` 由同一个十六进制密钥派生，
  保持 CDN 密文、上传元数据和图片消息三者一致。
- 回归覆盖：fake CDN 捕获真实上传密文，从图片消息还原协议密钥并完成 AES 解密，
  同时校验 `mid_size` 和上传请求密钥，避免 fake 接口只验证字段存在而与错误实现共同自洽。
- 验证：`npm test` 35 passed，`npm run build` 通过，Python 全量 164 tests OK；
  `import main` 和 `git diff --check` 通过。
- 未验证项：未调用真实微信或 CDN；自动化通过后仍需用户在手机端重新发送一张新图片验收。
