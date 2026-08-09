"""TASK-005 单测：上下文预算动态配置 + 占用显示。

覆盖四块：
1. 配置读取/回退：config.json 含 context_budget_tokens 时生效；缺字段回退
   默认 524288 且不报错（向后兼容）。
2. usage 流事件：AgentLoop 每次模型响应后向 stream_sink 推送
   {type:"usage", input_tokens/cached/uncached/budget/ratio/cache_ratio}；
   真实 usage 缺失时回退估算。
3. /context 命令：web / feishu / weixin / cli 四渠道命中后直接从回调查询
   并回复文本（不经过模型）；未注入回调时优雅降级。
4. get_context_usage()：返回 System/历史估算、上次真实 usage、预算、占用比。
"""

import asyncio
import contextlib
import io
import json
import re
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from bus.queue import MessageBus, OutboundMessage
from channels.cli import CLIChannel
from channels.feishu import FeishuChannel
from channels.web import WebChannel
from channels.weixin import WeixinChannel, encode_weixin_target
from config import NanoClawConfig, load_config
from main import format_context_usage
from agent.context import ContextBuilder
from agent.loop import AgentLoop
from agent.memory import ContextCompactor
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from providers.base import LLMProvider, LLMResponse, ToolCallRequest
from providers.usage import PromptCacheUsage
from session.manager import SessionManager


# —— /context 回复格式（main.format_context_usage）——

class FormatContextUsageTests(unittest.TestCase):
    def test_formats_budget_input_ratio_and_cache_hit(self):
        usage = {
            "budget": 524_288,
            "system_tokens": 2_100,
            "history_tokens": 41_000,
            "estimate_total": 43_100,
            "ratio": 43_100 / 524_288,
            "last_usage": {
                "input_tokens": 45_230,
                "cached": 37_000,
                "uncached": 8_230,
                "cache_ratio": 37_000 / 45_230,
                "availability": "available",
            },
        }
        text = format_context_usage(usage)
        self.assertIn("当前会话上下文占用", text)
        self.assertIn("512.0k（524,288 tokens）", text)
        self.assertIn("45,230", text)
        self.assertIn("缓存命中 82%", text)
        self.assertIn("上一回合 input_tokens", text)
        self.assertIn("System ~2.1k + 历史 ~40.0k", text)
        self.assertIn("8.2%", text)

    def test_formats_turn_text_with_calls(self):
        usage = {
            "budget": 524_288,
            "system_tokens": 2_000,
            "history_tokens": 1_000,
            "estimate_total": 3_000,
            "ratio": 3_000 / 524_288,
            "last_usage": {
                "input_tokens": 45_230,
                "cached": 37_000,
                "uncached": 8_230,
                "cache_ratio": 37_000 / 45_230,
                "availability": "available",
                "calls": 3,
            },
        }
        text = format_context_usage(usage)
        self.assertIn("上一回合 input_tokens：45,230", text)
        self.assertIn("调用 3 次", text)
        self.assertIn("缓存命中 82%", text)

    def test_formats_without_real_usage_gracefully(self):
        usage = {
            "budget": 524_288,
            "system_tokens": 2_000,
            "history_tokens": 0,
            "estimate_total": 2_000,
            "ratio": 2_000 / 524_288,
            "last_usage": None,
        }
        text = format_context_usage(usage)
        self.assertIn("暂无真实数据", text)
        self.assertIn("占用比：约 0.4%", text)

    def test_formats_without_budget(self):
        usage = {
            "budget": None,
            "system_tokens": 1_000,
            "history_tokens": 500,
            "estimate_total": 1_500,
            "ratio": None,
            "last_usage": None,
        }
        text = format_context_usage(usage)
        self.assertIn("未启用上下文压缩", text)
        self.assertIn("估算：System ~1000 + 历史 ~500", text)


# —— 配置读取 / 回退 ——

class ContextBudgetConfigTests(unittest.TestCase):
    def test_default_budget_is_512k(self):
        self.assertEqual(NanoClawConfig().context_budget_tokens, 524288)

    def test_config_file_overrides_budget(self):
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as f:
            json.dump({"context_budget_tokens": 100_000}, f)
            f.flush()
            self.assertEqual(load_config(f.name).context_budget_tokens, 100_000)

    def test_missing_budget_falls_back_without_error(self):
        # 旧 config.json 无该字段：回退默认 512k，且不报错（向后兼容）
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as f:
            json.dump({"model": "old-model"}, f)
            f.flush()
            cfg = load_config(f.name)
            self.assertEqual(cfg.context_budget_tokens, 524288)
            self.assertEqual(cfg.model, "old-model")

    def test_missing_config_file_keeps_default(self):
        cfg = load_config("/nonexistent/nano/config.json")
        self.assertEqual(cfg.context_budget_tokens, 524288)

    def test_budget_flows_into_context_compactor(self):
        # ContextCompactor 构造器按 token_budget 生效（main.py factory 装配的等价路径）
        compactor = ContextCompactor(None, "/tmp", token_budget=524_288)  # type: ignore[arg-type]
        self.assertEqual(compactor.token_budget, 524_288)


