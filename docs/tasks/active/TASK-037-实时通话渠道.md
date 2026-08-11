# TASK-037-实时通话渠道

> 状态：实现中
> 创建：2026-08-11 ｜ 负责人：小奈 + code-master ｜ 基线 commit：8c8992d（TASK-036 完成时 HEAD）

## 目标

新增独立「实时通话」语音渠道（内部名 `realtime`），基于豆包端到端实时语音大模型-全双工版本（Seeduplex，model `1.2.6.1`），实现「KWS 唤醒 → 全双工语音对话 → 优雅退出」的完整闭环；旧 voice 渠道（KWS+ASR+LLM+甘雨TTS）**原样保留、零改动**。

## 背景

- 乖宝调研豆包端到端实时语音（2026-08-11），拍板**双轨并行**：旧 voice 保留，新渠道独立开发，避免直接改造 voice 的高复杂度与回归风险。
- 豆包全双工是**端到端 S2S**（语音进→语音出），对话思考发生在豆包模型内部，因此新渠道**不走消息总线/Gateway**，独立闭环。
- 决策（乖宝 2026-08-11 确认）：
  - **命名**：不叫 doubao，叫「实时通话」，渠道名 `realtime`。
  - **工具**：开始不接任何 FC 工具，但 `fc_bridge` 架构先搭好（tools 声明 + call_id 配对回传骨架），后续可插。
  - **人设与个人记忆**：从根目录私有文件 `realtime_identity.md` 注入小奈完整人设和乖宝的生活记忆；工作记忆/项目信息完全不进。
  - **音色**：先用豆包内置音色；甘雨复刻音色后续单独想办法（另立任务）。

## 音色确认（乖宝 2026-08-11 提供控制台列表）

- **S2S-Omni & O 2.0 版本**（`jupiter` 系，即全双工文档 §1.4 的「4 个可用音色」）：
  | 音色名 | voice_type |
  |---|---|
  | vivi（默认推荐，中文+日文等多语种） | `zh_female_vv_jupiter_bigtts` |
  | 小何 | `zh_female_xiaohe_jupiter_bigtts` |
  | 云舟（男） | `zh_male_yunzhou_jupiter_bigtts` |
  | 小天（男） | `zh_male_xiaotian_jupiter_bigtts` |
- **SC 2.0 版本**（`saturn` 系，强人格版本，定位角色扮演/情感陪伴，非全双工默认模型）：傲娇女友 `saturn_zh_female_aojiaonvyou_tob`、病娇姐姐、成熟姐姐、可爱女生 `saturn_zh_female_keainvsheng_tob`、暖心学姐、贴心女友、温柔文雅、妩媚御姐、性感御姐、傲娇公子、成熟总裁、磁性男嗓 等（男声另有 傲气凌人/傲娇公子/傲娇精英/傲慢少爷/霸道少爷/病娇白莲/不羁青年/成熟总裁/磁性男嗓/醋精男友/风发少年/腹黑公子）。
  - **价值点**：SC 版本「声音复刻、人设一致性」正是陪伴场景所需，且音色更贴小奈甜系人设；**甘雨复刻的潜在路线**。需确认 SC 版本接入方式（是否支持全双工协议 1.2.6.1，或独立会话协议）——标记为后续探索，**不在本任务范围**。

## 范围

- 新增 `channels/realtime.py`：渠道壳（继承 `Channel`，`bus=None`，`send()` 空实现），生命周期挂进 main.py 统一管理。
- 新增 `voice/realtime_s2s/` 模块：
  - `client.py`：WebSocket 客户端（`wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue`），session 管理、事件收发、优雅关闭。
  - `uplink.py`：麦克风持续上行（复用 recorder 底层；16kHz PCM int16 小端，20ms/包=640B，Base64 内嵌 JSON）。
  - `downlink.py`：下行音频流直通播放（PCM 24k）；打断对齐官方 demo，由豆包服务端动态判停。
  - `fc_bridge.py`：Function Calling 桥接**骨架**（tools 空数组 + `response.function_call_arguments.done` 按 `call_id` 配对的执行器预留），默认不注册任何工具。
