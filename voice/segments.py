"""TTS 文本分段切句器（TASK-030）。

把网页端 ``webui/index.html`` 的 ``chooseTtsCut`` 切句策略移植到 Python，
供 voice 渠道分段流式播放使用。算法规则与网页端对齐：

- **强边界**：``。！？!?；;`` 与换行 ``\\n``
- **弱边界**：``，,：:``
- **首段**（segment_count == 0）：
  - 从位置 0 扫描，找到强边界且位置 ≥ 5 → 在该位置 +1 切断
  - 无强边界 → 若 buf ≥ 32 字，在首 32 字内找边界（强优先、弱次选），最小 12 字
  - 仍无 → 若 buf ≥ 64 字，硬上限 64（优先边界，否则硬切）
  - 否则返回 -1（等更多文本）
- **后续段**（segment_count > 0）：
  - 从位置 39 扫描，找强边界 → 在该位置 +1 切断
  - 无强边界 → 若 buf ≥ 80 字，在首 80 字内找边界，最小 40 字
  - 仍无 → 若 buf ≥ 120 字，硬上限 120
  - 否则返回 -1

``find_tts_boundary`` 在 limit 范围内找 **最后一个** 强边界（无则最后一个弱
边界），位置需 ≥ min_length——与网页端 ``findTtsBoundary`` 语义完全一致。

``segment_text(text)`` 是纯函数：输入完整文本，输出段列表。内部模拟网页端
``flushTtsSegments(force=True)`` 的最终刷出循环（逐段 chooseTtsCut，余量
不足时整段输出）。``[END]`` 等标记由调用方在 ``send()`` 前剥离，切句器不处理。
"""

from __future__ import annotations

import re

# —— 边界字符正则（与网页端 isStrongTtsBoundary / findTtsBoundary 对齐）——
_STRONG_BOUNDARY_RE = re.compile(r"[。！？!?；;\n]")
_WEAK_BOUNDARY_RE = re.compile(r"[，,：:]")

# —— 硬编码阈值常量（与网页端 chooseTtsCut 同款，初版无需配置）——
# 首段：从位置 0 找强边界，最小 5 字即可启动（TASK-030 从 8 降为 5）
_FIRST_STRONG_MIN = 5
# 首段 fallback：buf >= 32 时在首 32 字找边界，最小 12 字（TASK-030 从 16 降为 12）
_FIRST_FALLBACK_AT = 32
_FIRST_FALLBACK_MIN = 12
# 首段硬上限：buf >= 64 时硬切 64（优先边界）
_FIRST_HARD_LIMIT = 64

# 后续段：从位置 39 找强边界
_LATER_STRONG_START = 39
# 后续段 fallback：buf >= 80 时在首 80 字找边界，最小 40 字
_LATER_FALLBACK_AT = 80
_LATER_FALLBACK_MIN = 40
# 后续段硬上限：buf >= 120 时硬切 120
_LATER_HARD_LIMIT = 120


def _is_strong_boundary(ch: str) -> bool:
    """判断字符是否为强边界（句末标点或换行）。"""
    return bool(_STRONG_BOUNDARY_RE.match(ch))


def _is_weak_boundary(ch: str) -> bool:
    """判断字符是否为弱边界（逗号、冒号等分句标点）。"""
    return bool(_WEAK_BOUNDARY_RE.match(ch))


def find_tts_boundary(buf: str, limit: int, min_length: int) -> int:
    """在 ``buf`` 前 ``limit`` 个字符范围内找切句位置。

    遍历 0..limit-1，记录最后一个满足 ``n >= min_length`` 的强边界位置
    （优先）与弱边界位置（次选）。返回强边界（有则取最后一个），否则弱
    边界，否则 -1。

    与网页端 ``findTtsBoundary`` 语义完全一致：取 **最后一个** 边界而非
    第一个——这样段落尽量长、包含更多完整短句，听感更自然。
    """
    strong = -1
    weak = -1
    upper = min(len(buf), limit)
    for i in range(upper):
        n = i + 1
        if n < min_length:
            continue
        ch = buf[i]
        if _is_strong_boundary(ch):
            strong = n
        elif _is_weak_boundary(ch):
            weak = n
    return strong if strong >= 0 else weak


def _choose_cut(segment_count: int, buf: str) -> int:
    """对一段缓冲文本决定切句位置（返回切点字数，-1 表示等更多文本）。

    直接对照网页端 ``chooseTtsCut(turn, buf)`` 的分支逻辑：
    ``segment_count`` 对应 ``turn.ttsSegmentCount``。
    """
    first_segment = segment_count == 0

    if first_segment:
        # 首段：从位置 0 找强边界，位置 >= 5 即可启动
        for i in range(len(buf)):
            if i + 1 >= _FIRST_STRONG_MIN and _is_strong_boundary(buf[i]):
                return i + 1
    else:
        # 后续段：从位置 39 找强边界
        for j in range(_LATER_STRONG_START, len(buf)):
            if _is_strong_boundary(buf[j]):
                return j + 1

    # fallback：首段在 32 字处找分句边界，后续在 80 字处
    fallback_at = _FIRST_FALLBACK_AT if first_segment else _LATER_FALLBACK_AT
    fallback_min = _FIRST_FALLBACK_MIN if first_segment else _LATER_FALLBACK_MIN
    if len(buf) >= fallback_at:
        long_boundary = find_tts_boundary(buf, fallback_at, fallback_min)
        if long_boundary >= 0:
            return long_boundary

    # 硬上限：首段 64，后续 120
    hard_limit = _FIRST_HARD_LIMIT if first_segment else _LATER_HARD_LIMIT
    if len(buf) >= hard_limit:
        safe_boundary = find_tts_boundary(buf, hard_limit, fallback_min)
        return safe_boundary if safe_boundary >= 0 else hard_limit

    return -1


def segment_text(text: str) -> list[str]:
    """把完整文本切分为 TTS 播放段列表（纯函数）。

    内部模拟网页端 ``flushTtsSegments(turn, force=True)`` 的最终刷出循环：
    逐段调用 ``_choose_cut``，切点 >= 0 则截取并推进，余量不足以切断时整段
    作为最后一段输出（force 模式，不丢字）。

    - 空文本 → ``[]``
    - 纯空白 → ``[]``
    - 单句短文本 → ``[text]``（不够切断，整段输出）
    - 长文本 → 多段（每段 ~16~120 字）
    - 拼接所有段可还原原文（不丢字、不加字）
    """
    if not text or not text.strip():
        return []

    segments: list[str] = []
    buf = text
    segment_count = 0

    while buf:
        cut = _choose_cut(segment_count, buf)
        if cut < 0:
            # 余量不足以切断（force 模式）：整段作为最后一段
            if buf.strip():
                segments.append(buf)
            break
        segments.append(buf[:cut])
        segment_count += 1
        buf = buf[cut:]

    return segments
