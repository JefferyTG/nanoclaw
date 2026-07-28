"""首次运行的人设引导与安全持久化。

人设是实例级配置，而不是某个会话的聊天内容。若配置的人设文件不存在，
``IdentityBootstrapper`` 会在任一渠道的首条普通消息到来时先询问用户；同一
会话的下一条文本被视为人设描述，经过轻量模板整理后原子写入文件。写入完成
后 ``ContextBuilder`` 会在下一轮自动重读，无需重启进程。
"""

import asyncio
import os
import tempfile
from typing import Optional


DEFAULT_IDENTITY = (
    "你是一个务实、可靠、乐于助人的 AI 助手。\n"
    "你会优先用已有信息解决问题，遇到不确定会主动澄清，而不是猜测。\n"
    "你使用中文与用户交流，回答简洁、准确、可执行。"
)

IDENTITY_PROMPT = (
    "检测到当前实例还没有人设文件。请先告诉我你希望我是什么样的助手，"
    "可以包括：称呼、与你的关系、性格、语气、回答偏好和需要避免的行为。\n\n"
    "例如：你叫小南，是我的技术搭档；说话自然直接，先给结论再解释，"
    "遇到不确定先询问。\n\n"
    "直接回复人设描述即可；回复 /default 可使用默认人设。"
)


class IdentityBootstrapper:
    """协调多渠道首次人设采集，并把结果写入实例人设文件。"""

    def __init__(
        self,
        workspace: str,
        identity_file: str = "identity.md",
        max_description_chars: int = 8_000,
    ) -> None:
        self.workspace = os.path.realpath(os.path.abspath(workspace))
        self.identity_file = identity_file
        self.max_description_chars = max_description_chars
        self.identity_path = self._resolve_identity_path(identity_file)
        self._pending_sessions: set[str] = set()
        self._lock = asyncio.Lock()

    def _resolve_identity_path(self, identity_file: str) -> str:
        if not isinstance(identity_file, str) or not identity_file.strip():
            raise ValueError("identity_file 必须是工作区内的非空相对路径")
        candidate = os.path.abspath(os.path.join(self.workspace, identity_file))
        try:
            if os.path.commonpath((candidate, self.workspace)) != self.workspace:
                raise ValueError
        except ValueError as exc:
            raise ValueError("identity_file 必须位于 workspace 内") from exc
        return candidate

    def is_ready(self) -> bool:
        """人设文件存在且包含非空内容时视为已经完成引导。"""
        try:
            if not os.path.isfile(self.identity_path):
                return False
            with open(self.identity_path, "r", encoding="utf-8") as handle:
                return bool(handle.read().strip())
        except (OSError, UnicodeError):
            return False

    async def handle(self, session_key: str, text: str) -> Optional[str]:
        """消费一次引导消息；人设已存在时返回 ``None`` 进入正常聊天。"""
        async with self._lock:
            if self.is_ready():
                self._pending_sessions.clear()
                return None

            if session_key not in self._pending_sessions:
                self._pending_sessions.add(session_key)
                return IDENTITY_PROMPT

            description = (text or "").strip()
            if not description:
                return "人设描述不能为空，请重新发送；或回复 /default 使用默认人设。"
            if len(description) > self.max_description_chars:
                return (
                    f"人设描述过长（最多 {self.max_description_chars} 个字符），"
                    "请精简后重新发送。"
                )

            use_default = description.casefold() in {
                "/default",
                "使用默认人设",
                "默认人设",
            }
            content = DEFAULT_IDENTITY if use_default else self._format(description)
            try:
                self._write_atomic(content)
            except OSError as exc:
                return f"⚠️ 人设文件写入失败：{exc}。请检查工作区权限后重试。"

            self._pending_sessions.clear()
            if use_default:
                return "✅ 已生成人设文件并启用默认人设。现在可以重新发送你的任务。"
            return "✅ 已根据你的描述生成人设文件。现在可以重新发送你的任务。"

    @staticmethod
    def _format(description: str) -> str:
        return (
            "# NanoClaw 人设\n\n"
            "## 用户确认的设定\n\n"
            f"{description}\n\n"
            "## 执行原则\n\n"
            "- 始终以用户确认的设定作为称呼、语气和协作方式的依据。\n"
            "- 信息不足或要求存在歧义时，先澄清再行动。\n"
            "- 回答应准确、自然、可执行，不虚构事实。\n"
        )

    def _write_atomic(self, content: str) -> None:
        parent = os.path.dirname(self.identity_path)
        # realpath 会解析已存在的软链接路径，避免配置借父目录软链接越出工作区。
        real_parent = os.path.realpath(parent)
        try:
            if os.path.commonpath((real_parent, self.workspace)) != self.workspace:
                raise OSError("identity_file 的父目录越过 workspace 边界")
        except ValueError as exc:
            raise OSError("identity_file 的父目录无效") from exc

        os.makedirs(parent, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".identity-", dir=parent, text=True)
        try:
            os.chmod(temp_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content.rstrip() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.identity_path)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