# —— 测试替身 ——

class _EchoTool(Tool):
    name = "echo"
    description = "echo"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        return "echoed"


class _RecordingProvider(LLMProvider):
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def chat(self, messages, tools=None, model=None, **kwargs):
        self.requests.append({
            "messages": deepcopy(messages),
            "tools": deepcopy(tools),
            "model": model,
        })
        return self.responses.pop(0)


def _usage(input_tokens: int, cached_tokens: int) -> PromptCacheUsage:
    return PromptCacheUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        uncached_input_tokens=input_tokens - cached_tokens,
        cache_ratio=cached_tokens / input_tokens,
        availability="available",
    )


def _make_loop(tmp, provider, *, budget=524_288, sink=None):
    sessions = SessionManager(tmp)
    tools = ToolRegistry()
    tools.freeze()
    compactor = ContextCompactor(provider, tmp, token_budget=budget)
    return AgentLoop(
        provider, tools, ContextBuilder(tmp), sessions,
        session_key="cli:ctx", compactor=compactor,
    )


# —— usage 流事件 ——

class UsageEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_event_pushed_after_each_model_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = _RecordingProvider([
                LLMResponse(content="hello", cache_usage=_usage(45_230, 37_000)),
                LLMResponse(content="world", cache_usage=_usage(45_500, 37_300)),
            ])
            events = []
            async def sink(event):
                events.append(event)
            loop = _make_loop(tmp, provider, sink=sink)
            await loop.run("first", stream_sink=sink)
            await loop.run("second", stream_sink=sink)

            usage_events = [e for e in events if e.get("type") == "usage"]
            self.assertEqual(len(usage_events), 2)
            first = usage_events[0]
            self.assertEqual(first["input_tokens"], 45_230)
            self.assertEqual(first["cached"], 37_000)
            self.assertEqual(first["uncached"], 8_230)
            self.assertEqual(first["budget"], 524_288)
            self.assertAlmostEqual(first["cache_ratio"], 37_000 / 45_230)
            self.assertAlmostEqual(first["ratio"], 45_230 / 524_288)
            self.assertIsNone(first["estimate"])

    async def test_usage_event_falls_back_to_estimate_without_real_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = _RecordingProvider([
                LLMResponse(content="ok", cache_usage=PromptCacheUsage()),
            ])
            events = []
            async def sink(event):
                events.append(event)
            loop = _make_loop(tmp, provider, sink=sink)
            await loop.run("first", stream_sink=sink)

            usage_events = [e for e in events if e.get("type") == "usage"]
            self.assertEqual(len(usage_events), 1)
            first = usage_events[0]
            self.assertIsNone(first.get("input_tokens"))
            self.assertIsInstance(first["estimate"], int)
            self.assertGreater(first["estimate"], 0)
            self.assertEqual(first["budget"], 524_288)
            self.assertIsInstance(first["ratio"], float)

    async def test_no_usage_event_without_stream_sink(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = _RecordingProvider([
                LLMResponse(content="ok", cache_usage=_usage(100, 50)),
            ])
            loop = _make_loop(tmp, provider, sink=None)
            await loop.run("first")
            # last_cache_metrics 仍在回合结束后生成（观测链不变）
            self.assertIsNotNone(loop.last_cache_metrics)
            self.assertEqual(loop.last_cache_metrics.input_tokens, 100)
            # 无 sink：不推送任何回合汇总事件，但回合观测照常收敛
            self.assertEqual(loop.last_cache_metrics.calls, 1)

    async def test_usage_turn_event_pushed_after_turn_with_multiple_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(tmp)
            tools = ToolRegistry()
            tools.register(_EchoTool())
            tools.freeze()
            provider = _RecordingProvider([
                LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest(
                        "call-1", "echo", {}, reasoning_content="r"
                    )],
                    finish_reason="tool_calls",
                    cache_usage=_usage(45_230, 37_000),
                ),
                LLMResponse(content="final", cache_usage=_usage(45_500, 37_300)),
            ])
            compactor = ContextCompactor(provider, tmp, token_budget=524_288)
            events = []

            async def sink(event):
                events.append(event)

            loop = AgentLoop(
                provider, tools, ContextBuilder(tmp), sessions,
                session_key="cli:ctx", compactor=compactor,
            )
            await loop.run("multi-call", stream_sink=sink)

            turn_events = [e for e in events if e.get("type") == "usage_turn"]
            self.assertEqual(len(turn_events), 1)
            turn = turn_events[0]
            self.assertEqual(turn["turn"], 1)
            self.assertEqual(turn["calls"], 2)  # 整轮模型调用 2 次
            # 回合累计（两次调用之和）与单次 usage 事件区分
            self.assertEqual(turn["input_tokens"], 45_230 + 45_500)
            self.assertEqual(turn["cached"], 37_000 + 37_300)
            self.assertEqual(turn["uncached"], (45_230 - 37_000) + (45_500 - 37_300))
            self.assertAlmostEqual(
                turn["cache_ratio"], (37_000 + 37_300) / (45_230 + 45_500)
            )
            self.assertEqual(turn["availability"], "available")
            self.assertEqual(turn["budget"], 524_288)
            # 占用语义沿用最近一次调用（45_500），而非回合累计
            last = turn["last_usage"]
            self.assertIsNotNone(last)
            self.assertEqual(last["input_tokens"], 45_500)
            self.assertAlmostEqual(last["cache_ratio"], 37_300 / 45_500)
            self.assertAlmostEqual(turn["last_ratio"], 45_500 / 524_288)
            self.assertAlmostEqual(turn["ratio"], 45_500 / 524_288)
            # 逐次 usage 事件仍在推送（实时刷新不被破坏）
            usage_events = [e for e in events if e.get("type") == "usage"]
            self.assertEqual(len(usage_events), 2)
            self.assertNotEqual(turn["input_tokens"], usage_events[-1]["input_tokens"])

    async def test_usage_turn_event_single_call_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = _RecordingProvider([
                LLMResponse(content="ok", cache_usage=_usage(100, 50)),
            ])
            events = []

            async def sink(event):
                events.append(event)

            loop = _make_loop(tmp, provider, sink=sink)
            await loop.run("first", stream_sink=sink)

            turn_events = [e for e in events if e.get("type") == "usage_turn"]
            self.assertEqual(len(turn_events), 1)
            turn = turn_events[0]
            self.assertEqual(turn["calls"], 1)
            self.assertEqual(turn["input_tokens"], 100)
            self.assertEqual(turn["last_usage"]["input_tokens"], 100)


