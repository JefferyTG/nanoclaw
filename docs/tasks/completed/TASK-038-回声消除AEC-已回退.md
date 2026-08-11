# TASK-038-回声消除AEC

> 状态：**已放弃并回退**（2026-08-11 17:25 乖宝拍板：AEC 相关代码全部移除、speexdsp 已卸载，回到无 AEC 状态）
> 复盘：外放被 `response.canceled` 掐断的根因未定位（AEC、demo 行为对齐、音频管线
> 对齐均无效；耳机正常、py demo 外放正常但咱们外放必掐）。AEC 方案（speexdsp 客户端
> 线性回声消除）实验后确认不是解药，已整体回退：`aec.py` 删除、uplink/downlink/channel
> 去注入、config 去开关、`brew uninstall speexdsp`。回退后全量 940 OK。
> 后续若要重启外放方向，候选：macOS VoiceProcessingIO（与浏览器 getUserMedia AEC 同源，
> web demo 靠它外放正常）、tools/location/唤醒回应等会话参数差异、服务端判停行为实测。
> 创建：2026-08-11 ｜ 负责人：小奈 + code-master ｜ 基线：HEAD 8c8992d（TASK-036）+ 工作区含 TASK-037 未提交修改

## 目标

为 realtime 实时通话渠道（TASK-037）增加**客户端回声消除（AEC）**，解决外放场景下
「服务端动态判停被扬声器回声误触发，导致回复被掐断」的问题，恢复**外放 + 全双工**
（模型完整播放、用户随时可打断）。

## 背景

- TASK-037 冒烟确认：豆包 S2S API **无服务端 AEC**（Seeduplex「精准抗干扰」只区分
  人声/噪音，不消除扬声器回声）。外放时：小奈回复 → 扬声器出声 → 麦克风收回 →
  上行豆包 → 服务端误判「用户插话」→ `response.canceled` 掐断输出（每句只听开头）。
- 耳机实测 100% 确认「回声是唯一问题」：耳机下完整播放 + 对话正常。
- 曾用「播放时上行喂静音」压制回声，代价是播放期间用户无法打断（乖宝不接受），已移除。
- 技术选型（2026-08-11 调研实测）：
  - `pip install speexdsp` ❌ 需 swig 编译，macOS 无 swig；
  - `webrtc-audio-processing` ❌ 无 macOS wheel；
  - **`brew install speexdsp`（bottled 预编译二进制）+ ctypes 调用** ✅ 免编译、零 Python
    新依赖（ctypes 标准库），C API 仅 4-5 个函数，选此方案。

## 范围

- 新增 `voice/realtime_s2s/aec.py`：speexdsp C 库的 ctypes 封装
  - `speex_echo_state_init(frame_size=320, filter_length)` → 状态指针
  - `speex_echo_cancellation(state, 近端麦克风, 参考信号, 输出)`：16k PCM int16，20ms 帧
  - `speex_echo_state_destroy(state)` 释放
  - 库加载：ctypes.CDLL 找 brew 安装路径（`/opt/homebrew/lib/libspeexdsp.dylib`）
  - 指针/内存管理严格，防 segfault（POC 也要稳）
- `voice/realtime_s2s/downlink.py`：播放前把下行音频（24k → 16k 重采样）作为
  **参考信号**喂给 AEC（复用/新增轻量重采样，stdlib/numpy 即可）
- `voice/realtime_s2s/uplink.py`：麦克风数据**过 AEC 后**再分包上行（近端输入）
- `channels/realtime.py`：AEC 实例创建/注入/启停（对话级生命周期，随 session 一换一）
- `main.py` / `config`：可配置开关 `realtime.aec_enabled`（默认 true），必要时可关
- brew 依赖：`brew install speexdsp`（系统级，装完记录到任务卡，不落代码）

## 非目标

- 不做 WebRTC AEC / macOS VoiceProcessingIO / 自写 NLMS（speexdsp 够用，先跑通）
- 不做服务端改造（豆包无此能力）
- 不追求极端声学环境（强混响/大音量失真）的完美消除，普通房间够用即可
- 暂不恢复本地 VAD 打断（`interrupt_energy_threshold` 保持 0）；AEC 生效后
  **可选**重新启用并实测打断手感（记入「下一步」候选）

## 验收标准

- [x] `brew install speexdsp` 完成（1.2.1，2026-08-11 15:50 小奈执行）；`aec.py` ctypes 能加载库并跑通一次
      `speex_echo_cancellation`（单测断言输出非空、无 segfault）
