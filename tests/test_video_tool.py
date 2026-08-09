"""视频生成工具 create_video / query_video 与 VideoStore 的单元测试。

覆盖：创建任务返回 video_id（不轮询）、查询 completed 落盘、in_progress/failed
状态处理、未配置友好提示、429 重试、旧版查询兜底、VideoStore 落盘/清理，以及
AgentLoop 对 video 工具注入 session_key 的端到端落盘验证。
"""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from agent.context import ContextBuilder
from agent.loop import AgentLoop
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from agent.tools.spawn import DummySessionManager
from agent.tools.video import CreateVideoTool, QueryVideoTool, _get_field
from config import NanoClawConfig, load_config, save_config
from agent.videostore import VideoStore
from providers.base import LLMProvider, LLMResponse, ToolCallRequest


def _config(**video_overrides):
    video = {
        "api_key": "video-key",
        "base_url": "https://video.example.test/v1",
        "model": "agnes-video-v2.0",
        "timeout_sec": 5,
        "download": True,
    }
    video.update(video_overrides)
    return SimpleNamespace(
        workspace=".",
        image_gen_model={
            "api_key": "img-key",
            "base_url": "https://video.example.test/v1",
            "model": "agnes-image",
        },
        video_gen_model=video,
    )


def _empty_config(**video_overrides):
    video = {
        "api_key": "",
        "base_url": "",
        "model": "agnes-video-v2.0",
        "timeout_sec": 5,
        "download": True,
    }
    video.update(video_overrides)
    return SimpleNamespace(
        workspace=".",
        image_gen_model={"api_key": "", "base_url": "", "model": ""},
        video_gen_model=video,
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _execute(tool, **kwargs):
    with patch("agent.tools.video.asyncio.sleep", new=AsyncMock()):
        return await tool.execute(**kwargs)


class CreateVideoToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_returns_video_id_and_never_polls(self):
        calls = []

        def handler(request):
            calls.append(request)
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/v1/videos")
            self.assertEqual(request.url.host, "video.example.test")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "agnes-video-v2.0")
            self.assertEqual(body["prompt"], "一只猫在海滩散步")
            return httpx.Response(
                200,
                json={
                    "id": "task-1",
                    "task_id": "task-1",
                    "video_id": "vid-1",
                    "status": "queued",
                    "progress": 0,
                    "seconds": "10.0",
                    "size": "1152x768",
                },
            )

        with tempfile.TemporaryDirectory() as tmp:
            tool = CreateVideoTool(
                VideoStore(tmp), _config(), client_factory=lambda: _client(handler)
            )
            result = await tool.execute("一只猫在海滩散步", session_key="web:user:0")

        self.assertIn("vid-1", result)
        self.assertIn("queued", result)
        self.assertIn("10", result)          # seconds 字段
        self.assertIn("稍后", result)         # 提示稍后查询
        self.assertIn("立即返回", result)     # 明确说明不等待
        self.assertEqual(len(calls), 1)      # 只发一次创建请求，绝不轮询

    async def test_create_passes_optional_params(self):
        received = {}

        def handler(request):
            received.update(json.loads(request.content))
            return httpx.Response(
                200, json={"video_id": "vid-2", "status": "queued", "progress": 0}
            )

        with tempfile.TemporaryDirectory() as tmp:
            tool = CreateVideoTool(
                VideoStore(tmp), _config(), client_factory=lambda: _client(handler)
            )
            result = await tool.execute(
                "动画",
                image="https://cdn.example.test/source.png",
                width=768,
                height=1280,
                num_frames=121,
                frame_rate=24,
                seed=42,
                negative_prompt="模糊, 抖动",
                session_key="web:user:0",
            )

        self.assertIn("vid-2", result)
        self.assertEqual(received["image"], "https://cdn.example.test/source.png")
        self.assertEqual(received["width"], 768)
        self.assertEqual(received["height"], 1280)
        self.assertEqual(received["num_frames"], 121)
        self.assertEqual(received["frame_rate"], 24)
        self.assertEqual(received["seed"], 42)
        self.assertEqual(received["negative_prompt"], "模糊, 抖动")

    async def test_create_rejects_invalid_num_frames_without_http(self):
        hit = []

        def handler(request):
            hit.append(request)
            return httpx.Response(200, json={"video_id": "vid-3"})

        with tempfile.TemporaryDirectory() as tmp:
            tool = CreateVideoTool(
                VideoStore(tmp), _config(), client_factory=lambda: _client(handler)
            )
            result = await tool.execute("视频", num_frames=100, session_key="k")

        self.assertIn("num_frames", result)
        self.assertIn("8n+1", result)
        self.assertEqual(hit, [])

    async def test_create_not_configured_returns_friendly_hint(self):
        tool = CreateVideoTool(VideoStore("."), _empty_config())
        result = await tool.execute("视频", session_key="k")
        self.assertIn("未配置视频", result)
        self.assertIn("VIDEO_GEN_API_KEY", result)

    async def test_create_retries_429_then_succeeds(self):
        attempts = 0

        def handler(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(429)
            return httpx.Response(
                200,
                json={
                    "video_id": "vid-4",
                    "status": "queued",
                    "progress": 0,
                    "seconds": "5.0",
                },
            )

        with tempfile.TemporaryDirectory() as tmp:
            tool = CreateVideoTool(
                VideoStore(tmp), _config(), client_factory=lambda: _client(handler)
            )
            result = await _execute(tool, prompt="视频", session_key="k")

        self.assertIn("vid-4", result)
        self.assertEqual(attempts, 2)

    async def test_execution_timeout_is_30_seconds(self):
        tool = CreateVideoTool(VideoStore("."), _config())
        self.assertEqual(tool.execution_timeout_sec, 30.0)
        self.assertIsInstance(tool, Tool)


class QueryVideoToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_completed_downloads_video_to_store(self):
        def handler(request):
            if request.url.path == "/agnesapi":
                self.assertEqual(request.url.host, "video.example.test")
                self.assertEqual(request.url.params["video_id"], "vid-1")
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "progress": 100,
                        "metadata": {
                            "url": "https://cdn.example.test/output/video.mp4"
                        },
                    },
                )
            # 下载直链
            return httpx.Response(
                200,
                content=b"fake-mp4-bytes",
                headers={"content-type": "video/mp4"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            store = VideoStore(tmp)
            tool = QueryVideoTool(
                store, _config(), client_factory=lambda: _client(handler)
            )
            result = await tool.execute(video_id="vid-1", session_key="web:user:0")

            self.assertIn("已生成完成", result)
            self.assertIn("保存到本会话", result)
            self.assertIn("vid-1", result)
            # 落盘到 <safe_key>_videos/ 目录
            videos_dir = os.path.join(tmp, "web_user_0_videos")
            self.assertTrue(os.path.isdir(videos_dir))
            names = os.listdir(videos_dir)
            self.assertEqual(len(names), 1)
            self.assertTrue(names[0].endswith(".mp4"))
            ref = store.resolve("web:user:0", names[0].split(".")[0])
            self.assertIsNotNone(ref)
            self.assertEqual(ref.mime, "video/mp4")

    async def test_query_completed_with_top_level_url_fallback(self):
        """Agnes 实际响应无 metadata 对象、直链在顶层 url 字段时也能正确取到并落盘。"""
        def handler(request):
            if request.url.path == "/agnesapi":
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "progress": 100,
                        "url": "https://cdn.example.test/output/top-level.mp4",
                    },
                )
            # 下载直链
            return httpx.Response(
                200,
                content=b"fake-mp4-bytes",
                headers={"content-type": "video/mp4"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            store = VideoStore(tmp)
            tool = QueryVideoTool(
                store, _config(), client_factory=lambda: _client(handler)
            )
            result = await tool.execute(video_id="vid-1", session_key="web:user:0")

            self.assertIn("已生成完成", result)
            self.assertIn("保存到本会话", result)
            # 落盘到 <safe_key>_videos/ 目录
            videos_dir = os.path.join(tmp, "web_user_0_videos")
            self.assertTrue(os.path.isdir(videos_dir))
            names = os.listdir(videos_dir)
            self.assertEqual(len(names), 1)
            self.assertTrue(names[0].endswith(".mp4"))
            ref = store.resolve("web:user:0", names[0].split(".")[0])
            self.assertIsNotNone(ref)
            self.assertEqual(ref.mime, "video/mp4")

    async def test_query_in_progress_returns_progress_and_not_ready(self):
        def handler(request):
            return httpx.Response(
                200,
                json={"status": "in_progress", "progress": 42, "metadata": {}},
            )

        with tempfile.TemporaryDirectory() as tmp:
            tool = QueryVideoTool(
                VideoStore(tmp), _config(), client_factory=lambda: _client(handler)
            )
            result = await tool.execute(video_id="vid-1", session_key="k")

        self.assertIn("还没好", result)
        self.assertIn("42%", result)
        self.assertIn("稍后", result)

    async def test_query_processing_alias_is_in_progress(self):
        def handler(request):
            return httpx.Response(200, json={"status": "processing", "progress": 7})

        with tempfile.TemporaryDirectory() as tmp:
            tool = QueryVideoTool(
                VideoStore(tmp), _config(), client_factory=lambda: _client(handler)
            )
            result = await tool.execute(video_id="vid-1", session_key="k")

        self.assertIn("还没好", result)
        self.assertIn("7%", result)

    async def test_query_failed_passes_through_error(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "status": "failed",
                    "error": {"message": "显存不足，请降低分辨率后重试"},
                },
            )

        with tempfile.TemporaryDirectory() as tmp:
            tool = QueryVideoTool(
                VideoStore(tmp), _config(), client_factory=lambda: _client(handler)
            )
            result = await tool.execute(video_id="vid-1", session_key="k")

        self.assertIn("失败", result)
        self.assertIn("显存不足", result)

    async def test_query_not_configured_returns_friendly_hint(self):
        tool = QueryVideoTool(VideoStore("."), _empty_config())
        result = await tool.execute(video_id="vid-1", session_key="k")
        self.assertIn("未配置视频", result)

    async def test_query_missing_video_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            tool = QueryVideoTool(VideoStore(tmp), _config())
            result = await tool.execute(session_key="k")
        self.assertIn("video_id", result)

    async def test_download_disabled_returns_url_without_saving(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "progress": 100,
                    "metadata": {"url": "https://cdn.example.test/v.mp4"},
                },
            )

        with tempfile.TemporaryDirectory() as tmp:
            store = VideoStore(tmp)
            tool = QueryVideoTool(
                store,
                _config(download=False),
                client_factory=lambda: _client(handler),
            )
            result = await tool.execute(video_id="vid-1", session_key="web:user:0")

        self.assertIn("直链", result)
        self.assertIn("https://cdn.example.test/v.mp4", result)
        self.assertFalse(os.path.isdir(os.path.join(tmp, "web_user_0_videos")))

    async def test_query_falls_back_to_legacy_endpoint_on_404(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/agnesapi":
                return httpx.Response(404)
            # 旧版 GET {base_url}/videos/<id>
            return httpx.Response(
                200, json={"status": "in_progress", "progress": 30}
            )

        with tempfile.TemporaryDirectory() as tmp:
            tool = QueryVideoTool(
                VideoStore(tmp), _config(), client_factory=lambda: _client(handler)
            )
            result = await tool.execute(video_id="vid-legacy", session_key="k")

        self.assertIn("还没好", result)
        self.assertIn("/agnesapi", calls)
        self.assertTrue(any(path.endswith("/videos/vid-legacy") for path in calls))


class VideoStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_resolve_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = VideoStore(tmp)
            ref = store.save(
                "feishu:chat:0", b"video-bytes", "mp4", "video/mp4"
            )
            self.assertTrue(os.path.isfile(ref.path))
            self.assertEqual(ref.mime, "video/mp4")

            resolved = store.resolve("feishu:chat:0", ref.id)
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.path, ref.path)

            # 未知 video_id → None
            self.assertIsNone(store.resolve("feishu:chat:0", "no-such-id"))

            store.clear("feishu:chat:0")
            self.assertFalse(os.path.isdir(os.path.join(tmp, "feishu_chat_0_videos")))
            # 重复 clear 静默忽略
            store.clear("feishu:chat:0")


