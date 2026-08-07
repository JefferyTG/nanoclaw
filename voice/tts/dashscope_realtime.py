"""DashScope 甘雨音色流式 TTS Provider（QwenTtsRealtime WebSocket）。

- **合成**：走 dashscope SDK 的 ``QwenTtsRealtime``（WebSocket 流式），
  provider 内部边生成边收集 PCM，最终封装为 WAV（``audio/wav``）字节返回。
- **完成判定**：采用 **commit 模式**（非参考实现里的 server_commit）：
  ``append_text`` 一次提交完整文本 → 显式 ``commit()`` 触发一次合成 → 服务端
  按序返回 ``response.created → response.audio.delta（base64 PCM）→
  response.audio.done → response.done`` → 客户端调 ``session.finish`` →
  服务端回 ``session.finished`` 并关闭连接。完成只依赖服务端事件（
  ``response.done`` / ``session.finished`` / 连接关闭），**不使用静默超时判完成**，
  因此不会砍掉长停顿文本的尾巴（参考实现 2s 静默超时的已知坑）。
- **不阻塞事件循环**：SDK 的 ``connect/update_session/append_text/commit`` 全是
  同步阻塞调用，统一放 ``asyncio.to_thread``；SDK 后台线程的回调经
  ``loop.call_soon_threadsafe`` 桥接回 asyncio 队列。
- **可测性**：沿用 edge.py 的可注入 factory 模式（``realtime_factory`` 注入点），
  测试注入 fake，不真连 WebSocket、不调真实 API。

- **复刻换音色**：模块级 ``create_voice_by_clone`` 上传录音（10~20s 无背景音）
  → POST ``customization`` 接口（model=qwen-voice-enrollment，action=create，
  target_model 为合成模型）→ 返回新 voice_id。Qwen 系复刻音色创建即可用
  （无审核延迟），把新 voice_id 赋给 provider（或写入配置）后立即生效。
"""

import asyncio
import base64
import json
import struct
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from voice.tts.base import TTSError, TTSProvider, TTSResult

# 甘雨音色绑定模型（音色不可跨模型使用；该音色只能走 realtime WebSocket，
# HTTP multimodal-generation 接口对其返回 400）。
DEFAULT_MODEL = "qwen3-tts-vc-realtime-2026-01-15"
# 默认音色即甘雨（2026-08-07 复刻，绑定 DEFAULT_MODEL）。
DEFAULT_VOICE_ID = "qwen-tts-vc-myclone-voice-20260807125201837-750c"
DEFAULT_SAMPLE_RATE = 24000  # qwen3-tts-vc-realtime 固定 24kHz 单声道 16bit PCM
DEFAULT_LANGUAGE_TYPE = "Chinese"

# 录音复刻（Voice Cloning）接口。model=qwen-voice-enrollment，action=create。
CUSTOMIZATION_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"
)

# commit 模式下本次响应的结束事件：响应（本次 commit 的全部音频）已完成。
_RESPONSE_DONE_EVENTS = frozenset({"response.done", "response.completed"})
# 服务端已按 session.finish 清理会话。
_SESSION_FINISHED_EVENTS = frozenset({"session.finished"})


class VoiceCloneError(RuntimeError):
    """录音复刻接口失败（HTTP 非 200 / 响应异常 / 网络错误）。"""


def pcm_to_wav(
    pcm: bytes,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    channels: int = 1,
    bits: int = 16,
) -> bytes:
    """给裸 PCM 加 44 字节 RIFF/WAVE 头，返回完整 WAV。"""
    if not isinstance(pcm, (bytes, bytearray)) or not pcm:
        raise ValueError("pcm_to_wav 需要非空 PCM 字节")
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    data_size = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        b"data",
        data_size,
    )
    return header + bytes(pcm)


def _default_realtime_factory(*, model: str, callback: Any) -> Any:
    """默认构造真实的 QwenTtsRealtime（惰性导入 dashscope，便于模块级导入）。"""
    from dashscope.audio.qwen_tts_realtime.qwen_tts_realtime import (
        QwenTtsRealtime,
    )

    return QwenTtsRealtime(model=model, callback=callback)


