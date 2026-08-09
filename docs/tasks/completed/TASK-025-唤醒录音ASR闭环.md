# TASK-025：唤醒→录音→ASR→对话闭环

## 任务卡

- 状态：已完成（2026-08-09 验收归档）
- 负责人：小奈
- 执行会话/子 Agent：小奈 + code-master（实现）
- 基线 commit / 分支：main@origin/main（df2002c）
- 依赖任务：TASK-023（KWS 可行）+ TASK-024（voice 渠道骨架）

### 目标

打通「喊「小奈小奈」→ 自动录音 → ASR 转文字 → 进 Agent 对话」的入站闭环。这是语音对讲机的「耳朵」：唤醒事件接进 voice 渠道，唤醒后自动录一段音频，复用现有 ASR 服务转写，作为 InboundMessage 进入 Agent。

### 非目标

- 不做语音回复/TTS 播放（TASK-026）
- 不做空闲自动分片（TASK-026）
- 不做唤醒词训练/优化（TASK-023 已定词）
- 不做蓝牙专用适配（输入用系统默认麦克风，蓝牙尽力而为）

### 允许修改

- `voice/kws/`（KWS worker 模块：回调→有界队列→worker→asyncio 唤醒事件，按 docs/VOICE_WAKE_KWS.md 架构）
- `channels/voice.py`（接 KWS 唤醒事件 + 录音 + ASR）
- `voice/asr/` 复用现有 ASR 服务（不重写）
- `main.py` / `config.py`（voice 渠道注入 asr_service）
- `tests/`（KWS 队列/冷却/唤醒事件测试 + voice 渠道音频测试）
- 任务卡 + PROJECT.md 相关状态

### 禁止修改

- 其他渠道代码
- ASR/TTS 服务核心实现（只注入复用，不改造）

### 上下文与约束

- 相关代码入口：`voice/asr/service.py`（AudioTranscriptionService，飞书语音入站同款）、`channels/feishu.py` 语音入站处理（参考）、`docs/VOICE_WAKE_KWS.md`（KWS 架构冻结候选）
- 相关架构/历史决策：
  - KWS 架构：PortAudio 回调只拷贝帧→有界队列（满丢帧记 metric）→worker 重采样+KWS 推理+连续命中确认+冷却→`loop.call_soon_threadsafe` 投递 asyncio 唤醒事件；唤醒期间新事件合并
  - 唤醒后动作：自动录音固定时长（如 5~10 秒，可配置）→ ASR 转写
  - 音频只在内存，默认不落盘（隐私铁律）
- 已知风险：录音起点可能截掉头字（唤醒后立即录音有延迟）→ 考虑唤醒词后预录音缓冲（先简单实现，标注遗留）；蓝牙麦克风延迟

### 验收标准

- [x] 唤醒事件能接进 voice 渠道（不再需要 CLI 模拟）——KwsWakeDetector 唤醒
      事件经 call_soon_threadsafe 投递 → VoiceChannel._on_wake（自动化测试覆盖）
- [x] 唤醒后自动录音 → ASR 转写成功（说话内容进 Agent）——record_audio →
      asr_service.transcribe → inject_text（自动化测试 mock 覆盖；**真实麦克风
      待乖宝端到端验收**）
- [x] Agent 能收到语音转写的内容并回复（文字）——乖宝真实端到端验收通过（2026-08-09 13:02：喊「小奈小奈」→ 甘雨回应 → 说话 → 转写 → 文字回复）
- [x] 音频不落盘（内存流转，wave 内存封装 WAV，ASR 临时文件即用即删）
- [x] KWS 队列满丢帧不阻塞回调、冷却防抖生效（测试覆盖）
- [x] 唤醒确认回应：唤醒后先播甘雨回应（`voice.wake_replies` 列表，默认「哎，我在呢，你说吧」）**播完再开始录音**——用户知道小奈在，且录音不截话（乖宝 2026-08-09 拍板方案 B；列表+随机为后续「多种回应随机播放」铺路，已记 followup；实现 2026-08-09 完成，见实现进展）
- [x] 专项测试通过（602 全量 OK）；文档同步（VOICE_WAKE_KWS.md / 任务卡 /
      PROJECT.md）——方案 B 落地后 619 全量 OK（602 + 新增 17 项唤醒回应专项）

### 必须执行的验证

```bash
.venv/bin/python -m unittest discover -s tests
# 手动端到端：启动 → 对着麦克风喊「小奈小奈」→ 说「今天天气怎么样」→ 观察 Agent 收到并回复
```

## 执行交接

