# TASK-003：微信文件接收（按月归档 + 按需读取）

## 任务卡

- 状态：已完成（2026-08-05 乖宝实机验收通过）
- 负责人：乖宝（验收）
- 执行会话/子 Agent：code-master（实现）
- 基线 commit / 分支：main @ e27bbe0
- 依赖任务：无（TASK-001/002 已完成）

### 目标

微信用户传的文件能被 Agent 感知并按需读取。**设计核心（乖宝 2026-08-04 拍板）**：
- 文件**按月归档**到统一目录 `workspace/files/YYYY-MM/`（不是按会话分散存放，便于乖宝查找）
- **发给大模型只给名字/路径/大小引用，不读内容、不花 token**；乖宝说「帮我看看」时 Agent 再 `read_file` 按需读取内容
- 文件是长期资产：`/clear` 会话**不删文件**，按月留存，由用户自己管理

### 非目标

- 不解析二进制文档内容（PDF/Word 等 v1 只告知「收到文件但读不了内容」，不引入解析库）
- 不做文件出站发送（本次只收，小奈暂不能给乖宝发文件）
- 不处理飞书/网页渠道的文件（本次只做微信；其它渠道后续再说）
- 不改 AgentLoop 核心（文件引用直接拼进入站 content 文本，run 签名不变）

### 允许修改

- `integrations/weixin_bridge/bridge.mjs`：新增 FILE 入站下载（仿 `inboundImages`：下载到临时目录 → `inbound_message.data.files = [{file_path, file_name, size}]`）
- `bus/queue.py`：新增 `FileRef` dataclass（id/path/name/size/mime 可空）+ `InboundMessage.files: Optional[List[FileRef]]`
- `channels/weixin.py`：处理 `data.files` → 读临时文件字节 → `FileStore.save` → 构造 `FileRef` + 把文件引用（相对路径+大小）拼进入站 content 文本
- `agent/filestore.py`（新增）：`FileStore`，落盘 `workspace/files/YYYY-MM/`，文件名消毒 + 重名加后缀（`-1`/`-2`）+ 大小上限 + 0600 权限
- `main.py`：装配 FileStore（若需要）
- 测试：bridge 测试 + Python unittest
- 文档：PROJECT.md 能力矩阵 / DECISIONS.md 决策记录

### 禁止修改

- `agent/loop.py` 的 run 签名与核心循环（文件引用走 content 文本）
- config 结构（文件大小上限 v1 用代码常量）
- 其它渠道 / 图片链路 / 语音链路

### 上下文与约束

- 相关代码入口：
  - `integrations/weixin_bridge/vendor/wechat-ilink-client/src/api/types.ts`：`FileItem { media?, file_name?, md5?, len? }`；`src/media/download.ts`：`downloadMediaFromItem` 有 FILE 分支（下载+解密，返回 `{data, kind:"file", fileName}`）
  - `bridge.mjs`：`inboundImages`（约 630 行）是下载落盘范式；`extractText` 已处理 TEXT+VOICE；FILE 项当前忽略
  - `channels/weixin.py`：`_consume_inbound_image`（图片：读临时文件字节 → ImageStore.save）；`_handle_inbound` 合并窗口
  - `agent/imagestore.py`：ImageStore 范式（会话目录落盘）——**本次文件不走会话目录，走按月目录**，参考其 save/权限模式
  - `agent/tools/filesystem.py`：`read_file` 读工作区内文件，Agent 按需读取用（文件在 `workspace/files/` 下，天然可读）
- 相关架构/历史决策：DECISIONS.md 微信固定 Node Bridge；入站 ack 后批次提交 cursor；「文件引用 vs 内容」分离（乖宝设计：发消息只带引用，按需读内容）
- 已知风险：
  - 文件名不可信：需消毒（去路径分隔符/控制字符/空字符、限长），防路径穿越
  - 文件大小不可控：设上限（建议 50MB，超限丢弃并提示），防撑爆磁盘
  - 重名：同一月目录下同名文件加 `-1`/`-2` 后缀
  - 二进制文件 read_file 读不了：Agent 应告知「收到但读不了内容」
  - 月度目录不存在时自动创建（0700）

### 验收标准

- [x] 微信传文件 → `inbound_message.data.files` 含 {file_path, file_name, size}
- [x] Python 端 FileStore 落盘 `workspace/files/YYYY-MM/原名`（消毒后），重名自动加后缀，大小超限丢弃并提示
- [x] 入站 content 文本带文件引用（如 `📎 收到文件：files/2026-08/xxx.md（1.2MB）`），**不含文件内容**
- [x] 纯文件消息（无文本）也有默认文本（如「收到文件，请告诉我你想让我看什么」或仅引用文本），能进 Agent
- [x] 二进制/不可读文件：Agent 能感知存在但告知读不了内容（无需特殊代码，read_file 失败时自然处理；测试覆盖引用文本正确即可）
- [x] Bridge `npm test` 全过（新增 FILE 用例）+ Python unittest 全过
- [x] 实机验证（乖宝微信传文件 → 小奈知道收到并能在乖宝要求下读取内容）
- [x] 文档同步：PROJECT.md 能力矩阵 + DECISIONS.md 决策

