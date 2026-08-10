"""TASK-030 切句器纯函数单测。

覆盖 ``voice/segments.py`` 的 ``segment_text`` 与 ``find_tts_boundary``：
- 长文本按句切分
- 首段可短启动（>=5 字有句号即切）
- 无标点文本兜底硬上限
- 不丢字（拼接还原原文）
- 多种标点边界（。！？；，…等）
- 空文本 / 单句 / 超长无标点
- 后续段更长（39+ 才找强边界）
"""

import unittest

from voice.segments import (
    find_tts_boundary,
    segment_text,
    _choose_cut,
    _is_strong_boundary,
    _is_weak_boundary,
)


class TestBoundaryChars(unittest.TestCase):
    """边界字符判定。"""

    def test_strong_boundaries(self):
        for ch in "。！？!?；;\n":
            self.assertTrue(_is_strong_boundary(ch), f"强边界判定失败: {ch!r}")

    def test_strong_boundary_rejects_non_boundary(self):
        for ch in "，,：:abc。,":
            if ch in "，,：:":
                self.assertFalse(_is_strong_boundary(ch))
            elif ch in "。":
                self.assertTrue(_is_strong_boundary(ch))
            else:
                self.assertFalse(_is_strong_boundary(ch))

    def test_weak_boundaries(self):
        for ch in "，,：:":
            self.assertTrue(_is_weak_boundary(ch), f"弱边界判定失败: {ch!r}")


class TestFindTtsBoundary(unittest.TestCase):
    """find_tts_boundary：在 limit 范围内找最后一个边界。"""

    def test_returns_last_strong_boundary(self):
        buf = "你好。世界。再见。"
        # limit=12 时，强边界在 3(。), 6(。), 9(。)，最后一个 >= min_length 的
        result = find_tts_boundary(buf, 12, min_length=2)
        self.assertEqual(result, 9)  # "你好。世界。再见。" 最后一个 。在 index 8, n=9

    def test_returns_weak_when_no_strong(self):
        buf = "你好，世界，再见，"
        # 只有弱边界（逗号）
        result = find_tts_boundary(buf, 15, min_length=2)
        # 最后一个 ，在 index 8, n=9
        self.assertEqual(result, 9)

    def test_strong_preferred_over_weak(self):
        buf = "你好，世界。再见，"
        # 强边界在 index 5(。), n=6
        # 弱边界在 index 2(，), n=3 和 index 8(，), n=9
        # 强边界优先
        result = find_tts_boundary(buf, 15, min_length=2)
        self.assertEqual(result, 6)

    def test_returns_negative_when_no_boundary(self):
        buf = "你好世界再见你好世界"
        result = find_tts_boundary(buf, 20, min_length=2)
        self.assertEqual(result, -1)

    def test_min_length_filters_early_boundaries(self):
        buf = "你。好。世界。"
        # min_length=5: index 1 的 。(n=2) 被过滤, index 3 的 。(n=4) 被过滤
        # index 6 的 。(n=7) 通过
        result = find_tts_boundary(buf, 10, min_length=5)
        self.assertEqual(result, 7)

    def test_limit_truncates_search(self):
        buf = "你好。世界。再见。"
        # limit=5: 只搜到 index 4, 强边界在 index 2(n=3)
        result = find_tts_boundary(buf, 5, min_length=2)
        self.assertEqual(result, 3)


