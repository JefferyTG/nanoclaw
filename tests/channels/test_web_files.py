"""WebChannel 文件上传分流与文件引用入站回归测试（TASK-041）。

覆盖：
- /upload 按 MIME/扩展名分流：图片 → ImageStore，其它文件 → FileStore；
- 超限文件 413、非法 key 400、缺失 file 400；
- 聊天 JSON 带 files：内容文本化（微信格式）且 InboundMessage.files 携带引用；
- 伪造/越界路径被拒绝（FileStore.resolve 校验）。
"""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from agent.filestore import FileStore
from agent.imagestore import ImageStore
from channels.web import WebChannel, _human_file_size


class FakeUpload:
    def __init__(self, data: bytes, filename: str, content_type: str):
        self.file = SimpleNamespace(read=lambda: data)
        self.filename = filename
        self.content_type = content_type


class FakeRequest:
    def __init__(self, key, data):
        self.query = {"key": key}
        self._data = data

    async def post(self):
        return self._data


class FakeBus:
    def __init__(self):
        self.inbound = []

    async def publish_inbound(self, message):
        self.inbound.append(message)


def make_channel(tmp: str):
    """构造带真实 ImageStore + 由 config.workspace 自动装配 FileStore 的 WebChannel。"""
    sessions = os.path.join(tmp, "sessions")
    os.makedirs(sessions, exist_ok=True)
    config = SimpleNamespace(workspace=tmp)
    return WebChannel(
        "web", FakeBus(), "127.0.0.1", 0, config, "config.json",
        image_store=ImageStore(sessions),
    )


def make_bare_channel():
    """无存储/无配置的裸渠道（仅测纯文本组装）。"""
    return WebChannel("web", FakeBus(), "127.0.0.1", 0, None, "config.json")


class WebUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_image_goes_to_image_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = make_channel(tmp)
            req = FakeRequest(
                "web:conn:0",
                {"file": FakeUpload(b"\x89PNG fake", "photo.png", "image/png")},
            )
            resp = await ch._handle_upload(req)
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.text)
            self.assertTrue(body["ok"])
            self.assertTrue(body["image_id"])
            self.assertEqual(body["mime"], "image/png")

    async def test_upload_pdf_goes_to_file_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = make_channel(tmp)
            data = b"%PDF-1.4 fake"
            req = FakeRequest(
                "web:conn:0",
                {"file": FakeUpload(data, "报告.pdf", "application/pdf")},
            )
            resp = await ch._handle_upload(req)
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.text)
            self.assertTrue(body["ok"])
            self.assertEqual(body["name"], "报告.pdf")
            self.assertTrue(body["path"].startswith("workspace/files/"), body["path"])
            self.assertEqual(body["size"], len(data))
            self.assertEqual(body["mime"], "application/pdf")
            # 文件确实落盘且路径相对 workspace（Agent read_file 可直接读）
            self.assertTrue(os.path.isfile(os.path.join(tmp, body["path"])))

    async def test_upload_txt_by_mime_goes_to_file_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = make_channel(tmp)
            req = FakeRequest(
                "web:conn:0",
                {"file": FakeUpload(b"hello", "note.txt", "text/plain")},
            )
            resp = await ch._handle_upload(req)
            body = json.loads(resp.text)
            self.assertTrue(body["ok"])
            self.assertIn("file_id", body)

    async def test_upload_oversize_rejected_413(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = make_channel(tmp)
            big = b"x" * (50 * 1024 * 1024 + 1)
            req = FakeRequest(
                "web:conn:0",
                {"file": FakeUpload(big, "big.bin", "application/octet-stream")},
            )
            resp = await ch._handle_upload(req)
            self.assertEqual(resp.status, 413)

    async def test_upload_invalid_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = make_channel(tmp)
            req = FakeRequest(
                "../evil",
                {"file": FakeUpload(b"x", "a.txt", "text/plain")},
            )
            resp = await ch._handle_upload(req)
            self.assertEqual(resp.status, 400)

    async def test_upload_missing_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = make_channel(tmp)
            resp = await ch._handle_upload(FakeRequest("web:conn:0", {}))
            self.assertEqual(resp.status, 400)

    def test_human_file_size(self):
        self.assertEqual(_human_file_size(0), "0B")
        self.assertEqual(_human_file_size(512), "512B")
        self.assertEqual(_human_file_size(2048), "2.0KB")
        self.assertEqual(_human_file_size(5 * 1024 * 1024), "5.0MB")


class WebInboundFileTests(unittest.IsolatedAsyncioTestCase):
    async def _upload(self, ch, filename, data, mime):
        req = FakeRequest("web:conn:0", {"file": FakeUpload(data, filename, mime)})
        resp = await ch._handle_upload(req)
        return json.loads(resp.text)

    async def test_chat_json_with_files_textifies_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = make_channel(tmp)
            up = await self._upload(ch, "note.txt", b"hello", "text/plain")
            msg = ch._build_inbound_chat("conn", {"text": "帮我看看", "files": [up]})
            self.assertIsNotNone(msg)
            self.assertTrue(msg.content.startswith("帮我看看"))
            self.assertIn("📎 收到文件：", msg.content)
            self.assertIn("note.txt", msg.content)
            self.assertIn("5B", msg.content)
            self.assertEqual(len(msg.files or []), 1)
            self.assertEqual(msg.files[0].path, up["path"])
            self.assertEqual(msg.files[0].size, 5)

    async def test_chat_json_file_only_no_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = make_channel(tmp)
            up = await self._upload(ch, "a.zip", b"PK\x03\x04", "application/zip")
            msg = ch._build_inbound_chat("conn", {"files": [up]})
            self.assertIsNotNone(msg)
            self.assertTrue(msg.content.startswith("📎 收到文件："))
            self.assertIn("a.zip", msg.content)
            self.assertEqual(len(msg.files or []), 1)

    async def test_chat_json_images_still_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = make_channel(tmp)
            up = await self._upload(ch, "photo.png", b"\x89PNG fake", "image/png")
            msg = ch._build_inbound_chat("conn", {"text": "", "images": [up["image_id"]]})
            self.assertIsNotNone(msg)
            self.assertEqual(msg.content, "请分析这张图片。")
            self.assertEqual(len(msg.images or []), 1)
            self.assertIsNone(msg.files)

    async def test_chat_json_rejects_forged_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = make_channel(tmp)
            msg = ch._build_inbound_chat(
                "conn",
                {"files": [{"file_id": "x", "path": "../../etc/passwd", "name": "passwd", "size": 1}]},
            )
            self.assertIsNone(msg)  # 内容为空 → 不发布

    async def test_chat_json_empty_payload_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = make_channel(tmp)
            self.assertIsNone(ch._build_inbound_chat("conn", {"text": "", "images": [], "files": []}))

    def test_build_content_text_then_files(self):
        ch = make_bare_channel()
        ref = SimpleNamespace(path="workspace/files/2026-08/a.txt", size=3)
        content = ch._build_content("摘要如下", [], [ref])
        lines = content.split("\n")
        self.assertEqual(lines[0], "摘要如下")
        self.assertIn("a.txt", lines[1])
        self.assertIn("3B", lines[1])

    def test_build_content_images_fallback(self):
        ch = make_bare_channel()
        self.assertEqual(ch._build_content("", ["r1"], []), "请分析这张图片。")
        self.assertEqual(ch._build_content("", ["r1", "r2"], []), "请分析这些图片。")
        self.assertEqual(ch._build_content("", [], []), "")


class FileStoreResolveTests(unittest.TestCase):
    def test_resolve_valid_and_forged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileStore(os.path.join(tmp, "workspace", "files"), ref_root=tmp)
            ref = store.save(b"data", "real.txt", "text/plain")
            resolved = store.resolve(ref)
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.path, ref.path)
            self.assertEqual(resolved.size, 4)
            # 越界路径
            self.assertIsNone(store.resolve(SimpleNamespace(path="../../etc/passwd")))
            # 不存在的文件
            self.assertIsNone(store.resolve(SimpleNamespace(path="workspace/files/2026-08/nope.txt")))


if __name__ == "__main__":
    unittest.main()
