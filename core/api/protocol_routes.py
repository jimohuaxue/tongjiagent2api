"""Shared helpers for protocol-specific chat routes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.api.auth import require_api_key
from core.api.chat_handler import ChatHandler
from core.api.file_store import (
    anthropic_content_blocks_from_attachments,
    append_message_path_parts,
    append_pdf_text_parts_from_messages,
    append_parts_to_last_user_message,
    openai_file_parts_from_attachments,
    resolve_openai_file_references,
)
from core.protocol.base import ProtocolAdapter
from core.protocol.service import CanonicalChatService

StreamErrorFormatter = Callable[[dict[str, Any]], str]

STREAMING_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def create_protocol_router() -> APIRouter:
    """Create a protocol router with API-key auth applied."""
    return APIRouter(dependencies=[Depends(require_api_key)])


def format_openai_stream_error(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def format_anthropic_stream_error(payload: dict[str, Any]) -> str:
    return "event: error\n" f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def handle_protocol_chat_request(
    *,
    adapter: ProtocolAdapter,
    provider: str,
    request: Request,
    handler: ChatHandler,
    stream_error_formatter: StreamErrorFormatter,
) -> Any:
    raw_body = await request.json()
    if adapter.protocol_name in {"openai", "anthropic"}:
        raw_body = resolve_openai_file_references(request.app, raw_body)
        messages = raw_body.get("messages")
        if isinstance(messages, list):
            body = dict(raw_body)
            copied_messages = [
                dict(item) if isinstance(item, dict) else item for item in messages
            ]
            attachment_parts = (
                anthropic_content_blocks_from_attachments(raw_body)
                if adapter.protocol_name == "anthropic"
                else openai_file_parts_from_attachments(raw_body)
            )
            append_parts_to_last_user_message(
                copied_messages,
                attachment_parts,
            )
            append_message_path_parts(
                copied_messages,
                protocol=adapter.protocol_name,
            )
            append_pdf_text_parts_from_messages(copied_messages)
            body["messages"] = copied_messages
            raw_body = body
    try:
        canonical_req = adapter.parse_request(provider, raw_body)
    except Exception as exc:
        status, payload = adapter.render_error(exc)
        return JSONResponse(status_code=status, content=payload)

    service = CanonicalChatService(handler)
    if canonical_req.stream:

        async def sse_stream() -> AsyncIterator[str]:
            try:
                async for event in adapter.render_stream(
                    canonical_req,
                    service.stream_raw(canonical_req),
                ):
                    yield event
            except Exception as exc:
                status, payload = adapter.render_error(exc)
                del status
                yield stream_error_formatter(payload)

        return StreamingResponse(
            sse_stream(),
            media_type="text/event-stream",
            headers=STREAMING_HEADERS,
        )

    try:
        raw_events = await service.collect_raw(canonical_req)
        return adapter.render_non_stream(canonical_req, raw_events)
    except Exception as exc:
        status, payload = adapter.render_error(exc)
        return JSONResponse(status_code=status, content=payload)
