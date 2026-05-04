# OpenAI Chat Completions API 完整格式参考

> 覆盖所有场景：普通对话、Function Calling、Vision、流式输出、错误响应等

---

## 目录

1. [请求基础结构](#1-请求基础结构)
2. [消息格式（messages）](#2-消息格式messages)
3. [普通对话：请求 & 响应](#3-普通对话请求--响应)
4. [Function Calling：完整流程](#4-function-calling完整流程)
5. [Vision（图片输入）](#5-vision图片输入)
6. [流式输出（Streaming）](#6-流式输出streaming)
7. [结构化输出（JSON Mode）](#7-结构化输出json-mode)
8. [Logprobs（对数概率）](#8-logprobs对数概率)
9. [响应字段完整说明](#9-响应字段完整说明)
10. [错误响应格式](#10-错误响应格式)

---

## 1. 请求基础结构

**Endpoint:** `POST https://api.openai.com/v1/chat/completions`

**Headers:**

```
Authorization: Bearer <YOUR_API_KEY>
Content-Type: application/json
```

**完整请求字段一览：**

```json
{
  "model": "gpt-4.1",                    // 必填
  "messages": [...],                      // 必填

  // 采样参数
  "temperature": 1.0,                     // 0~2，默认1，越高越随机
  "top_p": 1.0,                           // 核采样，与temperature二选一调
  "max_tokens": 4096,                     // 最大输出token数
  "n": 1,                                 // 生成几个候选回复
  "stop": ["\n", "END"],                  // 遇到此字符串停止生成（string或array）
  "presence_penalty": 0.0,               // -2~2，惩罚已出现的token，鼓励新话题
  "frequency_penalty": 0.0,              // -2~2，惩罚高频token，减少重复
  "logit_bias": { "50256": -100 },        // 调整特定token的概率（token_id: bias）
  "seed": 42,                             // 固定随机种子（尽力复现）
  "user": "user-abc123",                  // 用户唯一标识（用于滥用监控）

  // 工具调用
  "tools": [...],                         // 定义可用工具列表
  "tool_choice": "auto",                  // 工具调用策略

  // 输出控制
  "stream": false,                        // 是否流式输出
  "logprobs": false,                      // 是否返回对数概率
  "top_logprobs": null,                   // 返回每个token的top N概率（1~20）
  "response_format": { "type": "text" },  // 输出格式

  // 其他
  "parallel_tool_calls": true,            // 是否允许并行调用多个工具
  "store": false                          // 是否存储对话（用于fine-tuning等）
}
```

---

## 2. 消息格式（messages）

messages 是一个数组，每条消息必须有 `role` 和 `content` 字段。

### 2.1 system 消息

```json
{
  "role": "system",
  "content": "你是一个专业的客服助手，回答要简洁、友好。"
}
```

### 2.2 user 消息（纯文本）

```json
{
  "role": "user",
  "content": "今天天气怎么样？"
}
```

### 2.3 user 消息（多模态，含图片）

```json
{
  "role": "user",
  "content": [
    {
      "type": "text",
      "text": "这张图片里有什么？"
    },
    {
      "type": "image_url",
      "image_url": {
        "url": "https://example.com/image.jpg",
        "detail": "auto" // "low" | "high" | "auto"
      }
    }
  ]
}
```

### 2.4 assistant 消息（普通回复）

```json
{
  "role": "assistant",
  "content": "今天天气晴朗，气温25度。"
}
```

### 2.5 assistant 消息（含 tool_calls，用于多轮对话回传）

```json
{
  "role": "assistant",
  "content": null,
  "tool_calls": [
    {
      "id": "call_abc123",
      "type": "function",
      "function": {
        "name": "get_weather",
        "arguments": "{\"location\": \"Beijing\", \"units\": \"celsius\"}"
      }
    }
  ]
}
```

### 2.6 tool 消息（函数执行结果回传）

```json
{
  "role": "tool",
  "tool_call_id": "call_abc123",
  "content": "{\"temperature\": 25, \"condition\": \"晴天\"}"
}
```

---

## 3. 普通对话：请求 & 响应

### 请求

```json
{
  "model": "gpt-4.1",
  "messages": [
    { "role": "system", "content": "你是一个有帮助的助手。" },
    { "role": "user", "content": "用一句话解释量子纠缠。" }
  ],
  "temperature": 0.7,
  "max_tokens": 200
}
```

### 响应

```json
{
  "id": "chatcmpl-A1B2C3D4E5F6G7H8",
  "object": "chat.completion",
  "created": 1716936000,
  "model": "gpt-4.1-2025-04-14",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "量子纠缠是指两个粒子无论相距多远，对其中一个的测量会瞬间影响另一个的状态。",
        "refusal": null
      },
      "logprobs": null,
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 35,
    "completion_tokens": 32,
    "total_tokens": 67,
    "prompt_tokens_details": {
      "cached_tokens": 0,
      "audio_tokens": 0
    },
    "completion_tokens_details": {
      "reasoning_tokens": 0,
      "audio_tokens": 0,
      "accepted_prediction_tokens": 0,
      "rejected_prediction_tokens": 0
    }
  },
  "system_fingerprint": "fp_a1b2c3d4e5"
}
```

**`finish_reason` 取值说明：**

| 值               | 含义                             |
| ---------------- | -------------------------------- |
| `stop`           | 正常结束（遇到停止词或自然结束） |
| `length`         | 达到 max_tokens 上限             |
| `tool_calls`     | 模型发起了工具调用               |
| `content_filter` | 内容被过滤                       |
| `null`           | 流式输出中间块（非最终块）       |

---

## 4. Function Calling：完整流程

### 步骤一：携带工具定义发起请求

```json
{
  "model": "gpt-4.1",
  "messages": [{ "role": "user", "content": "北京现在天气怎么样？" }],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "获取指定城市的当前天气信息。",
        "strict": true,
        "parameters": {
          "type": "object",
          "properties": {
            "location": {
              "type": "string",
              "description": "城市名称，如：Beijing, China"
            },
            "units": {
              "type": ["string", "null"],
              "enum": ["celsius", "fahrenheit"],
              "description": "温度单位，默认 celsius"
            }
          },
          "required": ["location", "units"],
          "additionalProperties": false
        }
      }
    }
  ],
  "tool_choice": "auto"
}
```

### 步骤一：模型响应（包含 tool_calls）

```json
{
  "id": "chatcmpl-A1B2C3D4E5F6G7H8",
  "object": "chat.completion",
  "created": 1716936000,
  "model": "gpt-4.1-2025-04-14",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call_abc123xyz",
            "type": "function",
            "function": {
              "name": "get_weather",
              "arguments": "{\"location\": \"Beijing, China\", \"units\": \"celsius\"}"
            }
          }
        ],
        "refusal": null
      },
      "logprobs": null,
      "finish_reason": "tool_calls"
    }
  ],
  "usage": {
    "prompt_tokens": 82,
    "completion_tokens": 23,
    "total_tokens": 105
  }
}
```

### 步骤二：执行函数后，将结果回传

```json
{
  "model": "gpt-4.1",
  "messages": [
    { "role": "user", "content": "北京现在天气怎么样？" },
    {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        {
          "id": "call_abc123xyz",
          "type": "function",
          "function": {
            "name": "get_weather",
            "arguments": "{\"location\": \"Beijing, China\", \"units\": \"celsius\"}"
          }
        }
      ]
    },
    {
      "role": "tool",
      "tool_call_id": "call_abc123xyz",
      "content": "{\"temperature\": 28, \"condition\": \"晴天\", \"humidity\": 45}"
    }
  ],
  "tools": [...]
}
```

### 步骤二：最终响应

```json
{
  "id": "chatcmpl-B2C3D4E5F6G7H8I9",
  "object": "chat.completion",
  "created": 1716936010,
  "model": "gpt-4.1-2025-04-14",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "北京现在天气晴朗，气温28°C，湿度45%，是个好天气！",
        "refusal": null
      },
      "logprobs": null,
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 130,
    "completion_tokens": 25,
    "total_tokens": 155
  }
}
```

### 并行工具调用（Parallel Tool Calls）

模型可以在一次响应中发起多个工具调用：

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call_001",
            "type": "function",
            "function": {
              "name": "get_weather",
              "arguments": "{\"location\": \"Beijing, China\", \"units\": \"celsius\"}"
            }
          },
          {
            "id": "call_002",
            "type": "function",
            "function": {
              "name": "get_weather",
              "arguments": "{\"location\": \"Shanghai, China\", \"units\": \"celsius\"}"
            }
          }
        ]
      },
      "finish_reason": "tool_calls"
    }
  ]
}
```

回传时需要为每个 `tool_call_id` 提供一条 tool 消息：

```json
{
  "messages": [
    ...,
    { "role": "tool", "tool_call_id": "call_001", "content": "{\"temperature\": 28}" },
    { "role": "tool", "tool_call_id": "call_002", "content": "{\"temperature\": 32}" }
  ]
}
```

### tool_choice 的所有取值

```json
// 自动（默认）
"tool_choice": "auto"

// 必须调用至少一个工具
"tool_choice": "required"

// 禁止调用工具
"tool_choice": "none"

// 强制调用某个特定函数
"tool_choice": {
  "type": "function",
  "function": { "name": "get_weather" }
}
```

---

## 5. Vision（图片输入）

### 请求

```json
{
  "model": "gpt-4.1",
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "请描述这张图片，并告诉我图中文字内容。"
        },
        {
          "type": "image_url",
          "image_url": {
            "url": "https://example.com/screenshot.png",
            "detail": "high"
          }
        }
      ]
    }
  ],
  "max_tokens": 500
}
```

也可以传 Base64 编码的图片：

```json
{
  "type": "image_url",
  "image_url": {
    "url": "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQ...",
    "detail": "low"
  }
}
```

**`detail` 参数说明：**

| 值     | 含义                                                     |
| ------ | -------------------------------------------------------- |
| `auto` | 自动选择（默认）                                         |
| `low`  | 低分辨率处理，消耗 85 tokens，速度快                     |
| `high` | 高分辨率处理，按 512px 瓦片切割，更精确但消耗更多 tokens |

---

## 6. 流式输出（Streaming）

### 请求

```json
{
  "model": "gpt-4.1",
  "messages": [{ "role": "user", "content": "写一首关于秋天的诗" }],
  "stream": true
}
```

### 响应（SSE 格式，每行一个 chunk）

```
data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1716936000,"model":"gpt-4.1-2025-04-14","choices":[{"index":0,"delta":{"role":"assistant","content":""},"logprobs":null,"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1716936000,"model":"gpt-4.1-2025-04-14","choices":[{"index":0,"delta":{"content":"秋"},"logprobs":null,"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1716936000,"model":"gpt-4.1-2025-04-14","choices":[{"index":0,"delta":{"content":"风"},"logprobs":null,"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1716936000,"model":"gpt-4.1-2025-04-14","choices":[{"index":0,"delta":{},"logprobs":null,"finish_reason":"stop"}]}

data: [DONE]
```

### 流式 Function Calling 的 chunk 格式

```
data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_abc","type":"function","function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}]}

data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"loc"}}]},"finish_reason":null}]}

