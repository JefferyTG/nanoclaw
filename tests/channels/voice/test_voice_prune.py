"""TASK-026 子任务 2「voice 会话保留上限」专项测试。

覆盖 ``VoiceChannel._prune_old_sessions`` 的清理时机与边界：
1. max_sessions=2 + mock session_pruner：连续 /new 三次（seq 到 3，共 4 个
   会话 0..3 超上限）→ pruner 被调用删最老 seq 0、1，剩余 2、3；
2. session_pruner=None 时超限不崩（不调用任何删除）；
3. max_sessions ≤ 0 不限制（pruner 永不调用）；
4. 空闲分片触发新会话后同样 prune（组合场景：分片 + 保留上限同时生效）。

全程用 mock pruner（只记录调用），绝不触碰真实 workspace/sessions 文件。
"""

import unittest

from bus.queue import MessageBus
from channels.voice import VoiceChannel


class _Clock:
    """可手动推进的可变时钟（组合场景用于触发空闲分片）。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


class _RecordingPruner:
    """记录被调用序号的 mock pruner。"""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, seq: int) -> None:
        self.calls.append(seq)


class VoicePruneTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_sessions_beyond_limit_prune_oldest(self):
        pruner = _RecordingPruner()
        voice = VoiceChannel(
            MessageBus(), max_sessions=2, session_pruner=pruner
        )
        await voice.inject_text("/new")  # seq=1：会话 0,1 共 2 个，未超限
        await voice.inject_text("/new")  # seq=2：会话 0,1,2 共 3 个 → 删最老 0
        await voice.inject_text("/new")  # seq=3：会话 0..3 共 4 个 → 删最老 0、1
        # 最老两个被清理，剩余会话 2、3；当前会话是最新序号 3，不会被误删
        self.assertEqual(voice._session_seq, 3)
        self.assertEqual(voice._current_session, 3)
        self.assertEqual(set(pruner.calls), {0, 1})
        self.assertNotIn(2, pruner.calls)
        self.assertNotIn(3, pruner.calls)
        # 第二次 /new 删 0；第三次 /new 再删 0、1（0 幂等重复，见实现注释）
        self.assertEqual(len(pruner.calls), 3)

    async def test_pruner_none_no_crash_when_over_limit(self):
        voice = VoiceChannel(MessageBus(), max_sessions=2, session_pruner=None)
        for _ in range(5):
            await voice.inject_text("/new")
        # 超限但未注入 pruner：不崩、不调用任何删除，新会话照常创建
        self.assertEqual(voice._session_seq, 5)
        self.assertEqual(voice._current_session, 5)

    async def test_unlimited_max_sessions_never_prunes(self):
        pruner = _RecordingPruner()
        voice = VoiceChannel(
            MessageBus(), max_sessions=0, session_pruner=pruner
        )
        for _ in range(5):
            await voice.inject_text("/new")
        self.assertEqual(voice._session_seq, 5)
        self.assertEqual(pruner.calls, [])  # 不限制 → 永不清理

    async def test_idle_split_prunes_combined(self):
        pruner = _RecordingPruner()
        clock = _Clock()
        voice = VoiceChannel(
            MessageBus(),
            idle_ttl_sec=30.0,
            now_fn=clock,
            max_sessions=2,
            session_pruner=pruner,
        )
        await voice.inject_text("a")  # session 0（1 个，未超限）
        clock.advance(31.0)
        await voice.inject_text("b")  # 分片 → session 1（0,1 共 2 个，未超限）
        self.assertEqual(voice._current_session, 1)
        self.assertEqual(pruner.calls, [])
        clock.advance(31.0)
        await voice.inject_text("c")  # 分片 → session 2（0,1,2 共 3 个 → 删 0）
        self.assertEqual(voice._current_session, 2)
        self.assertEqual(pruner.calls, [0])
        clock.advance(31.0)
        await voice.inject_text("d")  # 分片 → session 3（0..3 共 4 个 → 删 0、1）
        self.assertEqual(voice._current_session, 3)
        self.assertEqual(set(pruner.calls), {0, 1})
        self.assertNotIn(2, pruner.calls)
        self.assertNotIn(3, pruner.calls)


if __name__ == "__main__":
    unittest.main()
