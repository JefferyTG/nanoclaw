# TASK-041：webui 输入栏「豆包化」（布局+按住说话交互+文件上传）

## 任务卡

- 状态：已完成（乖宝 2026-08-12 16:2x 真机验收通过）
- 负责人：乖宝（验收）/ code-master（实现）
- 执行会话/子 Agent：code-master
- 基线 commit / 分支：`8a4046a`（main，TASK-039 归档提交，工作区干净）
- 依赖任务：**与 TASK-040 冲突**——两者都改 `webui/index.html` + `channels/web.py`，AGENTS.md 文件所有权规则要求**顺序执行**（先做哪个由乖宝定；041 不依赖 040 的代码，逻辑独立）

### 目标

把 webui 底部输入栏改成「豆包式」布局与交互（参考乖宝 2026-08-12 提供的豆包手机端截图）：

1. **底部输入栏布局**（左→右）：`📷 相机 ｜ 大输入框（占位「发消息或按住说话...」）｜ 🎙 麦克风 ｜ ＋ 加号`
2. **去掉发送按钮**（回车发送）
3. **喇叭（自动朗读 ttsBtn）从底部移到右上角 header**
4. **麦克风 = 「打字 ↔ 按住说话」模式切换**（微信式）：
   - 常态：打字模式，输入框可输入
   - 点一下麦克风 → 输入框变「按住说话」条，键盘收起
   - 按住：条变深，显示「松开发送 · 上滑取消」
   - 上滑超过阈值：变红「松开取消」
   - 松开发送区 / 取消区 → 对应手势反馈
   - **本轮不真录音**：无录音内容，松开时弹轻提示占位（如「语音功能待开放，先打字吧」），手势交互完整做好，未来接录音时直接复用
5. **加号（＋）= 上传入口，本轮就做文件上传**：图片 + 文件（pdf/doc/txt/zip 等）都能传，Agent 可引用

### 非目标

- ❌ 真录音/语音转写（手机 HTTP 无 getUserMedia + 乖宝暂不做；「按住说话」仅交互与占位提示）
- ❌ 实时通话图标（右上角只放喇叭，不加通话入口）
- ❌ 修改聊天功能逻辑本身（文件引用按「文件名+路径+大小」文本化接入，不动 Agent 核心）
- ❌ 多端历史同步（TASK-040 独立处理）
- ❌ HTTPS/PWA/实时语音（后续任务）

### 允许修改

- `webui/index.html`：布局重排、麦克风模式切换与手势、相机/加号按钮、ttsBtn 移位
- `channels/web.py`：`/upload` 扩展支持非图片文件（复用 `agent/filestore.py` 底座，参考微信文件接收 TASK-003 思路）；入站消息带文件引用时文本化（文件名+路径+大小）
- 必要的小范围支撑：如 filestore 接口不满足 web 场景可小改（先说明）

### 禁止修改

- `main.py`、`gateway.py`、`config.json`、其它渠道代码（feishu/weixin/voice/realtime/cli）
- Agent 核心：会话管理、AgentLoop、工具注册、消息协议（不得给 Inbound/Outbound 增加会破坏其它渠道的字段）
- 未授权的 git commit / push

### 上下文与约束

- **参考截图**：豆包手机端（乖宝 08-12 提供）——底部：相机（拍照）｜输入框「发消息或按住说话...」｜麦克风（圆形声波）｜加号；右上角：通话+静音图标（咱们不加通话，只放喇叭）
- **相关代码入口**：
  - 前端 `webui/index.html`：`#bar`（L205 起：attachBtn🖼 / fileInput / recordBtn🎙 / ttsBtn🔇 / input / sendBtn 发送）
  - 后端 `channels/web.py`：`_handle_upload`（L199，仅图片，走 ImageStore）；入站消息图片引用处理（L512-520）；`/upload` 路由（L120）
  - `agent/filestore.py`：文件存储（微信 TASK-003 在用，按 YYYY-MM 归档、消毒名、重名后缀、50MB 上限）
  - `agent/imagestore.py`：图片存储（现状 web 在用）
