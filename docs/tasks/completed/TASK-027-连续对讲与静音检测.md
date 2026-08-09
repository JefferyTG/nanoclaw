# TASK-027：连续对讲（回复播完自动续听）+ 静音检测 + [END] 结束语 + voice 简短俏皮 Prompt

## 任务卡

- 状态：已完成（2026-08-09 乖宝验收通过：简单实测后确认可完成，验收反馈的打印/TTS 清洗需求均已实现）
- 负责人：小奈
- 执行会话/子 Agent：小奈 + code-master（实现）
- 基线 commit / 分支：main@origin/main（e60ba68，TASK-026 归档后）
- 依赖任务：TASK-025（唤醒→录音→ASR→对话闭环）、TASK-026（回复 TTS 播放 + 空闲分片 + 保留上限）

### 目标

把 voice 渠道从「单轮唤醒对讲」升级为「连续对讲机」：小奈播完回复后**自动开始听下一句**（不用每次喊「小奈小奈」），像真对讲机一样连续聊天；同时解决两个体验问题——①录音固定等满 `record_sec`（8s）才转写，说话快的人要干等 → 加**静音检测**，说完话停顿即提前结束录音立即回复；②语音播报听不了长篇大论 → 给 voice 渠道加专属 Prompt：回复**简短俏皮口语化**（除非知识类问题），并约定结束语带 `[END]` 标记自动退出连续对话。

### 非目标

- 不做播放防炸麦（音量归一化/限幅）——TASK-028 单独做（乖宝 2026-08-09 拆分，先讨论）
- 不做 voice 渠道工具白名单限制——乖宝 2026-08-09 拍板先记待办（followups.jsonl），功能做好后再加
- 不做唤醒词优化/训练、多设备音频路由、跨渠道联动

### 允许修改

- `channels/voice.py`（连续对讲状态机、[END] 处理、静默退出、分片暂停）
- `voice/kws/recorder.py` 或 `voice/kws/` 新增模块（静音检测 VAD：流式录音 + 能量检测 + 说完提前结束 + 静默判定）
- `agent/context.py`（`_channel_section` 加 voice 分支：简短俏皮指令 + [END] 约定，与 weixin 分支并列）
- `config.py` / `config.json`（`voice.record_delay_sec` 默认 0.5、`voice.silence_timeout_sec` 默认 5.0、VAD 参数 `voice.vad.*`，并纳入 `_VOICE_FIELDS` / `_VOICE_KWS_FIELDS` 白名单；旧 config.json 缺字段回退默认）
- `main.py`（voice 渠道装配新参数）
- `tests/`（VAD / 状态机 / [END] / 静默退出 / Prompt 注入测试）
- 任务卡 + PROJECT.md + DECISIONS.md 相关状态

### 禁止修改

- 其他渠道代码（feishu/weixin/web/cli）
- TTS 服务核心实现（`voice/tts/`）与 `voice/kws/player.py`（播放/炸麦留给 TASK-028）
- config.json 本机配置（只改默认值与白名单）

### 上下文与约束

- 相关代码入口：
  - `channels/voice.py`：`_handle_wake`（唤醒→播回应→录音→ASR→inject_text）、`_play_wake_reply`、`send()`（TTS 播放，TASK-026）、`inject_text`、`_maybe_split_session`（空闲分片）、`_create_session`、`_prune_old_sessions`
  - `voice/kws/recorder.py`：`record_audio(duration_sec, sample_rate, device)` 目前用 `sd.rec` 固定时长 + `sd.wait`，WAV 封装；需加流式 VAD 版本
  - `voice/kws/player.py`：`play_audio`（播完才返回，TASK-026 回复播放已用它）
  - `agent/context.py`：`_channel_section()` 已有 `weixin` 分支（微信日常对话模式，TASK-019），加 `voice` 分支同款做法
  - `config.py`：`_VOICE_FIELDS` 白名单（TASK-026 已加 idle_ttl_sec/max_sessions/max_voice_chars）
- 相关架构/历史决策（2026-08-09 乖宝逐条拍板）：
  1. **连续对讲循环**：回复播完 → 等 `record_delay_sec`（默认 0.5s，避免截到小奈自己话音尾巴）→ 自动开始录音 → ASR → inject_text → Agent → send() 播放 → 循环；全程不用再喊唤醒词
  2. **静音检测 VAD**：录音中持续检测人声（能量/RMS 阈值），说完话停顿 `vad.silence_end_sec`（默认 1.2s，可配置）→ 提前结束录音立即转写回复（不等满 record_sec）；`record_sec` 变**最长上限**；全程无人声 → 判定静默（返回标记），调用方据此退出连续对话。唤醒单轮与连续对讲共用
  3. **静默退出**：开听后 `silence_timeout_sec`（默认 5s，可配置）没检测到人声 → 静默退出连续对话回待唤醒
  4. **[END] 结束语退出**：voice 渠道 Prompt 约定——用户告别/结束话题（拜拜、不聊了、晚安等）时，模型回复**末尾带 `[END]` 标记**；渠道在 send() 检测到标记 → 剥离标记 → 正常播放告别语（模型说的话）→ 播完退出连续对话回待唤醒（纯文字回退路径也退出）
  5. **空闲分片配合**：连续对话进行期间**不触发**空闲分片（对话活跃不分片）；退出连续对话后长时间没人说话，空闲分片照旧（TASK-026 逻辑不变）
  6. **voice 简短俏皮 Prompt**（乖宝强调「比较重要」）：voice 渠道回复要**简短俏皮口语化**（一两句话说完），**除非用户问知识类/需要详细解释的问题**——语音播报听不了长篇大论；与 TASK-019 微信日常模式同做法，纯 Prompt 层，不影响其他渠道
