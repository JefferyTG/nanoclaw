"""Offline contract tests for Weixin inbound file handling (TASK-003)."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bus.queue import MessageBus
from channels.weixin import WeixinChannel, encode_weixin_target
from agent.filestore import FileStore


def current_month():
    return time.strftime("%Y-%m")


class _Writer:
    def __init__(self): self.lines = []
    def write(self, line): self.lines.append(json.loads(line))
    async def drain(self): pass


class _Reader:
    def __init__(self): self.queue = asyncio.Queue()
    async def readline(self): return await self.queue.get()


class _Process:
    def __init__(self):
        self.stdin, self.stdout, self.stderr = _Writer(), _Reader(), _Reader()
        self.returncode = None
    def terminate(self): self.returncode = 0
    def kill(self): self.returncode = -9
    async def wait(self): self.returncode = self.returncode if self.returncode is not None else 0


class WeixinFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.process = _Process()
        self.patcher = patch("channels.weixin.asyncio.create_subprocess_exec", return_value=self.process)
        self.spawn = self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.bus = MessageBus()
        self._responded_request_ids = set()
        self._state_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._state_tmp.cleanup)
        self.state_dir = Path(self._state_tmp.name)
        (self.state_dir / "inbound").mkdir()
        self.files_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.files_tmp.cleanup)
        # 与生产一致：files_dir=<根>/workspace/files、ref_root=<项目根>，
        # 因此 ref.path = workspace/files/YYYY-MM/name。
        self.file_store = FileStore(
            os.path.join(self.files_tmp.name, "workspace", "files"),
            ref_root=self.files_tmp.name,
        )
        self.channel = WeixinChannel(
            bus=self.bus, bridge_command=["fake"], allowed_user_ids=["user"],
            state_dir=self.state_dir, file_store=self.file_store,
            request_timeout_sec=0.01, stop_timeout_sec=0.01,
        )

    async def asyncTearDown(self):
        await self.channel.stop()

    async def _respond(self, *, method=None, result=None, ok=True, error=None):
        for _ in range(20):
            if self.process.stdin.lines:
                request = next(
                    (item for item in reversed(self.process.stdin.lines)
                     if item["id"] not in self._responded_request_ids
                     and (method is None or item["method"] == method)),
                    None,
                )
                if request is None:
                    await asyncio.sleep(0)
                    continue
                await self.process.stdout.queue.put(json.dumps({"v": 1, "type": "response", "id": request["id"], "ok": ok, "result": result or {}, "error": error}).encode() + b"\n")
                self._responded_request_ids.add(request["id"])
                return request
            await asyncio.sleep(0)
        self.fail("no request")

    def _write_temp_file(self, name="报告.md", data=b"file bytes"):
        temp = self.state_dir / "inbound" / name
        temp.write_bytes(data)
        return temp

    def _file_event(self, delivery_id, temp, file_name="报告.md", size=None):
        return {
            "account_id": "account", "user_id": "user", "delivery_id": delivery_id,
            "text": "", "images": [],
            "files": [{
                "file_path": str(temp),
                "file_name": file_name,
                "size": size if size is not None else temp.stat().st_size,
            }],
        }

    async def _deliver_inbound(self, data, *, consume=False):
        task = asyncio.create_task(self.channel._handle_inbound(data))
        inbound = None
        if consume:
            inbound = await asyncio.wait_for(self.bus.consume_inbound(), 1)
        request = await self._respond(method="ack_inbound")
        self.assertEqual(request["params"]["delivery_id"], data["delivery_id"])
        await task
        return inbound

    async def test_pure_file_message_builds_reference_content_and_saves_to_filestore(self):
        temp = self._write_temp_file(data=b"hello nano claw")
        self.channel.merge_window_sec = 0
        inbound = await self._deliver_inbound(
            self._file_event("file-1", temp), consume=True
        )
        month = current_month()
        expected_path = f"workspace/files/{month}/报告.md"
        self.assertIn(f"📎 收到文件：{expected_path}", inbound.content)
        self.assertIn("15B", inbound.content)  # human-readable size
        self.assertNotIn("hello nano claw", inbound.content)  # content never leaks
        self.assertEqual(len(inbound.files), 1)
        ref = inbound.files[0]
        self.assertEqual(ref.path, expected_path)
        self.assertEqual(ref.name, "报告.md")
        self.assertEqual(ref.size, 15)
        # temp file consumed; archived file exists in FileStore monthly dir
        self.assertFalse(temp.exists())
        saved = self.file_store.list_files(month)
        self.assertEqual([r.name for r in saved], ["报告.md"])
        with open(self.file_store.files_dir + "/" + month + "/报告.md", "rb") as source:
            self.assertEqual(source.read(), b"hello nano claw")

    async def test_file_plus_text_merge_keeps_text_first_then_reference(self):
        temp = self._write_temp_file(data=b"x" * (2 * 1024 * 1024))
        self.channel.merge_window_sec = 0
        event = self._file_event("file-2", temp)
        event["text"] = "看下这个文件"
        inbound = await self._deliver_inbound(event, consume=True)
        self.assertTrue(inbound.content.startswith("看下这个文件"))
        self.assertIn(f"workspace/files/{current_month()}/报告.md（2.0MB）", inbound.content)
        self.assertEqual(len(inbound.files), 1)

    async def test_oversized_file_is_discarded_but_text_message_survives(self):
        temp = self._write_temp_file(data=b"too big for this channel")
        self.channel.max_inbound_file_bytes = 4  # 4 bytes cap
        self.channel.merge_window_sec = 0
        event = self._file_event("file-3", temp)
        event["text"] = "附件的字节数超限"
        inbound = await self._deliver_inbound(event, consume=True)
        self.assertEqual(inbound.content, "附件的字节数超限")
        self.assertEqual(inbound.files, None)
        self.assertFalse(temp.exists())  # temp cleaned up
        self.assertEqual(self.file_store.list_files(current_month()), [])

    async def test_pure_file_batch_flushes_with_reference_after_window(self):
        temp = self._write_temp_file(data=b"batch file")
        self.channel.merge_window_sec = 60
        await self._deliver_inbound(self._file_event("file-batch", temp))
        self.assertTrue(self.bus.inbound_queue.empty())
        batch = next(iter(self.channel._pending_message_batches.values()))
        self.assertEqual(len(batch.files), 1)
        flush_task = asyncio.create_task(
            self.channel._flush_message_batch_locked(("account", "user"), {})
        )
        inbound = await asyncio.wait_for(self.bus.consume_inbound(), 1)
        await flush_task
        self.assertIn(f"workspace/files/{current_month()}/报告.md", inbound.content)
        self.assertEqual(len(inbound.files), 1)

    async def test_redelivered_file_delivery_is_deduplicated_and_temp_removed(self):
        first = self._write_temp_file(name="one.txt", data=b"one")
        duplicate = self._write_temp_file(name="two.txt", data=b"duplicate")
        self.channel.merge_window_sec = 60
        event = self._file_event("same", first, file_name="one.txt")
        await self._deliver_inbound(event)
        dup_event = {
            **self._file_event("same", duplicate, file_name="two.txt"),
            "text": "",
        }
        await self._deliver_inbound(dup_event)
        batch = next(iter(self.channel._pending_message_batches.values()))
        self.assertEqual(len(batch.files), 1)
        self.assertEqual(batch.files[0].name, "one.txt")
        self.assertFalse(duplicate.exists())
        self.assertFalse(first.exists())

    async def test_bind_command_with_file_is_still_a_text_command(self):
        calls = []
        self.channel._bind_callback = lambda *args: calls.append(args) or "bound"
        temp = self._write_temp_file(data=b"data")
        event = self._file_event("bind-file", temp)
        event["text"] = "/bind-reminders"
        handler = asyncio.create_task(self.channel._handle_inbound(event))
        reply = await asyncio.wait_for(self.bus.consume_outbound(), 1)
        self.assertEqual(reply.content, "bound")
        await self._respond(method="ack_inbound")
        await handler
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.file_store.list_files(current_month()), [])

    async def test_pending_batch_with_file_persists_and_restores(self):
        temp = self._write_temp_file(data=b"durable file")
        self.channel.merge_window_sec = 60
        await self._deliver_inbound(self._file_event("durable", temp))
        batch = next(iter(self.channel._pending_message_batches.values()))
        batch.deadline_ms = int(time.time() * 1000) - 1
        self.channel._persist_pending_message_batches_locked()
        self.assertTrue((self.state_dir / "pending_image_batches.json").exists())

        restored_bus = MessageBus()
        restored = WeixinChannel(
            bus=restored_bus, bridge_command=["fake"], state_dir=self.state_dir,
            allowed_user_ids=["user"], file_store=self.file_store,
        )
        restore = asyncio.create_task(restored._restore_pending_message_batches())
        inbound = await asyncio.wait_for(restored_bus.consume_inbound(), 0.5)
        await restore
        self.assertIn(f"workspace/files/{current_month()}/报告.md", inbound.content)
        self.assertEqual(len(inbound.files), 1)
        self.assertEqual(inbound.files[0].size, 12)
        self.assertFalse((self.state_dir / "pending_image_batches.json").exists())
        await restored.stop()


if __name__ == "__main__":
    unittest.main()
