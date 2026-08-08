# TASK-020-飞书语音转写（入站）

> 状态：✅ 已完成（2026-08-08 归档｜乖宝真实飞书语音端到端验收通过）
> 创建：2026-08-08 ｜ 负责人：小奈 + code-master ｜ 基线 commit：d605283（TASK-019 微信日常对话模式）

## 目标
乖宝在飞书给小奈发语音消息，小奈能听懂——语音自动转成文字进入对话，效果对齐微信语音（微信是腾讯 STT 服务端转好，飞书需自己下载+转写）。

## 背景
- 现状：飞书渠道 `_on_message` 白名单只有 `text` / `image`（`channels/feishu.py:176`），`audio` 消息直接丢弃。
- 微信对照：语音经腾讯 STT 在服务端转写，`voice_item.text` 直接带文字（`integrations/weixin_bridge/bridge.mjs`），无需本地 ASR。
- 飞书差异：开放 API 只返回 `file_key`，语音内容必须自己下载再转写——没有现成服务端转写。
- 零件现成（2026-08-08 调研确认）：
  - 入站下载：复用 `im.v1.message_resource.get`（与 `_download_image_sync` 同款资源 API，`file_key` 仅作参数不当路径）。
  - 转写：`voice/asr/service.py` `AudioTranscriptionService`（ffmpeg 归一化 + provider 转写，即用即删临时文件，已有大小/时长/并发护栏）；`main.py:99` `build_asr_service` 已实现（`provider=openai_compatible`，走硅基流动），web 渠道在用。
  - ASR 配置：复用 `asr_model`（api_key/base_url/model，可用 ASR_API_KEY 环境变量），无新增配置项。

## 范围
- `channels/feishu.py`：`_on_message` 白名单加 `"audio"`；新增 audio 消息处理路径（解析 file_key → 下载 → ASR 转写 → 转写文本作为用户消息进 Agent）；ASR 未启用/转写失败时回明确错误提示。
- `main.py`：把 `build_asr_service` 产出的 ASR service 注入 feishu channel（未启用时飞书收到语音回「未启用 ASR」提示）。
- `tests/`：新增飞书语音转写测试（mock 消息事件 + mock ASR service）。
- 文档同步：PROJECT.md 能力矩阵（新增「飞书语音转写」行）；归档任务卡。

## 非目标
- ❌ 不做飞书**出站**语音（小奈用甘雨声音回语音）——独立立项（需 OPUS 转换 + 上传 `im/v1/files` + 发 audio）。
- ❌ 不改 web 渠道现有 ASR 链路。
- ❌ 不做语音+文字组合发送。
- ❌ 不落盘音频（沿用 ASR service 临时文件即用即删模式）。
- ❌ 不改微信语音链路。

## 验收标准
- [x] 飞书 `audio` 消息不再被过滤，能下载并转写成功（mock 验证 + 乖宝真实飞书语音端到端验收 ✅）
- [x] 转写文本作为用户消息进入 Agent（渠道 feishu / 用户标识正确，mock 验证）
- [x] ASR 未启用或转写失败时回复明确错误提示，不崩溃、不静默（mock 验证）
- [x] 既有 text/image 处理不回归（test_feishu_images 9 项全过）
- [x] `.venv/bin/python -m unittest discover -s tests` 全过（549 tests OK）
- [x] `git diff --check` 通过

## 相关模块
- `channels/feishu.py`（`_on_message` / `_download_image_sync` / `_publish_text_inbound` / `_publish_image_error` 参考）
- `voice/asr/service.py`、`voice/asr/openai_compat.py`（复用）
- `main.py`（`build_asr_service` 装配注入）
- `config.py` / `config.example.json`（`asr_model` 配置，如无缺省则补示例）