data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"ation\":"}}]},"finish_reason":null}]}

data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\"Beijing\"}"}}]},"finish_reason":null}]}

data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}

data: [DONE]
```

> **关键点：** 流式输出中 `arguments` 是分片追加的，需要拼接后再 JSON.parse。

---

## 7. 结构化输出（JSON Mode）

### 方式一：JSON Mode（只保证输出是合法 JSON，不保证结构）

```json
{
  "model": "gpt-4.1",
  "messages": [
    {
      "role": "system",
      "content": "你必须以 JSON 格式回复，包含字段：name, age, city。"
    },
    { "role": "user", "content": "介绍一下张三，28岁，住在上海。" }
  ],
  "response_format": { "type": "json_object" }
}
```

响应 `content` 示例：

```json
"{\"name\": \"张三\", \"age\": 28, \"city\": \"上海\"}"
```

### 方式二：JSON Schema（强制按 Schema 输出，推荐）

```json
{
  "model": "gpt-4.1",
  "messages": [...],
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "person_info",
      "strict": true,
      "schema": {
        "type": "object",
        "properties": {
          "name":  { "type": "string" },
          "age":   { "type": "integer" },
          "city":  { "type": "string" }
        },
        "required": ["name", "age", "city"],
        "additionalProperties": false
      }
    }
  }
}
```

---

## 8. Logprobs（对数概率）

### 请求

```json
{
  "model": "gpt-4.1",
  "messages": [{ "role": "user", "content": "天空是什么颜色？" }],
  "logprobs": true,
  "top_logprobs": 3
}
```

### 响应（logprobs 部分）

```json
{
  "choices": [
    {
      "message": { "role": "assistant", "content": "蓝色" },
      "logprobs": {
        "content": [
          {
            "token": "蓝",
            "logprob": -0.0023,
            "bytes": [232, 147, 157],
            "top_logprobs": [
              { "token": "蓝", "logprob": -0.0023, "bytes": [232, 147, 157] },
              { "token": "天", "logprob": -6.21,   "bytes": [229, 164, 169] },
              { "token": "深", "logprob": -7.45,   "bytes": [230, 150, 177] }
            ]
          },
          {
            "token": "色",
            "logprob": -0.0001,
            "bytes": [232, 137, 178],
            "top_logprobs": [...]
          }
        ]
      },
      "finish_reason": "stop"
    }
  ]
}
```

---

## 9. 响应字段完整说明

```json
{
  "id": "chatcmpl-xxx",          // 本次请求唯一 ID
  "object": "chat.completion",   // 固定值
  "created": 1716936000,         // Unix 时间戳
  "model": "gpt-4.1-2025-04-14", // 实际使用的模型版本（含快照日期）
  "system_fingerprint": "fp_xxx", // 系统配置指纹，与 seed 配合确保可复现性

  "choices": [
    {
      "index": 0,                // 候选回复索引（n>1时有多个）
      "message": {
        "role": "assistant",
        "content": "...",         // 文本内容（tool_calls时为null）
        "tool_calls": [...],      // 工具调用列表（可选）
        "refusal": null           // 模型拒绝回复时的原因（字符串或null）
      },
      "logprobs": null,           // 对数概率（需开启才有值）
      "finish_reason": "stop"
    }
  ],

  "usage": {
    "prompt_tokens": 35,
    "completion_tokens": 32,
    "total_tokens": 67,
    "prompt_tokens_details": {
      "cached_tokens": 0,         // 命中 Prompt Cache 的 token 数（计费更低）
      "audio_tokens": 0
    },
    "completion_tokens_details": {
      "reasoning_tokens": 0,      // o系列推理模型内部推理消耗的 token
      "audio_tokens": 0,
      "accepted_prediction_tokens": 0,   // Predicted Outputs 中被接受的 token
      "rejected_prediction_tokens": 0    // Predicted Outputs 中被拒绝的 token
    }
  }
}
```

---

## 10. 错误响应格式

所有错误都会返回对应的 HTTP 状态码，响应体格式如下：

```json
{
  "error": {
    "message": "This model's maximum context length is 128000 tokens. However, you requested 150000 tokens.",
    "type": "invalid_request_error",
    "param": "messages",
    "code": "context_length_exceeded"
  }
}
```

**常见错误一览：**

| HTTP 状态码 | type                    | code                      | 说明               |
| ----------- | ----------------------- | ------------------------- | ------------------ |
| 400         | `invalid_request_error` | `context_length_exceeded` | 超出上下文长度     |
| 400         | `invalid_request_error` | `invalid_api_key`         | API Key 格式错误   |
| 401         | `authentication_error`  | —                         | 认证失败           |
| 403         | `permission_error`      | —                         | 无权限访问该模型   |
| 404         | `invalid_request_error` | `model_not_found`         | 模型不存在         |
| 422         | `invalid_request_error` | —                         | 请求参数不合法     |
| 429         | `rate_limit_error`      | `rate_limit_exceeded`     | 超过速率限制       |
| 500         | `api_error`             | —                         | 服务器内部错误     |
| 503         | `api_error`             | `engine_overloaded`       | 服务过载，稍后重试 |

**`type` 字段取值：**

| 值                      | 含义           |
| ----------------------- | -------------- |
| `invalid_request_error` | 请求参数有误   |
| `authentication_error`  | 认证/授权失败  |
| `permission_error`      | 权限不足       |
| `rate_limit_error`      | 频率或配额超限 |
| `api_error`             | 服务端错误     |

---

## 附：多轮对话完整消息历史示例

```json
{
  "model": "gpt-4.1",
  "messages": [
    { "role": "system", "content": "你是一个专业助手。" },
    { "role": "user", "content": "北京今天天气？" },
    {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        {
          "id": "call_001",
          "type": "function",
          "function": { "name": "get_weather", "arguments": "{\"location\":\"Beijing\"}" }
        }
      ]
    },
    {
      "role": "tool",
      "tool_call_id": "call_001",
      "content": "{\"temp\": 28, \"condition\": \"晴\"}"
    },
    { "role": "assistant", "content": "北京今天晴天，气温28°C。" },
    { "role": "user", "content": "那上海呢？" },
    {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        {
          "id": "call_002",
          "type": "function",
          "function": { "name": "get_weather", "arguments": "{\"location\":\"Shanghai\"}" }
        }
      ]
    },
    {
      "role": "tool",
      "tool_call_id": "call_002",
      "content": "{\"temp\": 32, \"condition\": \"多云\"}"
    }
  ]
}
```
