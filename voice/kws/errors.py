"""KWS / 录音链路的可读错误类型（TASK-025）。"""


class KwsError(Exception):
    """KWS 唤醒或录音链路的安全错误，携带分类与用户可读消息。

    ``category`` 供程序化判断（model_missing / keywords_missing /
    mic_error / already_running / invalid_duration 等）；``message``
    为可直接展示给用户的中文提示，不暴露原始栈。
    """

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message

    def __str__(self) -> str:
        return self.message
