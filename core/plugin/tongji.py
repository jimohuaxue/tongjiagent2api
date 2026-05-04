"""
同济大学 MaaS (HiAgent) 插件。

认证方式：账号密码经由同济 IAM SSO 完成登录，无需手动获取 session cookie。
auth 字段需包含：
  - username: 工号/学号
  - password: 密码

可选 auth 字段（不填则使用默认值）：
  - workspace_id: 工作空间 ID，默认 personal-example（请在配置页替换为自己的工作空间）

支持的模型（动态从 ListModelByPublic 刷新，冷启动时使用以下静态列表）：
  gemma-4-26b, gemma-4-31b, intern-s1-pro, glm-5.1,
  qwen3.5-27b, qwen3.5-35b, qwen3.5-122b, qwen3.5-397b, qwen3.5-397b-thinking,
  minimax-m2.7, kimi-k2.5, qwen3-vl-235b, tongyi-deepresearch-30b,
  qwen3-coder-480b, qwenlong-l1.5, qwen3-235b, qwen3-235b-thinking, qwen3-32b,
  deepseek-r1, deepseek-r1-distill-qwen-32b, deepseek-r1-distill-llama-70b,
  deepseek-v3, qwen-plus-latest
"""

import hashlib
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, AsyncIterator

from playwright.async_api import BrowserContext, Page

from core.api.schemas import InputAttachment
from core.plugin.base import AbstractPlugin, PluginRegistry
from core.plugin.helpers import (
    request_json_via_page_fetch,
    stream_raw_via_page_fetch,
)

logger = logging.getLogger(__name__)

_INLINE_TEXT_MIME_TYPES = {
    "application/json",
    "application/javascript",
    "application/x-javascript",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
    "application/toml",
}
_INLINE_TEXT_EXTENSIONS = {
    "c",
    "cc",
    "conf",
    "cpp",
    "css",
    "csv",
    "go",
    "h",
    "hpp",
    "html",
    "ini",
    "java",
    "js",
    "json",
    "log",
    "md",
    "py",
    "rs",
    "sh",
    "sql",
    "toml",
    "ts",
    "txt",
    "xml",
    "yaml",
    "yml",
}


def _should_inline_attachment(attachment: InputAttachment) -> bool:
    mime_type = (attachment.mime_type or "").split(";", 1)[0].strip().lower()
    if mime_type.startswith("text/") or mime_type in _INLINE_TEXT_MIME_TYPES:
        return True
    ext = attachment.filename.rsplit(".", 1)[-1].lower() if "." in attachment.filename else ""
    return ext in _INLINE_TEXT_EXTENSIONS

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
_BASE_URL = "https://agent.tongji.edu.cn"
_LOGIN_URL = f"{_BASE_URL}/login"

_DEFAULT_WORKSPACE_ID = "personal-example"
_DEFAULT_MODEL_KEY = "glm-5.1"

_API_BASE = f"{_BASE_URL}/api/aigw"
_BYPASS_BASE = f"{_BASE_URL}/api/bypass/aigw"
_UPLOAD_BASE = f"{_BASE_URL}/api/bypass/up"
_API_VER = "Version=2023-08-01&Region=cn-north-1"
_UPLOAD_VER = "Version=2022-01-01&Region=cn-north-1"

# JS：从浏览器内获取文件是否已上传（UploadRawCheck）
_UPLOAD_CHECK_JS = """
async ({ sha256 }) => {
  const checkUrl = "/api/bypass/up?Action=UploadRawCheck&Version=2022-01-01&Region=cn-north-1&Id=" + sha256;
  const csrf = (document.cookie.match(/(?:^|; )x-csrf-token=([^;]+)/) || [])[1] || "";
  const resp = await fetch(checkUrl, {
    credentials: "include",
    headers: {
      "Accept": "application/json, text/plain, */*",
      "Accept-Language": "zh",
      "X-CSRF-Token": decodeURIComponent(csrf),
      "X-Top-Region": "cn-north-1",
      "X-Up-Sha256": sha256,
    },
  });
  return await resp.json();
}
"""

