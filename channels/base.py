"""渠道抽象基类。

``Channel`` 把「具体消息来源」（CLI / 飞书 / QQ / Web 等）与 Agent 主循环
彻底解耦：每个渠道只负责两件事——

1. 把收到的用户消息封装成 :class:`InboundMessage` 投递进 ``MessageBus``；
2. 消费 :class:`OutboundMessage`，把回复下发回对应渠道。

具体的 Agent、工具、历史管理等业务逻辑都不在渠道里，由 ``main.py`` 注入，
渠道保持纯粹的消息搬运角色。
"""

from abc import ABC, abstractmethod

from bus.queue import MessageBus, OutboundMessage


class Channel(ABC):
    """渠道抽象基类。

    子类必须实现 :meth:`start`（启动监听并投递入站消息）和
    :meth:`send`（把出站回复下发回渠道）。:meth:`stop` 提供默认空实现，
    子类按需覆盖。
    """

    def __init__(self, name: str, bus: MessageBus) -> None:
        self.name = name
        self.bus = bus

    @abstractmethod
    async def start(self) -> None:
        """启动渠道：开始监听/读取用户消息并投递进 bus。

        通常是长期运行的循环（直到收到退出信号或 :meth:`stop` 被调用）。
        """
        raise NotImplementedError

    @abstractmethod
    async def send(self, message: OutboundMessage) -> None:
        """把一条出站回复下发回该渠道。"""
        raise NotImplementedError

    async def stop(self) -> None:
        """可选：停止渠道并释放资源。默认空实现，子类按需覆盖。"""
        pass
