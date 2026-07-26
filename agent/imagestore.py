"""图片存储（ImageStore）。

把关心的图片落盘到 ``<sessions_dir>/<safe_key>_images/`` 目录下，与现有的
``<safe_key>.jsonl`` 会话文件**并存**（目录 vs 文件，名字不同，互不冲突），
因此 ``SessionManager`` 的扁平 ``.jsonl`` 结构一行都不用改。

设计要点：
- 落盘位置用 ``session_key`` 推导的 ``safe_key``（``:`` 换成 ``_``），与
  ``SessionManager._get_session_path`` 的命名规则保持一致，便于按会话清理。
- 仅存图片字节与 ``ImageRef``（id / path / mime），base64 不落盘、调用视觉模型时
  现生成。
- ``resolve`` 按 ``session_key + image_id`` 找回 ``ImageRef``，供 ``ask_image``
  工具或基础模型多模态直传时使用；文件已删则返回 ``None``。
- ``clear`` 在 ``/clear`` / 删会话时调用，删除该会话的图片目录，避免无限堆积。
"""

import os
import shutil
import uuid


class ImageStore:
    """按会话维度管理图片的落盘、解析与清理。"""

    def __init__(self, sessions_dir: str) -> None:
        # 与 SessionManager 共用同一 sessions_dir（默认 workspace/sessions）
        self.sessions_dir = sessions_dir

    @staticmethod
    def _safe_key(session_key: str) -> str:
        """把 session_key 中的 ':' 换成 '_'，与 SessionManager 命名规则一致。"""
        return session_key.replace(":", "_")

    def _images_dir(self, session_key: str) -> str:
        return os.path.join(self.sessions_dir, self._safe_key(session_key) + "_images")

    def save(
        self,
        session_key: str,
        data: bytes,
        ext: str,
        mime: str = "image/png",
    ) -> "object":
        """保存一张图片，返回 ``ImageRef``。

        参数：
            session_key: 会话标识（含渠道前缀，如 ``web:ws-xxx:0``）。
            data: 图片字节。
            ext: 文件扩展名（不含点），如 ``png`` / ``jpg``。
            mime: MIME 类型。

        返回：
            ``ImageRef``，含生成的全局唯一 id 与落盘绝对路径。
        """
        from bus.queue import ImageRef

        images_dir = self._images_dir(session_key)
        os.makedirs(images_dir, exist_ok=True)
        image_id = uuid.uuid4().hex
        ext = (ext or "png").lstrip(".")
        path = os.path.join(images_dir, f"{image_id}.{ext}")
        with open(path, "wb") as f:
            f.write(data)
        return ImageRef(id=image_id, path=path, mime=mime or "image/png")

    def resolve(self, session_key: str, image_id: str):
        """按 session_key + image_id 找回 ``ImageRef``；不存在返回 ``None``。

        在 ``<safe_key>_images/`` 下匹配 ``<image_id>.*`` 的第一个文件
        （扩展名不限定，保证前端传 ``png``/``jpg`` 都能命中）。
        """
        from bus.queue import ImageRef

        images_dir = self._images_dir(session_key)
        if not os.path.isdir(images_dir):
            return None
        for name in os.listdir(images_dir):
            if name.startswith(image_id + "."):
                path = os.path.join(images_dir, name)
                ext = os.path.splitext(name)[1].lstrip(".").lower() or "png"
                mime = {
                    "png": "image/png",
                    "jpg": "image/jpeg",
                    "jpeg": "image/jpeg",
                    "gif": "image/gif",
                    "webp": "image/webp",
                    "bmp": "image/bmp",
                }.get(ext, "image/png")
                return ImageRef(id=image_id, path=path, mime=mime)
        return None

    def clear(self, session_key: str) -> None:
        """删除该会话的图片目录（不存在则静默忽略）。"""
        images_dir = self._images_dir(session_key)
        if os.path.isdir(images_dir):
            shutil.rmtree(images_dir, ignore_errors=True)
