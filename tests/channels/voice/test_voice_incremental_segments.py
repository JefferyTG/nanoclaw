"""TASK-032 增量切句器（IncrementalSegmenter）单测。

覆盖 ``voice/segments.py`` 的 :class:`IncrementalSegmenter`：
- 逐 token 喂入，验证切出段正确
- 不丢字（feed + flush 拼接还原所有喂入文本）
- 与全文 segment_text 结果一致
- flush 切完剩余
- 空文本/None 喂入不崩
- 多种喂入粒度（逐字、逐词、大块）
"""

import unittest

from voice.segments import IncrementalSegmenter, segment_text


class TestIncrementalSegmenter(unittest.TestCase):
    """IncrementalSegmenter feed/flush 接口。"""

    def test_empty_feed_returns_empty(self):
        """空文本/None 喂入不追加缓冲，返回空列表。"""
        seg = IncrementalSegmenter()
        self.assertEqual(seg.feed(""), [])
        self.assertEqual(seg.feed(None), [])  # type: ignore

    def test_flush_empty_returns_empty(self):
        """无缓冲时 flush 返回空列表。"""
        seg = IncrementalSegmenter()
        self.assertEqual(seg.flush(), [])

    def test_single_short_text_flush_returns_whole(self):
        """短文本不够切断 → feed 返回空，flush 返回整段。"""
        seg = IncrementalSegmenter()
        self.assertEqual(seg.feed("你好呀"), [])
        self.assertEqual(seg.flush(), ["你好呀"])

    def test_first_segment_strong_boundary(self):
        """首段 >=5 字有强边界即切。"""
        seg = IncrementalSegmenter()
        # "你好世界。" = 5 字, 。at index 4 (n=5 >= 5) → 切
        result = seg.feed("你好世界。")
        self.assertEqual(result, ["你好世界。"])
        # 后续 flush 为空
        self.assertEqual(seg.flush(), [])

    def test_incremental_feeds_match_full_text_segmentation(self):
        """逐字喂入与全文 segment_text 结果一致。"""
        text = "你好世界。今天天气很好。我们去公园。中午吃饭。下午看电影。晚上休息。再见。"
        full = segment_text(text)

        seg = IncrementalSegmenter()
        inc = []
        for ch in text:
            inc.extend(seg.feed(ch))
        inc.extend(seg.flush())

        self.assertEqual(inc, full)

    def test_no_loss_concat_restores_original(self):
        """feed + flush 拼接还原所有喂入文本（不丢字、不加字）。"""
        texts = [
            "你好。世界。再见。",
            "这是一段没有任何标点的很长很长很长很长很长很长的文字" * 10,
            "你好世界。今天天气很好，我们去公园。中午一起吃饭！下午看电影？晚上休息。",
            "首段短句。后续段比较长一些，有逗号也有句号。继续写更多内容来测试切段效果。再来一句。",
            "",
            "单句无标点但也不够长所以整段输出",
            "了" * 500,
        ]
        for text in texts:
            seg = IncrementalSegmenter()
            inc = []
            # 模拟逐 token 喂入（不同粒度）
            chunk_size = 3
            for i in range(0, len(text), chunk_size):
                inc.extend(seg.feed(text[i:i + chunk_size]))
            inc.extend(seg.flush())

            if not text.strip():
                self.assertEqual(inc, [])
            else:
                self.assertEqual("".join(inc), text, f"丢字: {text[:50]!r}...")

    def test_large_chunk_feed(self):
        """大块文本一次喂入也能正确切段。"""
        text = "你好世界。今天天气很好。我们去公园。中午吃饭。"
        seg = IncrementalSegmenter()
        result = seg.feed(text)
        result.extend(seg.flush())
        self.assertEqual("".join(result), text)
        # 与全文切句一致
        self.assertEqual(result, segment_text(text))

    def test_multiple_feeds_accumulate_buffer(self):
        """多次 feed 追加缓冲，攒够一句才切。"""
        seg = IncrementalSegmenter()
        # 逐字喂入"你好世"，不够 5 字有强边界
        self.assertEqual(seg.feed("你"), [])
        self.assertEqual(seg.feed("好"), [])
        self.assertEqual(seg.feed("世"), [])
        # 喂"界。"→ 缓冲 "你好世界。" → 切
        result = seg.feed("界。")
        self.assertEqual(result, ["你好世界。"])
        self.assertEqual(seg.flush(), [])

    def test_flush_after_partial_feed(self):
        """feed 部分文本后 flush 强制切出剩余。"""
        seg = IncrementalSegmenter()
        seg.feed("你好世界")
        # 缓冲 "你好世界"（4 字 < 5 无强边界，< 32 无 fallback，< 64 无硬限）
        self.assertEqual(seg.flush(), ["你好世界"])

    def test_segment_count_progression(self):
        """首段短、后续段长的分段逻辑在增量模式下正确。"""
        # 构造首段短 + 后续超长文本
        text = "首段。" + "了" * 200  # 首段 3 字，不够 5 字强边界
        # 实际 "首段。" = 3 字 + 句号 = 3+1=4 < 5 → 不切
        # 继续喂 "了"*200 → 缓冲 "首段。了了了..." 
        # 等等，"首段。" 的 。at index 2, n=3 < 5 → 不切
        # 但后续喂入多了字，buf 变长，从位置 0 重新扫描...
        # Actually _choose_cut(0, "首段。了了...") → 。at index 2, n=3 < 5
        # → 不切。继续扫描... 了不是边界。fallback at 32: find_tts_boundary(buf, 32, 12)
        # "首段。了了了..." 无弱边界在 32 内（了不是边界）
        # hard limit 64: 切 64
        seg = IncrementalSegmenter()
        result = seg.feed(text)
        result.extend(seg.flush())
        self.assertEqual("".join(result), text)
        # 首段走硬上限 64
        self.assertEqual(len(result[0]), 64)

    def test_whitespace_only_flush_returns_empty(self):
        """纯空白 flush 返回空列表。"""
        seg = IncrementalSegmenter()
        seg.feed("   \n\n   ")
        self.assertEqual(seg.flush(), [])

    def test_newline_as_strong_boundary(self):
        """换行符是强边界，喂入时即切。"""
        seg = IncrementalSegmenter()
        # "你好世界\n" = 5 字, \n at index 4 (n=5 >= 5) → 切
        result = seg.feed("你好世界\n")
        self.assertEqual(result, ["你好世界\n"])

    def test_feed_returns_multiple_segments(self):
        """一次 feed 可切出多段（缓够长时）。"""
        seg = IncrementalSegmenter()
        # 喂入一段超长无标点文本 → 走硬上限切出多段
        text = "了" * 200
        result = seg.feed(text)
        # 首段 64，后续段 120
        self.assertEqual(len(result[0]), 64)
        self.assertEqual(len(result[1]), 120)
        # 还有剩余
        remaining = seg.flush()
        self.assertTrue(len(remaining) >= 1)
        # 不丢字
        self.assertEqual("".join(result) + "".join(remaining), text)

    def test_end_marker_not_processed_by_segmenter(self):
        """[END] 标记由调用方剥离，切句器不处理——喂入含 [END] 的文本正常切段。"""
        seg = IncrementalSegmenter()
        result = seg.feed("你好世界。[END]")
        # "你好世界。" = 5 字 → 切
        self.assertEqual(result, ["你好世界。"])
        # 剩余 "[END]" 在缓冲
        remaining = seg.flush()
        self.assertEqual(remaining, ["[END]"])


if __name__ == "__main__":
    unittest.main()