## 实现方案
1. **白名单**：`_on_message` 中 `message.message_type not in ("text", "image")` → 加 `"audio"`；audio 分支解析 `content.file_key` + `message_id`，校验缺失时回错误提示。
2. **处理路径**（仿 image 的异步模式）：audio 消息走主事件循环 `_queue_audio_message`：`asyncio.to_thread` 下载（新增 `_download_audio_sync`，复用 `im.v1.message_resource.get`）→ `AudioTranscriptionService.transcribe(raw, filename=..., media_type=...)` → 成功后 `_publish_text_inbound`（文本=转写结果）；`ASRError` → 回 `⚠️ 语音转写失败：…`（不崩溃）。
3. **ASR 未启用**：feishu channel 的 `asr_service is None` 时，收到 audio 回「⚠️ 当前实例未启用语音转写（ASR）」。channel 构造签名新增可选 `asr_service` 参数（默认 None 向后兼容）。
4. **音频细节**：飞书语音下载的格式（预期 opus/m4a）交给 `AudioTranscriptionService` 内部 ffmpeg 归一化，`media_type` 从下载响应或内容嗅探（参考 `_image_ext_mime` 思路）；audio 消息是完整消息，无需图片那种「等待文字说明」批次合并。
5. **群聊**：沿用现有「群聊仅 @ 才响应」逻辑，audio 分支同样受此约束。
6. **字段确认点**：飞书 audio 事件 content 结构（预期 `{"file_key": "..."}`）以真实 SDK/事件为准，实现时核对；若 SDK 字段不同，按实际适配。✅ 已核对：audio 事件 content 确为 `{"file_key": ...}`，下载资源 type 用 `"file"`（官方文档确认 `image` 仅对应图片，`file` 对应文件/音频/视频）。

## 测试方式
- `.venv/bin/python -m unittest tests.test_feishu_audio -v`（新增：白名单放行 / 下载转写成功投递 / ASRError 回错误 / asr_service 未注入回未启用 / text-image 不回归）
- `.venv/bin/python -m unittest discover -s tests`（全量回归）
- `git diff --check`
- `uv run python -m compileall -q agent channels voice`

## 风险
- ~~飞书语音真实链路未端到端验证~~ → ✅ 2026-08-08 乖宝真实飞书语音验收通过（转写成功，重启后生效）。
- 飞书语音时长/大小限制由 ASR service 既有护栏兜底（10MB / 120s，可配置）。
- ASR 走硅基流动为付费 API；测试用 mock，不触发真实调用。
- `im:resource` 权限：下载语音资源与图片同一权限族，若飞书应用未开对应权限需在开放平台补（乖宝环境已验证可下载，权限已具备）。

## 下一步
- ✅ 已归档（2026-08-08）。

## 实现记录（2026-08-08 · code-master）
状态：✅ 已完成并归档。真实飞书语音端到端验收通过后提交。

### 改动文件
- `channels/feishu.py`：
  - `_on_message` 白名单加 `"audio"`；audio 分支仿 image 异步模式（SDK 回调立即返回），群聊沿用「仅 @ 才响应」约束（位于群聊判断之后）。
  - 新增 `_queue_audio_message`：`asyncio.to_thread(_download_audio_sync)` 下载 → `asr_service` 为 None 回「⚠️ 当前实例未启用语音转写（ASR）」→ `transcribe()` → 成功 `_publish_text_inbound`（sender_id=f"{chat_id}:{sequence}"）；`ASRError` 回「⚠️ 语音转写失败：{message}」；其它异常 `logger.exception` + 回安全提示。
  - 新增 `_download_audio_sync`：复用 `im.v1.message_resource.get`，`.type("file")`（飞书语音/音频/视频走 file 而非 image），返回 (raw_bytes, content-type)。
  - 新增 `_audio_file_meta`：file_key 派生 filename + content-type 推断扩展名；缺失/非 audio/* 兜底 `application/octet-stream`；OggS 最小嗅探。
  - `__init__` 新增可选 `asr_service=None`（默认 None 向后兼容）。
- `main.py`：FeishuChannel 装配处注入 `asr_service=shared["asr_service"]`（与 Web 渠道同源；可为 None）。
- `tests/test_feishu_audio.py`（新增 10 项，mock 风格同 test_feishu_images）。
- `PROJECT.md`：「语音输入 ASR」行更新为 Web + 飞书语音入站自动转写（TASK-020）。

### 验证（真实执行）
- `.venv/bin/python -m unittest tests.test_feishu_audio -v` → Ran 10 tests, OK
- `.venv/bin/python -m unittest tests.test_feishu_images -v` → Ran 9 tests, OK（不回归）
- `.venv/bin/python -m unittest discover -s tests` → Ran 549 tests, OK
- `uv run python -m compileall -q channels voice main.py` → OK
- `uv run python -c "import main"` → OK
- `git diff --check` → OK
- 全部 ASR 测试用 mock，未触发真实硅基流动 API。
- ✅ 端到端：2026-08-08 15:21 乖宝重启后发真实飞书语音，转写成功（小奈收到文字并正常回复）。

### 遗留问题 / 风险
- 飞书语音时长/大小限制由 ASR service 既有护栏兜底（10MB / 120s，可配置）。
- ASR 走硅基流动为付费 API；测试均 mock，不触发真实调用。
- `_publish_image_error` 现被 image/audio 复用（通用错误回执方法，命名沿用）。
