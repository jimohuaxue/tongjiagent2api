"""
测试图片识别：通过 web2api 反代，分别用 URL 直传和 base64 上传两种方式测试图片识别。
运行前确保服务已启动：python main.py 或 uvicorn core.app:app --port 9000
"""

import base64
import json
import sys
import urllib.error
import urllib.request

# ── 配置 ────────────────────────────────────────────────────────────────────
API_BASE = "http://127.0.0.1:9000"
API_KEY = "tongji-api-key"
PROVIDER = "tongji"         # 改为 "claude" 可切换到 Claude 提供者
MODEL = "qwen3-vl-235b"     # tongji 视觉模型；claude 提供者改为 "s4"

IMAGE_URL = "https://i2.hdslb.com/bfs/archive/1e8327d23d7017c013e30b95cc3f6c1960302c66.jpg"
QUESTION = "请详细描述这张图片里有什么内容？"

CHAT_URL = f"{API_BASE}/openai/{PROVIDER}/v1/chat/completions"

_ZERO_WIDTH = {0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0xFEFF, 0x180E,
               0x2060, 0x2061, 0x2062, 0x2063, 0x2064, 0xFFFC}


def strip_invisible(text: str) -> str:
    return "".join(
        c for c in text
        if (ord(c) >= 32 or c in "\n\r\t") and ord(c) not in _ZERO_WIDTH
    )


def do_request(payload: dict) -> str:
    req = urllib.request.Request(
        CHAT_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = resp.read().decode()
        result = json.loads(body)
        content = result["choices"][0]["message"]["content"]
        return strip_invisible(content)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"HTTP {e.code}: {body}")


def download_as_base64(url: str) -> tuple[str, str]:
    """下载图片，返回 (base64字符串, mime_type)。"""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
        ct = str(resp.headers.get_content_type() or "image/jpeg").lower()
    return base64.b64encode(data).decode(), ct


# ── 测试 1：URL 直传 ─────────────────────────────────────────────────────────
def test_url():
    print("=" * 60)
    print("[测试1] URL 直传图片")
    print(f"  URL  : {IMAGE_URL}")
    print(f"  模型 : {MODEL}  提供者: {PROVIDER}")
    payload = {
        "model": MODEL,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": QUESTION},
                    {"type": "image_url", "image_url": {"url": IMAGE_URL}},
                ],
            }
        ],
    }
    try:
        reply = do_request(payload)
        print("\n=== 模型回复 ===")
        print(reply)
        print("[测试1] 通过 ✓")
    except Exception as e:
        print(f"[测试1] 失败: {e}", file=sys.stderr)


# ── 测试 2：base64 上传 ──────────────────────────────────────────────────────
def test_base64():
    print()
    print("=" * 60)
    print("[测试2] 下载图片后转 base64 上传")
    print(f"  URL  : {IMAGE_URL}")
    print(f"  模型 : {MODEL}  提供者: {PROVIDER}")

    print("  正在下载图片…", end="", flush=True)
    try:
        b64, mime = download_as_base64(IMAGE_URL)
        print(f" {len(b64)//1024} KB  mime={mime}")
    except Exception as e:
        print(f"\n[测试2] 下载失败: {e}", file=sys.stderr)
        return

    data_url = f"data:{mime};base64,{b64}"
    payload = {
        "model": MODEL,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": QUESTION},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    try:
        reply = do_request(payload)
        print("\n=== 模型回复 ===")
        print(reply)
        print("[测试2] 通过 ✓")
    except Exception as e:
        print(f"[测试2] 失败: {e}", file=sys.stderr)


if __name__ == "__main__":
    print(f"目标: {CHAT_URL}")
    test_url()
    test_base64()
