# TASK-021-飞书出站语音（语音对语音）

> 状态：已完成（2026-08-08 小奈复核并归档）
> 创建：2026-08-08 ｜ 负责人：小奈 + code-master ｜ 基线 commit：1921905（TASK-020 飞书语音转写入站）

## 目标
乖宝在飞书发语音，小奈用甘雨声音回语音气泡——**语音对语音**（发语音→回语音；发文字→回文字）。

## 背景
- TASK-020 已完成飞书入站语音（audio → ASR 转写进 Agent），出站语音当时列为非目标独立立项。
- 零件现成（2026-08-08 调研确认）：
  - TTS：`TextToSpeechService.synthesize(text)`（`dashscope_realtime` 甘雨音色 + `instructions` 情绪，返回 **WAV bytes**，TASK-017/018；`edge-tts` 可切回）。
  - ffmpeg：ASR 已在用（`voice/media.py`），转 OPUS 现成命令。
  - 上传：lark SDK `CreateFileRequest`（`im/v1/files`，file_type=opus）✅ 已确认存在。
  - 发送：`CreateMessageRequest` msg_type 现有 text/image，加 `"audio"` + content `{"file_key": ...}`。
- 触发语义（乖宝 2026-08-08 拍板）：**语音对语音**——不搞全量语音、不做命令开关，保持极简。

## 范围
- `channels/feishu.py`：语音入站成功转写后记录「语音回复待发」标记（chat_id 维度）；`send()` 命中标记时走语音回复链路（TTS 合成 → ffmpeg 转 OPUS → `CreateFileRequest` 上传 → `msg_type("audio")` 发送），消费后清除标记。
- 新增 wav→opus 转换（复用 ffmpeg，临时文件即用即删，仿 ASR service 模式）。
- `main.py`：feishu channel 注入 `tts_service`（可 None）。
- `tests/`：新增飞书出站语音测试（mock TTS + mock 上传）。
- 文档同步：PROJECT.md 能力矩阵（语音朗读/出站语音行）；归档任务卡。

## 非目标
- ❌ 不做全量语音回复（默认语音对语音触发）。
- ❌ 不做 `/voice` 命令开关（乖宝拍板简化）。
- ❌ 不改 web 渠道 TTS 链路。
- ❌ 不做「语音模式」持续状态（仅单次：语音入站 → 本次回复语音；多轮对话中乖宝后续发文字即回文字）。
- ❌ 不改微信渠道。
- ❌ 不改 `bus/queue.py` 的 Inbound/Outbound 结构（触发用 channel 内部状态，最小侵入）。

## 验收标准
- [x] 语音入站后，该 chat 下一次出站回复为语音气泡（mock 验证请求构造：`msg_type=="audio"` 且 content 含 file_key）
- [x] 文字入站回复仍为文字（不回归）
- [x] TTS 合成 / OPUS 转换 / 上传 / 发送任一失败 → 回文字原文或明确提示，不静默、不崩溃
- [x] 超长文本（超过 `max_voice_chars` 阈值）→ 只发文字，不硬转语音
- [x] `tts_service` 未注入（None）时语音入站回复仍正常（文字兜底）
- [x] 既有 text/image/audio 入站与 text/image 出站不回归（test_feishu_audio / test_feishu_images 全过）
- [x] `.venv/bin/python -m unittest discover -s tests` 全过
- [x] `git diff --check` 通过

## 相关模块
- `channels/feishu.py`（`send` / `_send_request` / `_upload_image` / `_queue_audio_message` / `_publish_image_error` 参考）
- `voice/tts/service.py`、`voice/tts/dashscope_realtime.py`（复用）
- `voice/media.py`（ffmpeg 调用参考；新增 wav→opus 转换可放这里或 feishu 内部）
- `main.py`（`tts_service` 注入）
- `bus/queue.py`（不改结构，仅参考字段）

## 实现方案
1. **触发标记**：feishu channel 新增 `self._voice_reply_pending: set[str]`（chat_id 集合）。`_queue_audio_message` 转写成功并 `_publish_text_inbound` 后 `add(chat_id)`（转写失败不标记）。
2. **send() 语音分支**：`if chat_id in self._voice_reply_pending and self.tts_service is not None` → 尝试语音回复：
   - 文本超 `max_voice_chars`（默认 300）→ 跳过语音直接发文字。
   - `await self.tts_service.synthesize(text)` → WAV bytes（TTSError 兜底）。
   - ffmpeg 转 OPUS：`-acodec libopus -ac 1 -ar 16000`（临时目录即用即删）→ opus bytes。
   - `_upload_audio_sync`：`CreateFileRequest` file_type=opus（仿 `_upload_image` 的异步包装 + 错误分类）。
   - `CreateMessageRequest` msg_type="audio" content=`{"file_key": ...}` 发送。
   - 成功 → 不再发文字；失败（任何一步）→ 回文字原文 + logger 记录，不静默。
   - `finally` 清除该 chat_id 标记（无论成败，单次消费）。
3. **音色**：复用 `tts_service`（dashscope_realtime 甘雨 + instructions，config 已配；可切 edge-tts）。
4. **长文本**：`max_voice_chars` 配置项（channel 构造参数或常量），超限只发文字——飞书语音有大小限制、长文本合成慢且无必要语音化。
5. **音频不落盘**：转换用临时目录即用即删（仿 ASR service 的 TemporaryDirectory 模式）。
6. **多轮竞态**：同一 chat 语音入站→回复期间又来一条新语音：`_voice_reply_pending` 是 set，重复 add 幂等；send 消费时若还有 pending（说明又收到语音）本次照常发，标记留给下一次（设计上可接受，任务卡记录即可）。