- 已知风险：VAD 能量阈值需真实环境调参（阈值太高吃话尾/太低提前切）；连续对讲期间唤醒词再触发需防抖合并（沿用 `_wake_in_progress`）；蓝牙单流麦克风尽力而为；[END] 标记可能被模型漏打/误打（Prompt 约定 + 兜底：静默超时也能退）

### 验收标准

- [x] 连续对讲：回复播完 → 等 0.5s → 自动录音 → 转写 → 回复 → 循环，不用每次喊唤醒词（状态机+专项测试✅；真实听感待乖宝）
- [x] 静音检测：说完话停顿 1.2s 提前结束录音立即回复，不等满 8s（VAD 模块+测试✅；真实环境参数待调）
- [x] 静默退出：开听后 5s 没人声 → 自动退出回待唤醒（状态机+测试✅）
- [x] [END] 退出：说「拜拜」→ 小奈播告别语 → 退出连续对话；[END] 标记不出现在播报内容里（剥离逻辑+测试✅；真实听感待乖宝）
- [x] 空闲分片：连续对话期间不触发；退出后超时照旧分片（测试✅）
- [x] Prompt：voice 渠道回复简短俏皮（真实端到端听感），其他渠道不受影响（Prompt 注入✅；乖宝简单实测后确认完成）
- [x] 专项测试通过；文档同步（任务卡/PROJECT.md 能力矩阵/DECISIONS/MEMORY 指针）

### 必须执行的验证

```bash
.venv/bin/python -m unittest discover -s tests   # ✅ 676 全过（2026-08-09）
# 手动端到端（待乖宝）：喊醒 → 连续聊 3 轮以上不用再喊 → 说「拜拜」听告别语后退出 → 再喊恢复；
# 验证说短句快速得到回复（VAD 提前结束）；验证 5s 不说话自动退出；
# 验证连续聊天超过 30 分钟不分片（或临时调 idle_ttl_sec 模拟）
```

## 执行交接

- 状态：已完成（2026-08-09 乖宝验收通过）
- 实际改动文件：
  - `voice/kws/vad.py`（新增）：`record_audio_vad(max_duration_sec, *, sample_rate, device, silence_end_sec=1.2, energy_threshold=400.0, min_voice_sec=0.3, block_sec=0.05) -> (wav_bytes, is_silent)` 流式静音检测录音；sd.InputStream callback → 队列 → RMS 状态机（安静→有声→静音达 silence_end_sec 提前结束；全程人声 < min_voice_sec 判静默）；to_thread 执行；PortAudioError→KwsError；非法参数回退默认
  - `channels/voice.py`（+338/-90 + 调试打印补充）：连续对讲状态机——`_enter_continuous`/`_exit_continuous`（幂等）、`_schedule_next_listen`（`_listen_task` 任务引用防重入）、`_listen_round`（sleep record_delay_sec → record_audio_vad → 静默累计/清零 → ASR → inject_text）、`_strip_end_marker`（TTS 合成前剥离 [END]，播完退出，纯文字兜底也退出）、`_maybe_split_session` 连续对讲中短路不分片、`_on_wake` 追加 `or self._continuous` 防抖合并；新构造参数 `record_delay_sec=0.5` / `silence_timeout_sec=5.0` / `vad_params=None`（`_sanitize_vad_params` 过滤渠道级字段）
  - `agent/context.py`（+11）：`_channel_section()` 新增 voice 分支——【voice 连续对讲模式】简短俏皮口语化（知识类问题可多讲）+ 告别/结束话题时回复末尾带 [END] 约定（与 weixin 分支并列，纯 Prompt 层）
  - `config.py`：`_VOICE_FIELDS` 增 record_delay_sec/silence_timeout_sec/vad；新增 `_VOICE_VAD_FIELDS=(energy_threshold, silence_end_sec, min_voice_sec, block_sec)`；voice 默认值补全（0.5 / 5.0 / vad 默认四键与 vad.py 一致）；load_config/save_config vad 白名单合并（同 kws 模式）
  - `main.py`：voice 装配传 record_delay_sec/silence_timeout_sec/vad_params（vad dict 透传，渠道内过滤渠道级键）
  - `tests/`：`test_voice_vad.py`（8 例）、`test_voice_continuous.py`（16 例）、`test_voice_main.py`（5 例装配）、`test_voice_kws.py`（+5 config 用例）、`test_voice_wake_reply.py`（适配）、`test_channel_context.py`（+3 voice Prompt 用例）
  - **乖宝验收反馈补充①（2026-08-09）**：voice 渠道前台调试打印（print + `[voice]` 前缀 + emoji 区分）——①唤醒词命中（`_on_wake`，对话进行中合并时注明）；②退出连续对讲（`_exit_continuous`）；③乖宝说的转写文本（`_listen_round` 注入 Agent 前）；④小奈回复文本（`send()` 剥离 [END] 后，含标记时注明）。纯打印不影响逻辑，全量 676 测试仍通过
  - **乖宝验收反馈补充②（2026-08-09）**：TTS 前文本清洗（防奇怪标点/表情被念出来）——双保险：① Prompt 层：context.py voice 分支加「纯口语文字」约束（不用 markdown/emoji/奇怪符号，情绪用哈哈/呜呜表达，原 ③ 变 ④）；② 渠道层兜底：`VoiceChannel._sanitize_for_tts`（静态方法）在 send() 剥离 [END] 后、TTS 合成前清洗——剥 markdown（链接/加粗/代码/行首标题/列表/引用/数字列表）、删 emoji 与装饰符号块（emoji/区域指示符/箭头/几何/杂项/变体选择符/ZWJ）、压缩连续标点（`\1+`：！！→！、？？→？、。。。→。）、压缩多余空白；中文标点【】「」（）保留。全 emoji 回复清洗后为空 → 不播直接退出。新增 tests/test_voice_sanitize.py（15 例含 send() 集成），全量 691 测试通过
