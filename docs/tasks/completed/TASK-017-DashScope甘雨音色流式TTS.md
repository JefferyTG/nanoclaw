# TASK-017：DashScope 甘雨音色流式 TTS + 录音复刻换音色

## 任务卡

- 状态：实现完成（待负责人验收）
- 负责人：小奈（项目管理）+ code-master（实现）
- 执行会话/子 Agent：code-master（已执行）
- 基线 commit：c377c12（TASK-016）
- 依赖任务：无

### 目标

1. 网页语音朗读可使用**甘雨音色**（Qwen 复刻音色）合成语音，走 **WebSocket 真流式**（边生成边出音频，不等全量）。
2. 支持**换音色**：给一段录音（10~20s 无背景音）→ 调用百炼复刻接口生成新 voice_id → 更新配置 → 新音色立即生效。
3. **不改变现有 voice/ 模块架构**：沿用 `TTSProvider`（`async synthesize(text) -> TTSResult`）与 `TextToSpeechService` 编排，流式在 provider 内部实现。
4. 默认音色 = 甘雨；edge-tts 保留，可随时切回。

### 非目标

- ❌ 不做微信真·语音条发送（iLink 协议不支持 bot 主动发 VOICE，另有结论）。
- ❌ 不做浏览器端帧级音频推送（保持现有段级增量播放，`POST /api/tts` 段切分不动）。
- ❌ 不重构 `voice/` 模块接口、不改 `agent/` `bus/` `memory/` `integrations/`。
- ❌ 不做 webui 录音上传界面（复刻能力先以模块 API + 配置切换形式交付，界面后续再说）。

### 允许修改

- `voice/tts/`（新增 provider / clone 能力，删除已废弃的 `dashscope.py`——已完成）
- `config.py`（`tts_model` 配置字段扩展，**只加不改默认值**）
- `config.json`（未跟踪文件，可加 `tts_model` 覆盖；api_key 放这里或环境变量）
- `main.py`（仅 `build_tts_service()` 增加 provider 分支，其余不动）
- `pyproject.toml`（加 `dashscope` 依赖）
- `tests/voice/`（新增测试）
- 文档：`PROJECT.md`（能力矩阵 + git 状态指针）、`docs/TTS.md`、`docs/DECISIONS.md`、`docs/ARCHITECTURE.md`（如涉及）

### 禁止修改

- `agent/`、`bus/`、`memory/`、`integrations/weixin_bridge/`、`webui/` 核心逻辑
- 现有 `voice/tts/base.py` / `service.py` / `edge.py` 的既有接口与默认行为
- 微信、飞书、提醒等其它子系统

### 上下文与约束

- **甘雨音色 voice_id**：`qwen-tts-vc-myclone-voice-20260807125201837-750c`（2026-08-07 12:52 复刻，绑定模型 `qwen3-tts-vc-realtime-2026-01-15`）
- 音色**绑定模型**，不可跨模型使用；该音色绑定 realtime（WebSocket）模型 → 合成必须走 `QwenTtsRealtime`（dashscope SDK），HTTP multimodal-generation 接口用不了这个音色（实测 400 InvalidParameter）
- 参考实现：`/Users/xx/WorkBuddy/t t s/tts-demo/app/tts.py`（`synthesize_stream_vc` WebSocket 流式、`create_voice_by_clone` 复刻、`pcm_to_wav`）
- 复刻接口：POST `customization`，model=`qwen-voice-enrollment`，action=`create`（target_model 须为合成模型，此处用 realtime 模型）
- 音色列表接口已验证可用：`qwen-voice-enrollment` + action=`list`（共 5 个 Qwen 音色）
- **API Key**：`DASHSCOPE_API_KEY`（`sk-ws-` 开头，北京区）已从 tts-demo/.env 取得；写入 config.json（被 gitignore，安全）或环境变量，支持环境变量覆盖
- 已知坑（从 tts-demo 学到的）：不要用 `asyncio.run()`（NanoClaw 是 asyncio 环境）；不要用「2s 静默超时」判完成（会砍长停顿文本尾巴）；`time.sleep(0.8)` 魔法数字要审慎处理
- 测试跑法：`.venv/bin/python -m unittest discover -s tests`
- 文档同步铁律：每干一步同步文档，不攒最后

### 验收标准