## 测试方式
- `.venv/bin/python -m unittest tests.test_feishu_voice_reply -v`（新增：语音入站→出站语音请求构造 / 文字入站→文字 / TTS 失败兜底文字 / 上传失败兜底文字 / 超长文本只发文字 / tts_service None 兜底 / 标记消费后清除 / 不回归）
- `.venv/bin/python -m unittest tests.test_feishu_audio tests.test_feishu_images -v`（不回归）
- `.venv/bin/python -m unittest discover -s tests`（全量回归）
- `git diff --check`、`uv run python -m compileall -q channels voice main.py`、`uv run python -c "import main"`
- 环境预检：`ffmpeg -encoders 2>/dev/null | grep -i opus`（确认 libopus 可用）

## 风险
- 真实飞书语音**发送**链路未端到端验证（需真实环境；本地到 mock 请求构造）。
- OPUS 编码依赖 ffmpeg libopus（本机 ffmpeg 已用于 ASR，大概率支持，实施前预检）。
- 飞书语音大小/时长限制（30MB 上限）——`max_voice_chars` 前置兜底 + TTS service 已有护栏。
- TTS 为付费 API（DashScope）；测试全 mock，不触发真实调用。
- 上传 audio 资源需飞书应用「上传文件」权限（`im/v1/files`，与图片权限族不同，真实环境需在开放平台确认）。
- 语音合成耗时（甘雨实时 TTS 流式，长文本更慢）——`max_voice_chars` 控制，避免拖慢回复。

## 下一步
- ⏸ 已建卡未开工，等乖宝确认后：派遣 code-master 实施 → 跑测试 → 文档同步 → commit（先问乖宝）。


---

## 实施记录（2026-08-08 code-master）

### 改动文件
- `channels/feishu.py`：`tts_service=None` + `max_voice_chars=300` 构造参数；`_voice_reply_pending: set[str]` 触发标记（`_queue_audio_message` 转写成功并投递后 `add`）；`send()` 语音分支（图片之后、text 分片之前）；`_try_send_voice_reply` / `_convert_audio_to_opus` / `_upload_audio` / `_upload_audio_sync`（`CreateFileRequest` file_type=opus）。
- `main.py`：FeishuChannel 注入 `tts_service=shared["tts_service"]`（可 None）。
- `voice/media.py`：新增 `encode_to_opus()`（复用 ffmpeg/libopus，`-acodec libopus -ac 1 -ar 16000`，临时目录即用即删）；模块 docstring 同步为 ASR+TTS 双用途。
- `tests/test_feishu_voice_reply.py`：11 个用例（mock TTS + mock 上传；OPUS 转换走真实 ffmpeg）。

### 验证（真实输出）
- `tests.test_feishu_voice_reply`：Ran 11 tests OK
- `tests.test_feishu_audio tests.test_feishu_images`：Ran 19 tests OK（不回归）
- `unittest discover -s tests`：Ran 560 tests OK
- `git diff --check`：通过
- `uv run python -m compileall -q channels voice main.py` / `uv run python -c "import main"`：通过

### 关键决策
- 标记「先取走再尝试」（而非 try/finally 清除）：满足任务卡多轮竞态约定——合成期间又来新语音时标记被重新加入，留给下一次回复；tts_service=None 时同样消费标记，避免残留。
- wav→opus 转换放 `voice/media.py`（与 `normalize_to_pcm_wav` 同款异步 ffmpeg 封装），feishu 只做 TemporaryDirectory 即用即删包装。
- 音频全程内存（`io.BytesIO` 上传），不落盘。

### 发现的范围外问题（记录，不顺手扩大）
1. **PROJECT.md 能力矩阵「语音朗读/出站语音」行 + 任务卡归档**：本会话授权范围不含 PROJECT.md，待小奈/项目负责人确认后执行。
2. **上传权限**：飞书 `im/v1/files` 需应用「上传文件」权限（与图片权限族不同），真实环境需在开放平台确认（风险节已记录，未验证）。
3. **真实飞书语音发送链路**未端到端验证（仅 mock 请求构造），与任务卡风险节一致。
4. **测试 ResourceWarning**：`tests/test_feishu_voice_reply` 因真实 asyncio 子进程（ffmpeg）在解释器退出时产生 `unclosed event loop` 警告，纯告警不影响结果（Ran OK），如追求零告警可后续将 ffmpeg 调用改为 `asyncio.to_thread(subprocess.run, ...)`。

### 待办（需小奈确认）
- [ ] 文档同步 PROJECT.md 能力矩阵（语音朗读/出站语音行）
- [ ] 归档任务卡
- [ ] commit / push（先问乖宝）

## 完成记录（2026-08-08 小奈复核）
- 验收标准 8 项全部勾选；全量 `unittest discover -s tests` Ran 560 tests OK；`git diff --check` / `compileall` / `import main` 全过。
- 乖宝已重启实例验证（收/发语音不落盘确认）；已授权 commit + push。
- 遗留（记录不阻塞归档）：① 真实飞书语音发送链路未端到端验证（仅 mock 请求构造）；② `im/v1/files` 上传权限需飞书开放平台确认；③ 测试 ResourceWarning（unclosed event loop，纯告警）；④ 后续可选优化：ffmpeg 调用改 `asyncio.to_thread(subprocess.run, ...)` 消除告警。