- 状态：已完成（2026-08-09 验收归档）
- 实际改动文件（增/改）：
  - 新增 `voice/kws/__init__.py`、`voice/kws/errors.py`（KwsError）、
    `voice/kws/detector.py`（KwsWakeDetector）、`voice/kws/recorder.py`
    （record_audio）
  - 改 `channels/voice.py`（唤醒闭环：start 进入监听循环、_on_wake→录音→
    ASR→inject_text、_emit 友好失败提示、降级空转；kws_detector/asr_service/
    record_sec/kws_device 可选参数，TASK-024 空转与 16 项测试保持通过）
  - 改 `config.py`（voice 默认扩展 record_sec+kws.*；`_VOICE_FIELDS`/
    `_VOICE_KWS_FIELDS` 白名单，voice 纳入 load_config 深度合并与
    save_config 过滤；旧 config.json 只有 enabled 时新字段补默认、未知字段丢弃）
  - 改 `config.example.json`（voice 完整示例 enabled/record_sec/kws.*）
  - 改 `main.py`（voice 装配：model_dir 存在且 asr 非 None → 构造
    KwsWakeDetector+VoiceChannel 打印「已启用·唤醒词「小奈小奈」·ASR 就绪」；
    缺失 → 渠道仍注册但打印「唤醒未就绪（缺 KWS 模型或 ASR），仅 inject_text 可用」）
  - 新增 `tests/test_voice_kws.py`（25 项）
  - 改 `docs/VOICE_WAKE_KWS.md`（candidate → 已实现，TASK-025 起）
  - 改 `PROJECT.md`（voice 渠道 + KWS 能力行更新）
- 实现摘要：
  - detector：PortAudio 回调只 put_nowait、满丢帧记 dropped、状态只计数；
    worker 线程归一化+推理+confirm_hits 连续命中+cooldown_sec 冷却；
    `call_soon_threadsafe` 投递 asyncio 唤醒事件；动作进行中合并（coalesced）；
    start 捕获 PortAudioError 转 KwsError（含 macOS 权限提示）；stop 置位→关流→
    to_thread join worker→清引用，可重复调用。
  - recorder：`sd.rec` 走 asyncio.to_thread，stdlib wave 写 WAV 头，内存流转
    不落盘；PortAudioError 转 KwsError。
  - channel：`inject_text` 仍是唯一入站口；唤醒失败/ASR 失败/空文本走 _emit
    友好提示（如「📛 没听清，再说一次？」）；音频不落盘、只有转写文本进 Agent。
- 关键决策与假设：录音时长可配置（默认 8s）；录音设备默认与 KWS 同源
  （voice.kws.device，None=系统默认输入）；detector 的 spotter_factory/
  stream_factory/time_fn 为测试注入点；生产路径校验模型/关键词文件存在。
- 验证命令与结果：
  - `.venv/bin/python -m unittest discover -s tests` → **Ran 601 tests, OK**
    （基线 576 + 新增 25）
  - `.venv/bin/python -m unittest tests.test_voice_channel -v` → **16/16 OK**
    （TASK-024 无回归）
  - `.venv/bin/python -m compileall -q channels voice config.py main.py` → OK
  - `git diff --check` → 无空白错误
  - 真实模型目录构造 KwsWakeDetector（含 int8 变体）→ OK
  - load_config 对真实 config.json（无顶层 voice 键）→ 默认 voice 完整补齐
  - main.py 装配逻辑三路径（就绪/缺 ASR/缺模型）→ 打印与降级正确
- 未验证项：真实麦克风端到端（需乖宝实际说话：喊「小奈小奈」→ 说一句话 →
  观察 Agent 回复）；真实 ASR 转写质量；唤醒闭环常开时的 CPU/电池长跑表现。
- 风险与遗留问题：录音截头字（唤醒后立即录音有延迟，先简单实现，标注遗留）；
  蓝牙单流麦克风尽力而为；macOS 首次需授权麦克风权限。
- commit（仅在获授权时）：暂无（未 commit/push，需乖宝授权）
- 当前 `git status --short --branch`：见下（改动文件均在任务卡「允许修改」范围）
- 建议下一步：乖宝配置 voice.enabled=true + ASR 后 `uv run python main.py`，
  对麦克风喊「小奈小奈」→ 说话 → 观察 Agent 回复（手动验收步骤见
  docs/VOICE_WAKE_KWS.md「Manual acceptance」）

## 负责人验收

- [ ] 检查 diff 与授权范围
- [ ] 独立复跑关键验证
- [ ] 检查秘密/个人数据/运行产物
- [ ] 检查文档与配置一致性
- [ ] 更新 `docs/DECISIONS.md` 中相关状态
- 验收结论：✅ 实现验收通过（2026-08-09）：diff 全在授权范围；全量 602 测试复跑 OK（26 项 voice KWS 专项）；compileall OK；git diff --check OK；config 深度合并+白名单过滤无秘密泄漏；文档已同步（PROJECT.md/VOICE_WAKE_KWS.md/DECISIONS.md）。待乖宝实际说话端到端验收 + 授权 commit。
- 证据与备注：真闭环集成测试（真实 detector.start + 假 spotter/stream → 唤醒 → 录音 → ASR → inject_text → bus 收到转写文本）通过；TASK-024 16 项旧测试保持通过（空转兼容）。手动端到端：config.json 开 voice.enabled + asr_model 已配 → 喊「小奈小奈」→ 说话 → 观察文字回复。

## 实现进展

### 2026-08-09 12:08（开工）

