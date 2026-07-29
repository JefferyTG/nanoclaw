"""CLI 渠道：从终端 stdin 读取用户输入，把回复打印回终端。

``CLIChannel`` 不依赖 Agent 本体——它只负责「读输入 → 投递进 bus → 等回复 →
打印回复」这一套 IO 循环。工具列表（``tool_names``）和清空历史回调
（``_clear_callback``）都由 ``main.py`` 在创建后注入，渠道本身不碰业务逻辑，
从而保持与 Agent 的彻底解耦。

多会话：以整数序号区分不同会话，对应 ``sender_id="local{n}"``，Gateway 据此
推导出 session_key ``"cli:local{n}"``，每个序号对应一个独立会话（历史、Agent
实例各自隔离）。内置命令 ``/new`` 开新会话、``/sessions`` 列表、``/switch <n>``
切换；``/clear`` 只清当前会话。
"""

import asyncio
import threading

from channels.base import Channel
from bus.queue import InboundMessage, OutboundMessage


class CLIChannel(Channel):
    """命令行交互渠道（name 固定为 "cli"）。"""

    def __init__(self, bus) -> None:
        super().__init__(name="cli", bus=bus)
        # 同步「等回复」用的事件：start() 投递进站消息后 await 它，
        # send() 下发回复时 set() 唤醒，start() 才能回到下一轮 input()。
        self._response_event = asyncio.Event()
        # 以下两个属性由 main.py 在创建后注入，CLIChannel 自身不依赖 Agent
        self.tool_names: list = []          # 工具名列表（/tools 命令打印）
        self._clear_callback = None         # 清空历史回调（/clear 命令调用）

        # —— 多会话状态 ——
        # 会话以整数序号标识，sender_id="local{n}" -> Gateway 的 session_key
        # 为 "cli:local{n}"。_session_seq 是已创建会话数（也作最新序号），
        # _current_session 是当前活动会话序号。
        self._session_seq: int = 0
        self._current_session: int = 0

    def _current_sender_id(self) -> str:
        """当前活动会话对应的 sender_id（Gateway 据此推导 session_key）。"""
        return f"local{self._current_session}"

    async def _read_line(self) -> str:
        """Read one terminal line without borrowing asyncio's default executor.

        ``input()`` cannot be cancelled.  ``asyncio.to_thread`` therefore left
        a default-executor worker blocked after SIGINT, and ``asyncio.run``
        waits for that worker while shutting down.  A one-shot daemon thread is
        intentionally not joined: cancellation can finish the application
        immediately, while normal input/EOF/KeyboardInterrupt is delivered
        back to the event loop exactly once.
        """
        loop = asyncio.get_running_loop()
        result = loop.create_future()

        def deliver(value=None, error=None) -> None:
            if result.done():
                return
            if error is not None:
                result.set_exception(error)
            else:
                result.set_result(value)

        def read_from_stdin() -> None:
            try:
                value = input("你> ")
            except BaseException as exc:  # forward EOFError/KeyboardInterrupt
                try:
                    loop.call_soon_threadsafe(deliver, None, exc)
                except RuntimeError:  # loop has already closed after SIGINT
                    pass
            else:
                try:
                    loop.call_soon_threadsafe(deliver, value, None)
                except RuntimeError:  # loop has already closed after SIGINT
                    pass

        threading.Thread(
            target=read_from_stdin, name="nanoclaw-cli-input", daemon=True
        ).start()
        return await result

    async def start(self) -> None:
        """终端交互循环：读输入、处理命令、投递消息、等待回复。"""
        print("（CLI 渠道已启动｜/exit 退出｜/clear 清当前会话｜/new 新会话"
              "｜/sessions 列表｜/switch <n> 切换｜/tools 看工具）")
        while True:
            try:
                line = await self._read_line()
            except (EOFError, KeyboardInterrupt):
                print("\n👋 再见")
                break

            text = line.strip()
            if not text:
                continue

            # —— 内置命令 ——
            if text == "/exit":
                print("👋 再见")
                break
            if text == "/clear":
                if self._clear_callback is not None:
                    self._clear_callback(f"cli:local{self._current_session}")
                print(f"🧹 当前会话 #{self._current_session} 历史已清空")
                continue
            if text == "/tools":
                if self.tool_names:
                    print("🔧 可用工具：" + ", ".join(self.tool_names))
                else:
                    print("🔧 暂未注入工具列表")
                continue
            if text == "/new":
                # 新建一个空白会话并立即切换过去；旧会话保留（磁盘+缓存），
                # 可经 /switch 切回，不会被丢弃。
                self._session_seq += 1
                self._current_session = self._session_seq
                print(f"🆕 已新建会话 #{self._current_session}"
                      f"（旧会话已保留，可 /switch 切回）")
                continue
            if text == "/sessions":
                items = []
                for i in range(self._session_seq + 1):
                    mark = " ← 当前" if i == self._current_session else ""
                    items.append(f"  会话 #{i}{mark}")
                print("📋 已有会话：\n" + "\n".join(items))
                continue
            if text.startswith("/switch"):
                parts = text.split()
                if len(parts) != 2 or not parts[1].isdigit():
                    print("⚠️ 用法：/switch <会话序号>，例如 /switch 0")
                    continue
                target = int(parts[1])
                if target < 0 or target > self._session_seq:
                    print(f"⚠️ 会话 #{target} 不存在"
                          f"（有效范围 0~{self._session_seq}）")
                    continue
                self._current_session = target
                print(f"🔀 已切换到会话 #{target}")
                continue

            # —— 正常输入：封装成入站消息投递进 bus ——
            # sender_id 携带当前会话序号，Gateway 据此派生独立 session_key，
            # 从而不同会话的历史互不干扰。
            msg = InboundMessage(
                channel="cli",
                sender_id=self._current_sender_id(),
                chat_id="direct",
                content=text,
            )
            await self.bus.publish_inbound(msg)

            # 等待本轮回复完成（由 send() 在打印回复后 set），再回到 input
            await self._response_event.wait()
            self._response_event.clear()

    async def send(self, message: OutboundMessage) -> None:
        """打印回复，并唤醒正在等待的 start() �循环。"""
        print(f"🤖 {message.content}")
        self._response_event.set()