- main.py 接线：`realtime` 配置读取 + **与 voice 麦克风互斥校验** + 实例化 + `channels.append`（~30 行）。
- `config.example.json` 增加 `realtime` 配置段。
- 复用：`voice/kws/detector.py`（KWS 唤醒，渠道无关）、`voice/kws/player.py`（播放）、唤醒回应本地 WAV 缓存、`voice/kws/vad.py`（可选静音检测）。
- 测试：`tests/channels/realtime/` + `tests/voice/realtime_s2s/`（fake WebSocket，不真连云端）。

## 非目标

- **不改旧 voice 渠道任何代码**（channels/voice.py 及 voice/asr、voice/tts 全部不动）。
- 不接 Function Calling 工具（只搭骨架，不注册工具）。
- 不做甘雨复刻音色接入 / SC 2.0 版本探索（均后续单独任务）。
- 不做工作记忆同步 / 知识库 / 会话落盘接入（固定个人记忆来自 `realtime_identity.md`，对话上下文留在豆包服务端会话内）。
- 不接入消息总线 / Gateway / AgentLoop。
- 不实现主动搭话（豆包模型能力，后续再评估）。

## 验收标准

- [ ] `config` 新增 `realtime` 段；与 `voice` 互斥校验生效（同时 enable 报错或强制二选一）。
- [ ] 启动后 KWS 待命；唤醒词命中 → 建立豆包连接 → 全双工对话。
- [ ] 音频上行正确（16k PCM Base64 分包）；下行音频可播放（PCM 24k）。
- [ ] 对话中可打断；客户端不主动发 `response.cancel`，由服务端动态判停，打断后能继续听。
- [ ] 退出路径：`[END]` / 静默超时 / 手动关闭 → 先 `session.close` 收到回复再断开 → 回到 KWS 待命（避免 ContextCanceled）。
- [ ] `fc_bridge` 骨架存在：tools 空数组、下行函数调用事件按 call_id 配对的执行器预留（默认仅日志）。
- [ ] `realtime_identity.md` 人设与个人记忆生效（豆包回复符合小奈设定、称呼乖宝「洵洵」）；不存在 config 或代码内置的第二份人设。
- [ ] 默认音色 vivi 生效（`zh_female_vv_jupiter_bigtts`），可配置切换其它 3 个内置音色。
- [ ] 旧 voice 渠道测试全绿；新增 realtime 测试全绿（`unittest discover -s tests -t .`）。
- [ ] `pyproject.toml` 显式声明 `websockets` 依赖。

## 相关模块

- `channels/base.py`（Channel 基类）、`channels/voice.py`（参照但不改动）
- `voice/kws/detector.py`（复用）、`voice/kws/player.py`（复用）、`voice/kws/vad.py`（复用）
- `main.py`（接线）、`config.py` / `config.example.json`（配置）
- `docs/ARCHITECTURE.md`（渠道架构说明，需同步）

## 实现方案

### 数据流（唤醒 → 退出）

```
[KWS 待命] → 唤醒词命中（on_wake 回调）
  → session.create（model=1.2.6.1，audio.input=16k PCM，audio.output=24k PCM，
    instructions=实时读取 realtime_identity.md，tools=[]，extension 透传联网开关）
  → 播唤醒回应（复用本地 WAV 缓存，免费秒回；缓存空则跳过或降级）
  → 全双工对话循环：
      上行：麦克风 16k/20ms → base64 → input_audio_buffer.append
      下行：output_audio.delta → play_audio 播放（PCM 24k）
      打断：服务端动态判停（客户端不发 response.cancel）
      退出检测：[END] 标记 / 静默超时 / 手动 stop
  → 优雅关闭：session.close → 等 session.closed → 断 WebSocket → 回 KWS 待命
```

### 关键决策

