# TASK-043：webui 按住发语音（按住说话 → 转文字 → 发送）

> 状态：**已完成**（乖宝 2026-08-12 建卡；08-12 17:5x code-master 实现；18:38-19:02 真机排查修复 3 轮；19:02 乖宝真机验收通过）
> 创建：2026-08-12 ｜ 负责人：乖宝（验收）/ code-master（实现）
> 基线 commit：`71f59f0`（main，TASK-042 HTTPS 归档提交）

## 目标

webui 手机端「按住说话」真正生效：按住开始录音（MediaRecorder）→ 松开自动转写（/api/asr）→ 文本作为消息发送；上滑取消。同时补齐 TASK-042 遗留的 getUserMedia 真机验证。

## 背景

- TASK-042 已完成 HTTPS（mkcert），浏览器 `getUserMedia` 麦克风权限已解锁（安全上下文），真机弹窗待本任务验证
- TASK-041 已做「输入栏豆包化」：`📷 输入框 🎙 ＋` 布局，麦克风=打字↔按住说话切换，按住说话手势**只有交互占位**（按住变深/上滑取消/松开轻提示「正在开发中」类占位），验收标准当时明确「不真录音」
- **可复用资产已就绪**：
  - 后端 `channels/web.py` `POST /api/asr` 已完整实现（FormData `file` → `asr_service.transcribe` → `{ok, text}`；asr_service 由 composition root 注入；未配置返回 503 `asr_unavailable`；超限 413、空结果 422）
  - 前端 `webui/index.html` 已有旧 ASR 录音链路（长按 🎙 触发）：`MediaRecorder` 兼容检测（含 iOS Safari 编码类型探测）、`recorder`/`recordingBusy` 状态、FormData 上传 `/api/asr`、转写成功发送
  - `voice/asr/` 本地 ASR 服务（base / openai_compat / service）

## 范围

- 改 `webui/index.html`：把 TASK-041 的「按住说话」手势（voice 模式下的 `voiceBar` + `press` 状态）接到真实录音链路——按住达 HOLD_MS 后开始录音，按住期间显示录音状态（如「松开 发送 · 上滑 取消」+ 计时/红点），松开上传转写并发送，上滑取消丢弃
- 复用现有 `/api/asr`、MediaRecorder 检测、上传与转写逻辑；只做接线，不重写
- 转写失败/无权限/ASR 未配置时给出友好提示（复用现有 `addSys` 与错误结构）
- 后端如需微调（如录音时长上限提示、错误文案）允许小改 `channels/web.py`
- 文档同步：ARCHITECTURE.md / PROJECT.md 能力矩阵 / DECISIONS.md（如需）

## 非目标

- ❌ 不做实时通话右上角入口（后续任务，TASK-037 底层已有）
- ❌ 不做语音回复/TTS 联动（TASK-041 已有喇叭朗读，不扩展）
- ❌ 不做 PWA/推送
- ❌ 不动 Agent 核心、消息协议、其它渠道
- ❌ 不 commit/push（除非乖宝授权）

## 验收标准

- [x] 手机/桌面 HTTPS 下点 🎙 进「按住说话」模式，按住 `voiceBar` → 开始录音（UI 明确反馈：变色/红点/计时），松开 → 自动上传转写 → 转写文本作为消息发送 —— **乖宝 iPhone 局域网真机验收通过（19:02）**
- [x] 上滑手势 → 取消，不发送 —— headless 验证 PASS
- [x] 录音/转写中状态提示正确（防止重复触发；转写中不能再录）—— headless 验证 PASS
- [x] 无麦克风权限 / 非 HTTPS / ASR 未配置时，提示友好可懂（分别对应权限拒绝、安全上下文、503 `asr_unavailable`）—— headless + 冒烟验证 PASS
- [x] 旧能力不回归：长按 🎙 旧 ASR 录音、打字模式、图片上传等原功能正常 —— headless 回归 PASS
- [x] **真机验证 getUserMedia 权限弹窗**（iPhone Safari 局域网 + Tailscale，补齐 TASK-042 遗留项）：首次按住弹权限 → 允许 → 能录能发 —— **乖宝验收通过（局域网 HTTPS；Tailscale 未单独复测，同一代码路径）**
- [x] 全量测试通过：`uv run python -m unittest discover -s tests -t .` —— **955 tests OK**（含 3 个新回归用例）
- [x] 后端改动过 `compileall`；`git diff --check` OK
- [x] 文档同步：ARCHITECTURE.md / PROJECT.md 能力矩阵 / DECISIONS.md 更新；任务卡归档

