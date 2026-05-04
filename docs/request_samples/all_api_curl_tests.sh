#!/usr/bin/env bash

# 用法：复制任意一条 curl 命令单独执行即可
# 说明：
# - 覆盖当前项目里已暴露的全部 HTTP 接口
# - PUT /api/config 有副作用，默认注释
# - 流式接口使用 curl -N，便于直接观察 SSE 输出

# =============================================================
# 1. OpenAI 协议: GET /openai/{provider}/v1/models
# =============================================================
curl -sS -H "Authorization: Bearer tongji-api-key" "http://127.0.0.1:9000/openai/claude/v1/models"

# =============================================================
# 2. OpenAI 协议: POST /openai/{provider}/v1/chat/completions 非流式
# =============================================================
curl -sS -H "Authorization: Bearer tongji-api-key" -H "Content-Type: application/json" "http://127.0.0.1:9000/openai/claude/v1/chat/completions" -d '{"model": "s4", "stream": false, "messages": [{"role": "user", "content": "你好，请用一句话介绍一下你自己。"}]}'

# =============================================================
# 3. OpenAI 协议: POST /openai/{provider}/v1/chat/completions 流式
# =============================================================
curl -N -sS -H "Authorization: Bearer tongji-api-key" -H "Content-Type: application/json" "http://127.0.0.1:9000/openai/claude/v1/chat/completions" -d '{"model": "s4", "stream": true, "messages": [{"role": "user", "content": "请流式输出三条学习建议。"}]}'

# =============================================================
# 4. 旧兼容路径: GET /{provider}/v1/models
# =============================================================
curl -sS -H "Authorization: Bearer tongji-api-key" "http://127.0.0.1:9000/claude/v1/models"

# =============================================================
# 5. 旧兼容路径: POST /{provider}/v1/chat/completions 非流式
# =============================================================
curl -sS -H "Authorization: Bearer tongji-api-key" -H "Content-Type: application/json" "http://127.0.0.1:9000/claude/v1/chat/completions" -d '{"model": "s4", "stream": false, "messages": [{"role": "user", "content": "这条请求走的是旧兼容路径，请回复 ok。"}]}'

# =============================================================
# 6. 旧兼容路径: POST /{provider}/v1/chat/completions 流式
# =============================================================
curl -N -sS -H "Authorization: Bearer tongji-api-key" -H "Content-Type: application/json" "http://127.0.0.1:9000/claude/v1/chat/completions" -d '{"model": "s4", "stream": true, "messages": [{"role": "user", "content": "这条请求走的是旧兼容路径，请流式输出 ok。"}]}'

# =============================================================
# 7. Anthropic 协议: POST /anthropic/{provider}/v1/messages 非流式
# =============================================================
curl -sS -H "Authorization: Bearer tongji-api-key" -H "Content-Type: application/json" "http://127.0.0.1:9000/anthropic/claude/v1/messages" -d '{"model": "claude-sonnet-4-5-20250929", "max_tokens": 512, "stream": false, "messages": [{"role": "user", "content": [{"type": "text", "text": "你好，请用一句话介绍一下你自己。"}]}]}'

# =============================================================
# 8. Anthropic 协议: POST /anthropic/{provider}/v1/messages 流式
# =============================================================
curl -N -sS -H "Authorization: Bearer tongji-api-key" -H "Content-Type: application/json" "http://127.0.0.1:9000/anthropic/claude/v1/messages" -d '{"model": "claude-sonnet-4-5-20250929", "max_tokens": 512, "stream": true, "messages": [{"role": "user", "content": [{"type": "text", "text": "请流式输出三条学习建议。"}]}]}'