_CSRF_TOKEN_JS = """
() => {
  const token = (document.cookie.match(/(?:^|; )x-csrf-token=([^;]+)/) || [])[1] || "";
  return decodeURIComponent(token);
}
"""

# ---------------------------------------------------------------------------
# 静态模型目录（冷启动 / 动态刷新失败时的兜底）
# 键 = OpenAI 模型名（小写），值 = {service_id, key, name}
# service_id: ListModelByPublic 返回的 ID 字段
# key:        平台 Key 字段（用作 CreateConversation 的 ModelName）
# name:       平台 Name 字段（用作 DisplayName）
# ---------------------------------------------------------------------------
_STATIC_CATALOG: dict[str, dict[str, str]] = {
    "gemma-4-26b":                    {"service_id": "d7ar2a7os1os9723s74g", "key": "Gemma-4-26B",                   "name": "Gemma-4-26B"},
    "gemma-4-31b":                    {"service_id": "d7ar0g0ijabqvmsbt9p0", "key": "Gemma-4-31B",                   "name": "Gemma-4-31B"},
    "intern-s1-pro":                  {"service_id": "d6obnrfvaofmtoctkpu0", "key": "Intern-S1-Pro",                 "name": "Intern-S1-Pro"},
    "glm-5.1":                        {"service_id": "d6n78knvaofmtoctk8b0", "key": "GLM-5.1",                       "name": "GLM-5.1"},
    "qwen3.5-27b":                    {"service_id": "d6l21envaofmtoctis3g", "key": "Qwen3.5-27B",                   "name": "Qwen3.5-27B"},
    "qwen3.5-35b":                    {"service_id": "d6l20etofdmc7u58ecv0", "key": "Qwen3.5-35B",                   "name": "Qwen3.5-35B"},
    "qwen3.5-122b":                   {"service_id": "d6l1u7fvaofmtoctirr0", "key": "Qwen3.5-122B",                  "name": "Qwen3.5-122B"},
    "qwen3.5-397b-thinking":          {"service_id": "d6gfu25ofdmc7u58c200", "key": "Qwen3.5-397B",                  "name": "Qwen3.5-397B-Thinking"},
    "qwen3.5-397b":                   {"service_id": "d6gf69fvaofmtoctgg60", "key": "Qwen3.5-397B",                  "name": "Qwen3.5-397B"},
    "minimax-m2.7":                   {"service_id": "d5d0srqvkd0kgrmoo320", "key": "MiniMax-M2.5",                  "name": "MiniMax-M2.7"},
    "kimi-k2.5":                      {"service_id": "d49em0a55dci57rdrrn0", "key": "Kimi-K2.5",                     "name": "Kimi-K2.5"},
    "qwen3-vl-235b":                  {"service_id": "d3tj7vem6fag1tq3f55g", "key": "Qwen3-VL-235B",                 "name": "Qwen3-VL-235B"},
    "tongyi-deepresearch-30b":        {"service_id": "d36ajuoiifqtuesfmxh0", "key": "DeepResearch-30B",              "name": "Tongyi-DeepResearch-30B"},
    "qwen3-coder-480b":               {"service_id": "d2145qu37n57mcahe3s0", "key": "Qwen3-Coder-480B",              "name": "Qwen3-Coder-480B"},
    "qwenlong-l1.5":                  {"service_id": "d0r6fckbcrjfersflcl0", "key": "QwenLong-L1.5-30B",             "name": "QwenLong-L1.5"},
    "qwen3-235b-thinking":            {"service_id": "d083pfjqpga1104sqcs1", "key": "Qwen3-235B-Thinking",           "name": "Qwen3-235B-Thinking"},
    "qwen3-32b":                      {"service_id": "d08858ngqkghlnoileag", "key": "Qwen3-32B",                     "name": "Qwen3-32B"},
    "qwen3-235b":                     {"service_id": "d083pfjqpga1104sqcsg", "key": "Qwen3-235B",                    "name": "Qwen3-235B"},
    "deepseek-r1":                    {"service_id": "cuq2j2qk4sonpk8qfqu0", "key": "DeepSeek-R1",                   "name": "DeepSeek-R1-671B-0528"},
    "deepseek-r1-distill-qwen-32b":   {"service_id": "cujc5pqk4sonpk8qai10", "key": "DeepSeek-R1-Distill-Qwen-32B", "name": "DeepSeek-R1-Distill-Qwen-32B"},
    "deepseek-r1-distill-llama-70b":  {"service_id": "cu8qtom8n9lkg2eku0r0", "key": "DeepSeek-R1-Distill-Llama-70B","name": "DeepSeek-R1-Distill-Llama-70B"},
    "deepseek-v3":                    {"service_id": "cu26hmm8n9lkg2ejro20", "key": "DeepSeek-V3",                   "name": "DeepSeek-V3_1-Terminus"},
    "qwen-plus-latest":               {"service_id": "crpnb4qtn3e5t29rt6e0", "key": "qwen-plus-latest",              "name": "Qwen-Plus-Latest"},
    # 别名
    "glm":   {"service_id": "d6n78knvaofmtoctk8b0", "key": "GLM-5.1", "name": "GLM-5.1"},
    "glm-4.7": {"service_id": "d6n78knvaofmtoctk8b0", "key": "GLM-5.1", "name": "GLM-5.1"},
}


