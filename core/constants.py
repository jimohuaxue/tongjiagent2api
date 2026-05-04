"""全局常量：浏览器路径、CDP 端口等（新架构专用）。"""

from pathlib import Path

# 与现有 multi_web2api 保持一致，便于同机运行时分端口
CHROMIUM_BIN = "/Applications/Chromium.app/Contents/MacOS/Chromium"
REMOTE_DEBUGGING_PORT = 9223  # 默认端口，单浏览器兼容
# 多浏览器并存时的端口池（按 ProxyKey 各占一端口，仅当 refcount=0 时关闭并回收端口）
CDP_PORT_RANGE = list(range(9223, 9243))  # 9223..9232，最多 20 个并发浏览器
CDP_ENDPOINT = "http://127.0.0.1:9223"
TIMEZONE = "America/Chicago"
USER_DATA_DIR_PREFIX = "fp-data"  # user_data_dir = home / fp-data / fingerprint_id


def user_data_dir(fingerprint_id: str) -> Path:
    """按指纹 ID 拼接 user-data-dir，不依赖 profile_id。"""
    return Path.home() / USER_DATA_DIR_PREFIX / fingerprint_id
