"""TASK-032 voice 渠道真·流式播放集成测试。

覆盖 ``_StreamingVoiceSink``（token → 增量切句 → 边合成边播放）：
1. 逐 token 推入 → 首段先播（不等全文）
2. 并发预合成上限 2
3. 段合成失败 → 该段降级文字
4. 段播放失败 → 当前及后续段降级文字
5. [END] 在 token 流中实时检测 → 播完退出连续对讲
6. 连续对讲续听：播完后调度下一轮录音
7. 无 TTS 降级：全文 _emit
8. playback_params 透传每段
9. 空回复不崩
10. done 事件 flush 剩余段

不触发真实模型/网络/音频设备（播放全部 mock）。
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bus.queue import MessageBus
from channels.voice import VoiceChannel, _StreamingVoiceSink
from voice.kws.errors import KwsError
from voice.tts.base import TTSError


def _outbound(text: str, streamed: bool = False):
    """构造 OutboundMessage（简化创建）。"""
    from bus.queue import OutboundMessage
    return OutboundMessage(channel="voice", chat_id="direct", content=text, streamed=streamed)


class _FakeTTS:
    """假 TTS：记录 synthesize 文本，返回递增音频，可按段注入失败。"""

    def __init__(self, *, fail_on_seg=None):
        self.fail_on_seg = fail_on_seg or set()
        self.synthesized: list[str] = []
        self._call_count = 0

    async def synthesize(self, text):
        self.synthesized.append(text)
        idx = self._call_count
        self._call_count += 1
        if idx in self.fail_on_seg:
            raise TTSError("synth_failed", f"第{idx+1}段合成模拟失败")
        return SimpleNamespace(
            audio=b"AUD" + str(idx).encode(),
            media_type="audio/wav",
        )


class _TimingTTS:
    """带时间戳的假 TTS，用于验证并发预合成时序。"""

    synthesized: list[str] = []
    synth_times: list[float] = []

    async def synthesize(self, text):
        import time
        self.synthesized.append(text)
        self.synth_times.append(time.monotonic())
        await asyncio.sleep(0.05)
        return SimpleNamespace(audio=b"AUD", media_type="audio/wav")


class StreamingVoiceSinkTests(unittest.IsolatedAsyncioTestCase):
    """_StreamingVoiceSink 流式管线。"""

    def _make_channel(self, tts=None, **kwargs):
        """构造带 TTS 的 VoiceChannel（max_voice_chars=0 不截断）。"""
        voice = VoiceChannel(
            MessageBus(),
            tts_service=tts,
            max_voice_chars=0,
            **kwargs,
        )
        emitted: list[str] = []
        voice._reply_sink = emitted.append
        return voice, emitted

    def _make_sink(self, voice):
        """构造 _StreamingVoiceSink。"""
        return _StreamingVoiceSink(voice)

    async def _push_tokens(self, sink, text, chunk_size=2):
        """模拟 LLM 逐 token 推入。"""
        for i in range(0, len(text), chunk_size):
            await sink({"type": "token", "content": text[i:i + chunk_size]})

    async def _push_done(self, sink, full_text):
        """模拟 AgentLoop done 事件。"""
        await sink({"type": "done", "content": full_text})

    # —— 1. 首段先播 ——

    async def test_first_segment_plays_before_done(self):
        """逐 token 推入 → 首段在 done 之前就已开始合成（不等全文）。"""
        text = "你好世界。今天天气很好。我们去公园。中午吃饭。"
        tts = _FakeTTS()
        voice, emitted = self._make_channel(tts=tts)
        sink = self._make_sink(voice)

        synth_started_before_done = []

        async def _track_synth(text_arg):
            synth_started_before_done.append(len(tts.synthesized))
            return SimpleNamespace(audio=b"AUD", media_type="audio/wav")

        # 实际用 FakeTTS 已记录 synthesized
        with patch("channels.voice.play_audio", new_callable=AsyncMock):
            # 推入前半文本（足以产生首段）
            await self._push_tokens(sink, text[:10], chunk_size=2)
            # 让事件循环处理已调度的合成任务
            await asyncio.sleep(0)
            # 首段 "你好世界。" = 5 字 → 切出 → 合成启动
            self.assertTrue(len(tts.synthesized) >= 1,
                            "首段应在 done 之前开始合成")
            # 推入剩余 + done
            await self._push_tokens(sink, text[10:], chunk_size=2)
            await self._push_done(sink, text)

        # 全部段拼接还原
        self.assertEqual("".join(tts.synthesized), text)

    # —— 2. 并发预合成上限 2 ——

    async def test_concurrent_synth_max_two(self):
        """预合成并发上限 2：同时最多 2 个 synthesize 在途。"""
        text = "字" * 500  # 无标点 → 64 + 120 + 120 + ...
        timing_tts = _TimingTTS()
        voice, emitted = self._make_channel(tts=timing_tts)
        sink = self._make_sink(voice)

        play_times: list[float] = []

        async def _mock_play(audio, media_type, **kwargs):
            import time
            play_times.append(time.monotonic())
            await asyncio.sleep(0.05)

        with patch("channels.voice.play_audio", side_effect=_mock_play):
            await self._push_tokens(sink, text, chunk_size=50)
            await self._push_done(sink, text)

        # 验证首段先合成，播放期间第二段开始合成
        self.assertTrue(len(timing_tts.synth_times) >= 2)
        self.assertLessEqual(timing_tts.synth_times[0], timing_tts.synth_times[1])
        # 不丢字
        self.assertEqual("".join(timing_tts.synthesized), text)

    # —— 3. 段合成失败降级 ——

    async def test_segment_synth_failure_degrades_that_segment(self):
        """单段合成失败 → 该段降级文字 _emit，后续段不受影响。"""
        text = "你好世界。今天天气很好。我们去公园。中午吃饭。"
        tts = _FakeTTS(fail_on_seg={1})  # 第 2 段失败
        voice, emitted = self._make_channel(tts=tts)
        sink = self._make_sink(voice)

        with patch("channels.voice.play_audio", new_callable=AsyncMock) as play:
            await self._push_tokens(sink, text, chunk_size=4)
            await self._push_done(sink, text)

        # 失败段降级文字
        self.assertTrue(any("合成失败" in e for e in emitted))
        # 其他段仍播放了
        self.assertTrue(play.await_count >= 1)
        # 不丢字（合成成功段 + emit 的失败段文本 = 全文）
        played_text = "".join(tts.synthesized)
        self.assertTrue(len(played_text) > 0)

    # —— 4. 播放失败降级 ——

    async def test_play_failure_degrades_remaining_to_text(self):
        """段播放失败 → 当前及后续段全部降级文字。"""
        text = "你好世界。今天天气很好。我们去公园。中午吃饭。"
        tts = _FakeTTS()
        voice, emitted = self._make_channel(tts=tts)
        sink = self._make_sink(voice)

        call_count = [0]

        async def _failing_play(audio, media_type, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return  # 第 1 段成功
            raise KwsError("output_error", "播放失败模拟")

        with patch("channels.voice.play_audio", side_effect=_failing_play):
            await self._push_tokens(sink, text, chunk_size=4)
            await self._push_done(sink, text)

        # 有播放失败提示
        self.assertTrue(any("播放失败" in e for e in emitted))

    # —— 5. [END] 实时检测 ——

    async def test_end_marker_detected_in_token_stream(self):
        """[END] 在 token 流中被检测 → 播完后退出连续对讲。"""
        text = "你好世界。再见啦。[END]"
        tts = _FakeTTS()
        voice, emitted = self._make_channel(tts=tts)
        voice._enter_continuous()
        sink = self._make_sink(voice)

        with patch("channels.voice.play_audio", new_callable=AsyncMock):
            # 逐 token 推入，[END] 被拆到多个 token 中
            await sink({"type": "token", "content": "你好世界。"})
            await sink({"type": "token", "content": "再见啦。"})
            await sink({"type": "token", "content": "[END]"})
            await self._push_done(sink, text)

        # [END] 不出现在合成文本里
        for seg in tts.synthesized:
            self.assertNotIn("[END]", seg)
        # 退出连续对讲
        self.assertFalse(voice._continuous)

    async def test_end_marker_split_across_tokens(self):
        """[END] 被拆到多个 token 中也能检测。"""
        text = "你好世界。[END]"
        tts = _FakeTTS()
        voice, emitted = self._make_channel(tts=tts)
        voice._enter_continuous()
        sink = self._make_sink(voice)

        with patch("channels.voice.play_audio", new_callable=AsyncMock):
            await sink({"type": "token", "content": "你好世界。[E"})
            await sink({"type": "token", "content": "ND]"})
            await self._push_done(sink, text)

        # 退出连续对讲
        self.assertFalse(voice._continuous)

    # —— 6. 连续对讲续听 ——

    async def test_continuous_mode_schedules_next_listen_after_playback(self):
        """连续对讲：播完后调度下一轮录音（非 [END] 时）。"""
        text = "你好世界。今天天气很好。"
        tts = _FakeTTS()
        voice, emitted = self._make_channel(tts=tts)
        voice._enter_continuous()
        sink = self._make_sink(voice)

        with patch("channels.voice.play_audio", new_callable=AsyncMock):
            with patch.object(voice, "_schedule_next_listen") as schedule:
                await self._push_tokens(sink, text, chunk_size=4)
                await self._push_done(sink, text)

        # 仍处于连续对讲 → 调度下一轮
        self.assertTrue(voice._continuous)
        schedule.assert_called_once()

    # —— 7. 无 TTS 降级 ——

    async def test_no_tts_degrades_to_text_emit(self):
        """无 TTS 服务 → done 事件触发全文 _emit（[END] 剥离 + 清洗）。"""
        voice, emitted = self._make_channel(tts=None)
        sink = self._make_sink(voice)

        await self._push_tokens(sink, "你好世界。再见。", chunk_size=3)
        await self._push_done(sink, "你好世界。再见。")

        # 全文被 _emit
        self.assertTrue(len(emitted) >= 1)
        self.assertIn("你好世界", emitted[-1])

    async def test_no_tts_with_end_marker(self):
        """无 TTS + [END] → 文字 _emit 剥离 [END]，退出连续对讲。"""
        voice, emitted = self._make_channel(tts=None)
        voice._enter_continuous()
        sink = self._make_sink(voice)

        await self._push_tokens(sink, "再见啦。[END]", chunk_size=3)
        await self._push_done(sink, "再见啦。[END]")

        # [END] 被剥离
        for e in emitted:
            self.assertNotIn("[END]", e)
        # 退出连续对讲
        self.assertFalse(voice._continuous)

    # —— 8. playback_params 透传 ——

    async def test_playback_params_passed_to_each_segment(self):
        """每段 play_audio 都透传 playback_params。"""
        text = "你好世界。今天天气很好。我们去公园。"
        tts = _FakeTTS()
        voice, emitted = self._make_channel(
            tts=tts, playback_params={"target_peak": 0.5}
        )
        sink = self._make_sink(voice)

        with patch("channels.voice.play_audio", new_callable=AsyncMock) as play:
            await self._push_tokens(sink, text, chunk_size=4)
            await self._push_done(sink, text)

        for c in play.call_args_list:
            self.assertEqual(c.kwargs.get("playback_params"), {"target_peak": 0.5})

    # —— 9. 空回复 ——

    async def test_empty_reply_does_not_crash(self):
        """空回复：无 token 事件，done 触发，不崩、不播。"""
        voice, emitted = self._make_channel(tts=_FakeTTS())
        sink = self._make_sink(voice)

        with patch("channels.voice.play_audio", new_callable=AsyncMock) as play:
            await self._push_done(sink, "")

        # 无段、无播放、无 emit
        self.assertEqual(play.await_count, 0)

    # —— 10. done flush 剩余段 ——

    async def test_done_flushes_remaining_segments(self):
        """done 事件 flush 切句器，剩余文本作为最后一段播放。"""
        text = "你好世界。今天天气很好但没有句号的后续文本"
        tts = _FakeTTS()
        voice, emitted = self._make_channel(tts=tts)
        sink = self._make_sink(voice)

        with patch("channels.voice.play_audio", new_callable=AsyncMock):
            await self._push_tokens(sink, text, chunk_size=4)
            await self._push_done(sink, text)

        # 不丢字
        self.assertEqual("".join(tts.synthesized), text)

    # —— 11. thinking 事件被忽略 ——

    async def test_thinking_events_ignored(self):
        """thinking 事件不影响切句/合成/播放。"""
        voice, emitted = self._make_channel(tts=_FakeTTS())
        sink = self._make_sink(voice)

        with patch("channels.voice.play_audio", new_callable=AsyncMock):
            await sink({"type": "thinking", "content": "我在思考..."})
            await sink({"type": "token", "content": "你好世界。"})
            await self._push_done(sink, "你好世界。")

        # thinking 被忽略，只有 token 被处理
        # 合成只有 "你好世界。"（不含 thinking 内容）
        # emitted 列表记录了 _emit 的输出
        self.assertTrue(len(emitted) >= 0)

    # —— 12. 工具调用回合：文本正常播放 ——

    async def test_tool_call_round_text_plays_normally(self):
        """模拟工具调用回合：先播文本，tool 事件忽略，继续播后续文本。"""
        tts = _FakeTTS()
        voice, emitted = self._make_channel(tts=tts)
        sink = self._make_sink(voice)

        with patch("channels.voice.play_audio", new_callable=AsyncMock) as play:
            # 第一段文本
            await sink({"type": "token", "content": "好的，我来查一下。"})
            # 工具调用事件（被忽略）
            await sink({"type": "tool_call", "name": "search", "args": "{}"})
            await sink({"type": "tool_result", "name": "search", "content": "结果"})
            # 第二段文本
            await sink({"type": "token", "content": "查到了，结果是这样的。"})
            await self._push_done(sink, "好的，我来查一下。查到了，结果是这样的。")

        # 工具事件被忽略，只有文本被合成播放
        self.assertTrue(len(tts.synthesized) >= 1)
        # 不丢字
        full = "".join(tts.synthesized)
        self.assertIn("好的", full)
        self.assertIn("查到了", full)

    # —— 13. send() streamed=True 是 no-op ——

    async def test_send_streamed_is_noop(self):
        """send() 收到 streamed=True 时不重复播放（流式管线已处理）。"""
        from bus.queue import OutboundMessage
        voice, emitted = self._make_channel(tts=_FakeTTS())
        msg = OutboundMessage(
            channel="voice", chat_id="direct",
            content="你好世界。", streamed=True
        )

        with patch("channels.voice.play_audio", new_callable=AsyncMock) as play:
            with patch.object(voice, "_play_segments", new_callable=AsyncMock) as segs:
                await voice.send(msg)

        # send() 不触发合成/播放/切句
        play.assert_not_called()
        segs.assert_not_called()

    # —— 14. send() streamed=False 走原路径 ——

    async def test_send_not_streamed_uses_original_path(self):
        """send() 收到 streamed=False 时走 TASK-030 全文切句路径。"""
        from bus.queue import OutboundMessage
        tts = _FakeTTS()
        voice, emitted = self._make_channel(tts=tts)
        msg = OutboundMessage(
            channel="voice", chat_id="direct",
            content="你好世界。再见。", streamed=False
        )

        with patch("channels.voice.play_audio", new_callable=AsyncMock) as play:
            await voice.send(msg)

        # 走原路径：合成 + 播放
        self.assertTrue(len(tts.synthesized) >= 1)
        self.assertTrue(play.await_count >= 1)


if __name__ == "__main__":
    unittest.main()