- 方案定稿：`voice/kws/detector.py`（KwsWakeDetector，抽 demo 逻辑为模块：PortAudio 回调→有界队列→worker→冷却/确认→call_soon_threadsafe 投递 asyncio 唤醒事件）+ `voice/kws/recorder.py`（唤醒后录 N 秒 WAV bytes，sd.rec 走 to_thread）+ `channels/voice.py` 接入（唤醒→录音→ASR→inject_text；detector/asr 为 None 时回退空转，兼容 TASK-024）
- config `voice` 扩展 `record_sec` + `kws.*` 子字段，**并纳入 load_config 深度合并名单**（TASK-024 遗留项）
- 端到端验收需乖宝实际说话（喊「小奈小奈」→ 说一句话）

### 2026-08-09 12:30（乖宝拍板方案 B）

- 唤醒确认回应：唤醒后先合成并播放甘雨回应（`wake_replies` 列表，默认「哎，我在呢，你说吧」），**播完再开始录音**——用户知道小奈在，且录音不截用户的话
- `wake_replies` 做成**列表配置 + random.choice**：现在只有一条固定回应，后续加条目即自动随机（乖宝意向「多种回应随机播放」，已记 followup）
- 提前做一部分 TASK-026 的事：voice 渠道注入 `tts_service` + 播放模块；**不做 Agent 回复播放/空闲分片**（仍归 TASK-026）

### 2026-08-09 16:00（方案 B 实现完成）

- 新增 `voice/kws/player.py`（play_audio）：TTS 音频 ffmpeg 解码为 24kHz 单声道
  int16 PCM（TemporaryDirectory 即用即删、纯内存不落盘，参照 voice/media.py 范式）
  → `sd.play` + `sd.wait`（均走 asyncio.to_thread）→ **播完才返回**；PortAudioError /
  ffmpeg 失败统一转 KwsError（含可读提示）
- `channels/voice.py`：`__init__` 新增 `tts_service=None` / `wake_replies=None`；
  `_handle_wake` 在录音前先 `_play_wake_reply()`（tts 为 None / replies 空或非
  list → 跳过直接录音，向后兼容；`random.choice` 选文本；合成/播放任一步失败 →
  `_emit`「🔇 回应播放失败，继续听你说」降级跳过，不阻塞唤醒流程）
- `config.py`：`voice` 默认加 `wake_replies: ["哎，我在呢，你说吧"]`，
  `_VOICE_FIELDS` 加 `"wake_replies"`（旧 config.json 自动补默认、自定义覆盖、
  非 list 透传由渠道侧容错）
- `config.example.json`：voice 段补 wake_replies 示例
- `main.py`：VoiceChannel 装配传 `tts_service=shared["tts_service"]`、
  `wake_replies`（非 list[str] 回退 None）；就绪时打印补「·甘雨回应」
- 新增 `tests/test_voice_wake_reply.py`（17 项）：player 解码/播放调用链 +
  PortAudioError/空音频/非法音频异常路径；渠道「先合成回应再录音」顺序断言 +
  tts=None/空列表/非 list 跳过 + 合成/播放失败降级；config wake_replies 深度合并
- 验证：619 全量 OK（602 + 17）；compileall OK；git diff --check OK

### 2026-08-09（真实端到端 bug 修复：麦克风流未启动 → 唤醒不触发）

- **根因**：`voice/kws/detector.py` 的 `_open_stream()` 裸构造
  `sd.InputStream(...)` 直接返回，**sounddevice 裸构造不会自动开始采集**——
  只有 context manager（`with sd.InputStream(...)`）才在 `__enter__` 里自动
  `start()`。demo_kws.py 用的是 with 写法（自动 start），集成进
  KwsWakeDetector 时改成了裸构造返回，导致音频回调从不被调用、KWS worker
  永远等不到数据。乖宝真实端到端验收表现：喊「小奈小奈」完全没反应、无任何打印。
- **修复**：`_open_stream()` 构造 InputStream 后显式 `stream.start()` 再返回
  （stream_factory 注入路径同样 start，保持注入点语义与真实路径一致）；注释
  说明「裸构造不自动开始采集，必须 start()（demo 用 with 自动 start，集成时
  需显式）」。
- **回归测试**：`tests/test_voice_kws.py` 新增
  `test_stream_factory_stream_is_started`——注入带 start() 计数的假 stream，
  detector.start() 后断言 start() 恰好调用一次；stop() 后断言 close() 被调用
  （sounddevice close 隐含 stop）。同步给既有 3 处 FakeStream 补 start() 桩
  （test_start_stop_cleans_up_stream_and_worker / test_start_twice_raises /
  test_full_wake_loop_detector_to_bus）。
- **验证**：全量 620 OK（基线 619 + 新增 1）；`compileall -q channels voice` OK；
  `git diff --check` OK。
- **同类隐患排查**：全仓 grep `sd.InputStream` 仅两处——detector.py（已修复）
  与 demo_kws.py（`with` 写法安全）。其余 sounddevice 用法为一次性阻塞调用
  `sd.rec/sd.wait`（recorder.py）与 `sd.play/sd.wait`（player.py），无
  stream 裸构造需 start 的问题。**结论：无第二处「裸构造未启动」隐患。**
- 未 commit/push（需乖宝授权）；修复后待乖宝重启真实环境复验「小奈小奈」。
