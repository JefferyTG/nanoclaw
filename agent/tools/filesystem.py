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
"""

import os

from agent.tools.base import Tool


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

    def __init__(self, workspace: str):
        self.workspace = os.path.abspath(workspace)

    def _safe_path(self, user_path: str) -> str | None:
        target = os.path.abspath(os.path.join(self.workspace, user_path))
        if target == self.workspace or target.startswith(self.workspace + os.sep):
            return target
        return None

    async def execute(self, file_path: str, content: str, **kwargs) -> str:
        absolute = self._safe_path(file_path)
        if absolute is None:
            return f"错误：路径 '{file_path}' 越过工作区边界，已被拦截"

        try:
            parent = os.path.dirname(absolute)
            os.makedirs(parent, exist_ok=True)
            with open(absolute, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as exc:  # noqa: BLE001 - 把写文件异常转成字符串反馈
            return f"写入失败：{exc}"

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
