"""记忆与会话全文检索（SQLite + LIKE）。

原计划用 FTS5，但实测 FTS5 默认 unicode61 tokenizer 对中文 MATCH 检索失败，
trigram tokenizer 对 2 字中文词（如「记忆」「安静」）也失败。个人助手数据量小
（USER/MEMORY 各几千字符、daily 几个文件、sessions 若干 jsonl），改用 SQLite
普通表 + LIKE 子串匹配：对中文友好、实现简单、性能足够。这是 v2 计划里
「FTS5 不可用则降级 LIKE」备选方案的落地。

索引范围：
- 记忆文件：USER.md / MEMORY.md / daily/*.md（按文件粒度，source=user/memory/daily）
- 会话历史：sessions/*.jsonl（按消息粒度，source=session，ref=session_key）

新鲜度策略：
- 启动时 ``rebuild_all()`` 全量重建；
- 每次 ``search()`` 前自动 ``refresh_memory()`` 重建记忆文件部分（文件少，毫秒级），
  保证刚写入的记忆能被搜到；
- 每次 ``search()`` 前自动 ``refresh_sessions()`` 增量刷新会话部分：按会话文件
  的 mtime/size 与内存状态（``_session_state``）对比，只对「有变化的会话」整会话
  重索引、对「已删除的会话」清索引，未变化的会话跳过（126 会话规模实测毫秒级），
  保证新增/追加的会话消息实时可搜、无需重启（TASK-012）。
"""

import json
import os
import sqlite3
from datetime import datetime
from typing import List, Optional

from session.manager import SessionManager