- 关键决策与假设：见上方「相关架构/历史决策」6 条（乖宝 2026-08-09 逐条确认）；实现中补充：静默轮按实际录音时长（WAV 头解析）累计、wav=None 按整轮 record_sec 计防 0 时长死循环；[END] 任意位置剥离（模型可能不放末尾）
- 验证命令与结果：`.venv/bin/python -m unittest discover -s tests` → **Ran 676 tests, OK**（2026-08-09，含既有 88 个 voice 渠道测试回归）
- 未验证项：真实麦克风端到端（喊醒→连续聊→拜拜退出→再喊恢复）、VAD 阈值真实调参、[END] 模型遵循度、语音播报听感（乖宝验收项）
- 风险与遗留问题：VAD 能量阈值 400 为合成音频验证保守值需真实环境调参；`asyncio.to_thread` 取消语义（底层 InputStream 不被真正取消，当前无硬取消需求）；[END] 依赖模型 Prompt 遵循度（有静默退出兜底）；`_channel_section` 基础文案「渠道名取内部名（feishu/weixin/web/cli）」未含 voice（共享文案，一行改动，留待需要时）；炸麦（TASK-028）与工具白名单（followup）另行跟进
- commit（仅在获授权时）：待乖宝授权后提交（含 PROJECT.md 状态行修改）
- 当前 `git status --short --branch`：main...origin/main（e60ba68）；工作区含本任务全部改动 + PROJECT.md 状态行修改 + 任务卡（未跟踪）
- 建议下一步：乖宝真实端到端验收（喊醒→连续聊 3 轮→拜拜→再喊恢复；短句快速回复；5s 静默退出；临时调 idle_ttl_sec 验证不分片）→ 验收通过后归档 + commit

## 负责人验收

- [x] 检查 diff 与授权范围（仅任务卡授权文件：voice.py / vad.py / context.py / config.py / main.py / tests/ / 文档）
- [x] 独立复跑关键验证（全量 `unittest discover -s tests` = 691 OK；`git diff --check` / `compileall` / `import main` 均通过）
- [x] 检查秘密/个人数据/运行产物（无新增密钥；音频全程内存不落盘；无运行产物入仓库）
- [x] 检查文档与配置一致性（任务卡 / PROJECT.md 能力矩阵+Git 状态段 / DECISIONS.md 均已同步）
- [x] 更新 `docs/DECISIONS.md` 中相关状态（已新增「voice 连续对讲 + 静音检测 + [END] 结束语（TASK-027 决策）」行）
- 验收结论：**通过**（乖宝 2026-08-09 简单实测确认；验收反馈补充需求①调试打印②TTS 文本清洗均已实现并测试）
- 证据与备注：全量 691 tests OK；TASK-027 决策行已入 DECISIONS；遗留风险（VAD 阈值需真实调参 / to_thread 取消语义 / [END] 模型遵循度 / 云端 ASR 慢）记入任务卡与 DECISIONS，不阻塞归档
