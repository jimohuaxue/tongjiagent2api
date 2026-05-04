"""图片输入解析与下载。"""

from __future__ import annotations

import asyncio
import base64
import imghdr
import mimetypes
import urllib.parse
import urllib.request
from dataclasses import dataclass


SUPPORTED_IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024  # 文档/代码文件上限 20MB


@dataclass
class PreparedImage:
    filename: str
    mime_type: str
    data: bytes


def _validate_image_bytes(data: bytes, mime_type: str) -> None:
    if mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
        raise ValueError(f"暂不支持的图片类型: {mime_type}")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("单张图片不能超过 10MB")


def _default_filename(mime_type: str, *, prefix: str = "image") -> str:
    ext = mimetypes.guess_extension(mime_type) or ".bin"
    if ext == ".jpe":
        ext = ".jpg"
    return f"{prefix}{ext}"


def parse_data_url(url: str, *, prefix: str = "image") -> PreparedImage:
    if not url.startswith("data:") or ";base64," not in url:
        raise ValueError("仅支持 data:image/...;base64,... 格式")
    header, payload = url.split(",", 1)
    mime_type = header[5:].split(";", 1)[0].strip().lower()
    data = base64.b64decode(payload, validate=True)
    _validate_image_bytes(data, mime_type)
    return PreparedImage(
        filename=_default_filename(mime_type, prefix=prefix),
        mime_type=mime_type,
        data=data,
    )


def parse_base64_image(
    data_b64: str,
    mime_type: str,
    *,
    prefix: str = "image",
) -> PreparedImage:
    mime = mime_type.strip().lower()
    data = base64.b64decode(data_b64, validate=True)
    _validate_image_bytes(data, mime)
    return PreparedImage(
        filename=_default_filename(mime, prefix=prefix),
        mime_type=mime,
        data=data,
    )


def _sniff_mime_type(data: bytes, url: str) -> str:
    kind = imghdr.what(None, data)
    if kind == "jpeg":
        return "image/jpeg"
    if kind in {"png", "gif", "webp"}:
        return f"image/{kind}"
    guessed, _ = mimetypes.guess_type(url)
    return (guessed or "application/octet-stream").lower()


def _download_remote_image_sync(url: str, *, prefix: str = "image") -> PreparedImage:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("image_url 仅支持 http/https 或 data URL")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "web2api/1.0", "Accept": "image/*"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read(MAX_IMAGE_BYTES + 1)
        mime_type = str(resp.headers.get_content_type() or "").lower()
    if not mime_type or mime_type == "application/octet-stream":
        mime_type = _sniff_mime_type(data, url)
    _validate_image_bytes(data, mime_type)
    filename = urllib.parse.unquote(
        parsed.path.rsplit("/", 1)[-1]
    ) or _default_filename(mime_type, prefix=prefix)
    if "." not in filename:
        filename = _default_filename(mime_type, prefix=prefix)
    return PreparedImage(filename=filename, mime_type=mime_type, data=data)


async def download_remote_image(url: str, *, prefix: str = "image") -> PreparedImage:
    return await asyncio.to_thread(_download_remote_image_sync, url, prefix=prefix)


# ---------------------------------------------------------------------------
# 通用文件（代码/文档）解析，不限制 MIME 类型
# ---------------------------------------------------------------------------

def parse_file_data_url(url: str, *, prefix: str = "file") -> PreparedImage:
    """解析 data:...;base64,... 格式的任意文件（不限图片类型）。"""
    if not url.startswith("data:") or ";base64," not in url:
        raise ValueError("仅支持 data:...;base64,... 格式")
    header, payload = url.split(",", 1)
    mime_type = header[5:].split(";", 1)[0].strip().lower() or "application/octet-stream"
    data = base64.b64decode(payload, validate=True)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"文件不能超过 {MAX_FILE_BYTES // 1024 // 1024}MB")
    return PreparedImage(
        filename=_default_filename(mime_type, prefix=prefix),
        mime_type=mime_type,
        data=data,
    )


def parse_file_base64(
    data_b64: str,
    mime_type: str,
    *,
    prefix: str = "file",
) -> PreparedImage:
    """解析 base64 编码的任意文件。"""
    mime = mime_type.strip().lower() or "application/octet-stream"
    data = base64.b64decode(data_b64, validate=True)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"文件不能超过 {MAX_FILE_BYTES // 1024 // 1024}MB")
    return PreparedImage(filename=_default_filename(mime, prefix=prefix), mime_type=mime, data=data)