## 相关模块

- `webui/index.html` ✅ +140/−18：手势接线到真实录音链路（详细见下）
- `voice/media.py` ✅ 修复：WebKit 流式 WebM 兼容（无 duration + Non-monotonic DTS）
- `tests/voice/test_asr_core.py` ✅ +2 回归测试 + 1 断言
- `tests/channels/voice/test_voice_kws.py` ✅ +5：唤醒测试隔离真实 WAV 目录（防测试真播放）
- `channels/web.py` 未改（临时诊断代码已删除，恢复原样）
- `voice/asr/`、`config.py` 未改

## 实现进展（2026-08-12）

### 1. 前端手势接线（code-master，17:5x）

| 位置 | 内容 |
|---|---|
| L96–99 | CSS：`#voiceBar.recording` 红点(`::before`) + 红色描边 |
| L1383–1398 | `updateVoiceBarLabel()`：按手势/录音状态渲染文案（红点+计时） |
| L1400–1414 | `startPress`：HOLD_MS 达标 → 忙态拦截 → 更新文案 → `startRecording()` |
| L1416–1424 | `movePress`：上滑阈值 `SLIDE_CANCEL_PX`(60px 可调) → 取消区 |
| L1424–1443 | `endPress`：松开(send) → `stopRecording()`；上滑(cancel) → `discardRecording()`；授权在途 → `recordingStartAborted` |
| L1445–1450 | `cancelPress`：触摸中断/切后台 → 丢弃（best-effort） |
| L1502–1506 | `micBtn.onclick`：录音中单击停止 → 统一走 `stopRecording()`（旧入口共用状态机） |
| L1519–1524 | 新状态：`MAX_RECORD_SEC=60`、`recStartTs`、`recTickTimer`、`recordingStarting`、`recordingStartAborted`、`recordingDiscarded` |
| L1534–1572 | `stopRecording()` / `discardRecording()` / `startRecTick()`（计时+60s 自动停止）/ `clearRecTick()` |
| L1574–1582 | `setRecordingState`：录音中开计时、停止/转写中关计时 |
| L1601–1621 | `uploadRecording`：错误码映射（`asr_unavailable`/`file_too_large`/`empty_transcript`/`asr_failed`）→ 友好提示 |
| L1624–1652 | `startRecording`：重置放弃/丢弃标记；授权在途放弃检查（不创建 recorder） |
| L1671–1687 | `recorder.onstop`：丢弃分支（上滑取消不上传）+ **转写中置忙**（禁止再次触发） |
| L1708–1724 | `getUserMedia` 失败：`NotAllowedError/NotFoundError/NotReadableError` 分场景可操作提示 |

要点：按住达 HOLD_MS(200ms) → 录音；录音中红点+秒数+「松开 发送 · 上滑 取消」；松开 → 转写发送；上滑(60px) → 丢弃；录音/转写中再按拦截；权限弹窗中松手防幽灵启动；60s 自动停止；iOS 编码探测保留。

### 2. 真机问题排查（18:38-18:55，乖宝 iPhone 局域网 HTTPS）

**现象 1**：`语音转写失败：无法读取音频时长。`（终端 logger.warning；web 用标准库 logging 与 main 的 loguru 两套体系，日志不进 nanoclaw.log 只在终端）

**诊断**：临时在 `_handle_asr` 失败分支保存原始录音 → `workspace/tmp/asr-debug/asr-*.webm`（64KB）→ `file`/`ffprobe`/`xxd` 分析：
- 新版 iOS Safari（18.4+）支持 WebM 录制，前端选中 `audio/webm;codecs=opus`
- **WebKit 流式 WebM 不写 duration 元数据**：ffprobe `streams=[{"index":0}]` 但 `format:{}` 无 duration → 后端 `float(None)` → 「无法读取音频时长」
- ffmpeg 能正常转码（数据有效）

**修复 1**（voice/media.py）：ffprobe 读不到 duration → 不报错，转码后用标准库 `wave` 从 WAV 头兜底校验时长上限（恶意超长照样拦）。+2 回归测试。

**现象 2**：重启后仍失败，报错变「音频文件无法处理。」（非时长问题）。本地真实文件复现：
- normalize 的 ffmpeg 命令带 `-xerror`：exit=234，stderr `Non-monotonic DTS; previous: 0, current: -16` + `Error muxing a packet: Invalid argument`
- **WebKit 流式 WebM 时间戳从 -16ms（pre-roll padding）开始**，`-xerror` 把该警告当错误 → ffmpeg 退出

