#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地中转代理（forward proxy）。

用途：
- 浏览器只配置无鉴权的本地代理：127.0.0.1:<port>
- 本地代理再转发到“带用户名密码鉴权”的上游代理（HTTP proxy）

实现重点：
- 支持 CONNECT（HTTPS 隧道）——浏览器最常见的代理用法
- 兼容少量 HTTP 明文请求（GET http://... 这种 absolute-form）
"""

from __future__ import annotations

import base64
import contextlib
import select
import socket
import socketserver
import threading
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse


def _basic_proxy_auth(username: str, password: str) -> str:
    raw = f"{username}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _recv_until(
    sock: socket.socket, marker: bytes, max_bytes: int = 256 * 1024
) -> bytes:
    data = bytearray()
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > max_bytes:
            break
    return bytes(data)


def _split_headers(data: bytes) -> tuple[bytes, bytes]:
    idx = data.find(b"\r\n\r\n")
    if idx < 0:
        return data, b""
    return data[: idx + 4], data[idx + 4 :]


def _parse_first_line(header_bytes: bytes) -> tuple[str, str, str]:
    # e.g. "CONNECT example.com:443 HTTP/1.1"
    first = header_bytes.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
    parts = first.strip().split()
    if len(parts) >= 3:
        return parts[0].upper(), parts[1], parts[2]
    if len(parts) == 2:
        return parts[0].upper(), parts[1], "HTTP/1.1"
    return "GET", "/", "HTTP/1.1"


def _remove_hop_by_hop_headers(header_bytes: bytes) -> bytes:
    # 仅做最小处理：去掉 Proxy-Authorization / Proxy-Connection，避免重复/冲突
    lines = header_bytes.split(b"\r\n")
    if not lines:
        return header_bytes
    out = [lines[0]]
    for line in lines[1:]:
        lower = line.lower()
        if lower.startswith(b"proxy-authorization:"):
            continue
        if lower.startswith(b"proxy-connection:"):
            continue
        out.append(line)
    return b"\r\n".join(out)


def _relay_bidi(a: socket.socket, b: socket.socket, stop_evt: threading.Event) -> None:
    a.setblocking(False)
    b.setblocking(False)
    socks = [a, b]
    try:
        while not stop_evt.is_set():
            r, _, _ = select.select(socks, [], [], 0.5)
            if not r:
                continue
            for s in r:
                try:
                    data = s.recv(65536)
                except BlockingIOError:
                    continue
                if not data:
                    stop_evt.set()
                    break
                other = b if s is a else a
                try:
                    other.sendall(data)
                except OSError:
                    stop_evt.set()
                    break
    finally:
        with contextlib.suppress(Exception):
            a.close()
        with contextlib.suppress(Exception):
            b.close()


class _ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


@dataclass(frozen=True)
class UpstreamProxy:
    host: str
    port: int
    username: str
    password: str

    @property
    def auth_header_value(self) -> str:
        return _basic_proxy_auth(self.username, self.password)


def parse_proxy_server(proxy_server: str) -> tuple[str, int]:
    """
    支持：
    - http://host:port
    - host:port
    """
    s = (proxy_server or "").strip()
    if not s:
        raise ValueError("proxy_server 为空")
    if "://" not in s:
        s = "http://" + s
    u = urlparse(s)
    if not u.hostname or not u.port:
        raise ValueError(f"无法解析 proxy_server: {proxy_server!r}")
    return u.hostname, int(u.port)


class LocalProxyForwarder:
    """
    启动一个本地 HTTP 代理，并把请求/隧道转发到上游代理（带 Basic 鉴权）。
    """

    def __init__(
        self,
        upstream: UpstreamProxy,
        *,
        listen_host: str = "127.0.0.1",
        listen_port: int = 0,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._upstream = upstream
        self._listen_host = listen_host
        self._listen_port = listen_port
        self._on_log = on_log

        self._server: _ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if not self._server:
            raise RuntimeError("forwarder 尚未启动")
        return int(self._server.server_address[1])

    @property
    def proxy_url(self) -> str:
        return f"http://{self._listen_host}:{self.port}"

    def _log(self, msg: str) -> None:
        if self._on_log:
            try:
                self._on_log(msg)
            except Exception:
                pass

    def start(self) -> "LocalProxyForwarder":
        if self._server is not None:
            return self

        upstream = self._upstream
        parent = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                client = self.request
                try:
                    data = _recv_until(client, b"\r\n\r\n")
                    if not data:
                        return
                    header, rest = _split_headers(data)
                    method, target, _ver = _parse_first_line(header)

                    upstream_sock = socket.create_connection(
                        (upstream.host, upstream.port), timeout=15
                    )
                    upstream_sock.settimeout(20)

                    if method == "CONNECT":
                        # 通过上游代理建立到 target 的隧道
                        connect_req = (
                            f"CONNECT {target} HTTP/1.1\r\n"
                            f"Host: {target}\r\n"
                            f"Proxy-Authorization: {upstream.auth_header_value}\r\n"
                            f"Proxy-Connection: keep-alive\r\n"
                            f"Connection: keep-alive\r\n"
                            f"\r\n"
                        ).encode("latin-1", errors="ignore")
                        upstream_sock.sendall(connect_req)
                        upstream_resp = _recv_until(upstream_sock, b"\r\n\r\n")
                        if not upstream_resp:
                            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                            return
                        # 将上游响应直接回给浏览器（一般是 200 Connection Established）
                        client.sendall(upstream_resp)

                        # CONNECT 时，header 后可能不会有 body；但如果有残留，丢给上游
                        if rest:
                            upstream_sock.sendall(rest)

                        stop_evt = threading.Event()
                        _relay_bidi(client, upstream_sock, stop_evt)
                        return

                    # 非 CONNECT：把请求转发给上游代理（absolute-form 请求）
                    # 注：这里只做最小实现，主要为兼容偶发 http:// 明文请求
                    filtered = _remove_hop_by_hop_headers(header)
                    # 插入 Proxy-Authorization
                    parts = filtered.split(b"\r\n")
                    out_lines = [parts[0]]
                    inserted = False
                    for line in parts[1:]:
                        if not inserted and line == b"":
                            out_lines.append(
                                f"Proxy-Authorization: {upstream.auth_header_value}".encode(
                                    "latin-1", errors="ignore"
                                )
                            )
                            inserted = True
                        out_lines.append(line)
                    new_header = b"\r\n".join(out_lines)
                    upstream_sock.sendall(new_header)
                    if rest:
                        upstream_sock.sendall(rest)

                    # 单向把响应回写给客户端直到连接关闭
                    while True:
                        chunk = upstream_sock.recv(65536)
                        if not chunk:
                            break
                        client.sendall(chunk)
                except Exception as e:
                    parent._log(f"[proxy] handler error: {e}")
                    with contextlib.suppress(Exception):
                        client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                finally:
                    with contextlib.suppress(Exception):
                        client.close()

        self._server = _ThreadingTCPServer(
            (self._listen_host, self._listen_port), Handler
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is None:
            return
        with contextlib.suppress(Exception):
            self._server.shutdown()
        with contextlib.suppress(Exception):
            self._server.server_close()
        self._server = None
        self._thread = None

    def __enter__(self) -> "LocalProxyForwarder":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()
