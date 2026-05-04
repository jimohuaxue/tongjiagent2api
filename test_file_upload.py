"""
测试代码文件上传：发一段 Python 代码给 tongji/v1/chat/completions，使用 minimax-m2.7。
运行前确保服务已启动：python -m uvicorn core.app:app --port 9000
"""

import base64
import json
import sys
import urllib.request

API_URL = "http://127.0.0.1:9000/openai/tongji/v1/chat/completions"
API_KEY = "tongji-api-key"

# 随便写一段有 bug 的 Python 代码
CODE = """\
def fibonacci(n):
    if n <= 0:
        return []
    elif n == 1:
        return [0]
    result = [0, 1]
    for i in range(2, n):
        result.append(result[i-1] + result[i-2])
    return result

# 有 bug：应该打印 fibonacci(10) 但写错了
print(fibonacci(0))
print(fibonacci(1))
print(fibonacci("ten"))   # 类型错误
"""

code_b64 = base64.b64encode(CODE.encode()).decode()
data_url = f"data:text/x-python;base64,{code_b64}"

payload = {
    "model": "glm-5.1",
    "stream": False,
    "messages": [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "请分析这段代码，找出其中的 bug 并给出修复建议。",
                },
                {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                },
            ],
        }
    ],
}

req = urllib.request.Request(
    API_URL,
    data=json.dumps(payload).encode(),
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    },
    method="POST",
)

print(f"发送请求到 {API_URL}")
print(f"模型: minimax-m2.7  文件大小: {len(CODE)} bytes\n")

try:
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read().decode()
    result = json.loads(body)
    content = result["choices"][0]["message"]["content"]
    # 去掉零宽字符 session marker（零宽空格/连接符/非连接符/BOM 等）
    _ZERO_WIDTH = {0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0xFEFF, 0x180E,
                   0x2060, 0x2061, 0x2062, 0x2063, 0x2064, 0xFFFC}
    visible = "".join(
        c for c in content
        if (ord(c) >= 32 or c in "\n\r\t") and ord(c) not in _ZERO_WIDTH
    )
    print("=== 模型回复 ===")
    print(visible)
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"HTTP {e.code}: {body}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)
