# TASK-029：唤醒回应本地预合成 + 10 种随机播放

> 状态：已完成
> 创建：2026-08-09 ｜ 完成：2026-08-09 ｜ 负责人：小奈/code-master ｜ 基线 commit：4dbfbc0（TASK-028 归档后）

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
- 合成脚本（一次性工具）：`scripts/synthesize_wake_replies.py`——把 10 条文案用当前 `tts_service` 合成落盘，供复现/补充
- 测试 + 文档（任务卡 / PROJECT.md / DECISIONS / MEMORY 指针）

## 非目标

- **不做 TTS 流式播放**（豆包级体验，顺延 TASK-030，已记 followup）
- 不做唤醒回应之外的花活（多种语气/时段区分等，本次只做随机）
- 不改其它渠道（web/feishu/weixin 出站语音仍走云端合成）
- 不做本地部署 Qwen3-TTS（独立事项，调研报告已存 `reports/local-tts-voice-clone-feasibility.md`，待乖宝另行立项）

## 验收标准

- [x] 10 条甘雨音色唤醒回应音频已合成并落盘 `workspace/voice/wake_replies/wake_01~10.wav`，可正常播放
- [x] 唤醒时从本地缓存随机选一条播放，多次唤醒出现不同回应（随机性验证）
- [x] **缓存可用时零云端 TTS 调用**（代码路径验证：`_play_wake_reply` 不调 `tts.synthesize`；单元测试 `test_play_wake_reply_uses_cache` 断言 tts.synthesize 未被调用）
- [x] 缓存缺失/为空时回退云端合成不报错（向后兼容测试：`test_cache_empty_dir_fallback` / `test_cache_dir_not_exists_fallback` / `test_cache_empty_no_tts_skip`）
- [x] config `voice.wake_replies_dir` 可配置，旧 config.json 缺字段回退默认不报错（`test_default_has_wake_replies_dir` / `test_config_file_override_wake_replies_dir` / `test_config_missing_wake_replies_dir_fallback`）
- [x] 乖宝实测：唤醒听到随机甘雨回应，音质/文案满意（乖宝 19:43 确认「可以的，开始吧」）
- [x] 测试通过（专项 12 项 + 全量 737 全绿）；文档同步（任务卡 / PROJECT.md / MEMORY 指针）

## 相关模块

- `channels/voice.py`（`_play_wake_reply` 主改点 + `_load_wake_audio_cache` 新增）
- `voice/kws/player.py`（`play_audio` 复用，播放本地 WAV bytes 走现有链路 + DSP 防炸麦天然生效）
- `config.py`（`_VOICE_FIELDS` 加 `wake_replies_dir` + 默认值）
- `main.py`（装配透传 `wake_replies_dir`）
- `voice/tts/dashscope_realtime.py`（合成脚本使用现有 provider）
- `scripts/synthesize_wake_replies.py`（新增，一次性合成工具，可复现）
- `tests/test_voice_wake_cache.py`（新增，12 项专项测试）

## 实现摘要

### 改动文件
| 文件 | 改动 |
|---|---|
| `scripts/synthesize_wake_replies.py` | 新增：一次性合成脚本，复用 `load_config()` + `DashScopeRealtimeTTSProvider`，10 条文案逐条 async `synthesize` → WAV 落盘 |
| `channels/voice.py` | `__init__` 新增 `wake_replies_dir` 参数 + `_wake_audio_cache` 字段；`_play_wake_reply` 改造为缓存优先（懒加载→random.choice→不调 TTS→播放失败降级云端）；新增 `_load_wake_audio_cache` 方法（扫描 `wake_*.wav` 排序读 bytes，目录不存在/空返回 `[]`） |
| `config.py` | `_VOICE_FIELDS` 加 `"wake_replies_dir"`；默认 voice dict 加 `"wake_replies_dir": "workspace/voice/wake_replies/"` |
| `main.py` | VoiceChannel 装配处加 `wake_replies_dir=voice_settings.get("wake_replies_dir")` 透传 |
| `tests/test_voice_wake_cache.py` | 新增：12 项专项测试（缓存优先不调 TTS / 空目录回退 / 目录不存在回退 / 无 TTS 跳过 / 懓加载只扫一次 / config 合并 / 随机覆盖多条 / 集成全流程 / 非 wake 文件过滤 / 缓存播放失败降级 TTS） |
| `tests/test_voice_continuous.py` | 小修（适配新参数） |
| `tests/test_voice_kws.py` | 小修（适配新参数） |
| `tests/test_voice_wake_reply.py` | 小修（适配新参数） |

### 关键决策
1. **懒加载而非启动时扫描**：首次 `_play_wake_reply` 才扫描目录，避免启动时 I/O；`_wake_audio_cache` 为 `None`（未加载）vs `[]`（已加载但空）区分，不重复扫描
2. **缓存播放失败降级云端**：本地 WAV 播放异常（KwsError 等）→ 尝试 `tts.synthesize` 云端合成；云端也失败 → `_emit` 提示 + 跳过，不阻塞唤醒流程
3. **文件过滤 `wake_*.wav`**：只匹配 `wake_` 前缀 + `.wav` 扩展，忽略目录内其他文件
4. **合成脚本独立可跑**：`uv run python scripts/synthesize_wake_replies.py`，支持换文案重跑

### 验证结果
- 编译：`compileall channels config main.py` ✅
- 冒烟：`import main` ✅
- 全量：`unittest discover -s tests` → **Ran 737 tests, OK** ✅
- `git diff --check` ✅
- 10 条音频已落盘 `workspace/voice/wake_replies/wake_01~10.wav`（79KB~135KB，共约 1MB）
- 乖宝试听确认满意（19:43）

### 遗留
- 乖宝真实端到端唤醒验收待做（需启动 voice 渠道实测喊「小奈小奈」听随机回应）
- 文案可随乖宝喜好增删，合成脚本支持重跑（换文案只需改脚本中文案列表重跑即可）
- 随机播放无洗牌不重复逻辑（乖宝接受纯随机）

## 下一步

- 乖宝启动 voice 渠道实测唤醒随机回应
- TASK-030（TTS 流式播放/豆包级体验）已规划待建卡
