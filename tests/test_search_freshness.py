"""TASK-012 会话索引实时性：memory_search(scope=session) 增量刷新测试。

覆盖验收点：
- 新会话写入消息后立即可搜（无需重启）；
- 既有会话追加消息后增量刷新能补上新内容；
- 未变化的会话不重复重索引（幂等，不重复插入）；
- /clear 清空会话后旧内容搜不到（索引随文件删除同步清除）；
- 126 会话规模下 refresh_sessions() 耗时毫秒级（宽松阈值 <100ms，避免 CI 抖动）。
"""

import os
import tempfile
import time
import unittest

from agent.search import MemorySearcher
from session.manager import SessionManager


class SearchFreshnessTests(unittest.TestCase):
    """用临时目录构造 SessionManager + MemorySearcher，覆盖会话索引增量刷新。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.memory_dir = os.path.join(self._tmp.name, "memory")
        self.sessions_dir = os.path.join(self._tmp.name, "sessions")
        self.session_manager = SessionManager(self.sessions_dir)
        self.searcher = MemorySearcher(self.memory_dir, self.session_manager)

    def tearDown(self):
        self.searcher.close()
        self._tmp.cleanup()

    def _session_row_count(self) -> int:
        """documents 表中 source='session' 的行数（供幂等断言）。"""
        return self.searcher._conn.execute(
            "SELECT COUNT(*) FROM documents WHERE source='session'"
        ).fetchone()[0]

    def test_new_session_searchable_immediately(self):
        """rebuild_all 空库 → 写入新会话消息 → search(scope=session) 立即能搜到。"""
        self.searcher.rebuild_all()
        self.assertEqual(self._session_row_count(), 0)
        self.session_manager.save_message(
            "cli:new", {"role": "user", "content": "昨晚我们讨论了增量索引方案"}
        )
        results = self.searcher.search("增量索引", scope="session")
        self.assertTrue(any("增量索引" in r["snippet"] for r in results))

    def test_appended_message_searchable(self):
        """先写会话 A 两条消息并 rebuild_all → 追加一条 → 搜索能搜到追加内容。"""
        self.session_manager.save_message(
            "cli:a", {"role": "user", "content": "第一条消息"}
        )
        self.session_manager.save_message(
            "cli:a", {"role": "assistant", "content": "好的，收到"}
        )
        self.searcher.rebuild_all()
        self.assertFalse(self.searcher.search("追加的独特内容XYZ", scope="session"))

        self.session_manager.save_message(
            "cli:a", {"role": "user", "content": "追加的独特内容XYZ"}
        )
        results = self.searcher.search("独特内容XYZ", scope="session")
        self.assertTrue(any("独特内容XYZ" in r["snippet"] for r in results))

    def test_unchanged_session_not_reindexed(self):
        """索引后会话未变化 → 连续 search 两次 → session 行数不变（幂等）。"""
        self.session_manager.save_message(
            "cli:u", {"role": "user", "content": "保持不变的内容"}
        )
        self.searcher.rebuild_all()
        before = self._session_row_count()
        self.searcher.search("保持不变", scope="session")
        self.searcher.search("保持不变", scope="session")
        self.assertEqual(self._session_row_count(), before)

    def test_clear_removes_index(self):
        """索引会话 → clear 删除文件 → search 搜不到该会话旧内容。"""
        self.session_manager.save_message(
            "cli:c", {"role": "user", "content": "要清掉的旧内容"}
        )
        self.searcher.rebuild_all()
        self.assertTrue(self.searcher.search("要清掉的旧内容", scope="session"))

        self.session_manager.clear("cli:c")
        self.assertEqual(self.searcher.search("要清掉的旧内容", scope="session"), [])

    def test_refresh_sessions_perf(self):
        """126 会话（每会话 3~5 条消息）rebuild_all 后，refresh_sessions 耗时 < 100ms。"""
        for i in range(126):
            key = f"cli:perf{i}"
            for j in range(3 + (i % 3)):  # 3~5 条
                self.session_manager.save_message(
                    key, {"role": "user", "content": f"会话{i}第{j}条消息"}
                )
        self.searcher.rebuild_all()
        self.assertGreater(self._session_row_count(), 0)

        start = time.perf_counter()
        newly_indexed = self.searcher.refresh_sessions()
        elapsed_ms = (time.perf_counter() - start) * 1000

        # 无变化 → 不重索引任何文档（幂等）且耗时毫秒级
        self.assertEqual(newly_indexed, 0)
        self.assertLess(elapsed_ms, 100)


if __name__ == "__main__":
    unittest.main()
