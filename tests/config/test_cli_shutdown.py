"""Regression tests for CLI stdin cancellation without a real terminal."""

import asyncio
import subprocess
import sys
import threading
import textwrap
import unittest
from unittest.mock import patch

from bus.queue import MessageBus
from channels.cli import CLIChannel


class CLIShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_exit_command_still_returns(self):
        channel = CLIChannel(MessageBus())
        with patch("builtins.input", return_value="/exit"):
            await asyncio.wait_for(channel.start(), timeout=1)

    async def test_eof_still_returns(self):
        channel = CLIChannel(MessageBus())
        with patch("builtins.input", side_effect=EOFError):
            await asyncio.wait_for(channel.start(), timeout=1)

    async def test_cancel_does_not_wait_for_blocked_input_thread(self):
        channel = CLIChannel(MessageBus())
        entered = threading.Event()
        release = threading.Event()

        def blocking_input(_prompt):
            entered.set()
            release.wait(timeout=5)
            return "/exit"

        with patch("builtins.input", side_effect=blocking_input):
            task = asyncio.create_task(channel.start())
            for _ in range(50):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(entered.is_set())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=0.2)
            # Let the daemon reader leave before this isolated event loop closes.
            release.set()

    async def test_process_exits_while_stdin_reader_remains_blocked(self):
        script = textwrap.dedent("""
            import asyncio
            from bus.queue import MessageBus
            from channels.cli import CLIChannel

            async def run():
                task = asyncio.create_task(CLIChannel(MessageBus()).start())
                await asyncio.sleep(0.1)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            asyncio.run(run())
        """)
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            for _ in range(200):
                if process.poll() is not None:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(
                process.poll(), 0,
                process.stderr.read() if process.poll() is not None else "child did not exit",
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


if __name__ == "__main__":
    unittest.main()
