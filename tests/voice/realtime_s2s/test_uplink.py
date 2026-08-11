"""UplinkSender 单测：16k PCM 分包（20ms=640B）+ Base64 编码正确性（TASK-037）。"""

import asyncio
import base64
import unittest

from voice.realtime_s2s.uplink import UplinkSender


class _FakeClient:
    def __init__(self):
        self.events = []

    async def send_event(self, event):
        self.events.append(event)


class UplinkSenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_640_bytes_per_20ms_packet(self):
        client = _FakeClient()
        sender = UplinkSender(client, sample_rate=16000)
        self.assertEqual(sender.chunk_bytes, 640)
        await sender.send_pcm(bytes(640 * 2))
        self.assertEqual(len(client.events), 2)
        for ev in client.events:
            self.assertEqual(ev["type"], "input_audio_buffer.append")
            self.assertEqual(len(base64.b64decode(ev["audio"])), 640)

    async def test_partial_packet_held_until_filled(self):
        client = _FakeClient()
        sender = UplinkSender(client, sample_rate=16000)
        await sender.send_pcm(bytes(320))
        self.assertEqual(client.events, [])  # 不足一包，留在缓冲
        await sender.send_pcm(bytes(320))
        self.assertEqual(len(client.events), 1)
        self.assertEqual(len(base64.b64decode(client.events[0]["audio"])), 640)

    async def test_empty_pcm_is_noop(self):
        client = _FakeClient()
        sender = UplinkSender(client, sample_rate=16000)
        await sender.send_pcm(b"")
        self.assertEqual(client.events, [])

    async def test_mic_factory_stream_feeds_packets(self):
        # start() 用 mic_factory 打开的采集对象 → feed_pcm → 上行 → stop 关闭
        class _FakeMic:
            def __init__(self):
                self.closed = False

            def start(self):
                pass

            def close(self):
                self.closed = True

        client = _FakeClient()
        mic = _FakeMic()
        sender = UplinkSender(client, sample_rate=16000, mic_factory=lambda: mic)
        await sender.start()
        sender.feed_pcm(bytes(640))
        await asyncio.sleep(0.05)
        await sender.stop()
        self.assertEqual(len(client.events), 1)
        self.assertEqual(len(base64.b64decode(client.events[0]["audio"])), 640)
        self.assertTrue(mic.closed)