- [x] 数据流正确（代码+单测）：下行播放音频 → 重采样 16k → AEC 参考；麦克风 → AEC → 上行豆包
- [ ] 外放场景（扬声器 + 内置麦克风）：模型回复**完整播放**，不再被
      `response.canceled` 掐断（**真机冒烟，待做**）
- [ ] 外放场景用户插话仍能打断（模型动态判停正常，不因回声误停也不因插话迟钝）（**真机冒烟，待做**）
- [ ] 延迟对齐/滤波器长度调参后可接受（听感无明显回声尾音、无明显卡顿）（**真机听感，待做**）
- [x] 全量测试全绿（`unittest discover -s tests -t .`）：**957 OK**（61.95s）；新增 AEC 单测（fake 管线）9 用例
- [x] 旧 voice 渠道零改动；realtime 单测无回归（channel 21 / uplink+downlink 22 通过）

## 相关模块

- `voice/realtime_s2s/aec.py`（新增）、`downlink.py`、`uplink.py`
- `channels/realtime.py`（AEC 接线）、`main.py`（可选配置）、`config.py` / `config.example.json`
- 参考：官方 python3.7_duplex_demo（workspace/demo/）、speexdsp C API 文档

## 实现方案

### 数据流

```
麦克风(16k) ──┐
              ├─→ speex_echo_cancellation → 干净上行 → 分包 Base64 → 豆包
下行音频(24k) ─┴─→ 重采样16k（参考信号）─────┘
```

### 关键决策

- **ctypes 而非 pip 包**：brew speexdsp 是 bottled 二进制，免 swig/编译；ctypes 直接
  调 C ABI，零 Python 新依赖。库路径优先环境变量/常见 brew 路径，找不到报可读错误。
- **帧对齐**：16k / 20ms = 320 样本/帧；参考与近端必须同步推进（downlink 播放的
  每 20ms 音频对应一帧 AEC 处理）。上行分包已是 20ms/640B，天然对齐。
- **滤波器长度**：默认 512（≈32ms 回声尾长，普通桌面场景足够），可配置调参。
- **延迟对齐**：扬声器→麦克风声学延迟 + 播放管线延迟需补偿；先固定偏移起步，
  听感调参（AEC 对轻微失配有一定鲁棒性）。
- **重采样**：24k→16k 用 numpy 线性插值（轻量，够用；音质不敏感因只做参考信号）。

### 实现细节（code-master 2026-08-11）

- `aec.py`：`SPEEXDSP_LIB_PATH` 环境变量严格覆盖；默认探测 `/opt/homebrew`（Apple Silicon）
  → `/usr/local`（Intel）→ `ctypes.util.find_library` 兜底；失败抛可读 `AecLibraryError`
  （提示 brew install）。C 函数全绑定 argtypes/restype。`Aec` 类：init 320 帧 + `SPEEX_ECHO_SET_SAMPLING_RATE=16000`
  （mdf.c 源码查证：该 ctl 只重算自适应步长/DC 陷波，不改帧长；不调用默认按 8k 处理），
  process() 输入任意长度先规整（奇数补 0/过长截断/过短补零）再调 C，close() 幂等 + context manager + `__del__` 兜底。
- `downlink.py`：`resample_pcm()` numpy `np.interp` 线性插值 24k→16k（比率 2/3，20ms=320 帧精确对齐上行分包）；
  `DownlinkPlayer.aec` 注入点，播放时喂参考；`aec=None` 原样行为。
- `uplink.py`：`UplinkSender.aec` 注入点，每 20ms 包先 `aec.process()` 再 Base64 上行；`aec=None` 原样透传。
- `channels/realtime.py`：`aec_enabled`（默认 true）/ `aec_factory`（测试注入）；`_run_conversation` 对话级
  创建 Aec → 注入 uplink/downlink → finally 关闭；库缺失捕获 `AecLibraryError` 回退直通并告警。
- 参考信号经 `queue.Queue`（下行播放任务 → 上行任务）按序消费，缺参考用静音兜底，队列满丢最旧。
- 延迟对齐：**零偏移起步**——512 样本滤波器（≈32ms）可吸收普通桌面声学延迟，真机听感调参留后续。

### 需要同步的文档

- `docs/ARCHITECTURE.md`：realtime 渠道补 AEC 模块说明 ✅（code-master 已改）
- `PROJECT.md`：能力矩阵 realtime 行补「客户端 AEC（speexdsp）」✅（code-master 已改）
- 完成后归档任务卡并同步 MEMORY 指针

## 测试方式

