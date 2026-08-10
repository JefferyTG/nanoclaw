"""Offline unit tests for :mod:`agent.filestore` (FileStore)."""

import asyncio
import os
import stat
import tempfile
import time
import unittest

from agent.filestore import FileStore, FileTooLargeError, MAX_FILE_BYTES


def current_month():
    return time.strftime("%Y-%m")


def make_store(tmp):
    """构造与生产一致的 FileStore：落盘 <tmp>/workspace/files，引用根为 <tmp>。

    生产环境 files_dir = <项目根>/workspace/files、ref_root = 项目根
    （config.workspace），因此 FileRef.path = workspace/files/YYYY-MM/name，
    Agent 可直接 read_file。
    """
    return FileStore(os.path.join(tmp, "workspace", "files"), ref_root=tmp)


class FileStoreSanitizeTests(unittest.TestCase):
    def test_strips_path_separators_and_control_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            ref = store.save(b"data", "../../evil\0\u0001name.txt")
            # separators/control chars removed, leading dots stripped
            self.assertEqual(ref.name, "evilname.txt")
            self.assertNotIn("/", ref.name)
            self.assertNotIn("\\", ref.name)
            self.assertNotIn("\x00", ref.name)
            self.assertNotIn("\x01", ref.name)
            self.assertTrue(ref.name.endswith(".txt"))

    def test_empty_or_dot_name_falls_back_to_file(self):
        for bad in ("", "   ", "...", "..", "/", ".", " . "):
            with tempfile.TemporaryDirectory() as tmp:
                store = make_store(tmp)
                ref = store.save(b"data", bad)
                self.assertEqual(ref.name, "file")

    def test_leading_dots_and_spaces_are_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            ref = store.save(b"data", "  .hidden.txt")
            self.assertEqual(ref.name, "hidden.txt")

    def test_long_name_is_truncated_to_200_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            long_name = "x" * 300 + ".md"
            ref = store.save(b"data", long_name)
            self.assertLessEqual(len(ref.name), 200)
            self.assertTrue(ref.name.endswith(".md"))


class FileStoreSaveTests(unittest.TestCase):
    def test_saves_under_current_month_directory_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            ref = store.save(b"hello file", "报告.md")
            month = current_month()
            # path 是相对 ref_root（项目根）的引用，Agent 可直接 read_file。
            self.assertEqual(ref.path, f"workspace/files/{month}/报告.md")
            self.assertEqual(ref.size, 10)
            month_dir = os.path.join(store.files_dir, month)
            self.assertTrue(os.path.isdir(month_dir))
            self.assertEqual(stat.S_IMODE(os.stat(month_dir).st_mode), 0o700)
            absolute = os.path.join(month_dir, "报告.md")
            with open(absolute, "rb") as source:
                self.assertEqual(source.read(), b"hello file")
            self.assertEqual(stat.S_IMODE(os.stat(absolute).st_mode), 0o600)
            # 补充断言：用返回的 path 经 ReadFileTool（workspace=项目根=tmp）
            # 能读到落盘字节 —— 这正是本修复要保证的语义。
            from agent.tools.filesystem import ReadFileTool

            content = asyncio.run(ReadFileTool(tmp).execute(ref.path))
            self.assertEqual(content, "hello file")

    def test_duplicate_names_get_incremented_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            first = store.save(b"one", "notes.md")
            second = store.save(b"two", "notes.md")
            third = store.save(b"three", "notes.md")
            self.assertEqual(first.name, "notes.md")
            self.assertEqual(second.name, "notes-1.md")
            self.assertEqual(third.name, "notes-2.md")
            month = current_month()
            month_dir = os.path.join(store.files_dir, month)
            self.assertEqual(
                sorted(os.listdir(month_dir)),
                ["notes-1.md", "notes-2.md", "notes.md"],
            )
            with open(os.path.join(month_dir, "notes-1.md"), "rb") as source:
                self.assertEqual(source.read(), b"two")

    def test_oversized_data_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            with self.assertRaises(FileTooLargeError):
                store.save(b"x" * (MAX_FILE_BYTES + 1), "big.bin")
            self.assertEqual(os.listdir(tmp), [])  # nothing written

    def test_non_bytes_data_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            with self.assertRaises(TypeError):
                store.save("not bytes", "x.txt")

    def test_mime_is_carried_into_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            ref = store.save(b"pdf", "doc.pdf", mime="application/pdf")
            self.assertEqual(ref.mime, "application/pdf")

    def test_list_files_returns_sorted_refs_for_a_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.save(b"a", "b.txt")
            store.save(b"b", "a.txt")
            month = current_month()
            refs = store.list_files(month)
            self.assertEqual([ref.name for ref in refs], ["a.txt", "b.txt"])
            self.assertTrue(
                all(ref.path.startswith(f"workspace/files/{month}/") for ref in refs)
            )
            self.assertEqual(store.list_files("2099-01"), [])

    def test_delete_removes_saved_file_and_ignores_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            ref = store.save(b"data", "gone.txt")
            absolute = os.path.join(store.files_dir, current_month(), "gone.txt")
            self.assertTrue(os.path.exists(absolute))
            store.delete(ref)
            self.assertFalse(os.path.exists(absolute))
            store.delete(ref)  # already gone: silent no-op
            # 越界路径（ref_root 之外）不会删除任何文件
            store.delete(type("F", (), {"path": "../../etc/passwd"})())
            self.assertTrue(os.path.exists("/etc/passwd"))


if __name__ == "__main__":
    unittest.main()
