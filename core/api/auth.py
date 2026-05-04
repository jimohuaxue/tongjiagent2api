"""
API 与配置页鉴权。

- auth.api_key: 保护 /{type}/v1/*
- auth.config_secret: 保护 /config 与 /api/config、/api/types

config_secret 会在启动时从明文自动转换成带前缀的 PBKDF2 哈希并回写到 config.yaml。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, Request, status

from core.config.settings import get, get_config_path, load_config, reset_cache

API_AUTH_REALM = "Bearer"
DEFAULT_API_KEY = "tongji-api-key"
CONFIG_SECRET_PREFIX = "web2api_pbkdf2_sha256"
CONFIG_SECRET_ITERATIONS = 600_000
ADMIN_SESSION_COOKIE = "web2api_admin_session"
DEFAULT_ADMIN_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_ADMIN_LOGIN_MAX_FAILURES = 5
DEFAULT_ADMIN_LOGIN_LOCK_SECONDS = 10 * 60


def configured_api_keys() -> list[str]:
    raw = get("auth", "api_key", DEFAULT_API_KEY)
    if isinstance(raw, list):
        keys = [str(item).strip() for item in raw if str(item).strip()]
        return keys or [DEFAULT_API_KEY]
    if raw is None or str(raw).strip() == "":
        return [DEFAULT_API_KEY]
    text = str(raw).replace("\n", ",")
    return [part.strip() for part in text.split(",") if part.strip()] or [
        DEFAULT_API_KEY
    ]


def configured_api_key_text() -> str:
    raw = get("auth", "api_key", None)
    if isinstance(raw, list):
        text = ",".join(str(item).strip() for item in raw if str(item).strip())
    elif raw is None:
        text = ""
    else:
        text = str(raw).strip()
    return text or DEFAULT_API_KEY


def _extract_request_api_key(request: Request) -> str:
    key = (request.headers.get("x-api-key") or "").strip()
    if key:
        return key
    authorization = (request.headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def require_api_key(request: Request) -> None:
    expected_keys = configured_api_keys()
    if not expected_keys:
        return
    provided = _extract_request_api_key(request)
    if provided:
        for expected in expected_keys:
            if secrets.compare_digest(provided, expected):
                return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="未授权，请提供正确的 API Key",
        headers={"WWW-Authenticate": API_AUTH_REALM},
    )


def _is_hashed_config_secret(value: str) -> bool:
    return value.startswith(f"{CONFIG_SECRET_PREFIX}$")


def configured_config_secret_hash() -> str:
    value = str(get("auth", "config_secret", "") or "").strip()
    return value if _is_hashed_config_secret(value) else ""


def config_login_enabled() -> bool:
    return bool(configured_config_secret_hash())


def configured_config_login_max_failures() -> int:
    raw = get("auth", "config_login_max_failures", DEFAULT_ADMIN_LOGIN_MAX_FAILURES)
    try:
        return max(1, int(raw))
    except Exception:
        return DEFAULT_ADMIN_LOGIN_MAX_FAILURES


def configured_config_login_lock_seconds() -> int:
    raw = get("auth", "config_login_lock_seconds", DEFAULT_ADMIN_LOGIN_LOCK_SECONDS)
    try:
        return max(1, int(raw))
    except Exception:
        return DEFAULT_ADMIN_LOGIN_LOCK_SECONDS


def hash_config_secret(secret: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        salt,
        CONFIG_SECRET_ITERATIONS,
    )
    return (
        f"{CONFIG_SECRET_PREFIX}"
        f"${CONFIG_SECRET_ITERATIONS}"
        f"${base64.urlsafe_b64encode(salt).decode('ascii')}"
        f"${base64.urlsafe_b64encode(digest).decode('ascii')}"
    )


def _yaml_scalar(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _write_auth_value(key: str, value: str) -> None:
    config_path = get_config_path()
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    rendered = f"{key}: {_yaml_scalar(value)}"

    if not original.strip():
        updated = f"auth:\n  {rendered}\n"
    else:
        lines = original.splitlines(keepends=True)
        auth_index: int | None = None
        auth_indent = 0
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped == "auth:":
                auth_index = index
                auth_indent = _line_indent(line)
                break

        if auth_index is None:
            suffix = "" if original.endswith("\n") else "\n"
            updated = original + suffix + f"auth:\n  {rendered}\n"
        else:
            section_end = len(lines)
            for index in range(auth_index + 1, len(lines)):
                line = lines[index]
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("#"):
                    continue
                if _line_indent(line) <= auth_indent:
                    section_end = index
                    break

            child_indent = " " * (auth_indent + 2)
            key_index: int | None = None
            key_indent = auth_indent + 2
            key_pattern = re.compile(rf"^([ \t]+){re.escape(key)}\s*:")
            for index in range(auth_index + 1, section_end):
                match = key_pattern.match(lines[index])
                if match:
                    key_index = index
                    key_indent = len(match.group(1))
                    child_indent = match.group(1)
                    break

            new_line = f"{child_indent}{rendered}\n"
            if key_index is None:
                lines.insert(auth_index + 1, new_line)
            else:
                remove_end = key_index + 1
                while remove_end < section_end:
                    line = lines[remove_end]
                    stripped = line.strip()
                    if not stripped:
                        break
                    if not stripped.startswith("#") and _line_indent(line) <= key_indent:
                        break
                    if stripped.startswith("#") and _line_indent(line) <= key_indent:
                        break
                    remove_end += 1
                lines[key_index:remove_end] = [new_line]
            updated = "".join(lines)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp_path.write_text(updated, encoding="utf-8")
    tmp_path.replace(config_path)
    reset_cache()
    load_config()


def set_api_key(api_key: str | None) -> str:
    value = str(api_key or "").strip() or DEFAULT_API_KEY
    _write_auth_value("api_key", value)
    return value


def set_config_secret(secret: str) -> str:
    encoded = hash_config_secret(secret)
    _write_auth_value("config_secret", encoded)
    return encoded


def verify_config_secret(secret: str, encoded: str) -> bool:
    try:
        prefix, iterations_s, salt_b64, digest_b64 = encoded.split("$", 3)
    except ValueError:
        return False
    if prefix != CONFIG_SECRET_PREFIX:
        return False
    try:
        iterations = int(iterations_s)
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)


def ensure_config_secret_hashed() -> None:
    cfg = load_config()
    auth_cfg = cfg.get("auth")
    if not isinstance(auth_cfg, dict):
        return
    raw_value = auth_cfg.get("config_secret")
    secret = str(raw_value or "").strip()
    if not secret or _is_hashed_config_secret(secret):
        return
    set_config_secret(secret)


@dataclass
class AdminSessionStore:
    ttl_seconds: int = DEFAULT_ADMIN_SESSION_TTL_SECONDS
    _sessions: dict[str, float] = field(default_factory=dict)

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = time.time() + self.ttl_seconds
        return token

    def is_valid(self, token: str) -> bool:
        if not token:
            return False
        self.cleanup()
        expires_at = self._sessions.get(token)
        if expires_at is None:
            return False
        if expires_at < time.time():
            self._sessions.pop(token, None)
            return False
        return True

    def revoke(self, token: str) -> None:
        if token:
            self._sessions.pop(token, None)

    def cleanup(self) -> None:
        now = time.time()
        expired = [token for token, expires_at in self._sessions.items() if expires_at < now]
        for token in expired:
            self._sessions.pop(token, None)


@dataclass
class LoginAttemptState:
    failures: int = 0
    locked_until: float = 0.0
    last_seen: float = 0.0


@dataclass
class AdminLoginAttemptStore:
    max_failures: int = DEFAULT_ADMIN_LOGIN_MAX_FAILURES
    lock_seconds: int = DEFAULT_ADMIN_LOGIN_LOCK_SECONDS
    _attempts: dict[str, LoginAttemptState] = field(default_factory=dict)

    def is_locked(self, client_ip: str) -> int:
        self.cleanup()
        state = self._attempts.get(client_ip)
        if state is None:
            return 0
        remaining = int(state.locked_until - time.time())
        if remaining <= 0:
            return 0
        return remaining

    def record_failure(self, client_ip: str) -> int:
        now = time.time()
        state = self._attempts.setdefault(client_ip, LoginAttemptState())
        if state.locked_until > now:
            state.last_seen = now
            return int(state.locked_until - now)
        state.failures += 1
        state.last_seen = now
        if state.failures >= self.max_failures:
            state.failures = 0
            state.locked_until = now + self.lock_seconds
            return self.lock_seconds
        return 0

    def record_success(self, client_ip: str) -> None:
        self._attempts.pop(client_ip, None)

    def cleanup(self) -> None:
        now = time.time()
        stale_before = now - max(self.lock_seconds * 2, 3600)
        expired = [
            ip
            for ip, state in self._attempts.items()
            if state.locked_until <= now and state.last_seen < stale_before
        ]
        for ip in expired:
            self._attempts.pop(ip, None)


def _admin_store(request: Request) -> AdminSessionStore:
    store = getattr(request.app.state, "admin_sessions", None)
    if store is None:
        raise HTTPException(status_code=503, detail="管理会话未初始化")
    return store


def _admin_login_attempt_store(request: Request) -> AdminLoginAttemptStore:
    store = getattr(request.app.state, "admin_login_attempts", None)
    if store is None:
        raise HTTPException(status_code=503, detail="登录限流未初始化")
    return store


def client_ip_of(request: Request) -> str:
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return str(host or "unknown")


def check_admin_login_rate_limit(request: Request) -> None:
    remaining = _admin_login_attempt_store(request).is_locked(client_ip_of(request))
    if remaining > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"登录失败次数过多，请 {remaining} 秒后再试",
        )


def record_admin_login_failure(request: Request) -> int:
    return _admin_login_attempt_store(request).record_failure(client_ip_of(request))


def record_admin_login_success(request: Request) -> None:
    _admin_login_attempt_store(request).record_success(client_ip_of(request))


def admin_logged_in(request: Request) -> bool:
    if not config_login_enabled():
        return False
    token = (request.cookies.get(ADMIN_SESSION_COOKIE) or "").strip()
    return _admin_store(request).is_valid(token)


def require_config_login_enabled() -> None:
    if not config_login_enabled():
        raise HTTPException(status_code=404, detail="配置页面未启用")


def require_config_login(request: Request) -> None:
    require_config_login_enabled()
    if admin_logged_in(request):
        return
    raise HTTPException(status_code=401, detail="请先登录配置页面")
