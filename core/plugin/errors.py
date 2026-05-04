"""插件层公共异常，独立模块避免循环导入。"""


class AccountFrozenError(RuntimeError):
    """
    插件在检测到账号被限流/额度用尽时抛出，携带解冻时间戳（Unix 秒）。
    由 chat_handler 捕获后写入配置并重试其他账号。
    """

    def __init__(self, message: str, unfreeze_at: int) -> None:
        super().__init__(message)
        self.unfreeze_at = unfreeze_at
