"""NanoClaw 文件系统工具集。

本模块提供三个基于工作区（workspace）隔离的文件操作工具，全部继承自
``agent.tools.base.Tool``：

- ``ReadFileTool``：读取文件内容（超长截断）。
- ``WriteFileTool``：写入文件（自动建父目录）。
- ``ListDirTool``：列出目录内容（文件名+大小 / 目录名+/）。

安全是第一原则：所有工具都在构造时接收一个 ``workspace`` 根目录，任何用户
传入的相对路径都会先 ``os.path.join(workspace, 用户输入)`` 再 ``os.path.abspath``
归一化，并严格校验解析后的绝对路径仍落在 workspace 之内。这能挡住
``../../etc/passwd`` 这类路径穿越，也挡住绝对路径越界。越界一律返回拦截提示，
绝不执行。

注意：路径边界用 ``target == base or target.startswith(base + os.sep)`` 判定，
而非简单的 ``startswith(base)``。后者会把 ``/data/work`` 误判为 ``/data/workspace``
的同名前缀而放行，属于常见安全漏洞，这里做了修正。

TASK-004（WriteFileTool）：写 ``workspace/memory/USER.md`` / ``MEMORY.md``
成功后，记录变更日志（changelog.jsonl）并递增全局 revision；若本轮由
AgentLoop 注入了 ``session_key``（内部机制，不进模型可见 schema），同步刷新
该会话的 ``memory_revision``（自写刷基线，防给自己发补丁）。daily/ 永不记录。
"""

import logging
import os

from agent.memory_sync import MemoryChangeLog, diff_lines
from agent.tools.base import Tool

logger = logging.getLogger("nanoclaw.tools.filesystem")


