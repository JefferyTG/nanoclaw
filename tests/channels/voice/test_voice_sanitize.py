"""TASK-027 补充：voice 渠道 TTS 前文本清洗（_sanitize_for_tts）专项测试。

覆盖：markdown 剥离（链接/加粗/代码/行首标题/列表/引用/数字列表）、
emoji 与装饰符号删除（emoji 块/箭头/几何/杂项/变体选择符/ZWJ）、
连续标点压缩、多余空白压缩；并验证 send() 链路实际合成的是清洗后文本。
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

from channels.voice import VoiceChannel


class SanitizeForTtsTests(unittest.TestCase):
    """_sanitize_for_tts 静态方法单测。"""

    def test_markdown_bold(self):
        self.assertEqual(
            VoiceChannel._sanitize_for_tts("这个**加粗**和__双下划线__都没了"),
            "这个加粗和双下划线都没了",
        )

    def test_markdown_code(self):
        self.assertEqual(
            VoiceChannel._sanitize_for_tts("用 `read_file` 读取文件"),
            "用 read_file 读取文件",
        )

    def test_markdown_link(self):
        self.assertEqual(
            VoiceChannel._sanitize_for_tts("看[这篇文档](https://example.com/a)就行"),
            "看这篇文档就行",
        )

    def test_markdown_line_markers(self):
        self.assertEqual(
            VoiceChannel._sanitize_for_tts("# 标题\n- 列表项\n> 引用\n1. 第一点"),
            "标题\n列表项\n引用\n第一点",
        )

    def test_emoji_removed(self):
        self.assertEqual(
            VoiceChannel._sanitize_for_tts("哈哈😊 好开心❤️✨"),
            "哈哈 好开心",
        )

    def test_symbols_removed(self):
        # 箭头/几何/装饰符号：→ ★ ☆ ◆ ◇ ● ○ ◎ △ ▲ ■ □ ♪ ☺ ✓
        self.assertEqual(
            VoiceChannel._sanitize_for_tts("看箭头→这里★☆◆◇●○◎△▲■□♪☺✓"),
            "看箭头这里",
        )

    def test_repeat_punct_compressed(self):
        self.assertEqual(
            VoiceChannel._sanitize_for_tts("太好了！！！真的？？。。。"),
            "太好了！真的？。",
        )

    def test_spaces_compressed(self):
        self.assertEqual(
            VoiceChannel._sanitize_for_tts("有  多余   空格　全角"),
            "有 多余 空格 全角",
        )

    def test_chinese_punct_kept(self):
        # 中文标点【】「」（）等保留（TTS 可正常读）
        text = "我叫「小奈」，外号（甜甜的）～"
        self.assertEqual(VoiceChannel._sanitize_for_tts(text), text)

    def test_plain_text_unchanged(self):
        text = "今天天气不错，我们去散步吧。"
        self.assertEqual(VoiceChannel._sanitize_for_tts(text), text)

    def test_empty_and_none(self):
        self.assertEqual(VoiceChannel._sanitize_for_tts(""), "")
        self.assertEqual(VoiceChannel._sanitize_for_tts(None), "")

    def test_mixed_long_text(self):
        src = (
            "好的乖宝～**这个问题**很简单：\n"
            "1. 先看 `config.py` 里的白名单\n"
            "2. 再调参数→就好啦！😊"
        )
        want = "好的乖宝～这个问题很简单：\n先看 config.py 里的白名单\n再调参数就好啦！"
        self.assertEqual(VoiceChannel._sanitize_for_tts(src), want)


class SendSanitizeIntegrationTests(unittest.TestCase):
    """send() 链路：TTS 合成收到的是清洗后文本，文字兜底也用清洗后文本。"""

    def _make_channel(self):
        bus = MagicMock()
        ch = VoiceChannel(bus)
        return ch

    def test_tts_synthesizes_cleaned_text(self):
        ch = self._make_channel()
        ch._tts_service = MagicMock()
        ch._tts_service.synthesize = AsyncMock(return_value=MagicMock(
            audio=b"wav", media_type="audio/wav",
        ))
        # play_audio 走真实模块：patch 掉
        from unittest.mock import patch
        with patch("channels.voice.play_audio", new=AsyncMock()) as play:
            from bus.queue import OutboundMessage
            import asyncio
            asyncio.run(ch.send(OutboundMessage(
                channel="voice", chat_id="local:0", content="好的乖宝～😊 这是**重点**！",
            )))
            synth_text = ch._tts_service.synthesize.await_args.args[0]
            self.assertEqual(synth_text, "好的乖宝～ 这是重点！")
            play.assert_awaited_once()

    def test_text_fallback_uses_cleaned_text(self):
        ch = self._make_channel()
        ch._tts_service = None  # 未配置 TTS → 纯文字兜底
        replies = []
        ch._reply_sink = lambda text: replies.append(text)
        from bus.queue import OutboundMessage
        import asyncio
        asyncio.run(ch.send(OutboundMessage(
            channel="voice", chat_id="local:0", content="好的✨ 我记住了！",
        )))
        self.assertEqual(replies, ["好的 我记住了！"])

    def test_all_emoji_reply_plays_nothing(self):
        ch = self._make_channel()
        ch._tts_service = None
        replies = []
        ch._reply_sink = lambda text: replies.append(text)
        from bus.queue import OutboundMessage
        import asyncio
        asyncio.run(ch.send(OutboundMessage(
            channel="voice", chat_id="local:0", content="😊😊😊",
        )))
        self.assertEqual(replies, [])  # 清洗后为空 → 什么都不播


if __name__ == "__main__":
    unittest.main()
