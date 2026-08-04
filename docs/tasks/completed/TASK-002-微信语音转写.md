# TASK-002：微信语音转写（复用腾讯 STT，不落地本地 ASR）

## 任务卡

- 状态：实现完成，待验收（code-master 已完成代码/测试/文档）
- 负责人：乖宝（验收）
- 执行会话/子 Agent：code-master（实现）
- 基线 commit / 分支：main @ 7c57a95
- 依赖任务：无（TASK-001 已完成）

### 目标

微信用户发来的**语音消息**能被 Agent 理解并回复。实现方式：**直接用 iLink 协议 `voice_item.text`（腾讯服务端已转写好的文本）**，不落地本地 ASR。用户发语音 → 小奈收到转写文本 → 正常回复。

### 非目标

- **不做本地 ASR fallback**（乖宝 2026-08-04 拍板：腾讯 text 为空时保持现状——语音消息不处理，不引入本地 ASR / silk 解码器）
- 不改变 Python 侧入站协议（`channels/weixin.py` 预期零改动：转写文本合并进现有 text 路径）
- 不下载/保存语音文件（腾讯 text 够用，无需落盘音频）
- 不做语音消息的 UI 标记（如「🎤 语音转写」），直接作为普通文本进入

### 允许修改

- `integrations/weixin_bridge/bridge.mjs`：
  - 扩展 `extractText`：除 TEXT 项外，收集 VOICE 项的 `voice_item.text`（非空时追加，换行分隔）
  - **移除临时调研探针**（`[voice-probe]` 代码块 + `_vprobe` 辅助 + 文件写入），代码保持干净
- `integrations/weixin_bridge/test/`：新增语音转写测试（fake iLink 构造带 VOICE item 的入站消息）
- 相应文档：PROJECT.md 能力矩阵 / DECISIONS.md 决策记录

### 禁止修改

- `channels/weixin.py`（除非实现中发现必须，先停下问主 Agent）
- `voice/`（本地 ASR 模块，本次不用）
- 其它渠道 / Python 入站合并窗口逻辑
- config 结构

### 上下文与约束

- 相关代码入口：
  - `integrations/weixin_bridge/bridge.mjs`：`extractText(message)`（约 610 行）只取 `MessageItemType.TEXT`；`inbound_message` 事件 data 含 `text`/`images`
  - `integrations/weixin_bridge/vendor/wechat-ilink-client/src/api/types.ts`：`VoiceItem.text?: string`（腾讯 STT，实测 2026-08-04 有值：「哈喽哈喽，小奈在吗」一字不差）
  - 实测探针结果（2026-08-04，临时探针已确认）：`voice_item.text` 有值、语音可下载但实际编码 silk（`#!SILK_V3`）——**因此本地 ASR fallback 需 silk 解码器，乖宝决定不做**
  - 探针实现位置：`const images = await inboundImages(message, signal);` 之后（约 819 行），正式实现时移除探针、把 VOICE 文本合并进 `extractText`
- 相关架构/历史决策：
  - DECISIONS.md：微信采用固定 Node Bridge（vendor 固定提交）；入站 ack 后批次提交 cursor
  - 微信入站目前只处理 TEXT + IMAGE；VOICE/FILE/VIDEO 忽略（本次只做 VOICE 的 text 转写，FILE/VIDEO 仍忽略）
- 已知风险：
  - 腾讯 `text` 偶发为空 → 语音消息保持现状（忽略），不降级
  - 腾讯转写可能有误转/方言不准 → 直接采用，不二次校正
  - 多条语音/图文混合消息：text 合并顺序需稳定（TEXT 项在前、VOICE 转写追加在后）

### 验收标准

- [x] 入站消息含 VOICE 项且 `voice_item.text` 非空 → `inbound_message.text` 包含转写文本（TEXT 项 + VOICE 转写，换行分隔）
- [x] 纯语音消息（无 TEXT 项）→ text 即语音转写
- [x] VOICE 项无 text → 行为不变（忽略语音，仅 TEXT 项进 text）
- [x] 临时探针完全移除（bridge.mjs 无 `voice-probe` / `_vprobe` 残留；`/tmp/nanoclaw-voice-probe.log` 删除）
- [x] `npm test`（bridge 43 用例：原 39 + 新增 4）全过；`npm run build`（node --check）通过
- [x] 实机验证（乖宝微信发语音 → 小奈能听懂并回复）——依赖真实端点，需用户受控验收
- [x] 文档同步：PROJECT.md 能力矩阵 + DECISIONS.md 决策

