"""运行时键类型：代理组唯一标识。"""

from typing import NamedTuple

from core.constants import TIMEZONE


class ProxyKey(NamedTuple):
    """唯一标识一个代理组（一个浏览器进程）。"""

    proxy_host: str
    proxy_user: str
    fingerprint_id: str
    use_proxy: bool = True
    timezone: str = TIMEZONE
