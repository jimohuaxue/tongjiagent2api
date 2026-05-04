"""
Hub 层：以 **OpenAI 语义**作为唯一中间态。

设计目标：
- 插件侧：把“站点平台格式”转换成 OpenAI 语义（请求 + 结构化流事件）。
- 协议侧：把 OpenAI 语义转换成不同对外协议（OpenAI / Anthropic / Kimi ...）。

当前仓库历史上存在 Canonical 模型用于多协议解析；Hub 层用于把“内部执行语义”
固定为 OpenAI，降低插件/协议扩展的学习成本。
"""

from .schemas import OpenAIStreamEvent

__all__ = ["OpenAIStreamEvent"]
