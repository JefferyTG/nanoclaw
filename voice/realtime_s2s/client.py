"""豆包端到端实时语音（Seeduplex 全双工）WebSocket 客户端（TASK-037）。

职责：连接 ``wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue``
（请求头 ``X-Api-Key``），负责 session 生命周期管理、事件收发与优雅关闭：

- ``connect()``：建立连接并启动后台接收任务（5xx 重试，4xx / 其它直接失败）；
- ``create_session()``：发送 ``session.create`` 并等待 ``session.created``；
- ``send_event()`` / ``iter_events()``：上行事件发送 / 下行事件消费；
- ``close_session()``：优雅关闭——先 ``session.close``，等 ``session.closed``
  回复（超时兜底）再断 WebSocket，避免服务端 ContextCanceled。

``ws_factory`` 为测试注入点：默认用 ``websockets.connect``，测试传 fake，
不真连云端。``api_key`` 只进内存与请求头，绝不落任何文档/git。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Awaitable, Callable

import websockets
from loguru import logger

WS_URL = "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue"
DEFAULT_MODEL_VERSION = "1.2.6.1"
DEFAULT_VOICE = "zh_female_vv_jupiter_bigtts"

# ws 连接工厂签名：async (url, headers) -> WebSocket 对象（测试注入 fake）
WsFactory = Callable[[str, dict], Awaitable[object]]


def _http_status(exc: Exception) -> int:
    """尽力从异常中提取 HTTP 状态码（连接失败 / InvalidStatus 等），取不到返回 -1。"""
    code = getattr(exc, "status_code", None)
    if code is None:
        response = getattr(exc, "response", None)
        code = getattr(response, "status_code", None)
    try:
        return int(code or -1)
    except (TypeError, ValueError):
        return -1


class RealtimeS2SClient:
    """豆包全双工 WebSocket 会话管理。一个实例管理一次连接内的一个会话。"""

    # 接收循环结束哨兵（放入事件队列，iter_events 据此正常返回）
    _END = object()

    def __init__(
        self,
        api_key: str,
        *,
        url: str | None = None,
        ws_factory: WsFactory | None = None,
        connect_timeout_sec: float = 15.0,
        close_timeout_sec: float = 5.0,
        max_reconnects: int = 2,
    ) -> None:
        self._api_key = api_key
        self._url = url or WS_URL
        self._ws_factory = ws_factory
        self._connect_timeout_sec = float(connect_timeout_sec)
        self._close_timeout_sec = float(close_timeout_sec)
        self._max_reconnects = max(0, int(max_reconnects))

        self._ws = None
        self._events: asyncio.Queue | None = None
        self._recv_task: asyncio.Task | None = None
        self._session_created: asyncio.Future | None = None
        self._session_closed: asyncio.Future | None = None

    # —— 连接与生命周期 ——

    @property
    def connected(self) -> bool:
        return (
            self._ws is not None
            and self._recv_task is not None
            and not self._recv_task.done()
        )

    async def connect(self) -> None:
        """建立 WebSocket 连接并启动接收任务；5xx 重试，4xx / 其它直接抛。"""
        if self.connected:
            return
        self._events = asyncio.Queue()
        ws = await self._open_with_retry()
        self._ws = ws
        self._session_created = asyncio.get_running_loop().create_future()
        self._session_closed = asyncio.get_running_loop().create_future()
        self._recv_task = asyncio.create_task(self._recv_loop())
        logger.debug(f"realtime ws 已连接：{self._url}")

    async def _open_with_retry(self):
        attempts = 0
        while True:
            try:
                if self._ws_factory is not None:
                    return await self._ws_factory(
                        self._url, {"X-Api-Key": self._api_key}
                    )
                return await websockets.connect(
                    self._url,
                    additional_headers={"X-Api-Key": self._api_key},
                    open_timeout=self._connect_timeout_sec,
                    ping_interval=None,
                )
            except Exception as exc:  # noqa: BLE001 - 连接失败统一按状态码判定
                if _http_status(exc) >= 500 and attempts < self._max_reconnects:
                    attempts += 1
                    await asyncio.sleep(0.5 * attempts)
                    continue
                raise

    async def disconnect(self) -> None:
        """断开连接：停接收任务并关闭 ws（可重复调用，幂等）。"""
        if self._recv_task is not None:
            task, self._recv_task = self._recv_task, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception as exc:  # noqa: BLE001 - 关闭失败不掩盖主流程
                logger.debug(f"realtime ws.close 异常：{exc!r}")
            logger.debug("realtime ws 已断开（disconnect）")
        self._session_created = None
        self._session_closed = None

    async def close_session(self) -> None:
        """优雅关闭会话：``session.close`` → 等 ``session.closed`` → 断 ws。

        不收到服务端 ``session.closed`` 就断开会导致服务端判定 ContextCanceled，
        因此先等回复再断；超时（``close_timeout_sec``）也照常断开兜底。
        """
        if self._ws is None:
            return
        logger.debug("realtime close_session：发送 session.close")
        try:
            await self.send_event(
                {"type": "session.close", "event_id": f"evt_{uuid.uuid4().hex[:8]}"}
            )
            if self._session_closed is not None:
                await asyncio.wait_for(
                    asyncio.shield(self._session_closed), self._close_timeout_sec
                )
            logger.debug("realtime close_session：已收到 session.closed")
        except asyncio.TimeoutError:
            logger.warning("realtime session.close 未收到 session.closed，按超时断开")
        except Exception as exc:  # noqa: BLE001 - 关闭失败不阻断断开
            logger.warning(f"realtime session.close 发送失败：{exc}")
        finally:
            await self.disconnect()

    # —— 事件收发 ——

    async def send_event(self, event: dict) -> None:
        """上行发送一个 JSON 事件（如 input_audio_buffer.append）。"""
        if self._ws is None:
            raise ConnectionError(
                f"realtime WebSocket 未连接（_recv_task done={self._recv_task is not None and self._recv_task.done()}）"
            )
        await self._ws.send(json.dumps(event, ensure_ascii=False))

    async def iter_events(self, poll_interval: float = 1.0):
        """下行事件消费：依次产出服务端事件；连接结束（哨兵）时正常返回。

        ``poll_interval`` 为轮询心跳间隔：队列空闲超时产出 ``None``（供调用方
        做静默超时/保活检查，不结束迭代）。用 ``wait_for`` 包 ``queue.get``
        而非 ``wait_for(anext(...))``——超时取消前者不伤队列，取消后者会
        连带关闭 async generator（TASK-037 冒烟发现：对话 1 秒即退的根因）。
        """
        while True:
            try:
                event = await asyncio.wait_for(self._events.get(), poll_interval)
            except asyncio.TimeoutError:
                yield None
                continue
            if event is self._END:
                return
            yield event

    # —— session 创建 ——

    async def create_session(
        self,
        *,
        instructions: str = "",
        voice_type: str | None = None,
        tools: list | None = None,
        model: str | None = None,
        input_sample_rate: int = 16000,
        output_sample_rate: int = 24000,
        enable_websearch: bool = False,
        event_timeout_sec: float = 10.0,
    ) -> dict:
        """发送 ``session.create`` 并等待 ``session.created``，返回该事件。"""
        # payload 对齐官方 python3.7_duplex_demo（2026-08-11 冒烟确认）：
        # - audio.input/output.format 为对象 {type, rate}（字符串不被识别，
        #   服务端回退默认 OGG-Opus → 下行爆音）；
        # - 输出格式 pcm_s16le（24k/16bit）由 format.type 指定；
        # - voice 在 audio.output 下；
        # - extension 在事件顶层（asr/tts/dialog 三块透传）。
        audio_input = {
            "format": {"type": "pcm", "rate": int(input_sample_rate)},
        }
        audio_output = {
            "format": {"type": "pcm_s16le", "rate": int(output_sample_rate)},
            "voice": voice_type or DEFAULT_VOICE,
        }
        # extension.dialog.extra 对齐官方 web/py demo（TASK-038 真机对比）：
        # - enable_loudness_norm=true：2.0 版本输出音频响度均衡（官方 demo 开启，
        #   疑似影响服务端判停/回声识别行为——py demo 带此参数外放不掐断，我们
        #   空 extra 被掐；官网文档：true 打开响度均衡，默认 false）；
        # - enable_music=false：关闭唱歌能力（官方 demo 默认值）；
        # - audit_response：内容审核不通过时的回复话术（官方 demo 携带）。
        extension: dict = {
            "asr": {"extra": {}},
            "tts": {"extra": {}},
            "dialog": {
                "extra": {
                    "enable_loudness_norm": True,
                    "enable_music": False,
                    "audit_response": "抱歉，这个问题我无法回答，你可以换个其他话题，我会尽力为你提供帮助。",
                }
            },
        }
        if enable_websearch:
            # 位置待实测确认（官方 demo 未开联网；POC 默认关）
            extension["dialog"]["extra"]["enable_volc_websearch"] = True
        session = {
            "id": str(uuid.uuid4()),  # 客户端会话 id（官方 demo 携带）
            "model": model or DEFAULT_MODEL_VERSION,
            "instructions": instructions or "",
            "audio": {"input": audio_input, "output": audio_output},
            "tools": list(tools or []),
        }
        logger.debug(f"realtime 发送 session.create：model={session['model']} "
                    f"voice={session['audio']['output']['voice']} "
                    f"tools={len(session['tools'])}")
        await self.send_event(
            {
                "type": "session.create",
                "event_id": f"evt_{uuid.uuid4().hex[:8]}",
                "session": session,
                "extension": extension,
            }
        )
        created = await asyncio.wait_for(self._session_created, event_timeout_sec)
        logger.debug(f"realtime 收到 session.created：id={created.get('session', {}).get('id')}")
        return created

    # —— 内部：接收循环 ——

    async def _recv_loop(self) -> None:
        ws = self._ws
        try:
            async for raw in ws:
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                etype = event.get("type")
                if etype == "session.created" and self._session_created is not None:
                    if not self._session_created.done():
                        self._session_created.set_result(event)
                elif etype == "session.closed" and self._session_closed is not None:
                    if not self._session_closed.done():
                        self._session_closed.set_result(event)
                try:
                    self._events.put_nowait(event)
                except asyncio.QueueFull:
                    pass  # ponytail: 事件队列满丢事件（重负载），延迟敏感时改背压
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 连接异常终止：以哨兵通知消费方结束
            logger.warning(f"realtime 接收循环异常终止（连接丢失）：{exc!r}")
        finally:
            try:
                self._events.put_nowait(self._END)
            except (AttributeError, asyncio.QueueFull):
                pass
            logger.debug("realtime 接收循环结束（哨兵已投递）")
