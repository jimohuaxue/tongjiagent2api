"""Small in-process OpenAI-compatible file store."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from typing import Any

_FILE_STORE_ATTR = "openai_file_store"
_FILE_STORE_TTL_SECONDS = 24 * 60 * 60
_FILE_STORE_MAX_ENTRIES = 512
_LOCAL_FILE_MAX_BYTES = 20 * 1024 * 1024
_PDF_TEXT_MAX_CHARS = 120_000
_PDF_TEXT_TIMEOUT_SECONDS = 15
_PDF_TEXT_PART_MARKER = "pdf_text"
_LOCAL_PATH_SCAN_CHARS = 4096
_LOCAL_PATH_START_RE = re.compile(r"(?:~?/|/)")
_LOCAL_PATH_EXT_RE = re.compile(r"\.[A-Za-z0-9][A-Za-z0-9_+-]{0,15}")
_LOCAL_PATH_TERMINATORS = {'"', "'", "`", "<", ">", "\r", "\n", "\t"}
_LOCAL_PATH_TRAILING_CHARS = ".,;:!?，。；：！？、)]}"
_LOCAL_FILENAME_CANDIDATE_RE = re.compile(
    r"""(?P<name>[^<>"'\r\n\t]*?\.[A-Za-z0-9][A-Za-z0-9_+-]{0,15})"""
)
_LOCAL_FILENAME_SCAN_EXTENSIONS = {
    ".pdf",
    ".md",
    ".txt",
    ".csv",
    ".json",
    ".jsonl",
    ".docx",
    ".xlsx",
    ".pptx",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
}
_LOCAL_FILENAME_SEARCH_DIRS = (
    "Zotero/storage",
    "sync",
    "Documents",
    "Downloads",
    "Desktop",
)


@dataclass(slots=True)
class MultipartFile:
    field_name: str
    filename: str
    mime_type: str
    data: bytes


def store_openai_file(
    app: Any,
    *,
    filename: str,
    data: bytes,
    mime_type: str = "application/octet-stream",
    purpose: str = "assistants",
) -> dict[str, Any]:
    store = _file_store(app)
    now = int(time.time())
    _prune_file_store(store, now=now)
    digest = hashlib.sha256(data).hexdigest()
    file_id = f"file-{digest[:24]}"
    record = {
        "id": file_id,
        "object": "file",
        "bytes": len(data),
        "created_at": now,
        "filename": filename or "upload.bin",
        "purpose": purpose or "assistants",
        "mime_type": mime_type or "application/octet-stream",
        "data": data,
    }
    store[file_id] = record
    return record


def get_openai_file(app: Any, file_id: str | None) -> dict[str, Any] | None:
    if not file_id:
        return None
    store = _file_store(app)
    _prune_file_store(store)
    return store.get(file_id)


def list_openai_files(app: Any) -> list[dict[str, Any]]:
    store = _file_store(app)
    _prune_file_store(store)
    return [_file_metadata(record) for record in store.values()]


def delete_openai_file(app: Any, file_id: str) -> bool:
    store = _file_store(app)
    return store.pop(file_id, None) is not None


def openai_file_metadata(record: dict[str, Any]) -> dict[str, Any]:
    return _file_metadata(record)


def openai_file_to_data_part(record: dict[str, Any]) -> dict[str, str]:
    return {
        "file_data": base64.b64encode(record["data"]).decode("ascii"),
        "filename": str(record.get("filename") or "upload.bin"),
        "mime_type": str(record.get("mime_type") or "application/octet-stream"),
    }


def openai_file_part_to_chat_part(part: Any) -> dict[str, Any] | None:
    """Convert a loose OpenAI file/attachment object into a chat content part."""
    if isinstance(part, str):
        part = {"path": part}
    if not isinstance(part, dict):
        return None

    file_obj = part.get("file") if isinstance(part.get("file"), dict) else {}
    source_obj = part.get("source") if isinstance(part.get("source"), dict) else {}
    file_data = _first_file_data_value(
        part.get("file_data"),
        part.get("data"),
        part.get("content"),
        part.get("bytes"),
        part.get("base64"),
        part.get("file"),
        source_obj.get("data"),
        source_obj.get("text"),
    )
    filename = _filename_from_part(part, file_obj)
    mime_type = _mime_type_from_part(part, file_obj, source_obj)
    if file_data:
        if source_obj.get("type") == "text" and not _looks_like_data_or_base64(
            str(file_data)
        ):
            file_data = base64.b64encode(str(file_data).encode()).decode("ascii")
        return {
            "type": "file",
            "file_data": str(file_data),
            "filename": filename,
            "mime_type": mime_type,
        }

    path = _file_path_value(part) or _file_path_value(file_obj)
    if not path:
        return None
    prepared = _local_file_to_data_part(path)
    return {
        "type": "file",
        "file_data": prepared["file_data"],
        "filename": filename if filename != "input_file" else prepared["filename"],
        "mime_type": mime_type
        if mime_type != "application/octet-stream"
        else prepared["mime_type"],
        "source_path": str(Path(path).expanduser()),
    }


def anthropic_content_blocks_from_attachments(
    raw_body: dict[str, Any],
) -> list[dict[str, Any]]:
    """Convert top-level loose file attachments into Anthropic content blocks."""
    blocks: list[dict[str, Any]] = []
    for part in openai_file_parts_from_attachments(raw_body):
        block = _openai_file_chat_part_to_anthropic_block(part)
        if block is not None:
            blocks.append(block)
    return blocks


def anthropic_content_blocks_from_message_paths(
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    """Convert local file paths mentioned in user text into Anthropic blocks."""
    blocks: list[dict[str, Any]] = []
    for part in openai_file_parts_from_message_paths(message):
        block = _openai_file_chat_part_to_anthropic_block(part)
        if block is not None:
            blocks.append(block)
    return blocks


def openai_file_parts_from_attachments(raw_body: dict[str, Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for key in ("attachments", "files"):
        value = raw_body.get(key)
        if isinstance(value, list):
            items = value
        elif isinstance(value, (dict, str)):
            items = [value]
        else:
            continue
        for item in items:
            file_part = openai_file_part_to_chat_part(item)
            if file_part is not None:
                parts.append(file_part)
    return parts


def openai_file_parts_from_message_paths(
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    """Convert local file paths mentioned in message text into chat file parts."""
    parts: list[dict[str, Any]] = []
    for path in local_file_paths_from_message(message):
        file_part = openai_file_part_to_chat_part(path)
        if file_part is not None:
            parts.append(file_part)
    return parts


def local_file_paths_from_message(message: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for text in _message_text_values(message):
        for path in local_file_paths_from_text(text):
            key = str(Path(path).expanduser())
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
    return paths


def local_file_paths_from_text(text: str) -> list[str]:
    """Find existing local file paths embedded in user text.

    The scan is extension-driven so paths with spaces can be found without
    treating the following natural-language instruction as part of the path.
    """
    if not text:
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for match in _LOCAL_PATH_START_RE.finditer(text):
        start = match.start()
        if start > 0 and text[start - 1] in {":", "/"}:
            continue
        segment = _path_scan_segment(text[start : start + _LOCAL_PATH_SCAN_CHARS])
        for ext_match in _LOCAL_PATH_EXT_RE.finditer(segment):
            candidate = _normalize_text_path_value(segment[: ext_match.end()])
            if not candidate:
                continue
            resolved = Path(candidate).expanduser()
            key = str(resolved)
            if key in seen or not _is_readable_local_file(resolved):
                continue
            seen.add(key)
            paths.append(candidate)
            break
    for path in local_file_names_from_text(text):
        key = str(Path(path).expanduser())
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def local_file_names_from_text(text: str) -> list[str]:
    if not text:
        return []
    paths: list[str] = []
    seen_names: set[str] = set()
    for candidate in _filename_candidates_from_text(text):
        name = Path(candidate).name
        if name in seen_names:
            continue
        seen_names.add(name)
        resolved = _resolve_unique_local_filename(name)
        if resolved is not None:
            paths.append(str(resolved))
    return paths


def append_message_path_parts(
    messages: list[dict[str, Any]],
    *,
    protocol: str,
) -> None:
    """Attach files referenced by local paths in user message text."""
    for message in messages:
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "user") != "user":
            continue
        parts = (
            anthropic_content_blocks_from_message_paths(message)
            if protocol == "anthropic"
            else openai_file_parts_from_message_paths(message)
        )
        _append_parts_to_message(message, parts)


def append_pdf_text_parts_from_messages(messages: list[dict[str, Any]]) -> None:
    """Inject pdftotext output for PDF file parts so web models see the content."""
    for message in messages:
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "user") != "user":
            continue
        parts = _pdf_text_parts_from_message(message)
        _append_parts_to_message(message, parts)


def append_parts_to_last_user_message(
    messages: list[dict[str, Any]],
    parts: list[dict[str, Any]],
) -> None:
    if not parts:
        return
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "user") != "user":
            continue
        _append_parts_to_message(message, parts)
        return
    messages.append({"role": "user", "content": parts})


def parse_multipart_form(
    content_type: str,
    body: bytes,
) -> tuple[dict[str, str], list[MultipartFile]]:
    if "multipart/form-data" not in content_type.lower():
        raise ValueError("Content-Type must be multipart/form-data")
    header = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n"
        "\r\n"
    ).encode("utf-8")
    message = BytesParser(policy=default).parsebytes(header + body)
    fields: dict[str, str] = {}
    files: list[MultipartFile] = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = _decode_multipart_filename(part.get_filename())
        payload = part.get_payload(decode=True) or b""
        if filename:
            content_type = str(part.get_content_type() or "").lower()
            guessed_type = mimetypes.guess_type(filename)[0]
            mime_type = (
                guessed_type
                if content_type == "text/plain" and guessed_type
                else content_type or guessed_type or "application/octet-stream"
            )
            files.append(
                MultipartFile(
                    field_name=str(name),
                    filename=str(filename),
                    mime_type=mime_type,
                    data=payload,
                )
            )
        else:
            fields[str(name)] = payload.decode("utf-8", errors="replace")
    return fields, files


def _decode_multipart_filename(filename: str | None) -> str | None:
    if not filename:
        return filename
    try:
        return urllib.parse.unquote(filename)
    except Exception:
        return filename


def resolve_openai_file_references(app: Any, raw_body: dict[str, Any]) -> dict[str, Any]:
    """Expand OpenAI file_id references anywhere in a chat/responses request body."""
    resolved, changed = _resolve_value(app, raw_body)
    if changed and isinstance(resolved, dict):
        return resolved
    return raw_body


def _resolve_value(app: Any, value: Any) -> tuple[Any, bool]:
    if isinstance(value, list):
        items: list[Any] = []
        changed = False
        for item in value:
            resolved_item, item_changed = _resolve_value(app, item)
            items.append(resolved_item)
            changed = changed or item_changed
        return (items if changed else value), changed

    if not isinstance(value, dict):
        return value, False

    resolved_dict: dict[str, Any] = {}
    changed = False
    for key, nested in value.items():
        resolved_nested, nested_changed = _resolve_value(app, nested)
        resolved_dict[key] = resolved_nested
        changed = changed or nested_changed

    resolved_part = _resolve_file_part(app, resolved_dict)
    if resolved_part is not resolved_dict:
        return resolved_part, True
    return (resolved_dict if changed else value), changed


def _resolve_file_part(app: Any, part: dict[str, Any]) -> dict[str, Any]:
    part_type = str(part.get("type") or "")
    file_obj = part.get("file") if isinstance(part.get("file"), dict) else {}
    file_id = _file_id_from_part(part, file_obj)
    if not file_id:
        return part
    record = get_openai_file(app, file_id)
    if record is None:
        return part

    if part_type in {"image", "input_image"}:
        return _resolve_image_part(part, record)

    data_part = openai_file_to_data_part(record)
    return {
        **part,
        "type": part.get("type") or "file",
        "file_data": part.get("file_data") or data_part["file_data"],
        "filename": part.get("filename")
        or part.get("name")
        or part.get("file_name")
        or data_part["filename"],
        "mime_type": part.get("mime_type")
        or part.get("media_type")
        or part.get("mimeType")
        or data_part["mime_type"],
    }


def _file_data_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("file_data", "data", "content", "bytes", "base64"):
            nested = _file_data_value(value.get(key))
            if nested:
                return nested
    return None


def _first_file_data_value(*values: Any) -> str | None:
    for value in values:
        resolved = _file_data_value(value)
        if resolved:
            return resolved
    return None


def _message_text_values(message: dict[str, Any]) -> list[str]:
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []

    values: list[str] = []
    for item in content:
        if isinstance(item, str):
            values.append(item)
        elif isinstance(item, dict) and str(item.get("type") or "") in {
            "",
            "text",
            "input_text",
        }:
            if item.get("web2api_generated") == _PDF_TEXT_PART_MARKER:
                continue
            text = item.get("text")
            if isinstance(text, str):
                values.append(text)
    return values


def _filename_candidates_from_text(text: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if "/" in line or "\\" in line:
            continue
        normalized_line = line.strip()
        if not normalized_line:
            continue
        for match in _LOCAL_FILENAME_CANDIDATE_RE.finditer(normalized_line):
            candidate = _normalize_filename_candidate(match.group("name"))
            if not candidate:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            candidates.append(candidate)
    return candidates


def _normalize_filename_candidate(value: str) -> str:
    candidate = value.strip()
    while candidate and candidate[0] in "([{\"'`":
        candidate = candidate[1:].lstrip()
    while candidate and candidate[-1] in _LOCAL_PATH_TRAILING_CHARS + "\"'`":
        candidate = candidate[:-1].rstrip()
    if not candidate or "/" in candidate or "\\" in candidate:
        return ""
    suffix = Path(candidate).suffix.lower()
    if suffix not in _LOCAL_FILENAME_SCAN_EXTENSIONS:
        return ""
    return Path(candidate).name


def _resolve_unique_local_filename(filename: str) -> Path | None:
    for root in _filename_search_roots():
        matches = [
            path for path in _find_files_by_name(root, filename, limit=32)
            if _is_readable_local_file(path)
        ]
        if not matches:
            continue
        return max(matches, key=lambda path: path.stat().st_mtime)
    return None


def _filename_search_roots() -> list[Path]:
    roots: list[Path] = [Path.cwd()]
    home = Path.home()
    for relative in _LOCAL_FILENAME_SEARCH_DIRS:
        roots.append(home / relative)
    out: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            resolved = root.expanduser().resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen or not resolved.is_dir():
            continue
        seen.add(key)
        out.append(resolved)
    return out


def _find_files_by_name(root: Path, filename: str, *, limit: int) -> list[Path]:
    if limit <= 0:
        return []
    matches: list[Path] = []
    target = filename.casefold()
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            item
            for item in dirnames
            if item not in {".git", ".venv", "node_modules", "__pycache__"}
        ]
        for item in filenames:
            if item.casefold() != target:
                continue
            matches.append(Path(current_root) / item)
            if len(matches) >= limit:
                return matches
    return matches


def _path_scan_segment(text: str) -> str:
    chars: list[str] = []
    for char in text:
        if char in _LOCAL_PATH_TERMINATORS:
            break
        chars.append(char)
    return "".join(chars)


def _normalize_text_path_value(value: str) -> str:
    candidate = value.strip()
    while candidate and candidate[-1] in _LOCAL_PATH_TRAILING_CHARS:
        candidate = candidate[:-1].rstrip()
    return candidate


def _is_readable_local_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size <= _LOCAL_FILE_MAX_BYTES
    except OSError:
        return False


def _append_parts_to_message(
    message: dict[str, Any],
    parts: list[dict[str, Any]],
) -> None:
    if not parts:
        return
    content = message.get("content")
    if isinstance(content, list):
        existing = {
            fingerprint
            for item in content
            if isinstance(item, dict)
            for fingerprint in [_content_part_fingerprint(item)]
            if fingerprint is not None
        }
        new_parts: list[dict[str, Any]] = []
        for part in parts:
            fingerprint = _content_part_fingerprint(part)
            if fingerprint is not None and fingerprint in existing:
                continue
            if fingerprint is not None:
                existing.add(fingerprint)
            new_parts.append(part)
        content.extend(new_parts)
    elif isinstance(content, str) and content:
        message["content"] = [{"type": "text", "text": content}, *parts]
    else:
        message["content"] = parts


def _pdf_text_parts_from_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return []

    parts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in content:
        if not isinstance(item, dict):
            continue
        part = _pdf_text_part_from_content_part(item)
        if part is None:
            continue
        fingerprint = _content_part_fingerprint(part)
        if fingerprint is not None and fingerprint in seen:
            continue
        if fingerprint is not None:
            seen.add(fingerprint)
        parts.append(part)
    return parts


def _pdf_text_part_from_content_part(part: dict[str, Any]) -> dict[str, Any] | None:
    part_type = str(part.get("type") or "")
    if part_type in {"file", "input_file"}:
        return _pdf_text_part_from_openai_file_part(part)
    if part_type == "document":
        return _pdf_text_part_from_anthropic_document_part(part)
    return None


def _pdf_text_part_from_openai_file_part(
    part: dict[str, Any],
) -> dict[str, Any] | None:
    file_obj = part.get("file") if isinstance(part.get("file"), dict) else {}
    filename = _filename_from_part(part, file_obj)
    mime_type = _mime_type_from_part(part, file_obj)
    source_path = str(part.get("source_path") or "").strip()
    if not _is_pdf_file_reference(
        filename=filename,
        mime_type=mime_type,
        source_path=source_path,
    ):
        return None

    if source_path:
        text = _extract_pdf_text_from_path(source_path)
        return _pdf_text_part(
            filename=filename,
            source_path=source_path,
            text=text,
            digest=_pdf_source_digest(source_path=source_path),
        )

    data_value = _first_file_data_value(
        part.get("file_data"),
        part.get("data"),
        part.get("content"),
        part.get("bytes"),
        part.get("base64"),
        part.get("file"),
        file_obj.get("file_data"),
        file_obj.get("data"),
        file_obj.get("content"),
        file_obj.get("bytes"),
        file_obj.get("base64"),
    )
    data = _pdf_bytes_from_data_value(data_value, fallback_mime_type=mime_type)
    if data is None:
        return None
    text = _extract_pdf_text_from_bytes(data)
    return _pdf_text_part(
        filename=filename,
        source_path="",
        text=text,
        digest=hashlib.sha256(data).hexdigest(),
    )


def _pdf_text_part_from_anthropic_document_part(
    part: dict[str, Any],
) -> dict[str, Any] | None:
    source = part.get("source") if isinstance(part.get("source"), dict) else {}
    filename = str(part.get("title") or part.get("filename") or "input_file")
    mime_type = str(
        source.get("media_type")
        or source.get("mime_type")
        or source.get("mimeType")
        or "application/octet-stream"
    )
    if not _is_pdf_file_reference(
        filename=filename,
        mime_type=mime_type,
        source_path="",
    ):
        return None
    if source.get("type") != "base64":
        return None
    data = _pdf_bytes_from_data_value(source.get("data"), fallback_mime_type=mime_type)
    if data is None:
        return None
    text = _extract_pdf_text_from_bytes(data)
    return _pdf_text_part(
        filename=filename,
        source_path="",
        text=text,
        digest=hashlib.sha256(data).hexdigest(),
    )


def _is_pdf_file_reference(
    *,
    filename: str,
    mime_type: str,
    source_path: str,
) -> bool:
    media_type = mime_type.split(";", 1)[0].strip().lower()
    if media_type == "application/pdf":
        return True
    if Path(filename).suffix.lower() == ".pdf":
        return True
    return bool(source_path and Path(source_path).suffix.lower() == ".pdf")


def _pdf_text_part(
    *,
    filename: str,
    source_path: str,
    text: str | None,
    digest: str,
) -> dict[str, Any] | None:
    if not text:
        return None
    path_line = f"\n路径: {source_path}" if source_path else ""
    return {
        "type": "text",
        "text": (
            "\n\n[web2api 已从 PDF 提取正文]\n"
            f"文件名: {filename}"
            f"{path_line}\n"
            "以下是 pdftotext 提取的 PDF 正文。请直接基于这段正文回答，"
            "不要再调用文件搜索工具查找同名 PDF。\n\n"
            f"{text}\n"
            "[/web2api 已从 PDF 提取正文]"
        ),
        "web2api_generated": _PDF_TEXT_PART_MARKER,
        "web2api_pdf_digest": digest,
        "web2api_pdf_filename": filename,
    }


def _pdf_source_digest(*, source_path: str) -> str:
    path = Path(source_path).expanduser()
    try:
        resolved = path.resolve()
        stat = resolved.stat()
        value = f"{resolved}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        value = str(path)
    return hashlib.sha256(value.encode()).hexdigest()


def _pdf_bytes_from_data_value(
    value: Any,
    *,
    fallback_mime_type: str,
) -> bytes | None:
    data_value = _file_data_value(value)
    if not data_value:
        return None
    data_b64, _media_type = _base64_payload_and_mime(data_value, fallback_mime_type)
    try:
        data = base64.b64decode(data_b64, validate=True)
    except Exception:
        return None
    if len(data) > _LOCAL_FILE_MAX_BYTES:
        return None
    return data


def _extract_pdf_text_from_bytes(data: bytes) -> str | None:
    if not data or len(data) > _LOCAL_FILE_MAX_BYTES:
        return None
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix="web2api_pdf_",
            suffix=".pdf",
            delete=False,
        ) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        return _extract_pdf_text_from_path(tmp_path)
    except OSError:
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _extract_pdf_text_from_path(path_value: str | Path) -> str | None:
    path = Path(path_value).expanduser()
    try:
        if not path.is_file() or path.stat().st_size > _LOCAL_FILE_MAX_BYTES:
            return None
        result = subprocess.run(
            ["pdftotext", str(path), "-"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_PDF_TEXT_TIMEOUT_SECONDS,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 and not result.stdout.strip():
        return None
    return _normalize_pdf_text(result.stdout)


def _normalize_pdf_text(text: str) -> str | None:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n")
    normalized = re.sub(r"\n{4,}", "\n\n\n", normalized).strip()
    if not normalized:
        return None
    if len(normalized) <= _PDF_TEXT_MAX_CHARS:
        return normalized
    return (
        normalized[:_PDF_TEXT_MAX_CHARS].rstrip()
        + f"\n\n[PDF 正文过长，已截断到前 {_PDF_TEXT_MAX_CHARS} 个字符]"
    )


def _content_part_fingerprint(part: dict[str, Any]) -> tuple[str, str, str, str] | None:
    part_type = str(part.get("type") or "")
    if part_type in {"text", "input_text"} and (
        part.get("web2api_generated") == _PDF_TEXT_PART_MARKER
    ):
        return (
            "text",
            _PDF_TEXT_PART_MARKER,
            str(part.get("web2api_pdf_digest") or ""),
            str(part.get("web2api_pdf_filename") or ""),
        )
    if part_type in {"file", "input_file"}:
        return (
            part_type,
            str(part.get("filename") or part.get("name") or part.get("file_name") or ""),
            str(part.get("mime_type") or part.get("media_type") or part.get("mimeType") or ""),
            str(part.get("file_data") or part.get("data") or ""),
        )
    if part_type in {"document", "image"}:
        source = part.get("source") if isinstance(part.get("source"), dict) else {}
        return (
            part_type,
            str(part.get("title") or ""),
            str(
                source.get("media_type")
                or source.get("mime_type")
                or source.get("mimeType")
                or ""
            ),
            str(source.get("data") or ""),
        )
    return None


def _filename_from_part(part: dict[str, Any], file_obj: dict[str, Any]) -> str:
    return str(
        part.get("filename")
        or part.get("name")
        or part.get("file_name")
        or part.get("title")
        or file_obj.get("filename")
        or file_obj.get("name")
        or file_obj.get("file_name")
        or file_obj.get("title")
        or "input_file"
    )


def _mime_type_from_part(
    part: dict[str, Any],
    file_obj: dict[str, Any],
    source_obj: dict[str, Any] | None = None,
) -> str:
    source_obj = source_obj or {}
    mime_type = (
        part.get("mime_type")
        or part.get("media_type")
        or part.get("mimeType")
        or file_obj.get("mime_type")
        or file_obj.get("media_type")
        or file_obj.get("mimeType")
        or source_obj.get("media_type")
        or source_obj.get("mime_type")
        or source_obj.get("mimeType")
        or "application/octet-stream"
    )
    if str(mime_type) in {"file", "input_file"}:
        mime_type = "application/octet-stream"
    mime_type_text = str(mime_type)
    if mime_type_text == "application/octet-stream":
        guessed_type = mimetypes.guess_type(_filename_from_part(part, file_obj))[0]
        if guessed_type:
            return guessed_type
    return mime_type_text


def _openai_file_chat_part_to_anthropic_block(
    part: dict[str, Any],
) -> dict[str, Any] | None:
    data_value = str(part.get("file_data") or "")
    if not data_value:
        return None
    fallback_mime_type = str(part.get("mime_type") or "application/octet-stream")
    data, media_type = _base64_payload_and_mime(data_value, fallback_mime_type)
    filename = str(part.get("filename") or "input_file")
    source = {
        "type": "base64",
        "media_type": media_type,
        "data": data,
    }
    if media_type.split(";", 1)[0].lower().startswith("image/"):
        return {"type": "image", "source": source}
    return {"type": "document", "title": filename, "source": source}


def _base64_payload_and_mime(
    data_value: str,
    fallback_mime_type: str,
) -> tuple[str, str]:
    data_text = data_value.strip()
    if data_text.startswith("data:"):
        header, separator, payload = data_text.partition(",")
        if separator:
            header_value = header[5:]
            media_type = header_value.split(";", 1)[0] or fallback_mime_type
            if ";base64" in header_value.lower():
                return "".join(payload.split()), media_type
            return (
                base64.b64encode(urllib.parse.unquote_to_bytes(payload)).decode(
                    "ascii"
                ),
                media_type,
            )
    compact = "".join(data_text.split())
    try:
        base64.b64decode(compact, validate=True)
        return compact, fallback_mime_type
    except Exception:
        return (
            base64.b64encode(data_value.encode()).decode("ascii"),
            fallback_mime_type,
        )


def _looks_like_data_or_base64(value: str) -> bool:
    if value.startswith("data:"):
        return True
    compact = "".join(value.split())
    if not compact:
        return False
    try:
        base64.b64decode(compact, validate=True)
    except Exception:
        return False
    return True


def _file_path_value(part: dict[str, Any]) -> str | None:
    for key in ("path", "file_path", "filepath", "local_path"):
        value = part.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _local_file_to_data_part(path_value: str) -> dict[str, str]:
    path = Path(path_value).expanduser()
    if not path.is_file():
        raise ValueError(f"file attachment path not found: {path_value}")
    if path.stat().st_size > _LOCAL_FILE_MAX_BYTES:
        raise ValueError(
            f"file attachment cannot exceed {_LOCAL_FILE_MAX_BYTES // 1024 // 1024}MB"
        )
    data = path.read_bytes()
    guessed_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return {
        "file_data": base64.b64encode(data).decode("ascii"),
        "filename": path.name,
        "mime_type": guessed_type,
    }


def _file_id_from_part(part: dict[str, Any], file_obj: dict[str, Any]) -> str:
    part_type = str(part.get("type") or "")
    is_file_like = part_type in {"file", "input_file", "image", "input_image"} or (
        "file_id" in part
    )
    if not is_file_like:
        return ""
    file_id = (
        part.get("file_id")
        or (part.get("id") if part_type in {"file", "input_file"} else None)
        or file_obj.get("file_id")
        or file_obj.get("id")
    )
    return str(file_id or "").strip()


def _resolve_image_part(part: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    mime_type = str(record.get("mime_type") or "application/octet-stream")
    data = base64.b64encode(record["data"]).decode("ascii")
    data_url = f"data:{mime_type};base64,{data}"
    part_type = str(part.get("type") or "")
    return {
        **part,
        "type": "image_url" if part_type == "image" else part.get("type") or "input_image",
        "image_url": {"url": data_url},
        "url": data_url,
    }


def _file_store(app: Any) -> dict[str, dict[str, Any]]:
    store = getattr(app.state, _FILE_STORE_ATTR, None)
    if not isinstance(store, dict):
        store = {}
        setattr(app.state, _FILE_STORE_ATTR, store)
    return store


def _file_metadata(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "object": "file",
        "bytes": int(record.get("bytes") or 0),
        "created_at": int(record.get("created_at") or 0),
        "filename": str(record.get("filename") or ""),
        "purpose": str(record.get("purpose") or "assistants"),
        "status": "processed",
    }


def _prune_file_store(
    store: dict[str, dict[str, Any]],
    *,
    now: int | None = None,
) -> None:
    current = int(time.time() if now is None else now)
    stale_ids = [
        file_id
        for file_id, record in store.items()
        if current - int(record.get("created_at") or 0) > _FILE_STORE_TTL_SECONDS
    ]
    for file_id in stale_ids:
        store.pop(file_id, None)
    overflow = len(store) - _FILE_STORE_MAX_ENTRIES
    if overflow <= 0:
        return
    oldest = sorted(store.items(), key=lambda item: int(item[1].get("created_at") or 0))
    for file_id, _record in oldest[:overflow]:
        store.pop(file_id, None)
