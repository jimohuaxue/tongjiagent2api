"""协议层内部统一模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CanonicalContentBlock(BaseModel):
    type: Literal["text", "thinking", "tool_use", "tool_result", "image", "document"]
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    is_error: bool | None = None
    mime_type: str | None = None
    data: str | None = None
    url: str | None = None
    filename: str | None = None  # document 类型用：原始文件名


class CanonicalMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: list[CanonicalContentBlock] = Field(default_factory=list)


class CanonicalToolSpec(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    strict: bool = False


class CanonicalChatRequest(BaseModel):
    protocol: Literal["openai", "anthropic"]
    provider: str
    model: str
    system: list[CanonicalContentBlock] = Field(default_factory=list)
    messages: list[CanonicalMessage] = Field(default_factory=list)
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop_sequences: list[str] = Field(default_factory=list)
    tools: list[CanonicalToolSpec] = Field(default_factory=list)
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    resume_session_id: str | None = None
    client_conversation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def tool_schema_map(self) -> dict[str, dict[str, Any]]:
        return {tool.name: tool.input_schema for tool in self.tools if tool.name}


class CanonicalStreamEvent(BaseModel):
    type: Literal[
        "message_start",
        "text_delta",
        "thinking_delta",
        "tool_call",
        "usage",
        "message_stop",
        "error",
    ]
    text: str | None = None
    id: str | None = None
    name: str | None = None
    arguments: str | None = None
    stop_reason: str | None = None
    session_id: str | None = None
    usage: dict[str, int] | None = None
    error: str | None = None
