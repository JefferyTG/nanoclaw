"""Offline contract tests for :mod:`channels.weixin`."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bus.queue import ImageRef, MessageBus, OutboundMessage
from channels.weixin import WeixinChannel, decode_weixin_target, encode_weixin_target


class _Writer:
    def __init__(self): self.lines = []
    def write(self, line): self.lines.append(json.loads(line))
    async def drain(self): pass


class _Reader:
    def __init__(self): self.queue = asyncio.Queue()
    async def readline(self): return await self.queue.get()


class _Process:
    def __init__(self):
        self.stdin, self.stdout, self.stderr = _Writer(), _Reader(), _Reader()
        self.returncode = None
    def terminate(self): self.returncode = 0
    def kill(self): self.returncode = -9
    async def wait(self): self.returncode = self.returncode if self.returncode is not None else 0


class _Store:
    def __init__(self): self.saved = []
    def save(self, session_key, data, ext, mime):
        self.saved.append((session_key, data, ext, mime))
        return ImageRef("image", "/unused", mime)


class WeixinChannelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.process = _Process()
        self.patcher = patch("channels.weixin.asyncio.create_subprocess_exec", return_value=self.process)
        self.spawn = self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.bus = MessageBus()
        self.channel = WeixinChannel(bus=self.bus, bridge_command=["fake"], allowed_user_ids=["user"], state_dir=tempfile.gettempdir(), request_timeout_sec=0.01, stop_timeout_sec=0.01)

    async def asyncTearDown(self):
        await self.channel.stop()

    async def _respond(self, *, method=None, result=None, ok=True, error=None):
        for _ in range(20):
            if self.process.stdin.lines:
                request = self.process.stdin.lines[-1]
                if method is not None and request["method"] != method:
                    await asyncio.sleep(0)
                    continue
                await self.process.stdout.queue.put(json.dumps({"v": 1, "type": "response", "id": request["id"], "ok": ok, "result": result or {}, "error": error}).encode() + b"\n")
                return request
            await asyncio.sleep(0)
        self.fail("no request")

    async def test_target_round_trip_handles_delimiters(self):
        target = encode_weixin_target("a:b/中文", "u:1/中文")
        self.assertEqual(decode_weixin_target(target), ("a:b/中文", "u:1/中文"))
        with self.assertRaises(ValueError): decode_weixin_target("wx1.invalid")

    async def test_send_preserves_stable_correlation_and_maps_rejection(self):
        target = encode_weixin_target("account", "user")
        task = asyncio.create_task(self.channel.send(OutboundMessage("weixin", target, "hello", correlation_id="stable")))
        request = await self._respond(ok=False, error={"code": "api_rejected", "message": "redacted", "retryable": False})
        self.assertEqual(request["params"]["correlation_id"], "stable")
        result = await task
        self.assertFalse(result.success)
        self.assertEqual(result.code, "api_rejected")

    async def test_delivery_event_before_response_is_the_single_authoritative_result(self):
        target = encode_weixin_target("account", "user")
        task = asyncio.create_task(self.channel.send(OutboundMessage(
            "weixin", target, "hello", correlation_id="stable-event",
        )))
        for _ in range(20):
            if self.process.stdin.lines:
                break
            await asyncio.sleep(0)
        request = self.process.stdin.lines[-1]
        await self.process.stdout.queue.put(json.dumps({
            "v": 1, "type": "event", "event": "delivery_result", "data": {
                "correlation_id": "stable-event", "success": True,
                "retryable": False, "code": "ok", "provider_message_id": "event-id",
            },
        }).encode() + b"\n")
        await self.process.stdout.queue.put(json.dumps({
            "v": 1, "type": "response", "id": request["id"], "ok": True,
            "result": {"success": True, "code": "response-code", "provider_message_id": "response-id"},
        }).encode() + b"\n")
        result = await task
        self.assertTrue(result.success)
        self.assertEqual(result.code, "ok")
        self.assertEqual(result.provider_message_id, "event-id")
        self.assertEqual(self.channel._delivery_events, {})

    async def test_qr_event_prefers_scannable_image_payload(self):
        with patch("builtins.print") as output:
            await self.channel._handle_event("qr_code", {"qrcode": "opaque-handle", "image": "scan-this", "ascii": "██ QR ██"})
        self.assertIn("██ QR ██", output.call_args.args[0])
        self.assertNotIn("scan-this", output.call_args.args[0])
        self.assertNotIn("opaque-handle", output.call_args.args[0])

    async def test_channel_error_logs_only_allowlisted_diagnostic_reason(self):
        with self.assertLogs("nanoclaw.weixin", level="WARNING") as captured:
            await self.channel._handle_event("channel_error", {
                "code": "protocol_error", "reason": "invalid_messages",
                "message": "provider body with secret-token",
            })
            await self.channel._handle_event("channel_error", {
                "code": "secret-code", "reason": "secret-token",
            })
            await self.channel._handle_event("channel_error", {
                "code": {"secret": "code"}, "reason": ["secret-token"],
            })
        output = "\n".join(captured.output)
        self.assertIn("invalid_messages", output)
        self.assertIn("channel error: unknown", output)
        self.assertNotIn("provider body", output)
        self.assertNotIn("secret-code", output)
        self.assertNotIn("secret-token", output)

    async def test_start_performs_hello_login_then_poll_and_passes_no_parent_secrets(self):
        self.channel.auto_login = True
        with patch.dict("channels.weixin.os.environ", {"NANOCLAW_API_KEY": "parent-secret"}):
            await self.channel._ensure_process()
        start = asyncio.create_task(self.channel.start())
        hello = await self._respond(method="hello", result={"bridge_version": "test"})
        self.assertEqual(hello["method"], "hello")
        login = await self._respond(method="login", result={"account_id": "account"})
        self.assertFalse(login["params"]["force"])
        self.assertEqual(login["params"]["timeout_ms"], 480_000)
        polling = await self._respond(method="start", result={"started": True})
        self.assertEqual(polling["method"], "start")
        await start
        self.assertTrue(self.channel._started)
        kwargs = self.spawn.call_args.kwargs
        self.assertIn("NANOCLAW_WEIXIN_STATE_DIR", kwargs["env"])
        self.assertIn("NANOCLAW_WEIXIN_MEDIA_ROOT", kwargs["env"])
        self.assertNotIn("bot_token", kwargs["env"])
        self.assertNotIn("context_token", kwargs["env"])
        self.assertNotIn("NANOCLAW_API_KEY", kwargs["env"])
        self.assertEqual(
            kwargs["env"]["NANOCLAW_WEIXIN_MAX_LINE_BYTES"],
            str(self.channel.max_ipc_line_bytes),
        )
        self.assertEqual(kwargs["limit"], self.channel.max_ipc_line_bytes)

    async def test_login_passes_configured_deadline_to_bridge(self):
        self.channel.login_timeout_sec = 1.25
        task = asyncio.create_task(self.channel.login(force=True))
        request = await self._respond(method="login", result={"account_id": "account"})
        self.assertTrue(request["params"]["force"])
        self.assertEqual(request["params"]["timeout_ms"], 1250)
        await task

    async def test_crashed_bridge_restarts_and_resumes_polling(self):
        replacement = _Process()
        self.spawn.side_effect = [self.process, replacement]
        self.channel.auto_login = False
        self.channel.restart_backoff_sec = 0
        started = asyncio.create_task(self.channel.start())
        await self._respond(method="hello")
        await self._respond(method="start")
        await started
        await self.process.stdout.queue.put(b"")
        # The restarted process must perform a full persisted-session handshake.
        for method, result in (("hello", {}), ("login", {"account_id": "account"}), ("start", {"started": True})):
            for _ in range(40):
                if replacement.stdin.lines and replacement.stdin.lines[-1]["method"] == method:
                    request = replacement.stdin.lines[-1]
                    await replacement.stdout.queue.put(json.dumps({"v": 1, "type": "response", "id": request["id"], "ok": True, "result": result}).encode() + b"\n")
                    break
                await asyncio.sleep(0)
            else:
                self.fail(f"restart did not request {method}")
        for _ in range(40):
            if self.channel._process is replacement and self.channel._started:
                break
            await asyncio.sleep(0)
        self.assertIs(self.channel._process, replacement)

    async def test_session_expired_forces_login_then_resumes_polling(self):
        self.channel.restart_backoff_sec = 0
        await self.channel._handle_event("session_expired", {"code": -14})
        await self.channel._handle_event("session_expired", {"code": -14})
        login = await self._respond(method="login", result={"account_id": "account"})
        self.assertTrue(login["params"]["force"])
        self.assertEqual(sum(line["method"] == "login" for line in self.process.stdin.lines), 1)
        start = await self._respond(method="start", result={"started": True})
        self.assertEqual(start["method"], "start")
        for _ in range(20):
            if self.channel._started:
                break
            await asyncio.sleep(0)
        self.assertTrue(self.channel._started)

    async def test_stop_cancels_pending_session_reauthentication(self):
        self.channel.restart_backoff_sec = 0
        await self.channel._handle_event("session_expired", {"code": -14})
        # The login response is deliberately withheld: stop must cancel its
        # forced-login waiter before it can schedule a second supervisor.
        for _ in range(20):
            if self.process.stdin.lines and self.process.stdin.lines[-1]["method"] == "login":
                break
            await asyncio.sleep(0)
        else:
            self.fail("reauthentication did not request login")
        stop = asyncio.create_task(self.channel.stop())
        await self._respond(method="stop", result={"stopped": True})
        await stop
        self.assertIsNone(self.channel._supervisor_task)

    async def test_inbound_saves_controlled_image_then_acks(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbound_dir = Path(tmp) / "inbound"
            inbound_dir.mkdir()
            image = inbound_dir / "in.png"
            image.write_bytes(b"image")
            self.channel.state_dir = Path(tmp).resolve()
            self.channel.image_store = _Store()
            handler = asyncio.create_task(self.channel._handle_inbound({"account_id": "account", "user_id": "user", "delivery_id": "d1", "text": "look", "images": [{"file_path": str(image), "mime_type": "image/png"}]}))
            inbound = await asyncio.wait_for(self.bus.consume_inbound(), 1)
            self.assertEqual(inbound.sender_id, encode_weixin_target("account", "user"))
            self.assertFalse(image.exists())
            request = await self._respond()
            self.assertEqual(request["method"], "ack_inbound")
            await handler
            self.assertEqual(self.channel.image_store.saved[0][0], inbound.sender_id)

    async def test_empty_or_unsupported_inbound_is_acked_without_bus_publish(self):
        handler = asyncio.create_task(self.channel._handle_inbound({
            "account_id": "account", "user_id": "user", "delivery_id": "empty", "text": "", "images": [],
        }))
        request = await self._respond(method="ack_inbound")
        self.assertEqual(request["params"]["delivery_id"], "empty")
        await handler
        self.assertTrue(self.bus.inbound_queue.empty())

    async def test_inbound_image_cannot_read_bridge_state_files_and_is_acked(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            sensitive = state_dir / "state.json"
            sensitive.write_bytes(b"not-an-image")
            self.channel.state_dir = state_dir.resolve()
            self.channel.image_store = _Store()
            handler = asyncio.create_task(self.channel._handle_inbound({
                "account_id": "account", "user_id": "user", "delivery_id": "bad-image", "text": "",
                "images": [{"file_path": str(sensitive), "mime_type": "image/png"}],
            }))
            request = await self._respond(method="ack_inbound")
            self.assertEqual(request["params"]["delivery_id"], "bad-image")
            await handler
            self.assertTrue(sensitive.exists())
            self.assertEqual(self.channel.image_store.saved, [])

    async def test_outbound_image_is_staged_in_bridge_controlled_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.png"
            source.write_bytes(b"image")
            self.channel.state_dir = (Path(tmp) / "state").resolve()
            task = asyncio.create_task(self.channel.send(OutboundMessage(
                "weixin", encode_weixin_target("account", "user"), "caption",
                images=[ImageRef("image", str(source), "image/png")], correlation_id="stable",
            )))
            request = await self._respond(method="send_image", result={"success": True, "provider_message_id": "p"})
            staged = Path(request["params"]["file_path"])
            self.assertTrue(staged.is_file())
            self.assertEqual(staged.parent, self.channel.state_dir / "outbound")
            result = await task
            self.assertTrue(result.success)
            self.assertFalse(staged.exists())

    async def test_outbound_directory_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.png"
            source.write_bytes(b"image")
            state = Path(tmp) / "state"
            outside = Path(tmp) / "outside"
            state.mkdir()
            outside.mkdir()
            (state / "outbound").symlink_to(outside, target_is_directory=True)
            self.channel.state_dir = state.resolve()
            result = await self.channel.send(OutboundMessage(
                "weixin", encode_weixin_target("account", "user"), "caption",
                images=[ImageRef("image", str(source), "image/png")],
            ))
            self.assertFalse(result.success)
            self.assertEqual(result.code, "invalid_image")
            self.assertEqual(list(outside.iterdir()), [])

    async def test_unallowed_inbound_is_not_published_but_is_acked(self):
        handler = asyncio.create_task(self.channel._handle_inbound({"account_id": "a", "user_id": "other", "delivery_id": "d", "text": "x"}))
        request = await self._respond(method="ack_inbound")
        self.assertEqual(request["params"]["delivery_id"], "d")
        await handler
        self.assertTrue(self.bus.inbound_queue.empty())

    async def test_timeout_and_stop_are_safe(self):
        self.channel.command_timeout_sec = 0.01
        result = await self.channel.send(OutboundMessage("weixin", encode_weixin_target("a", "user"), "x"))
        self.assertFalse(result.success)
        self.assertEqual(result.code, "timeout")
        await self.channel.stop()
        self.assertIsNone(self.channel._process)


if __name__ == "__main__":
    unittest.main()
