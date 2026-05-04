"""
OpenAI 协议路由。

支持：
- /openai/{provider}/v1/chat/completions
- /openai/{provider}/v1/responses
- /openai/{provider}/v1/models
"""

import base64
import hashlib
import json
import logging
import math
import re
import struct
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from core.api.conv_parser import decode_latest_session_id, strip_session_id_suffix
from core.api.file_store import (
    append_message_path_parts,
    append_pdf_text_parts_from_messages,
    append_parts_to_last_user_message,
    delete_openai_file,
    get_openai_file,
    list_openai_files,
    openai_file_part_to_chat_part,
    openai_file_parts_from_attachments,
    openai_file_metadata,
    parse_multipart_form,
    resolve_openai_file_references,
    store_openai_file,
)
from core.api.protocol_models import (
    format_openai_models_response,
    list_provider_model_ids,
)
from core.api.protocol_routes import (
    STREAMING_HEADERS,
    create_protocol_router,
    format_openai_stream_error,
    handle_protocol_chat_request,
)
from core.api.chat_handler import ChatHandler
from core.api.deps import get_chat_handler
from core.api.tagged_output import (
    TaggedOutputError,
    TaggedToolCall,
    format_openai_tagged_answer,
    parse_tagged_output,
    strip_leading_final_answer_wrapper,
)
from core.api.tool_arguments import (
    match_tool_schema_property,
    normalize_tool_arguments_for_schema,
    select_unknown_argument_for_property,
    tool_schema_properties,
    tool_schema_required,
    value_matches_tool_schema,
)
from core.protocol.service import CanonicalChatService
from core.protocol.openai import OpenAIProtocolAdapter

logger = logging.getLogger(__name__)

_RESPONSES_SESSION_STORE_ATTR = "openai_responses_session_map"
_RESPONSES_SESSION_TTL_SECONDS = 6 * 60 * 60
_RESPONSES_SESSION_MAX_ENTRIES = 4096
_EMBEDDING_DEFAULT_DIMENSIONS = 1536
_EMBEDDING_MAX_DIMENSIONS = 3072


