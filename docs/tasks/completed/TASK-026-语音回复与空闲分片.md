# TASK-026：语音回复 + 空闲自动分片

## 任务卡

- 状态：✅ 已完成（2026-08-09 归档）
- 负责人：小奈
- 执行会话/子 Agent：小奈 + code-master（实现）
- 基线 commit / 分支：main@origin/main（c494525，TASK-025 归档后当前 HEAD；2026-08-09 开工时校准）
- 依赖任务：TASK-025（入站闭环）

### 目标

完成语音对讲机最后一公里：Agent 回复用甘雨 TTS 合成并**播放到系统默认输出设备**（macOS 自动路由到蓝牙耳机或扬声器，小奈不关心目标设备）；voice 渠道增加**空闲自动分片**——超过阈值（默认 30 分钟）没消息，自动开新会话（seq+1，旧会话保留），并控制会话保留上限（默认最近 50 段，超了清最老）。

### 非目标

- 不做唤醒词优化/训练
- 不做多设备音频路由（系统默认输出即可）
- 不做跨渠道联动（voice 与微信/飞书独立）
- 不做连续对讲（回复播完自动续听）——乖宝 2026-08-09 拆分到 TASK-027
- 不做播放防炸麦（音量归一化/限幅）——乖宝 2026-08-09 拆分到 TASK-028

### 允许修改

- `channels/voice.py`（TTS 播放 + 空闲分片 + 保留上限）
- `voice/tts/` 复用现有 TTS 服务（甘雨音色，不重写）；播放复用 `voice/kws/player.py` 的 `play_audio`
- `config.py` / `config.json`（`voice.idle_ttl_sec` 默认 1800、`voice.max_sessions` 默认 50、`voice.max_voice_chars` 默认 300、`voice.record_sec`）
- `main.py`（voice 渠道注入 tts_service / idle_ttl_sec / max_sessions / max_voice_chars / session_pruner）
- `tests/`（空闲分片/保留上限/播放 mock 测试）
- 任务卡 + PROJECT.md 相关状态

### 禁止修改

- 其他渠道代码
- TTS 服务核心实现（只注入复用）

### 上下文与约束

- 相关代码入口：`voice/tts/service.py`（TextToSpeechService，甘雨 dashscope_realtime 或 edge-tts）、`voice/kws/player.py`（`play_audio(audio_bytes, media_type)`，ffmpeg 解码 + sounddevice 播放到默认输出、播完才返回）、`channels/base.py`（`Channel.send` 是 async）、`session/manager.py`（`clear` 删 JSONL+meta；`workspace/sessions/voice_local_<seq>.jsonl|.meta.json|_images|_videos`）
- 相关架构/历史决策：
  - 播放到系统默认输出设备（`play_audio` 已实现，device=None）；macOS 自动路由蓝牙/扬声器，代码不感知目标设备
  - 空闲分片用 `/new` 机制（seq+1 开新会话，旧会话保留可回查），不是 `/clear`（清空会丢历史）
  - 分片检查是惰性的：每次入站消息时检查距上次消息时间，超阈值自动 seq+1
  - 会话保留上限：超 `max_sessions` 清理最老语音会话（JSONL + meta + `_images`/`_videos` 目录，仅 voice 渠道）
  - 播放长文本体验差 → 新增 `voice.max_voice_chars`（默认 300，参考 TASK-021 飞书先例）：Agent 回复超长时直接回文字不合成播放；≤0 表示不截断
  - 时间用 `time.time()` 存 unix 秒；可注入 `now_fn`（测试用可控时钟）
  - 清理复用现有能力：`session_manager.clear` + `image_store.clear` + `video_store.clear`（与 `clear_callback` 同范式）
- 已知风险：TTS 播放延迟（甘雨实时流式已低延迟）；清理老会话是删除操作，只清 voice 渠道；回复播放期间若用户再次唤醒可能打断播放（非目标，记录）；真实播放偶发炸麦（峰值满幅削波，TASK-028 跟进音量归一化/限幅）

### 验收标准

- [x] Agent 回复文字 → 甘雨 TTS → 播放到默认输出（扬声器/蓝牙耳机自动路由）——已实现（`send()` 合成+播放），mock 测试覆盖；真实出声已由乖宝端到端验收（听到甘雨回复，但发现炸麦→TASK-028）
- [x] TTS 失败回退：合成/播放失败时回复文字（不静默）——已实现，测试覆盖（TTSError / KwsError / 其它异常）
- [x] 空闲超时自动分片：模拟超过 `idle_ttl_sec` 无消息 → 下次消息进入新会话 `voice:local:<seq+1>`，旧会话保留——已实现，测试覆盖（可控时钟）
- [x] 会话保留上限生效：超过 `max_sessions` 清最老 voice 会话（仅 voice 渠道，不动其他渠道）——已实现，测试覆盖（mock pruner）；真实删除逻辑复用三连 clear
- [x] 专项测试通过；文档同步——640 全绿，任务卡/PROJECT.md/DECISIONS/MEMORY 已同步

### 必须执行的验证