class MemorySearcher:
    """基于 SQLite + LIKE 的记忆与会话检索。"""

    # 检索结果片段的上下文半径（字符）
    _SNIPPET_RADIUS = 40
    # 单次返回上限
    _DEFAULT_LIMIT = 10

    def __init__(self, memory_dir: str, session_manager: Optional[SessionManager] = None):
        """初始化。

        Args:
            memory_dir: 记忆根目录（USER.md / MEMORY.md / daily/ 所在）。
            session_manager: 会话管理器，提供 sessions 目录与历史读取；为 None
                时只检索记忆文件、不检索会话历史。
        """
        self.memory_dir = memory_dir
        self.session_manager = session_manager
        self.db_path = os.path.join(memory_dir, "index.db")
        self._conn: Optional[sqlite3.Connection] = None
        # 会话文件索引状态：stem（list_sessions 的返回值）-> (st_mtime_ns, st_size)，
        # 供 refresh_sessions() 增量判断「哪些会话文件变了/新增/被删」。
        self._session_state: dict[str, tuple[int, int]] = {}
        self._ensure_schema()

    # —— 连接与 schema ——
    def _ensure_schema(self) -> None:
        """打开连接并确保表结构存在。"""
        os.makedirs(self.memory_dir, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,      -- user / memory / daily / session
                ref TEXT NOT NULL,         -- 文件路径(user/memory/daily) 或 session_key
                content TEXT NOT NULL,     -- 文件全文(user/memory/daily) 或单条消息内容
                indexed_at TEXT NOT NULL   -- 索引时间，调试用
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source)"
        )
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # —— 全量重建 ——
    def rebuild_all(self) -> int:
        """全量重建索引（记忆文件 + 会话历史），返回索引文档数。

        先清空 documents，再重新读取所有来源。启动时调用一次即可。
        """
        cur = self._conn.cursor()
        cur.execute("DELETE FROM documents")
        # 全量重建以磁盘文件为准，重置会话增量状态避免残留旧会话
        self._session_state = {}
        now = datetime.now().isoformat(timespec="seconds")
        count = 0
        count += self._index_memory_files(cur, now)
        count += self._index_sessions(cur, now)
        self._conn.commit()
        return count

    def refresh_memory(self) -> int:
        """只重建记忆文件部分（USER/MEMORY/daily），返回索引文档数。

        每次 search 前调用，保证刚写入的记忆可被搜到。会话部分由
        ``refresh_sessions()`` 单独增量刷新。
        """
        cur = self._conn.cursor()
        cur.execute(
            "DELETE FROM documents WHERE source IN ('user', 'memory', 'daily')"
        )
        now = datetime.now().isoformat(timespec="seconds")
        count = self._index_memory_files(cur, now)
        self._conn.commit()
        return count

    def _index_memory_files(self, cur: sqlite3.Cursor, now: str) -> int:
        """索引 USER.md / MEMORY.md / daily/*.md，返回文档数。"""
        count = 0
        # USER.md
        user_path = os.path.join(self.memory_dir, "USER.md")
        c = self._read_file(user_path)
        if c:
            cur.execute(
                "INSERT INTO documents(source, ref, content, indexed_at) VALUES(?,?,?,?)",
                ("user", user_path, c, now),
            )
            count += 1
        # MEMORY.md
        mem_path = os.path.join(self.memory_dir, "MEMORY.md")
        c = self._read_file(mem_path)
        if c:
            cur.execute(
                "INSERT INTO documents(source, ref, content, indexed_at) VALUES(?,?,?,?)",
                ("memory", mem_path, c, now),
            )
            count += 1
        # daily/*.md
        daily_dir = os.path.join(self.memory_dir, "daily")
        if os.path.isdir(daily_dir):
            for name in sorted(os.listdir(daily_dir)):
                if not name.endswith(".md"):
                    continue
                p = os.path.join(daily_dir, name)
                c = self._read_file(p)
                if c:
                    cur.execute(
                        "INSERT INTO documents(source, ref, content, indexed_at) VALUES(?,?,?,?)",
                        ("daily", p, c, now),
                    )
                    count += 1
        return count

    def _index_sessions(self, cur: sqlite3.Cursor, now: str) -> int:
        """索引所有会话历史（按消息粒度），返回文档数。

        无 session_manager 时跳过。每条 user/assistant 文本消息一行；
        tool 消息（无 content）跳过。逐会话同步填充 ``_session_state``
        （文件 mtime/size），供后续 ``refresh_sessions()`` 增量判断。
        """
        if self.session_manager is None:
            return 0
        count = 0
        for stem in self.session_manager.list_sessions():
            # list_sessions 返回文件名 stem（冒号已替换为下划线），
            # 还原成 session_key 供 ref 使用
            session_key = stem.replace("_", ":")
            path = os.path.join(self.session_manager.sessions_dir, stem + ".jsonl")
            try:
                st = os.stat(path)
            except OSError:
                continue
            self._session_state[stem] = (st.st_mtime_ns, st.st_size)
            records = self.session_manager.get_session_messages(session_key)
            for rec in records:
                content = (rec.get("content") or "").strip()
                if not content:
                    continue
                # 跳过工具结果噪音：tool 角色的内容通常是结构化结果，检索意义低
                if rec.get("role") == "tool":
                    continue
                cur.execute(
                    "INSERT INTO documents(source, ref, content, indexed_at) VALUES(?,?,?,?)",
                    ("session", session_key, content, now),
                )
                count += 1
        return count

    def refresh_sessions(self) -> int:
        """增量刷新会话索引，返回本次新索引的文档数。

        对比每个会话文件的 mtime/size 与内存状态 ``_session_state``：
        - 文件有变化或首次见的会话 → 整会话重索引（DELETE 该会话旧索引 +
          逐条 INSERT 全部消息，复用 ``_index_sessions`` 相同的过滤逻辑）；
        - 状态中存在但 ``list_sessions()`` 已不存在的 stem（文件被删，
          如 /clear 清空会话）→ 删除其索引并从状态移除，保证旧内容搜不到；
        - 未变化的会话跳过（幂等，不重复插入）。

        全部处理完统一 commit。get_session_messages 自带 JSONDecodeError
        容错，文件正在写入时读到半截行会跳过损坏行，不崩。
        """
        if self.session_manager is None:
            return 0
        stems = self.session_manager.list_sessions()
        stems_set = set(stems)
        cur = self._conn.cursor()
        now = datetime.now().isoformat(timespec="seconds")
        count = 0

        # 1) 新增/有变化的会话：整会话重索引；消失的会话：清索引
        for stem in stems:
            session_key = stem.replace("_", ":")
            path = os.path.join(self.session_manager.sessions_dir, stem + ".jsonl")
            try:
                st = os.stat(path)
            except OSError:
                # 文件在 list 与 stat 之间被删：按删除处理（清索引 + 移除状态）
                st = None
            state = self._session_state.get(stem)
            if st is not None and state == (st.st_mtime_ns, st.st_size):
                continue  # 文件未变化，跳过
            cur.execute(
                "DELETE FROM documents WHERE source='session' AND ref=?",
                (session_key,),
            )
            if st is None:
                self._session_state.pop(stem, None)
                continue
            self._session_state[stem] = (st.st_mtime_ns, st.st_size)
            records = self.session_manager.get_session_messages(session_key)
            for rec in records:
                content = (rec.get("content") or "").strip()
                if not content:
                    continue
                if rec.get("role") == "tool":
                    continue
                cur.execute(
                    "INSERT INTO documents(source, ref, content, indexed_at) VALUES(?,?,?,?)",
                    ("session", session_key, content, now),
                )
                count += 1

        # 2) 状态中存在但会话文件已被删除的 stem：删除索引并移除状态
        for stem in list(self._session_state.keys()):
            if stem not in stems_set:
                session_key = stem.replace("_", ":")
                cur.execute(
                    "DELETE FROM documents WHERE source='session' AND ref=?",
                    (session_key,),
                )
                del self._session_state[stem]

        self._conn.commit()
        return count

    @staticmethod
    def _read_file(path: str) -> str:
        """读取文件全文；不存在或读失败返回空串。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            return ""

    # —— 检索 ——
    def search(
        self,
        query: str,
        scope: str = "memory",
        limit: int = _DEFAULT_LIMIT,
    ) -> List[dict]:
        """LIKE 子串检索。

        Args:
            query: 检索词（中文友好，子串匹配）。
            scope: 检索范围：
                - ``memory``：只搜记忆文件（USER/MEMORY/daily）—— 默认，先搜记忆
                - ``session``：只搜会话历史
                - ``all``：都搜
            limit: 返回上限。

        Returns:
            结果列表，每项 ``{source, ref, snippet}``，按 source 分组、无特殊排序
            （LIKE 无法排序相关性；个人助手数据量小，全量返回即可）。
        """
        query = (query or "").strip()
        if not query:
            return []

        # 每次搜索前刷新记忆文件，保证新鲜
        self.refresh_memory()
        # 增量刷新会话索引，保证新会话/新消息实时可搜（TASK-012）
        self.refresh_sessions()

        # scope → source 过滤
        if scope == "memory":
            where_source = "AND source IN ('user','memory','daily')"
        elif scope == "session":
            where_source = "AND source = 'session'"
        else:  # all 或其它
            where_source = ""

        sql = (
            "SELECT source, ref, content FROM documents "
            f"WHERE content LIKE ? {where_source} LIMIT ?"
        )
        rows = self._conn.execute(sql, (f"%{query}%", limit)).fetchall()

        results = []
        for source, ref, content in rows:
            results.append({
                "source": source,
                "ref": ref,
                "snippet": self._make_snippet(content, query),
            })
        return results

    @classmethod
    def _make_snippet(cls, content: str, query: str) -> str:
        """截取 query 命中位置附近的片段（半径 _SNIPPET_RADIUS 字符）。

        找不到位置时返回 content 前 100 字符。
        """
        idx = content.find(query)
        if idx == -1:
            return content[:100]
        start = max(0, idx - cls._SNIPPET_RADIUS)
        end = min(len(content), idx + len(query) + cls._SNIPPET_RADIUS)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(content) else ""
        return prefix + content[start:end] + suffix
