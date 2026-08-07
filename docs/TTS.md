# Web Text-to-Speech (v1)

## Scope

Web TTS is an optional, best-effort presentation feature. The page starts with
auto-read disabled. Once the user enables it, only later live Agent replies are
spoken; user messages, thinking/tool events, control replies and history replay
are excluded. TTS never changes MessageBus, Agent or session persistence.

The first provider is `edge-tts`, an unofficial client for Microsoft Edge's
online speech service. Text selected for speech leaves the local process. No
audio or TTS request is written to workspace, session JSONL or logs.

## Incremental flow

```text
live Agent token events
  -> text segmenter
  -> POST /api/tts for one bounded segment
  -> edge-tts MP3 in memory
  -> ordered browser playback
```

The segmenter starts the first segment at an early complete sentence boundary,
then combines later short sentences into larger speech units before cutting at
strong punctuation (`。！？!?；;` and newline). Weak punctuation (`，,：:`) is
only a long-text fallback. A higher hard limit handles text with no punctuation,
preferring a safe boundary whenever possible. The `done` event flushes the
remaining text but never submits the full reply again. Synthesis may run while
the previous segment is playing; audio must remain ordered and bounded.

Before synthesis, the service removes emoji presentation characters while
preserving normal Chinese/English text and punctuation. The browser may prepare
up to two segments concurrently, but playback always follows source order.

## Lifecycle and failure boundary

- Disabling auto-read aborts the active HTTP request, stops audio, revokes the
  Blob URL and clears pending text/audio.
- Sending a new user message, changing/deleting a session or losing the
  WebSocket stops the previous reply's speech.
- A generation identifier prevents a late response from playing after cancel.
- One segment failure abandons the rest of that reply but keeps auto-read
  enabled for a later reply.
- TTS HTTP and playback failures are fully caught and do not append chat
  messages, alter the WebSocket or block the send controls.

## Server limits

`tts_model` configures provider, voice, rate, total/connect/receive timeouts,
maximum input characters, maximum MP3 bytes and concurrency. The service
accepts plain text only, performs no automatic retry and returns `audio/mpeg`
with `Cache-Control: no-store`. It uses no temporary file or media player.
Audio exists only in the server's in-memory buffer and browser Blob URLs; Blob
URLs are revoked on completion or cancellation, so no disk cleanup job is
required for this implementation.

## Acceptance

Automated tests use fake providers/Communicate streams and must cover empty and
oversize input, output cap, timeout, cancellation, concurrency, safe HTTP
errors and normal WebSocket chat after TTS failure. Browser acceptance covers
default-off state, incremental first playback, strict ordering, disable/stop,
new-message/session/disconnect cancellation, history exclusion and autoplay
rejection. Tests must not call the real Microsoft service without explicit
authorization.

## DashScope 甘雨音色流式 Provider（TASK-017）

第二 provider 为 `dashscope_realtime`：用 Qwen 复刻音色（默认即甘雨）经
DashScope SDK 的 `QwenTtsRealtime`（WebSocket）流式合成，provider 内部边生成边
收集 PCM，最终封装为 **WAV（`audio/wav`）** 字节返回。

### 配置（`tts_model`）

```json
{
  "provider": "dashscope_realtime",
  "dashscope_realtime": {
    "api_key": "",
    "voice_id": "qwen-tts-vc-myclone-voice-20260807125201837-750c",
    "model": "qwen3-tts-vc-realtime-2026-01-15",
    "sample_rate": 24000,
    "overall_timeout_sec": 120,
    "session_ready_timeout_sec": 10,
    "close_grace_sec": 5,
    "max_audio_bytes": 16777216
  }
}
```

- `api_key`：留空则读环境变量 `DASHSCOPE_API_KEY`（`sk-ws-` 开头，北京区；最高优先级，不落盘）。
- `voice_id`：默认甘雨；**音色绑定模型**（`model`），不可跨模型使用；该音色只能走
  realtime WebSocket，HTTP multimodal-generation 接口对其返回 400。
- `mode` 固定为 `commit`（provider 内部，不可配置）：一次 commit = 一次响应 =
  `response.done` 确定完成，**不使用静默超时判完成**，长停顿文本尾巴不会被打断。
- 其它超时语义：`session_ready_timeout_sec` 等服务端确认会话；`close_grace_sec`
  是 response 完成后等待服务端关闭的宽限；`overall_timeout_sec` 是 provider 内
  兜底总超时（`TextToSpeechService` 外层还有 `tts_model.timeout_sec` 的 60s
  wait_for 先兜底）。

### 录音复刻换音色

模块级 API（不阻塞事件循环，`httpx` 异步实现，`post` 参数可注入 fake 测试）：

```python
from voice.tts.dashscope_realtime import create_voice_by_clone
voice_id = await create_voice_by_clone(
    audio_bytes,          # 10~20s 无背景音（上限 60s / ≤10MB / ≥16kHz）
    api_key="sk-ws-xxx",  # 或复用 config / 环境变量
    target_model="qwen3-tts-vc-realtime-2026-01-15",
)
provider.voice_id = voice_id   # 运行时立即换音色；持久化则写 config.json 后重启
```

复刻走 `POST https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization`
（model=`qwen-voice-enrollment`，action=`create`）。Qwen 系复刻音色创建即可用
（无审核延迟）。

### 边界与已知风险

- `channels/web.py` 的 `/api/tts` 固定返回 `audio/mpeg`（既有安全契约，未授权改动）；
  WAV 字节以 `audio/mpeg` 头下发，浏览器按容器嗅探（RIFF）通常可正常播放，但建议
  后续按 `TTSResult.media_type` 返回正确 Content-Type。
- `asyncio.to_thread` 无法取消底层线程：异常/取消路径下 worker 线程可能运行至
  SDK 5s 连接超时上限，靠 `client.close()` best-effort 清理。
- 真实 DashScope API 需乖宝授权后受控验证（合成 1 次 + 复刻 1 次）。
