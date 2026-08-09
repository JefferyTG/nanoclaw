# TASK-028：播放防炸麦（音量归一化/限幅）

> 状态：✅ 已完成（2026-08-09 乖宝实测验收通过）
> 创建：2026-08-09 ｜ 负责人：小奈 ｜ 基线 commit：659d841（TASK-027 归档后）
> 归档：2026-08-09 ｜ 实现（code-master）→ v2 诊断修复（乖宝实测）→ 验收

## 目标

消除 voice 渠道语音播放「炸麦」——TTS 合成音频峰值满幅削波导致爆音/刺耳：播放前对 PCM 做**音量归一化 + 限幅**，让播报音量稳定、不炸不刺耳。

## 背景

- TASK-026 验收遗留（2026-08-09 乖宝验收拆分）：「播放炸麦（峰值满幅削波）→ TASK-028 音量归一化/限幅」
- 播放链路：`voice/kws/player.py` `play_audio`：ffmpeg 解码 → int16 PCM → `sd.play`。当前**无任何音量处理**——音频峰值接近满幅（int16 满幅 32767）或设备增益高时削波爆音（方波刺耳）
- 播放层是通用出口（唤醒回应 + Agent 回复都走 `player.py`），在播放层做 DSP 一处生效，不动 TTS 合成源

## 范围（全部完成）

- ✅ `voice/kws/normalize.py`（新增）：`normalize_playback_pcm(data, *, target_peak=0.89, max_gain_db=0.0, soft_clip=True)`——纯 numpy int16 PCM DSP，无新依赖
- ✅ `voice/kws/player.py`：`play_audio` 新增 `playback_params` 参数，解码后、`sd.play` 前调 DSP（None 用默认，向后兼容既有调用）
- ✅ `config.py`：`voice.playback.*` 白名单（`_VOICE_FIELDS` 加 `playback`；新增 `_VOICE_PLAYBACK_FIELDS`）、默认值（target_peak=0.89≈-1dBFS / max_gain_db=0 只压不抬 / soft_clip=True）、load_config 深度合并 + save_config 白名单过滤（同 vad 模式），旧 config.json 缺字段回退默认不报错
- ✅ `channels/voice.py`：`VoiceChannel.__init__` 新增 `playback_params` + `_sanitize_playback_params` 静态清洗（白名单 3 键、类型归一、非法回退默认）；`send()` 与唤醒回应播放两处透传
- ✅ `main.py`：装配处仿 `voice_vad_cfg` 推导 `voice_playback_cfg = voice_settings.get("playback") or {}` 传 `playback_params=`
- ✅ `tests/test_voice_normalize.py`（新增 34 项）+ 既有 voice 测试断言更新（`playback_params={}`）
- ✅ 文档：任务卡 / PROJECT.md / DECISIONS.md / MEMORY 指针

## 非目标

- 不做 EBU R128 响度标准化 / 多段压缩 / 动态范围压缩
- 不改 TTS 服务（`voice/tts/`）
- 不做设备/系统音量调节、蓝牙专项适配
- 不做录音侧（入站）处理

## 验收标准（全部通过）

- [x] 峰值超标的音频（满幅/近满幅正弦或方波）播放前被归一化/限幅到目标峰值内（单元测试验证处理后峰值 ≤ target_peak）
- [x] 低响度音频不被无脑放大炸底噪（增益有上限或只压不抬，测试覆盖）
- [x] play_audio 集成：DSP 处理后再 `sd.play`（测试 mock `sd.play` 断言收到的数组已处理）
- [x] config `voice.playback.*` 可配置，旧 config.json 缺字段回退默认不报错
- [x] **真实听感（2026-08-09 乖宝实测）**：第一轮实测「不炸了但偶发噼里啪啦」→ v2 加播放缓冲 latency 修复 → 第二轮实测「很好了，没有噼啪声了」✅
- [x] 专项测试通过（34 新专项 + 全量 725 OK）；文档同步（任务卡/PROJECT.md/DECISIONS/MEMORY）

## 迭代记录

### v1（2026-08-09 15:20，code-master 实现）
- DSP 核心：峰值检测（int32 安全 abs，规避 -32768 回绕误判静音）→ 峰值归一化（gain_db 语义、±48dB 钳制、低响度只压不抬提前 copy 返回）→ tanh 软限幅兜底 → clip → rint int16
- 34 项新专项 + 全量 725 过