- **当前底部元素**：🖼 attachBtn（fileInput accept=image/* 隐藏）、🎙 recordBtn（点击录音→/api/asr 转写）、🔇 ttsBtn（自动朗读开关）、input、sendBtn
- **已知风险**：
  - 移动端手势（touchstart/move/end）需同时兼容桌面（mouse 事件）与触屏；防误触
  - 键盘收起/弹出（iOS）与模式切换的时序
  - 文件上传类型校验/大小限制/路径安全（复用 filestore 已有消毒逻辑）
  - 文件引用进 Agent 的文本化格式需与微信渠道一致或合理（保持单一事实源）
  - 与 TASK-040 顺序执行，避免同文件并发冲突

### 验收标准

- [ ] 手机+桌面底部输入栏：`📷 输入框 🎙 ＋`，无发送按钮；回车可发送
- [ ] 相机按钮：手机点开调起本地相机拍照（`capture`），拍完可发图
- [ ] 点麦克风 → 输入框变「按住说话」条、键盘收起；再点麦克风或点输入框 → 切回打字
- [ ] 按住说话条：按住变深显示「松开发送 · 上滑取消」；上滑超阈值变红「松开取消」；松开发送区→轻提示占位；松开取消区→恢复正常
- [ ] 喇叭（自动朗读）在右上角 header，开关功能与桌面一致
- [ ] 加号上传：图片 + 文件（如 pdf/txt）均可上传，会话中可见，Agent 能引用（read_file 读到内容/至少拿到路径）
- [ ] 桌面浏览器（≥1024px）无回归；手机端无横向滚动（039 成果不回退）
- [ ] 全量测试通过：`unittest discover -s tests -t .`
- [ ] 文档同步：PROJECT.md 能力矩阵（网页端行）、ARCHITECTURE.md（web 渠道文件上传说明）如涉及、任务卡归档时同步

### 必须执行的验证

```bash
git diff --check
unittest discover -s tests -t .          # 全量
python -m compileall -q channels agent   # 后端语法
# 手动验证（乖宝）：手机 + 桌面浏览器逐项对照验收标准
```

## 实现方案（建议，code-master 可优化）

1. **前端布局重排（index.html）**：
   - `#bar` 重排：`📷 相机按钮`（隐藏 `<input type="file" accept="image/*" capture="environment">`）｜ 输入框 ｜ `🎙 麦克风按钮` ｜ `＋ 加号按钮`（隐藏 fileInput accept 多类型：image/*,pdf,doc,docx,txt,zip 等或菜单）
   - 删除 sendBtn（保留回车发送逻辑）
   - ttsBtn 移入 header（右上角，样式对齐 header 按钮）
   - 输入框占位文字改为「发消息或按住说话...」
2. **麦克风模式切换 + 手势（index.html JS）**：
   - 状态机：`mode: 'keyboard' | 'voice'`；点麦克风切换；voice 模式隐藏输入框、显示「按住说话」条（圆角灰条）
   - 手势：touchstart/mousemove 等记录按下点，touchmove/mousemove 判断上滑距离（阈值 ~60px）；发送态显示「松开发送 · 上滑取消」、取消态变红「松开取消」
   - touchend/mouseup：发送态 → 轻提示（「语音功能待开放，先打字吧」自关闭 toast）；取消态 → 复位
   - 切回 keyboard：点麦克风或点输入框
3. **加号上传（后端 web.py）**：
   - `/upload` 扩展：按 MIME/扩展名分流——图片走 ImageStore；其它文件走 FileStore（归档 workspace/files/YYYY-MM/，消毒名+重名后缀+大小上限，复用 TASK-003 逻辑），返回 `{file_id, name, size, mime}`
   - 入站消息：图片引用照旧（image_id→ImageRef）；文件引用文本化拼进消息上下文（「📎 文件：xxx.pdf（路径，大小）」）供 Agent read_file 按需读取——**不动 Agent 核心协议**
   - 前端消息发送携带文件 id，后端解析为可读文本
4. **兼容与回归**：手势代码桌面（mouse）与移动（touch）双通道；TASK-039 的移动端 CSS 不受影响

## 后续任务（本任务不处理，仅记录）

- TASK-040：webui 多端历史同步（与 041 顺序执行）
- TASK-0??：webui HTTPS（解锁真录音/ASR/PWA/推送前提）
- TASK-0??：webui 实时语音（豆包 S2S，右上角通话入口届时再加）

## 执行交接

- 状态：**已完成**（乖宝 2026-08-12 真机验收通过，含手机 Android + Mac 桌面）
- 实际改动文件：
  - `webui/index.html`（TASK-041 主体：豆包化布局/相机/加号/麦克风模式+按住说话手势/tts 移位/stop 移位/标题改短「小奈」/回车发送多端修复/图片自动上传+压缩/缩略图圆环进度/前后端日志）
  - `channels/web.py`（/upload 按 MIME/扩展名分流图片与文件；WS JSON 支持 files；入站文件引用校验+文本化；**入站日志**：聊天消息 info、引用解析失败/空消息/处理异常 warning）
  - `agent/filestore.py`（新增 `FileStore.resolve()`，入站前校验路径越界/存在性；唯一被允许的小范围支撑）
  - `tests/channels/test_web_files.py`（新增 15 个回归测试）
  - `PROJECT.md`（网页端能力行 + 里程碑段）
  - `docs/ARCHITECTURE.md`（web 渠道文件上传说明）
- 实现摘要：
  1. **底部输入栏豆包化**：`📷 相机 ｜ 大输入框（占位「发消息或按住说话...」）｜ 🎙 麦克风 ｜ ＋ 加号`，删除 sendBtn（回车发送）；ttsBtn（自动朗读）移入右上角 header；原「⏹ 停止回合」改为 header 内 stopBtn（仅回合中显示）。
  2. **相机**：`capture="environment"` 隐藏 input，手机调起相机、桌面选图。
  3. **麦克风＝模式切换 + 按住说话手势**：点 🎙 输入框变「按住说话」条并收起键盘；按住 ≥200ms 进入手势（变深「松开发送 · 上滑取消」，上滑 >60px 变红「松开取消」）；松开发送区 → toast「语音功能待开放，先打字吧」；取消区复位；再点 🎙 或点输入框切回打字。touch/mouse 双通道 + 防误触；**长按(>400ms)保留旧 ASR 录音**（单击=模式切换，录音中单击=停止转写）。
  4. **＋ 加号上传**：图片+文件（pdf/doc/txt/zip 等）可多选；`/upload` 按 MIME/扩展名分流（图片→ImageStore、其它→FileStore 按月归档），`client_max_size` 20MB→50MB。
  5. **回车发送多端修复**（验收中连修 4 个坑）：
     - 中文输入法组词回车被防误发逻辑吞掉（删发送按钮后回车是唯一路径）→ 首次回车=上屏、第二次=发送（乖宝明确要此习惯），不自动补发；
     - Android 输入法 keydown 的 keyCode 恒为 229 导致永远发不出 → 只认标准 `isComposing`，去掉 229 硬条件；
     - iOS/部分安卓键盘「发送」键触发的是 form submit 而非 keydown → `#bar` 包成 form + submit 兜底，keydown 全 preventDefault 防重复；
     - `enterkeyhint="send"` 手机键盘显示发送键。
  6. **选图即自动上传**（乖宝 2026-08-12 要求「不点回车也要传」）：addPendingFile → tryAutoUploads() 立即上传；会话未就绪时 session_changed 后自动补传；**图片压缩**（最长边 1600px/JPEG 85%，956KB→44KB，实测 1/22；小图不压；失败回退原文件）；**缩略图圆环进度**（选图=灰圈 → 上传=蓝圈+真实百分比 → 完成后隐藏；onProgress 100% 不隐藏、后端响应 done 才隐藏，修「圈消失还提示上传中」时序）；上传失败重置灰圈可重发；文件（pdf 等）不压缩。
  7. **超时**：上传 30s→180s（大文件不催）；上传未完成点发送提示「上传未完成，稍等…」。
  8. **标题**：「NanoClaw 网页端」→「小奈」（title/header/欢迎语，手机窄屏显示不全问题）。
  9. **日志**（乖宝要求排查「没回复」）：前端 console `[send]/[upload] start|done|error`；后端 `web 入站聊天消息` info + 引用失败/空消息/异常 warning（`workspace/logs/nanoclaw.log`）。
- 关键决策与假设：
  - 旧 ASR 录音能力 → **长按 🎙 保留**（单击=模式切换），录音中单击=停止转写；
  - 图片压缩为产品行为（豆包/微信同款），视觉理解无损；文件不压缩；
  - 圆环进度只显示真实字节进度（乖宝明确「不要假的」），模拟进度已删除；
  - 文件引用文本化沿用微信渠道格式 `📎 收到文件：{path}（{size}）`（单一事实源）；
  - `InboundMessage.files` 字段 TASK-003 已存在，Agent 核心协议零改动。
- 验证命令与结果：
  - `git diff --check` → 通过
  - `uv run python -m unittest discover -s tests -t .` → `Ran 953 tests ... OK`（含新增 15 个 web 文件用例）
  - `uv run python -m compileall -q channels agent` → 通过
  - `uv run python -c "import main"` → 通过
  - 前端 JS `node --check` → 通过
  - headless Chrome 多轮实测：回车三通道（keydown/submit/组词）、带图发送、自动上传、圆环进度、慢网进度、时序修复
  - 乖宝真机验收：Android 手机 + Mac 桌面全部通过（相机/加号/按住说话/回车/图片自动上传/压缩/圆环/喇叭/无横向滚动）
- 未验证项：
  - iOS 真机（手机为 Android；iOS 键盘 form submit 兜底为代码保障，未真机验证）
  - 上传中删除附件（上传请求不 abort，可能残留落盘文件——ImageStore 无单张删除接口，已知小问题）
- 风险与遗留问题：
  - 同上残留文件问题；长按录音可发现性一般
  - 后续任务：TASK-040 webui 多端历史同步（排队，041 完成后即可开始）

## 负责人验收

- [x] 检查 diff 与授权范围（仅 webui/index.html + channels/web.py + filestore.resolve + 测试 + 文档）
- [x] 独立复跑关键验证（diff check / 953 tests / compileall / import main / 前端 node check）
- [x] 检查秘密/个人数据/运行产物（无 key 落盘；测试临时脚本已清理）
- [x] 检查文档与配置一致性（PROJECT.md / ARCHITECTURE.md 已同步）
- [x] 更新 `docs/DECISIONS.md` 中相关状态（本轮无需新增决策；TASK-041 关键决策已记录在任务卡）
- 验收结论：**通过**（乖宝 2026-08-12 Android 手机 + Mac 桌面真机验收，功能全部符合预期）
- 证据与备注：953 tests OK；压缩 956KB→44KB；自动上传/圆环进度/一次回车发送实测通过