# —— get_context_usage ——

class GetContextUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_before_any_turn_returns_estimates_without_real_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = _RecordingProvider([])
            loop = _make_loop(tmp, provider, budget=524_288)
            usage = loop.get_context_usage()
            self.assertEqual(usage["budget"], 524_288)
            self.assertIsInstance(usage["system_tokens"], int)
            self.assertEqual(usage["history_tokens"], 0)
            self.assertGreater(usage["estimate_total"], 0)
            self.assertIsInstance(usage["ratio"], float)
            self.assertIsNone(usage["last_usage"])

    async def test_after_turn_contains_last_real_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = _RecordingProvider([
                LLMResponse(content="hello", cache_usage=_usage(45_230, 37_000)),
            ])
            loop = _make_loop(tmp, provider, budget=524_288)
            await loop.run("first")
            usage = loop.get_context_usage()
            last = usage["last_usage"]
            self.assertIsNotNone(last)
            self.assertEqual(last["input_tokens"], 45_230)
            self.assertEqual(last["cached"], 37_000)
            self.assertEqual(last["uncached"], 8_230)
            self.assertAlmostEqual(last["cache_ratio"], 37_000 / 45_230)
            self.assertEqual(last["availability"], "available")
            self.assertEqual(last["calls"], 1)
            self.assertEqual(usage["budget"], 524_288)
            self.assertIsInstance(usage["ratio"], float)

    async def test_without_memory_consolidator_returns_none_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionManager(tmp)
            tools = ToolRegistry()
            tools.freeze()
            provider = _RecordingProvider([])
            loop = AgentLoop(
                provider, tools, ContextBuilder(tmp), sessions,
                session_key="cli:ctx", compactor=None,
            )
            usage = loop.get_context_usage()
            self.assertIsNone(usage["budget"])
            self.assertIsNone(usage["ratio"])
            # 无压缩器时仍可给出估算（get_context_usage 不依赖 memory）
            self.assertIsInstance(usage["system_tokens"], int)


# —— /context 命令（四渠道）——

class _FakeImageStore:
    """WebChannel 构造所需的最小 image_store 占位。"""
    def resolve(self, session_key, image_id):
        return None


