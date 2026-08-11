"""RealtimeChannel 状态机测试：唤醒→对话→退出，fake client/组件（TASK-037）。

覆盖：KWS 待命直到 stop、唤醒→建会话→全双工→静默/优雅退出回待命、
静默超时退出、stop 取消对话优雅关闭、唤醒回应本地 WAV 缓存播放，
以及打断完全交由服务端动态判停（客户端不发 response.cancel）。
不真连云端 / 不开麦克风 / 不碰 sounddevice（全部 fake）。
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from channels.realtime import DEFAULT_VOICE_TYPE, RealtimeChannel

TEST_IDENTITY_FILE = Path(__file__).with_name("realtime_identity.fixture.md")


# —— fake 组件 ——


class _FakeClient:
    """记录调用、按脚本回放下行事件的假 client；脚本放完后挂起等 stop。"""

    def __init__(self, script=()):
        self.script = list(script)
        self.sent = []
        self.connects = 0
        self.session_kw = None
        self.closed_sessions = 0
        self.disconnects = 0

    async def connect(self):
        self.connects += 1

    async def create_session(self, **kwargs):
        self.session_kw = kwargs
        return {"type": "session.created"}

    async def send_event(self, event):
        self.sent.append(event)

    async def iter_events(self):
        for ev in self.script:
            yield ev
        # 脚本耗尽后模拟真实心跳（真实 client 为 1s，测试加速）：
        # 对话循环靠 None 心跳做静默超时检查
        while True:
            await asyncio.sleep(0.01)
            yield None

    async def close_session(self):
        self.closed_sessions += 1

    async def disconnect(self):
        self.disconnects += 1


class _FakeUplink:
    def __init__(self, client):
        self.client = client
        self.started = 0
        self.stopped = 0

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1


class _FakeDownlink:
    """镜像真实 DownlinkPlayer 的服务端事件状态机。"""

    def __init__(self):
        self.is_playing = False
        self.response_id = None
        self.deltas = []
        self.started = 0
        self.stopped = 0

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1

    def on_response_start(self, rid):
        self.response_id = rid
        self.is_playing = True

    def feed_delta(self, b64):
        self.deltas.append(b64)

    async def on_output_audio_done(self):
        pass

    async def on_response_cancel(self):
        self.is_playing = False
        self.response_id = None

    async def on_response_done(self):
        self.is_playing = False
        self.response_id = None

    async def clear_audio_buffer(self):
        # 对齐真实实现：只清播放缓冲，不改播放态、不发 cancel
        self.deltas.clear()


class _FakeDetector:
    def __init__(self):
        self.on_wake = None
        self.started = 0
        self.stopped = 0

    async def start(self, on_wake):
        self.on_wake = on_wake
        self.started += 1

    async def stop(self):
        self.stopped += 1


# —— 工具函数 ——


async def _wait_task(channel, rounds=100, delay=0.02):
    """等待对话任务结束（最多 rounds 轮 * delay 秒），返回该任务。

    唤醒流程中 ``_conversation_task`` 由 ``_on_wake`` 异步创建，可能晚于
    调用点；轮询直到任务出现或超时。
    """
    task = channel._conversation_task
    for _ in range(rounds):
        if task is not None and task.done():
            return task
        await asyncio.sleep(delay)
        task = channel._conversation_task
    return task


def _make_channel(client, detector=None, **kwargs):
    kwargs.setdefault("wake_replies_dir", "/nonexistent-wake-dir")
    kwargs.setdefault("uplink_factory", lambda c: _FakeUplink(c))
    kwargs.setdefault("downlink_factory", lambda: _FakeDownlink())
    channel = RealtimeChannel(
        None,
        client_factory=lambda: client,
        kws_detector=detector,
        **kwargs,
    )
    channel._identity_file = TEST_IDENTITY_FILE
    return channel


class RealtimeChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_standby_until_stop(self):
        detector = _FakeDetector()
        channel = _make_channel(_FakeClient(), detector=detector)
        task = asyncio.create_task(channel.start())
        for _ in range(50):
            if detector.started:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(detector.started, 1)
        self.assertIsNotNone(detector.on_wake)  # KWS 待命：唤醒回调已注册
        await channel.stop()
        await task
        self.assertEqual(detector.stopped, 1)

    async def test_start_idles_without_detector(self):
        channel = _make_channel(_FakeClient())  # 无 detector → 空转
        task = asyncio.create_task(channel.start())
        await asyncio.sleep(0.05)
        await channel.stop()
        await task

    async def test_wake_runs_conversation_and_graceful_close(self):
        detector = _FakeDetector()
        client = _FakeClient(
            script=[
                {"type": "response.output_audio.started", "response_id": "r1"},
                {
                    "type": "response.output_audio.delta",
                    "response_id": "r1",
                    "delta": "AQID",
                },
                {"type": "response.output_audio.done", "response_id": "r1"},
                {"type": "response.done", "response_id": "r1"},
            ]
        )
        # 用记录型 downlink_factory：对话可能极快完成，直接拿创建出来的实例断言
        created = []

        def make_dl():
            dl = _FakeDownlink()
            created.append(dl)
            return dl

        channel = _make_channel(
            client,
            detector=detector,
            downlink_factory=make_dl,
            silence_timeout_sec=0.1,  # 事件消费完后靠静默超时退出
        )
        # 完整路径：start() 进入 KWS 待命 → 唤醒 → 对话 → 优雅退出 → 回待命
        start_task = asyncio.create_task(channel.start())
        for _ in range(50):
            if detector.started:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(detector.started, 1)  # 待命已就绪
        await detector.on_wake()  # 模拟唤醒词命中
        task = await _wait_task(channel)
        self.assertTrue(task.done())
        self.assertEqual(len(created), 1)
        downlink = created[0]
        # 会话建立：connect + session.create（文件人设 + 默认音色 + tools 空）
        self.assertEqual(client.connects, 1)
        self.assertEqual(client.session_kw["voice_type"], DEFAULT_VOICE_TYPE)
        self.assertEqual(client.session_kw["tools"], [])
        self.assertIn("小奈", client.session_kw["instructions"])
        self.assertIn("洵洵", client.session_kw["instructions"])
        self.assertFalse(client.session_kw["enable_websearch"])
        # 下行 delta 播放入 fake downlink
        self.assertGreaterEqual(len(downlink.deltas), 1)
        # 静默超时退出 → 优雅关闭（session.close）
        self.assertEqual(client.closed_sessions, 1)
        # 回到 KWS 待命：对话开始停 KWS（释放麦克风）→ 对话结束重启
        self.assertEqual(detector.stopped, 1)
        self.assertEqual(detector.started, 2)
        self.assertIsNotNone(detector.on_wake)
        self.assertFalse(channel._in_conversation)
        await channel.stop()
        await start_task

    def test_identity_file_is_required_and_must_not_be_empty(self):
        channel = _make_channel(_FakeClient())
        with tempfile.TemporaryDirectory() as tmp:
            channel._identity_file = Path(tmp, "missing.md")
            with self.assertRaisesRegex(RuntimeError, "人设读取失败"):
                channel._load_identity()

            channel._identity_file.write_text("   \n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "人设为空"):
                channel._load_identity()

    def test_identity_file_is_reloaded(self):
        channel = _make_channel(_FakeClient())
        with tempfile.TemporaryDirectory() as tmp:
            channel._identity_file = Path(tmp, "identity.md")
            channel._identity_file.write_text("第一版人设", encoding="utf-8")
            self.assertEqual(channel._load_identity(), "第一版人设")

            channel._identity_file.write_text("第二版人设", encoding="utf-8")
            self.assertEqual(channel._load_identity(), "第二版人设")

    async def test_wake_reply_played_from_local_cache(self):
        detector = _FakeDetector()
        client = _FakeClient(script=[])
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "wake_01.wav").write_bytes(b"\x00" * 64)
            channel = _make_channel(
                client,
                detector=detector,
                wake_replies_dir=tmp,
                silence_timeout_sec=0.1,
            )
            with patch(
                "channels.realtime.play_audio", new=AsyncMock(return_value=None)
            ) as play:
                await channel._on_wake()
                task = await _wait_task(channel)
                self.assertTrue(task.done())
            play.assert_awaited_once()
        await channel.stop()

    async def test_wake_reply_skipped_when_cache_empty(self):
        detector = _FakeDetector()
        client = _FakeClient(script=[])
        channel = _make_channel(
            client, detector=detector, silence_timeout_sec=0.1
        )
        with patch(
            "channels.realtime.play_audio", new=AsyncMock(return_value=None)
        ) as play:
            await channel._on_wake()
            task = await _wait_task(channel)
            self.assertTrue(task.done())
        play.assert_not_awaited()
        await channel.stop()

    async def test_silence_timeout_exits_conversation(self):
        detector = _FakeDetector()
        client = _FakeClient(script=[])  # 无任何事件 → 循环空等
        channel = _make_channel(
            client, detector=detector, silence_timeout_sec=0.1
        )
        await channel._on_wake()
        task = await _wait_task(channel, rounds=300)  # 最多等 ~6s
        self.assertTrue(task.done())
        self.assertEqual(client.closed_sessions, 1)
        await channel.stop()

    async def test_awaiting_response_blocks_silence_timeout(self):
        # 用户说完（transcription.completed）→ 等待模型回复 → 静默超时被挡
        detector = _FakeDetector()
        client = _FakeClient(
            script=[{"type": "conversation.item.input_audio_transcription.completed"}]
        )
        channel = _make_channel(
            client, detector=detector, silence_timeout_sec=0.05
        )
        await channel._on_wake()
        await asyncio.sleep(0.3)  # 远超静默超时（0.05s）仍不应退出
        task = channel._conversation_task
        self.assertIsNotNone(task)
        self.assertFalse(task.done(), "等待模型回复期间不应静默退出")
        await channel.stop()

    async def test_awaiting_response_blocks_silence_timeout(self):
        # 用户说完（transcription.completed）→ 等待模型回复 → 静默超时被挡
        detector = _FakeDetector()
        client = _FakeClient(
            script=[{"type": "conversation.item.input_audio_transcription.completed"}]
        )
        channel = _make_channel(
            client, detector=detector, silence_timeout_sec=0.05
        )
        await channel._on_wake()
        await asyncio.sleep(0.3)  # 远超静默超时（0.05s）仍不应退出
        task = channel._conversation_task
        self.assertIsNotNone(task)
        self.assertFalse(task.done(), "等待模型回复期间不应静默退出")
        await channel.stop()

    async def test_response_done_refreshes_silence_window(self):
        # 模型回完（response.done）→ 刷新静默计时 → 用户有完整接话窗口
        detector = _FakeDetector()
        client = _FakeClient(
            script=[
                {"type": "conversation.item.input_audio_transcription.completed"},
                {"type": "response.done"},
            ]
        )
        channel = _make_channel(
            client, detector=detector, silence_timeout_sec=0.1
        )
        await channel._on_wake()
        task = await _wait_task(channel, rounds=300)  # 事件消费完后靠静默退出
        self.assertTrue(task.done())
        self.assertEqual(client.closed_sessions, 1)
        await channel.stop()

    async def test_stop_during_conversation_cancels_gracefully(self):
        detector = _FakeDetector()
        client = _FakeClient(script=[])  # 一直挂起 → 模拟长对话
        channel = _make_channel(client, detector=detector)
        await channel._on_wake()
        for _ in range(100):
            if client.connects:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(client.connects, 1)
        await channel.stop()
        # stop 取消对话任务 → finally 仍走优雅关闭（close_session）
        self.assertEqual(client.closed_sessions, 1)
        # detector.stop 两次：对话开始释放麦克风 + 渠道 stop（幂等）
        self.assertEqual(detector.stopped, 2)
        self.assertFalse(channel._in_conversation)

    async def test_transcription_started_clears_buffer_without_cancel(self):
        # 用户开口（transcription.started）→ 只清播放缓冲，**不发 response.cancel**
        # （对齐官方 py/web demo：打断完全由服务端动态判停完成；客户端 cancel
        # 会把「回声误报」升级成实锤取消，TASK-038 复盘确认 demo 从不发 cancel）
        detector = _FakeDetector()
        client = _FakeClient(
            script=[
                {"type": "response.output_audio.started", "response_id": "r1"},
                {
                    "type": "response.output_audio.delta",
                    "delta": "AAAA",
                },
                {"type": "conversation.item.input_audio_transcription.started"},
            ]
        )
        channel = _make_channel(client, detector=detector)
        await channel._on_wake()
        for _ in range(100):
            if channel._downlink is not None and channel._downlink.is_playing:
                break
            await asyncio.sleep(0.01)
        downlink = channel._downlink
        self.assertIsNotNone(downlink)
        # 服务端已发 transcription.started → 清缓冲（deltas 清空）
        self.assertEqual(downlink.deltas, [])
        # 播放态保持（服务端若判定回声误报继续下发，还能播）
        self.assertTrue(downlink.is_playing)
        # 关键断言：没有任何 response.cancel 上行
        self.assertEqual(
            [e for e in client.sent if e["type"] == "response.cancel"], []
        )
        await channel.stop()