### v2（2026-08-09 15:36~15:46，乖宝实测迭代）
- **现象**：音量正常（DSP 归一化生效）但**偶发「噼里啪啦」爆音**
- **排查**：网页端朗读正常 → TTS 源音频无爆音，排除 `voice/tts/`；默认输出 = MacBook Neo扬声器 48kHz 内置（非蓝牙）；结论：sounddevice 默认 low latency（~12ms）缓冲在 KWS/ASR/TTS 同机负载下 **underrun（缓冲区欠载）** → 偶发噼啪
- **修复**：`player.py` 新增 `_PLAYBACK_LATENCY_SEC = 0.15`，`sd.play` 显式传 `latency=0.15` 加大缓冲扛 CPU 抖动（对讲机场景播完才继续，延迟无感）
- **验收**：乖宝重启实测「很好了，没有噼啪声了」✅

## 相关模块

- `voice/kws/player.py`（播放出口，主改点）
- `voice/kws/normalize.py`（新增，DSP 实现）
- `channels/voice.py`（传参）
- `config.py`（白名单 + 默认值）
- `tests/`（新专项测试）

## 实现方案

- 在 `play_audio` 解码出 int16 numpy 数组后、`sd.play` 前调用 `normalize_playback_pcm(pcm, **playback_params)`：
  1. **峰值检测**：`np.abs(data.astype(np.int32))` 取 max（int32 安全 abs——int16 的 -32768 无正表示，直接用 np.abs 会回绕成 -32768，导致「全 -32768」满幅信号被误判为静音放行，已用 `test_all_minus_32768_not_mistaken_for_silence` 锁定）
  2. **峰值归一化**：`gain_db = 20*log10(target_abs/peak)`；`max_gain_db` None/<0/0 → 只压不抬；`gain_eff = min(gain_db, max_gain_db)`；钳制 ±48dB 防放大爆炸；低响度+不抬升 → 提前 copy 返回零开销
  3. **软限幅兜底**：`y = target_abs * tanh(x/target_abs)`（soft_clip=True 时）→ clip [-32768, 32767] → `np.rint().astype(np.int16)`
- 全程 float64 计算再转回 int16；空数组/全零静音原样返回；非 int16 输入 raise ValueError
- 播放缓冲：`_PLAYBACK_LATENCY_SEC = 0.15` 显式传给 `sd.play`（v2，抗 underrun 噼啪）
- 参数默认：`target_peak=0.89`（≈ -1dBFS）、`max_gain_db=0`（只压不抬）、`soft_clip=True`；全部经 config `voice.playback.*` 可配
- `channels/voice.py` 构造新增 `playback_params`（清洗透传，同 `vad_params` 模式），`send()` 与 `_play_wake_reply` 两处调用透传

## 测试方式

- `.venv/bin/python -m unittest discover -s tests`：**全量 725 通过（基线 691 + 新增 34）**
- 单元（13 项）：满幅正弦/方波 → 处理后峰值 ≤ target_peak；低响度 → 峰值不变（只压不抬）；max_gain_db=6 受控抬升不超 target；int16 边界（-32768/32767）无溢出；空数组/静音安全；-32768 满幅不误判静音；soft_clip=False 纯 clip 路径
- 集成（5 项）：mock `sd.play` 断言收到已处理数组；正常小音量播放正常返回
- config（5 项）：playback 白名单合并/未知丢弃/缺字段回退；save 过滤
- 渠道（8 项）：`_sanitize_playback_params` 类型归一/非法回退；存储与透传；main 装配镜像

## 风险 / 遗留

- 参数保守（target_peak=0.89 峰值处 tanh 约 24% 压缩）：若嫌小可在 config.json 调大 target_peak 或放开 max_gain_db 受控抬升（乖宝实测音量舒适，未调）
- `play_audio` 本身不清洗 playback_params（直接绕过渠道调用传未知键会 TypeError）；生产链路渠道已清洗
- 与设备/系统音量叠加，实际响度以乖宝听感为准（系统音量 80% 实测舒适）

## 下一步（已立项规划）

- **TASK-029（规划）**：TTS 流式播放（真正「边说边播」）——provider 暴露流式合成接口 + player 改 `sd.OutputStream` 边收边写 + DSP 适配逐块归一化；乖宝目标「豆包级」体验，分期推进（TTS 流式播放 → LLM 按句切分流式喂 TTS → 流式 ASR）
