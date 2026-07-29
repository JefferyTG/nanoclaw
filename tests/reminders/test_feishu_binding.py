import asyncio
import json
import unittest
from types import SimpleNamespace

from bus.queue import MessageBus
from channels.feishu import FeishuChannel


def _event(text, *, chat_type="p2p", open_id="ou_1"):
    message = SimpleNamespace(
        message_type="text", chat_type=chat_type, chat_id="chat_1",
        content=json.dumps({"text": text}),
        mentions=[SimpleNamespace(key="@bot")] if chat_type == "group" else [],
    )
    sender = SimpleNamespace(sender_id=SimpleNamespace(open_id=open_id))
    return SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))


class FeishuBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_p2p_bind_and_unbind_forward_chat_and_open_id(self):
        calls = []

        async def bind(chat_id, open_id):
            calls.append(("bind", chat_id, open_id))
            return "bound"

        def unbind(chat_id, open_id):
            calls.append(("unbind", chat_id, open_id))
            return "unbound"

        channel = FeishuChannel(
            "feishu", MessageBus(), "id", "secret", bind_callback=bind,
            unbind_callback=unbind,
        )
        channel._loop = asyncio.get_running_loop()
        channel._on_message(_event("/bind-reminders", open_id="ou_first"))
        self.assertEqual((await channel.bus.consume_outbound()).content, "bound")
        channel._on_message(_event("/unbind-reminders", open_id="ou_second"))
        self.assertEqual((await channel.bus.consume_outbound()).content, "unbound")
        self.assertEqual(calls, [
            ("bind", "chat_1", "ou_first"),
            ("unbind", "chat_1", "ou_second"),
        ])

    async def test_group_commands_do_not_invoke_callback(self):
        called = []
        channel = FeishuChannel(
            "feishu", MessageBus(), "id", "secret",
            bind_callback=lambda *args: called.append(args),
        )
        channel._loop = asyncio.get_running_loop()
        channel._on_message(_event("/bind-reminders", chat_type="group"))
        reply = await channel.bus.consume_outbound()
        self.assertIn("私聊", reply.content)
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