- **不走总线**：`Channel` 子类但 `bus=None`，`send()` 空实现；只复用 Channel 的 start/stop 生命周期接口（main.py 统一 `ch.start()` 管理）。
- **KWS 复用**：`KwsWakeDetector` 渠道无关，直接注入；KWS 待命与对话串行，天然不抢麦克风（config 再加 voice/realtime 二选一保险）。
- **唤醒回应**：优先复用现有 `wake_replies_dir` 本地 WAV 缓存随机播放（同旧 voice 体验）；后续可评估豆包 `speech_text_buffer.commit`（SayHello）统一音色。
- **联网**：豆包自带 `enable_volc_websearch`（需 `volc_websearch_api_key`，web 普通模式默认）；作为配置项，默认关闭（POC 阶段先不开，避免额外 key 依赖）。
- **音色**：默认 vivi（`zh_female_vv_jupiter_bigtts`），config 可切换（vivi/小何/云舟/小天）。
- **鉴权待验证**：乖宝提供 `ark-` 前缀 key（疑为火山方舟 Ark 格式）；豆包语音文档要求请求头 `X-Api-Key`。**需在冒烟时验证 ark key 是否可直接用于语音接口**（语音控制台 API Key 管理中的 key 才是文档所指；若不通，请乖宝从语音控制台重新获取）。key 值不落任何文档/git。
- **重连**：5xx 统一重连；4xx 停止重试并告警；10 分钟无交互断连需 `input_mod=keep_alive` 或会话内心跳策略（POC 可先接受断连，静默超时前主动退出）。
- **instructions 唯一来源**：每轮建会话前读取根目录私有文件 `realtime_identity.md`，包含小奈人设和乖宝个人生活记忆；文件被 Git 忽略，工作记忆不进入。配置层不提供覆盖项。

### 需要同步的文档

- `docs/ARCHITECTURE.md`：新增 realtime 渠道说明（不走总线的特例）。
- `PROJECT.md`：能力矩阵加「实时通话渠道」。
- 完成后归档任务卡并同步 MEMORY 指针。

## 测试方式

- 全量：`cd /Users/xx/WorkBuddy/nanoclaw && .venv/bin/python -m unittest discover -s tests -t .`
- 新增单测（不真连云端）：
  - `tests/voice/realtime_s2s/test_client.py`：fake WebSocket 下的事件收发、session 生命周期、优雅关闭、5xx 重连。
  - `tests/voice/realtime_s2s/test_uplink.py`：16k PCM 分包 + Base64 编码正确性。
  - `tests/voice/realtime_s2s/test_downlink.py`：delta 音频解码、顺序播放、服务端打断事件。
  - `tests/voice/realtime_s2s/test_fc_bridge.py`：call_id 配对回传骨架（空工具默认路径）。
  - `tests/channels/realtime/test_realtime_channel.py`：唤醒→对话→退出状态机（fake client）。
  - `tests/channels/realtime/test_realtime_config.py`：配置读取 + voice 互斥校验。
- 冒烟（有可用 Key 后）：手动配置 api_key，唤醒真实对话一轮，验证音色/延迟/打断。

## 风险

- **ark key 是否可用于语音接口待验证**（见「关键决策-鉴权待验证」）；若不通需从语音控制台重新获取，key 不落文档。
- **websockets 依赖**：lock 中已有（传递依赖），需在 pyproject.toml 显式声明。
- **麦克风互斥**：voice 与 realtime 同时 enable 必须被拦截。
- **计费**：豆包 S2S 按分钟计费，长时间挂机成本需注意（POC 默认唤醒后对话、静默退出，不常驻连接）。
- **播放格式**：输出默认 OGG-Opus；若本地播放器不支持需配 `extension.tts.audio_config` 返回 PCM 24k（推荐直接要 PCM）。
- **打断时机**：本地 VAD 判定打断的灵敏度需实测调参，避免误打断。
- **SC 2.0 / 甘雨复刻**：不在本任务范围，后续评估 SC 版本接入方式与声音复刻可行性。

## 下一步

1. 验证 ark key 是否可用于语音接口（语音控制台 API Key 管理）；不可用则请乖宝从语音控制台获取语音专用 key。
2. 乖宝确认「开始」后，派 code-master 按本卡实现。
3. 实现完成后冒烟：真实唤醒对话，验证 vivi 音色/延迟/打断。
4. 后续任务候选：SC 2.0 版本探索（陪伴人设）、甘雨复刻音色、FC 工具接入、联网搜索开启。

