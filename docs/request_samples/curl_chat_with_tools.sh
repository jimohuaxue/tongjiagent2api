#!/bin/bash
# 带 tools 的流式 chat 请求，用于测试 has_tools 时 mock 回复是否正常返回
# 服务默认：main.py -> 127.0.0.1:8001

curl -s -N -X POST 'http://127.0.0.1:8001/claude/v1/chat/completions' \
  -H 'Content-Type: application/json' \
  -d '{
  "model": "claude-sonnet-4-5-20250929",
  "messages": [
    { "role": "user", "content": "你好" }
  ],
  "stream": true,
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "read_file",
        "description": "Read contents of a file",
        "parameters": {
          "type": "object",
          "properties": {
            "path": { "type": "string", "description": "File path to read" }
          },
          "required": ["path"]
        }
      }
    }
  ]
}'
