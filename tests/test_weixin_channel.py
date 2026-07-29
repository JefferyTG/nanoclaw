"""Offline contract tests for :mod:`channels.weixin`."""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bus.queue import ImageRef, MessageBus, OutboundMessage
from channels.weixin import WeixinChannel, decode_weixin_target, encode_weixin_target
from agent.imagestore import ImageStore
from agent.tools.vision import AskImageTool


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
        self._responded_request_ids = set()
        self.channel = WeixinChannel(bus=self.bus, bridge_command=["fake"], allowed_user_ids=["user"], state_dir=tempfile.gettempdir(), request_timeout_sec=0.01, stop_timeout_sec=0.01)

    async def asyncTearDown(self):
        await self.channel.stop()

    async def _respond(self, *, method=None, result=None, ok=True, error=None):
        for _ in range(20):
            if self.process.stdin.lines:
                request = next(
                    (item for item in reversed(self.process.stdin.lines)
                     if item["id"] not in self._responded_request_ids
                     and (method is None or item["method"] == method)),
                    None,
                )
                if request is None:
                    await asyncio.sleep(0)
                    continue
                await self.process.stdout.queue.put(json.dumps({"v": 1, "type": "response", "id": request["id"], "ok": ok, "result": result or {}, "error": error}).encode() + b"\n")
                self._responded_request_ids.add(request["id"])
                return request
            await asyncio.sleep(0)
        self.fail("no request")

    async def _deliver_inbound(self, data, *, consume=False):
        task = asyncio.create_task(self.channel._handle_inbound(data))
        inbound = None
        if consume:
            inbound = await asyncio.wait_for(self.bus.consume_inbound(), 1)
        request = await self._respond(method="ack_inbound")
        self.assertEqual(request["params"]["delivery_id"], data["delivery_id"])
        await task
        return inbound

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

    async def test_start_restores_pending_images_before_polling(self):
        restore_entered = asyncio.Event()
        allow_restore = asyncio.Event()

        async def blocked_restore():
            restore_entered.set()
            await allow_restore.wait()

        with patch.object(
            self.channel, "_restore_pending_image_batches", side_effect=blocked_restore
        ):
            start = asyncio.create_task(self.channel.start())
            await self._respond(method="hello")
            await asyncio.wait_for(restore_entered.wait(), 1)
            self.assertNotIn("start", [line["method"] for line in self.process.stdin.lines])
            allow_restore.set()
            await self._respond(method="start", result={"started": True})
            await start

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
            self.assertEqual(
                self.channel.image_store.saved[0][0],
                f"weixin:{inbound.sender_id}",
            )

    async def test_inbound_image_uses_exact_gateway_session_key_and_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            inbound_dir = state / "inbound"
            inbound_dir.mkdir(parents=True)
            image = inbound_dir / "in.png"
            image.write_bytes(b"image")
            self.channel.state_dir = state.resolve()
            self.channel.image_store = ImageStore(str(Path(tmp) / "sessions"))
            self.channel.image_merge_window_sec = 0
            inbound = await self._deliver_inbound({
                "account_id": "account", "user_id": "user", "delivery_id": "resolve",
                "text": "caption", "images": [{"file_path": str(image), "mime_type": "image/png"}],
            }, consume=True)
            self.assertEqual(inbound.sender_id, encode_weixin_target("account", "user"))
            self.assertNotIn(".", inbound.images[0].id)
            session_key = f"weixin:{inbound.sender_id}"
            resolved = self.channel.image_store.resolve(session_key, inbound.images[0].id)
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.path, inbound.images[0].path)
            self.assertEqual(Path(resolved.path).stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(resolved.path).parent.stat().st_mode & 0o777, 0o700)
            content = AskImageTool(
                self.channel.image_store, SimpleNamespace(workspace=tmp)
            )._build_content(
                [inbound.images[0].id], [], "describe", session_key
            )
            self.assertEqual(content[1]["type"], "image_url")
            self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    async def test_image_only_waits_then_publishes_default_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbound_dir = Path(tmp) / "inbound"
            inbound_dir.mkdir()
            image = inbound_dir / "in.png"
            image.write_bytes(b"image")
            self.channel.state_dir = Path(tmp).resolve()
            self.channel.image_store = _Store()
            self.channel.image_merge_window_sec = 0.05
            await self._deliver_inbound({
                "account_id": "account", "user_id": "user", "delivery_id": "image-only",
                "text": "", "images": [{"file_path": str(image), "mime_type": "image/png"}],
            })
            self.assertTrue(self.bus.inbound_queue.empty())
            inbound = await asyncio.wait_for(self.bus.consume_inbound(), 0.5)
            self.assertEqual(inbound.content, "请分析这张图片。")
            self.assertEqual(len(inbound.images), 1)

    async def test_followup_text_merges_pending_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbound_dir = Path(tmp) / "inbound"
            inbound_dir.mkdir()
            image = inbound_dir / "in.png"
            image.write_bytes(b"image")
            self.channel.state_dir = Path(tmp).resolve()
            self.channel.image_store = _Store()
            self.channel.image_merge_window_sec = 1
            await self._deliver_inbound({
                "account_id": "account", "user_id": "user", "delivery_id": "image",
                "text": "", "images": [{"file_path": str(image), "mime_type": "image/png"}],
            })
            inbound = await self._deliver_inbound({
                "account_id": "account", "user_id": "user", "delivery_id": "text",
                "text": "这是什么？", "images": [],
            }, consume=True)
            self.assertEqual(inbound.content, "这是什么？")
            self.assertEqual(len(inbound.images), 1)
            self.assertTrue(self.bus.inbound_queue.empty())

    async def test_consecutive_images_reset_merge_timer(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbound_dir = Path(tmp) / "inbound"
            inbound_dir.mkdir()
            first, second = inbound_dir / "one.png", inbound_dir / "two.png"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            self.channel.state_dir = Path(tmp).resolve()
            self.channel.image_store = _Store()
            self.channel.image_merge_window_sec = 0.15
            await self._deliver_inbound({"account_id": "account", "user_id": "user", "delivery_id": "one", "text": "", "images": [{"file_path": str(first), "mime_type": "image/png"}]})
            await asyncio.sleep(0.05)
            await self._deliver_inbound({"account_id": "account", "user_id": "user", "delivery_id": "two", "text": "", "images": [{"file_path": str(second), "mime_type": "image/png"}]})
            await asyncio.sleep(0.08)
            self.assertTrue(self.bus.inbound_queue.empty())
            inbound = await asyncio.wait_for(self.bus.consume_inbound(), 0.5)
            self.assertEqual(inbound.content, "请分析这些图片。")
            self.assertEqual(len(inbound.images), 2)

    async def test_pending_images_are_isolated_by_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbound_dir = Path(tmp) / "inbound"
            inbound_dir.mkdir()
            image = inbound_dir / "one.png"
            image.write_bytes(b"one")
            self.channel.state_dir = Path(tmp).resolve()
            self.channel.image_store = _Store()
            self.channel.allowed_user_ids = frozenset({"user", "other"})
            self.channel.image_merge_window_sec = 0.05
            await self._deliver_inbound({"account_id": "account", "user_id": "user", "delivery_id": "image", "text": "", "images": [{"file_path": str(image), "mime_type": "image/png"}]})
            other = await self._deliver_inbound({"account_id": "account", "user_id": "other", "delivery_id": "text", "text": "other text", "images": []}, consume=True)
            self.assertEqual(other.content, "other text")
            self.assertEqual(other.sender_id, encode_weixin_target("account", "other"))
            image_message = await asyncio.wait_for(self.bus.consume_inbound(), 0.5)
            self.assertEqual(image_message.sender_id, encode_weixin_target("account", "user"))
            self.assertEqual(len(image_message.images), 1)

    async def test_stop_cancels_pending_image_timer_but_preserves_durable_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            inbound_dir = state / "inbound"
            inbound_dir.mkdir(parents=True)
            image = inbound_dir / "one.png"
            image.write_bytes(b"one")
            self.channel.state_dir = state.resolve()
            self.channel.image_store = ImageStore(str(Path(tmp) / "sessions"))
            self.channel.image_merge_window_sec = 60
            await self._deliver_inbound({"account_id": "account", "user_id": "user", "delivery_id": "pending", "text": "", "images": [{"file_path": str(image), "mime_type": "image/png"}]})
            batch = next(iter(self.channel._pending_image_batches.values()))
            saved_path = Path(batch.images[0].path)
            self.assertTrue(saved_path.exists())
            await self.channel.stop()
            self.assertTrue(saved_path.exists())
            self.assertTrue(self.channel._pending_image_batches)
            pending_path = state / "pending_image_batches.json"
            self.assertTrue(pending_path.exists())
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            self.assertEqual(pending_path.stat().st_mode & 0o777, 0o600)
            await asyncio.sleep(0)
            self.assertTrue(self.bus.inbound_queue.empty())

    async def test_restart_restores_expired_pending_batch_and_flushes(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            inbound_dir = state / "inbound"
            inbound_dir.mkdir(parents=True)
            image = inbound_dir / "one.png"
            image.write_bytes(b"one")
            store = ImageStore(str(Path(tmp) / "sessions"))
            self.channel.state_dir = state.resolve()
            self.channel.image_store = store
            self.channel.image_merge_window_sec = 60
            await self._deliver_inbound({"account_id": "account", "user_id": "user", "delivery_id": "persisted", "text": "", "images": [{"file_path": str(image), "mime_type": "image/png"}]})
            batch = next(iter(self.channel._pending_image_batches.values()))
            batch.deadline_ms = int(time.time() * 1000) - 1
            self.channel._persist_pending_image_batches_locked()
            restored_bus = MessageBus()
            restored = WeixinChannel(
                bus=restored_bus, bridge_command=["fake"], state_dir=state,
                allowed_user_ids=["user"], image_store=store,
            )
            restore = asyncio.create_task(restored._restore_pending_image_batches())
            inbound = await asyncio.wait_for(restored_bus.consume_inbound(), 0.5)
            await restore
            self.assertEqual(inbound.content, "请分析这张图片。")
            self.assertEqual(inbound.sender_id, encode_weixin_target("account", "user"))
            self.assertTrue(store.resolve(f"weixin:{inbound.sender_id}", inbound.images[0].id))
            self.assertFalse((state / "pending_image_batches.json").exists())

    async def test_redelivered_pending_delivery_is_deduplicated_and_temp_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbound_dir = Path(tmp) / "inbound"
            inbound_dir.mkdir()
            first, duplicate = inbound_dir / "one.png", inbound_dir / "redelivery.png"
            first.write_bytes(b"one")
            duplicate.write_bytes(b"duplicate")
            self.channel.state_dir = Path(tmp).resolve()
            self.channel.image_store = _Store()
            self.channel.image_merge_window_sec = 60
            event = {"account_id": "account", "user_id": "user", "delivery_id": "same", "text": "", "images": [{"file_path": str(first), "mime_type": "image/png"}]}
            await self._deliver_inbound(event)
            duplicate_event = {**event, "images": [{"file_path": str(duplicate), "mime_type": "image/png"}]}
            await self._deliver_inbound(duplicate_event)
            batch = next(iter(self.channel._pending_image_batches.values()))
            self.assertEqual(len(batch.images), 1)
            self.assertEqual(len(self.channel.image_store.saved), 1)
            self.assertFalse(duplicate.exists())
            self.assertTrue(self.bus.inbound_queue.empty())

    async def test_persist_failure_does_not_ack_or_poison_redelivery_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbound_dir = Path(tmp) / "inbound"
            inbound_dir.mkdir()
            first, retry = inbound_dir / "first.png", inbound_dir / "retry.png"
            first.write_bytes(b"first")
            self.channel.state_dir = Path(tmp).resolve()
            self.channel.image_store = _Store()
            self.channel.image_merge_window_sec = 60
            event = {"account_id": "account", "user_id": "user", "delivery_id": "retryable", "text": "", "images": [{"file_path": str(first), "mime_type": "image/png"}]}
            with patch.object(self.channel, "_persist_pending_image_batches_locked", side_effect=OSError("disk full")):
                await self.channel._handle_inbound(event)
            self.assertFalse(self.process.stdin.lines)
            self.assertFalse(self.channel._pending_image_batches)
            self.assertTrue(self.bus.inbound_queue.empty())
            retry.write_bytes(b"retry")
            await self._deliver_inbound({**event, "images": [{"file_path": str(retry), "mime_type": "image/png"}]})
            batch = next(iter(self.channel._pending_image_batches.values()))
            self.assertEqual(batch.delivery_ids, {"retryable"})
            self.assertEqual(len(batch.images), 1)
            self.assertEqual(len(self.channel.image_store.saved), 2)

    async def test_restore_expired_batch_keeps_other_durable_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            inbound_dir = state / "inbound"
            inbound_dir.mkdir(parents=True)
            first, second = inbound_dir / "one.png", inbound_dir / "two.png"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            store = ImageStore(str(Path(tmp) / "sessions"))
            self.channel.state_dir = state.resolve()
            self.channel.image_store = store
            self.channel.allowed_user_ids = frozenset({"user", "other"})
            self.channel.image_merge_window_sec = 60
            await self._deliver_inbound({"account_id": "account", "user_id": "user", "delivery_id": "expired", "text": "", "images": [{"file_path": str(first), "mime_type": "image/png"}]})
            await self._deliver_inbound({"account_id": "account", "user_id": "other", "delivery_id": "future", "text": "", "images": [{"file_path": str(second), "mime_type": "image/png"}]})
            self.channel._pending_image_batches[("account", "user")].deadline_ms = int(time.time() * 1000) - 1
            self.channel._persist_pending_image_batches_locked()
            restored_bus = MessageBus()
            restored = WeixinChannel(bus=restored_bus, bridge_command=["fake"], state_dir=state, allowed_user_ids=["user", "other"], image_store=store)
            restore = asyncio.create_task(restored._restore_pending_image_batches())
            inbound = await asyncio.wait_for(restored_bus.consume_inbound(), 0.5)
            await restore
            self.assertEqual(inbound.sender_id, encode_weixin_target("account", "user"))
            with open(state / "pending_image_batches.json", encoding="utf-8") as source:
                remaining = json.load(source)["batches"]
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["delivery_ids"], ["future"])
            await restored.stop()

    async def test_durable_batch_is_retained_until_bus_consumer_accepts_flush(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            inbound_dir = state / "inbound"
            inbound_dir.mkdir(parents=True)
            image = inbound_dir / "one.png"
            image.write_bytes(b"one")
            self.channel.state_dir = state.resolve()
            self.channel.image_store = ImageStore(str(Path(tmp) / "sessions"))
            self.channel.image_merge_window_sec = 60
            await self._deliver_inbound({"account_id": "account", "user_id": "user", "delivery_id": "image", "text": "", "images": [{"file_path": str(image), "mime_type": "image/png"}]})

            handler = asyncio.create_task(self.channel._handle_inbound({
                "account_id": "account", "user_id": "user",
                "delivery_id": "caption", "text": "describe", "images": [],
            }))
            for _ in range(40):
                if not self.bus.inbound_queue.empty():
                    break
                await asyncio.sleep(0)
            else:
                self.fail("merged image was not handed to MessageBus")
            self.assertTrue((state / "pending_image_batches.json").exists())
            self.assertTrue(self.channel._pending_image_batches)
            inbound = await self.bus.consume_inbound()
            request = await self._respond(method="ack_inbound")
            self.assertEqual(request["params"]["delivery_id"], "caption")
            await handler
            self.assertEqual(inbound.content, "describe")
            self.assertFalse((state / "pending_image_batches.json").exists())
            self.assertFalse(self.channel._pending_image_batches)

    async def test_stop_cancels_unaccepted_bus_handoff_without_losing_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            inbound_dir = state / "inbound"
            inbound_dir.mkdir(parents=True)
            image = inbound_dir / "one.png"
            image.write_bytes(b"one")
            self.channel.state_dir = state.resolve()
            self.channel.image_store = ImageStore(str(Path(tmp) / "sessions"))
            self.channel.image_merge_window_sec = 60
            await self._deliver_inbound({"account_id": "account", "user_id": "user", "delivery_id": "image", "text": "", "images": [{"file_path": str(image), "mime_type": "image/png"}]})
            self.channel._spawn(self.channel._handle_inbound({
                "account_id": "account", "user_id": "user",
                "delivery_id": "caption", "text": "describe", "images": [],
            }))
            for _ in range(40):
                if not self.bus.inbound_queue.empty():
                    break
                await asyncio.sleep(0)
            else:
                self.fail("merged image was not handed to MessageBus")

            stop = asyncio.create_task(self.channel.stop())
            await self._respond(method="stop", result={"stopped": True})
            await asyncio.wait_for(stop, 0.5)
            self.assertTrue((state / "pending_image_batches.json").exists())
            self.assertTrue(self.channel._pending_image_batches)

    async def test_failed_pending_restore_can_be_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            pending = state / "pending_image_batches.json"
            pending.write_text("not-json", encoding="utf-8")
            self.channel.state_dir = state.resolve()
            self.channel.image_store = ImageStore(str(Path(tmp) / "sessions"))
            with self.assertRaises(json.JSONDecodeError):
                await self.channel._restore_pending_image_batches()
            self.assertFalse(self.channel._pending_batches_restored)
            pending.write_text('{"version":1,"batches":[]}', encoding="utf-8")
            await self.channel._restore_pending_image_batches()
            self.assertTrue(self.channel._pending_batches_restored)

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