## 冒烟问题与修复记录（2026-08-11）

### 问题：唤醒后「realtime 对话异常退出」，连接即断

**现象**：唤醒后 19 秒内异常退出，日志只显示 `realtime 对话异常退出：`（异常信息被
`getattr(exc, 'message', exc)` 吞掉，websockets ConnectionClosed 的 message 为空），
随后 `session.close 发送失败：no close frame received or sent`。

**根因**（诊断脚本真连服务端复现）：`session.create` 的 payload 格式不符合豆包全双工
协议，服务端返回 `{"error":{"type":"Bad Request","code":"45000000","message":"model is invalid"}}`
后直接断开连接。具体错误：

1. `session.model` 发成了对象 `{"name":"Seeduplex","version":"1.2.6.1"}` —— 协议要求
   **固定字符串 `"1.2.6.1"`**（接入必读：model 固定单值）。
2. 音色字段位置错：发在 `audio.output.voice_type` —— 协议要求 `audio.voice`。
3. 输出格式 `"pcm"` → 应为 **`"pcm_s16le"`**（24k/16bit/小端）；且下行 PCM 由
   `extension.tts.audio_config` 控制（否则默认 OGG-Opus）。

**修复**（对应官方文档 docs.volcengine.com/docs/6561/2549778 + 接入必读 2549732）：
- `client.py create_session()`：`model` 改为字符串；`audio.input={format:pcm, sample_rate:16000}`；
  `audio.output={format:pcm_s16le, sample_rate:24000}`；`voice` 放 `audio.voice`；
  `extension.tts.audio_config={channel:1, format:pcm_s16le, sample_rate:24000}`；
  websearch 开启时 `extension.dialog.enable_volc_websearch`。
- config 字段对齐 config.json：`voice_type`→`voice`、`model_name/model_version`→`model`
  （config.json 本来就存 `model`/`voice`，之前字段名不匹配靠默认值兜底，是隐患）。
- 日志修复：`对话异常退出` 改打 `{exc!r}`，不再吞异常信息。
- 验证：诊断脚本真连收到 `session.created` ✅；全量 935 测试全绿 ✅。

**遗留**：`model is invalid` 已解决；仍需真机冒烟确认下行音频为 PCM 24k 可播放、
vivi 音色听感、延迟与打断手感。

### 第二次冒烟问题：唤醒后 1 秒即退出，豆包无回复（2026-08-11 已修）

**现象**：会话建立成功（收到 session.created、进入全双工），但恰好 1 秒后
开始优雅关闭，豆包来不及回复；上行音频也发不出。

**根因**：`_conversation_loop` 用 `asyncio.wait_for(anext(events), timeout=1.0)`
做轮询。wait_for 超时取消的是 `anext()` 协程，**连带杀死 async generator**——
第二次 `anext` 立即抛 `StopAsyncIteration`，对话循环误判「连接结束」break。
（debug 日志显示：每次都是进入对话后精确 1.000s 退出。）

**修复**：
- `client.iter_events(poll_interval=1.0)`：心跳下沉到内部，用 `wait_for` 包
  `queue.get`（超时取消不伤队列），空闲产 `None` 心跳；对话循环直接 `anext`，
  收到 `None` 才做静默超时检查。
- `_on_mic_voice`：用户开口（能量≥interrupt_energy_threshold）刷新
  `_last_user_voice`，静默计时更准确。
- 测试 fake iter_events 心跳化；全量 935 全绿。

**待验证**：真实唤醒后全双工对话（上行音频 → 豆包语音回复 → 下行播放）。

### 第三次冒烟问题：豆包说话了但听不到（2026-08-11 已修）

**现象**：会话建立成功、豆包正常回复（日志有 response.output_audio.delta 事件），
但喇叭无声音。

**根因**：下行事件处理与官方协议不一致（对照官方 python3.7_duplex_demo.zip）：
1. 响应开始事件名：我们监听 `response.start`，官方是 `response.output_audio.started`
   （带 response_id）→ `_playing` 永远 False → feed_delta 全部丢弃。