- [x] `build_tts_service` 支持 `provider=dashscope_realtime`：配好 voice_id + api_key 后启动，网页朗读用甘雨音色合成 WAV 成功（代码 + fake 单测覆盖；真实 API 待授权）
- [x] 合成走 WebSocket 流式（provider 内部流式收集），首包延迟低，不阻塞事件循环（SDK 同步调用全部放 asyncio.to_thread；流式收集在 provider 内部）
- [x] 录音复刻能力：模块级 API 输入音频 bytes → 返回新 voice_id；更新配置后新音色生效（`create_voice_by_clone` + `provider.voice_id` 每次合成读取、运行时切换立即生效）
- [x] 配置不合法（缺 key / 缺 voice_id / 参数非法）→ 优雅降级：打印警告禁用朗读，不影响其它服务（含单测）
- [x] edge-tts 仍可用：`provider=edge_tts` 行为不变（既有测试全绿）
- [x] 单元测试覆盖（fake provider / fake 网络，不真调 API）：正常合成、空文本、超时、provider 失败、音频过大、并发（test_tts_dashscope.py 18 例 + 装配测试 6 例 + api_key 注入顺序回归 1 例）
- [x] 文档同步：PROJECT.md 能力矩阵、docs/TTS.md、DECISIONS.md、ARCHITECTURE.md（MEMORY 指针属 AGENTS.md 禁止修改范围，以 DECISIONS.md 为跨会话记录，见执行交接风险）
- [x] 真实 API 受控验证：甘雨音色合成 1 次（乖宝 2026-08-07 网页朗读实测成功，效果很棒）；复刻 1 次（可选，未做）

### 必须执行的验证

```bash
git diff --check
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m compileall -q voice main.py config.py
.venv/bin/python -c "import main"
```

## 执行交接

- 状态：**已完成（2026-08-07 归档）**
- 实际改动文件：
  - 新增 `voice/tts/dashscope_realtime.py`：DashScope 甘雨音色流式 TTS Provider（`DashScopeRealtimeTTSProvider`）+ 录音复刻模块 API（`create_voice_by_clone`）+ `pcm_to_wav`；惰性导入 dashscope（模块级导入不依赖包存在）
  - 新增 `tests/voice/test_tts_dashscope.py`：18 个用例（fake realtime factory / fake post，零真实网络）
  - 修改 `voice/tts/__init__.py`：导出新 Provider / 复刻 API / 常量
  - 修改 `config.py`：`tts_model` 默认 dict 新增 `dashscope_realtime` 分支字段（只加不改，provider 默认仍 edge_tts）
  - 修改 `config.example.json`：同步 dashscope_realtime 分支示例（api_key 空值）
  - 修改 `main.py`：仅 `build_tts_service()` 增加 provider 分支 + 新装配函数 `_build_dashscope_realtime_tts_service`；edge 路径逐字未动
  - 修改 `pyproject.toml` / `uv.lock`：`uv add dashscope`（1.26.5）
  - 修改 `tests/voice/test_tts_config.py`：新增 5 个装配分支测试 + 1 个未知 provider 测试
  - 文档：本任务卡、PROJECT.md、docs/TTS.md、docs/DECISIONS.md、docs/ARCHITECTURE.md
  - 已确认无残留：`voice/tts/dashscope.py` 不存在（git 历史亦无入库记录）
- 实现摘要：
  - 合成：`QwenTtsRealtime`（WebSocket 流式）走 **commit 模式**：append_text 一次提交 → 显式 commit() 触发一次合成 → 服务端 `response.created → response.audio.delta（base64 PCM）→ response.audio.done → response.done` → 客户端 `session.finish` → 服务端 `session.finished` 并关闭连接。完成判定只依赖服务端事件（response.done / session.finished / 连接关闭），**不用静默超时判完成**（规避参考实现 2s 超时砍长停顿尾巴的坑）
  - 不阻塞事件循环：SDK 同步调用（connect/update_session/append_text/commit）全部放 `asyncio.to_thread`；SDK 后台线程回调经 `loop.call_soon_threadsafe` 桥接回 asyncio 队列；API Key 在 worker 线程内、factory 构造 QwenTtsRealtime 之前注入 `dashscope.api_key`（SDK 构造时快照）
  - 用服务端 `session.created/updated` 握手（threading.Event）替代参考实现的 `time.sleep(0.8)` 魔法数字
  - 复刻：`create_voice_by_clone(audio_bytes, api_key=...)` → POST `customization`（model=qwen-voice-enrollment, action=create, target_model=qwen3-tts-vc-realtime-2026-01-15）→ 返回新 voice_id；`post` 参数可注入 fake；Qwen 系复刻音色创建即可用
  - 换音色：`provider.voice_id` 每次合成时读取，运行时赋值新 voice_id 立即生效；持久化需改 config.json + 重启（启动期配置）
  - 默认音色即甘雨（config 默认 voice_id = qwen-tts-vc-myclone-voice-20260807125201837-750c）
