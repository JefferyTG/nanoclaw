"""会话管理模块：把对话消息按会话维度持久化到 JSONL 文件。

每个会话对应一个 .jsonl 文件，一行一条消息（JSON 格式）。
所有数据都落在 workspace/sessions/ 下，不污染项目根目录。

TASK-004：会话元数据（``session.memory_revision``）用同名侧车文件
``<safe_key>.meta.json`` 持久化，与消息 JSONL 解耦——不改动消息行的读取、
规范化和 Web 历史回放格式；重启恢复时经 ``get_memory_revision`` 读回。

TASK-015（NC-MEM-002）：``save_messages`` 新增 ``preserve_timestamps`` 参数，
「压缩/快照重写历史但保留原始时间戳」的场景不再把整段会话消息的时间戳统一
改写为写入时刻。
"""

import json
import os
from datetime import datetime
from typing import Optional

from agent.history import canonicalize_history, canonicalize_history_message


def _message_identity_key(message: dict) -> Optional[tuple]:
    """返回消息的稳定身份键，供 ``save_messages`` 保留模式匹配 timestamp。

    ``canonicalize_history_message`` 会剥离 ``timestamp`` 字段（API 历史里不
    允许出现），因此保留模式在写入前先把「原始 timestamp」按身份键暂存、
    canonicalize 后再挂回。键取 role / tool_call_id / content / tool_calls 的
    稳定序列化，保证 canonicalize 前后同一消息身份一致（canonicalize 只会清洗
    字段、补缺失工具结果，不会改变这些字段的值）。
    """
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    tool_call_id = message.get("tool_call_id")
    try:
        content_key = json.dumps(
            message.get("content"), ensure_ascii=False, sort_keys=True
        )
    except (TypeError, ValueError):
        content_key = str(message.get("content"))
    calls = message.get("tool_calls")
    try:
        calls_key = json.dumps(calls, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        calls_key = str(calls)
    return (role, tool_call_id, content_key, calls_key)


class SessionManager:
    """按会话维度管理对话历史的持久化、读取与清理。"""

    def __init__(self, sessions_dir: str = "workspace/sessions"):
        # 所有会话数据都存放在 sessions_dir 下（默认位于 workspace/ 内）
        self.sessions_dir = sessions_dir
        # 构造时确保存储目录存在；脚本退出后目录仍保留，供后续会话复用
        os.makedirs(self.sessions_dir, exist_ok=True)

    def _get_session_path(self, session_key: str) -> str:
        """根据会话标识算出对应的 JSONL 文件路径。

        session_key 中的 ':' 会被替换成 '_'，避免路径中出现冒号这类
        易引发歧义/非法的字符。
        例如 'cli:direct' -> '<sessions_dir>/cli_direct.jsonl'。
        """
        safe_key = session_key.replace(":", "_")
        return os.path.join(self.sessions_dir, safe_key + ".jsonl")

    def _get_meta_path(self, session_key: str) -> str:
        """会话元数据侧车文件路径（与 JSONL 同名、扩展名 .meta.json）。"""
        return self._get_session_path(session_key).replace(".jsonl", ".meta.json")

    def save_message(self, session_key: str, message: dict) -> None:
        """追加一条消息到对应会话的 JSONL 文件。

        - 自动附加 timestamp（ISO 8601 格式当前时间）
        - 以 append 模式写入，ensure_ascii=False 保证中文不被转义
        - 复制一份再附加时间戳，不污染调用方传入的原始 dict
        """
        path = self._get_session_path(session_key)
        record = canonicalize_history_message(message)
        record["timestamp"] = datetime.now().isoformat(timespec="seconds")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def save_messages(
        self,
        session_key: str,
        messages: list[dict],
        preserve_timestamps: bool = False,
    ) -> None:
        """用给定消息列表【覆盖写回】某会话的 JSONL 文件。

        与 :meth:`save_message`（追加单条）不同，本方法会先清空原文件、
        再逐行写入 ``messages``，用于「会话压缩」等需要把历史整体替换为更短
        版本的写时转换场景——否则压缩结果只留在内存、磁盘上的原始长历史
        不缩短，重启后又会从长历史重新压缩。

        ``preserve_timestamps``（TASK-015 / NC-MEM-002）：
            - False（默认）：与旧行为一致——每条消息统一附加当前时间
              （覆盖写回语义，调用方行为不变）；
            - True：**保留原始时间戳**——按「规范化身份」依次取：① 输入消息
              自带的 ``timestamp``；② 原文件里同身份消息的 ``timestamp``
              （覆盖写回前读一次旧文件，找回被覆盖的原始时间戳）；两者都取
              不到（如新生成的摘要/快照消息）才补当前时间。供「压缩/快照
              重写历史但保留原始时间戳真实性」的场景使用：压缩写回时尾部
              保留的消息应保持原时间戳、仅新摘要取压缩时刻；TASK-007 的
              「压缩→无条件重建完整快照」紧随压缩在同一回合执行，同样需要
              保留模式，否则重建会立刻把时间戳再改写掉。

        无论哪种模式，写入后的消息都带 ``timestamp``，后续 :meth:`get_history`
        读取时的格式完全一致。
        """
        path = self._get_session_path(session_key)
        # 保留模式：先把「输入消息自带 timestamp」与「原文件已有 timestamp」
        # 按身份键暂存（canonicalize 会剥离 timestamp，需写入前捕获、规范化
        # 后挂回）。原文件查找是 NC-MEM-002 的关键：覆盖写回前旧文件还保留
        # 原始时间戳，压缩/快照重建的输入消息本身不带 timestamp，要靠它找回。
        ts_by_identity: dict = {}
        if preserve_timestamps:
            for m in messages:
                if not isinstance(m, dict):
                    continue
                ts = m.get("timestamp")
                key = _message_identity_key(m)
                if ts and key is not None:
                    ts_by_identity.setdefault(key, ts)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            old = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(old, dict):
                            continue
                        ts = old.get("timestamp")
                        key = _message_identity_key(old)
                        if ts and key is not None:
                            ts_by_identity.setdefault(key, ts)
            except OSError:
                pass  # 文件不存在/读失败：无原始时间戳可参考，缺失补当前时间
        now = datetime.now().isoformat(timespec="seconds")
        with open(path, "w", encoding="utf-8") as f:
            for message in canonicalize_history(messages):
                record = canonicalize_history_message(message)
                ts = None
                if preserve_timestamps:
                    key = _message_identity_key(record)
                    ts = ts_by_identity.get(key) if key is not None else None
                record["timestamp"] = ts or now
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def get_history(self, session_key: str) -> list[dict]:
        """读取某会话的全部历史消息。

        - 逐行解析 JSON，跳过空行与损坏行
        - 返回的每条消息**不含 timestamp** 字段（OpenAI API 不认识它）
        - 文件不存在时返回空列表
        """
        path = self._get_session_path(session_key)
        if not os.path.exists(path):
            return []

        messages = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # 单行损坏不影响其余历史
                    continue
                messages.append(canonicalize_history_message(msg))

        return canonicalize_history(messages)

    @staticmethod
    def _normalize_tool_history(messages: list[dict]) -> list[dict]:
        """把旧版或中断产生的工具消息恢复成合法的 API 顺序。

        支持三类损坏：

        - 旧版把 tool 写在 assistant(tool_calls) 前面：识别相邻的前置结果并重排；
        - assistant 声明了工具但结果缺失：在下一条普通消息前补占位结果；
        - 找不到任何对应 tool_calls 的孤立 tool：从模型历史中丢弃。

        原始 JSONL 不在读取时覆盖，避免破坏时间戳和 Web 历史展示元数据；返回给
        Agent 的内存历史始终满足 ``assistant(tool_calls) → tool...`` 契约。
        """
        return canonicalize_history(messages)

    def clear(self, session_key: str) -> None:
        """删除某会话的 JSONL 与元数据侧车文件（不存在则静默忽略）。"""
        path = self._get_session_path(session_key)
        if os.path.exists(path):
            os.remove(path)
        meta = self._get_meta_path(session_key)
        if os.path.exists(meta):
            os.remove(meta)

    def get_session_messages(self, session_key: str) -> list[dict]:
        """读取某会话的全部原始消息（保留 timestamp，供前端历史回放展示）。

        与 :meth:`get_history` 不同，本方法**不剥离** timestamp 与
        reasoning_content，因为网页侧边栏需要按时间排序、并回放思考过程。
        文件不存在时返回空列表。
        """
        path = self._get_session_path(session_key)
        if not os.path.exists(path):
            return []
        out: list[dict] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def get_memory_revision(self, session_key: str) -> Optional[int]:
        """读取会话元数据中的 ``memory_revision``（TASK-004）。

        不存在或损坏时返回 ``None``。该值由 AgentLoop 在会话创建/重启时
        写入，重启后据此恢复「会话记忆快照对应的全局 revision」。
        """
        path = self._get_meta_path(session_key)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            return int(meta.get("memory_revision"))
        except (OSError, ValueError, TypeError):
            return None

    def set_memory_revision(self, session_key: str, revision: int) -> None:
        """持久化会话元数据中的 ``memory_revision``（TASK-004）。"""
        path = self._get_meta_path(session_key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"memory_revision": int(revision)}, f, ensure_ascii=False)

    def list_sessions_detailed(self, prefix: str = None) -> list[dict]:
        """列出会话（含元数据），供网页侧边栏使用。

        返回按更新时间（updated_at）倒序的列表，每项::

            {"key": <会话标识>, "count": <消息数>,
             "title": <首条用户消息前 40 字>, "preview": <末条消息前 80 字>,
             "updated_at": <末条 timestamp>}

        ``prefix`` 非空时只返回以它开头的会话（如 ``"web:"`` 仅网页会话）。
        注意 session_key 在落盘时把 ``:`` 替换为 ``_``，这里再还原回来，
        以便前端可直接用返回的 ``key`` 回传 get/clear。
        """
        out: list[dict] = []
        for stem in self.list_sessions():
            key = stem.replace("_", ":")  # 还原 session_key
            if prefix and not key.startswith(prefix):
                continue
            records = self.get_session_messages(key)
            if not records:
                continue
            title = ""
            for r in records:
                if r.get("role") == "user" and not title:
                    title = (r.get("content") or "").strip()[:40]
                    break
            last = records[-1]
            preview = ((last.get("content") or last.get("reasoning_content") or "")
                       ).strip()[:80]
            out.append({
                "key": key,
                "count": len(records),
                "title": title or "(空会话)",
                "preview": preview,
                "updated_at": last.get("timestamp", ""),
            })
        out.sort(key=lambda x: x["updated_at"], reverse=True)
        return out

    def list_sessions(self) -> list[str]:
        """列出所有已存在的会话标识（即 .jsonl 文件名去掉扩展名）。

        注意：返回的是文件名 stem（冒号已被替换为下划线），与
        _get_session_path 的映射保持一致，因此可直接回传给
        get_history / clear。
        """
        if not os.path.isdir(self.sessions_dir):
            return []
        keys = []
        for name in os.listdir(self.sessions_dir):
            if name.endswith(".jsonl"):
                keys.append(name[: -len(".jsonl")])
        return sorted(keys)