- 全量：`cd /Users/xx/WorkBuddy/nanoclaw && .venv/bin/python -m unittest discover -s tests -t .` → 957 OK
- 新增单测（不真开设备）：
  - `tests/voice/realtime_s2s/test_aec.py`：9 用例——库加载、状态创建/销毁、cancellation 调用输出长度正确、
    伪造近端=参考时输出能量明显降低（回声被消：in_rms=5663 → out_rms=0.4，抑制到 0.01%）、
    context manager、环境变量覆盖
- 冒烟（真机，待做）：外放 + 麦克风，唤醒后让小奈说一段话 → 验证完整播放、无回声尾音；
  播放中插话 → 验证能打断

## 风险

- **真机效果未验证**：单测是理想回声路径（近端=参考），真实扬声器→麦克风路径含声学延迟/混响/失真，
  需外放冒烟；效果不够时调 `filter_length` 或加固定延迟偏移。
- **延迟失配**：零偏移起步，若设备延迟超滤波器覆盖（>32ms）会消不净或引入尾音，需调参。
- **ctypes 指针/内存**：封装不当会 segfault → 严格生命周期管理 + 单测保护 ✅ 已做
- **brew 库路径**：Intel/Apple Silicon 路径不同（/usr/local vs /opt/homebrew）→ 探测 ✅ 已做
- **计费**：外放场景恢复全双工后，注意对话时长控制（静默退出机制保留）

## 实施记录

- 2026-08-11 15:50：`brew install speexdsp` 完成（1.2.1，bottled）；基线=HEAD 8c8992d +
  工作区 TASK-037 未提交修改；code-master 已确认带 ponytail 技能；已派发实现。
- 2026-08-11 ~15:58：code-master 实现完成——新增 `aec.py` + `test_aec.py`（9 用例）；改
  `downlink.py`（重采样+参考喂送）、`uplink.py`（近端过 AEC）、`channels/realtime.py`（AEC 生命周期/开关）、
  `config.py` / `config.example.json`（`aec_enabled` 默认 true）、`main.py`（装配）、
  `docs/ARCHITECTURE.md` / `PROJECT.md`（文档同步）；全量 957 OK；`git diff --check` 干净。
  未 commit。
- 2026-08-11 16:51~17:03：**按 demo 方法对齐（乖宝指引）**——py demo 实测完整播放，
  **对比出三大行为差距**：
  1. session.create 参数：demo 带 enable_loudness_norm=true / enable_music=false /
     audit_response，我们空 extra → 已对齐（官网查证参数含义）；
  2. **response.canceled 处理**：demo 只打印日志不清播放队列（已收到音频继续播完），
     我们 _drain_chunks() 清空停播 → **已改为不清缓冲**（对齐 demo）；
  3. **transcription.started 处理**：demo 在此 clear_queue（用户真开口才停），我们只
     刷计时 → 已改为 request_interrupt 打断播放；
  4. 上行 AEC：demo 裸发无 AEC，我们过 speexdsp → config.json `aec_enabled=false`
     关掉对齐 demo（若此版成功，AEC 方案可大幅简化或移除）；
  测试：全量 959 OK（新增 canceled 不清缓冲单测）；git diff --check 干净。未 commit。
  待乖宝重启真机冒烟。
- 2026-08-11 16:10~16:15：**真机冒烟失败复盘（乖宝操作）**——外放仍被 `response.canceled`
  掐断（每轮 audio.started→done≈560ms→canceled），服务端持续转写「插话」= 回声未消净。
  **根因（小奈排查+实验验证）**：默认 `filter_length=512` 仅 32ms@16k，而官方 speex_echo.h
  明确要求 100-500ms；真实延迟链=声卡缓冲（downlink blocksize 1200@24k≈50ms）+ 声学传播
  ≈55-80ms，32ms 滤波器覆盖不了（实验：50ms 延迟下 512 只消 52%，2048 消 81%）。
  **修复**：`DEFAULT_FILTER_LENGTH` 512→2048（≈128ms），配置化 `realtime.aec_filter_length`
  （默认 2048）；`config.py`/`config.example.json`/`main.py`/`channels/realtime.py` 透传；
  新增 `test_echo_suppressed_with_playback_delay` 单测（模拟 60ms 播放延迟场景，防回归）。
  全量 958 OK；`git diff --check` 干净。未 commit。
