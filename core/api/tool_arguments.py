"""Tool argument normalization helpers."""

from __future__ import annotations

import re
from typing import Any


_ALIASES = {
    "cmd": {"command", "shell", "script", "bash", "input"},
    "command": {"cmd", "shell", "script", "bash", "input"},
    "path": {"file_path", "filepath", "filename", "file"},
    "file_path": {"path", "filepath", "filename", "file"},
    "pattern": {"query", "regex", "regexp", "glob"},
    "query": {"pattern", "search", "keyword", "keywords", "q"},
    "content": {"text", "body", "value"},
    "text": {"content", "body", "value"},
    "url": {"uri", "link", "href"},
}


def normalize_tool_arguments_for_schema(
    arguments: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    arguments = _unwrap_json_string_arguments(arguments, schema)
    properties = tool_schema_properties(schema)
    if not arguments or not properties:
        return arguments

    normalized: dict[str, Any] = {}
    unknown: dict[str, Any] = {}
    for raw_key, value in arguments.items():
        key = str(raw_key).strip()
        prop = match_tool_schema_property(key, properties)
        if prop:
            normalized[prop] = value
        else:
            unknown[key] = value

    missing_required = [
        prop for prop in tool_schema_required(schema) if prop not in normalized
    ]
    for prop in missing_required:
        candidate = select_unknown_argument_for_property(
            prop,
            unknown,
            properties,
            missing_required,
        )
        if candidate is not None:
            normalized[prop] = unknown.pop(candidate)

    normalized.update(unknown)
    normalized = _coerce_known_argument_values(normalized, properties)
    return normalized


def normalize_tool_call_arguments(
    name: str,
    arguments: dict[str, Any],
    tool_schemas: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return normalize_tool_arguments_for_schema(arguments, tool_schemas.get(name) or {})


def tool_call_arguments_are_complete(
    name: str,
    arguments: dict[str, Any],
    tool_schemas: dict[str, dict[str, Any]],
) -> bool:
    schema = tool_schemas.get(name) or {}
    required = tool_schema_required(schema)
    if not required:
        return True
    return all(prop in arguments for prop in required)


def tool_schema_properties(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in properties.items():
        if isinstance(key, str) and isinstance(value, dict):
            out[key] = value
    return out


def tool_schema_required(schema: dict[str, Any]) -> list[str]:
    required = schema.get("required") if isinstance(schema, dict) else None
    if not isinstance(required, list):
        return []
    return [item for item in required if isinstance(item, str)]


def match_tool_schema_property(
    key: str,
    properties: dict[str, dict[str, Any]],
) -> str | None:
    if key in properties:
        return key
    lowered = _canonical_key(key)
    for prop in sorted(properties, key=len, reverse=True):
        prop_lower = _canonical_key(prop)
        if lowered == prop_lower:
            return prop
        if lowered == prop_lower + prop_lower:
            return prop
        if lowered in _ALIASES.get(prop_lower, set()):
            return prop
        if prop_lower in _ALIASES.get(lowered, set()):
            return prop
        if (
            len(prop_lower) >= 3
            and lowered.startswith(prop_lower)
            and lowered.endswith(prop_lower)
        ):
            return prop
    return None


def select_unknown_argument_for_property(
    prop: str,
    unknown: dict[str, Any],
    properties: dict[str, dict[str, Any]],
    missing_required: list[str],
) -> str | None:
    if not unknown:
        return None
    prop_schema = properties.get(prop) or {}
    prop_lower = _canonical_key(prop)
    for key, value in unknown.items():
        key_lower = _canonical_key(key)
        if (
            (
                prop_lower in key_lower
                or key_lower in _ALIASES.get(prop_lower, set())
                or prop_lower in _ALIASES.get(key_lower, set())
            )
            and len(prop_lower) >= 3
            and value_matches_tool_schema(value, prop_schema)
        ):
            return key
    if len(missing_required) != 1:
        return None
    candidates = [
        key
        for key, value in unknown.items()
        if value_matches_tool_schema(value, prop_schema)
    ]
    return candidates[0] if len(candidates) == 1 else None


def value_matches_tool_schema(value: Any, prop_schema: dict[str, Any]) -> bool:
    raw_type = prop_schema.get("type")
    allowed_types = raw_type if isinstance(raw_type, list) else [raw_type]
    if "string" in allowed_types:
        return isinstance(value, str)
    if "integer" in allowed_types:
        return isinstance(value, int) and not isinstance(value, bool)
    if "number" in allowed_types:
        return isinstance(value, int | float) and not isinstance(value, bool)
    if "boolean" in allowed_types:
        return isinstance(value, bool)
    if "array" in allowed_types:
        return isinstance(value, list)
    if "object" in allowed_types:
        return isinstance(value, dict)
    return True


def _unwrap_json_string_arguments(
    arguments: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    properties = tool_schema_properties(schema)
    if len(arguments) != 1 or not properties:
        return arguments
    only_value = next(iter(arguments.values()))
    if not isinstance(only_value, str):
        return arguments
    stripped = only_value.strip()
    if not stripped.startswith("{"):
        return arguments
    try:
        parsed = _json_loads_object(stripped)
    except ValueError:
        return arguments
    if not parsed:
        return arguments
    if not any(match_tool_schema_property(str(key), properties) for key in parsed):
        return arguments
    return parsed


def _json_loads_object(value: str) -> dict[str, Any]:
    import json

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError from exc
    if not isinstance(parsed, dict):
        raise ValueError
    return parsed


def _coerce_known_argument_values(
    arguments: dict[str, Any],
    properties: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not properties:
        return arguments
    coerced: dict[str, Any] = {}
    for key, value in arguments.items():
        prop_schema = properties.get(key)
        coerced[key] = _coerce_value_for_schema(value, prop_schema or {})
    return coerced


def _coerce_value_for_schema(value: Any, prop_schema: dict[str, Any]) -> Any:
    raw_type = prop_schema.get("type")
    allowed_types = raw_type if isinstance(raw_type, list) else [raw_type]
    if isinstance(value, str):
        cleaned = _clean_tool_string_value(value)
        if "integer" in allowed_types:
            return _coerce_int(cleaned, value)
        if "number" in allowed_types:
            return _coerce_number(cleaned, value)
        if "boolean" in allowed_types:
            return _coerce_bool(cleaned, value)
        return cleaned
    return value


def _clean_tool_string_value(value: str) -> str:
    cleaned = value.strip()
    for terminal in (
        "</arg_value>",
        "</tool_call>",
        "</tool_calls>",
        "<arg_value>",
        "<tool_call>",
        "<tool_calls>",
        "tool_calls>",
        "tool_call>",
    ):
        index = cleaned.find(terminal)
        if index >= 0:
            cleaned = cleaned[:index].rstrip()
    while cleaned.endswith(("<", ">", '"')) and cleaned.count('"') % 2 == 1:
        cleaned = cleaned[:-1].rstrip()
    return cleaned.rstrip("<>").rstrip()


def _coerce_int(value: str, fallback: str) -> Any:
    try:
        return int(value)
    except ValueError:
        return fallback


def _coerce_number(value: str, fallback: str) -> Any:
    try:
        return int(value) if re.fullmatch(r"[+-]?\d+", value) else float(value)
    except ValueError:
        return fallback


def _coerce_bool(value: str, fallback: str) -> Any:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return fallback


def _canonical_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
