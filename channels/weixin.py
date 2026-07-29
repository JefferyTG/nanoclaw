"""WeChat iLink JSONL bridge channel.

The Node bridge owns every WeChat credential and protocol cursor.  This
adapter deliberately only sees stable account/user identifiers, messages, and
controlled temporary image files.  It is usable without the composition root
so its process boundary can be tested entirely with a fake subprocess.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any, Sequence

from bus.queue import InboundMessage, OutboundMessage
from channels.base import Channel

logger = logging.getLogger("nanoclaw.weixin")

_PROTOCOL_VERSION = 1
_MAX_LINE_BYTES = 1024 * 1024
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
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


class WeixinBridgeError(RuntimeError):
    """A structured bridge failure safe to present to the local scheduler."""

    def __init__(self, code: str, message: str = "bridge request failed", *, retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


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
        request_timeout_sec: float = 30.0,
        login_timeout_sec: float = 480.0,
        inbound_ack_timeout_sec: float = 30.0,
        stop_timeout_sec: float = 10.0,
        max_ipc_line_bytes: int = _MAX_LINE_BYTES,
        max_inbound_image_bytes: int = _MAX_IMAGE_BYTES,
        max_outbound_image_bytes: int = _MAX_IMAGE_BYTES,
        restart_backoff_sec: float = 1.0,
        auto_login: bool = False,
    ) -> None:
        super().__init__(name=name, bus=bus)
        if not bridge_command:
            raise ValueError("weixin bridge_command is required")
        self.bridge_command = tuple(str(part) for part in bridge_command)
        self.state_dir = Path(state_dir).resolve() if state_dir else None
        self.allowed_user_ids = frozenset(str(item) for item in (allowed_user_ids or ()))
        self.image_store = image_store
        self.command_timeout_sec = max(0.1, float(request_timeout_sec))
        self.login_timeout_sec = max(0.1, float(login_timeout_sec))
        self.inbound_ack_timeout_sec = max(0.1, float(inbound_ack_timeout_sec))
        self.stop_timeout_sec = max(0.1, float(stop_timeout_sec))
        self.max_ipc_line_bytes = max(1024, int(max_ipc_line_bytes))
        self.max_inbound_image_bytes = max(1, int(max_inbound_image_bytes))
        self.max_outbound_image_bytes = max(1, int(max_outbound_image_bytes))
        self.restart_backoff_sec = max(0.0, float(restart_backoff_sec))
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

    def target(self, account_id: str, user_id: str) -> str:
        return encode_weixin_target(account_id, user_id)

    @staticmethod
    def parse_target(target: str) -> tuple[str, str]:
        return decode_weixin_target(target)

    def _allowed(self, user_id: str) -> bool:
        return "*" in self.allowed_user_ids or user_id in self.allowed_user_ids

    async def start(self) -> None:
        await self._ensure_process()
        await self._request("hello", {}, timeout=self.command_timeout_sec)
        if self.auto_login:
            await self.login()
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
        self._stopping = True
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
        images = []
        try:
            for descriptor in data.get("images") or []:
                image = self._consume_inbound_image(descriptor, encode_weixin_target(account_id, user_id))
                if image is not None:
                    images.append(image)
            text = data.get("text") if isinstance(data.get("text"), str) else ""
            if not text and images:
                text = "请分析这张图片。" if len(images) == 1 else "请分析这些图片。"
            if not text:
                logger.info("discarded empty or unsupported Weixin inbound message")
                await self._ack_inbound(delivery_id)
                return
            target = encode_weixin_target(account_id, user_id)
            await self.bus.publish_inbound(InboundMessage(self.name, target, target, text, raw={"message_id": data.get("message_id"), "delivery_id": delivery_id}, images=images or None))
        except Exception as exc:
            logger.warning("discarded Weixin inbound message: %s", exc)
            # A syntactically valid delivery that V1 cannot consume (for
            # example unsupported media) must not pin the bridge cursor.
            await self._ack_inbound(delivery_id)
            return

        await self._ack_inbound(delivery_id)

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

    def _spawn(self, coroutine) -> None:
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
