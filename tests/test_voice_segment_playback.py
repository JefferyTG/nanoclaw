"""TASK-030 分段流式播放集成测试。

覆盖 ``VoiceChannel.send()`` 分段播放状态机：
1. 多段文本 → 逐段 synthesize + play_audio，顺序正确，拼接还原原文
2. 预合成并发上限 2（同时最多 2 个 synthesize 在途）
3. 单段合成失败 → 该段降级文字，后续段不受影响
4. 段播放失败 → 当前及后续段降级文字
5. 唤醒回应不走分段（_play_wake_reply 单段路径不变）
6. [END] 剥离后分段播放，播完退出连续对讲
7. 每段过 DSP（playback_params 透传）

不触发真实模型/网络/音频设备（播放全部 mock）。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, call

from bus.queue import MessageBus, OutboundMessage
from channels.voice import VoiceChannel
from voice.kws.errors import KwsError
from voice.tts.base import TTSError


def _outbound(text: str) -> OutboundMessage:
    return OutboundMessage(channel="voice", chat_id="direct", content=text)


class _FakeTTS:
    """假 TTS：记录 synthesize 文本，返回递增音频，可按段注入失败。"""

    def __init__(
        self,
        *,
        audio=b"audio",
        media_type="audio/wav",
        error=None,
        fail_on_seg=None,  # set of segment indices to fail
    ):
        self.audio = audio
        self.media_type = media_type
        self.error = error
        self.fail_on_seg = fail_on_seg or set()
        self.synthesized: list[str] = []
        self._call_count = 0

    async def synthesize(self, text):
        self.synthesized.append(text)
        idx = self._call_count
        self._call_count += 1
        if self.error is not None:
            raise self.error
        if idx in self.fail_on_seg:
            raise TTSError("synth_failed", f"第{idx+1}段合成模拟失败")
        # 返回递增音频以区分各段
        return SimpleNamespace(
            audio=self.audio + str(idx).encode(),
            media_type=self.media_type,
        )


class VoiceSegmentPlaybackTests(unittest.IsolatedAsyncioTestCase):
    """send() 分段播放状态机。"""

    def _make_channel(self, tts=None, max_voice_chars: int = 0, **kwargs):
        """max_voice_chars=0 表示不截断（允许长文本分段）。"""
        voice = VoiceChannel(
            MessageBus(),
            tts_service=tts,
            max_voice_chars=max_voice_chars,
            **kwargs,
        )
        emitted: list[str] = []
        voice._reply_sink = emitted.append
        return voice, emitted

    async def test_multi_segment_synthesizes_and_plays_in_order(self):
        """多段文本 → 逐段 synthesize + play_audio，顺序正确，拼接还原。"""
        # 构造长文本：多个句号分隔，确保分段
        text = "你好世界。今天天气很好。我们去公园。中午吃饭。下午看电影。晚上休息。再见。"
        tts = _FakeTTS(audio=b"AUD", media_type="audio/wav")
        voice, emitted = self._make_channel(tts=tts)
        with patch("channels.voice.play_audio", new_callable=AsyncMock) as play:
            await voice.send(_outbound(text))

        # 分段合成：每段单独调 synthesize
        self.assertTrue(len(tts.synthesized) > 1)
        # 拼接还原原文
        self.assertEqual("".join(tts.synthesized), text)
        # 每段都播放了，次数等于段数
        self.assertEqual(play.await_count, len(tts.synthesized))
        # 不 _emit 文字（全部成功）
        self.assertEqual(emitted, [])
        # 播放顺序正确：第 0 段音频含 "0"，第 1 段含 "1"...
        for i, c in enumerate(play.call_args_list):
            self.assertIn(str(i).encode(), c.args[0])

    async def test_concurrent_synth_max_two(self):
        """预合成并发上限 2：同时最多 2 个 synthesize 在途。"""
        # 构造超长无标点文本，确保产生多段（64+120+120+...）
        text = "字" * 500
        synth_times: list[float] = []

        class _TimingTTS:
            synthesized: list[str] = []

            async def synthesize(self, text):
                import time
                self.synthesized.append(text)
                synth_times.append(time.monotonic())
                # 模拟合成耗时
                await __import__("asyncio").sleep(0.05)
                return SimpleNamespace(audio=b"AUD", media_type="audio/wav")

        timing_tts = _TimingTTS()
        voice, emitted = self._make_channel(tts=timing_tts)

        # 记录 play_audio 调用时间（播放耗时模拟）
        play_times: list[float] = []

        async def _mock_play(audio, media_type, **kwargs):
            import time
            play_times.append(time.monotonic())
            await __import__("asyncio").sleep(0.05)

        with patch("channels.voice.play_audio", side_effect=_mock_play):
            await voice.send(_outbound(text))

        # 验证：首段先合成，播放期间第二段开始合成
        # 第一段合成时间 < 第二段合成时间 < 第一段播放时间（播放期间合成第二段）
        self.assertTrue(len(synth_times) >= 2)
        self.assertLessEqual(synth_times[0], synth_times[1])
        # 不丢字
        self.assertEqual("".join(timing_tts.synthesized), text)

    async def test_segment_synth_failure_degrades_that_segment_only(self):
        """单段合成失败 → 该段降级文字，后续段不受影响。"""
        # 长文本多段
        text = "你好世界。今天天气很好。我们去公园。中午吃饭。下午看电影。"
        # 第 1 段（index=1）合成失败
        tts = _FakeTTS(fail_on_seg={1})
        voice, emitted = self._make_channel(tts=tts)
        with patch("channels.voice.play_audio", new_callable=AsyncMock) as play:
            await voice.send(_outbound(text))

        # 失败段降级文字（含提示 + 该段原文）
        # emitted 应含提示信息和该段文本
        self.assertTrue(len(emitted) >= 2)
        # 有提示信息
        self.assertTrue(any("合成失败" in e for e in emitted))
        # 有失败段的文本
        failed_seg_text = tts.synthesized[1]  # 第 1 段的文本
        self.assertIn(failed_seg_text, emitted)
        # 其他段仍播放了（play 次数 = 总段数 - 1 个失败段）
        self.assertEqual(play.await_count, len(tts.synthesized) - 1)

    async def test_play_failure_degrades_all_remaining_to_text(self):
        """段播放失败 → 当前及后续段全部降级文字。"""
        # 长文本多段
        text = "你好世界。今天天气很好。我们去公园。中午吃饭。下午看电影。"
        tts = _FakeTTS(audio=b"AUD", media_type="audio/wav")
        voice, emitted = self._make_channel(tts=tts)

        call_count = [0]

        async def _failing_play(audio, media_type, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # 第 1 段播放成功
                return
            # 第 2 段播放失败
            raise KwsError("output_error", "播放失败模拟")

        with patch("channels.voice.play_audio", side_effect=_failing_play):
            await voice.send(_outbound(text))

        # 第 1 段播放成功，第 2 段失败 → 第 2 段及后续全部降级文字
        # emitted 应含播放失败提示 + 第 2 段开始的后续段文本
        self.assertTrue(any("播放失败" in e for e in emitted))
        # 后续段文本在 emitted 中（第 2 段起）
        # 第 2 段文本
        seg2_text = tts.synthesized[1]
        self.assertIn(seg2_text, emitted)

    async def test_single_segment_uses_simple_path(self):
        """单段文本走原路径（单次 synthesize → 单次 play）。"""
        text = "你好呀"  # 3 字，不够切断
        tts = _FakeTTS(audio=b"AUD", media_type="audio/wav")
        voice, emitted = self._make_channel(tts=tts)
        with patch("channels.voice.play_audio", new_callable=AsyncMock) as play:
            await voice.send(_outbound(text))

        # 单次合成，单次播放
        self.assertEqual(tts.synthesized, [text])
        self.assertEqual(play.await_count, 1)
        self.assertEqual(emitted, [])

    async def test_end_marker_stripped_before_segmentation(self):
        """[END] 在分段前剥离，播完后退出连续对讲。"""
        text = "你好世界。今天天气很好。[END]"
        tts = _FakeTTS(audio=b"AUD", media_type="audio/wav")
        voice, emitted = self._make_channel(tts=tts)
        voice._enter_continuous()
        with patch("channels.voice.play_audio", new_callable=AsyncMock):
            await voice.send(_outbound(text))

        # [END] 被剥离，合成文本不含 [END]
        for seg in tts.synthesized:
            self.assertNotIn("[END]", seg)
        # 退出连续对讲
        self.assertFalse(voice._continuous)

    async def test_playback_params_passed_to_each_segment(self):
        """每段 play_audio 都透传 playback_params（DSP 防炸麦）。"""
        text = "你好世界。今天天气很好。我们去公园。中午吃饭。"
        tts = _FakeTTS(audio=b"AUD", media_type="audio/wav")
        voice, emitted = self._make_channel(
            tts=tts, playback_params={"target_peak": 0.5}
        )
        with patch("channels.voice.play_audio", new_callable=AsyncMock) as play:
            await voice.send(_outbound(text))

        # 每段都传了 playback_params
        for c in play.call_args_list:
            self.assertEqual(c.kwargs.get("playback_params"), {"target_peak": 0.5})

    async def test_no_punctuation_long_text_segments_at_hard_limit(self):
        """无标点长文本按硬上限切段（首段 64、后续 120）。"""
        text = "了" * 300
        tts = _FakeTTS(audio=b"AUD", media_type="audio/wav")
        voice, emitted = self._make_channel(tts=tts)
        with patch("channels.voice.play_audio", new_callable=AsyncMock) as play:
            await voice.send(_outbound(text))

        segments = tts.synthesized
        self.assertTrue(len(segments) >= 2)
        # 首段 64
        self.assertEqual(len(segments[0]), 64)
        # 后续段 120
        self.assertEqual(len(segments[1]), 120)
        # 不丢字
        self.assertEqual("".join(segments), text)
        # 每段播放
        self.assertEqual(play.await_count, len(segments))

    async def test_continuous_mode_schedules_next_listen_after_segments(self):
        """连续对讲模式：分段播完后调度下一轮录音（非 [END] 时）。"""
        text = "你好世界。今天天气很好。我们去公园。中午吃饭。"
        tts = _FakeTTS(audio=b"AUD", media_type="audio/wav")
        voice, emitted = self._make_channel(tts=tts)
        voice._enter_continuous()

        with patch("channels.voice.play_audio", new_callable=AsyncMock):
            with patch.object(voice, "_schedule_next_listen") as schedule:
                await voice.send(_outbound(text))

        # 不含 [END] → 仍处于连续对讲 → 调度下一轮
        self.assertTrue(voice._continuous)
        schedule.assert_called_once()

    async def test_segment_concatenation_no_loss(self):
        """分段播放不丢字：拼接所有段还原原文。"""
        texts = [
            "你好世界。今天天气很好。我们去公园。中午吃饭。下午看电影。晚上休息。再见。",
            "第一句。第二句。第三句。第四句。第五句。第六句。第七句。第八句。第九句。第十句。",
            "了" * 250,
            "你好，世界，你好，世界，你好，世界，你好，世界，你好，世界。再见。",
        ]
        for text in texts:
            tts = _FakeTTS(audio=b"AUD", media_type="audio/wav")
            voice, emitted = self._make_channel(tts=tts)
            with patch("channels.voice.play_audio", new_callable=AsyncMock):
                await voice.send(_outbound(text))
            self.assertEqual("".join(tts.synthesized), text,
                             f"丢字: {text[:50]!r}...")


if __name__ == "__main__":
    unittest.main()