2. delta 字段名：我们读 `audio`，官方音频字段是 **`delta`** → 即使播放态正确也解不到数据。
3. 打断确认事件：`response.cancel` → 官方 `response.canceled`。
4. `[END]` 退出标记：官方 `response.done` 只有 usage 无 content → 改为从
   `response.output_text.done` 的 `text` 检测。
5. error 事件结构：`event.error` 对象（非顶层 code/message），出错应结束本轮对话。

**验证**：实测脚本真连服务端，`speech_text_buffer.commit` 让模型说话 → 收到
`response.output_audio.started` / `delta`（pcm_s16le base64）→ 字段名确认。
全量 935 测试全绿（测试事件脚本同步改为官方事件名）。

**待验证**：真实唤醒对话 → 应能听到小奈语音；继续验证音色/延迟/打断。

### 第四次冒烟问题：能对话但无声音 + 启动爆音（2026-08-11 已修）

**现象**：事件流全部正常（output_audio.started/delta/done 都收到），但播放失败：
`dtype mismatch: 'bytes...' vs 'int16'`（第一次修复后变）→
`buffer size must be a multiple of element size` + `Stream is stopped [PaErrorCode -9983]`；
且唤醒后有一声「呲啦」爆音。

**根因与修复**：
1. `sd.OutputStream.write` 不接受 raw bytes → `np.frombuffer(chunk, dtype="<i2")`
   转 numpy int16（同 player.py 口径）。
2. 服务端 delta 包边界不必对齐 2 字节（奇数长度）→ `frombuffer` 前补 0。
3. `on_response_done/on_response_cancel/request_interrupt` 调 `stream.stop()` 后
   **下一轮响应不再 start** → 流永久停、写入全失败。改为**流常开**：响应结束
   只清缓冲（drain）不 stop，打断同理；渠道 stop() 时才真正释放。
4. 启动「呲啦」爆音：疑似流冷启动/设备切换，待复测；若仍存在再处理
   （延迟启动/首写静音）。

**验证**：全量 935 测试全绿。待真机复测声音输出与爆音。


### 第五次冒烟问题：下行爆音（呲啦声）（2026-08-11 已修）

**现象**：能对话但喇叭只有刺耳爆音，无语音内容。

**根因（实测抓包）**：dump `response.output_audio.delta` 原始字节，前 16B =
`4f 67 67 53`（"OggS"）——**服务端返回的是 OGG-Opus，不是 PCM**！我们
`audio.output.format = "pcm_s16le"`（字符串）不被服务端识别，回退默认 OGG，
把 OGG 数据当 16bit PCM 硬播 → 爆音。

**修复（完全对齐官方 python3.7_duplex_demo）**：
- `audio.input.format` 为对象 `{"type": "pcm", "rate": 16000}`（非字符串）
- `audio.output.format` 为对象 `{"type": "pcm_s16le", "rate": 24000}`
- `voice` 移到 `audio.output` 下
- `extension` 移到事件顶层：`{asr:{extra:{}}, tts:{extra:{}}, dialog:{extra:{}}}`
- `session.id` 带客户端 UUID（官方 demo 携带）

**验证**：同结构实测返回真 PCM（前 16B 为语音波形，1 秒语音 48178 字节
≈ 24k*2B/s ✅）；全量 935 全绿。待真机复测语音输出。


### 设计修正：移除 [END] 退出标记（2026-08-11）

**背景**：原设计「豆包回复含 [END] → 退出对话」。实测发现：
- `DEFAULT_INSTRUCTIONS` 未指示模型说 [END]，模型永远不会输出它；
- `response.done` 只有 usage 无 content（官方协议），[END] 检测需从
  `response.output_text.done.text` 走，但模型不说就是死代码。

**决定（乖宝确认）**：移除 [END] 机制。退出路径收敛为：
1. 静默超时（`silence_timeout_sec`，默认 5s）：用户不说话 + 模型没在播 → 退出回待命
2. `stop()`：渠道手动关闭

**改动**：删 `_END_MARKER` / `_contains_end_marker` / `output_text.done` 分支；
测试改为静默超时退出；全量 935 全绿。