```bash
.venv/bin/python -m unittest discover -s tests   # ✅ 640 tests OK（2026-08-09 归档前复跑）
# 手动端到端：乖宝 2026-08-09 真实验收——喊「小奈小奈」→ 甘雨回应 → 说话 → 甘雨语音回复 ✅；
# 发现①回复完回到待唤醒（单轮，非连续对讲）→TASK-027；②播放炸麦→TASK-028
```

## 执行交接

- 状态：✅ 已完成（2026-08-09 归档）
- 实际改动文件：
  - `channels/voice.py`：`send()` 实现 TTS 合成+播放（失败/超长/未配置回文字）；`__init__` 新增 `max_voice_chars` / `idle_ttl_sec` / `now_fn` / `max_sessions` / `session_pruner`；新增 `_bump_activity` / `_create_session` / `_prune_old_sessions` / `_maybe_split_session`；命令分支只记活动不触发分片
  - `config.py`：`_VOICE_FIELDS` 追加 `idle_ttl_sec` / `max_sessions` / `max_voice_chars`；voice 默认 dict 补 `idle_ttl_sec: 1800` / `max_sessions: 50` / `max_voice_chars: 300`
  - `main.py`：VoiceChannel 注入 `max_voice_chars` / `idle_ttl_sec` / `max_sessions` / `session_pruner`；`prune_voice_session` 复用 `session_manager.clear` + `image_store.clear` + `video_store.clear`（删 JSONL+meta+_images+_videos，仅 voice 渠道）
  - `tests/test_voice_tts_reply.py`（11 用例）、`tests/test_voice_idle_split.py`（5 用例）、`tests/test_voice_prune.py`（4 用例）
- 实现摘要：TASK-026 三目标全部落地——① Agent 回复→甘雨 TTS→`play_audio` 播默认输出，失败/超长（max_voice_chars=300）/未配置回文字不静默；② 空闲分片惰性检查（每次入站消息对比 `idle_ttl_sec`=1800s，超时自动 seq+1 开新会话，旧会话保留可 `/switch` 切回，命令不触发分片）；③ 会话保留上限（`max_sessions`=50，超限从最老 seq 0 清到 ≤ 上限，复用 session/image/video 三连 clear，仅 voice 渠道）。`/new` 与分片共用 `_create_session()`；时间用 `time.time()` + 可注入 `now_fn` 测试。
- 关键决策与假设：idle 阈值默认 30 分钟；保留上限 50 段；播放走系统默认输出（复用 `play_audio`）；超长回复回文字 `max_voice_chars`（默认 300，TASK-021 先例）；清理复用现有 clear 能力（不手写删除）
- 验证命令与结果：
  - `.venv/bin/python -m compileall -q channels voice config.py main.py` ✅
  - `.venv/bin/python -m unittest tests.test_voice_tts_reply tests.test_voice_idle_split tests.test_voice_prune -v` ✅ 20 tests OK
  - `.venv/bin/python -m unittest discover -s tests` ✅ **640 tests OK**（归档前复跑）
  - voice 专项 80 tests OK（既有 test_voice_channel/wake_reply/kws 无回归）
  - `.venv/bin/python -c "import main"` ✅；`git diff --check` ✅
- 未验证项：空闲分片/保留上限的真实等待与清理需长时间运行观察（逻辑已 mock 测试覆盖）；真实唤醒期间播放回复的并发时序未验证
- 风险与遗留问题：老会话清理是删除操作，只清 voice 渠道（已确认范围）；TTS 播放需要音频设备可用；**播放炸麦（峰值满幅削波）→ TASK-028 音量归一化/限幅跟进**；**回复播放完回到待唤醒、需再喊唤醒词（单轮模式）→ TASK-027 连续对讲跟进**；回复播放中再次唤醒可能打断（记录，非目标）
- commit（仅在获授权时）：待乖宝确认（提交 NanoClaw 仓库，含 4 改 + 3 新测试文件）
- 当前 `git status --short --branch`：main...origin/main；M channels/voice.py、config.py、main.py、任务卡归档；?? 3 个新测试文件
- 建议下一步：TASK-027（连续对讲：回复播完自动续听）/ TASK-028（防炸麦）建卡开工

## 负责人验收

- [x] 检查 diff 与授权范围——改动全在任务卡允许列表内，未触碰其他渠道/TTS 核心
- [x] 独立复跑关键验证——compileall / import main / 640 全量 OK / git diff --check
- [x] 检查秘密/个人数据/运行产物——未提交 config.json 等敏感文件；测试不触碰真实 sessions
- [x] 检查文档与配置一致性——任务卡/PROJECT.md/DECISIONS/MEMORY 已同步
- [x] 更新 `docs/DECISIONS.md` 中相关状态——已加 TASK-026 决策行
- 验收结论：✅ 乖宝 2026-08-09 同意归档（真实端到端验收发现炸麦与单轮模式，已拆分 TASK-027/028 跟进，不阻塞本任务）
- 证据与备注：640 tests OK；真实语音回复播出成功但音质有炸麦（TASK-028）