class _DashScopeRealtimeCallback:
    """把 SDK 后台线程的事件桥接进 asyncio 队列（threadsafe）。

    ``session_ready`` 是一个 threading.Event，worker 线程用它等待服务端确认
    会话配置（替代参考实现里的 ``time.sleep(0.8)`` 魔法数字）。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        self._loop = loop
        self._queue = queue
        self.session_ready = threading.Event()

    def _push(self, item) -> None:
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:
            # 事件循环已关闭（测试/进程退出），丢弃即可，回调内绝不能抛异常。
            pass

    def on_open(self) -> None:
        pass

    def on_close(self, close_status_code, close_msg) -> None:
        self._push(("close", close_msg))

    def on_event(self, message) -> None:
        try:
            if isinstance(message, str):
                message = json.loads(message)
            if not isinstance(message, dict):
                return
            mtype = message.get("type")
            if mtype in ("session.created", "session.updated"):
                self.session_ready.set()
            elif mtype == "response.audio.delta":
                delta = message.get("delta")
                if delta:
                    self._push(("data", base64.b64decode(delta)))
            elif mtype in _RESPONSE_DONE_EVENTS:
                self._push(("response_done", None))
            elif mtype in _SESSION_FINISHED_EVENTS:
                self._push(("finished", None))
            elif mtype == "error":
                self._push(("error", str(message)[:300]))
        except Exception:
            # SDK 回调线程内绝不能抛异常（会导致连接线程异常退出）。
            pass


class DashScopeRealtimeTTSProvider(TTSProvider):
    """用甘雨等 Qwen 复刻音色合成语音（WebSocket 流式收集，WAV 输出）。

    - ``voice_id`` 每次合成时读取，运行时直接改 ``provider.voice_id`` 即可
      立即换音色（复刻接口返回的新 voice_id 赋值即生效）。
    - ``realtime_factory`` 为可注入 factory（同 edge.py 的 communicate_factory），
      测试用它注入 fake，不真调 API。
    """

    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str,
        model: str = DEFAULT_MODEL,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        language_type: str = DEFAULT_LANGUAGE_TYPE,
        session_ready_timeout_sec: float = 10.0,
        close_grace_sec: float = 5.0,
        overall_timeout_sec: float = 120.0,
        max_audio_bytes: int = 16 * 1024 * 1024,
        realtime_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must not be empty")
        if not isinstance(voice_id, str) or not voice_id.strip():
            raise ValueError("voice_id must not be empty")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must not be empty")
        if type(sample_rate) is not int or sample_rate <= 0:
            raise ValueError("sample_rate must be a positive integer")
        if not all(
            isinstance(value, (int, float)) and value > 0
            for value in (
                session_ready_timeout_sec,
                close_grace_sec,
                overall_timeout_sec,
            )
        ):
            raise ValueError("timeouts must be positive")
        if type(max_audio_bytes) is not int or max_audio_bytes <= 0:
            raise ValueError("max_audio_bytes must be a positive integer")

        self.api_key = api_key.strip()
        self.voice_id = voice_id.strip()
        self.model = model.strip()
        self.sample_rate = sample_rate
        self.language_type = language_type
        self.session_ready_timeout_sec = float(session_ready_timeout_sec)
        self.close_grace_sec = float(close_grace_sec)
        self.overall_timeout_sec = float(overall_timeout_sec)
        self.max_audio_bytes = max_audio_bytes
        self._realtime_factory = realtime_factory or _default_realtime_factory

    async def synthesize(self, text: str) -> TTSResult:
        """WebSocket 流式合成完整文本，返回 WAV 字节（audio/wav）。"""
        if not isinstance(text, str) or not text.strip():
            raise TTSError("input_empty", "语音合成文本为空。")

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        callback = _DashScopeRealtimeCallback(loop, queue)
        holder: dict[str, Any] = {}
        voice_id = self.voice_id

        def run_sync() -> None:
            """在 worker 线程里完成 SDK 的同步 WebSocket 流程。"""
            try:
                # SDK 在构造 QwenTtsRealtime 时读取 dashscope.api_key（构造时快照
                # 进 self.apikey），因此必须在 factory 构造前注入。
                import dashscope

                dashscope.api_key = self.api_key
                client = self._realtime_factory(model=self.model, callback=callback)
                holder["client"] = client
                client.connect()  # 阻塞至连接成功（SDK 内部超时上限 5s）
                client.update_session(
                    voice=voice_id,
                    mode="commit",
                    sample_rate=self.sample_rate,
                    language_type=self.language_type,
                )
                # 用服务端 session.created/updated 握手替代魔法 sleep(0.8)：
                # 确认会话就绪后再 append，避免 append 过早导致合成失败。
                if not callback.session_ready.wait(
                    timeout=self.session_ready_timeout_sec
                ):
                    raise TimeoutError("服务端未确认会话配置")
                client.append_text(text)
                client.commit()  # commit 模式：显式触发一次合成，完成事件确定
            except Exception as exc:
                loop.call_soon_threadsafe(
                    queue.put_nowait, ("error", str(exc)[:300])
                )

        worker = asyncio.create_task(asyncio.to_thread(run_sync))
        audio = bytearray()
        response_done = False
        deadline = loop.time() + self.overall_timeout_sec
        try:
            while True:
                if response_done:
                    # 音频已完整：只需等待服务端 session.finished / 关闭连接。
                    timeout = self.close_grace_sec
                else:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise TTSError("timeout", "语音合成超时。")
                    timeout = remaining
                try:
                    kind, payload = await asyncio.wait_for(
                        queue.get(), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    if response_done:
                        # 服务端未及时关闭会话，不阻塞返回已完整音频。
                        break
                    continue
                if kind == "data":
                    audio.extend(payload)
                    if len(audio) > self.max_audio_bytes:
                        raise TTSError(
                            "audio_too_large", "语音合成音频超过允许大小。"
                        )
                elif kind == "response_done":
                    response_done = True
                    client = holder.get("client")
                    if client is not None:
                        try:
                            client.finish()  # 通知服务端结束会话（best-effort）
                        except Exception:
                            pass
                elif kind in ("finished", "close"):
                    break
                elif kind == "error":
                    raise TTSError(
                        "provider_failed", "语音合成服务暂时不可用。"
                    )
        except asyncio.CancelledError:
            raise
        finally:
            worker.cancel()
            client = holder.get("client")
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

        if not audio:
            raise TTSError("empty_audio", "语音合成服务未返回音频。")
        return TTSResult(
            audio=pcm_to_wav(bytes(audio), sample_rate=self.sample_rate),
            media_type="audio/wav",
        )


async def _default_post(
    url: str, *, json: dict, headers: dict, timeout_sec: float
) -> Any:
    """默认 HTTP POST 实现（httpx，异步不阻塞事件循环）。"""
    import httpx

    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        return await client.post(url, json=json, headers=headers)


async def create_voice_by_clone(
    audio_bytes: bytes,
    *,
    api_key: str,
    target_model: str = DEFAULT_MODEL,
    prefix: str = "myclone",
    timeout_sec: float = 120.0,
    post: Callable[..., Awaitable[Any]] | None = None,
) -> str:
    """用录音样本复刻音色，返回新 voice_id（音色绑定 ``target_model``）。

    音频要求：10~20s 无背景音（接口上限 60s / ≤10MB / 采样率≥16kHz）。
    走 POST ``customization``（model=qwen-voice-enrollment，action=create）。
    Qwen 系复刻音色创建即可用（无审核延迟），拿到 voice_id 后即可合成。
    ``post`` 为可注入的异步 HTTP 实现（测试注入 fake，不真调网络）。
    """
    if not isinstance(audio_bytes, (bytes, bytearray)) or not audio_bytes:
        raise ValueError("复刻音色需要非空音频字节")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("复刻音色需要 api_key")

    data_uri = (
        "data:audio/mpeg;base64,"
        + base64.b64encode(bytes(audio_bytes)).decode("ascii")
    )
    payload = {
        "model": "qwen-voice-enrollment",
        "input": {
            "action": "create",
            "target_model": target_model,
            "preferred_name": prefix,
            "audio": {"data": data_uri},
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
    }
    poster = post or _default_post
    try:
        resp = await poster(
            CUSTOMIZATION_URL, json=payload, headers=headers, timeout_sec=timeout_sec
        )
    except Exception as exc:
        raise VoiceCloneError(f"调用复刻接口失败：{exc}") from exc

    status = getattr(resp, "status_code", None)
    if status != 200:
        detail = getattr(resp, "text", "") or ""
        raise VoiceCloneError(
            f"创建复刻音色失败 HTTP {status}: {str(detail)[:300]}"
        )
    try:
        data = resp.json()
    except Exception as exc:
        raise VoiceCloneError(f"创建复刻音色失败：响应解析失败（{exc}）") from exc
    out = data.get("output") or {}
    voice_id = out.get("voice") or out.get("voice_id")
    if not voice_id:
        raise VoiceCloneError("创建复刻音色失败：响应未包含 voice_id")
    return str(voice_id)