### 第六次冒烟问题：每句只听开头（回声自打断）（2026-08-11 已修）

**现象**：能听到每句话开头，随即被截断。日志模式：
`output_audio.started → 0.7s → response.canceled → transcription.started`
循环出现。

**根因**：**本地 VAD 打断误伤**。扬声器播小奈回复 → 麦克风收到回声（能量
≥ interrupt_energy_threshold=400）→ 误判「用户开口」→ `request_interrupt`
清缓冲 + 发 `response.cancel` → 服务端停止输出 → 每句开头即断。

**修复**：
- POC 阶段禁用本地 VAD 打断（`interrupt_energy_threshold` 默认 0.0）：全双工
  模型本身具备动态判停（用户真插话服务端会自动停），本地打断待回声消除
  （AEC）方案落地后再开启；代码保留打断路径，显式配置阈值可启用。
- 静默计时改由服务端驱动：`conversation.item.input_audio_transcription.started`
  （服务端 VAD 判定用户开口）刷新 `_last_user_voice`，比本地能量阈值准确。
- 测试：打断逻辑测试显式开启阈值（fake 环境无回声）；全量 935 全绿。

**待验证**：真实对话整句播放；打断体验（依赖服务端动态判停）。


### 第七次冒烟问题：服务端动态判停被回声骗（2026-08-11 已修）

**现象**：禁用本地打断后依然「每句只听开头」。日志模式不变：
`output_audio.started → 0.7s → response.canceled → transcription.started`。

**根因**：`response.canceled` 不是我们发的（本地打断已禁用）——是**服务端
主动动态判停**。豆包 API 无内置 AEC：扬声器播出的回复被麦克风收回并上行，
服务端误判「用户插话」→ 主动取消输出。

**修复（回声压制）**：
- `UplinkSender.set_muted()`：模型播放期间上行**喂静音包**（麦克风真实数据
  丢弃），服务端听不到回声 → 动态判停不误触发；响应结束（response.done）
  恢复真实上行。
- 触发：`response.output_audio.started` → mute；`response.done` → unmute。
- 代价：模型播放期间用户无法打断（POC 可接受，先保证完整播放）；打断体验
  依赖后续 AEC（WebRTC AEC / 耳机硬件方案）。

**验证**：全量 935 全绿。待真机复测：完整播放 + 对话轮次。


### 第八次对齐：收敛到官方 Python demo 行为（2026-08-11）

**结论**：第六/七次记录中的本地 VAD、`response.cancel`、上行静音和 AEC
都不再是当前实现。最终按官方 Python demo 收敛：

- 删除 `interrupt_energy_threshold`、上行 RMS 回调、本地
  `request_interrupt` 与客户端 `response.cancel` 整条路径；
- `transcription.started` 只清已缓冲播放音频，`response.canceled`
  不清已收音频，打断决策由豆包服务端动态判停；
- `output_audio.delta` 收到即入队，下行队列不设客户端丢帧上限；
- WebSocket 对齐 `ping_interval=None`，`session.create/session.close` 补
  `event_id`；保留已对齐的 16k/20ms 上行和 24k/1024 播放缓冲。

**验证**：实时通话相关 39 测试通过；全量 936 测试通过。
待真机外放冒烟确认完整播放与服务端插话判停。


### 第九次调整：人设与个人记忆收敛为单文件（2026-08-11）

- 新增本地私有 `realtime_identity.md`，包含小奈完整陪伴人设和乖宝的生活记忆，
  明确不加入项目/工作记忆；文件被 `.gitignore` 排除。
- 每次 `session.create` 前重新读取该文件，文件修改在下一次唤醒时生效。
- 删除渠道内置 `DEFAULT_INSTRUCTIONS`，并删除 `realtime.instructions` 的配置白名单、
  默认值、示例和本机配置，保证运行时只有一个人设事实源。
- 缺失、无法解码或空人设文件时，本轮通话明确失败并回到 KWS 待命，不带空人设建会话。

**验证**：实时通话相关 46 项测试通过；全量 938 项测试通过。