# ---------------------------------------------------------------------------
# SSE 解析
# ---------------------------------------------------------------------------

def _parse_tongji_sse_chunk(line: str) -> tuple[list[str], str | None, str | None]:
    """
    解析单行 SSE data 行（格式：data:<json> 无空格）。
    返回 (texts, finish_reason, error)。
    """
    if not line.startswith("data:"):
        return [], None, None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return [], "done" if payload == "[DONE]" else None, None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return [], None, None

    error = _extract_tongji_sse_error(obj)
    if error:
        return [], None, error

    choices = obj.get("choices") or []
    if not choices:
        return [], None, None

    choice = choices[0]
    finish = choice.get("finish_reason")
    delta = choice.get("delta") or {}
    content = delta.get("content", "")
    texts = [content] if content else []
    return texts, finish, None


def _extract_tongji_sse_error(obj: dict[str, Any]) -> str | None:
    meta = obj.get("ResponseMetadata") or {}
    if isinstance(meta, dict) and meta.get("Error"):
        return str(meta.get("Error"))
    for key in ("error", "Error"):
        value = obj.get(key)
        if isinstance(value, dict):
            message = value.get("message") or value.get("Message") or value.get("msg")
            code = value.get("code") or value.get("Code") or value.get("type")
            if message and code:
                return f"{code}: {message}"
            if message:
                return str(message)
            if code:
                return str(code)
        elif value:
            return str(value)
    message = obj.get("message") or obj.get("Message") or obj.get("msg")
    if message and obj.get("code"):
        return f"{obj.get('code')}: {message}"
    return str(message) if message and not obj.get("choices") else None


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------