class ReadFileTool(Tool):
    """读取工作区内的本地文件，内容过长时截断。"""

    name = "read_file"
    description = (
        "读取工作区内指定文件的文本内容。仅能访问工作区根目录及其子目录，"
        "路径穿越会被拦截。超过 16000 字符的内容会被截断。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "相对于工作区的文件路径，例如 'src/main.py' 或 'README.md'",
            }
        },
        "required": ["file_path"],
    }

    def __init__(self, workspace: str):
        self.workspace = os.path.abspath(workspace)
        self._max_chars = 16000

    def _safe_path(self, user_path: str) -> str | None:
        """把用户输入解析为绝对路径，越界返回 None。"""
        target = os.path.abspath(os.path.join(self.workspace, user_path))
        if target == self.workspace or target.startswith(self.workspace + os.sep):
            return target
        return None

    async def execute(self, file_path: str, **kwargs) -> str:
        absolute = self._safe_path(file_path)
        if absolute is None:
            return f"错误：路径 '{file_path}' 越过工作区边界，已被拦截"

        if not os.path.isfile(absolute):
            return f"错误：文件不存在：{file_path}"

        try:
            with open(absolute, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as exc:  # noqa: BLE001 - 把读文件异常转成字符串反馈
            return f"读取失败：{exc}"

        if len(content) > self._max_chars:
            content = content[: self._max_chars] + "\n...[内容过长已截断]..."
        return content


class WriteFileTool(Tool):
    """向工作区内写入文件，自动创建所需父目录。"""

    name = "write_file"
    description = (
        "将文本内容写入工作区内指定文件。仅能写入工作区根目录及其子目录，"
        "路径穿越会被拦截。父目录不存在时自动创建。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "相对于工作区的文件路径，例如 'output/result.txt'",
            },
            "content": {
                "type": "string",
                "description": "要写入的文本内容",
            },
        },
        "required": ["file_path", "content"],
    }

    def __init__(self, workspace: str, memory_changelog: MemoryChangeLog | None = None):
        self.workspace = os.path.abspath(workspace)
        # 记忆变更日志（TASK-004）：记录 USER.md / MEMORY.md 的写入，递增全局
        # revision。不传时按 workspace 惰性构造，保证 main.py 现有装配零改动。
        self._changelog = memory_changelog or MemoryChangeLog(self.workspace)
        self._session_manager = None

    def _safe_path(self, user_path: str) -> str | None:
        target = os.path.abspath(os.path.join(self.workspace, user_path))
        if target == self.workspace or target.startswith(self.workspace + os.sep):
            return target
        return None

    def _is_memory_file(self, absolute: str) -> bool:
        """只同步 USER.md / MEMORY.md 两个文件；daily/ 永不注入。"""
        memory_dir = os.path.join(self.workspace, "workspace", "memory")
        return absolute in (
            os.path.join(memory_dir, "USER.md"),
            os.path.join(memory_dir, "MEMORY.md"),
        )

    def _get_session_manager(self):
        """惰性构造会话管理器（与生产装配的 sessions 目录一致）。

        仅在「写记忆文件 + 注入 session_key」时才会用到，避免普通文件写入
        产生任何额外开销。
        """
        if self._session_manager is None:
            from session.manager import SessionManager

            self._session_manager = SessionManager(
                os.path.join(self.workspace, "workspace", "sessions")
            )
        return self._session_manager

    def _record_memory_write(
        self, absolute: str, old_text: str, new_text: str, session_key: str
    ) -> None:
        """写记忆文件成功后：记变更日志 + 递增全局 revision + 刷会话基线。

        全程静默降级：任何失败只记日志，绝不影响 write_file 本身的返回结果
        （记忆同步不能因为日志失败而让对话挂掉）。
        """
        added, removed = diff_lines(old_text, new_text)
        if not added and not removed:
            # 内容无实际变化：不记日志、不递增 revision，避免空 diff 假补丁
            return
        rel_path = os.path.relpath(absolute, self.workspace).replace(os.sep, "/")
        try:
            new_revision = self._changelog.append(rel_path, "write", added, removed)
        except Exception:  # noqa: BLE001 - 变更日志失败不能阻断写文件本身
            logger.exception("记录记忆变更日志失败：%s", rel_path)
            return
        if session_key:
            try:
                self._get_session_manager().set_memory_revision(
                    session_key, new_revision
                )
            except Exception:  # noqa: BLE001 - 会话基线刷新失败不能阻断写文件
                logger.exception(
                    "刷新会话 memory_revision 失败：session_key=%s", session_key
                )

    async def execute(self, file_path: str, content: str, **kwargs) -> str:
        absolute = self._safe_path(file_path)
        if absolute is None:
            return f"错误：路径 '{file_path}' 越过工作区边界，已被拦截"

        # 写前读旧内容，用于写成功后计算行级 diff（记忆文件专用）
        old_text = ""
        if os.path.isfile(absolute):
            try:
                with open(absolute, "r", encoding="utf-8") as f:
                    old_text = f.read()
            except Exception:  # noqa: BLE001 - 读旧内容失败按空处理
                old_text = ""

        try:
            parent = os.path.dirname(absolute)
            os.makedirs(parent, exist_ok=True)
            with open(absolute, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as exc:  # noqa: BLE001 - 把写文件异常转成字符串反馈
            return f"写入失败：{exc}"

        # TASK-004：记忆文件写入 → 全局 revision + 自写刷基线（session_key 由
        # AgentLoop 注入，属内部机制，不进模型可见参数）
        if self._is_memory_file(absolute):
            self._record_memory_write(
                absolute, old_text, content, kwargs.get("session_key", "")
            )

        return f"已写入 {len(content)} 字符到 {file_path}"


class ListDirTool(Tool):
    """列出工作区内某目录的内容，含名称、大小，并按名称排序。"""

    name = "list_dir"
    description = (
        "列出工作区内指定目录的内容。目录以 '/' 结尾，文件附带大小。"
        "结果按名称排序。仅能访问工作区内部，路径穿越会被拦截。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "dir_path": {
                "type": "string",
                "description": "要列出的目录（相对于工作区）；为空或省略时表示工作区根目录",
            }
        },
        "required": [],
    }

    def __init__(self, workspace: str):
        self.workspace = os.path.abspath(workspace)

    def _safe_path(self, user_path: str) -> str | None:
        target = os.path.abspath(os.path.join(self.workspace, user_path))
        if target == self.workspace or target.startswith(self.workspace + os.sep):
            return target
        return None

    @staticmethod
    def _fmt_size(size: int) -> str:
        """把字节数格式化为带单位的易读字符串。"""
        units = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        f = float(size)
        while f >= 1024 and i < len(units) - 1:
            f /= 1024
            i += 1
        if units[i] == "B":
            return f"{int(f)} B"
        return f"{f:.1f} {units[i]}"

    async def execute(self, dir_path: str = "", **kwargs) -> str:
        absolute = self._safe_path(dir_path)
        if absolute is None:
            return f"错误：路径 '{dir_path}' 越过工作区边界，已被拦截"

        if not os.path.isdir(absolute):
            return f"错误：目录不存在：{dir_path}"

        try:
            names = sorted(os.listdir(absolute))
        except Exception as exc:  # noqa: BLE001
            return f"列出失败：{exc}"

        if not names:
            return "(空目录)"

        lines = []
        for name in names:
            full = os.path.join(absolute, name)
            if os.path.isdir(full):
                lines.append(f"{name}/")
            else:
                lines.append(f"{name}  {self._fmt_size(os.path.getsize(full))}")
        return "\n".join(lines)
