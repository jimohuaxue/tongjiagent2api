"""全局常量：浏览器路径、CDP 端口等（新架构专用）。"""

import os
import platform
import shutil
import glob
from pathlib import Path

CHROMIUM_BIN_ENV_KEY = "WEB2API_CHROMIUM_BIN"
MACOS_CHROMIUM_BIN = "/Applications/Chromium.app/Contents/MacOS/Chromium"
MACOS_GOOGLE_CHROME_BIN = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)
LINUX_FINGERPRINT_CHROMIUM_BIN = "/opt/fingerprint-chromium/chrome"
LINUX_CHROMIUM_BIN_CANDIDATES = (
    LINUX_FINGERPRINT_CHROMIUM_BIN,
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/snap/bin/chromium",
)
CHROMIUM_BIN_PATH_NAMES = (
    "fingerprint-chromium",
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
)
REMOTE_DEBUGGING_PORT = 9223  # 默认端口，单浏览器兼容
# 多浏览器并存时的端口池（按 ProxyKey 各占一端口，仅当 refcount=0 时关闭并回收端口）
CDP_PORT_RANGE = list(range(9223, 9243))  # 9223..9232，最多 20 个并发浏览器
CDP_ENDPOINT = "http://127.0.0.1:9223"
TIMEZONE = "America/Chicago"
USER_DATA_DIR_PREFIX = "fp-data"  # user_data_dir = home / fp-data / fingerprint_id


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def chromium_bin_candidates() -> list[str]:
    """返回当前平台可尝试的 Chromium 可执行文件路径。"""
    system = platform.system()
    candidates: list[str] = []
    if system == "Darwin":
        candidates.extend([MACOS_CHROMIUM_BIN, MACOS_GOOGLE_CHROME_BIN])
    elif system == "Linux":
        candidates.extend(LINUX_CHROMIUM_BIN_CANDIDATES)
    elif system == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("PROGRAMFILES", "")
        program_files_x86 = os.environ.get("PROGRAMFILES(X86)", "")
        for root in (local_app_data, program_files, program_files_x86):
            if not root:
                continue
            candidates.extend(
                [
                    str(Path(root) / "Chromium" / "Application" / "chrome.exe"),
                    str(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"),
                    str(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
                ]
            )
    candidates.extend(
        [
            LINUX_FINGERPRINT_CHROMIUM_BIN,
            *LINUX_CHROMIUM_BIN_CANDIDATES,
            MACOS_CHROMIUM_BIN,
            MACOS_GOOGLE_CHROME_BIN,
        ]
    )
    candidates.extend(
        sorted(
            glob.glob(
                str(
                    Path.home()
                    / ".cache"
                    / "ms-playwright"
                    / "chromium-*"
                    / "chrome-linux*"
                    / "chrome"
                )
            ),
            reverse=True,
        )
    )
    return _dedupe(candidates)


def is_chromium_executable(path: str | Path) -> bool:
    """检查路径是否存在且可执行。"""
    if isinstance(path, str) and path and not any(sep in path for sep in ("/", "\\")):
        return shutil.which(path) is not None
    candidate = Path(path).expanduser()
    return candidate.is_file() and os.access(candidate, os.X_OK)


def resolve_chromium_bin(configured: str | None = None) -> str:
    """解析 Chromium 路径。

    优先级：
    1. 配置文件显式填写的 browser.chromium_bin
    2. WEB2API_CHROMIUM_BIN 环境变量
    3. 当前系统常见安装路径
    4. PATH 中的常见命令名

    若未找到真实可执行文件，返回当前平台最可能的路径，后续启动时会给出明确错误。
    """
    configured = (configured or "").strip()
    if configured:
        if not any(sep in configured for sep in ("/", "\\")):
            found = shutil.which(configured)
            if found:
                return found
        return str(Path(configured).expanduser())

    env_value = os.environ.get(CHROMIUM_BIN_ENV_KEY, "").strip()
    if env_value:
        if not any(sep in env_value for sep in ("/", "\\")):
            found = shutil.which(env_value)
            if found:
                return found
        return str(Path(env_value).expanduser())

    for candidate in chromium_bin_candidates():
        if is_chromium_executable(candidate):
            return str(Path(candidate).expanduser())

    for name in CHROMIUM_BIN_PATH_NAMES:
        found = shutil.which(name)
        if found:
            return found

    candidates = chromium_bin_candidates()
    if candidates:
        return candidates[0]
    return "chromium"


def chromium_bin_missing_message(chromium_bin: str) -> str:
    """生成可操作的 Chromium 缺失错误。"""
    candidates = ", ".join(chromium_bin_candidates())
    command_names = ", ".join(CHROMIUM_BIN_PATH_NAMES)
    return (
        f"Chromium 不存在或不可执行: {chromium_bin}. "
        "请安装 Chromium/fingerprint-chromium，或在 config.yaml 的 "
        f"browser.chromium_bin（也可用环境变量 {CHROMIUM_BIN_ENV_KEY}）指定可执行文件路径。"
        f"自动查找路径: {candidates}; PATH 命令名: {command_names}"
    )


# 兼容旧代码直接引用 CHROMIUM_BIN；运行时新代码会调用 resolve_chromium_bin。
CHROMIUM_BIN = resolve_chromium_bin()


def user_data_dir(fingerprint_id: str) -> Path:
    """按指纹 ID 拼接 user-data-dir，不依赖 profile_id。"""
    return Path.home() / USER_DATA_DIR_PREFIX / fingerprint_id