class _ScriptedProvider(LLMProvider):
    def __init__(self, responses):
        self.responses = list(responses)

    async def chat(self, messages, tools=None, model=None, **kwargs):
        return self.responses.pop(0)


class VideoLoopInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_injects_session_key_into_query_video(self):
        def handler(request):
            if request.url.path == "/agnesapi":
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "progress": 100,
                        "metadata": {"url": "https://cdn.example.test/loop.mp4"},
                    },
                )
            return httpx.Response(
                200,
                content=b"loop-video-bytes",
                headers={"content-type": "video/mp4"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            store = VideoStore(tmp)
            query_tool = QueryVideoTool(
                store, _config(), client_factory=lambda: _client(handler)
            )
            registry = ToolRegistry()
            registry.register(query_tool)
            loop = AgentLoop(
                provider=_ScriptedProvider([
                    LLMResponse(
                        None,
                        [ToolCallRequest("c1", "query_video", {"video_id": "vid-1"})],
                        "tool_calls",
                    ),
                    LLMResponse("视频已保存到本会话"),
                ]),
                tools=registry,
                context=ContextBuilder(tmp),
                session_manager=DummySessionManager(),
                session_key="feishu:chat:0",
            )

            result = await loop.run("查一下视频好了吗")

            self.assertIn("视频已保存", result)
            # 视频落到了会话目录 <safe_key>_videos/，证明 session_key 被注入
            videos_dir = os.path.join(tmp, "feishu_chat_0_videos")
            self.assertTrue(os.path.isdir(videos_dir))
            self.assertEqual(len(os.listdir(videos_dir)), 1)


# —— 配置驱动多服务商适配层（新增用例，不破坏旧结构用例） ——

def _provider_config(provider="agnes", **overrides):
    """构造带 video_provider / video_providers 新结构的配置。

    video_gen_model 故意填成与 agnes 项不同的旧地址/模型/开关，用于证明
    「新结构优先于旧结构」：若代码误读旧结构，请求地址与模型会不一致。
    """
    providers = {
        "agnes": {
            "api_key": "video-key",
            "base_url": "https://video.example.test/v1",
            "model": "agnes-video-v2.0",
            "timeout_sec": 5,
            "download": True,
            "create": {"method": "POST", "path": "/videos"},
            "query": {
                "method": "GET",
                "path": "/agnesapi",
                "id_in": "query",
                "id_param": "video_id",
            },
            "fields": {
                "task_id": "video_id",
                "status": "status",
                "progress": "progress",
                "seconds": "seconds",
                "size": "size",
                "url": "metadata.url",
                "url_fallback": "url",
            },
        },
        "kling": {
            "api_key": "kling-key",
            "base_url": "https://api.klingai.test",
            "model": "kling-v2.6-pro",
            "timeout_sec": 5,
            "download": True,
            "create": {"method": "POST", "path": "/v1/videos/text2video"},
            "query": {
                "method": "GET",
                "path": "/v1/videos/{id}",
                "id_in": "path",
                "id_placeholder": "{id}",
            },
            "fields": {
                "task_id": "task_id",
                "status": "status",
                "progress": "progress",
                "seconds": "seconds",
                "size": "size",
                "url": "result.videos[0].url",
                "url_fallback": "",
            },
        },
    }
    if provider in providers:
        providers[provider].update(overrides)
    return SimpleNamespace(
        workspace=".",
        image_gen_model={
            "api_key": "img-key",
            "base_url": "https://video.example.test/v1",
            "model": "agnes-image",
        },
        video_gen_model={
            "api_key": "legacy-key",
            "base_url": "https://legacy.example.test/v1",
            "model": "legacy-model",
            "timeout_sec": 9,
            "download": False,
        },
        video_provider=provider,
        video_providers=providers,
    )


class FieldPathTests(unittest.TestCase):
    """fields 映射 getter（点路径 + 数组下标）单元测试。"""

    def test_get_field_dot_path(self):
        body = {"metadata": {"url": "https://cdn.test/v.mp4"}, "status": "completed"}
        self.assertEqual(_get_field(body, "metadata.url"), "https://cdn.test/v.mp4")
        self.assertEqual(_get_field(body, "status"), "completed")
        self.assertIsNone(_get_field(body, "metadata.nonexist"))
        self.assertIsNone(_get_field(body, "nested.missing"))
        # 空串 / None 路径一律返回 None（url_fallback 留空即"没有第二来源"）
        self.assertIsNone(_get_field(body, ""))
        self.assertIsNone(_get_field(body, None))

    def test_get_field_array_path(self):
        body = {
            "result": {
                "videos": [
                    {"url": "https://cdn.test/a.mp4"},
                    {"url": "https://cdn.test/b.mp4"},
                ]
            }
        }
        self.assertEqual(_get_field(body, "result.videos[0].url"), "https://cdn.test/a.mp4")
        self.assertEqual(_get_field(body, "result.videos[1].url"), "https://cdn.test/b.mp4")
        self.assertIsNone(_get_field(body, "result.videos[9].url"))          # 越界下标
        self.assertIsNone(_get_field(body, "result.videos[-1].url"))         # 负数下标
        self.assertIsNone(_get_field(body, "result.videos[abc].url"))        # 非数字下标
        self.assertIsNone(_get_field(body, "result.videos[0].missing"))      # 深层缺字段
        self.assertIsNone(_get_field({"videos": []}, "videos[0]"))           # 空数组越界
        self.assertIsNone(_get_field({"videos": "not-a-list"}, "videos[0]")) # 中间值不是列表
        self.assertIsNone(_get_field(None, "result.videos[0].url"))          # None 对象
        self.assertEqual(_get_field([{"url": "x"}], "[0].url"), "x")         # 路径以数组下标开头


class ProviderConfigTests(unittest.IsolatedAsyncioTestCase):
    """配置驱动适配层：旧结构兜底 / 新结构 agnes / kling 查询占位符替换。"""

    def test_legacy_video_gen_model_builds_agnes_provider(self):
        """a) 旧结构（video_gen_model，无新结构）能构造 agnes provider 配置。"""
        tool = CreateVideoTool(VideoStore("."), _config())
        cfg = tool._provider_cfg()
        self.assertEqual(cfg["provider"], "agnes")
        self.assertEqual(cfg["api_key"], "video-key")
        self.assertEqual(cfg["base_url"], "https://video.example.test/v1")
        self.assertEqual(cfg["model"], "agnes-video-v2.0")
        self.assertEqual(cfg["timeout_sec"], 5)
        self.assertEqual(cfg["download"], True)
        # 默认 schema 等价旧行为
        self.assertEqual(cfg["create"], {"method": "POST", "path": "/videos"})
        self.assertEqual(cfg["query"]["id_in"], "query")
        self.assertEqual(cfg["query"]["id_param"], "video_id")
        self.assertEqual(cfg["fields"]["url"], "metadata.url")
        self.assertEqual(cfg["fields"]["url_fallback"], "url")

    async def test_legacy_video_gen_model_create_and_query(self):
        """a) 旧结构能正常创建 / 查询（显式覆盖一遍端到端路径）。"""
        def handler(request):
            if request.url.path == "/v1/videos":
                return httpx.Response(
                    200, json={"video_id": "lg-1", "status": "queued"}
                )
            if request.url.path == "/agnesapi":
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "progress": 100,
                        "metadata": {"url": "https://cdn.test/legacy.mp4"},
                    },
                )
            return httpx.Response(
                200, content=b"legacy-mp4", headers={"content-type": "video/mp4"}
            )

        with tempfile.TemporaryDirectory() as tmp:
            store = VideoStore(tmp)
            create_tool = CreateVideoTool(
                store, _config(), client_factory=lambda: _client(handler)
            )
            created = await _execute(create_tool, prompt="旧结构", session_key="k")
            self.assertIn("lg-1", created)

            query_tool = QueryVideoTool(
                store, _config(), client_factory=lambda: _client(handler)
            )
            result = await _execute(query_tool, video_id="lg-1", session_key="k")
            self.assertIn("已生成完成", result)

    async def test_new_structure_agnes_create_and_query(self):
        """b) 新结构（video_providers.agnes）正常创建 / 查询且优先于旧结构。"""
        def handler(request):
            # 若误读旧结构，host 会是 legacy.example.test、model 是 legacy-model
            if request.url.path == "/v1/videos":
                self.assertEqual(request.url.host, "video.example.test")
                self.assertEqual(
                    json.loads(request.content)["model"], "agnes-video-v2.0"
                )
                return httpx.Response(
                    200, json={"video_id": "nv-1", "status": "queued"}
                )
            if request.url.path == "/agnesapi":
                self.assertEqual(request.url.params["video_id"], "nv-1")
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "progress": 100,
                        "metadata": {"url": "https://cdn.test/new.mp4"},
                    },
                )
            return httpx.Response(
                200, content=b"new-mp4", headers={"content-type": "video/mp4"}
            )

        with tempfile.TemporaryDirectory() as tmp:
            store = VideoStore(tmp)
            cfg = _provider_config("agnes")
            create_tool = CreateVideoTool(
                store, cfg, client_factory=lambda: _client(handler)
            )
            created = await _execute(create_tool, prompt="新结构", session_key="k")
            self.assertIn("nv-1", created)

            query_tool = QueryVideoTool(
                store, cfg, client_factory=lambda: _client(handler)
            )
            result = await _execute(query_tool, video_id="nv-1", session_key="k")
            self.assertIn("已生成完成", result)
            self.assertIn("nv-1", result)

    async def test_new_structure_kling_create(self):
        """新结构 kling 创建：POST {base_url}/v1/videos/text2video，task_id 走 fields。"""
        def handler(request):
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.host, "api.klingai.test")
            self.assertEqual(request.url.path, "/v1/videos/text2video")
            self.assertEqual(json.loads(request.content)["model"], "kling-v2.6-pro")
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"task_id": "kl-1", "task_status": "pending"},
                },
            )

        with tempfile.TemporaryDirectory() as tmp:
            tool = CreateVideoTool(
                VideoStore(tmp),
                _provider_config("kling"),
                client_factory=lambda: _client(handler),
            )
            result = await _execute(tool, prompt="海浪", session_key="k")
        self.assertIn("kl-1", result)

    async def test_kling_query_id_in_path_replaces_placeholder(self):
        """e) query.id_in=path：URL 正确替换占位符；d) 数组路径取直链；succeed 视为完成。"""
        calls = []

        def handler(request):
            calls.append(request.url)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "task_id": "kl-123",
                        "status": "succeed",
                        "result": {
                            "videos": [{"url": "https://cdn.kling.test/out.mp4"}]
                        },
                    },
                },
            )

        with tempfile.TemporaryDirectory() as tmp:
            store = VideoStore(tmp)
            tool = QueryVideoTool(
                store,
                _provider_config("kling", download=False),
                client_factory=lambda: _client(handler),
            )
            result = await _execute(tool, video_id="kl-123", session_key="k")

        self.assertEqual(len(calls), 1)  # 只打查询端点，不下载
        self.assertEqual(calls[0].host, "api.klingai.test")
        self.assertEqual(calls[0].path, "/v1/videos/kl-123")
        self.assertIn("已生成完成", result)
        # 数组路径 result.videos[0].url 解析出直链
        self.assertIn("https://cdn.kling.test/out.mp4", result)
        self.assertIn("直链", result)

    async def test_kling_query_array_path_downloads_to_store(self):
        """d) 数组路径（result.videos[0].url）解析并落盘；succeed 视为完成。"""
        def handler(request):
            if request.url.path == "/v1/videos/kl-9":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "task_id": "kl-9",
                            "status": "succeed",
                            "result": {
                                "videos": [{"url": "https://cdn.kling.test/out.mp4"}]
                            },
                        },
                    },
                )
            return httpx.Response(
                200, content=b"kling-mp4", headers={"content-type": "video/mp4"}
            )

        with tempfile.TemporaryDirectory() as tmp:
            store = VideoStore(tmp)
            tool = QueryVideoTool(
                store,
                _provider_config("kling"),
                client_factory=lambda: _client(handler),
            )
            result = await _execute(tool, video_id="kl-9", session_key="web:user:0")

            self.assertIn("已生成完成", result)
            videos_dir = os.path.join(tmp, "web_user_0_videos")
            self.assertTrue(os.path.isdir(videos_dir))
            names = os.listdir(videos_dir)
            self.assertEqual(len(names), 1)
            self.assertTrue(names[0].endswith(".mp4"))

    def test_new_structure_config_round_trip(self):
        """config.py 能解析并持久化 video_provider / video_providers 新结构。"""
        providers = {
            "agnes": {
                "api_key": "",
                "base_url": "https://apihub.agnes-ai.com/v1",
                "model": "agnes-video-v2.0",
            },
            "kling": {
                "api_key": "",
                "base_url": "https://api.klingai.com",
                "model": "kling-v2.6-pro",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            cfg = NanoClawConfig(video_provider="kling", video_providers=providers)
            save_config(cfg, path)
            loaded = load_config(path)
            self.assertEqual(loaded.video_provider, "kling")
            self.assertEqual(loaded.video_providers["agnes"]["base_url"],
                             "https://apihub.agnes-ai.com/v1")
            self.assertEqual(loaded.video_providers["kling"]["model"], "kling-v2.6-pro")
            # 缺省仍为 agnes，且旧结构 video_gen_model 正常解析
            self.assertEqual(NanoClawConfig().video_provider, "agnes")
            self.assertIsInstance(NanoClawConfig().video_gen_model, dict)




if __name__ == "__main__":
    unittest.main()
