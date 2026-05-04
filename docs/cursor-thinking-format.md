# Cursor 如何识别并展示 Thinking

## 查询结论

### 1. Cursor 官方未公开「自定义 API 下的 thinking 格式」

- Cursor 公开文档（cursor.com/docs）只描述 **Admin / Analytics / Cloud Agents** 等团队 API，**没有**说明 Chat 界面在对接「自定义 OpenAI 兼容端点」时如何解析或展示 thinking/reasoning。
- 论坛与社区讨论也**没有**给出「自定义 Base URL + OpenAI 格式下，返回什么字段 Cursor 会显示思考」的明确说明。

### 2. Thinking 展示很可能只针对「原生 Anthropic」通道

当使用 **Anthropic 官方 API**（Cursor 内选 Claude 且走 Anthropic 后端）时，格式是 **Anthropic Messages API**，不是 OpenAI：

- **非流式**：`message.content` 为 **数组**，例如：
  ```json
  "content": [
    { "type": "thinking", "thinking": "推理内容...", "signature": "..." },
    { "type": "text", "text": "最终回答..." }
  ]
  ```
- **流式**：通过 SSE 事件 `content_block_start` / `content_block_delta` / `content_block_stop`，其中 `delta` 有 `thinking_delta` 与 `text_delta`。

Cursor 的「思考」折叠/展示 UI 很可能是按这套 **Anthropic 的 content 数组 + type: "thinking"** 实现的，**仅在使用 Anthropic 通道时生效**。

### 3. 使用「自定义 OpenAI 兼容端点」时的情况

你把 Cursor 的 **Base URL** 指到自己的服务（如 CDPDemo）时：

- Cursor 会按 **OpenAI Chat Completions** 发请求、解析响应。
- OpenAI 标准里 **没有** 定义 `reasoning_content` 或 `thinking_blocks`；扩展字段是否被 Cursor 使用**未在文档中说明**。
- 因此当前较合理的推断是：**在自定义 OpenAI 端点下，Cursor 可能根本没有实现「解析并展示 thinking」的逻辑**，所以无论返回 `reasoning_content`、`<think>` 标签，还是把思考塞进 `content` 字符串，界面上都可能不出现「思考」区域。

### 4. 你之前的「<think> 方案」来源

你在 `chat_response_debug.json` 里提到的「匹配 <think> 和 </think> 再替换成 div」：

- 那是**别的产品/前端的实现方式**（例如 Dify、自建前端），用来在**自己的** Markdown 里把 <think> 当成思考块渲染。
- **不是** Cursor 官方文档或公开行为说明；Cursor 是否在任意模式下解析 <think>，**没有可查依据**。

---

## 建议

1. **若必须看到 Cursor 内的「思考」展示**

   - 使用 Cursor 内置的 **Claude（Anthropic）** 并开启 **Extended thinking**，这样走的是 Anthropic 的 content 数组 + thinking 块，Cursor 大概率会按原生逻辑展示。

2. **若必须走自己的代理（OpenAI 兼容）**

   - 当前没有证据表明 Cursor 在自定义端点下会解析任何 thinking 字段或 <think> 标签。
   - 可以保留你在代理里加的「固定推理」用于**调试/日志**（例如看响应 body 或抓包），但不要依赖 Cursor 界面会单独展示它。

3. **可选尝试（仅作实验）**
   - 若你希望「万一 Cursor 将来或内部支持」：
     - 可尝试在 **非流式** 响应里把 `choices[0].message.content` 改成 **数组**，例如：
       ```json
       "content": [
         { "type": "thinking", "thinking": "固定推理内容" },
         { "type": "text", "text": "最终回答" }
       ]
       ```
     - 看 Cursor 是否会把第一块渲染成思考（多数 OpenAI 兼容实现只认 `type: "text"`，不认 `type: "thinking"`，因此不保证有效）。

---

## 参考

- [Anthropic: Building with extended thinking](https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking)
- [LiteLLM: Reasoning Content](https://docs.litellm.ai/docs/reasoning_content)（说明各厂商 reasoning 字段差异，未提 Cursor）
