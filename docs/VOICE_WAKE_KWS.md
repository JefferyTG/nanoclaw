# Local Voice Wake / KWS

## Status

**已实现（TASK-025 起）。** 唤醒词「小奈小奈」已接入 voice 渠道，形成完整入站
闭环：喊唤醒词 → 播甘雨确认回应（方案 B）→ 自动录音 → ASR 转写 → 进 Agent
对话。TASK-023 验证了 sherpa-onnx KWS 的可行性，TASK-024 建成 voice 渠道
无音频骨架，TASK-025 给渠道装上「耳朵」，并（2026-08-09 乖宝拍板方案 B）在
录音前先播放一条唤醒确认回应——用户听到「哎，我在呢，你说吧」就知道小奈在，
且回应播完才录音，不截用户的话。

## Implemented architecture（TASK-025 落地形态）

```text
PortAudio callback thread（只拷贝帧，put_nowait，绝不阻塞）
  -> bounded PCM queue（满丢帧记 dropped）
  -> one KWS worker thread（归一化 + 推理 + 连续命中确认 + 冷却防抖）
  -> loop.call_soon_threadsafe(WakeEvent)
  -> asyncio wake dispatch（动作进行中时新唤醒事件合并）
  -> VoiceChannel._on_wake
       -> _play_wake_reply（方案 B：tts_service + wake_replies 就绪时，
          random.choice 选一句 -> tts.synthesize(text) 甘雨合成
          -> play_audio 解码并播完才返回；失败/缺装配降级跳过）
       -> record_audio(record_sec)  -> 内存 WAV bytes（wave 头，不落盘）
       -> asr_service.transcribe("voice_wake.wav", "audio/wav")
       -> inject_text(转写文本) -> InboundMessage -> Agent
       -> 回复经 _emit 出站（TASK-026 接 Agent 回复 TTS）
```

模块位置：

- `voice/kws/detector.py`：`KwsWakeDetector`（TASK-023 demo 逻辑模块化；
  `build_spotter` 同参；`start(on_wake)` / `stop()`；PortAudio 打开失败抛
  `KwsError` 可读错误；队列满丢帧计数；冷却+confirm_hits 防抖；唤醒事件经
  `call_soon_threadsafe` 投递到 asyncio 侧）。
- `voice/kws/recorder.py`：`record_audio(duration_sec, sample_rate, device)`，
  `sd.rec` 走 `asyncio.to_thread`，标准库 `wave` 写 WAV 头，音频只在内存。
- `voice/kws/errors.py`：`KwsError(category, message)`。
- `voice/kws/player.py`：`play_audio(audio_bytes, media_type, sample_rate=24000, device=None)`，TTS 音频 ffmpeg 解码为 24kHz 单声道 int16 PCM（TemporaryDirectory 即用即删、纯内存不落盘）→ `sd.play` + `sd.wait`（均走 `asyncio.to_thread`）→ **播完才返回**；`sd.PortAudioError` / ffmpeg 失败统一转 `KwsError`（含可读提示）。
- `channels/voice.py`：`VoiceChannel(bus, *, kws_detector, asr_service,
  record_sec, kws_device, tts_service=None, wake_replies=None)`；`start()`
  在注入 detector 时进入唤醒监听循环，否则保持 TASK-024 空转；唤醒失败降级
  为仅 `inject_text`。方案 B：`_handle_wake` 在录音前先 `_play_wake_reply()`
  （tts_service None / wake_replies 空或非 list → 跳过直接录音向后兼容；
  合成/播放失败 `_emit`「🔇 回应播放失败，继续听你说」降级不阻塞）。
- `config.py`：`voice` 扩展 `record_sec` + `kws.*`，纳入 `load_config` 深度
  合并白名单（`_VOICE_FIELDS` / `_VOICE_KWS_FIELDS`，未知字段丢弃、旧配置
  兼容）。
- `main.py`：voice 启用时按 `voice.kws.*` 装配 KwsWakeDetector；模型目录存在
  且 ASR 就绪 → 唤醒闭环；否则渠道仍注册但降级。

Input target：mono 16-bit PCM @16kHz（与模型一致）；每帧 0.1s
（`blocksize = sample_rate // 10`）。

