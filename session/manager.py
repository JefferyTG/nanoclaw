"""会话管理模块：把对话消息按会话维度持久化到 JSONL 文件。

每个会话对应一个 .jsonl 文件，一行一条消息（JSON 格式）。
所有数据都落在 workspace/sessions/ 下，不污染项目根目录。
"""

import json
import os
from datetime import datetime


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

    def save_message(self, session_key: str, message: dict) -> None:
        """追加一条消息到对应会话的 JSONL 文件。

        - 自动附加 timestamp（ISO 8601 格式当前时间）
        - 以 append 模式写入，ensure_ascii=False 保证中文不被转义
        - 复制一份再附加时间戳，不污染调用方传入的原始 dict
        """
        path = self._get_session_path(session_key)
        record = dict(message)
        record["timestamp"] = datetime.now().isoformat(timespec="seconds")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def save_messages(self, session_key: str, messages: list[dict]) -> None:
        """用给定消息列表【覆盖写回】某会话的 JSONL 文件。

        与 :meth:`save_message`（追加单条）不同，本方法会先清空原文件、
        再逐行写入 ``messages``，用于「会话压缩」等需要把历史整体替换为更短
        版本的写时转换场景——否则压缩结果只留在内存、磁盘上的原始长历史
        不缩短，重启后又会从长历史重新压缩。

        每条消息仍统一附加 ``timestamp``（与 :meth:`save_message` 行为一致），
        因此后续 :meth:`get_history` 读取时的格式完全一致。
        """
        path = self._get_session_path(session_key)
        with open(path, "w", encoding="utf-8") as f:
            for message in messages:
                record = dict(message)
                record["timestamp"] = datetime.now().isoformat(timespec="seconds")
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
                msg.pop("timestamp", None)
                # 思考内容（reasoning_content）是模型的临时内心独白，不应回放：
                # 一是避免发给 API 时与 assistant(tool_calls) 消息冲突（硅基流动
                # 等兼容实现对 tool_calls 消息上的 reasoning_content 敏感），
                # 二是省 token。下次再需要时模型会重新推理。
                msg.pop("reasoning_content", None)
                messages.append(msg)

        return self._normalize_tool_history(messages)

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
        normalized: list[dict] = []
        # 旧版错误顺序中，若干 tool 会紧邻出现在其 assistant 前面。
        leading_tools: list[dict] = []
        pending_ids: list[str] = []
        pending_tools: dict[str, dict] = {}

        def close_pending() -> None:
            nonlocal pending_ids, pending_tools
            for tool_call_id in pending_ids:
                tool_msg = pending_tools.get(tool_call_id)
                if tool_msg is None:
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": (
                            "（历史记录中缺失对应的工具结果，"
                            "已由会话管理器自动补全）"
                        ),
                    }
                normalized.append(tool_msg)
            pending_ids = []
            pending_tools = {}

        for message in messages:
            role = message.get("role")

            if pending_ids:
                if role == "tool":
                    tool_call_id = message.get("tool_call_id")
                    if (
                        tool_call_id in pending_ids
                        and tool_call_id not in pending_tools
                    ):
                        pending_tools[tool_call_id] = message
                    if all(tcid in pending_tools for tcid in pending_ids):
                        close_pending()
                    continue
                # OpenAI 协议不允许其它角色插在未完成的工具交换中间。
                close_pending()

            if role == "tool":
                # 暂存连续的前置 tool；只有紧随其后的 assistant 引用了相同 id
                # 才会恢复，否则它就是不可归属的孤立结果并被丢弃。
                leading_tools.append(message)
                continue

            if role == "assistant" and message.get("tool_calls"):
                expected_ids = []
                for tool_call in message.get("tool_calls") or []:
                    tool_call_id = tool_call.get("id")
                    if tool_call_id and tool_call_id not in expected_ids:
                        expected_ids.append(tool_call_id)
                if not expected_ids:
                    # tool_calls 结构本身无有效 id，去掉无效字段，保留可见正文。
                    cleaned = dict(message)
                    cleaned.pop("tool_calls", None)
                    normalized.append(cleaned)
                    leading_tools = []
                    continue

                normalized.append(message)
                pending_ids = expected_ids
                pending_tools = {}
                for tool_message in leading_tools:
                    tool_call_id = tool_message.get("tool_call_id")
                    if (
                        tool_call_id in pending_ids
                        and tool_call_id not in pending_tools
                    ):
                        pending_tools[tool_call_id] = tool_message
                leading_tools = []
                if all(tcid in pending_tools for tcid in pending_ids):
                    close_pending()
                continue

            # 普通消息构成边界；边界前仍未找到 assistant 的 tool 无法安全归属。
            leading_tools = []
            normalized.append(message)

        if pending_ids:
            close_pending()
        return normalized

    def clear(self, session_key: str) -> None:
        """删除某会话的 JSONL 文件（若不存在则静默忽略）。"""
        path = self._get_session_path(session_key)
        if os.path.exists(path):
            os.remove(path)

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