class WebContextCommandTests(unittest.TestCase):
    def _channel(self):
        return WebChannel(
            "web", MessageBus(), "127.0.0.1", 0, NanoClawConfig(), "config.json",
            session_manager=None, image_store=_FakeImageStore(),
        )

    def test_context_command_replies_from_callback(self):
        channel = self._channel()
        seen = []
        channel._context_callback = lambda key: (seen.append(key), "📊 测试占用")[1]
        reply = channel._handle_command("ws-test", "/context")
        self.assertIn("📊 测试占用", reply)
        self.assertEqual(seen, ["web:ws-test:0"])

    def test_context_command_without_callback_degrades(self):
        channel = self._channel()
        channel._context_callback = None
        reply = channel._handle_command("ws-test", "/context")
        self.assertIn("未注入", reply)

    def test_non_command_still_returns_none(self):
        channel = self._channel()
        channel._context_callback = lambda key: "x"
        self.assertIsNone(channel._handle_command("ws-test", "你好"))


class FeishuContextCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_command_replies_from_callback(self):
        bus = MessageBus()
        channel = FeishuChannel("feishu", bus, "id", "secret", None)
        channel._loop = asyncio.get_running_loop()
        seen = []
        channel._context_callback = lambda key: (seen.append(key), "📊 测试占用")[1]
        handled = channel._try_handle_command("chat-1", "/context")
        self.assertTrue(handled)
        self.assertEqual(seen, ["feishu:chat-1:0"])
        reply = await asyncio.wait_for(bus.consume_outbound(), 1)
        self.assertEqual(reply.chat_id, "chat-1")
        self.assertIn("📊 测试占用", reply.content)

    async def test_context_command_without_callback_degrades(self):
        bus = MessageBus()
        channel = FeishuChannel("feishu", bus, "id", "secret", None)
        channel._loop = asyncio.get_running_loop()
        handled = channel._try_handle_command("chat-1", "/context")
        self.assertTrue(handled)
        reply = await asyncio.wait_for(bus.consume_outbound(), 1)
        self.assertIn("未注入", reply.content)


class WeixinContextCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from unittest.mock import patch
        self.process = _FakeProcess()
        self.patcher = patch(
            "channels.weixin.asyncio.create_subprocess_exec",
            return_value=self.process,
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.bus = MessageBus()
        self.channel = WeixinChannel(
            bus=self.bus, bridge_command=["fake"], allowed_user_ids=["user"],
            state_dir="/tmp/weixin-ctx-test", request_timeout_sec=0.01,
        )

    async def asyncTearDown(self):
        await self.channel.stop()

    async def _respond_ack(self, delivery_id):
        for _ in range(20):
            if self.process.stdin.lines:
                request = next(
                    (item for item in reversed(self.process.stdin.lines)
                     if item["method"] == "ack_inbound"),
                    None,
                )
                if request is None:
                    await asyncio.sleep(0)
                    continue
                await self.process.stdout.queue.put(
                    json.dumps({"v": 1, "type": "response", "id": request["id"],
                                "ok": True, "result": {}, "error": None}).encode()
                    + b"\n"
                )
                return
            await asyncio.sleep(0)

    async def test_context_command_replies_from_callback_without_agent(self):
        target = encode_weixin_target("account", "user")
        seen = []
        self.channel._context_callback = lambda key: (seen.append(key), "📊 测试占用")[1]
        event = {
            "account_id": "account", "user_id": "user", "delivery_id": "ctx",
            "text": "/context", "images": [],
        }
        handler = asyncio.create_task(self.channel._handle_inbound(event))
        reply = await asyncio.wait_for(self.bus.consume_outbound(), 1)
        self.assertEqual(reply.chat_id, target)
        self.assertIn("📊 测试占用", reply.content)
        # 命令直接回复，绝不投递给 Agent / Gateway
        self.assertTrue(self.bus.inbound_queue.empty())
        self.assertEqual(seen, [f"weixin:{target}"])
        await self._respond_ack("ctx")
        await handler

    async def test_context_command_without_callback_degrades(self):
        target = encode_weixin_target("account", "user")
        event = {
            "account_id": "account", "user_id": "user", "delivery_id": "ctx2",
            "text": "/context", "images": [],
        }
        handler = asyncio.create_task(self.channel._handle_inbound(event))
        reply = await asyncio.wait_for(self.bus.consume_outbound(), 1)
        self.assertEqual(reply.chat_id, target)
        self.assertIn("未注入", reply.content)
        self.assertTrue(self.bus.inbound_queue.empty())
        await self._respond_ack("ctx2")
        await handler

    async def test_failed_context_callback_replies_generic_message(self):
        target = encode_weixin_target("account", "user")

        def broken(_key):
            raise RuntimeError("secret-detail")

        self.channel._context_callback = broken
        event = {
            "account_id": "account", "user_id": "user", "delivery_id": "ctx3",
            "text": "/context", "images": [],
        }
        handler = asyncio.create_task(self.channel._handle_inbound(event))
        reply = await asyncio.wait_for(self.bus.consume_outbound(), 1)
        self.assertEqual(reply.chat_id, target)
        self.assertIn("失败", reply.content)
        self.assertNotIn("secret-detail", reply.content)
        await self._respond_ack("ctx3")
        await handler


class CliContextCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_command_prints_callback_reply(self):
        channel = CLIChannel(MessageBus())
        seen = []
        channel._context_callback = lambda key: (seen.append(key), "📊 测试占用")[1]
        lines = ["/context", "/exit"]

        async def fake_read_line(_self):
            if lines:
                return lines.pop(0)
            raise EOFError()

        buf = io.StringIO()
        with patch.object(CLIChannel, "_read_line", fake_read_line):
            with contextlib.redirect_stdout(buf):
                await channel.start()
        self.assertEqual(seen, ["cli:local0"])
        self.assertIn("📊 测试占用", buf.getvalue())

    async def test_context_command_without_callback_degrades(self):
        channel = CLIChannel(MessageBus())
        lines = ["/context", "/exit"]

        async def fake_read_line(_self):
            if lines:
                return lines.pop(0)
            raise EOFError()

        buf = io.StringIO()
        with patch.object(CLIChannel, "_read_line", fake_read_line):
            with contextlib.redirect_stdout(buf):
                await channel.start()
        self.assertIn("未注入", buf.getvalue())


# —— 微信 Bridge 文本直通对齐（纯文本 /context 才拦截）——
class _FakeProcess:
    class _Stdin:
        def __init__(self):
            self.lines = []

        async def drain(self):
            pass

        def write(self, data):
            self.lines.append(json.loads(data.decode()))

    class _Stdout:
        def __init__(self):
            self.queue = asyncio.Queue()

        def readline(self):
            return self.queue.get()

    def __init__(self):
        self.stdin = _FakeProcess._Stdin()
        self.stdout = _FakeProcess._Stdout()
        self.stderr = _FakeProcess._Stdout()
        self.returncode = None

    def kill(self):
        pass

    def terminate(self):
        self.returncode = 0

    async def wait(self):
        return 0

    def send_signal(self, _sig):
        pass


class WeixinBridgeAlignmentTests(unittest.IsolatedAsyncioTestCase):
    """验证 /context 识别与既有 Bridge 文本直通命令完全对齐（纯文本精确匹配）。"""

    def test_command_set_contains_context(self):
        # 命令集合应包含 /context；与 /bind-reminders、/new 同类（纯文本精确命令）
        import inspect
        source = inspect.getsource(WeixinChannel._accept_inbound_locked)
        self.assertIn('"/context"', source)
        self.assertIn('"/new"', source)
        self.assertIn('"/bind-reminders"', source)
        # 命令判定仍是「文本精确、不带图片」分支，/context 不例外
        self.assertIn('not data.get("images")', source)



# —— WebUI 内联脚本静态回归（usage_turn 渲染分支 + node --check）——

class WebUiJsSyntaxTests(unittest.TestCase):
    """静态回归：WebUI 内联脚本仍是合法 JS，且包含回合级汇总渲染分支。"""

    @staticmethod
    def _script() -> str:
        page = Path(__file__).resolve().parents[1] / "webui" / "index.html"
        html = page.read_text(encoding="utf-8")
        match = re.search(r"<script>\s*(.*?)\s*</script>", html, re.DOTALL)
        assert match, "inline Web UI script is missing"
        return match.group(1)

    def test_inline_script_is_valid_javascript(self):
        script = self._script()
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8") as handle:
            handle.write(script)
            handle.flush()
            result = subprocess.run(
                ["node", "--check", handle.name],
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_usage_turn_rendering_branch_present(self):
        script = self._script()
        self.assertIn("t === 'usage_turn'", script)
        self.assertIn("u.last_ratio", script)
        self.assertIn("回合输入", script)
        self.assertIn("调用×", script)
        # 逐次 usage 事件处理与实时刷新分支仍在（未被回合汇总替换）
        self.assertIn("t === 'usage'", script)
        self.assertIn("renderCtxBar();", script)


if __name__ == "__main__":
    unittest.main()
