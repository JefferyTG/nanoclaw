"""文件存储（FileStore）。

把入站文件按月归档到 ``<data_root>/files/YYYY-MM/`` 目录下（默认
``workspace/files/2026-08/``），与图片/会话目录分开，便于用户按月份查找。
文件是长期资产：``/clear`` 会话不删除文件，由用户自行管理。

设计要点：
- 文件名不可信：落盘前消毒（去路径分隔符/控制字符/空字符、限长 200、
  防路径穿越），空名或 ``.``/``..`` 用 ``file`` 兜底；同月目录重名自动加
  ``-1``/``-2`` 后缀，保证不覆盖已有文件。
- 大小上限：``MAX_FILE_BYTES``（默认 50MB），超限抛 ``FileTooLargeError``，
  由调用方丢弃并提示用户。
- 权限：月度目录 ``0700``、文件 ``0600``。
- ``save`` 返回 ``FileRef``（id / 相对 ``ref_root`` 的路径（生产环境为
  ``workspace/files/YYYY-MM/name``，与 ReadFileTool 的 workspace 根一致） /
  消毒名 / 字节数），总线与入站 content 只搬运引用，不读内容、不花 token。
- ``list_files(month)`` 列出某月已归档文件，便于测试与用户自查。
"""

import os
import re
import time
import uuid

MAX_FILE_BYTES = 50 * 1024 * 1024  # 50MB；v1 用代码常量，不放进 config

_INVALID_NAME_CHARS = re.compile(r"[/\\\x00-\x1f\x7f]")
_MAX_NAME_CHARS = 200
_MAX_EXT_CHARS = 16


class FileTooLargeError(ValueError):
    """文件超过大小上限：调用方应丢弃该文件并提示用户。"""


class FileStore:
    """按月份归档入站文件的落盘、消毒、列出与删除。

    参数：
        files_dir: 落盘根目录（如 ``<项目根>/workspace/files``）。
        ref_root: ``FileRef.path`` 的引用根，即 Agent ``read_file`` 工具的
            workspace 根（生产环境为项目根）。缺省取 ``files_dir`` 自身，
            此时 path 形如 ``YYYY-MM/name``（默认行为，不传即可用）。
    """

    def __init__(self, files_dir: str, ref_root: str | None = None) -> None:
        # files_dir = <data_root>/files（默认 workspace/files）
        self.files_dir = os.path.abspath(files_dir)
        self.ref_root = (
            os.path.abspath(ref_root) if ref_root is not None else self.files_dir
        )

    # -- 内部工具 ------------------------------------------------------------

    @staticmethod
    def _sanitize_name(file_name: str) -> str:
        """把不可信的原始文件名消毒为安全的磁盘文件名。

        规则：去路径分隔符与控制字符、去首部点/空白（防隐藏文件）、
        限长 200 字符；空名或 ``.``/``..`` 用 ``file`` 兜底。
        """
        if not isinstance(file_name, str):
            file_name = ""
        name = _INVALID_NAME_CHARS.sub("", file_name).strip()
        name = name.lstrip(" .")
        if not name or name in {".", ".."}:
            return "file"
        if len(name) > _MAX_NAME_CHARS:
            stem, ext = os.path.splitext(name)
            ext = ext[:_MAX_EXT_CHARS]
            name = stem[: _MAX_NAME_CHARS - len(ext)] + ext
        return name

    def _month_dir(self, month: str | None = None) -> str:
        """返回某月归档目录的绝对路径（YYYY-MM），不存在则自动创建 0700。"""
        month = month or time.strftime("%Y-%m")
        directory = os.path.join(self.files_dir, month)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        return directory

    def _absolute_path(self, relative_path: str) -> str | None:
        """把 ``FileRef.path``（相对 ref_root）解析为绝对路径；非法返回 None。

        解析后的绝对路径必须落在 ``files_dir`` 之内（相对 files_dir 是
        形如 ``YYYY-MM/name`` 的两段路径），杜绝路径穿越与越界删除。
        """
        if not isinstance(relative_path, str):
            return None
        target = os.path.abspath(os.path.join(self.ref_root, relative_path))
        files_dir = self.files_dir
        if target == files_dir or not target.startswith(files_dir + os.sep):
            return None
        rel = os.path.relpath(target, files_dir)
        parts = [part for part in rel.split(os.sep) if part]
        if (
            len(parts) != 2
            or parts[0] in {".", ".."}
            or parts[1] in {".", ".."}
            or not parts[1]
        ):
            return None
        if _INVALID_NAME_CHARS.search(parts[1]):
            return None
        return target

    # -- 对外 API ------------------------------------------------------------

    def save(self, data: bytes, file_name: str, mime: str | None = None) -> "object":
        """把文件字节落盘到当前月份的 ``files/YYYY-MM/``，返回 ``FileRef``。

        参数：
            data: 文件字节。
            file_name: 原始文件名（仅作显示与消毒来源，最终名由本方法决定）。
            mime: MIME 类型（可选，如 ``application/pdf``）。

        抛出：
            ``FileTooLargeError``：字节数超过 ``MAX_FILE_BYTES``。
        """
        from bus.queue import FileRef

        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("file data must be bytes")
        data = bytes(data)
        if len(data) > MAX_FILE_BYTES:
            raise FileTooLargeError(
                f"file too large: {len(data)} bytes exceeds {MAX_FILE_BYTES}"
            )
        name = self._sanitize_name(file_name)
        month = time.strftime("%Y-%m")
        directory = self._month_dir(month)
        candidate = os.path.join(directory, name)
        index = 1
        while os.path.exists(candidate):
            stem, ext = os.path.splitext(name)
            candidate = os.path.join(directory, f"{stem}-{index}{ext}")
            index += 1
        fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        # FileRef.path 是相对 ref_root 的路径；生产环境 ref_root=项目根，
        # 于是 path = workspace/files/YYYY-MM/name，Agent 可直接 read_file。
        rel_path = os.path.relpath(candidate, self.ref_root).replace(os.sep, "/")
        return FileRef(
            id=uuid.uuid4().hex,
            path=rel_path,
            name=os.path.basename(candidate),
            size=len(data),
            mime=mime,
        )

    def list_files(self, month: str | None = None) -> list:
        """列出某月目录下的已归档文件，返回按名称排序的 ``FileRef`` 列表。"""
        from bus.queue import FileRef

        month = month or time.strftime("%Y-%m")
        directory = os.path.join(self.files_dir, month)
        if not os.path.isdir(directory):
            return []
        refs = []
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            rel_path = os.path.relpath(path, self.ref_root).replace(os.sep, "/")
            refs.append(
                FileRef(
                    id=uuid.uuid4().hex,
                    path=rel_path,
                    name=name,
                    size=os.path.getsize(path),
                )
            )
        return refs

    def delete(self, ref) -> None:
        """按 ``FileRef.path`` 删除已归档文件（best-effort，不存在静默忽略）。

        用于投递失败回滚，避免同一文件在重投时被再次落盘成 ``-1`` 副本。
        """
        target = self._absolute_path(getattr(ref, "path", None))
        if target is None:
            return
        try:
            if not os.path.islink(target):
                os.unlink(target)
        except OSError:
            pass