class TongjiPlugin(AbstractPlugin):
    """同济大学 HiAgent MaaS 平台插件。"""

    type_name = "tongji"

    def __init__(self) -> None:
        super().__init__()
        # 按 context id 缓存账户配置（workspace_id 等），在 apply_auth 时写入
        self._context_config: dict[int, dict[str, str]] = {}
        # 动态刷新的模型目录（从 ListModelByPublic 获取），初始为空（回退到静态目录）
        self._model_catalog: dict[str, dict[str, str]] = {}

    # ---- 模型目录 -----------------------------------------------------------

    def _catalog(self) -> dict[str, dict[str, str]]:
        """返回当前有效的模型目录（动态优先，回退到静态）。"""
        return self._model_catalog if self._model_catalog else _STATIC_CATALOG

    def _resolve_model_key(self, model_name: str) -> str:
        """将请求模型名解析为 OpenAI 兼容模型 ID，找不到时返回默认模型 ID。"""
        key = (model_name or "").lower().strip()
        catalog = self._catalog()
        if key in catalog:
            return key
        # 前缀/包含匹配（容错）
        for k in catalog:
            if key and (key in k or k in key):
                logger.debug("[tongji] 模型名模糊匹配 %r → %r", key, k)
                return k
        logger.debug("[tongji] 模型名 %r 未找到，使用默认 %s", key, _DEFAULT_MODEL_KEY)
        return _DEFAULT_MODEL_KEY

    def _resolve_model(self, model_name: str) -> dict[str, str]:
        """将 OpenAI 模型名解析为平台模型信息，找不到时返回默认模型。"""
        key = self._resolve_model_key(model_name)
        catalog = self._catalog()
        return catalog.get(key) or _STATIC_CATALOG[_DEFAULT_MODEL_KEY]

    def resolve_usage_model(self, model_name: str) -> str:
        """用量统计记录实际使用的 OpenAI 兼容模型 ID。"""
        return self._resolve_model_key(model_name)

    def model_mapping(self) -> dict[str, str] | None:
        """返回 OpenAI 兼容模型名 → 平台模型显示名的映射，用于 /v1/models 列表。"""
        return {k: v["name"] for k, v in self._catalog().items()}

    async def _refresh_model_catalog(self, page: Page) -> None:
        """调用 ListModelByPublic 刷新模型目录（仅保留 text-generation 类型）。"""
        url = f"{_API_BASE}?Action=ListModelByPublic&{_API_VER}"
        body = json.dumps({"PageNum": 1, "PageSize": 100})
        try:
            resp = await request_json_via_page_fetch(
                page, url, method="POST", body=body,
                headers={"Content-Type": "application/json"},
                timeout_ms=15000,
            )
        except Exception as e:
            logger.warning("[tongji] ListModelByPublic 请求失败: %s", e)
            return

        items = ((resp.get("json") or {}).get("Result") or {}).get("Items") or []
        catalog: dict[str, dict[str, str]] = {}
        for item in items:
            if item.get("Type") != "text-generation":
                continue
            if not item.get("IsPublished") or item.get("Status") != "Running":
                continue
            name: str = item.get("Name") or ""
            key_field: str = item.get("Key") or ""
            service_id: str = item.get("ID") or ""
            if not (name and service_id):
                continue
            openai_key = name.lower().replace("_", "-").replace(" ", "-")
            catalog[openai_key] = {"service_id": service_id, "key": key_field, "name": name}

        if catalog:
            self._model_catalog = catalog
            logger.info("[tongji] 模型目录已刷新，共 %d 个文本生成模型: %s",
                        len(catalog), ", ".join(catalog.keys()))

    # ---- create_page --------------------------------------------------------

    async def create_page(
        self,
        context: BrowserContext,
        reuse_page: Page | None = None,
    ) -> Page:
        page = reuse_page if reuse_page is not None else await context.new_page()
        try:
            await page.goto(
                f"{_BASE_URL}/product/maas/personal/{_DEFAULT_WORKSPACE_ID}/experience",
                wait_until="domcontentloaded",
                timeout=30000,
            )
        except Exception as e:
            logger.warning("[tongji] create_page goto 异常: %s", e)
        return page

    # ---- apply_auth ---------------------------------------------------------

    async def apply_auth(
        self,
        context: BrowserContext,
        page: Page,
        auth: dict[str, Any],
        *,
        reload: bool = True,
        **kwargs: Any,
    ) -> None:
        """
        检查是否已登录；未登录时通过 IAM SSO 完成账密认证，然后刷新模型目录。
        auth 须包含 username + password。
        """
        username = str(auth.get("username") or "").strip()
        password = str(auth.get("password") or "").strip()
        if not username or not password:
            raise ValueError("[tongji] auth 须包含 username 和 password")

        workspace_id = str(auth.get("workspace_id") or _DEFAULT_WORKSPACE_ID)

        if not await self._is_logged_in(page):
            logger.info("[tongji] 开始 SSO 登录，username=%s", username)
            # 清除旧 cookie（过期 cookie 会导致 SSO 服务端渲染非密码表单页）
            try:
                await context.clear_cookies()
            except Exception as e:
                logger.debug("[tongji] clear_cookies 异常（忽略）: %s", e)

            # 跳转到登录页；SSO 可能自动 redirect 到 iam.tongji.edu.cn，
            # 也可能因已有 session 直接跳到 app 页
            try:
                await page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                logger.warning("[tongji] 跳转登录页异常（继续等待表单）: %s", e)

            try:
                if "iam.tongji.edu.cn" not in page.url and not await self._is_logged_in(page):
                    await page.wait_for_url(
                        lambda url: "iam.tongji.edu.cn" in url,
                        timeout=5000,
                    )
            except Exception:
                pass

            # 等待跳转到：IAM SSO 页 或 app 主页（任意一个出现则停止等待）
            def _landed(url: str) -> bool:
                return "iam.tongji.edu.cn" in url or (
                    _BASE_URL in url and "/login" not in url
                )

            if not _landed(page.url):
                try:
                    await page.wait_for_url(_landed, timeout=30000)
                except Exception:
                    logger.warning("[tongji] SSO 跳转超时，当前 URL: %s", page.url)

            async def _submit_iam_form() -> None:
                try:
                    await page.wait_for_selector(
                        'input[name="j_username"]', state="visible", timeout=30000
                    )
                except Exception:
                    logger.warning("[tongji] 登录表单未出现，当前 URL: %s", page.url)
                    raise RuntimeError("SSO 登录页未能加载表单，请稍后重试")

                await page.locator('input[name="j_username"]').first.fill(username)
                await page.locator('input[name="j_password"]').first.fill(password)
                await page.locator('button:has-text("登录")').first.click()

                try:
                    await page.wait_for_url(f"{_BASE_URL}/**", timeout=30000)
                except Exception:
                    logger.warning("[tongji] SSO 回调超时，当前 URL: %s", page.url)
                    raise RuntimeError("SSO 登录失败，请检查账号密码")

                logger.info("[tongji] SSO 账密登录成功，URL: %s", page.url)

            if _BASE_URL in page.url and "/login" not in page.url and await self._is_logged_in(page):
                # 已有有效 session，直接跳到了 app 页
                logger.info("[tongji] cookie 有效，已自动跳转到 %s，跳过重新登录", page.url)
            elif "iam.tongji.edu.cn" in page.url:
                # 需要填账密
                await _submit_iam_form()
            else:
                logger.warning("[tongji] 未知登录状态，当前 URL: %s", page.url)
                raise RuntimeError("SSO 登录失败，无法确认登录状态")

            await page.goto(
                f"{_BASE_URL}/product/maas/personal/{workspace_id}/experience",
                wait_until="networkidle",
                timeout=30000,
            )
            logger.info("[tongji] 登录流程完成，最终 URL: %s", page.url)
        else:
            logger.info("[tongji] 已登录，跳过重新认证")

        if not await self._is_logged_in(page):
            raise RuntimeError(f"SSO 登录后仍未确认登录状态，当前 URL: {page.url}")

        self._context_config[id(context)] = {"workspace_id": workspace_id}

        # 刷新模型目录（所有账号共享同一公开目录）
        await self._refresh_model_catalog(page)

    # ---- create_conversation ------------------------------------------------

    async def create_conversation(
        self,
        context: BrowserContext,
        page: Page,
        **kwargs: Any,
    ) -> str | None:
        cfg = self._context_config.get(id(context)) or {}
        workspace_id = cfg.get("workspace_id") or _DEFAULT_WORKSPACE_ID

        # 从 kwargs 中读取请求指定的模型名（由 chat_handler 传入）
        model_info = self._resolve_model(kwargs.get("model") or _DEFAULT_MODEL_KEY)
        model_service_id = model_info["service_id"]
        model_key = model_info["key"]
        model_name = model_info["name"]
        logger.info("[tongji] create_conversation model=%s service_id=%s", model_name, model_service_id)

        url = f"{_API_BASE}?Action=CreateConversation&{_API_VER}"
        body = json.dumps({
            "ConversationName": "web2api",
            "WorkspaceID": workspace_id,
            "Models": [{
                "ModelServiceID": model_service_id,
                "ModelName": model_key,
                "DisplayName": model_name,
                "PublishSourceType": "external",
                "Source": "custom",
            }],
        })
        resp = await request_json_via_page_fetch(
            page, url, method="POST", body=body,
            headers={"Content-Type": "application/json"},
            timeout_ms=20000,
        )
        data = resp.get("json") or {}
        meta = data.get("ResponseMetadata") or {}
        if meta.get("Error"):
            logger.warning("[tongji] CreateConversation 失败: %s", meta["Error"])
            return None

        conv_info = (data.get("Result") or {}).get("ConversationInfo") or {}
        conv_id = conv_info.get("ConversationID")
        projects = conv_info.get("ProjectList") or []
        session_id = projects[0].get("SessionID", "") if projects else ""

        if not conv_id or not session_id:
            logger.warning("[tongji] CreateConversation 返回数据缺失: %s", data)
            return None

        self._session_state[conv_id] = {
            "session_id": session_id,
            "workspace_id": workspace_id,
        }
        logger.info("[tongji] 会话已创建 conv_id=%s session_id=%s model=%s",
                    conv_id, session_id, model_name)
        return conv_id

    # ---- stream_completion --------------------------------------------------

    async def _upload_attachment(
        self,
        context: BrowserContext,
        page: Page,
        attachment: InputAttachment,
        workspace_id: str,
    ) -> dict[str, Any] | None:
        """
        上传单个附件到平台，返回 Files 数组元素，失败返回 None。

        策略：先用浏览器 fetch 做 UploadRawCheck（需要 cookie），
        若文件不存在，则通过 BrowserContext.request 上传原始字节。
        页面内 fetch 会被站点前端链路拦截，二进制 body 会被序列化成 "{}"。
        """
        sha256 = hashlib.sha256(attachment.data).hexdigest()

        # 1. 检查是否已存在（通过浏览器 fetch，带 credentials）
        try:
            check_result = await page.evaluate(_UPLOAD_CHECK_JS, {"sha256": sha256})
        except Exception as e:
            logger.warning("[tongji] UploadRawCheck 异常: %s", e)
            return None

        logger.info("[tongji] UploadRawCheck sha256=%s result=%s", sha256, str(check_result)[:200])
        existing_path: str | None = None
        path: str
        size = len(attachment.data)
        if (check_result.get("Result") or {}).get("Exist"):
            existing_path = check_result["Result"].get("Path") or (
                f"upload/full/{sha256[0:2]}/{sha256[2:4]}/{sha256[4:]}"
            )
            logger.info("[tongji] 图片已存在 path=%s", existing_path)
            path = existing_path
            size = int((check_result.get("Result") or {}).get("Size") or size)
        else:
            # 2. 通过 Playwright context.request 上传原始二进制。
            #    该 request context 共享浏览器登录态，但不经过页面 JS / Service Worker。
            try:
                csrf_token = str(await page.evaluate(_CSRF_TOKEN_JS) or "")
                upload_url = (
                    f"{_UPLOAD_BASE}?Action=UploadRaw&{_UPLOAD_VER}"
                    f"&Id={sha256}&Expire=720h"
                )
                response = await context.request.fetch(
                    upload_url,
                    method="POST",
                    data=attachment.data,
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Accept-Language": "zh",
                        "Content-Type": attachment.mime_type
                        or "application/octet-stream",
                        "WorkspaceID": workspace_id,
                        "X-CSRF-Token": csrf_token,
                        "X-Top-Region": "cn-north-1",
                        "X-Up-Sha256": sha256,
                        "Origin": _BASE_URL,
                        "Referer": page.url or f"{_BASE_URL}/",
                    },
                    timeout=60000,
                    fail_on_status_code=False,
                )
                body = await response.text()
                if not response.ok:
                    raise RuntimeError(f"HTTP {response.status}: {body[:300]}")
                try:
                    result_json = json.loads(body)
                except json.JSONDecodeError:
                    result_json = {}
                result = result_json.get("Result") or {}
                path = result.get("Path") or (
                    f"upload/full/{sha256[0:2]}/{sha256[2:4]}/{sha256[4:]}"
                )
                size = int(result.get("Size") or len(attachment.data))
                logger.info(
                    "[tongji] 文件上传成功 sha256=%s path=%s bytes=%d resp=%s",
                    sha256,
                    path,
                    size,
                    body[:100],
                )
            except Exception as e:
                logger.warning("[tongji] 文件上传失败 sha256=%s: %s", sha256, e)
                return None

        encoded_path = urllib.parse.quote(path, safe="")
        download_url = (
            f"{_BASE_URL}/api/proxy/down"
            f"?Action=Download&Version=2022-01-01&IsAnonymous=true"
            f"&Path={encoded_path}"
        )
        return {
            "Path": path,
            "Name": attachment.filename,
            "Size": size,
            "Url": download_url,
        }

    async def stream_completion(
        self,
        context: BrowserContext,
        page: Page,
        session_id: str,
        message: str,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        state = self._session_state.get(session_id)
        if not state:
            raise RuntimeError(f"[tongji] 未知会话 ID: {session_id}")

        sess_id: str = state["session_id"]
        workspace_id: str = state["workspace_id"]
        request_id: str = kwargs.get("request_id") or session_id[:16]

        # 处理附件：文本/代码文件嵌入消息体；图片/PDF/Office 等二进制文件走平台附件上传。
        attachments: list[InputAttachment] = list(kwargs.get("attachments") or [])
        logger.info("[tongji] 收到附件 count=%d", len(attachments))
        uploaded_files: list[dict[str, Any]] = []
        inline_text_parts: list[str] = []
        for att in attachments:
            if not _should_inline_attachment(att):
                logger.info(
                    "[tongji] 准备上传附件 filename=%s mime=%s bytes=%d",
                    att.filename,
                    att.mime_type,
                    len(att.data),
                )
                file_info = await self._upload_attachment(context, page, att, workspace_id)
                if file_info:
                    uploaded_files.append(file_info)
                else:
                    logger.warning(
                        "[tongji] 附件上传未返回文件信息 filename=%s mime=%s bytes=%d",
                        att.filename,
                        att.mime_type,
                        len(att.data),
                    )
            else:
                # 文本/代码文件：解码后嵌入消息，作为 markdown 代码块。
                try:
                    text_content = att.data.decode("utf-8", errors="replace")
                except Exception:
                    text_content = att.data.decode("latin-1", errors="replace")
                ext = att.filename.rsplit(".", 1)[-1] if "." in att.filename else ""
                inline_text_parts.append(
                    f"```{ext}\n# {att.filename}\n{text_content}\n```"
                )
                logger.info("[tongji] 文件 %s 以文本方式嵌入消息", att.filename)

        # 将嵌入文件追加到消息末尾
        final_message = message
        if inline_text_parts:
            final_message = message + "\n\n" + "\n\n".join(inline_text_parts)

        # Step 1: 创建用户消息，获取 MessageID
        batch_url = f"{_API_BASE}?Action=BatchCreateMessages&{_API_VER}"
        msg_entry: dict[str, Any] = {
            "SessionID": sess_id,
            "ContentType": "text",
            "Content": final_message,
        }
        if uploaded_files:
            msg_entry["ExtendsInfo"] = {"Files": uploaded_files}
            logger.info("[tongji] 消息附带上传文件 count=%d", len(uploaded_files))
        elif attachments:
            logger.warning("[tongji] 本轮有附件但没有可附带的上传文件")
        batch_body = json.dumps({
            "ConversationID": session_id,
            "MessageList": [msg_entry],
            "WorkspaceID": workspace_id,
        })
        resp = await request_json_via_page_fetch(
            page, batch_url, method="POST", body=batch_body,
            headers={"Content-Type": "application/json"},
            timeout_ms=20000,
        )
        data = resp.get("json") or {}
        msg_list = (data.get("Result") or {}).get("MessageList") or []
        if not msg_list:
            raise RuntimeError(f"[tongji] BatchCreateMessages 失败: {str(data)[:300]}")
        msg_id: str = msg_list[0]["MessageID"]
        logger.info("[tongji] 消息已创建 msg_id=%s", msg_id)

        # Step 2: 发起 Chat 流式请求
        chat_url = f"{_BYPASS_BASE}?Action=Chat&{_API_VER}"
        chat_body = json.dumps({
            "SessionID": sess_id,
            "MessageID": msg_id,
            "WorkspaceID": workspace_id,
        })

        def _on_http_error(msg: str, headers: dict | None) -> int | None:
            logger.warning("[tongji] Chat HTTP error: %s", msg[:300])
            return None

        buffer = ""
        async for chunk in stream_raw_via_page_fetch(
            context, page, chat_url, chat_body, request_id,
            on_http_error=_on_http_error,
        ):
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.rstrip("\r")
                texts, finish, error = _parse_tongji_sse_chunk(line)
                if error:
                    logger.warning("[tongji] Chat SSE error: %s", error[:300])
                    raise RuntimeError(f"[tongji] Chat SSE error: {error[:300]}")
                for t in texts:
                    yield t
                if finish == "done":
                    return

    # ---- 辅助方法 -----------------------------------------------------------

    async def _is_logged_in(self, page: Page) -> bool:
        """通过 /api/auth/checkLogin 判断当前是否已登录。

        同济 MaaS 有时已经进入 experience 页面，但 checkLogin 返回的用户字段
        不是固定的 UserInfo/userInfo 形态。只要接口成功且没有明确错误，就把
        MaaS 业务页视为可用登录态，避免误判导致后续 API 完全不可调用。
        """
        try:
            resp = await request_json_via_page_fetch(
                page,
                f"{_BASE_URL}/api/auth/checkLogin",
                timeout_ms=8000,
            )
            data = resp.get("json") or {}
            result = data.get("Result") or {}
            if result.get("UserInfo") or result.get("userInfo"):
                return True
            meta = data.get("ResponseMetadata") or {}
            if meta.get("Error"):
                logger.debug("[tongji] checkLogin 返回错误: %s", meta.get("Error"))
                return False
            if bool(data.get("Error") or data.get("error")):
                logger.debug("[tongji] checkLogin 返回错误: %s", data)
                return False
            if (
                resp.get("ok")
                and _BASE_URL in (page.url or "")
                and "/login" not in (page.url or "")
                and "iam.tongji.edu.cn" not in (page.url or "")
            ):
                logger.debug("[tongji] checkLogin 未返回用户字段，但当前 MaaS 页面可用，视为已登录: %s", page.url)
                return True
            logger.debug("[tongji] checkLogin 未确认登录: %s", str(data)[:300])
            return False
        except Exception as e:
            logger.debug("[tongji] checkLogin 异常: %s", e)
            return False


# ---------------------------------------------------------------------------
# 注册入口
# ---------------------------------------------------------------------------

def register_tongji_plugin() -> None:
    """注册同济 MaaS 插件到全局 Registry。"""
    PluginRegistry.register(TongjiPlugin())
