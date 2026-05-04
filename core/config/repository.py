"""
配置持久化：独立 SQLite 文件，不修改现有 config_db。
表结构：proxy_group, account（含 name, type, auth JSON）。
"""

import os
import sqlite3
from pathlib import Path
from typing import Any

from core.config.schema import AccountConfig, ProxyGroupConfig, account_from_row


DB_FILENAME = "db.sqlite3"
DB_PATH_ENV_KEY = "WEB2API_DB_PATH"


def _get_db_path() -> Path:
    """专用 DB，与现有 account_pool.sqlite3 分离。"""
    configured = os.environ.get(DB_PATH_ENV_KEY, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent.parent.parent / DB_FILENAME


def _get_conn() -> sqlite3.Connection:
    p = _get_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(p)


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _init_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proxy_group (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proxy_host TEXT NOT NULL,
            proxy_user TEXT NOT NULL,
            proxy_pass TEXT NOT NULL,
            fingerprint_id TEXT NOT NULL DEFAULT '',
            use_proxy INTEGER NOT NULL DEFAULT 1,
            timezone TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proxy_group_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            auth TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (proxy_group_id) REFERENCES proxy_group(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_account_proxy_group_id ON account(proxy_group_id)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_account_type ON account(type)")
    # 解冻时间戳：接口返回后写入，判断可用性时与当前时间比较
    try:
        conn.execute("ALTER TABLE account ADD COLUMN unfreeze_at INTEGER")
    except sqlite3.OperationalError:
        pass  # 列已存在（如升级后再次初始化）
    try:
        conn.execute(
            "ALTER TABLE account ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
        )
    except sqlite3.OperationalError:
        pass  # 列已存在（如升级后再次初始化）
    try:
        conn.execute(
            "ALTER TABLE proxy_group ADD COLUMN use_proxy INTEGER NOT NULL DEFAULT 1"
        )
    except sqlite3.OperationalError:
        pass  # 列已存在（如升级后再次初始化）
    conn.commit()


class ConfigRepository:
    """配置的读写，面向对象封装。"""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or _get_db_path()

    def _conn(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self._db_path)

    def init_schema(self) -> None:
        """创建或更新表结构。"""
        conn = self._conn()
        try:
            _init_tables(conn)
        finally:
            conn.close()

    def load_groups(self) -> list[ProxyGroupConfig]:
        """加载全部代理组及账号。"""
        conn = self._conn()
        try:
            _init_tables(conn)
            groups: list[ProxyGroupConfig] = []
            for row in conn.execute(
                """
                SELECT id, proxy_host, proxy_user, proxy_pass, fingerprint_id, use_proxy, timezone
                FROM proxy_group ORDER BY id ASC
                """
            ).fetchall():
                (
                    gid,
                    proxy_host,
                    proxy_user,
                    proxy_pass,
                    fingerprint_id,
                    use_proxy,
                    timezone,
                ) = row
                accounts: list[AccountConfig] = []
                for acc_row in conn.execute(
                    "SELECT name, type, auth, enabled, unfreeze_at FROM account WHERE proxy_group_id = ? ORDER BY id ASC",
                    (gid,),
                ).fetchall():
                    name, type_, auth_json = acc_row[0], acc_row[1], acc_row[2]
                    enabled = (
                        bool(acc_row[3])
                        if len(acc_row) > 3 and acc_row[3] is not None
                        else True
                    )
                    unfreeze_at = (
                        acc_row[4]
                        if len(acc_row) > 4 and acc_row[4] is not None
                        else None
                    )
                    accounts.append(
                        account_from_row(
                            name,
                            type_,
                            auth_json or "{}",
                            enabled=enabled,
                            unfreeze_at=unfreeze_at,
                        )
                    )
                groups.append(
                    ProxyGroupConfig(
                        proxy_host=proxy_host,
                        proxy_user=proxy_user,
                        proxy_pass=proxy_pass,
                        fingerprint_id=fingerprint_id or "",
                        use_proxy=bool(use_proxy),
                        timezone=timezone,
                        accounts=accounts,
                    )
                )
            return groups
        finally:
            conn.close()

    def save_groups(self, groups: list[ProxyGroupConfig]) -> None:
        """全量覆盖写入代理组与账号。"""
        conn = self._conn()
        try:
            _init_tables(conn)
            conn.execute("DELETE FROM account")
            conn.execute("DELETE FROM proxy_group")
            for g in groups:
                cur = conn.execute(
                    """
                    INSERT INTO proxy_group (proxy_host, proxy_user, proxy_pass, fingerprint_id, use_proxy, timezone)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        g.proxy_host,
                        g.proxy_user,
                        g.proxy_pass,
                        g.fingerprint_id,
                        1 if g.use_proxy else 0,
                        g.timezone,
                    ),
                )
                gid = cur.lastrowid
                for a in g.accounts:
                    conn.execute(
                        """
                        INSERT INTO account (proxy_group_id, name, type, auth, enabled, unfreeze_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            gid,
                            a.name,
                            a.type,
                            a.auth_json(),
                            1 if a.enabled else 0,
                            a.unfreeze_at,
                        ),
                    )
            conn.commit()
        finally:
            conn.close()

    def load_raw(self) -> list[dict[str, Any]]:
        """与前端/API 一致的原始列表格式。"""
        groups = self.load_groups()
        return [
            {
                "proxy_host": g.proxy_host,
                "proxy_user": g.proxy_user,
                "proxy_pass": g.proxy_pass,
                "fingerprint_id": g.fingerprint_id,
                "use_proxy": g.use_proxy,
                "timezone": g.timezone,
                "accounts": [
                    {
                        "name": a.name,
                        "type": a.type,
                        "auth": a.auth,
                        "enabled": a.enabled,
                        "unfreeze_at": a.unfreeze_at,
                    }
                    for a in g.accounts
                ],
            }
            for g in groups
        ]

    def save_raw(self, raw: list[dict[str, Any]]) -> None:
        """从 API/前端原始格式写入并保存。"""
        groups = _raw_to_groups(raw)
        self.save_groups(groups)

    def update_account_unfreeze_at(
        self,
        fingerprint_id: str,
        account_name: str,
        unfreeze_at: int | None,
    ) -> None:
        """更新指定账号的解冻时间戳（如接口返回 429 时）。account 由 (fingerprint_id, name) 定位。"""
        conn = self._conn()
        try:
            _init_tables(conn)
            conn.execute(
                """
                UPDATE account SET unfreeze_at = ?
                WHERE proxy_group_id = (SELECT id FROM proxy_group WHERE fingerprint_id = ?)
                  AND name = ?
                """,
                (unfreeze_at, fingerprint_id, account_name),
            )
            conn.commit()
        finally:
            conn.close()


def _raw_to_groups(raw: list[dict[str, Any]]) -> list[ProxyGroupConfig]:
    """将 API 原始列表转为 ProxyGroupConfig 列表。"""
    groups: list[ProxyGroupConfig] = []
    for g in raw:
        accounts: list[AccountConfig] = []
        for a in g.get("accounts", []):
            name = str(a.get("name", "")).strip()
            type_ = str(a.get("type", "")).strip() or "tongji"
            auth = a.get("auth")
            if isinstance(auth, dict):
                pass
            elif isinstance(auth, str):
                try:
                    import json

                    auth = json.loads(auth) or {}
                except Exception:
                    auth = {}
            else:
                auth = {}
            if name:
                enabled = _as_bool(a.get("enabled", True), True)
                unfreeze_at = a.get("unfreeze_at")
                if isinstance(unfreeze_at, (int, float)):
                    unfreeze_at = int(unfreeze_at)
                else:
                    unfreeze_at = None
                accounts.append(
                    AccountConfig(
                        name=name,
                        type=type_,
                        auth=auth,
                        enabled=enabled,
                        unfreeze_at=unfreeze_at,
                    )
                )
        groups.append(
            ProxyGroupConfig(
                proxy_host=str(g.get("proxy_host", "")),
                proxy_user=str(g.get("proxy_user", "")),
                proxy_pass=str(g.get("proxy_pass", "")),
                fingerprint_id=str(g.get("fingerprint_id", "")),
                use_proxy=_as_bool(g.get("use_proxy", True), True),
                timezone=g.get("timezone"),
                accounts=accounts,
            )
        )
    return groups