### 必须执行的验证

```bash
cd integrations/weixin_bridge && npm test && npm run build
git diff --check
```

## 执行交接

- 状态：**实现完成，待验收**（实机语音链路待乖宝受控验证）
- 实际改动文件：
  - `integrations/weixin_bridge/bridge.mjs`（扩展 `extractText` + 移除探针）
  - `integrations/weixin_bridge/test/voice_transcript.test.mjs`（新增，4 用例）
  - `PROJECT.md`（能力矩阵 +「微信语音转写 ✅」行）
  - `docs/DECISIONS.md`（决策记录 +「微信语音直接用腾讯 STT」行）
  - 本任务卡（执行交接填写）
- 实现摘要：
  - `extractText` 改为两遍稳定合并：第一遍按 item 顺序收集全部 TEXT 项文本；第二遍收集 VOICE 项 `voice_item.text`（仅当 `typeof === 'string'` 且 `trim().length > 0`，去首尾空白后追加），TEXT 在前、VOICE 转写在后，换行分隔。
  - 完整删除 `[voice-probe]` / `_vprobe` / `fs.appendFileSync('/tmp/nanoclaw-voice-probe.log')` 探针块（HEAD 中本无探针，其为未提交临时代码，现已清除，无残留）。
  - 不下载语音、不落盘音频、不引入 silk 解码；VOICE 无 text 行为不变。
- 关键决策与假设：
  - 转写文本 push 前执行 `.trim()`：与收集条件（`trim().length > 0`）一致，避免 STT 文本首尾空白污染换行合并；对转写文本无信息损失。
  - TEXT/VOICE 两遍收集（而非单遍按 item 交错）刻意实现「TEXT 在前、VOICE 追加在后」的稳定顺序，与任务卡一致，且多图文+多语音混合消息顺序可预期。
  - `fs` import 保留：文件其它处（`controlledImage`、StateStore 等）仍使用 `fs.*`（共 13 处）。
  - Python 侧零改动：`inbound_message.data.text` 仍是字符串，Python 入站协议/`channels/weixin.py` 无需感知来源。
- 验证命令与结果：
  - `cd integrations/weixin_bridge && npm test` → `# tests 43 / # pass 43 / # fail 0`（原 39 + 新增 4）
  - `cd integrations/weixin_bridge && npm run build` → `node --check` 全部通过，exit 0
  - `git diff --check` → 无输出（clean）
  - `grep -rn "voice-probe\|_vprobe" integrations/weixin_bridge/bridge.mjs || echo "probe clean"` → `probe clean`
  - `rm -f /tmp/nanoclaw-voice-probe.log` → 已删除，`ls` 确认 absent
  - 未触碰检查：`git diff --name-only | grep -E "weixin.py|voice/|channels/|config"` → 无匹配
- 未验证项：
  - 实机语音链路（乖宝微信发语音 → 小奈听懂并回复）——需真实端点 + 扫码授权，依赖 NC-WEIXIN-001 类受控手工验收
  - 腾讯转写误转/方言场景（直接采用，不二次校正，属已接受风险）
- 风险与遗留问题：
  - 腾讯 `text` 偶发为空 → 语音消息保持忽略，不降级（既有决策）
  - VOICE/FILE/VIDEO 中非 VOICE 项仍忽略（本次范围外）
  - 转写文本将作为普通文本进入 Agent 回复内容，无「🎤」类 UI 标记（任务非目标）
- commit（仅在获授权时）：未提交（未授权 commit/push）
- 当前 `git status --short --branch`：
  ```
  ## main...origin/main
   M PROJECT.md
   M docs/DECISIONS.md
   M integrations/weixin_bridge/bridge.mjs
  ?? docs/tasks/active/TASK-002-微信语音转写.md
  ?? integrations/weixin_bridge/test/voice_transcript.test.mjs
  ?? kb-testset/
  ```
- 建议下一步：乖宝受控实机验证语音链路后验收本任务；如需提交，由项目负责人授权 commit。

## 负责人验收

- [ ] 检查 diff 与授权范围
- [ ] 独立复跑关键验证
- [ ] 检查秘密/个人数据/运行产物
- [ ] 检查文档与配置一致性
- [ ] 更新 `docs/DECISIONS.md` 中相关状态
- 验收结论：**通过** ✅（乖宝 2026-08-04 微信实机验证：发语音后小奈正常回复「我能听懂语音了」）
- 证据与备注：