class TestSegmentText(unittest.TestCase):
    """segment_text：完整文本切段。"""

    def test_empty_text_returns_empty_list(self):
        self.assertEqual(segment_text(""), [])
        self.assertEqual(segment_text("   "), [])
        self.assertEqual(segment_text("\n\n"), [])

    def test_single_short_sentence_returns_one_segment(self):
        """单句短文本不够切断，整段输出。"""
        text = "你好呀"
        result = segment_text(text)
        self.assertEqual(result, [text])

    def test_single_sentence_with_period(self):
        """首段 >=5 字有句号即切。"""
        text = "你好呀。"
        # 3 字 + 句号 = 4 字，i+1=4 >= 5? No. 需要至少 5 字
        # "你好呀。" 只有 4 字，i=0..3, i+1=4 at 。，4 < 5 不触发
        # buf.len=4 < 32 < 64 → cut=-1, 整段输出
        result = segment_text(text)
        self.assertEqual(result, [text])

    def test_first_segment_short_start_with_boundary_at_5(self):
        """首段 >=5 字有强边界即切。"""
        text = "你好世界，小奈在此。"
        # 10 字，句号在 index 9, n=10 >= 5 → cut=10
        result = segment_text(text)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], text)

    def test_long_text_splits_at_sentence_boundaries(self):
        """长文本按句号边界切分。"""
        text = (
            "你好世界。今天天气很好。我们去公园玩吧。"
            "中午一起吃饭。下午去看电影。晚上早点休息。"
        )
        result = segment_text(text)
        # 首段: "你好世界。" (4+1=5 字, n=5 >= 5 → 切！)
        # 实际: 从位置 0 找强边界 >= 5: index 4 是第一个 。(n=5), 5 >= 5 → cut=5
        # 首段 = "你好世界。" (5 字)
        # 后续段: 从位置 39 找强边界
        # buf = "今天天气很好。我们去公园玩吧。中午一起吃饭。下午去看电影。晚上早点休息。"
        #   (36 字)
        # 从 j=39 开始, 但 buf 只有 36 字 → 不进 strong loop
        # fallback: 36 < 80 → 不进 fallback
        # hard: 36 < 120 → cut=-1, 整段输出
        self.assertTrue(len(result) >= 2)
        # 不丢字
        self.assertEqual("".join(result), text)
        # 首段为第一句
        self.assertTrue(result[0].startswith("你好世界"))

    def test_no_punctuation_text_uses_hard_limit(self):
        """无标点长文本走硬上限 64（首段）/ 120（后续）。"""
        text = "了" * 200
        result = segment_text(text)
        self.assertTrue(len(result) >= 2)
        # 首段硬上限 64
        self.assertEqual(len(result[0]), 64)
        # 后续段硬上限 120
        self.assertEqual(len(result[1]), 120)
        # 不丢字
        self.assertEqual("".join(result), text)

    def test_no_loss_concat_restores_original(self):
        """拼接所有段可还原原文（不丢字、不加字）。"""
        texts = [
            "你好。世界。再见。",
            "这是一段没有任何标点的很长很长很长很长很长很长的文字" * 10,
            "你好世界。今天天气很好，我们去公园。中午一起吃饭！下午看电影？晚上休息。",
            "首段短句。后续段比较长一些，有逗号也有句号。继续写更多内容来测试切段效果。再来一句。",
            "",
            "单句无标点但也不够长所以整段输出",
        ]
        for text in texts:
            result = segment_text(text)
            if not text.strip():
                self.assertEqual(result, [])
            else:
                self.assertEqual("".join(result), text, f"丢字: {text!r}")

    def test_multiple_punctuation_types(self):
        """多种标点边界（。！？；，…等）都能正确切分。"""
        text = "你好世界！今天天气很好？我们去公园；中午吃饭。下午看电影。"
        result = segment_text(text)
        self.assertTrue(len(result) >= 1)
        self.assertEqual("".join(result), text)

    def test_newline_is_strong_boundary(self):
        """换行符是强边界。"""
        text = "你好世界\n今天天气很好\n我们去公园\n"
        # 首段: "你好世界\n" (4+1=5 字, n=5 >= 5 → 切！) cut=5
        # 后续段: "今天天气很好\n我们去公园\n" → 从 j=39 无强边界 → 整段输出
        result = segment_text(text)
        self.assertTrue(len(result) >= 1)
        self.assertEqual("".join(result), text)

    def test_first_segment_fallback_at_32(self):
        """首段无强边界但有弱边界时，32 字 fallback 找弱边界。"""
        # 33 字，只有逗号弱边界
        text = "你好，世界，你好，世界，你好，世界，你好，世界，你好，世界，结束"
        result = segment_text(text)
        # 首段: 无强边界 → fallback 32: find_tts_boundary(buf, 32, 12)
        # 弱边界在 3(，), 6(，)... 找最后一个 >= 12 的弱边界
        # 应该切在某个逗号处
        self.assertTrue(len(result) >= 1)
        self.assertEqual("".join(result), text)

    def test_later_segment_strong_start_at_39(self):
        """后续段从位置 39 找强边界，短于 39 不切。"""
        # 首段先切出一段，后续段短于 39 字无强边界 → 整段输出
        text = "首段八字符。后续段比较短只有二十几个字没有句号"
        result = segment_text(text)
        self.assertTrue(len(result) >= 1)
        self.assertEqual("".join(result), text)

    def test_ellipsis_boundary(self):
        """省略号 … 不是强边界（不在正则里），但。是。"""
        text = "你好世界…今天天气很好。再见。"
        # … 不在强边界正则 [。！？!?；;\n] 中
        # 但 。在
        result = segment_text(text)
        self.assertTrue(len(result) >= 1)
        self.assertEqual("".join(result), text)

    def test_each_segment_within_bounds(self):
        """每段长度在合理范围内（首段 <= 64，后续 <= 120）。"""
        text = (
            "这是一段测试文本。"
            + "后续段比较长一些，有逗号也有句号。继续写更多内容来测试切段效果。"
            + "再来一句。还有更多内容。不断地写。写写写写写写写写写写写写。"
            + "继续写更多。" * 5
            + "最后一段。"
        )
        result = segment_text(text)
        for i, seg in enumerate(result):
            if i == 0:
                self.assertLessEqual(len(seg), 64, f"首段超 64: {len(seg)}")
            else:
                self.assertLessEqual(len(seg), 120, f"后续段超 120: {len(seg)}")

    def test_single_very_long_no_punctuation(self):
        """超长无标点文本切段到硬上限。"""
        text = "字" * 500
        result = segment_text(text)
        # 首段 64, 后续各 120
        self.assertEqual(len(result[0]), 64)
        remaining = 500 - 64  # 436
        later_count = (remaining + 119) // 120  # ceil(436/120) = 4
        self.assertEqual(len(result), 1 + later_count)
        self.assertEqual("".join(result), text)

    def test_whitespace_only_stripped_to_empty(self):
        """纯空白/换行文本返回空列表。"""
        self.assertEqual(segment_text("   \n\n   "), [])

    def test_text_with_leading_spaces(self):
        """带前导空格的文本正常处理（空格非边界）。"""
        text = "  你好世界。再见。"
        result = segment_text(text)
        self.assertEqual("".join(result), text)


