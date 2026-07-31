"""视频存储（VideoStore）。

把生成好的视频落盘到 ``<sessions_dir>/<safe_key>_videos/`` 目录下，与现有的
``<safe_key>.jsonl`` 会话文件和 ``<safe_key>_images/`` 图片目录**并存**（目录 vs
文件，名字不同，互不冲突），因此 ``SessionManager`` 的扁平 ``.jsonl`` 结构一行
都不用改。

设计要点：
- 落盘位置用 ``session_key`` 推导的 ``safe_key``（``:`` 换成 ``_``），与
  ``SessionManager._get_session_path`` / ``ImageStore`` 的命名规则保持一致，
  便于按会话清理。
- 仅存视频字节与 ``VideoRef``（id / path / mime），与 ImageStore 对称。
- ``resolve`` 按 ``session_key + video_id`` 找回 ``VideoRef``；文件已删则返回
  ``None``。
- ``clear`` 在 ``/clear`` / 删会话时调用，删除该会话的视频目录，避免几十 MB
  的大文件无限堆积。
"""

import os
import shutil
import uuid
from dataclasses import dataclass


@dataclass
class VideoRef:
    """一个已落盘视频文件的引用（id / 绝对路径 / mime）。"""

    id: str
    path: str
    mime: str = "video/mp4"


class VideoStore:
    """按会话维度管理视频的落盘、解析与清理。"""

    def __init__(self, sessions_dir: str) -> None:
        # 与 SessionManager / ImageStore 共用同一 sessions_dir
        self.sessions_dir = sessions_dir

    @staticmethod
    def _safe_key(session_key: str) -> str:
        """把 session_key 中的 ':' 换成 '_'，与 SessionManager 命名规则一致。"""
        return session_key.replace(":", "_")

    def _videos_dir(self, session_key: str) -> str:
        return os.path.join(
            self.sessions_dir, self._safe_key(session_key) + "_videos"
        )

    def save(
        self,
        session_key: str,
        data: bytes,
        ext: str = "mp4",
        mime: str = "video/mp4",
    ) -> VideoRef:
        """保存一段视频，返回 ``VideoRef``。

        参数：
            session_key: 会话标识（含渠道前缀，如 ``web:ws-xxx:0``）。
            data: 视频字节。
            ext: 文件扩展名（不含点），如 ``mp4`` / ``mov``。
            mime: MIME 类型。
        """
        videos_dir = self._videos_dir(session_key)
        os.makedirs(videos_dir, mode=0o700, exist_ok=True)
        os.chmod(videos_dir, 0o700)
        video_id = uuid.uuid4().hex
        ext = (ext or "mp4").lstrip(".")
        path = os.path.join(videos_dir, f"{video_id}.{ext}")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return VideoRef(id=video_id, path=path, mime=mime or "video/mp4")

    def resolve(self, session_key: str, video_id: str):
        """按 session_key + video_id 找回 ``VideoRef``；不存在返回 ``None``。

        在 ``<safe_key>_videos/`` 下匹配 ``<video_id>.*`` 的第一个文件
        （扩展名不限定，与 ImageStore 行为一致）。
        """
        videos_dir = self._videos_dir(session_key)
        if not os.path.isdir(videos_dir):
            return None
        for name in os.listdir(videos_dir):
            if name.startswith(video_id + "."):
                path = os.path.join(videos_dir, name)
                ext = os.path.splitext(name)[1].lstrip(".").lower() or "mp4"
                mime = {
                    "mp4": "video/mp4",
                    "mov": "video/quicktime",
                    "webm": "video/webm",
                    "mkv": "video/x-matroska",
                    "avi": "video/x-msvideo",
                }.get(ext, "video/mp4")
                return VideoRef(id=video_id, path=path, mime=mime)
        return None

    def clear(self, session_key: str) -> None:
        """删除该会话的视频目录（不存在则静默忽略）。"""
        videos_dir = self._videos_dir(session_key)
        if os.path.isdir(videos_dir):
            shutil.rmtree(videos_dir, ignore_errors=True)
