"""
Mock Claude API 独立入口：仅运行 mock 服务，不依赖配置/浏览器。
用于调试时单独启动，main 服务在 config.yaml 的 claude.start_url、claude.api_base 指向本服务即可。
默认端口 8002，避免与 main(8001) 冲突。
"""

import logging
import sys
import uvicorn

from core.api.mock_claude import router as mock_claude_router
from core.config.settings import get, load_config
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_config()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

app = FastAPI(
    title="Mock Claude API",
    description="调试用 mock，与 claude.py 调用格式兼容，不消耗 token。",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(mock_claude_router)


def main() -> int:
    port = int(get("mock", "port") or 8002)
    uvicorn.run(
        "main_mock:app",
        host="127.0.0.1",
        port=port,
        reload=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