class TestChooseCut(unittest.TestCase):
    """_choose_cut 直接测试（对照网页端分支逻辑）。"""

    def test_first_segment_strong_boundary_at_5(self):
        """首段：位置 >= 5 有强边界即切。"""
        buf = "你好世界。再见世界。"  # 10 字, 。在 index 4(n=5) 和 9(n=10)
        # 第一个 。at n=5 >= 5 → cut=5（TASK-030 阈值从 8 降为 5）
        self.assertEqual(_choose_cut(0, buf), 5)

    def test_first_segment_short_strong_boundary_below_5(self):
        """首段：强边界 < 5 不切。"""
        buf = "你好。"
        # 。at n=3 < 5 → 不切; len=3 < 32 < 64 → -1
        self.assertEqual(_choose_cut(0, buf), -1)

    def test_first_segment_fallback_weak_at_32(self):
        """首段无强边界，32 字 fallback 找弱边界。"""
        buf = "你好，世界，你好，世界，你好，世界，你好，世界，你好，世界，结束"
        # 无强边界 → fallback 32: find_tts_boundary(buf, 32, 12)
        # 弱边界位置: 3,6,9,12,15,18,21,24,27,30
        # 最后一个 >= 12 的: n=30 (index 29 是 ，)
        result = _choose_cut(0, buf)
        self.assertEqual(result, 30)

    def test_first_segment_hard_limit_64(self):
        """首段无标点，64 字硬切。"""
        buf = "了" * 64
        self.assertEqual(_choose_cut(0, buf), 64)

    def test_first_segment_below_hard_limit_returns_neg(self):
        """首段 < 64 且无边界 → -1。"""
        buf = "了" * 63
        self.assertEqual(_choose_cut(0, buf), -1)

    def test_later_segment_strong_at_39(self):
        """后续段：从位置 39 找强边界。"""
        buf = "了" * 40 + "。"  # 41 字, 。at index 40
        self.assertEqual(_choose_cut(1, buf), 41)

    def test_later_segment_no_strong_below_80(self):
        """后续段 < 80 且无强边界(39+) → -1。"""
        buf = "了" * 50  # 50 字, 无边界
        # 50 < 80 → fallback 不触发; 50 < 120 → hard 不触发 → -1
        self.assertEqual(_choose_cut(1, buf), -1)

    def test_later_segment_hard_limit_120(self):
        """后续段无标点，120 字硬切。"""
        buf = "了" * 120
        self.assertEqual(_choose_cut(1, buf), 120)


if __name__ == "__main__":
    unittest.main()
