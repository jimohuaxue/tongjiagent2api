"""
配置数据模型：按代理 IP（指纹）分组，账号含 name / type / auth(JSON)。
不设 profile_id，user-data-dir 按指纹等由运行时拼接。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AccountConfig:
    """单个账号：名称、类别、认证 JSON。一个账号只属于一个 type。"""

    name: str
    type: str  # 如 claude, chatgpt, kimi
    auth: dict[str, Any]  # 由各插件定义 key，如 claude 用 sessionKey
    enabled: bool = True
    unfreeze_at: int | None = (
        None  # Unix 时间戳，接口返回的解冻时间；None 或已过则视为可用
    )

    def auth_json(self) -> str:
        """序列化为 JSON 字符串供 DB 存储。"""
        import json

        return json.dumps(self.auth, ensure_ascii=False)

    def is_available(self) -> bool:
        """已启用且当前时间 >= 解冻时间则可用（无解冻时间视为可用）。"""
        if not self.enabled:
            return False
        if self.unfreeze_at is None:
            return True
        import time

        return time.time() >= self.unfreeze_at


@dataclass
class ProxyGroupConfig:
    """一个代理 IP 组：代理参数 + 指纹 + 下属账号列表。"""

    proxy_host: str
    proxy_user: str
    proxy_pass: str
    fingerprint_id: str
    use_proxy: bool = True
    timezone: str | None = None
    accounts: list[AccountConfig] = field(default_factory=list)

    def account_ids(self) -> list[str]:
        """返回该组下账号的唯一标识，用于会话缓存等。格式 group_idx 由 repository 注入前不可用，这里用 name 区分。"""
        return [a.name for a in self.accounts]


def account_from_row(
    name: str,
    type: str,
    auth_json: str,
    enabled: bool = True,
    unfreeze_at: int | None = None,
) -> AccountConfig:
    """从 DB 行构造 AccountConfig。"""
    import json

    try:
        auth = json.loads(auth_json) if auth_json else {}
    except Exception:
        auth = {}
    return AccountConfig(
        name=name,
        type=type,
        auth=auth,
        enabled=enabled,
        unfreeze_at=unfreeze_at,
    )