## Lifecycle

1. `start()`：校验模型/关键词文件 → 构建 spotter（to_thread）→ 起 worker 线程
   → 打开 PortAudio 输入流（to_thread）；任一步失败回滚已启动资源并把
   PortAudioError 转成含权限提示的 `KwsError`，渠道降级空转，不崩溃。
2. `stop()`：置位停止事件 → 关闭流 → 限时 join worker（to_thread，不阻塞事件
   循环）→ 清引用；可安全重复调用。
3. 队列满丢帧计数（dropped），回调只 `put_nowait` 绝不阻塞；流状态异常只计数
   不打印（回调线程不做 I/O）。

## 遗留与风险（TASK-025 记录）

- **录音截头字**：唤醒词结束到录音启动之间存在延迟，首字可能被截掉。已按
  任务卡标注为遗留；后续可考虑唤醒词后预录音缓冲。
- **蓝牙单流麦克风**：录音与 KWS 输入流并存时，macOS 默认设备通常支持多流；
  蓝牙单流设备尽力而为。
- **真实麦克风端到端待验**：自动化测试全部走 mock，未在真实麦克风上端到端
  验证（需乖宝实际说话验收：喊「小奈小奈」→ 说一句话 → 观察 Agent 回复）。
- **唤醒词固定**：「小奈小奈」由 `keywords_xiaonai.txt` 决定（TASK-023 已定词，
  本任务不做训练/优化）。
- CPU/电池长跑指标：TASK-023 实测 RSS≈53MB / 空闲 CPU 9~14%，本任务未重新
  测量；唤醒闭环常开时的表现待真实端到端观察。

## Manual acceptance（手动验收）

1. `config.json` 中启用 `voice.enabled: true`（并确认 `asr_model.enabled: true`
   及 api_key/base_url/model 已配；密钥可用 `ASR_API_KEY` 环境变量）。
2. 启动：`uv run python main.py`，预期看到
   `（语音渠道：已启用·唤醒词「小奈小奈」·ASR 就绪）`。
3. 对麦克风喊「小奈小奈」，预期：先听到甘雨回应「哎，我在呢，你说吧」
   （`voice.wake_replies` 列表 + random，可加条目即自动随机），**回应播完
   才开始录音**；听到回应后立即说一句话（如「今天天气怎么样」），预期唤醒
   处理将转写文本送入 Agent，Agent 回复经 `_emit` 出站（TASK-026 前为文字打印）。
   若 TTS 未装配或回应播放失败，渠道降级为不播回应直接录音，唤醒流程不中断。
4. 麦克风权限：macOS 首次会弹权限框，需点「允许」；拒绝后可在
   系统设置 → 隐私与安全性 → 麦克风 中开启。

## 依赖与模型

- 依赖（TASK-023 已装，勿重装）：`sherpa-onnx==1.12.40`、`sounddevice>=0.5.5`、
  `onnxruntime==1.24.4`、`numpy`、`sentencepiece`、`pypinyin`。
- 模型目录 `voice/kws/models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`
  （已 gitignore，本机已有）。关键词文件默认 `<model_dir>/keywords_xiaonai.txt`
  （不存在则构造/启动报可读错误）。

## 测试

`tests/test_voice_kws.py`（25 项）：detector 队列满丢帧不阻塞、冷却防抖、
confirm_hits 连续命中、唤醒事件投递、唤醒动作进行中合并、PortAudio 打开失败
可读错误、start/stop 清理；recorder WAV 头与异常路径；voice 渠道唤醒闭环
（mock detector + mock asr → inject_text / ASR 失败 _emit 提示 / 无 ASR 禁用 /
降级空转）；config.voice 深度合并。
`tests/test_voice_wake_reply.py`（17 项，方案 B）：player 解码/播放调用链
（真实小 WAV + 真实 ffmpeg）与 PortAudioError/空音频/非法音频异常路径；渠道
「先合成回应再录音」顺序断言 + tts=None/空列表/非 list 跳过 + 合成/播放失败
降级录音不崩溃；config wake_replies 深度合并。
全量：`.venv/bin/python -m unittest discover -s tests`（619 项，OK）。
