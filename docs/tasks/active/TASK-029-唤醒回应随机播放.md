# TASK-029：唤醒回应本地预合成 + 10 种随机播放

> 状态：待开始
> 创建：2026-08-09 ｜ 负责人：小奈 ｜ 基线 commit：4dbfbc0（TASK-028 归档后）

## 目标

voice 渠道唤醒回应升级：**预合成 10 条甘雨音色回应音频存本地，唤醒时随机播一条**——彻底告别「每次唤醒都云端 TTS 合成扣字符费」，同时让回应更多样、更像活人。

## 背景

- 当前唤醒回应（TASK-025 方案 B）：`voice.wake_replies` 文本列表 + `random.choice`，但**每次唤醒都实时云端合成**（`_play_wake_reply` → `tts.synthesize` → `play_audio`），一条 9 字回应也按字符计费——2026-08-09 乖宝反馈 qwen 欠费，免费额度（1 万字符）耗尽，唤醒回应重复合成是浪费大头之一
- 乖宝需求（2026-08-09 16:08）：「合成几个唤醒回应，每次唤醒随机回复那种，生成 10 种」——先建卡，乖宝说「开始」再合成，合成完乖宝试听
- 甘雨音色已就绪（`qwen-tts-vc-myclone-voice-20260807125201837-750c`，本地 08-07 复刻创建），云端 TTS 服务 `tts_service` 可用

## 范围

- **预合成 10 条唤醒回应音频**（甘雨音色，WAV 落盘到 `workspace/voice/wake_replies/`，文件名 `wake_01.wav` ~ `wake_10.wav`），文案为小奈原创的 10 条甘雨风短句（见实现方案）
- `channels/voice.py`：`_play_wake_reply` 改为**优先播放本地缓存**——启动时（或首次唤醒时）扫描 `voice.wake_replies_dir` 加载音频列表，唤醒时 `random.choice` 选一条本地播放；**缓存可用时不再调用云端 TTS**
- 缓存缺失/为空/播放失败 → 回退现有行为（`tts_service` 云端合成；若 tts 也 None 则跳过回应），向后兼容
- `config.py`：`voice` 白名单新增 `wake_replies_dir`（默认 `workspace/voice/wake_replies/`，可配置）；旧 config.json 缺字段回退默认
- `main.py`：装配透传 `wake_replies_dir`
- 合成脚本（一次性工具）：`scripts/synthesize_wake_replies.py`（或直接手动合成，见实现方案）——把 10 条文案用当前 `tts_service` 合成落盘，供复现/补充
- 测试 + 文档（任务卡 / PROJECT.md / DECISIONS / MEMORY 指针）

## 非目标

- **不做 TTS 流式播放**（豆包级体验，顺延 TASK-030，已记 followup）
- 不做唤醒回应之外的花活（多种语气/时段区分等，本次只做随机）
- 不改其它渠道（web/feishu/weixin 出站语音仍走云端合成）
- 不做本地部署 Qwen3-TTS（独立事项，调研报告已存 `reports/local-tts-voice-clone-feasibility.md`，待乖宝另行立项）

## 验收标准

- [ ] 10 条甘雨音色唤醒回应音频已合成并落盘 `workspace/voice/wake_replies/wake_01~10.wav`，可正常播放
- [ ] 唤醒时从本地缓存随机选一条播放，多次唤醒出现不同回应（随机性验证）
- [ ] **缓存可用时零云端 TTS 调用**（代码路径验证：`_play_wake_reply` 不调 `tts.synthesize`；可用日志/计数验证）
- [ ] 缓存缺失/为空时回退云端合成不报错（向后兼容测试）
- [ ] config `voice.wake_replies_dir` 可配置，旧 config.json 缺字段回退默认不报错
- [ ] 乖宝实测：唤醒听到随机甘雨回应，音质/文案满意
- [ ] 测试通过（专项 + 全量）；文档同步（任务卡 / PROJECT.md / DECISIONS / MEMORY 指针）

## 相关模块