def _responses_image_url_value(value: Any) -> str | None:
    """提取 Responses/Chat 多种图片字段形态里的真实 URL 或 data URL。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("url", "image_url", "data"):
            nested = _responses_image_url_value(value.get(key))
            if nested:
                return nested
        return None
    return None


def _responses_image_part_to_chat_part(part: dict[str, Any]) -> dict[str, Any] | None:
    image_url = _responses_image_url_value(
        part.get("image_url") or part.get("url") or part.get("data")
    )
    if not image_url:
        return None
    return {"type": "image_url", "image_url": {"url": image_url}}


def _responses_file_data_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("file_data", "data", "content", "bytes", "base64"):
            nested = _responses_file_data_value(value.get(key))
            if nested:
                return nested
    return None


def _responses_input_to_messages(raw_input: Any) -> list[dict[str, Any]]:
    if isinstance(raw_input, str):
        return [{"role": "user", "content": raw_input}]
    if not isinstance(raw_input, list):
        return [{"role": "user", "content": str(raw_input or "")}]

    messages: list[dict[str, Any]] = []
    loose_content: list[dict[str, Any]] = []
    for item in raw_input:
        if not isinstance(item, dict):
            loose_content.append({"type": "text", "text": str(item)})
            continue
        item_type = str(item.get("type") or "")
        if item_type in {"message", ""}:
            role = str(item.get("role") or "user")
            # OpenAI Responses API 用 "developer" 替代旧的 "system" 角色（o-series 起）。
            # 我们的 canonical schema 只接受 system/user/assistant/tool，统一规范化。
            if role == "developer":
                role = "system"
            content = _responses_content_to_chat_content(item.get("content") or "")
            messages.append({"role": role, "content": content})
        elif item_type == "input_text":
            loose_content.append({"type": "text", "text": str(item.get("text") or "")})
        elif item_type == "input_image":
            image_part = _responses_image_part_to_chat_part(item)
            if image_part is not None:
                loose_content.append(image_part)
        elif item_type == "input_file":
            file_part = openai_file_part_to_chat_part(item)
            if file_part is not None:
                loose_content.append(file_part)
        elif item_type == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or "")
            name = str(item.get("name") or "")
            arguments = item.get("arguments") or "{}"
            if isinstance(arguments, dict):
                arguments_text = json.dumps(arguments, ensure_ascii=False)
            else:
                arguments_text = str(arguments)
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": arguments_text,
                            },
                        }
                    ],
                }
            )
        elif item_type == "function_call_output":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id") or ""),
                    "content": str(item.get("output") or ""),
                }
            )
    if loose_content:
        messages.append({"role": "user", "content": loose_content})
    if not messages:
        messages.append({"role": "user", "content": ""})
    return messages


def _responses_content_to_chat_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            parts.append({"type": "text", "text": str(part)})
            continue
        part_type = str(part.get("type") or "")
        if part_type in {"text", "input_text", "output_text"}:
            parts.append({"type": "text", "text": str(part.get("text") or "")})
        elif part_type in {"image_url", "input_image"}:
            image_part = _responses_image_part_to_chat_part(part)
            if image_part is not None:
                parts.append(image_part)
        elif part_type in {"file", "input_file"}:
            file_part = openai_file_part_to_chat_part(part)
            if file_part is not None:
                parts.append(file_part)
    return parts


def _responses_file_part_to_chat_part(part: dict[str, Any]) -> dict[str, Any] | None:
    return openai_file_part_to_chat_part(part)


def _openai_file_error(message: str, *, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error"
                if status_code < 500
                else "server_error",
            }
        },
    )


def _responses_body_to_chat_body(raw_body: dict[str, Any]) -> dict[str, Any]:
    body = dict(raw_body)
    messages = _responses_input_to_messages(raw_body.get("input", ""))
    append_parts_to_last_user_message(
        messages,
        openai_file_parts_from_attachments(raw_body),
    )
    append_message_path_parts(messages, protocol="openai")
    append_pdf_text_parts_from_messages(messages)
    body["messages"] = messages
    body.pop("input", None)
    return body


def _responses_session_store(app: Any) -> dict[str, tuple[str, float]]:
    store = getattr(app.state, _RESPONSES_SESSION_STORE_ATTR, None)
    if not isinstance(store, dict):
        store = {}
        setattr(app.state, _RESPONSES_SESSION_STORE_ATTR, store)
    return store


def _prune_responses_session_store(
    store: dict[str, tuple[str, float]],
    *,
    now: float | None = None,
) -> None:
    current = time.time() if now is None else now
    stale_keys = [
        response_id
        for response_id, (_session_id, seen_at) in store.items()
        if current - seen_at > _RESPONSES_SESSION_TTL_SECONDS
    ]
    for response_id in stale_keys:
        store.pop(response_id, None)

    overflow = len(store) - _RESPONSES_SESSION_MAX_ENTRIES
    if overflow <= 0:
        return
    oldest = sorted(store.items(), key=lambda item: item[1][1])[:overflow]
    for response_id, _value in oldest:
        store.pop(response_id, None)


def _store_responses_session(app: Any, response_id: str, session_id: str) -> None:
    if not response_id or not session_id:
        return
    store = _responses_session_store(app)
    now = time.time()
    _prune_responses_session_store(store, now=now)
    store[response_id] = (session_id, now)


def _lookup_responses_session(app: Any, response_id: Any) -> str | None:
    if not response_id:
        return None
    store = _responses_session_store(app)
    now = time.time()
    _prune_responses_session_store(store, now=now)
    value = store.get(str(response_id))
    if value is None:
        return None
    session_id, _seen_at = value
    store[str(response_id)] = (session_id, now)
    return session_id


def _extract_responses_session_id(raw_text: str) -> str | None:
    return decode_latest_session_id(raw_text)


def _response_payload(
    response_id: str,
    model: str,
    text: str,
    created: int,
    *,
    output: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": "completed",
        "model": model,
        "output": output
        if output is not None
        else [_response_message_item(f"msg_{uuid.uuid4().hex}", text, status="completed")],
        "output_text": text,
    }


def _response_message_item(message_id: str, text: str = "", *, status: str = "in_progress") -> dict[str, Any]:
    return {
        "id": message_id,
        "type": "message",
        "status": status,
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": text,
                "annotations": [],
            }
        ],
    }


def _response_function_call_item(tool_call: TaggedToolCall) -> dict[str, Any]:
    return {
        "id": f"fc_{uuid.uuid4().hex}",
        "type": "function_call",
        "status": "completed",
        "call_id": f"call_{uuid.uuid4().hex[:24]}",
        "name": tool_call.name,
        "arguments": json.dumps(tool_call.arguments, ensure_ascii=False),
    }


def _responses_available_tool_names(raw_tools: Any) -> set[str]:
    return set(_responses_available_tool_schemas(raw_tools))


def _responses_available_tool_schemas(raw_tools: Any) -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    if not isinstance(raw_tools, list):
        return schemas
    for tool in raw_tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = fn.get("name") if isinstance(fn, dict) else None
        if isinstance(name, str) and name:
            schema = fn.get("parameters") or fn.get("input_schema") or {}
            if isinstance(schema, str):
                try:
                    schema = json.loads(schema)
                except json.JSONDecodeError:
                    schema = {}
            schemas[name] = schema if isinstance(schema, dict) else {}
    return schemas


def _parse_loose_tool_call(
    text: str,
    tool_names: set[str],
    *,
    tool_schemas: dict[str, dict[str, Any]] | None = None,
) -> TaggedToolCall | None:
    tool_calls = _parse_loose_tool_calls(
        text,
        tool_names,
        tool_schemas=tool_schemas,
    )
    return tool_calls[0] if tool_calls else None


def _parse_loose_tool_calls(
    text: str,
    tool_names: set[str],
    *,
    tool_schemas: dict[str, dict[str, Any]] | None = None,
) -> list[TaggedToolCall]:
    if not tool_names:
        return []
    # alma / GLM-5.1 corruption variants seen in the wild:
    #   <tool_call>...</tool_call>           — proper close
    #   <tool_call>...</tool_calls>          — typo'd close
    #   <tool_call>...tool_calls>            — fully corrupted close (no leading "</")
    #   <tool_call>...                       — no close at all (truncated)
    #   <tool_call>...<tool_call>...         — consecutive loose calls
    schemas = tool_schemas or {}
    tool_calls: list[TaggedToolCall] = []
    for payload in _loose_tool_payloads(text, tool_names):
        tool_call = _parse_loose_tool_payload(
            payload,
            tool_names,
            schemas,
        )
        if tool_call is None:
            continue
        normalized = _normalize_tagged_tool_call_for_schema(tool_call, schemas)
        if _tool_call_has_required_arguments(
            normalized,
            schemas.get(normalized.name) or {},
        ):
            tool_calls.append(normalized)
    return tool_calls


def _loose_tool_payloads(text: str, tool_names: set[str]) -> list[str]:
    starts = [match.start() for match in re.finditer(r"<tool_call>", text)]
    payloads: list[str] = []
    cursor = 0
    while cursor < len(starts):
        start = starts[cursor] + len("<tool_call>")
        end = len(text)
        next_cursor = len(starts)
        for candidate_index in range(cursor + 1, len(starts)):
            candidate_start = starts[candidate_index]
            candidate_payload = text[candidate_start + len("<tool_call>") :]
            if _payload_starts_declared_tool(candidate_payload, tool_names):
                end = candidate_start
                next_cursor = candidate_index
                break
        if next_cursor == len(starts):
            terminal = _find_loose_tool_terminal(text, start)
            if terminal is not None:
                end = terminal
        payload = text[start:end].strip()
        if payload:
            payloads.append(payload)
        cursor = next_cursor
    return payloads


def _payload_starts_declared_tool(payload: str, tool_names: set[str]) -> bool:
    stripped = payload.lstrip()
    for name in sorted(tool_names, key=len, reverse=True):
        if not stripped.startswith(name):
            continue
        rest = stripped[len(name) :]
        if not rest or rest[0].isspace() or rest[0] in {".", "(", ":", ">", "<", "_", "-"}:
            return True
    return False


def _find_loose_tool_terminal(text: str, start: int) -> int | None:
    terminals = [
        "</tool_call>",
        "</tool_calls>",
        "<tool_calls>",
    ]
    positions = [
        position
        for position in (text.find(terminal, start) for terminal in terminals)
        if position >= 0
    ]
    positions.extend(
        start + match.start()
        for match in re.finditer(r"(?<!<)tool_calls?>", text[start:])
    )
    return min(positions) if positions else None


def _parse_loose_tool_payload(
    payload: str,
    tool_names: set[str],
    tool_schemas: dict[str, dict[str, Any]],
) -> TaggedToolCall | None:
    for name in sorted(tool_names, key=len, reverse=True):
        schema = tool_schemas.get(name) or {}
        function_style_args = _parse_function_style_tool_args(payload, name)
        if function_style_args is not None:
            function_style_args = _normalize_tool_arguments_for_schema(
                function_style_args,
                schema,
            )
            return TaggedToolCall(
                name=name,
                arguments=function_style_args,
                raw_json=json.dumps(
                    {"name": name, "arguments": function_style_args},
                    ensure_ascii=False,
                ),
            )
        if payload.startswith(name):
            alma_args = _parse_alma_tool_args(payload[len(name) :], schema)
            if alma_args:
                alma_args = _normalize_tool_arguments_for_schema(alma_args, schema)
                return TaggedToolCall(
                    name=name,
                    arguments=alma_args,
                    raw_json=json.dumps(
                        {"name": name, "arguments": alma_args},
                        ensure_ascii=False,
                    ),
                )
        prefixed_match = _match_alma_prefixed_tool_name(payload, name)
        if prefixed_match is not None:
            alma_args = _parse_alma_tool_args(prefixed_match, schema)
            if alma_args:
                alma_args = _normalize_tool_arguments_for_schema(alma_args, schema)
                return TaggedToolCall(
                    name=name,
                    arguments=alma_args,
                    raw_json=json.dumps(
                        {"name": name, "arguments": alma_args},
                        ensure_ascii=False,
                    ),
                )
        prefixes = (f"{name}>", f"{name}:")
        for prefix in prefixes:
            if payload.startswith(prefix):
                command = payload[len(prefix) :].strip()
                if not command:
                    return None
                arguments = {"cmd": command}
                arguments = _normalize_tool_arguments_for_schema(arguments, schema)
                return TaggedToolCall(
                    name=name,
                    arguments=arguments,
                    raw_json=json.dumps(
                        {"name": name, "arguments": arguments},
                        ensure_ascii=False,
                    ),
                )
    return None


def _parse_loose_final_answer(text: str) -> str | None:
    match = re.search(
        r"<tool_call>\s*final_answer>\s*(.*?)(?:</final_answer>|</tool_call>|\Z)",
        text,
        flags=re.DOTALL,
    )
    if not match:
        return None
    answer = _strip_tool_call_tail(match.group(1)).strip()
    return answer or None


def _normalize_tagged_tool_call_for_schema(
    tool_call: TaggedToolCall,
    tool_schemas: dict[str, dict[str, Any]],
) -> TaggedToolCall:
    schema = tool_schemas.get(tool_call.name) or {}
    arguments = _normalize_tool_arguments_for_schema(tool_call.arguments, schema)
    arguments = _fill_tool_argument_defaults(tool_call.name, arguments, schema)
    if arguments == tool_call.arguments:
        return tool_call
    return TaggedToolCall(
        name=tool_call.name,
        arguments=arguments,
        raw_json=json.dumps(
            {"name": tool_call.name, "arguments": arguments},
            ensure_ascii=False,
        ),
    )


def _tool_call_has_required_arguments(
    tool_call: TaggedToolCall,
    schema: dict[str, Any],
) -> bool:
    required = _tool_schema_required(schema)
    if not required:
        return True
    return all(prop in tool_call.arguments for prop in required)


def _fill_tool_argument_defaults(
    name: str,
    arguments: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    required = set(_tool_schema_required(schema))
    properties = _tool_schema_properties(schema)
    if (
        "description" not in required
        or "description" in arguments
        or not _is_bash_like_tool(name)
    ):
        return arguments
    desc_schema = properties.get("description") or {}
    if not _value_matches_tool_schema("Run shell command", desc_schema):
        return arguments
    command = (
        arguments.get("command")
        or arguments.get("cmd")
        or arguments.get("script")
        or arguments.get("shell")
    )
    if not isinstance(command, str) or not command.strip():
        return arguments
    return {**arguments, "description": _bash_description_for(command)}


def _is_bash_like_tool(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", name.lower())
    return normalized in {"bash", "shell", "terminal"}


def _bash_description_for(command: str) -> str:
    first_line = command.strip().splitlines()[0].strip()
    if not first_line:
        return "Run shell command"
    clipped = first_line[:80].rstrip()
    return f"Run: {clipped}"


def _normalize_tool_arguments_for_schema(
    arguments: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    return normalize_tool_arguments_for_schema(arguments, schema)


def _tool_schema_properties(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return tool_schema_properties(schema)


def _tool_schema_required(schema: dict[str, Any]) -> list[str]:
    return tool_schema_required(schema)


def _match_tool_schema_property(
    key: str,
    properties: dict[str, dict[str, Any]],
) -> str | None:
    return match_tool_schema_property(key, properties)


def _select_unknown_argument_for_property(
    prop: str,
    unknown: dict[str, Any],
    properties: dict[str, dict[str, Any]],
    missing_required: list[str],
) -> str | None:
    return select_unknown_argument_for_property(
        prop,
        unknown,
        properties,
        missing_required,
    )


def _value_matches_tool_schema(value: Any, prop_schema: dict[str, Any]) -> bool:
    return value_matches_tool_schema(value, prop_schema)


def _parse_function_style_tool_args(
    payload: str,
    tool_name: str,
) -> dict[str, Any] | None:
    # Some models emit tool calls like:
    #   <Skill(skill="system-info")</arg_value>
    #   Skill(skill="system-info")
    #   Bash command="pwd" description="Show directory"
    #   Bash.command="pwd", description="Show directory"
    #   Bash_arguments__command="pwd"
    #   Glob(pattern: "*.md", path: "/tmp")
    # instead of the requested JSON tagged protocol.
    trimmed = payload.strip()
    name_pattern = re.escape(tool_name)
    call_match = re.match(
        rf"^<?\s*{name_pattern}\s*\((.*?)\)",
        trimmed,
        flags=re.DOTALL,
    )
    attr_match = None
    if call_match is None:
        attr_match = re.match(
            rf"^<\s*{name_pattern}\s+([^>]*?)(?:/?>|</|\Z)",
            trimmed,
            flags=re.DOTALL,
        )
    bare_attr_match = None
    if call_match is None and attr_match is None:
        bare_attr_match = re.match(
            rf"^<?\s*{name_pattern}\s*(?:_arguments__|[.\s]+)(.+)",
            trimmed,
            flags=re.DOTALL,
        )
    args_text = ""
    if call_match is not None:
        args_text = call_match.group(1).strip()
    elif attr_match is not None:
        args_text = attr_match.group(1).strip()
    elif bare_attr_match is not None:
        args_text = _strip_tool_call_tail(bare_attr_match.group(1).strip())
    else:
        return None
    if not args_text:
        return {}

    args: dict[str, Any] = {}
    for match in re.finditer(
        r"""([A-Za-z_][\w.-]*)\s*(?:=|:)\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^,\s)]+)""",
        args_text,
        flags=re.DOTALL,
    ):
        key = match.group(1)
        raw_value = match.group(2).strip()
        args[key] = _parse_function_style_value(raw_value)
    return args if args else None


def _strip_tool_call_tail(value: str) -> str:
    cleaned = value.strip().rstrip("*")
    for terminal in (
        "</tool_call>",
        "</tool_calls>",
        "</arg_value>",
        "<arg_value>",
        "<tool_calls>",
        "<tool_call>",
        "tool_calls>",
        "tool_call>",
    ):
        index = cleaned.find(terminal)
        if index >= 0:
            cleaned = cleaned[:index]
    return cleaned.strip().rstrip("*")


def _parse_function_style_value(raw_value: str) -> Any:
    if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in {
        '"',
        "'",
    }:
        if raw_value[0] == '"':
            try:
                return json.loads(raw_value)
            except json.JSONDecodeError:
                pass
        return raw_value[1:-1].encode("utf-8").decode("unicode_escape")
    lowered = raw_value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    try:
        return int(raw_value)
    except ValueError:
        pass
    try:
        return float(raw_value)
    except ValueError:
        return raw_value


def _match_alma_prefixed_tool_name(payload: str, tool_name: str) -> str | None:
    # Alma may emit names such as Bash_PB-exec for a declared Bash tool.
    if not payload.startswith(tool_name):
        return None
    rest = payload[len(tool_name) :]
    if rest.startswith(("_", "-", ".")) and "</arg_key>" in rest:
        return rest
    return None


def _parse_alma_tool_args(
    payload: str,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = payload.strip()

    # Alma 有时输出：
    # <tool_call>Bash_PB-exec</arg_key>{"command":"..."}</tool_call>
    # 或当声明工具名为 Bash 时，传入这里的 payload 是：
    # _PB-exec</arg_key>{"command":"..."}
    json_after_arg_key = re.search(r"</arg_key>\s*(\{.*)", payload, flags=re.DOTALL)
    if json_after_arg_key:
        parsed = _parse_json_object_prefix(json_after_arg_key.group(1))
        if parsed:
            return _unwrap_arguments_object(parsed)

    jsonish = _parse_alma_jsonish_args(payload)
    if jsonish:
        return jsonish

    args = _parse_alma_arg_key_values(payload)
    if args:
        return args

    # Alma 有时会把首个参数输出成不完整片段：
    # <arg_key>arguments":{"command":"uname -a...</arg_value>
    # 这种情况下优先恢复为 command 参数。
    command_match = re.search(
        r"<arg_key>.*?\"command\"\s*:\s*\"?(.*?)</arg_value>",
        payload,
        flags=re.DOTALL,
    )
    if command_match:
        command = _clean_alma_arg_value(command_match.group(1))
        if command:
            return {"command": command}

    # 更糟的变体：<arg_key>command":"VALUE</arg_value>
    # key 前没有引号，整体没有 </arg_key>，VALUE 后可能直接是 </arg_value> 或截断。
    bare_match = re.search(
        r'<arg_key>\s*"?([A-Za-z_]\w*)"?\s*:\s*"?(.*?)</arg_value>',
        payload,
        flags=re.DOTALL,
    )
    if bare_match:
        key = bare_match.group(1).strip()
        value = _clean_alma_arg_value(bare_match.group(2))
        if key and value:
            return {key: value}

    schema_args = _parse_alma_schema_arg_key_values(payload, schema or {})
    if schema_args:
        return schema_args
    return {}


def _parse_alma_arg_key_values(payload: str) -> dict[str, Any]:
    pairs: list[tuple[int, str, Any]] = []
    for match in re.finditer(
        r"(?:<tool_call>)?<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>",
        payload,
        flags=re.DOTALL,
    ):
        key = _clean_alma_arg_key(match.group(1))
        value = _clean_alma_arg_value(match.group(2), preserve_json=key in {"command", "cmd"})
        if key and value:
            pairs.append((match.start(), key, value))
    for match in re.finditer(
        r"<tool_call>\s*([^<>\s]+)</arg_key>\s*<arg_value>(.*?)</arg_value>",
        payload,
        flags=re.DOTALL,
    ):
        key = _clean_alma_arg_key(match.group(1))
        value = _clean_alma_arg_value(match.group(2), preserve_json=key in {"command", "cmd"})
        if key and value:
            pairs.append((match.start(), key, value))

    args: dict[str, Any] = {}
    for _position, key, value in sorted(pairs, key=lambda item: item[0]):
        if key not in args:
            args[key] = value
    return args


def _parse_alma_schema_arg_key_values(
    payload: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    properties = _tool_schema_properties(schema)
    if not properties or "<arg_key>" not in payload:
        return {}

    spans: list[tuple[int, str, int]] = []
    for match in re.finditer(r"<arg_key>", payload):
        parsed = _parse_alma_schema_arg_key_at(payload, match.end(), properties)
        if parsed is not None:
            key, value_start = parsed
            spans.append((match.start(), key, value_start))

    args: dict[str, Any] = {}
    for index, (_marker_start, key, value_start) in enumerate(spans):
        value_end = spans[index + 1][0] if index + 1 < len(spans) else len(payload)
        value = _clean_alma_schema_arg_value(payload[value_start:value_end])
        if key and value and key not in args:
            args[key] = value
    return args


def _parse_alma_schema_arg_key_at(
    payload: str,
    key_start: int,
    properties: dict[str, dict[str, Any]],
) -> tuple[str, int] | None:
    next_marker = payload.find("<arg_key>", key_start)
    close = payload.find("</arg_key>", key_start)
    if close >= 0 and (next_marker < 0 or close < next_marker):
        raw_key = _clean_alma_arg_key(payload[key_start:close])
        key = _match_tool_schema_property(raw_key, properties)
        if not key:
            return None
        value_start = close + len("</arg_key>")
        if payload.startswith("<arg_value>", value_start):
            value_start += len("<arg_value>")
        return key, value_start

    tail = payload[key_start:]
    for prop in sorted(properties, key=len, reverse=True):
        if tail.startswith(prop):
            value_start = key_start + len(prop)
            if payload.startswith("</arg_key>", value_start):
                value_start += len("</arg_key>")
            if payload.startswith("<arg_value>", value_start):
                value_start += len("<arg_value>")
            return prop, value_start
    return None


def _clean_alma_schema_arg_value(value: str) -> str:
    cleaned = value.strip()
    if cleaned.startswith("<arg_value>"):
        cleaned = cleaned[len("<arg_value>") :]
    for terminal in (
        "</arg_value>",
        "</tool_call>",
        "</tool_calls>",
        "<tool_calls>",
        "tool_calls>",
        "tool_call>",
    ):
        index = cleaned.find(terminal)
        if index >= 0:
            cleaned = cleaned[:index]
    return cleaned.strip()


def _parse_alma_jsonish_args(payload: str) -> dict[str, Any]:
    fragments: list[str] = []
    after_arg_key = re.search(r"<arg_key>\s*(.*)", payload, flags=re.DOTALL)
    if after_arg_key:
        fragments.append(after_arg_key.group(1))
    fragments.append(payload)
    for fragment in fragments:
        parsed = _parse_jsonish_object_fragment(fragment)
        if parsed:
            return _unwrap_arguments_object(parsed)
    return {}


def _parse_jsonish_object_fragment(fragment: str) -> dict[str, Any]:
    cleaned = _clean_jsonish_fragment(fragment)
    if not cleaned:
        return {}
    candidates = [cleaned]
    if not cleaned.startswith("{"):
        candidates.append("{" + cleaned)
        candidates.append('{"' + cleaned)
    expanded: list[str] = []
    for candidate in candidates:
        expanded.append(candidate)
        if not candidate.endswith("}"):
            expanded.append(candidate + "}")
    for candidate in expanded:
        parsed = _parse_json_object_prefix(candidate)
        if parsed:
            return parsed
    return {}


def _clean_jsonish_fragment(fragment: str) -> str:
    cleaned = fragment.strip().rstrip("*")
    for terminal in (
        "</arg_value>",
        "</tool_call>",
        "</tool_calls>",
        "<tool_calls>",
        "<tool_call>",
        "tool_calls>",
        "tool_call>",
    ):
        index = cleaned.find(terminal)
        if index >= 0:
            cleaned = cleaned[:index]
    cleaned = re.sub(r"</?arg_key>", "", cleaned)
    cleaned = re.sub(r"</?arg_value>", "", cleaned)
    return cleaned.strip().rstrip("*")


def _unwrap_arguments_object(parsed: dict[str, Any]) -> dict[str, Any]:
    wrapped = parsed.get("arguments")
    if isinstance(wrapped, dict):
        rest = {key: value for key, value in parsed.items() if key != "arguments"}
        return {**wrapped, **rest}
    return parsed


def _parse_json_object_prefix(value: str) -> dict[str, Any]:
    try:
        payload, _ = json.JSONDecoder().raw_decode(value.strip())
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _clean_alma_arg_key(value: str) -> str:
    key = re.sub(r"<[^>]+>", "", value).strip().strip('"').strip("'")
    if key.endswith('":{"command'):
        return "command"
    return key


def _clean_alma_arg_value(value: str, *, preserve_json: bool = False) -> str:
    cleaned = re.sub(r"<[^>]+>", "", value).strip()
    cleaned = cleaned.rstrip()
    if preserve_json and cleaned.startswith("{"):
        parsed = _parse_json_object_prefix(cleaned)
        if parsed:
            return json.dumps(parsed, ensure_ascii=False)
    while cleaned.endswith(("}", '"')):
        cleaned = cleaned[:-1].rstrip()
    return cleaned


def _responses_render_output_items(
    raw_text: str,
    *,
    tool_names: set[str] | None = None,
    tool_schemas: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    content_for_parse = strip_session_id_suffix(raw_text)
    schemas = tool_schemas or {}
    available_tool_names = set(schemas) or (tool_names or set())
    try:
        parsed = parse_tagged_output(content_for_parse)
    except TaggedOutputError:
        loose_final_answer = _parse_loose_final_answer(content_for_parse)
        if loose_final_answer is not None:
            return loose_final_answer, [
                _response_message_item(
                    f"msg_{uuid.uuid4().hex}",
                    loose_final_answer,
                    status="completed",
                )
            ]
        loose_tool_calls = _parse_loose_tool_calls(
            content_for_parse,
            available_tool_names,
            tool_schemas=schemas,
        )
        if loose_tool_calls:
            text = ""
            items: list[dict[str, Any]] = []
            items.extend(
                _response_function_call_item(
                    _normalize_tagged_tool_call_for_schema(tool_call, schemas)
                )
                for tool_call in loose_tool_calls
            )
            return text, items
        text = strip_leading_final_answer_wrapper(content_for_parse)
        return text, [_response_message_item(f"msg_{uuid.uuid4().hex}", text, status="completed")]

    if parsed.is_tool_call:
        text = parsed.thinking or ""
        items: list[dict[str, Any]] = []
        if text:
            items.append(_response_message_item(f"msg_{uuid.uuid4().hex}", text, status="completed"))
        items.extend(
            _response_function_call_item(
                _normalize_tagged_tool_call_for_schema(tool_call, schemas)
            )
            for tool_call in parsed.tool_calls
        )
        return text, items

    text = format_openai_tagged_answer(parsed)
    return text, [_response_message_item(f"msg_{uuid.uuid4().hex}", text, status="completed")]


def _embedding_input_items(raw_input: Any) -> list[str]:
    if isinstance(raw_input, str):
        return [raw_input]
    if isinstance(raw_input, list):
        if all(isinstance(item, int) for item in raw_input):
            return [" ".join(str(item) for item in raw_input)]
        items = [_embedding_input_to_text(item) for item in raw_input]
        return items or [""]
    return [str(raw_input or "")]


def _embedding_input_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value or "")


def _embedding_dimensions(raw_body: dict[str, Any]) -> int:
    raw_dimensions = raw_body.get("dimensions")
    if raw_dimensions is None:
        return _EMBEDDING_DEFAULT_DIMENSIONS
    try:
        dimensions = int(raw_dimensions)
    except (TypeError, ValueError) as exc:
        raise ValueError("dimensions must be an integer") from exc
    if dimensions < 1 or dimensions > _EMBEDDING_MAX_DIMENSIONS:
        raise ValueError(
            f"dimensions must be between 1 and {_EMBEDDING_MAX_DIMENSIONS}"
        )
    return dimensions


def _embedding_tokens(text: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]|\S", text.lower())
    return tokens or [""]


def _embedding_vector(text: str, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    for token in _embedding_tokens(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
        index = int.from_bytes(digest[:8], "big") % dimensions
        sign = 1.0 if digest[8] & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def _embedding_as_base64(vector: list[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


def _embeddings_payload(raw_body: dict[str, Any]) -> dict[str, Any]:
    inputs = _embedding_input_items(raw_body.get("input", ""))
    dimensions = _embedding_dimensions(raw_body)
    encoding_format = str(raw_body.get("encoding_format") or "float").lower()
    model = str(raw_body.get("model") or "web2api-embedding")
    data: list[dict[str, Any]] = []
    total_tokens = 0
    for index, text in enumerate(inputs):
        total_tokens += len(_embedding_tokens(text))
        vector = _embedding_vector(text, dimensions)
        embedding: list[float] | str
        if encoding_format == "base64":
            embedding = _embedding_as_base64(vector)
        else:
            embedding = vector
        data.append(
            {
                "object": "embedding",
                "embedding": embedding,
                "index": index,
            }
        )
    return {
        "object": "list",
        "data": data,
        "model": model,
        "usage": {
            "prompt_tokens": total_tokens,
            "total_tokens": total_tokens,
        },
    }


def create_openai_router() -> APIRouter:
    """创建 OpenAI 协议路由。"""
    router = create_protocol_router()
    adapter = OpenAIProtocolAdapter()

    @router.get("/openai/{provider}/v1/models")
    def list_models(provider: str) -> dict[str, Any]:
        return format_openai_models_response(provider, list_provider_model_ids(provider))

    @router.post("/openai/{provider}/v1/files")
    async def upload_file(provider: str, request: Request) -> Any:
        del provider
        try:
            fields, files = parse_multipart_form(
                request.headers.get("content-type", ""),
                await request.body(),
            )
        except ValueError as exc:
            return _openai_file_error(str(exc))
        if not files:
            return _openai_file_error("multipart body must include a file field")
        upload = next((item for item in files if item.field_name == "file"), files[0])
        record = store_openai_file(
            request.app,
            filename=upload.filename,
            data=upload.data,
            mime_type=upload.mime_type,
            purpose=fields.get("purpose") or "assistants",
        )
        return openai_file_metadata(record)

    @router.get("/openai/{provider}/v1/files")
    async def files(provider: str, request: Request) -> dict[str, Any]:
        del provider
        return {"object": "list", "data": list_openai_files(request.app)}

    @router.get("/openai/{provider}/v1/files/{file_id}/content")
    async def file_content(provider: str, file_id: str, request: Request) -> Any:
        del provider
        record = get_openai_file(request.app, file_id)
        if record is None:
            return _openai_file_error(f"file not found: {file_id}", status_code=404)
        return Response(
            content=record["data"],
            media_type=str(record.get("mime_type") or "application/octet-stream"),
            headers={
                "Content-Disposition": f'attachment; filename="{record.get("filename") or "upload.bin"}"'
            },
        )

    @router.get("/openai/{provider}/v1/files/{file_id}")
    async def file_metadata(provider: str, file_id: str, request: Request) -> Any:
        del provider
        record = get_openai_file(request.app, file_id)
        if record is None:
            return _openai_file_error(f"file not found: {file_id}", status_code=404)
        return openai_file_metadata(record)

    @router.delete("/openai/{provider}/v1/files/{file_id}")
    async def delete_file(provider: str, file_id: str, request: Request) -> Any:
        del provider
        if not delete_openai_file(request.app, file_id):
            return _openai_file_error(f"file not found: {file_id}", status_code=404)
        return {"id": file_id, "object": "file", "deleted": True}

    @router.post("/openai/{provider}/v1/chat/completions")
    async def chat_completions(
        provider: str,
        request: Request,
        handler: ChatHandler = Depends(get_chat_handler),
    ) -> Any:
        return await handle_protocol_chat_request(
            adapter=adapter,
            provider=provider,
            request=request,
            handler=handler,
            stream_error_formatter=format_openai_stream_error,
        )

    @router.post("/openai/{provider}/v1/embeddings")
    async def embeddings(provider: str, request: Request) -> Any:
        del provider
        raw_body = await request.json()
        try:
            return _embeddings_payload(raw_body)
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": str(exc),
                        "type": "invalid_request_error",
                    }
                },
            )

    @router.post("/openai/{provider}/v1/responses")
    async def responses(
        provider: str,
        request: Request,
        handler: ChatHandler = Depends(get_chat_handler),
    ) -> Any:
        raw_body = resolve_openai_file_references(request.app, await request.json())
        chat_body = _responses_body_to_chat_body(raw_body)
        previous_response_id = str(raw_body.get("previous_response_id") or "").strip()
        if previous_response_id:
            resume_session_id = _lookup_responses_session(
                request.app,
                previous_response_id,
            )
            if resume_session_id:
                chat_body["resume_session_id"] = resume_session_id
            else:
                logger.info(
                    "[responses] previous_response_id=%s has no cached session",
                    previous_response_id,
                )
        tool_schemas = _responses_available_tool_schemas(raw_body.get("tools"))
        tool_names = set(tool_schemas)
        try:
            canonical_req = adapter.parse_request(provider, chat_body)
        except Exception as exc:
            logger.warning(
                "[responses] parse_request failed exc=%s | body_keys=%s | input_type=%s | n_messages=%d",
                exc,
                sorted(raw_body.keys()),
                type(raw_body.get("input")).__name__,
                len(chat_body.get("messages") or []),
            )
            status, payload = adapter.render_error(exc)
            return JSONResponse(status_code=status, content=payload)

        service = CanonicalChatService(handler)
        response_id = f"resp_{uuid.uuid4().hex}"
        created = int(time.time())

        if canonical_req.stream:

            async def sse_stream() -> AsyncIterator[str]:
                try:
                    raw_text = ""
                    yield (
                        "event: response.created\n"
                        f"data: {json.dumps({'type': 'response.created', 'response': _response_payload(response_id, canonical_req.model, '', created)}, ensure_ascii=False)}\n\n"
                    )
                    async for event in service.stream_raw(canonical_req):
                        if event.type == "content_delta" and event.content:
                            raw_text += event.content
                        elif event.type == "finish":
                            break
                    output_text, output_items = _responses_render_output_items(
                        raw_text,
                        tool_names=tool_names,
                        tool_schemas=tool_schemas,
                    )
                    session_id = _extract_responses_session_id(raw_text)
                    if session_id:
                        _store_responses_session(request.app, response_id, session_id)
                    for output_index, item in enumerate(output_items):
                        yield (
                            "event: response.output_item.added\n"
                            f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': output_index, 'item': item}, ensure_ascii=False)}\n\n"
                        )
                        if item.get("type") == "message":
                            content = item.get("content") or []
                            text = ""
                            if content and isinstance(content[0], dict):
                                text = str(content[0].get("text") or "")
                            if text:
                                yield (
                                    "event: response.output_text.delta\n"
                                    f"data: {json.dumps({'type': 'response.output_text.delta', 'item_id': item.get('id') or '', 'output_index': output_index, 'content_index': 0, 'delta': text}, ensure_ascii=False)}\n\n"
                                )
                        yield (
                            "event: response.output_item.done\n"
                            f"data: {json.dumps({'type': 'response.output_item.done', 'output_index': output_index, 'item': item}, ensure_ascii=False)}\n\n"
                        )
                    yield (
                        "event: response.completed\n"
                        f"data: {json.dumps({'type': 'response.completed', 'response': _response_payload(response_id, canonical_req.model, output_text, created, output=output_items)}, ensure_ascii=False)}\n\n"
                    )
                except Exception as exc:
                    logger.warning("[responses] stream failed exc=%s", exc)
                    _status, err_payload = adapter.render_error(exc)
                    error_obj: dict[str, Any] = {
                        "message": str(exc),
                        "code": "server_error",
                    }
                    if isinstance(err_payload, dict):
                        nested = err_payload.get("error")
                        if isinstance(nested, dict):
                            error_obj = {
                                "message": str(nested.get("message") or exc),
                                "code": str(nested.get("type") or "server_error"),
                            }
                    failed_response = _response_payload(
                        response_id, canonical_req.model, "", created
                    )
                    failed_response["status"] = "failed"
                    failed_response["error"] = error_obj
                    # OpenAI Responses API SSE: 失败时 yield response.failed 事件，
                    # 而非自定义 "event: error"——后者不在 alma/openai-sdk 的 Zod
                    # union 里，会导致客户端解析整段流崩溃。
                    yield (
                        "event: response.failed\n"
                        f"data: {json.dumps({'type': 'response.failed', 'response': failed_response}, ensure_ascii=False)}\n\n"
                    )

            return StreamingResponse(
                sse_stream(),
                media_type="text/event-stream",
                headers=STREAMING_HEADERS,
            )

        try:
            raw_events = await service.collect_raw(canonical_req)
        except Exception as exc:
            logger.warning("[responses] collect_raw failed exc=%s", exc)
            status, payload = adapter.render_error(exc)
            return JSONResponse(status_code=status, content=payload)
        text = "".join(
            event.content or ""
            for event in raw_events
            if event.type == "content_delta" and event.content
        )
        output_text, output_items = _responses_render_output_items(
            text,
            tool_names=tool_names,
            tool_schemas=tool_schemas,
        )
        session_id = _extract_responses_session_id(text)
        if session_id:
            _store_responses_session(request.app, response_id, session_id)
        return _response_payload(
            response_id,
            canonical_req.model,
            output_text,
            created,
            output=output_items,
        )

    return router