- 2026-08-11 16:17~16:35：**第二轮冒烟仍失败**（filter_length=2048 已生效但外放仍被掐）。
  小奈做了 6 组真实设备诊断实验，关键发现：
  1. 真实回声延迟 ≈90ms（声卡缓冲 50ms + 声学传播 40ms）；AEC 理想收敛需 1-1.5s，
     而服务端 audio.started→canceled 仅 ~720ms——**收敛前窗口内回声全裸上行**；
  2. 实验：numpy 线性插值重采样导致参考失真，真实管线抑制率从 91% 掉到 57%
     （speex_resampler 待试）；参考延迟补偿 80ms 时抑制率最优 89.9%（0ms 时 80.5%）；
  3. 本机麦克风底噪/AGC 很大（静音 RMS≈2600），回声路径稳定性存疑（三次同信号
     录音能量一致但波形差异大）；
  4. 已加诊断日志（uplink AEC 前/后 RMS+参考队列、downlink 参考喂送统计），
     待乖宝重启冒烟取真实数据。
- 2026-08-11 16:27~16:33：**第三轮冒烟（乖宝）+ 诊断日志实锤**：
  - AEC 前RMS=后RMS（1731→1731）——**AEC 根本没消**；参考队列几乎全程=0；
  - 根因确认：AEC 是**对话级一换一**，每次唤醒从零收敛；而服务端
    audio.started→canceled 仅 ~700ms——**AEC 永远没机会学完**；且参考领先
    回声 ~90ms（feed 在 write 前+声卡缓冲 50ms+声学 40ms）收敛更慢；
  - 实验 v15：**唤醒回应预热方案**验证成功——播放 WAV（2.6s）同时 playrec
    录回声喂 AEC，预热后第 1 秒抑制 83.5%（从零开始仅 4%）；
  - **已实现**（小奈）：`_play_wake_reply(aec)` 真实 Aec 时走
    `_warmup_aec_with_wake_reply`——sd.playrec 播 WAV+录音逐帧喂 AEC 学习
    回声路径（128 帧/2.56s），预热失败回退普通播放；测试 Fake 不走预热；
    `isinstance(aec, Aec)` 区分；全量 958 OK；真机预热验证第 2 秒 78% 抑制；
  - 待乖宝重启真机冒烟：外放完整播放 + 插话打断。
- 2026-08-11 16:34~16:40：**第四轮冒烟仍失败 + 诊断实锤架构缺陷**：
  - 预热已生效（132 帧/2640ms），但 AEC 前=后 RMS（1730→1733），参考队列
    积压 18 帧（360ms）时 AEC **反向放大**（前16→后567）——根因：queue 异步
    传递参考导致参考严重滞后回声，AEC 拿错参考乱消；
  - **架构级修复**：弃用 `speex_echo_cancellation`+跨任务队列，改用官方
    `speex_echo_playback` / `speex_echo_capture` 组合（内部 2 帧声卡延迟
    对齐，各自维护时间线，天然解决异步积压）——`aec.py` 改为 `playback()`
    / `capture()` 接口；downlink 播放前调 playback，uplink 每包调 capture；
  - 真机连续会话验证（拼接[唤醒+静音+测试]一次播放）：预热 127 帧后
    第 1 秒抑制 84.1%、第 2 秒 91.3%、第 3 秒 92.8%；
  - 全量 958 OK；`git diff --check` 干净。未 commit。
  待乖宝重启真机冒烟：外放完整播放 + 插话打断。
- 2026-08-11 16:44~16:49：**官方 demo 全读调研（乖宝指引）**：
  - 网页 demo（web_duplex_demo）：**靠浏览器 getUserMedia({echoCancellation:true})
    系统级 AEC**，自己没写任何回声消除；网页实测外放能完整播放（乖宝确认）；
  - py demo（python3.7_duplex_demo）：**完全没有 AEC**——mic_worker 裸采 16k
    直接上行，播放 24k 直出；收到 transcription.started 只 clear_queue 清播放队列
    （服务端开始转写才停，回声照样上行，外放必被掐）；config/README 均无 AEC；
  - **结论**：官方把 AEC 全部交给浏览器/系统；本地客户端需自建 AEC，且
    speexdsp 单麦线性 AEC 上限有限；**macOS VoiceProcessingIO（与浏览器同源
    系统级 AEC）是潜在正解**——待评估接入方式（PortAudio 默认不带，需查
    AVAudioSession/VPIO 配置或换采集方式）。
  未 commit。
