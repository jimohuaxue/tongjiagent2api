# 项目业务架构（当前实现）

## 1. 总体模型

这是一个基于 `FastAPI + Playwright + Chromium CDP` 的 OpenAI 兼容网关。

- 对外提供 `/{type}/v1/chat/completions`
- 对内按 **代理组 / 浏览器 / type tab / session** 调度站点请求
- 当前已落地的插件是 `claude`

## 2. 运行时层级

当前实现已经不再使用“一个 type 一个 page 池”的旧模型，而是：

```text
ProxyGroup（代理组）
  └── Browser（一个代理组一个 Chromium 进程）
        └── Tab（一个 type 在一个浏览器里只有一个 tab）
              └── Sessions（一个 tab 下可挂多个会话）
```

核心约束：

- 一个浏览器只属于一个代理组
- 一个浏览器里，一个 `type` 只允许一个 tab
- 一个 tab 绑定一个账号
- tab 只有在 drained 后才能切号

## 3. 配置与数据

### 账号配置

账号配置仍存 SQLite：

- `proxy_group`
- `account(name, type, auth, unfreeze_at)`

### 运行时配置

调度与回收参数从根目录 `config.yaml` 读取，例如：

- `scheduler.tab_max_concurrent`
- `scheduler.browser_gc_interval_seconds`
- `scheduler.tab_idle_seconds`
- `scheduler.resident_browser_count`

## 4. 浏览器与预热

启动后不会为所有代理组全部拉起浏览器。

当前策略是：

1. 根据 `resident_browser_count` 预热前若干个有可用账号的代理组
2. 每个预热浏览器内，为该组下每个可用 `type` 打开 1 个 tab
3. tab 创建后立即对该账号执行登录/写 cookie

这样可以保证至少有一批热浏览器，后续请求优先命中热资源。

## 5. Tab 调度规则

一次新请求（无法复用旧 session）会按下面的顺序选资源：

1. 已打开浏览器里，已有该 `type` 的可服务 tab
2. 已打开浏览器里，没有该 `type` tab，但该组有可用账号，可直接新开 tab
3. 已打开浏览器里，该 `type` tab 已 drained，且同组存在其他可用账号，可原地切号
4. 如果以上都没有，再新开一个有该 `type` 可用账号的浏览器

目标是：

- 优先复用热 tab
- 其次复用已打开浏览器
- 最后才冷启动新浏览器

## 6. 会话复用

### 会话 ID 传递方式

当前实现使用 **零宽字符** 携带 `session_id`：

- 服务端把 `session_id` 编码成零宽标记，附加到 assistant 回复末尾
- 客户端只要把 assistant 内容原样带回，服务端就能再次解析
- 解析时会从 **最后一条带标记的消息** 开始逆序查找，优先拿最新 session

### 会话绑定规则

每个 session 绑定到：

- `proxy_key`
- `type`
- `account_id`

复用时必须同时满足：

- session 仍在缓存中
- 对应 tab 还存在
- tab 当前绑定账号仍等于 session 的账号
- 插件内部仍持有该 session 的站点状态
- tab 当前可接新请求

任一条件不满足，则放弃复用，改为新建会话并回放完整历史。

## 7. 请求流程

### 复用旧会话

1. 解析 `type`
2. 逆序解析消息中的最新 `session_id`
3. 命中缓存后，校验 session 对应 tab/account 是否仍然有效
4. 若有效，直接复用站点会话，不回放完整历史

### 新建会话

1. 根据调度规则选择一个目标 tab
2. 如果该 tab 尚不存在，则新建并登录
3. 调用插件创建站点侧 conversation
4. 以**完整历史**组装 prompt，并发送首条消息
5. 将新 `session_id` 写入缓存并附加到回复末尾

## 8. 额度耗尽 / 429

插件上报 `AccountFrozenError` 后，当前实现会：

1. 把该账号的 `unfreeze_at` 写回数据库
2. 将该 tab 标记为 `draining/frozen`
3. 使该 tab 下已有 session 全部失效
4. 当前请求重试，重新按调度规则找资源

当该 tab 没有活跃请求后：

- 若原账号已恢复可用，则直接恢复该 tab
- 否则若同组有其他可用账号，则在当前 tab 上切号
- 否则关闭该 tab

## 9. 浏览器回收

后台维护循环会按 `browser_gc_interval_seconds` 周期扫描浏览器。

回收条件：

- 浏览器下所有 tab 都没有活跃请求
- 所有 tab 都已经空闲超过 `tab_idle_seconds`
- 当前打开的浏览器数大于 `resident_browser_count`

回收时会：

- 关闭浏览器下所有 tab
- 失效这些 tab 对应的全部 session
- 最后关闭浏览器进程

## 10. Tools / Function Calling

Tools 走 tagged tool protocol，不依赖站点原生 function calling：

- 请求带 `tools` 时，服务端注入 tagged prompt
- 模型输出 `<think>` + `<tool_call>` / `<final_answer>`
- 服务端解析成中间事件，再映射成 OpenAI `tool_calls` 或 Anthropic `tool_use`
- 新建会话时，完整历史回放会保留这套 tagged 协议语义

## 11. 插件职责

基础架构负责：

- 账号与代理组调度
- browser/tab/session 生命周期
- 会话缓存与失效
- 浏览器预热与回收

插件负责：

- 打开站点页面
- 写入认证
- 创建站点会话
- 在已有会话上发送消息
- 解析站点 SSE
- 报告 429/额度耗尽