- 关键决策与假设：
  1. **commit 模式替代 server_commit**：参考实现用 server_commit + 2s 静默超时判完成会砍长停顿尾巴（官方文档：server_commit 由服务端内部规则判断分段与合成时机，客户端无法确定整段何时结束）；commit 模式一次 commit = 一次响应 = response.done 确定完成。模式由 provider 内部固定为 commit，不暴露配置（防误用）
  2. 完成事件：response.done 后 best-effort 调 `session.finish`，再等 `session.finished`/连接关闭（close_grace_sec 默认 5s）；超时不阻塞返回已完整音频。总体兜底超时 overall_timeout_sec 默认 120s（TextToSpeechService 外层 wait_for 60s 先兜底）
  3. SDK 的 `connect()` 内部硬编码 5s 轮询等待，无法配置 → 未提供 connect_timeout 配置项
  4. 可注入 factory：`realtime_factory`（镜像 edge.py 的 communicate_factory）与复刻 `post`，测试零真实 API
  5. `channels/web.py` 的 `/api/tts` 固定返回 `audio/mpeg`（既有安全契约，任务未授权改动）；WAV 字节以 audio/mpeg 头返回，浏览器按容器嗅探（RIFF）通常可正常播放——作为风险记录，建议后续在 web.py 按 `TTSResult.media_type` 返回正确 Content-Type
- 验证命令与结果（全部真实执行）：
  - `git diff --check` → 通过（exit 0）
  - `.venv/bin/python -m unittest discover -s tests` → Ran 532 tests, OK
  - `.venv/bin/python -m compileall -q voice main.py config.py` → 通过
  - `.venv/bin/python -c "import main"` → OK
  - 冒烟（fake realtime）：`provider.synthesize("你好，我是甘雨。")` 返回 audio/wav、RIFF/WAVE 魔数、44 字节头 + PCM 完整
- 未验证项：
  - 复刻接口真实调用未做（可选验收项，乖宝未要求）
  - 首包延迟未量化测量（实测听感流畅，未打点）
  - 甘雨 voice_id 有效期未知（2026-08-07 复刻，后续失效再处理）
- 风险与遗留问题：
  - `channels/web.py` Content-Type 固定 audio/mpeg（见关键决策 5）
  - server_commit 模式未支持（有意的决策，commit 模式更可控；如未来需要 server_commit 需重新设计完成判定）
  - `asyncio.to_thread` 无法取消底层线程：异常/取消路径下 worker 线程可能继续运行至 SDK 5s 连接超时，靠 client.close() best-effort 清理
  - MEMORY 指针：`workspace/memory/MEMORY.md` 属 AGENTS.md 禁止修改范围，未改动；跨会话记录以 docs/DECISIONS.md 为准
- commit（仅在获授权时）：未 commit / 未 push
- 当前 `git status --short --branch`：`## main...origin/main`；已改 config.example.json/config.py/main.py/pyproject.toml/uv.lock/voice/tts/__init__.py/tests/voice/test_tts_config.py，新增 tests/voice/test_tts_dashscope.py、voice/tts/dashscope_realtime.py；未跟踪 `skills/web-render-fetch/`（任务前已有，与本任务无关）
- 建议下一步：commit（待乖宝授权）→ 可选跟进 web.py Content-Type 按 media_type 返回 → 可选 webui 录音上传界面 → 可选复刻真实验证

## 负责人验收

- [ ] 检查 diff 与授权范围
- [ ] 独立复跑关键验证
- [ ] 检查秘密/个人数据/运行产物
- [ ] 检查文档与配置一致性
- [ ] 更新 `docs/DECISIONS.md` 中相关状态
- 验收结论：待验收
- 证据与备注：