- 2026-08-11 16:51~17:03：**按 demo 方法对齐（乖宝指引）**——py demo 实测完整播放，
  **对比出三大行为差距**：
  1. session.create 参数：demo 带 enable_loudness_norm=true / enable_music=false /
     audit_response，我们空 extra → 已对齐（官网查证参数含义）；
  2. **response.canceled 处理**：demo 只打印日志不清播放队列（已收到音频继续播完），
     我们 _drain_chunks() 清空停播 → **已改为不清缓冲**（对齐 demo）；
  3. **transcription.started 处理**：demo 在此 clear_queue（用户真开口才停），我们只
     刷计时 → 已改为 request_interrupt 打断播放；
  4. 上行 AEC：demo 裸发无 AEC，我们过 speexdsp → config.json `aec_enabled=false`
     关掉对齐 demo（若此版成功，AEC 方案可大幅简化或移除）；
  测试：全量 959 OK（新增 canceled 不清缓冲单测）；git diff --check 干净。未 commit。
  待乖宝重启真机冒烟。
- 2026-08-11 17:13~17:15：**根因实锤改动（乖宝确认，小奈执行）——transcription.started
  不再发 response.cancel**。背景：17:03 对齐版（extra 参数 + canceled 不清缓冲 + AEC 关）
  真机仍被掐断；完整对比两个官方 demo 后确认 **py/web demo 从头到尾从不主动发
  response.cancel**（打断完全交给服务端动态判停），咱们是唯一在
  `transcription.started` 时主动发 `response.cancel` 的客户端——这把服务端「疑似
  插话」直接升级成实锤取消（且不带 response_id=取消所有响应），高度疑似掐断根因。
  改动：`DownlinkPlayer` 新增 `clear_audio_buffer()`（只清播放缓冲，保持播放态、
  不发 cancel）；`_conversation_loop` 的 transcription.started 分支改调
  `clear_audio_buffer()`；本地 VAD 打断路径（`request_interrupt`/`_send_response_cancel`）
  原样保留（默认禁用）。测试：新增 downlink 1 例 + channel 1 例（断言 transcription.started
  清缓冲但保持播放态、无 response.cancel 上行）；全量 **961 OK**。未 commit。
  待乖宝重启真机冒烟：外放完整播放 + 插话打断。若仍被掐 → 候选差异（按序）：
  静默超时 5s（demo 无）、tools 空数组（demo 都带）、下行播放丢帧、本地 WAV 唤醒回应。

- 2026-08-11 17:18~17:22：**音频管线对齐 py demo（乖宝确认，小奈执行）**。背景：
  17:15 版（transcription.started 不发 cancel）真机外放仍被掐；乖宝确认耳机正常。
  复盘发现此前误算 py demo 分包——`MIC_CHUNK=320` 是 pyaudio **帧数**（320 帧
  ×2B=640B=20ms），分包其实与咱们一致；真正差异是**采集/播放缓冲**：
  - 采集 blocksize：咱们 1600 样本（100ms）vs py demo 320 帧（20ms）→ 上行数据
    滞后最多 100ms，回声到达服务端时间（160-200ms）拖出动态判停免疫窗口
    （py demo 70-100ms 在窗口内）→ 外放被误判插话。已改 `blocksize=320`（20ms）。
  - 播放 blocksize：咱们 1200@24k（50ms）vs py demo 1024@24k（42.7ms）→ 已改 1024。
  - 改动：`uplink.py _open_mic`、`downlink.py _open_default_stream`；全量 **961 OK**；
    未 commit。待乖宝重启真机冒烟：外放完整播放 + 插话打断。
  若仍被掐 → 剩余候选差异（按序）：tools 空数组（demo 都带）、
  `extension.dialog.location`（py demo 有）、唤醒回应本地 WAV（demo 用服务端 TTS
  问候，服务端对自身 TTS 播放窗口可能免疫回声）、下行播放队列满丢帧（demo 有背压）。

- **教训（乖宝 2026-08-11 16:42 点名）**：给到 demo / 官方示例 / 参考代码时，
  **必须先完整通读一遍再动手**，不凭经验猜、不挑着看——本轮 `speex_echo_playback/
  capture` 组合就写在官方头文件注释里（声卡延迟对齐），因未第一轮通读绕了 4 轮。
  流程铁律：拿到 demo → 全读 → 再设计。

## 下一步

1. ~~乖宝确认后 `brew install speexdsp`~~ ✅ 已装
2. ~~派 code-master 按本卡实现~~ ✅ 完成（单测全绿）
3. **真机冒烟（乖宝操作，已修复 filter_length 后复测）**：外放完整播放（不掐断）+ 插话打断；听感调参（`aec_filter_length` / 延迟偏移）
4. 冒烟通过后：收尾归档（更新 PROJECT.md/ARCHITECTURE.md 状态 → 归档任务卡 → 同步 MEMORY 指针）→ 确认后 commit
5. 候选后续：AEC 生效后恢复本地 VAD 打断并实测；滤波器长度/延迟自动校准
