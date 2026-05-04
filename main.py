"""
架构入口：启动 FastAPI 服务，baseUrl 为 http://ip:port/{type}/v1/...
示例：http://127.0.0.1:8000/claude/v1/chat/completions
"""

# 尽早设置，让 Chromium 派生的 Node 子进程继承，抑制 url.parse 等 DeprecationWarning
import os
import logging
import sys
import uvicorn

from core.config.settings import get, load_config

load_config()

_opt = os.environ.get("NODE_OPTIONS", "").strip()
if "--no-deprecation" not in _opt:
    os.environ["NODE_OPTIONS"] = (_opt + " --no-deprecation").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


def main() -> int:
    host = str(get("server", "host") or "127.0.0.1").strip() or "127.0.0.1"
    port = int(get("server", "port") or 9000)
    uvicorn.run(
        "core.app:app",
        host=host,
        port=port,
        reload=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