- `channels/voice.py`（`_play_wake_reply` 主改点）
- `voice/kws/player.py`（`play_audio` 复用，可加本地文件播放入口或直接读文件 bytes 走现有链路）
- `config.py`（`_VOICE_FIELDS` 加 `wake_replies_dir`）
- `main.py`（装配透传）
- `voice/tts/dashscope_realtime.py`（合成脚本使用现有 provider）
- `scripts/synthesize_wake_replies.py`（新增，一次性合成工具，可复现）

## 实现方案

**10 条文案（小奈原创甘雨风，简短口语）**：
1. 哎，我在呢，你说吧
2. 嗯嗯，我听着呢
3. 怎么啦？我在这儿呢
4. 我在呢～有什么事儿吗
5. 嗯？叫我呀～我在的
6. 来了来了，你说吧
7. 在的呢，我一直都在
8. 怎么了呀？想跟我说什么
9. 嗯哼～想聊什么？说吧
10. 听到啦！什么事儿，你说～

**合成**：写一次性脚本 `scripts/synthesize_wake_replies.py`：读 config 拿 `tts_model.dashscope_realtime`（api_key/voice_id/model/instructions），用 `DashScopeRealtimeTTSProvider.synthesize` 逐条合成 → WAV bytes 写 `workspace/voice/wake_replies/wake_01.wav`...（目录不存在自动建）。合成后脚本打印每条的时长/大小。

**播放路径改造**（`channels/voice.py`）：
- `__init__` 新增 `wake_replies_dir: str | None = None`（默认 `workspace/voice/wake_replies/` 或相对 workspace）
- 首次 `_play_wake_reply` 时懒加载：扫描目录 `wake_*.wav`（排序），加载 bytes 列表存 `self._wake_audio_cache`；空目录/无文件 → 缓存为空
- 缓存非空 → `audio_bytes = random.choice(cache)`，`play_audio(audio_bytes, "audio/wav", playback_params=...)` 直接播放，**不调 tts_service**；播放失败（KwsError）→ 降级尝试云端合成或跳过（同现有失败路径）
- 缓存为空 → 走现有 `tts.synthesize` + `play_audio` 路径（向后兼容）
- 顺带小优化：`play_audio` 对本地 WAV 解码后同样过 DSP（播放防炸麦 TASK-028 已覆盖，天然生效）

**config**：`_VOICE_FIELDS` 加 `"wake_replies_dir"`；默认 `voice` dict 加 `"wake_replies_dir": "workspace/voice/wake_replies/"`；深度合并照常（顶层字段直接合并，无需子白名单）

**main.py**：`wake_replies_dir=voice_settings.get("wake_replies_dir")` 或默认路径，透传给 VoiceChannel

**测试**：
- 单元：`_play_wake_reply` 缓存非空时 mock `play_audio` 断言不调 `tts.synthesize`（可 mock tts_service 抛异常验证未被调用）；缓存为空回退云端路径；随机性（多次调用覆盖多条，不做严格断言）；懒加载只扫一次
- 集成：造临时目录放 2~3 条假 wav → 渠道构造 + `_play_wake_reply` → 断言播放的是缓存音频
- config：`wake_replies_dir` 白名单合并/缺字段回退
- 全量：`.venv/bin/python -m unittest discover -s tests`

## 风险

- 合成 10 条会消耗约 100~150 字符云端额度（一次性，~1 分钱，可接受）；若 qwen 欠费未充值，合成脚本会失败——乖宝已说充值 10 元，无碍
- 随机播放无法保证每条都听过（纯随机可能有重复）——本次不做「洗牌不重复」的聪明逻辑，乖宝接受随机即可
- 本地音频文件占用小（10 条 × 几 KB ~ 几十 KB），可 gitignore（workspace/ 本身不追踪，无需处理）
- 版权：文案为小奈原创，音色为乖宝付费复刻，本地自用无碍

## 下一步

- 乖宝说「开始」→ ①先写合成脚本跑 10 条 → 乖宝试听文案/音色 ②试听满意再接播放路径改造 + 测试 + 文档
- 文案可随乖宝喜好增删（换句子/加语气），合成脚本支持重跑
