"""DownlinkPlayer 单测：delta 解码、播放与服务端打断逻辑（TASK-037，fake 流）。"""

import asyncio
import base64
import unittest

from voice.realtime_s2s.downlink import DownlinkPlayer


class _FakeStream:
    """记录写入/停止/关闭的假输出流（模拟 sd.OutputStream 非阻塞写）。"""

    def __init__(self):
        self.started = False
        self.written: list[bytes] = []
        self.stops = 0
        self.closed = False

    def start(self):
        self.started = True

    def write(self, data: bytes):
        self.written.append(bytes(data))

    def stop(self):
        self.stops += 1

    def close(self):
        self.closed = True


class DownlinkPlayerTests(unittest.IsolatedAsyncioTestCase):
    async def _player(self, stream):
        p = DownlinkPlayer(sample_rate=24000, stream_factory=lambda: stream)
        await p.start()
        return p

    async def test_delta_decoded_and_played(self):
        stream = _FakeStream()
        p = await self._player(stream)
        pcm = b"\x01\x02\x03\x04"
        p.on_response_start("r1")
        p.feed_delta(base64.b64encode(pcm).decode())
        await asyncio.sleep(0.05)
        self.assertEqual(stream.written, [pcm])
        await p.stop()

    async def test_delta_is_queued_without_local_response_gate_like_demo(self):
        stream = _FakeStream()
        p = await self._player(stream)
        p.feed_delta(base64.b64encode(b"\x00\x01").decode())
        await asyncio.sleep(0.05)
        self.assertEqual(stream.written, [b"\x00\x01"])
        await p.stop()

    async def test_invalid_base64_dropped(self):
        stream = _FakeStream()
        p = await self._player(stream)
        p.on_response_start("r1")
        p.feed_delta("!!!not-base64!!!")
        await asyncio.sleep(0.05)
        self.assertEqual(stream.written, [])
        await p.stop()

    async def test_response_start_clears_previous_buffer(self):
        stream = _FakeStream()
        p = await self._player(stream)
        p.on_response_start("r1")
        p.feed_delta(base64.b64encode(b"\x00\x01\x00\x02").decode())
        await asyncio.sleep(0.05)
        # 新响应开始 → 清旧缓冲（旧响应被打断时缓冲内未播的块丢弃）
        p.on_response_start("r2")
        await asyncio.sleep(0.02)
        p.feed_delta(base64.b64encode(b"\xaa\xbb\xcc\xdd").decode())
        await asyncio.sleep(0.05)
        self.assertIn(b"\xaa\xbb\xcc\xdd", stream.written)
        await p.stop()

    async def test_clear_audio_buffer_keeps_playing_no_interrupt(self):
        # 对齐 py demo：transcription.started 只清播放缓冲——保持播放态、
        # 不发 response.cancel；服务端若判定回声误报继续下发 delta 仍能播
        stream = _FakeStream()
        p = await self._player(stream)
        p.on_response_start("r1")
        p.feed_delta(base64.b64encode(b"\x00\x01\x00\x02").decode())
        p.feed_delta(base64.b64encode(b"\x00\x03\x00\x04").decode())
        await asyncio.sleep(0.05)
        self.assertTrue(p.is_playing)
        await p.clear_audio_buffer()
        # 只清缓冲：播放态保持，客户端没有 response.cancel 上行路径
        self.assertTrue(p.is_playing)
        self.assertIsNotNone(p.response_id)
        # 服务端若未取消并继续下发 delta → 播放器不中断，继续播
        p.feed_delta(base64.b64encode(b"\x00\x05\x00\x06").decode())
        await asyncio.sleep(0.05)
        self.assertIn(b"\x00\x05\x00\x06", stream.written)
        self.assertEqual(stream.stops, 0)
        await p.stop()

    async def test_playback_queue_has_no_client_side_drop_limit(self):
        p = DownlinkPlayer(sample_rate=24000, stream_factory=_FakeStream)
        p.on_response_start("r1")
        chunk = base64.b64encode(b"\x00\x01").decode()
        for _ in range(1025):
            p.feed_delta(chunk)
        self.assertEqual(p._chunks.qsize(), 1025)
        await p.stop()

    async def test_response_done_ends_playback(self):
        stream = _FakeStream()
        p = await self._player(stream)
        p.on_response_start("r1")
        self.assertTrue(p.is_playing)
        await p.on_response_done()
        self.assertFalse(p.is_playing)
        self.assertIsNone(p.response_id)
        await p.stop()

    async def test_response_cancel_ends_playback(self):
        stream = _FakeStream()
        p = await self._player(stream)
        p.on_response_start("r1")
        await p.on_response_cancel()
        self.assertFalse(p.is_playing)
        self.assertIsNone(p.response_id)
        await p.stop()

    async def test_response_cancel_keeps_buffered_audio(self):
        """TASK-038 对齐 py demo：canceled 不清播放缓冲，已入队音频继续播完。

        服务端可能因回声误判插话发 canceled——此时清空缓冲会导致「每句只听
        开头」；真正的停播由 transcription.started（用户真开口）驱动。
        """
        stream = _FakeStream()
        p = await self._player(stream)
        p.on_response_start("r1")
        p.feed_delta(base64.b64encode(b"\x00" * 480).decode("ascii"))  # 入队 480B
        await p.on_response_cancel()
        self.assertFalse(p.is_playing)
        self.assertFalse(p._chunks.empty(), "canceled 不应清空播放缓冲（demo 同款）")
        # 播放线程仍能消费剩余缓冲
        await p.stop()