**修复 2**（voice/media.py）：normalize 的 ffmpeg 去掉 `-xerror`（真正无法解码的输入仍非零退出；`encode_to_opus` 保留）。+1 断言（防 -xerror 回归）。本地复现验证：真实 18:51 文件 → normalize OK（1.93s wav）。

**现象 3**：乖宝反馈「每次全量测试小奈会突然说话『哎～我在呢』」——排查出**测试真播放唤醒回应**：
- `test_voice_kws.py` 的 VoiceChannelWakeTests 构造 VoiceChannel 时**未传 `wake_replies_dir`** → 用默认值 `workspace/voice/wake_replies/`（含乖宝 08-11 生成的 3 句真实音色 WAV）
- `on_wake → _handle_wake → _play_wake_reply()`：**缓存非空就直接 random.choice 播放真实 WAV**（TASK-029 设计：本地缓存优先，不检查 tts/wake_replies 配置）
- 08-11 换新音色后目录有真实 WAV → 之后每次全量测试都真播放
- **修复**：这些测试传 `wake_replies_dir="/nonexistent/"` 隔离（与其他测试一致）

**第二轮排查（19:05 乖宝反馈仍有声音）**：spy 包装器实验定位——patch `channels.voice.play_audio` + `channels.realtime.play_audio` 为「记录调用栈不真播」，跑全量测试，log 实锤 **`test_voice_continuous.py` 2 个测试**（`test_silence_timeout_exits_continuous_mode` L268 / `test_silence_below_timeout_keeps_listening` L295）构造 VoiceChannel 未传 `wake_replies_dir`、未 patch play_audio，`_handle_wake()` → `_play_wake_reply()` → 真播放。修复：同加 `/nonexistent/` 隔离。**spy 复跑：0 次播放调用，彻底干净**。经验：凡「构造 VoiceChannel + 触发 `_handle_wake`/`_play_wake_reply`」的测试必须隔离唤醒目录或 mock play_audio；排查测试真播放可用 spy 包装器一次定位

### 3. 收尾（19:02 乖宝验收通过后）

- 删除 `channels/web.py` 临时诊断代码（恢复原样）
- 清理 `workspace/tmp/asr-debug/`（5 个 webm）+ `/tmp` 测试残留
- 全量 955 tests OK

## 测试结果（最终，真实执行）

- 全量：`uv run python -m unittest discover -s tests -t .` → **955 tests OK**（55s，含 3 新回归：无 duration 兜底 / wave 超长拒绝 / -xerror 断言）
- `git diff --check` OK；`compileall` OK（channels webui voice）
- 真机：iPhone Safari 局域网 HTTPS 按住说话 → 转写 → 发送 ✅（乖宝 19:02 验收）
- 录音不落盘：ASR 链路 `tempfile.TemporaryDirectory` 用完即删（设计保证）；诊断文件已清理

## 未验证项与风险

- Tailscale 域名真机未单独复测（局域网已验证，同一代码路径；乖宝可随时补验）
- 旧 iOS Safari（<18.4，无 WebM 仅 mp4）路径未实测：前端 `chooseRecorderOptions` 会退到 `audio/mp4`，后端 ffprobe 对普通 mp4 能读 duration，预期 OK
- 手势手感：`HOLD_MS=200` / `SLIDE_CANCEL_PX=60` 预估默认值，乖宝手感不适可调（有注释）
- iOS 切后台/中断：`touchcancel` → 丢弃为 best-effort
- 60s 上限假时钟已验证；真实长录音边界可临时调小 `MAX_RECORD_SEC` 实测

## 关键决策（入 DECISIONS.md）

- **WebKit/Safari 流式 WebM 兼容**：ffprobe 读不到 duration 时不报错（转码后 wave 兜底校验）；normalize 的 ffmpeg 不用 `-xerror`（Non-monotonic DTS 警告不致命）
- **测试隔离**：VoiceChannel 默认 `wake_replies_dir` 指向真实 WAV 目录，凡触发 `_play_wake_reply` 的测试必须传 `/nonexistent/` 隔离
- 前端不加 `recorder.start(timeslice)`：社区 workaround 主要缓解 whisper 长音频截断，与本 bug 无关

## 下一步

- 已归档。待乖宝授权 commit/push。
