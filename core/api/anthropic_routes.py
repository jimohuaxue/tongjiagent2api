"""
Anthropic 协议路由。

支持：
- /anthropic/{provider}/v1/messages
- /anthropic/{provider}/v1/models
- /anthropic/{provider}/v1/models/{model_id}
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from core.api.protocol_models import (
    ensure_provider_model,
    format_anthropic_model_response,
    format_anthropic_models_response,
    list_provider_model_ids,
)

from core.api.protocol_routes import (
    create_protocol_router,
    format_anthropic_stream_error,
    handle_protocol_chat_request,
)
from core.api.chat_handler import ChatHandler
from core.api.deps import get_chat_handler
from core.protocol.anthropic import AnthropicProtocolAdapter


def create_anthropic_router() -> APIRouter:
    router = create_protocol_router()
    adapter = AnthropicProtocolAdapter()

    @router.get("/anthropic/{provider}/v1/models")
    def list_models(provider: str) -> dict[str, Any]:
        return format_anthropic_models_response(list_provider_model_ids(provider))

    @router.get("/anthropic/{provider}/v1/models/{model_id}")
    def get_model(provider: str, model_id: str) -> dict[str, Any]:
        return format_anthropic_model_response(
            ensure_provider_model(provider, model_id)
        )

    @router.post("/anthropic/{provider}/v1/messages")
    async def messages(
        provider: str,
        request: Request,
        handler: ChatHandler = Depends(get_chat_handler),
    ) -> Any:
        return await handle_protocol_chat_request(
            adapter=adapter,
            provider=provider,
            request=request,
            handler=handler,
            stream_error_formatter=format_anthropic_stream_error,
        )

    return router
