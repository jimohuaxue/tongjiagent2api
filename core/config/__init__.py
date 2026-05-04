"""配置层 数据模型与持久化（独立 DB，不修改现有 config_db）。"""

from core.config.schema import AccountConfig, ProxyGroupConfig
from core.config.repository import ConfigRepository

__all__ = [
    "AccountConfig",
    "ProxyGroupConfig",
    "ConfigRepository",
]
