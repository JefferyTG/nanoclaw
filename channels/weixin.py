"""WeChat iLink JSONL bridge channel.

The Node bridge owns every WeChat credential and protocol cursor.  This
adapter deliberately only sees stable account/user identifiers, messages, and
controlled temporary image files.  It is usable without the composition root
so its process boundary can be tested entirely with a fake subprocess.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import os
import re
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from bus.queue import FileRef, InboundMessage, OutboundMessage
from channels.base import Channel

logger = logging.getLogger("nanoclaw.weixin")

_PROTOCOL_VERSION = 1
_MAX_LINE_BYTES = 1024 * 1024
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_FILE_BYTES = 50 * 1024 * 1024
_MAX_PENDING_STATE_BYTES = 8 * 1024 * 1024
def _human_file_size(num_bytes: int) -> str:
    """把字节数格式化为人类可读大小，如 1.2MB / 345B / 8KB。"""
    try:
        size = float(max(0, int(num_bytes or 0)))
    except (TypeError, ValueError):
        size = 0.0
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            break
        size /= 1024
    if unit == "B":
        return f"{int(size)}B"
    return f"{size:.1f}{unit}" if size < 10 else f"{size:.0f}{unit}"


_CHANNEL_ERROR_CODES = frozenset({
    "api_rejected",
    "cancelled",
    "context_missing",
    "invalid_request",
    "media_invalid",
    "network_error",
    "protocol_error",
    "timeout",
    "unsupported_message",
    "verification_required",
})
_CHANNEL_ERROR_REASONS = frozenset({
    "invalid_json_response",
    "response_too_large",
    "invalid_acceptance",
    "invalid_messages",
    "provider_protocol_error",
})
_IMAGE_MIMES = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
    "image/webp": "webp", "image/bmp": "bmp",
}
_SECRET_RE = re.compile(
    r'(?i)(["\']?(?:bot_token|token|secret|authorization|cookie)["\']?\s*[=:]\s*)["\']?[^\s,;"\']+'
)


@dataclass
class _PendingMessageBatch:
    """Messages from one peer awaiting the merge window before dispatch.

    Texts and images are merged together: each inbound text is appended to
    ``texts`` (joined with newlines on flush) and every image goes to
    ``images``; the batch is flushed when the window expires or when the
    number of merged messages reaches the merge limit.
    """

    account_id: str
    user_id: str
    target: str
    session_key: str
    sequence: int = 0
    texts: list = field(default_factory=list)
    images: list = field(default_factory=list)
    files: list = field(default_factory=list)
    delivery_ids: set[str] = field(default_factory=set)
    deadline_ms: int = 0
    timer: asyncio.Task | None = None


class WeixinBridgeError(RuntimeError):
    """A structured bridge failure safe to present to the local scheduler."""

    def __init__(self, code: str, message: str = "bridge request failed", *, retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class _UnsupportedInbound(ValueError):
    """A syntactically valid delivery V1 intentionally declines to consume."""


class _ReminderCommandError(RuntimeError):
    """A binding callback failed without exposing its error to the bridge."""


class _TypingActivity:
    """Opaque local handoff for one Agent turn's Weixin typing activity.

    The object intentionally contains only a stable target and a random
    activity identifier.  The Bridge, not Python, owns the typing ticket and
    context token.  ``close`` is synchronous so cleanup can be called from
    cancellation/finally paths without adding another await point.
    """

    __slots__ = ("channel", "target", "activity_id", "closed", "started")

    def __init__(self, channel: "WeixinChannel", target: str) -> None:
        self.channel = channel
        self.target = target
        self.activity_id = uuid.uuid4().hex
        self.closed = False
        self.started = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.channel._release_typing_activity(self)


def encode_weixin_target(account_id: str, user_id: str) -> str:
    """Return a stable reversible target without delimiter-collision hazards."""
    if not isinstance(account_id, str) or not isinstance(user_id, str) or not account_id or not user_id:
        raise ValueError("account_id and user_id must be non-empty strings")
    raw = json.dumps([account_id, user_id], ensure_ascii=False, separators=(",", ":")).encode()
    return "wx1." + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_weixin_target(target: str) -> tuple[str, str]:
    """Decode a target produced by :func:`encode_weixin_target`."""
    if not isinstance(target, str) or not target.startswith("wx1."):
        raise ValueError("invalid Weixin target")
    encoded = target[4:]
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        account_id, user_id = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Weixin target") from exc
    if not isinstance(account_id, str) or not isinstance(user_id, str) or not account_id or not user_id:
        raise ValueError("invalid Weixin target")
    return account_id, user_id


# Short aliases make the stable target contract convenient for future reminder code.
make_target = encode_weixin_target
parse_target = decode_weixin_target


class WeixinChannel(Channel):
    """Async process adapter for ``integrations/weixin_bridge`` IPC v1."""

    def __init__(
        self,
        name: str = "weixin",
        bus=None,
        *,
        bridge_command: Sequence[str] | None = None,
        state_dir: str | os.PathLike[str] | None = None,
        allowed_user_ids: Sequence[str] | None = None,
        image_store=None,
        file_store=None,
        request_timeout_sec: float = 30.0,
        login_timeout_sec: float = 480.0,
        inbound_ack_timeout_sec: float = 30.0,
        stop_timeout_sec: float = 10.0,
        max_ipc_line_bytes: int = _MAX_LINE_BYTES,
        max_inbound_image_bytes: int = _MAX_IMAGE_BYTES,
        max_inbound_file_bytes: int = _MAX_FILE_BYTES,
        max_outbound_image_bytes: int = _MAX_IMAGE_BYTES,
        restart_backoff_sec: float = 1.0,
        merge_window_sec: float | None = None,
        merge_max_messages: int = 10,
        image_merge_window_sec: float | None = None,
        auto_login: bool = False,
        bind_callback=None,
        unbind_callback=None,
        suspend_callback=None,
    ) -> None:
        super().__init__(name=name, bus=bus)
        if not bridge_command:
            raise ValueError("weixin bridge_command is required")
        self.bridge_command = tuple(str(part) for part in bridge_command)
        self.state_dir = Path(state_dir).resolve() if state_dir else None
        self.allowed_user_ids = frozenset(str(item) for item in (allowed_user_ids or ()))
        self.image_store = image_store
        self.file_store = file_store
        self.command_timeout_sec = max(0.1, float(request_timeout_sec))
        self.login_timeout_sec = max(0.1, float(login_timeout_sec))
        self.inbound_ack_timeout_sec = max(0.1, float(inbound_ack_timeout_sec))
        self.stop_timeout_sec = max(0.1, float(stop_timeout_sec))
        self.max_ipc_line_bytes = max(1024, int(max_ipc_line_bytes))
        self.max_inbound_image_bytes = max(1, int(max_inbound_image_bytes))
        self.max_inbound_file_bytes = max(1, int(max_inbound_file_bytes))
        self.max_outbound_image_bytes = max(1, int(max_outbound_image_bytes))
        self.restart_backoff_sec = max(0.0, float(restart_backoff_sec))
        try:
            if merge_window_sec is not None:
                merge_window = float(merge_window_sec)
            elif image_merge_window_sec is not None:
                merge_window = float(image_merge_window_sec)
            else:
                merge_window = 8.0
        except (TypeError, ValueError):
            merge_window = 8.0
        self.merge_window_sec = max(0.0, min(60.0, merge_window))
        try:
            merge_max = int(merge_max_messages)
        except (TypeError, ValueError):
            merge_max = 10
        self.merge_max_messages = max(1, merge_max)
        self.auto_login = auto_login
        self._process = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._delivery_events: dict[str, dict[str, Any]] = {}
        self._background: set[asyncio.Task] = set()
        self._request_number = 0
        self._write_lock = asyncio.Lock()
        self._stopping = False
        self._started = False
        self._supervisor_task: asyncio.Task | None = None
        self._pending_message_batches: dict[tuple[str, str], _PendingMessageBatch] = {}
        self._inbound_lock = asyncio.Lock()
        self._pending_batches_restored = False
        # Weixin-native typing is an optional, in-process lifecycle.  These
        # handles never contain provider secrets; the Bridge owns all ticket
        # and context state and performs the actual getconfig/sendtyping calls.
        self.typing_start_delay_sec = 0.25
        self._typing_activities: dict[str, dict[str, _TypingActivity]] = {}
        self._typing_tasks: set[asyncio.Task] = set()
        # 多会话状态：target -> {"seq": 已建会话数, "current": 当前活动序号}。
        # key 用 target（含 account_id），多账号天然隔离；与飞书 /new 机制对称。
        self._sessions: dict[str, dict] = {}
        # ReminderService owns authorization and persistence.  This adapter
        # only recognizes deterministic private-message commands and forwards
        # the stable, credential-free target identity.
        self._bind_callback = bind_callback
        self._unbind_callback = unbind_callback
        self._suspend_callback = suspend_callback

    @property
    def image_merge_window_sec(self) -> float:
        """兼容别名：旧调用方读写 ``image_merge_window_sec`` 等价于读写 ``merge_window_sec``。"""
        return self.merge_window_sec

    @image_merge_window_sec.setter
    def image_merge_window_sec(self, value: float) -> None:
        try:
            merge_window = float(value)
        except (TypeError, ValueError):
            merge_window = 8.0
        self.merge_window_sec = max(0.0, min(60.0, merge_window))

    def target(self, account_id: str, user_id: str) -> str:
        return encode_weixin_target(account_id, user_id)

    @staticmethod
    def parse_target(target: str) -> tuple[str, str]:
        return decode_weixin_target(target)

    def begin_activity(self, chat_id: str):
        """Begin a best-effort native typing activity for one Agent turn.

        This method is deliberately synchronous and cheap: the actual IPC is
        scheduled in the background so a broken Bridge can never delay model
        execution.  The short delay avoids a visible typing flash for quick
        replies.
        """
        if self._stopping:
            return None
        try:
            _account_id, user_id = decode_weixin_target(chat_id)
        except ValueError:
            return None
        if not self._allowed(user_id):
            return None
        activity = _TypingActivity(self, chat_id)
        self._typing_activities.setdefault(chat_id, {})[activity.activity_id] = activity
        self._track_typing_task(
            asyncio.create_task(
                self._start_typing_after_delay(activity),
                name="weixin-typing-start",
            )
        )
        return activity

    async def _start_typing_after_delay(self, activity: _TypingActivity) -> None:
        try:
            if self.typing_start_delay_sec:
                await asyncio.sleep(self.typing_start_delay_sec)
            if self._stopping or activity.closed:
                return
            activity.started = True
            await self._request_typing("typing_start", activity)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - typing is never answer-critical
            if not self._stopping:
                logger.warning(
                    "Weixin typing start failed: %s",
                    getattr(exc, "code", "unavailable"),
                )

    def _release_typing_activity(self, activity: _TypingActivity) -> None:
        activities = self._typing_activities.get(activity.target)
        if activities is None or activities.pop(activity.activity_id, None) is None:
            return
        if not activities:
            self._typing_activities.pop(activity.target, None)
        # If the short delay has not elapsed, no native state was started and
        # there is nothing to cancel.  This is the no-flicker fast-reply path.
        # Otherwise every activity gets its own stop.  The Bridge tracks all
        # activity ids and only cancels native typing after the last one ends.
        if not activity.started:
            return
        self._schedule_typing_request("typing_stop", activity)

    def _track_typing_task(self, task: asyncio.Task) -> None:
        self._typing_tasks.add(task)
        task.add_done_callback(self._typing_tasks.discard)

    def _schedule_typing_request(self, method: str, activity: _TypingActivity) -> None:
        if self._stopping:
            return
        self._track_typing_task(
            asyncio.create_task(
                self._request_typing(method, activity),
                name=f"weixin-{method}",
            )
        )

    async def _request_typing(self, method: str, activity: _TypingActivity) -> None:
        try:
            account_id, user_id = decode_weixin_target(activity.target)
            await self._request(
                method,
                {
                    "account_id": account_id,
                    "user_id": user_id,
                    "activity_id": activity.activity_id,
                },
                timeout=self.command_timeout_sec,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - cleanup/status is best-effort
            if method == "typing_start" and not self._stopping:
                logger.warning(
                    "Weixin typing request failed: %s",
                    getattr(exc, "code", "unavailable"),
                )

    async def _drain_typing_tasks(self) -> None:
        tasks = list(self._typing_tasks)
        if not tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=self.stop_timeout_sec,
            )
        except asyncio.TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _allowed(self, user_id: str) -> bool:
        return "*" in self.allowed_user_ids or user_id in self.allowed_user_ids

    async def start(self) -> None:
        await self._ensure_process()
        await self._request("hello", {}, timeout=self.command_timeout_sec)
        if self.auto_login:
            await self.login()
        try:
            await self._restore_pending_message_batches()
        except Exception as exc:
            raise WeixinBridgeError(
                "state_restore_failed",
                "unable to restore pending Weixin messages",
                retryable=True,
            ) from exc
        await self.start_polling()

    async def login(self, force: bool = False) -> dict[str, Any]:
        return await self._request(
            "login",
            {"force": bool(force), "timeout_ms": int(self.login_timeout_sec * 1000)},
            timeout=self.login_timeout_sec,
        )

    async def start_polling(self) -> dict[str, Any]:
        result = await self._request("start", {}, timeout=self.command_timeout_sec)
        self._started = True
        return result

    async def send(self, message: OutboundMessage):
        from reminders.models import DeliveryResult

        try:
            account_id, user_id = decode_weixin_target(message.chat_id)
        except ValueError:
            return DeliveryResult(False, code="invalid_target", message="invalid Weixin target")
        if not self._allowed(user_id):
            return DeliveryResult(False, code="access_denied", message="recipient is not allowed")
        correlation_id = message.correlation_id or f"wx-{uuid.uuid4().hex}"
        try:
            if message.images:
                last = None
                for index, image in enumerate(message.images):
                    result = await self._send_image(
                        account_id, user_id, image, message.content if index == 0 else "",
                        f"{correlation_id}:image:{index}",
                    )
                    last = result
                    if not result.success:
                        return result
                # iLink image messages carry their caption.  Sending the text
                # again would duplicate an Agent reply for every image turn.
                return last
            result = await self._request(
                "send_text",
                {"account_id": account_id, "user_id": user_id, "text": message.content or "", "correlation_id": correlation_id},
            )
            return self._delivery_from_result(self._delivery_events.pop(correlation_id, result), correlation_id)
        except asyncio.CancelledError:
            raise
        except WeixinBridgeError as exc:
            self._delivery_events.pop(correlation_id, None)
            return DeliveryResult(False, retryable=exc.retryable, code=exc.code, message=str(exc))
        except Exception as exc:  # the sender must not kill Gateway's dispatcher
            self._delivery_events.pop(correlation_id, None)
            logger.exception("Weixin send failed: %s", exc)
            return DeliveryResult(False, retryable=True, code="bridge_error", message="Weixin bridge unavailable")

    async def _send_image(self, account_id, user_id, image, caption, correlation_id):
        from reminders.models import DeliveryResult

        path = getattr(image, "path", None)
        if not path or not os.path.isfile(path):
            return DeliveryResult(False, code="missing_image", message="outbound image is unavailable")
        try:
            size = os.path.getsize(path)
        except OSError:
            return DeliveryResult(False, code="missing_image", message="outbound image is unavailable")
        if size <= 0 or size > self.max_outbound_image_bytes:
            return DeliveryResult(False, code="invalid_image", message="outbound image size is invalid")
        try:
            staged_path = self._stage_outbound_image(path, getattr(image, "mime", None))
        except (OSError, ValueError) as exc:
            return DeliveryResult(False, code="invalid_image", message="unable to stage outbound image")
        try:
            result = await self._request("send_image", {
                "account_id": account_id, "user_id": user_id, "file_path": staged_path,
                "caption": caption or "", "correlation_id": correlation_id,
            })
        except Exception:
            self._delivery_events.pop(correlation_id, None)
            raise
        finally:
            try:
                os.unlink(staged_path)
            except FileNotFoundError:
                pass
        return self._delivery_from_result(self._delivery_events.pop(correlation_id, result), correlation_id)

    def _stage_outbound_image(self, source: str, mime: str | None) -> str:
        """Copy a checked ImageStore file into the bridge's one permitted root."""
        if self.state_dir is None:
            raise ValueError("weixin state_dir is required for images")
        extension = _IMAGE_MIMES.get(mime or "", "bin")
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state_dir.is_symlink() or not self.state_dir.is_dir():
            raise ValueError("invalid Weixin state directory")
        state_root = self.state_dir.resolve(strict=True)
        directory = self.state_dir / "outbound"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("invalid Weixin outbound directory")
        try:
            directory.resolve(strict=True).relative_to(state_root)
        except ValueError as exc:
            raise ValueError("invalid Weixin outbound directory") from exc
        os.chmod(directory, 0o700)
        fd, destination = tempfile.mkstemp(prefix="send-", suffix=f".{extension}", dir=directory)
        source_fd = None
        try:
            os.fchmod(fd, 0o600)
            source_fd = os.open(
                source,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            source_info = os.fstat(source_fd)
            if not stat.S_ISREG(source_info.st_mode):
                os.close(source_fd)
                source_fd = None
                raise ValueError("invalid outbound image")
            input_file = os.fdopen(source_fd, "rb")
            source_fd = None
            with os.fdopen(fd, "wb") as output, input_file:
                written = 0
                while chunk := input_file.read(1024 * 1024):
                    written += len(chunk)
                    if written > self.max_outbound_image_bytes:
                        raise ValueError("outbound image exceeds size limit")
                    output.write(chunk)
            return destination
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            if source_fd is not None:
                try:
                    os.close(source_fd)
                except OSError:
                    pass
            try:
                os.unlink(destination)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _delivery_from_result(result: dict[str, Any], correlation_id: str):
        from reminders.models import DeliveryResult

        return DeliveryResult(
            # A malformed or partial response is never an acknowledgement.
            success=result.get("success") is True,
            retryable=bool(result.get("retryable", False)),
            code=result.get("code"), message=result.get("message"),
            provider_message_id=result.get("provider_message_id"),
        )

    async def stop(self) -> None:
        # Release active handles before marking the channel stopped so their
        # best-effort typing_stop IPC can be queued.  The Bridge also performs
        # a final idempotent cleanup during its own stop command.
        for activity in [
            activity
            for activities in self._typing_activities.values()
            for activity in activities.values()
        ]:
            activity.close()
        self._stopping = True
        inbound_tasks = list(self._background)
        for task in inbound_tasks:
            task.cancel()
        if inbound_tasks:
            await asyncio.gather(*inbound_tasks, return_exceptions=True)
        await self._cancel_pending_message_timers()
        await self._drain_typing_tasks()
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            await asyncio.gather(self._supervisor_task, return_exceptions=True)
            self._supervisor_task = None
        process = self._process
        if process is None:
            return
        try:
            await self._request("stop", {}, timeout=self.stop_timeout_sec)
        except (WeixinBridgeError, asyncio.TimeoutError):
            pass
        finally:
            await self._close_process()

    async def _ensure_process(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        self._stopping = False
        try:
            inherited_environment = (
                "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
                "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "PATHEXT",
            )
            env = {
                key: os.environ[key]
                for key in inherited_environment
                if key in os.environ
            }
            if self.state_dir is not None:
                # This path is not a credential.  The bridge is the only owner
                # of tokens persisted below it and no token is put in argv/env.
                env["NANOCLAW_WEIXIN_STATE_DIR"] = str(self.state_dir)
                env["NANOCLAW_WEIXIN_MEDIA_ROOT"] = str(self.state_dir / "outbound")
                env["NANOCLAW_WEIXIN_TIMEOUT_MS"] = str(
                    max(1, int(self.command_timeout_sec * 1000))
                )
                env["NANOCLAW_WEIXIN_ACK_TIMEOUT_MS"] = str(
                    max(1, int(self.inbound_ack_timeout_sec * 1000))
                )
                env["NANOCLAW_WEIXIN_MAX_LINE_BYTES"] = str(
                    self.max_ipc_line_bytes
                )
                env["NANOCLAW_WEIXIN_MAX_INBOUND_IMAGE_BYTES"] = str(
                    self.max_inbound_image_bytes
                )
                env["NANOCLAW_WEIXIN_MAX_OUTBOUND_IMAGE_BYTES"] = str(
                    self.max_outbound_image_bytes
                )
                env["NANOCLAW_WEIXIN_MAX_INBOUND_FILE_BYTES"] = str(
                    self.max_inbound_file_bytes
                )
            self._process = await asyncio.create_subprocess_exec(
                *self.bridge_command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env, limit=self.max_ipc_line_bytes,
            )
        except (OSError, ValueError) as exc:
            raise WeixinBridgeError("bridge_start_failed", "unable to start Weixin bridge", retryable=True) from exc
        self._reader_task = asyncio.create_task(self._read_stdout(), name="weixin-bridge-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="weixin-bridge-stderr")

    async def _request(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        await self._ensure_process()
        process = self._process
        if process is None or process.stdin is None:
            raise WeixinBridgeError("bridge_unavailable", "Weixin bridge is unavailable", retryable=True)
        self._request_number += 1
        request_id = f"py-{self._request_number}-{uuid.uuid4().hex[:8]}"
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        line = json.dumps({"v": _PROTOCOL_VERSION, "type": "request", "id": request_id, "method": method, "params": params}, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        if len(line) > self.max_ipc_line_bytes:
            self._pending.pop(request_id, None)
            raise WeixinBridgeError("request_too_large")
        try:
            async with self._write_lock:
                process.stdin.write(line)
                await process.stdin.drain()
            response = await asyncio.wait_for(asyncio.shield(future), timeout or self.command_timeout_sec)
            if response.get("ok"):
                return response.get("result") or {}
            error = response.get("error") or {}
            raise WeixinBridgeError(str(error.get("code") or "bridge_error"), str(error.get("message") or "bridge request failed"), retryable=bool(error.get("retryable")))
        except asyncio.TimeoutError as exc:
            raise WeixinBridgeError("timeout", f"Weixin {method} timed out", retryable=True) from exc
        finally:
            self._pending.pop(request_id, None)

    async def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                if len(line) > self.max_ipc_line_bytes:
                    raise WeixinBridgeError("protocol_error", "bridge emitted an oversized line")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    raise WeixinBridgeError("protocol_error", "bridge emitted invalid JSON")
                if message.get("v") != _PROTOCOL_VERSION:
                    raise WeixinBridgeError("protocol_version", "unsupported bridge protocol")
                if message.get("type") == "response":
                    future = self._pending.get(message.get("id"))
                    if future is not None and not future.done():
                        future.set_result(message)
                elif message.get("type") == "event":
                    event, data = message.get("event"), message.get("data") or {}
                    if event == "delivery_result" and isinstance(data.get("correlation_id"), str):
                        # The bridge emits this just before its matching command
                        # response.  Prefer it so response/event semantics stay
                        # identical without resolving a caller twice.
                        if len(self._delivery_events) >= 256:
                            # A malformed bridge could emit unmatched events;
                            # retain a bounded diagnostic/cache surface only.
                            self._delivery_events.pop(next(iter(self._delivery_events)))
                        self._delivery_events[data["correlation_id"]] = data
                        logger.info("Weixin delivery result: %s", data.get("code", "unknown"))
                    else:
                        self._spawn(self._handle_event(event, data))
                else:
                    raise WeixinBridgeError("protocol_error", "unexpected bridge message")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # EOF/errors fail every outstanding request
            self._fail_pending(exc)
        finally:
            if not self._stopping:
                self._fail_pending(WeixinBridgeError("bridge_exited", "Weixin bridge exited", retryable=True))
                if self._started:
                    self._schedule_restart()

    async def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        try:
            while line := await self._process.stderr.readline():
                text = line.decode("utf-8", "replace").strip()
                if text:
                    logger.warning("Weixin bridge: %s", _SECRET_RE.sub(r"\1=<redacted>", text))
        except asyncio.CancelledError:
            raise

    async def _handle_event(self, event: str | None, data: dict[str, Any]) -> None:
        if event == "inbound_message":
            if self._stopping:
                return
            await self._handle_inbound(data)
        elif event == "qr_code":
            # QR payload is intentionally shown only to the local operator; it
            # is never forwarded over Bus or included in structured logs.
            print(f"微信扫码登录：{data.get('ascii') or data.get('image') or data.get('qrcode') or '<二维码已生成>'}")
        elif event == "login_status":
            logger.info("Weixin login status: %s", data.get("status", "unknown"))
        elif event == "login_success":
            logger.info("Weixin login succeeded")
        elif event == "stopped":
            self._started = False
            logger.info("Weixin bridge stopped")
        elif event == "channel_error":
            code = data.get("code", "unknown")
            reason = data.get("reason")
            safe_code = (
                code
                if isinstance(code, str) and code in _CHANNEL_ERROR_CODES
                else "unknown"
            )
            if (
                safe_code == "protocol_error"
                and isinstance(reason, str)
                and reason in _CHANNEL_ERROR_REASONS
            ):
                logger.warning("Weixin bridge channel error: %s (%s)", safe_code, reason)
            else:
                # The child process is an IPC trust boundary.  Never reflect an
                # arbitrary provider message/body into ordinary application logs.
                logger.warning("Weixin bridge channel error: %s", safe_code)
        elif event == "session_expired":
            self._started = False
            logger.warning("Weixin session expired")
            await self._suspend_reminders()
            if not self._stopping:
                self._schedule_reauth()

    async def _handle_inbound(self, data: dict[str, Any]) -> None:
        account_id, user_id, delivery_id = data.get("account_id"), data.get("user_id"), data.get("delivery_id")
        if not all(isinstance(value, str) and value for value in (account_id, user_id, delivery_id)):
            logger.warning("discarded malformed Weixin inbound event")
            return
        if not self._allowed(user_id):
            logger.info("discarded Weixin inbound message from an unallowed user")
            # This is an intentional policy decision, not a transient delivery
            # failure.  Ack it so a denied sender cannot pin the durable cursor.
            try:
                await self._request("ack_inbound", {"delivery_id": delivery_id}, timeout=self.inbound_ack_timeout_sec)
            except WeixinBridgeError:
                logger.warning("failed to acknowledge denied Weixin inbound message")
            return
        try:
            async with self._inbound_lock:
                accepted = await self._accept_inbound_locked(
                    account_id, user_id, delivery_id, data
                )
        except _UnsupportedInbound as exc:
            logger.warning("discarded unsupported Weixin inbound message: %s", exc)
            await self._ack_inbound(delivery_id)
            return
        except _ReminderCommandError:
            logger.warning("failed to persist Weixin reminder command")
            return
        except Exception as exc:
            # ImageStore/FileStore, persistence and Bus failures are
            # recoverable.  Do not acknowledge: Bridge redelivery is the
            # at-least-once path.
            logger.warning("failed to accept Weixin inbound message: %s", exc)
            return
        if accepted:
            await self._ack_inbound(delivery_id)

    async def _accept_inbound_locked(
        self, account_id: str, user_id: str, delivery_id: str, data: dict[str, Any]
    ) -> bool:
        """Persist/merge an inbound event while serializing timer races.

        Returning true means the event was either published or durably accepted
        into its in-memory message batch and may therefore be acknowledged.
        """
        target = encode_weixin_target(account_id, user_id)
        st = self._session_state(target)
        sequence = st["current"]
        sender_id = self._session_sender_id(target, sequence)
        session_key = f"{self.name}:{sender_id}"
        text = data.get("text") if isinstance(data.get("text"), str) else ""
        text = text.strip()
        # These are deliberately exact, text-only commands.  In particular a
        # command with an image remains an ordinary image turn, so it cannot
        # unexpectedly bind a reminder target or consume a pending batch.
        if not data.get("images") and text in {"/bind-reminders", "/unbind-reminders", "/new"}:
            if text == "/new":
                await self._handle_new_command(account_id, user_id, target)
            else:
                await self._handle_reminder_command(target, text)
            return True
        key = (account_id, user_id)
        batch = self._pending_message_batches.get(key)
        if batch is not None and delivery_id in batch.delivery_ids:
            for descriptor in data.get("images") or []:
                self._discard_duplicate_inbound_image(descriptor)
            for descriptor in data.get("files") or []:
                self._discard_duplicate_inbound_file(descriptor)
            return True
        images = []
        for descriptor in data.get("images") or []:
            try:
                image = self._consume_inbound_image(descriptor, session_key)
            except ValueError as exc:
                raise _UnsupportedInbound(str(exc)) from exc
            if image is not None:
                images.append(image)
        files = []
        for descriptor in data.get("files") or []:
            try:
                file_ref = self._consume_inbound_file(descriptor, session_key)
            except ValueError as exc:
                raise _UnsupportedInbound(str(exc)) from exc
            if file_ref is not None:
                files.append(file_ref)
        if not text and not images and not files:
            logger.info("discarded empty or unsupported Weixin inbound message")
            return True
        # 合并关闭：每条消息立即独立投递，不进批次。
        if self.merge_window_sec == 0:
            content = self._build_content(text, images, files)
            try:
                await self._publish_inbound(
                    sender_id, target, content, images, data, files=files
                )
            except Exception:
                self._delete_image_refs(images)
                self._delete_file_refs(files)
                raise
            return True
        # 统一消息合并：文本/图片/图文混合全部进入同一批次。窗口内到达的
        # 新消息追加进当前批次（文本按行追加、图片随附）；窗口到期或批次
        # 内消息数达到 merge_max_messages 时整批 flush。
        old_batch = batch
        new_batch = _PendingMessageBatch(
            account_id, user_id, target, session_key,
            sequence=sequence,
            texts=list(old_batch.texts) if old_batch is not None else [],
            images=list(old_batch.images) if old_batch is not None else [],
            files=list(old_batch.files) if old_batch is not None else [],
            delivery_ids=set(old_batch.delivery_ids) if old_batch is not None else set(),
            deadline_ms=int((time.time() + self.merge_window_sec) * 1000),
        )
        if text:
            new_batch.texts.append(text)
        new_batch.images.extend(images)
        new_batch.files.extend(files)
        new_batch.delivery_ids.add(delivery_id)
        self._pending_message_batches[key] = new_batch
        try:
            self._persist_pending_message_batches_locked()
        except Exception:
            if old_batch is None:
                self._pending_message_batches.pop(key, None)
            else:
                self._pending_message_batches[key] = old_batch
            self._delete_image_refs(images)
            self._delete_file_refs(files)
            raise
        if old_batch is not None and old_batch.timer is not None:
            old_batch.timer.cancel()
        # 条数上限：每条入站消息恰好贡献一个 delivery_id，故 len == 消息数。
        # 先挂窗口定时器再 flush：若上限 flush 发布失败（异常上抛），新批次
        # 仍持有定时器，窗口到期会自动重试，不会卡死到"下一条消息或重启"。
        self._schedule_message_batch_timer_locked(key, new_batch)
        if len(new_batch.delivery_ids) >= self.merge_max_messages:
            await self._flush_message_batch_locked(key, data)
        return True

    # —— 多会话辅助（与飞书 /new 机制对称）——
    # 每个 target 维护自己的会话序号：普通文本 sender_id 形如 "<target>:<序号>"，
    # Gateway 据此派生独立 session_key，从而同一聊天框内可开多个互不干扰的会话。
    # 会话 0 复用原有无后缀 sender_id（weixin:<target>），保证升级后老会话记忆连续。
    def _session_state(self, target: str) -> dict:
        """取（或建）某 target 的会话状态；首条消息前默认序号 0。"""
        st = self._sessions.get(target)
        if st is None:
            st = {"seq": 0, "current": 0}
            self._sessions[target] = st
        return st

    @staticmethod
    def _session_sender_id(target: str, sequence: int) -> str:
        """把会话序号拼进 sender_id；会话 0 保持无后缀以延续既有会话。"""
        return target if sequence == 0 else f"{target}:{sequence}"

    async def _handle_reminder_command(self, target: str, command: str) -> None:
        """Persist a reminder binding before acknowledging its inbound command."""

        callback = (
            self._bind_callback
            if command == "/bind-reminders"
            else self._unbind_callback
        )
        if callback is None:
            outcome = "⚠️ 当前实例未启用主动提醒服务。"
        else:
            try:
                result = callback(target, target)
                outcome = await result if inspect.isawaitable(result) else result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _ReminderCommandError from exc
            default = (
                "✅ 已绑定主动提醒。"
                if command == "/bind-reminders"
                else "✅ 已解绑主动提醒。"
            )
            outcome = outcome if isinstance(outcome, str) and outcome else default
        await self.bus.publish_outbound(OutboundMessage(
            channel=self.name,
            chat_id=target,
            content=outcome,
        ))

    async def _handle_new_command(self, account_id: str, user_id: str, target: str) -> None:
        """/new：先让在途消息落进旧会话，再切到新会话并直接回复（不经 Agent）。"""
        key = (account_id, user_id)
        batch = self._pending_message_batches.get(key)
        if batch is not None:
            await self._flush_message_batch_locked(key, {})
        st = self._session_state(target)
        st["seq"] += 1
        st["current"] = st["seq"]
        await self.bus.publish_outbound(OutboundMessage(
            channel=self.name,
            chat_id=target,
            content=f"🆕 已新建会话 #{st['current']}（旧会话已保留）",
        ))

    async def _suspend_reminders(self) -> None:
        """Best-effort pause after session expiry without exposing bridge data."""

        if self._suspend_callback is None:
            return
        try:
            result = self._suspend_callback()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            # The callback may touch the reminder DB; it must not delay or
            # disable the credential-free QR reauthentication path.
            logger.warning("failed to suspend Weixin reminders after session expiry")

    async def _flush_message_batch_after_window(
        self, key: tuple[str, str], batch: _PendingMessageBatch
    ) -> None:
        try:
            await asyncio.sleep(max(0, batch.deadline_ms / 1000 - time.time()))
            async with self._inbound_lock:
                if self._pending_message_batches.get(key) is batch:
                    await self._flush_message_batch_locked(key, {})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("failed to publish queued Weixin messages")
            # 发布失败：批次保留。重新调度一个窗口定时器到期自动重试，避免批次
            # 滞留到"下一条消息或重启"。先推进 deadline，防止旧的已过期 deadline
            # 导致 sleep(0) 的紧循环重试。
            async with self._inbound_lock:
                if self._pending_message_batches.get(key) is batch:
                    batch.deadline_ms = int((time.time() + self.merge_window_sec) * 1000)
                    self._schedule_message_batch_timer_locked(key, batch)

    async def _flush_message_batch_locked(
        self, key: tuple[str, str], raw: dict[str, Any] | None = None
    ) -> None:
        """Publish a whole batch (merged texts + images) and drop it durably.

        Must be called while holding :attr:`_inbound_lock`.  The current
        message (if any) has already been merged into the batch before this
        call, so a failed publish restores the batch untouched and no image
        reference is released.
        """
        batch = self._pending_message_batches.get(key)
        if batch is None:
            return
        raw = raw or {}
        content = self._build_content(
            "\n".join(batch.texts) if batch.texts else "",
            batch.images,
            batch.files,
        )
        try:
            await self._publish_inbound(
                self._session_sender_id(batch.target, batch.sequence),
                batch.target, content, batch.images, raw,
                files=batch.files,
            )
        except Exception:
            # 发布失败：保留批次（含其文本与图片引用），等待重投/重试。
            raise
        self._pending_message_batches.pop(key, None)
        try:
            self._persist_pending_message_batches_locked()
        except Exception:
            self._pending_message_batches[key] = batch
            raise
        if batch.timer is not None and batch.timer is not asyncio.current_task():
            batch.timer.cancel()

    async def _publish_inbound(
        self,
        sender_id: str,
        target: str,
        text: str,
        images: list,
        raw: dict[str, Any],
        files: list | None = None,
    ) -> None:
        acceptance = asyncio.get_running_loop().create_future()
        await self.bus.publish_inbound(InboundMessage(
            self.name, sender_id, target, text,
            raw={"message_id": raw.get("message_id"), "delivery_id": raw.get("delivery_id")},
            images=images or None,
            files=files or None,
            acceptance_future=acceptance,
        ))
        await acceptance

    async def _cancel_pending_message_timers(self) -> None:
        # A timer can be holding _inbound_lock while it waits for MessageBus to
        # accept a flush.  Cancel it before acquiring that lock so a stop can
        # never deadlock behind an unconsumed in-memory handoff.
        timers = [
            batch.timer
            for batch in self._pending_message_batches.values()
            if batch.timer is not None
        ]
        for timer in timers:
            timer.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)
        async with self._inbound_lock:
            for batch in self._pending_message_batches.values():
                if batch.timer in timers:
                    batch.timer = None

    def _schedule_message_batch_timer_locked(
        self, key: tuple[str, str], batch: _PendingMessageBatch
    ) -> None:
        batch.timer = asyncio.create_task(
            self._flush_message_batch_after_window(key, batch),
            name="weixin-message-merge",
        )

    def _pending_batches_path(self) -> Path:
        if self.state_dir is None:
            raise ValueError("weixin state_dir is required for message batching")
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state_dir.is_symlink() or not self.state_dir.is_dir():
            raise ValueError("invalid Weixin state directory")
        os.chmod(self.state_dir, 0o700)
        # 持久化文件名故意保留旧名 pending_image_batches.json（含 image 字样）：
        # 目的是兼容旧版本遗留的数据文件，避免升级后旧批次被忽略而丢失。
        # 注意：文件内容现在存的是统一消息批次（文本 + 图片），已不只是图片。
        # 请勿随意改名；若要改名，必须先实现文件迁移逻辑（旧名 → 新名）再切换。
        return self.state_dir / "pending_image_batches.json"

    def _persist_pending_message_batches_locked(self) -> None:
        path = self._pending_batches_path()
        if os.path.lexists(path) and path.is_symlink():
            raise ValueError("invalid pending message state file")
        if not self._pending_message_batches:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            else:
                self._fsync_pending_directory()
            return
        payload = {
            "version": 1,
            "batches": [
                {
                    "account_id": batch.account_id,
                    "user_id": batch.user_id,
                    "target": batch.target,
                    "session_key": batch.session_key,
                    "delivery_ids": sorted(batch.delivery_ids),
                    "deadline_ms": batch.deadline_ms,
                    "texts": batch.texts,
                    "images": [{"id": image.id, "mime": image.mime} for image in batch.images],
                    "files": [
                        {
                            "id": ref.id,
                            "path": ref.path,
                            "name": ref.name,
                            "size": ref.size,
                            "mime": ref.mime,
                        }
                        for ref in batch.files
                    ],
                }
                for batch in self._pending_message_batches.values()
            ],
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > _MAX_PENDING_STATE_BYTES:
            raise ValueError("pending message state exceeds size limit")
        fd, temporary = tempfile.mkstemp(prefix=".pending-messages-", dir=self.state_dir)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            self._fsync_pending_directory()
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _fsync_pending_directory(self) -> None:
        directory_fd = os.open(self.state_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    async def _restore_pending_message_batches(self) -> None:
        if self._pending_batches_restored:
            return
        if self.state_dir is None:
            self._pending_batches_restored = True
            return
        async with self._inbound_lock:
            path = self._pending_batches_path()
            if not os.path.lexists(path):
                self._pending_batches_restored = True
                return
            if path.is_symlink():
                raise ValueError("invalid pending message state file")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size <= 0
                or info.st_size > _MAX_PENDING_STATE_BYTES
            ):
                os.close(fd)
                raise ValueError("invalid pending message state file")
            with os.fdopen(fd, encoding="utf-8") as source:
                payload = json.load(source)
            if (
                not isinstance(payload, dict)
                or payload.get("version") != 1
                or not isinstance(payload.get("batches"), list)
            ):
                raise ValueError("invalid pending message state")
            loaded: dict[tuple[str, str], _PendingMessageBatch] = {}
            expired: list[tuple[str, str]] = []
            for item in payload["batches"]:
                if not isinstance(item, dict):
                    continue
                account_id, user_id = item.get("account_id"), item.get("user_id")
                target, session_key = item.get("target"), item.get("session_key")
                if not all(isinstance(value, str) and value for value in (account_id, user_id, target, session_key)):
                    continue
                if not self._allowed(user_id):
                    continue
                if target != encode_weixin_target(account_id, user_id):
                    continue
                # 兼容两种批次格式：旧格式 session_key=weixin:<target> 表示会话 0
                # （sender_id 无后缀）；新格式 weixin:<target>:<N> 表示会话 N。
                prefix = f"{self.name}:{target}"
                sequence = 0
                if session_key.startswith(prefix + ":"):
                    suffix = session_key[len(prefix) + 1:]
                    if not suffix.isdigit() or int(suffix) <= 0:
                        continue
                    sequence = int(suffix)
                elif session_key != prefix:
                    continue
                text_items = item.get("texts")
                if text_items is None:
                    text_items = []  # 旧格式（仅图片批次）无 texts 字段，视为空文本
                image_items = item.get("images")
                # Legacy batches persisted before TASK-003 have no "files"
                # field; treat the missing key as an empty list.
                file_items = item.get("files")
                if not isinstance(file_items, list):
                    file_items = []
                delivery_items = item.get("delivery_ids")
                deadline = item.get("deadline_ms")
                if (
                    not isinstance(text_items, list)
                    or not all(isinstance(value, str) for value in text_items)
                    or not isinstance(image_items, list)
                    or not isinstance(delivery_items, list)
                    or not delivery_items
                    or not all(
                        isinstance(value, str) and 0 < len(value) <= 256
                        for value in delivery_items
                    )
                    or isinstance(deadline, bool)
                    or not isinstance(deadline, (int, float))
                    or deadline <= 0
                ):
                    continue
                images = []
                if self.image_store is not None:
                    for image_data in image_items:
                        if (
                            not isinstance(image_data, dict)
                            or not isinstance(image_data.get("id"), str)
                            or not 0 < len(image_data["id"]) <= 128
                        ):
                            continue
                        image = self.image_store.resolve(session_key, image_data["id"])
                        if image is not None:
                            images.append(image)
                files = []
                for file_data in file_items or []:
                    if not isinstance(file_data, dict):
                        continue
                    file_id = file_data.get("id")
                    file_path = file_data.get("path")
                    file_name = file_data.get("name")
                    file_size = file_data.get("size")
                    if (
                        not isinstance(file_id, str)
                        or not 0 < len(file_id) <= 128
                        or not isinstance(file_path, str)
                        or not file_path
                        or not isinstance(file_name, str)
                        or not file_name
                        or isinstance(file_size, bool)
                        or not isinstance(file_size, (int, float))
                        or file_size <= 0
                    ):
                        continue
                    mime = file_data.get("mime")
                    files.append(
                        FileRef(
                            id=file_id,
                            path=file_path,
                            name=file_name,
                            size=int(file_size),
                            mime=mime if isinstance(mime, str) and mime else None,
                        )
                    )
                if not images and not files and not text_items:
                    continue
                key = (account_id, user_id)
                if key in loaded:
                    continue
                batch = _PendingMessageBatch(
                    account_id, user_id, target, session_key,
                    sequence=sequence, texts=list(text_items), images=images,
                    files=files,
                    delivery_ids=set(delivery_items), deadline_ms=int(deadline),
                )
                loaded[key] = batch
                # 已过期或已达条数上限的批次直接 flush，不等定时器。
                if (
                    batch.deadline_ms <= int(time.time() * 1000)
                    or len(batch.delivery_ids) >= self.merge_max_messages
                ):
                    expired.append(key)
            self._pending_message_batches.update(loaded)
            self._persist_pending_message_batches_locked()
            for key in expired:
                await self._flush_message_batch_locked(key, {})
            for key, batch in loaded.items():
                if key in self._pending_message_batches and key not in expired:
                    self._schedule_message_batch_timer_locked(key, batch)
            self._pending_batches_restored = True

    def _discard_duplicate_inbound_image(self, descriptor: Any) -> None:
        """Remove a redelivered Bridge temp file without copying it again."""
        if not isinstance(descriptor, dict) or self.state_dir is None:
            return
        path_text, mime = descriptor.get("file_path"), descriptor.get("mime_type")
        if not isinstance(path_text, str) or mime not in _IMAGE_MIMES:
            return
        try:
            path = Path(path_text)
            if path.is_symlink():
                return
            state_root = self.state_dir.resolve(strict=True)
            inbound_root = (self.state_dir / "inbound").resolve(strict=True)
            inbound_root.relative_to(state_root)
            resolved = path.resolve(strict=True)
            resolved.relative_to(inbound_root)
            resolved.unlink()
        except (OSError, ValueError):
            return

    def _discard_duplicate_inbound_file(self, descriptor: Any) -> None:
        """Remove a redelivered Bridge temp file without copying it again."""
        if not isinstance(descriptor, dict) or self.state_dir is None:
            return
        path_text = descriptor.get("file_path")
        if not isinstance(path_text, str):
            return
        try:
            path = Path(path_text)
            if path.is_symlink():
                return
            state_root = self.state_dir.resolve(strict=True)
            inbound_root = (self.state_dir / "inbound").resolve(strict=True)
            inbound_root.relative_to(state_root)
            resolved = path.resolve(strict=True)
            resolved.relative_to(inbound_root)
            resolved.unlink()
        except (OSError, ValueError):
            return

    @staticmethod
    def _delete_image_refs(images: list) -> None:
        for image in images:
            try:
                os.unlink(image.path)
            except FileNotFoundError:
                pass

    def _delete_file_refs(self, files: list) -> None:
        """投递失败回滚：删除已归档文件，避免重投时落成 -1 副本。"""
        store = self.file_store
        if store is None:
            return
        for ref in files:
            try:
                store.delete(ref)
            except Exception:  # noqa: BLE001 - best-effort rollback
                logger.warning("failed to roll back saved Weixin file")

    async def _ack_inbound(self, delivery_id: str) -> None:
        """Best-effort acknowledgement for an already validated delivery id."""
        try:
            await self._request("ack_inbound", {"delivery_id": delivery_id}, timeout=self.inbound_ack_timeout_sec)
        except (WeixinBridgeError, asyncio.CancelledError):
            if not self._stopping:
                logger.warning("failed to acknowledge Weixin inbound message")

    def _consume_inbound_image(self, descriptor: Any, session_key: str):
        if self.image_store is None or not isinstance(descriptor, dict):
            raise ValueError("image storage unavailable")
        path_text, mime = descriptor.get("file_path"), descriptor.get("mime_type")
        if not isinstance(path_text, str) or mime not in _IMAGE_MIMES or self.state_dir is None:
            raise ValueError("invalid image descriptor")
        original_path = Path(path_text)
        if original_path.is_symlink():
            raise ValueError("invalid image file")
        state_root = self.state_dir.resolve(strict=True)
        inbound_directory = self.state_dir / "inbound"
        if inbound_directory.is_symlink() or not inbound_directory.is_dir():
            raise ValueError("invalid bridge inbound directory")
        inbound_root = inbound_directory.resolve(strict=True)
        try:
            inbound_root.relative_to(state_root)
        except ValueError as exc:
            raise ValueError("invalid bridge inbound directory") from exc
        path = original_path.resolve(strict=True)
        try:
            path.relative_to(inbound_root)
        except ValueError as exc:
            raise ValueError("image path outside bridge inbound directory") from exc
        descriptor_fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        info = os.fstat(descriptor_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > self.max_inbound_image_bytes:
            os.close(descriptor_fd)
            raise ValueError("invalid image file")
        try:
            with os.fdopen(descriptor_fd, "rb") as input_file:
                data = input_file.read(self.max_inbound_image_bytes + 1)
            if len(data) > self.max_inbound_image_bytes:
                raise ValueError("invalid image file")
            return self.image_store.save(session_key, data, _IMAGE_MIMES[mime], mime)
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _file_reference_text(self, ref) -> str:
        """把一份 FileRef 拼成入站 content 中的文件引用文本（不读文件内容）。"""
        return f"📎 收到文件：{ref.path}（{_human_file_size(ref.size)}）"

    def _build_content(self, text: str, images: list, files: list) -> str:
        """按文本/图片/文件组装入站 content。

        文本在前；文件引用随后（每份一行，路径为相对 workspace 的相对路径）。
        纯文件消息（无文本）的 content 就是文件引用文本，能直接进 Agent。
        """
        parts = []
        if text:
            parts.append(text)
        file_lines = [self._file_reference_text(ref) for ref in files]
        if file_lines:
            parts.append("\n".join(file_lines))
        if parts:
            return "\n".join(parts)
        if images:
            return "请分析这张图片。" if len(images) == 1 else "请分析这些图片。"
        return ""

    def _consume_inbound_file(self, descriptor: Any, session_key: str):
        """把 Bridge 临时文件搬进 FileStore，返回 FileRef；读失败/超限丢弃。

        与图片不同：大小超限或读取失败只丢弃该文件并返回 ``None``，不影响同
        批文本/图片继续投递。结构或安全校验失败（路径越界/符号链接）仍抛
        ``ValueError``，由调用方按不可支持消息整体丢弃（fail-closed）。
        """
        if self.file_store is None or not isinstance(descriptor, dict):
            raise ValueError("file storage unavailable")
        path_text = descriptor.get("file_path")
        file_name = descriptor.get("file_name")
        if not isinstance(path_text, str) or self.state_dir is None:
            raise ValueError("invalid file descriptor")
        if not isinstance(file_name, str) or not file_name:
            file_name = ""
        original_path = Path(path_text)
        if original_path.is_symlink():
            raise ValueError("invalid file path")
        state_root = self.state_dir.resolve(strict=True)
        inbound_directory = self.state_dir / "inbound"
        if inbound_directory.is_symlink() or not inbound_directory.is_dir():
            raise ValueError("invalid bridge inbound directory")
        inbound_root = inbound_directory.resolve(strict=True)
        try:
            inbound_root.relative_to(state_root)
        except ValueError as exc:
            raise ValueError("invalid bridge inbound directory") from exc
        path = original_path.resolve(strict=True)
        try:
            path.relative_to(inbound_root)
        except ValueError as exc:
            raise ValueError("file path outside bridge inbound directory") from exc
        descriptor_fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        info = os.fstat(descriptor_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size <= 0
            or info.st_size > self.max_inbound_file_bytes
        ):
            os.close(descriptor_fd)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return None
        try:
            with os.fdopen(descriptor_fd, "rb") as input_file:
                try:
                    data = input_file.read(self.max_inbound_file_bytes + 1)
                except OSError:
                    return None
            if len(data) > self.max_inbound_file_bytes:
                return None
            return self.file_store.save(data, file_name)
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _spawn(self, coroutine) -> None:
        if self._stopping:
            coroutine.close()
            return
        task = asyncio.create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _schedule_restart(self) -> None:
        """Ensure one cancellable supervisor restores an unexpectedly dead bridge."""
        if self._supervisor_task is None or self._supervisor_task.done():
            self._supervisor_task = asyncio.create_task(self._supervise_restart(), name="weixin-bridge-restart")

    def _schedule_reauth(self) -> None:
        """Start one forced-login loop after the bridge invalidates a session."""
        if self._supervisor_task is None or self._supervisor_task.done():
            self._supervisor_task = asyncio.create_task(self._supervise_reauth(), name="weixin-bridge-reauth")

    async def _supervise_reauth(self) -> None:
        """Prompt a fresh QR login without putting any credential in Python."""
        delay = self.restart_backoff_sec
        while not self._stopping:
            if delay:
                await asyncio.sleep(delay)
            if self._stopping:
                return
            try:
                await self.login(True)
                await self.start_polling()
                logger.info("Weixin session reauthenticated")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Weixin reauthentication failed: %s", getattr(exc, "code", "unavailable"))
                delay = min(max(delay * 2, 0.1), 30.0)

    async def _supervise_restart(self) -> None:
        """Retry reconnecting while this channel remains enabled.

        Each individual delay is bounded; cancellation from :meth:`stop` exits
        immediately.  A reconnect uses persisted bridge credentials, never
        transfers a token through Python.
        """
        delay = self.restart_backoff_sec
        while not self._stopping:
            if delay:
                await asyncio.sleep(delay)
            if self._stopping:
                return
            previous = self._process
            if previous is not None and previous.returncode is None:
                previous.terminate()
                try:
                    await asyncio.wait_for(previous.wait(), timeout=self.stop_timeout_sec)
                except asyncio.TimeoutError:
                    previous.kill()
                    await previous.wait()
            if self._process is previous:
                self._process = None
            if self._stderr_task is not None:
                self._stderr_task.cancel()
                await asyncio.gather(self._stderr_task, return_exceptions=True)
                self._stderr_task = None
            try:
                await self._ensure_process()
                await self._request("hello", {}, timeout=self.command_timeout_sec)
                await self.login(False)
                await self.start_polling()
                logger.info("Weixin bridge reconnected")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # no token/provider body is logged
                logger.warning("Weixin bridge reconnect failed: %s", getattr(exc, "code", "unavailable"))
                delay = min(max(delay * 2, 0.1), 30.0)

    def _fail_pending(self, exc: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)

    async def _close_process(self) -> None:
        for task in (self._reader_task, self._stderr_task, *self._background):
            if task is not None:
                task.cancel()
        tasks = [task for task in (self._reader_task, self._stderr_task, *self._background) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        process, self._process = self._process, None
        self._reader_task = self._stderr_task = None
        self._supervisor_task = None
        self._background.clear()
        self._fail_pending(WeixinBridgeError("bridge_stopped", "Weixin bridge stopped", retryable=True))
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