### 必须执行的验证

```bash
cd integrations/weixin_bridge && npm test && npm run build
.venv/bin/python -m unittest discover -s tests
git diff --check
uv run python -m compileall -q gateway agent channels bus main
uv run python -c "import main"
```

## 执行交接

- 状态：实现完成，待负责人验收
- 实际改动文件：
  - `integrations/weixin_bridge/bridge.mjs`：新增 `inboundFiles`（FILE type=4 入站下载+解密 → `state/inbound/<uuid>.file` 临时文件 → `data.files=[{file_path,file_name,size}]`）；下载失败/超限仅 `channel_error` 并跳过，不影响 ack 与其它消息；新增 `MAX_INBOUND_FILE_BYTES`（默认 50MB，env 可配）
  - `integrations/weixin_bridge/test/file_inbound.test.mjs`（新增）：5 个用例——纯文件下载落盘、文本+文件混合、无 FILE 回归、超限跳过、CDN 失败跳过
  - `bus/queue.py`：新增 `FileRef`（id/path/name/size/mime 可空）；`InboundMessage.files: Optional[List[FileRef]] = None`
  - `agent/filestore.py`（新增）：`FileStore`（按月目录 `files/YYYY-MM/`、文件名消毒/限长 200/防穿越/空名兜底 `file`、重名 `-1/-2` 后缀、`MAX_FILE_BYTES=50MB` 超限抛 `FileTooLargeError`、目录 0700/文件 0600、`list_files(month)`、`delete(ref)` 回滚）
  - `channels/weixin.py`：`file_store` 构造参数 + `max_inbound_file_bytes`；`_consume_inbound_file`（读字节→FileStore.save→FileRef，读失败/超限丢弃并清理临时文件）；`_build_content` 把引用拼进 content（`📎 收到文件：files/YYYY-MM/name（大小）`）；批次 `_PendingMessageBatch.files` 持久化/恢复；`_discard_duplicate_inbound_file` 重投去重；`NANOCLAW_WEIXIN_MAX_INBOUND_FILE_BYTES` 环境变量
  - `main.py`：创建 `FileStore(workspace/files)` 进 shared，`build_weixin_channel` 增加 `file_store` 参数并透传
  - `tests/test_filestore.py`（新增，11 例）、`tests/test_weixin_files.py`（新增，7 例）、`tests/test_weixin_main.py`（+1 例装配）
  - `PROJECT.md`：能力矩阵新增「微信文件接收 ✅」；`docs/DECISIONS.md`：新增决策「微信文件按月归档+引用式传递，不按会话分散」+ 时间线
- 实现摘要：
  - 微信 FILE 入站：Bridge 按 vendor `downloadMediaFromItem` FILE 分支（`file_item.media.aes_key` 直接传 `downloadCdn`）下载解密到 `state/inbound/<uuid>.file`，`file_name` 只作 metadata
  - Python 端 `_consume_inbound_file` 读临时文件 → `FileStore.save` 落盘 `workspace/files/YYYY-MM/消毒名`；content 拼「📎 收到文件：相对路径（大小）」，**不读文件内容、不花 token**
  - 纯文件消息 content 即引用文本；文本+文件混合 = 文本在前 + 引用在后；`/bind` 等命令仍按原逻辑仅文本判断（未改原条件，文件不参与）
  - AgentLoop.run 签名与核心循环未改；Gateway 只消费 content（引用已内联）
- 关键决策与假设：
  - FileRef.path 与 content 均使用「相对 workspace 数据根的相对路径」`files/YYYY-MM/name`（任务卡示例格式）；实际读取时 Agent 的 read_file 工具根为 `config.workspace`（项目根），读 `workspace/files/...` 才能命中——见「风险」
  - 大小上限 v1 用代码常量 50MB（Bridge 与 Python 双侧各自 50MB，Bridge 侧防下载撑爆、Python 侧防落盘超限），不引入 config 结构
  - 命令消息（/bind、/new）保持原纯文本判断，文件不参与（任务卡明确「仍按原逻辑仅文本判断」）
  - 批次持久化文件引用（FileRef 元数据）而非字节——文件已落 FileStore 月目录（长期资产），恢复时直接重建引用
  - 纯文件消息不带「想让我看什么」提示语，仅引用文本（满足验收「或仅引用文本」）
