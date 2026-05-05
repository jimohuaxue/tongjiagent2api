"""
配置 API：GET/PUT /api/config；配置页 GET /config。
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from core.api.auth import (
    ADMIN_SESSION_COOKIE,
    DEFAULT_API_KEY,
    admin_logged_in,
    check_admin_login_rate_limit,
    config_login_enabled,
    configured_api_key_text,
    configured_config_secret_hash,
    record_admin_login_failure,
    record_admin_login_success,
    require_config_login,
    require_config_login_enabled,
    set_api_key,
    set_config_secret,
    verify_config_secret,
)
from core.api.chat_handler import ChatHandler
from core.api.deps import get_config_repo
from core.config.repository import ConfigRepository
from core.plugin.base import PluginRegistry
from core.usage import usage_summary

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
ASSETS_DIR = STATIC_DIR / "assets"
NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}
ASSET_CACHE_HEADERS = {
    "Cache-Control": "public, max-age=86400",
}


class AdminLoginRequest(BaseModel):
    secret: str


class ApiSettingsRequest(BaseModel):
    api_key: str | None = None


class TongjiLoginRequest(BaseModel):
    username: str
    password: str
    workspace_id: str | None = ""
    account_name: str | None = "tongji-main"
    fingerprint_id: str | None = "tongji-local"


def _admin_login_response(request: Request, body: dict[str, Any]) -> JSONResponse:
    store = request.app.state.admin_sessions
    token = store.create()
    response = JSONResponse(body)
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=store.ttl_seconds,
        path="/",
    )
    return response


def _ensure_tongji_account(raw: list[dict[str, Any]], payload: TongjiLoginRequest) -> None:
    username = payload.username.strip()
    password = payload.password.strip()
    workspace_id = (payload.workspace_id or "").strip()
    account_name = (payload.account_name or "tongji-main").strip() or "tongji-main"
    fingerprint_id = (payload.fingerprint_id or "tongji-local").strip() or "tongji-local"

    target_group: dict[str, Any] | None = None
    target_account: dict[str, Any] | None = None
    first_tongji_group: dict[str, Any] | None = None

    for group in raw:
        accounts = group.setdefault("accounts", [])
        if not isinstance(accounts, list):
            group["accounts"] = accounts = []
        for account in accounts:
            if not isinstance(account, dict):
                continue
            if str(account.get("type") or "").lower() != "tongji":
                continue
            if first_tongji_group is None:
                first_tongji_group = group
            auth = account.get("auth") if isinstance(account.get("auth"), dict) else {}
            if auth.get("username") == username or account.get("name") == account_name:
                target_group = group
                target_account = account
                break
        if target_account is not None:
            break

    if target_group is None:
        if first_tongji_group is not None:
            target_group = first_tongji_group
        elif raw:
            target_group = raw[0]
        else:
            target_group = {
                "use_proxy": False,
                "proxy_host": "",
                "proxy_user": "",
                "proxy_pass": "",
                "fingerprint_id": fingerprint_id,
                "timezone": "Asia/Shanghai",
                "accounts": [],
            }
            raw.append(target_group)

    target_group.setdefault("use_proxy", False)
    target_group.setdefault("proxy_host", "")
    target_group.setdefault("proxy_user", "")
    target_group.setdefault("proxy_pass", "")
    target_group.setdefault("fingerprint_id", fingerprint_id)
    target_group.setdefault("timezone", "Asia/Shanghai")
    accounts = target_group.setdefault("accounts", [])
    if not isinstance(accounts, list):
        accounts = []
        target_group["accounts"] = accounts

    if target_account is None:
        target_account = {
            "name": account_name,
            "type": "tongji",
            "enabled": True,
            "auth": {},
            "unfreeze_at": None,
        }
        accounts.append(target_account)

    target_account["name"] = str(target_account.get("name") or account_name).strip()
    target_account["type"] = "tongji"
    target_account["enabled"] = True
    target_account["auth"] = {
        "username": username,
        "password": password,
        "workspace_id": workspace_id,
    }


async def _refresh_config_in_background(app: Any, repo: ConfigRepository) -> None:
    try:
        handler: ChatHandler | None = getattr(app.state, "chat_handler", None)
        if handler is None:
            return
        await handler.refresh_configuration(repo.load_groups(), config_repo=repo)
    except Exception:
        logger.exception("同济登录后刷新账号池失败")


def create_config_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/types")
    def get_types(_: None = Depends(require_config_login)) -> list[str]:
        """返回已注册的 type 列表，供配置页 type 下拉使用。"""
        return PluginRegistry.all_types()

    @router.get("/api/tongji/models")
    def get_tongji_models(_: None = Depends(require_config_login)) -> dict[str, Any]:
        """返回同济模型广场数据，供配置页展示。"""
        plugin = PluginRegistry.get("tongji")
        if plugin is None:
            raise HTTPException(status_code=404, detail="tongji 插件未注册")
        try:
            mapping = plugin.model_mapping()
        except Exception as e:
            logger.exception("读取同济模型列表失败")
            raise HTTPException(status_code=500, detail=str(e)) from e
        if not isinstance(mapping, dict) or not mapping:
            raise HTTPException(status_code=500, detail="同济模型列表为空")
        return {
            "provider": "tongji",
            "models": [
                {"id": model_id, "name": display_name}
                for model_id, display_name in mapping.items()
            ],
        }

    @router.get("/assets/{asset_path:path}", response_model=None)
    def static_asset(asset_path: str) -> FileResponse:
        """返回配置页使用的本地静态资源。"""
        root = ASSETS_DIR.resolve()
        path = (ASSETS_DIR / asset_path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(status_code=404, detail="静态资源不存在")
        return FileResponse(path, headers=ASSET_CACHE_HEADERS)

    @router.get("/api/config")
    def get_config(
        _: None = Depends(require_config_login),
        repo: ConfigRepository = Depends(get_config_repo),
    ) -> list[dict[str, Any]]:
        """获取配置（代理组 + 账号 name/type/auth）。"""
        return repo.load_raw()

    @router.get("/api/config/status")
    def get_config_status(
        request: Request,
        _: None = Depends(require_config_login),
        repo: ConfigRepository = Depends(get_config_repo),
    ) -> dict[str, Any]:
        """返回配置页需要的账号运行时状态。"""
        handler: ChatHandler | None = getattr(request.app.state, "chat_handler", None)
        if handler is None:
            raise HTTPException(status_code=503, detail="服务未就绪")
        runtime_status = handler.get_account_runtime_status()
        now = int(time.time())
        accounts: dict[str, dict[str, Any]] = {}
        for group in repo.load_groups():
            for account in group.accounts:
                account_id = f"{group.fingerprint_id}:{account.name}"
                runtime = runtime_status.get(account_id, {})
                is_frozen = (
                    account.unfreeze_at is not None and int(account.unfreeze_at) > now
                )
                accounts[account_id] = {
                    "fingerprint_id": group.fingerprint_id,
                    "account_name": account.name,
                    "enabled": account.enabled,
                    "unfreeze_at": account.unfreeze_at,
                    "is_frozen": is_frozen,
                    "is_active": bool(runtime.get("is_active")),
                    "tab_state": runtime.get("tab_state"),
                    "accepting_new": runtime.get("accepting_new"),
                    "active_requests": runtime.get("active_requests", 0),
                }
        return {"now": now, "accounts": accounts}

    @router.get("/api/usage")
    def get_usage(
        days: int = 7,
        granularity: str = "day",
        _: None = Depends(require_config_login),
    ) -> dict[str, Any]:
        """返回本地请求用量统计。Token 为本地估算值。"""
        return usage_summary(days=days, granularity=granularity)

    @router.get("/api/admin/status")
    def admin_status(request: Request) -> dict[str, Any]:
        initialized = config_login_enabled()
        return {
            "initialized": initialized,
            "logged_in": admin_logged_in(request) if initialized else False,
        }

    @router.get("/api/admin/settings")
    def get_admin_settings(
        _: None = Depends(require_config_login),
    ) -> dict[str, Any]:
        return {
            "api_key": configured_api_key_text(),
            "default_api_key": DEFAULT_API_KEY,
        }

    @router.put("/api/admin/settings")
    def put_admin_settings(
        payload: ApiSettingsRequest,
        _: None = Depends(require_config_login),
    ) -> dict[str, Any]:
        api_key = set_api_key(payload.api_key)
        return {
            "status": "ok",
            "api_key": api_key,
            "message": "API Key 已保存",
        }

    @router.put("/api/config")
    async def put_config(
        request: Request,
        config: list[dict[str, Any]],
        _: None = Depends(require_config_login),
        repo: ConfigRepository = Depends(get_config_repo),
    ) -> dict[str, Any]:
        """更新配置并立即生效。"""
        if not config:
            raise HTTPException(status_code=400, detail="配置不能为空")
        for i, g in enumerate(config):
            if not isinstance(g, dict):
                raise HTTPException(status_code=400, detail=f"第 {i + 1} 项应为对象")
            if "fingerprint_id" not in g:
                raise HTTPException(
                    status_code=400, detail=f"代理组 {i + 1} 缺少字段: fingerprint_id"
                )
            use_proxy = g.get("use_proxy", True)
            if isinstance(use_proxy, str):
                use_proxy = use_proxy.strip().lower() not in {
                    "0",
                    "false",
                    "no",
                    "off",
                }
            else:
                use_proxy = bool(use_proxy)
            if use_proxy and not str(g.get("proxy_host", "")).strip():
                raise HTTPException(
                    status_code=400,
                    detail=f"代理组 {i + 1} 启用了代理，需填写 proxy_host",
                )
            accounts = g.get("accounts", [])
            if not accounts:
                raise HTTPException(
                    status_code=400, detail=f"代理组 {i + 1} 至少需要一个账号"
                )
            for j, a in enumerate(accounts):
                if not isinstance(a, dict) or not (a.get("name") or "").strip():
                    raise HTTPException(
                        status_code=400,
                        detail=f"代理组 {i + 1} 账号 {j + 1} 需包含 name",
                    )
                if not (a.get("type") or "").strip():
                    raise HTTPException(
                        status_code=400,
                        detail=f"代理组 {i + 1} 账号 {j + 1} 需包含 type（如 tongji）",
                    )
                if "enabled" in a and not isinstance(
                    a.get("enabled"), (bool, int, str)
                ):
                    raise HTTPException(
                        status_code=400,
                        detail=f"代理组 {i + 1} 账号 {j + 1} 的 enabled 类型无效",
                    )
        try:
            repo.save_raw(config)
        except Exception as e:
            logger.exception("保存配置失败")
            raise HTTPException(status_code=400, detail=str(e)) from e
        # 立即生效：重新加载池并替换 chat_handler
        try:
            groups = repo.load_groups()
            handler: ChatHandler | None = getattr(
                request.app.state, "chat_handler", None
            )
            if handler is None:
                raise RuntimeError("chat_handler 未初始化")
            await handler.refresh_configuration(groups, config_repo=repo)
        except Exception as e:
            logger.exception("重载账号池失败")
            raise HTTPException(
                status_code=500, detail=f"配置已保存但重载失败: {e}"
            ) from e
        return {"status": "ok", "message": "配置已保存并生效"}

    @router.get("/login", response_model=None)
    def login_page(request: Request) -> FileResponse | RedirectResponse:
        if config_login_enabled() and admin_logged_in(request):
            return RedirectResponse(url="/config", status_code=302)
        path = STATIC_DIR / "login.html"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="登录页未就绪")
        return FileResponse(path, headers=NO_CACHE_HEADERS)

    @router.post("/api/admin/login", response_model=None)
    def admin_login(payload: AdminLoginRequest, request: Request) -> Response:
        check_admin_login_rate_limit(request)
        secret = payload.secret.strip()
        encoded = configured_config_secret_hash()
        if not secret:
            lock_seconds = record_admin_login_failure(request)
            if lock_seconds > 0:
                raise HTTPException(
                    status_code=429,
                    detail=f"登录失败次数过多，请 {lock_seconds} 秒后再试",
                )
            raise HTTPException(status_code=400, detail="请输入控制台密码")
        if not encoded:
            set_config_secret(secret)
            record_admin_login_success(request)
            return _admin_login_response(
                request,
                {"status": "ok", "initialized": True, "message": "控制台密码已保存"},
            )
        if not verify_config_secret(secret, encoded):
            lock_seconds = record_admin_login_failure(request)
            if lock_seconds > 0:
                raise HTTPException(
                    status_code=429,
                    detail=f"登录失败次数过多，请 {lock_seconds} 秒后再试",
                )
            raise HTTPException(status_code=401, detail="登录失败，secret 不正确")
        record_admin_login_success(request)
        return _admin_login_response(request, {"status": "ok"})

    @router.post("/api/tongji/login", response_model=None)
    async def tongji_login(
        payload: TongjiLoginRequest,
        request: Request,
        _: None = Depends(require_config_login),
        repo: ConfigRepository = Depends(get_config_repo),
    ) -> Response:
        require_config_login_enabled()
        check_admin_login_rate_limit(request)
        username = payload.username.strip()
        password = payload.password.strip()
        if not username or not password:
            lock_seconds = record_admin_login_failure(request)
            if lock_seconds > 0:
                raise HTTPException(
                    status_code=429,
                    detail=f"登录失败次数过多，请 {lock_seconds} 秒后再试",
                )
            raise HTTPException(status_code=400, detail="请输入同济账号和密码")

        raw = repo.load_raw()
        _ensure_tongji_account(raw, payload)
        try:
            repo.save_raw(raw)
        except Exception as e:
            logger.exception("保存同济账号失败")
            raise HTTPException(status_code=400, detail=str(e)) from e

        record_admin_login_success(request)
        asyncio.create_task(_refresh_config_in_background(request.app, repo))
        return _admin_login_response(
            request,
            {"status": "ok", "message": "同济账号已保存，正在后台刷新登录状态"},
        )

    @router.post("/api/admin/logout", response_model=None)
    def admin_logout(request: Request) -> Response:
        token = (request.cookies.get(ADMIN_SESSION_COOKIE) or "").strip()
        store = getattr(request.app.state, "admin_sessions", None)
        if store is not None:
            store.revoke(token)
        response = JSONResponse({"status": "ok"})
        response.delete_cookie(ADMIN_SESSION_COOKIE, path="/")
        return response

    @router.get("/config", response_model=None)
    def config_page(request: Request) -> FileResponse | RedirectResponse:
        """配置页入口。"""
        if not config_login_enabled():
            return RedirectResponse(url="/login", status_code=302)
        if not admin_logged_in(request):
            return RedirectResponse(url="/login", status_code=302)
        path = STATIC_DIR / "config.html"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="配置页未就绪")
        return FileResponse(path, headers=NO_CACHE_HEADERS)

    return router