# =============================================================
# 9. OpenAI 工具调用: POST /openai/{provider}/v1/chat/completions 非流式
# =============================================================
curl -sS -H "Authorization: Bearer tongji-api-key" -H "Content-Type: application/json" "http://127.0.0.1:9000/openai/claude/v1/chat/completions" -d '{"model": "s4", "stream": false, "messages": [{"role": "user", "content": "北京现在天气怎么样？"}], "tools": [{"type": "function", "function": {"name": "get_weather", "description": "获取指定城市的当前天气信息。", "parameters": {"type": "object", "properties": {"location": {"type": "string", "description": "城市名称"}, "units": {"type": "string", "enum": ["celsius", "fahrenheit"], "description": "温度单位"}}, "required": ["location"]}}}], "tool_choice": "auto"}'

# =============================================================
# 10. OpenAI 工具调用: POST /openai/{provider}/v1/chat/completions 流式
# =============================================================
curl -N -sS -H "Authorization: Bearer tongji-api-key" -H "Content-Type: application/json" "http://127.0.0.1:9000/openai/claude/v1/chat/completions" -d '{"model": "s4", "stream": true, "messages": [{"role": "user", "content": "北京现在天气怎么样？"}], "tools": [{"type": "function", "function": {"name": "get_weather", "description": "获取指定城市的当前天气信息。", "parameters": {"type": "object", "properties": {"location": {"type": "string", "description": "城市名称"}, "units": {"type": "string", "enum": ["celsius", "fahrenheit"], "description": "温度单位"}}, "required": ["location"]}}}], "tool_choice": "auto"}'

# =============================================================
# 11. Anthropic 工具调用: POST /anthropic/{provider}/v1/messages 非流式
# =============================================================
curl -sS -H "Authorization: Bearer tongji-api-key" -H "Content-Type: application/json" "http://127.0.0.1:9000/anthropic/claude/v1/messages" -d '{"model": "claude-sonnet-4-5-20250929", "max_tokens": 512, "stream": false, "messages": [{"role": "user", "content": [{"type": "text", "text": "北京现在天气怎么样？"}]}], "tools": [{"name": "get_weather", "description": "获取指定城市的当前天气信息。", "input_schema": {"type": "object", "properties": {"location": {"type": "string", "description": "城市名称"}, "units": {"type": "string", "enum": ["celsius", "fahrenheit"], "description": "温度单位"}}, "required": ["location"]}}], "tool_choice": {"type": "auto"}}'

# =============================================================
# 12. Anthropic 工具调用: POST /anthropic/{provider}/v1/messages 流式
# =============================================================
curl -N -sS -H "Authorization: Bearer tongji-api-key" -H "Content-Type: application/json" "http://127.0.0.1:9000/anthropic/claude/v1/messages" -d '{"model": "claude-sonnet-4-5-20250929", "max_tokens": 512, "stream": true, "messages": [{"role": "user", "content": [{"type": "text", "text": "北京现在天气怎么样？"}]}], "tools": [{"name": "get_weather", "description": "获取指定城市的当前天气信息。", "input_schema": {"type": "object", "properties": {"location": {"type": "string", "description": "城市名称"}, "units": {"type": "string", "enum": ["celsius", "fahrenheit"], "description": "温度单位"}}, "required": ["location"]}}], "tool_choice": {"type": "auto"}}'

# =============================================================
# 13. OpenAI 图片输入示例（需替换 base64 为真实图片数据）
# =============================================================
# curl -sS -H "Authorization: Bearer tongji-api-key" -H "Content-Type: application/json" "http://127.0.0.1:9000/openai/claude/v1/chat/completions" -d '{"model": "s4", "stream": false, "messages": [{"role": "user", "content": [{"type": "text", "text": "请描述这张图片"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA..."}}]}]}'

# =============================================================
# 14. Anthropic 图片输入示例（需替换 base64 为真实图片数据）
# =============================================================
# curl -sS -H "Authorization: Bearer tongji-api-key" -H "Content-Type: application/json" "http://127.0.0.1:9000/anthropic/claude/v1/messages" -d '{"model": "claude-sonnet-4-5-20250929", "max_tokens": 512, "stream": false, "messages": [{"role": "user", "content": [{"type": "text", "text": "请描述这张图片"}, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgoAAAANSUhEUgAA..."}}]}]}'