- 验证命令与结果：
  - `cd integrations/weixin_bridge && npm test` → 48 tests pass（原 43 + 新增 5）；`npm run build` → exit 0
  - `.venv/bin/python -m unittest discover -s tests` → Ran 309 tests OK（原 290 + 新增 19：filestore 11 + weixin_files 7 + main 1）
  - `git diff --check` → 无输出（exit 0）；新文件手动检查无尾随空白/制表符
  - `uv run python -m compileall -q gateway.py agent channels bus main.py` → exit 0（任务原命令 `gateway agent channels bus main` 中 gateway/main 是非目录文件，compileall 报 Can't list 但 exit 0；改用显式 .py 路径确认全部编译）
  - `uv run python -c "import main"` → exit 0
- 未验证项：
  - 真实微信端点扫码传文件（需真实账号授权，属 NC-WEIXIN-001 范畴）
  - read_file 端到端读取路径的模型实测（见风险）
  - 二进制/不可读文件（PDF/Word）的 Agent 自然应答行为（无特殊代码，read_file 失败时自然处理）
- 风险与遗留问题：
  - **read_file 路径语义**：content 中引用为 `files/YYYY-MM/name`（相对数据根 `workspace/`）；而 `agent/tools/filesystem.py::ReadFileTool` 根是 `config.workspace`（默认项目根）。Agent 按引用读需用 `workspace/files/YYYY-MM/name`。若要让 `files/...` 直接可读，需另开任务统一 read_file 根或调整路径语义
  - 命令消息（/bind、/new）携带文件时文件会被丢弃（与图片路径不对称——图片+命令算普通轮）；任务卡明确命令保持原逻辑，如需文件+命令共存需另行决策
  - 投递失败（Bus 拒绝）时已落盘文件会被 `_delete_file_refs` 回滚删除；若删除前进程崩溃，重投会生成 `-1` 副本（at-least-once 边界内可接受）
  - 大小上限两侧各 50MB 常量：若有人单独调低 Bridge env 上限，Python 侧 `max_inbound_file_bytes` 仍是 50MB（读满 50MB 不会超，无实际风险）
  - 月度目录按创建时墙钟月份归档；跨午夜场景文件归属旧月份（可接受）
- commit（仅在获授权时）：未 commit / 未 push
- 当前 `git status --short --branch`：`M bus/queue.py, M channels/weixin.py, M integrations/weixin_bridge/bridge.mjs, M main.py, M tests/test_weixin_main.py` + 未跟踪 `agent/filestore.py, tests/test_filestore.py, tests/test_weixin_files.py, integrations/weixin_bridge/test/file_inbound.test.mjs, docs/tasks/active/TASK-003-微信文件接收.md`；`kb-testset/` 为既有未跟踪目录（未改动）
- 建议下一步：
  - 实机验证：乖宝微信传文件 → 确认小奈收到引用、按「帮我看看」read_file 读取
  - 视实机结果决定是否调整引用路径语义（`files/...` vs `workspace/files/...`）
  - 后续可考虑微信文件出站、飞书/网页渠道文件、二进制解析（v2）

## 补充修复（2026-08-05 实机验收后）

- 现象：乖宝微信传 docx，微信端 Agent 收到文件引用后**自行读取了内容**（先 read_file 失败后用 exec + zipfile 解出 docx 文本），违背「按需读取」设计（等乖宝说「帮我看看」再读）
- 根因：代码层只保证「引用式传递」（content 不含内容），但**模型拿到引用后可能主动读取**——行为约束需在 System Prompt 层
- 修复：`agent/context.py::_channel_section` 微信分支新增规则：收到 📎 文件引用只确认收到，**不主动读取内容（含 read_file / exec 等任何方式）**，等用户明确说「帮我看看 / 读一下」后再读
- 验证：`.venv/bin/python -m unittest discover -s tests` → 309 tests OK（context 快照文案变更，无专门断言但全量回归通过）
- 文档：DECISIONS.md 新增决策 + 时间线 08-05 条目；本任务卡记录
- 实机效果：需重启后乖宝微信再传文件验证「只确认收到、不读内容」

## 负责人验收
## 负责人验收

- [ ] 检查 diff 与授权范围
- [ ] 独立复跑关键验证
- [ ] 检查秘密/个人数据/运行产物
- [ ] 检查文档与配置一致性
- [ ] 更新 `docs/DECISIONS.md` 中相关状态
- 验收结论：**通过**（2026-08-05 乖宝实机验收）
- 证据与备注：
  - 实机：微信传 docx → 落盘 `workspace/files/2026-08/调试文档.docx`（982KB）→ 回复「📎 收到文件」引用、不含内容；乖宝要求读取后 Agent 用 exec+zipfile 完整读出 docx 文本并整理要点
  - 实机补充发现：微信端 Agent 收到引用后曾自行读取（read_file 失败后用 exec 读取），违背「按需读取」；已补渠道快照行为约束（`agent/context.py::_channel_section` 微信分支：只确认收到，不主动读，等「帮我看看」再读），乖宝重启 + /new 新会话后复验通过
  - 回归：309 tests OK；`git diff --check` exit 0；compileall / import main 通过
