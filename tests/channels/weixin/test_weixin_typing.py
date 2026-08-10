"""Offline lifecycle tests for Weixin native typing."""

import asyncio
import tempfile
import unittest

from bus.queue import InboundMessage, MessageBus
from channels.weixin import WeixinBridgeError, WeixinChannel, encode_weixin_target
from gateway import Gateway


class _FakeBridge:
    def __init__(self, *, fail_typing=False):
        self.calls = []
        self.typing_started = asyncio.Event()
        self.reply_sent = asyncio.Event()
        self.typing_stopped = asyncio.Event()
        self.fail_typing = fail_typing
        self.active_typing_ids = set()
        self.typing_stop_ids = []
        self.native_cancel_calls = 0
        self.native_cancelled = asyncio.Event()

    async def request(self, method, params, *, timeout=None):
        self.calls.append((method, dict(params)))
        if method == "typing_start":
            self.active_typing_ids.add(params["activity_id"])
            self.typing_started.set()
            if self.fail_typing:
                raise WeixinBridgeError("typing_unavailable")
            return {"active": True}
        if method == "send_text":
            self.reply_sent.set()
            return {"success": True, "retryable": False, "code": "ok"}
        if method == "typing_stop":
            self.typing_stop_ids.append(params["activity_id"])
            self.active_typing_ids.discard(params["activity_id"])
            if not self.active_typing_ids:
                self.native_cancel_calls += 1
                self.native_cancelled.set()
            self.typing_stopped.set()
            return {"active": False}
        raise AssertionError(f"unexpected Bridge method: {method}")


class _Agent:
    def __init__(self, bridge, *, fail=False, wait_for_typing=True):
        self.bridge = bridge
        self.fail = fail
        self.wait_for_typing = wait_for_typing
        self.started = asyncio.Event()

    async def run(self, content, images=None, stream_sink=None):
        self.started.set()
        if self.wait_for_typing:
            await self.bridge.typing_started.wait()
        if self.fail:
            raise RuntimeError("agent failure")
        return "final reply"


class WeixinTypingLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def _make_gateway(self, agent, bridge):
        state = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_directory, state)
        channel = WeixinChannel(
            bus=MessageBus(),
            bridge_command=["fake"],
            state_dir=state.name,
            allowed_user_ids=["user"],
            request_timeout_sec=0.05,
            stop_timeout_sec=0.1,
        )
        channel.typing_start_delay_sec = 0
        channel._request = bridge.request
        gateway = Gateway(
            channel.bus,
            [channel],
            lambda _session_key: agent,
        )
        return state, channel, gateway

    @staticmethod
    async def _cleanup_directory(directory):
        directory.cleanup()

    async def _run_one(self, *, fail=False, wait_for_typing=True):
        bridge = _FakeBridge(fail_typing=fail)
        agent = _Agent(bridge, fail=fail, wait_for_typing=wait_for_typing)
        state, channel, gateway = await self._make_gateway(agent, bridge)
        inbound_task = asyncio.create_task(gateway._process_inbound())
        outbound_task = asyncio.create_task(gateway._dispatch_outbound())
        target = encode_weixin_target("account", "user")
        await gateway.bus.publish_inbound(
            InboundMessage("weixin", target, target, "do work")
        )
        await asyncio.wait_for(agent.started.wait(), 1)
        await asyncio.wait_for(bridge.reply_sent.wait(), 1)
        await asyncio.wait_for(bridge.typing_stopped.wait(), 1)
        inbound_task.cancel()
        outbound_task.cancel()
        await asyncio.gather(inbound_task, outbound_task, return_exceptions=True)
        await gateway.shutdown()
        return state, channel, bridge

    async def test_final_reply_is_sent_before_typing_cancel(self):
        _state, _channel, bridge = await self._run_one()
        methods = [method for method, _params in bridge.calls]
        self.assertLess(methods.index("send_text"), methods.index("typing_stop"))
        self.assertEqual(methods.count("typing_start"), 1)
        self.assertEqual(methods.count("typing_stop"), 1)
        for _method, params in bridge.calls:
            self.assertNotIn("typing_ticket", params)
            self.assertNotIn("context_token", params)

    async def test_typing_failure_does_not_change_normal_reply_delivery(self):
        _state, _channel, bridge = await self._run_one(fail=True)
        methods = [method for method, _params in bridge.calls]
        self.assertIn("send_text", methods)
        self.assertIn("typing_stop", methods)
        self.assertLess(methods.index("send_text"), methods.index("typing_stop"))

    async def test_overlapping_activities_each_release_bridge_id_before_final_cancel(self):
        bridge = _FakeBridge()
        _state, channel, _gateway = await self._make_gateway(
            _Agent(bridge, wait_for_typing=False), bridge
        )
        target = encode_weixin_target("account", "user")

        first = channel.begin_activity(target)
        second = channel.begin_activity(target)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        await asyncio.sleep(0)
        for _ in range(100):
            if sum(method == "typing_start" for method, _ in bridge.calls) == 2:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("both overlapping typing activities did not start")

        first.close()
        second.close()
        await asyncio.wait_for(bridge.native_cancelled.wait(), 1)

        self.assertEqual(
            set(bridge.typing_stop_ids),
            {first.activity_id, second.activity_id},
        )
        self.assertEqual(len(bridge.typing_stop_ids), 2)
        self.assertEqual(bridge.active_typing_ids, set())
        self.assertEqual(bridge.native_cancel_calls, 1)
        await channel.stop()

    async def test_cancelled_agent_turn_releases_typing_without_outbound_reply(self):
        bridge = _FakeBridge()
        started = asyncio.Event()

        class BlockingAgent(_Agent):
            async def run(self, content, images=None, stream_sink=None):
                self.started.set()
                await self.bridge.typing_started.wait()
                started.set()
                await asyncio.Event().wait()

        agent = BlockingAgent(bridge)
        _state, channel, gateway = await self._make_gateway(agent, bridge)
        inbound_task = asyncio.create_task(gateway._process_inbound())
        outbound_task = asyncio.create_task(gateway._dispatch_outbound())
        target = encode_weixin_target("account", "user")
        await gateway.bus.publish_inbound(
            InboundMessage("weixin", target, target, "cancel me")
        )
        await asyncio.wait_for(started.wait(), 1)
        gateway._cancel_session(f"weixin:{target}")
        await asyncio.wait_for(bridge.typing_stopped.wait(), 1)
        self.assertFalse(any(method == "send_text" for method, _ in bridge.calls))
        self.assertNotIn(f"weixin:{target}", gateway._active_tasks)
        inbound_task.cancel()
        outbound_task.cancel()
        await asyncio.gather(inbound_task, outbound_task, return_exceptions=True)
        await gateway.shutdown()

    async def test_gateway_shutdown_releases_active_typing(self):
        bridge = _FakeBridge()

        class ShutdownAgent(_Agent):
            async def run(self, content, images=None, stream_sink=None):
                self.started.set()
                await self.bridge.typing_started.wait()
                await asyncio.Event().wait()

        agent = ShutdownAgent(bridge)
        _state, _channel, gateway = await self._make_gateway(agent, bridge)
        inbound_task = asyncio.create_task(gateway._process_inbound())
        outbound_task = asyncio.create_task(gateway._dispatch_outbound())
        target = encode_weixin_target("account", "user")
        await gateway.bus.publish_inbound(
            InboundMessage("weixin", target, target, "shutdown me")
        )
        await asyncio.wait_for(agent.started.wait(), 1)
        await gateway.shutdown()
        await asyncio.wait_for(bridge.typing_stopped.wait(), 1)
        self.assertFalse(any(method == "send_text" for method, _ in bridge.calls))
        inbound_task.cancel()
        outbound_task.cancel()
        await asyncio.gather(inbound_task, outbound_task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
