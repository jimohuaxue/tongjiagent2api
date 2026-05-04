import unittest
import tempfile
import os
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from typing import Any

from fastapi.responses import JSONResponse, StreamingResponse

from core.api import file_store
from core.api.file_store import store_openai_file
from core.api.protocol_routes import (
    format_anthropic_stream_error,
    format_openai_stream_error,
    handle_protocol_chat_request,
)
from core.hub.schemas import OpenAIStreamEvent
from core.protocol.base import ProtocolAdapter
from core.protocol.schemas import CanonicalChatRequest


class _FakeRequest:
    def __init__(self, body: dict[str, Any], app: Any | None = None) -> None:
        self._body = body
        self.app = app or SimpleNamespace(state=SimpleNamespace())

    async def json(self) -> dict[str, Any]:
        return self._body


class _FakeHandler:
    def __init__(self, events: list[OpenAIStreamEvent]) -> None:
        self._events = events
        self.calls: list[tuple[str, str]] = []

    async def stream_openai_events(
        self,
        provider: str,
        openai_req: Any,
    ) -> AsyncIterator[OpenAIStreamEvent]:
        self.calls.append((provider, openai_req.model))
        for event in self._events:
            yield event


class _CaptureHandler:
    def __init__(self) -> None:
        self.openai_req: Any | None = None

    async def stream_openai_events(
        self,
        provider: str,
        openai_req: Any,
    ) -> AsyncIterator[OpenAIStreamEvent]:
        del provider
        self.openai_req = openai_req
        yield OpenAIStreamEvent(type="content_delta", content="ok")
        yield OpenAIStreamEvent(type="finish", finish_reason="stop")


class _FakeAdapter(ProtocolAdapter):
    protocol_name = "fake"

    def __init__(self, *, stream_raises: bool = False) -> None:
        self._stream_raises = stream_raises

    def parse_request(
        self,
        provider: str,
        raw_body: dict[str, Any],
    ) -> CanonicalChatRequest:
        return CanonicalChatRequest(
            protocol="openai",
            provider=provider,
            model=str(raw_body.get("model") or "fake-model"),
            stream=bool(raw_body.get("stream") or False),
        )

    def render_non_stream(
        self,
        req: CanonicalChatRequest,
        raw_events: list[OpenAIStreamEvent],
    ) -> dict[str, Any]:
        text = "".join(event.content or "" for event in raw_events)
        return {"protocol": self.protocol_name, "provider": req.provider, "text": text}

    async def render_stream(
        self,
        req: CanonicalChatRequest,
        raw_stream: AsyncIterator[OpenAIStreamEvent],
    ) -> AsyncIterator[str]:
        async for event in raw_stream:
            if self._stream_raises:
                raise RuntimeError("stream failed")
            if event.content:
                yield f"chunk:{event.content}"

    def render_error(self, exc: Exception) -> tuple[int, dict[str, Any]]:
        return 500, {"error": str(exc)}


async def _collect_streaming_response(response: StreamingResponse) -> str:
    parts: list[str] = []
    async for chunk in response.body_iterator:
        parts.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return "".join(parts)


class TestProtocolRouteHelper(unittest.IsolatedAsyncioTestCase):
    async def test_handle_protocol_chat_request_non_stream(self) -> None:
        adapter = _FakeAdapter()
        handler = _FakeHandler(
            [OpenAIStreamEvent(type="content_delta", content="hello world")]
        )

        response = await handle_protocol_chat_request(
            adapter=adapter,
            provider="demo",
            request=_FakeRequest({"model": "m1", "stream": False}),
            handler=handler,
            stream_error_formatter=format_openai_stream_error,
        )

        self.assertEqual(
            response,
            {"protocol": "fake", "provider": "demo", "text": "hello world"},
        )
        self.assertEqual(handler.calls, [("demo", "m1")])

    async def test_openai_chat_top_level_pdf_attachment_is_forwarded(self) -> None:
        from core.protocol.openai import OpenAIProtocolAdapter

        handler = _CaptureHandler()
        response = await handle_protocol_chat_request(
            adapter=OpenAIProtocolAdapter(),
            provider="demo",
            request=_FakeRequest(
                {
                    "model": "m1",
                    "stream": False,
                    "messages": [{"role": "user", "content": "总结这个 pdf"}],
                    "attachments": [
                        {
                            "filename": "paper.pdf",
                            "mime_type": "application/pdf",
                            "data": "data:application/pdf;base64,JVBERi0xLjQK",
                        }
                    ],
                }
            ),
            handler=handler,  # type: ignore[arg-type]
            stream_error_formatter=format_openai_stream_error,
        )

        self.assertEqual(response["choices"][0]["message"]["content"], "ok")
        self.assertIsNotNone(handler.openai_req)
        attachment = handler.openai_req.attachment_files_last_user[0]
        self.assertEqual(attachment.filename, "paper.pdf")
        self.assertEqual(attachment.mime_type, "application/pdf")
        self.assertEqual(attachment.data, b"%PDF-1.4\n")

    async def test_openai_chat_top_level_file_id_attachment_is_forwarded(self) -> None:
        from core.protocol.openai import OpenAIProtocolAdapter

        app = SimpleNamespace(state=SimpleNamespace())
        record = store_openai_file(
            app,
            filename="paper.pdf",
            mime_type="application/pdf",
            data=b"%PDF-1.4\n",
        )
        handler = _CaptureHandler()

        await handle_protocol_chat_request(
            adapter=OpenAIProtocolAdapter(),
            provider="demo",
            request=_FakeRequest(
                {
                    "model": "m1",
                    "stream": False,
                    "messages": [{"role": "user", "content": "总结这个 pdf"}],
                    "attachments": [{"file_id": record["id"]}],
                },
                app=app,
            ),
            handler=handler,  # type: ignore[arg-type]
            stream_error_formatter=format_openai_stream_error,
        )

        self.assertIsNotNone(handler.openai_req)
        attachment = handler.openai_req.attachment_files_last_user[0]
        self.assertEqual(attachment.filename, "paper.pdf")
        self.assertEqual(attachment.mime_type, "application/pdf")
        self.assertEqual(attachment.data, b"%PDF-1.4\n")

    async def test_openai_chat_top_level_path_attachment_is_forwarded(self) -> None:
        from core.protocol.openai import OpenAIProtocolAdapter

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            handler = _CaptureHandler()

            await handle_protocol_chat_request(
                adapter=OpenAIProtocolAdapter(),
                provider="demo",
                request=_FakeRequest(
                    {
                        "model": "m1",
                        "stream": False,
                        "messages": [{"role": "user", "content": "总结这个 pdf"}],
                        "attachments": [{"path": str(pdf_path)}],
                    }
                ),
                handler=handler,  # type: ignore[arg-type]
                stream_error_formatter=format_openai_stream_error,
            )

        self.assertIsNotNone(handler.openai_req)
        attachment = handler.openai_req.attachment_files_last_user[0]
        self.assertEqual(attachment.filename, "paper.pdf")
        self.assertEqual(attachment.mime_type, "application/pdf")
        self.assertEqual(attachment.data, b"%PDF-1.4\n")

    async def test_openai_chat_text_path_attachment_is_forwarded(self) -> None:
        from core.protocol.openai import OpenAIProtocolAdapter

        with tempfile.TemporaryDirectory() as tmp:
            md_path = Path(tmp) / "Alma 介绍.md"
            md_path.write_text("# Alma\n\n调用和检修方式\n", encoding="utf-8")
            handler = _CaptureHandler()

            await handle_protocol_chat_request(
                adapter=OpenAIProtocolAdapter(),
                provider="demo",
                request=_FakeRequest(
                    {
                        "model": "m1",
                        "stream": False,
                        "messages": [
                            {
                                "role": "user",
                                "content": f"阅读这个{md_path} 增加调用和检修方式",
                            }
                        ],
                    }
                ),
                handler=handler,  # type: ignore[arg-type]
                stream_error_formatter=format_openai_stream_error,
            )

        self.assertIsNotNone(handler.openai_req)
        attachment = handler.openai_req.attachment_files_last_user[0]
        self.assertEqual(attachment.filename, "Alma 介绍.md")
        self.assertEqual(attachment.mime_type, "text/markdown")
        self.assertEqual(attachment.data, "# Alma\n\n调用和检修方式\n".encode())

    async def test_openai_chat_bare_pdf_filename_attachment_is_forwarded(self) -> None:
        from core.protocol.openai import OpenAIProtocolAdapter

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = (
                Path(tmp)
                / "Coenen 等 - 2022 - Analysis of Thermal Crosstalk in Photonic Integrated Circuit Using Dynamic Compact Models.pdf"
            )
            pdf_path.write_bytes(b"%PDF-1.4\n")
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                handler = _CaptureHandler()

                await handle_protocol_chat_request(
                    adapter=OpenAIProtocolAdapter(),
                    provider="demo",
                    request=_FakeRequest(
                        {
                            "model": "m1",
                            "stream": False,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": (
                                        "总结这个pdf\n"
                                        "Coenen 等 - 2022 - Analysis of Thermal Crosstalk in Photonic Integrated Circuit Using Dynamic Compact Models.pdf"
                                    ),
                                }
                            ],
                        }
                    ),
                    handler=handler,  # type: ignore[arg-type]
                    stream_error_formatter=format_openai_stream_error,
                )
            finally:
                os.chdir(previous_cwd)

        self.assertIsNotNone(handler.openai_req)
        attachment = handler.openai_req.attachment_files_last_user[0]
        self.assertEqual(attachment.filename, pdf_path.name)
        self.assertEqual(attachment.mime_type, "application/pdf")
        self.assertEqual(attachment.data, b"%PDF-1.4\n")

    async def test_openai_chat_bare_pdf_filename_injects_extracted_text(self) -> None:
        from core.protocol.openai import OpenAIProtocolAdapter

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                handler = _CaptureHandler()

                with patch.object(
                    file_store,
                    "_extract_pdf_text_from_path",
                    return_value="Thermal crosstalk extracted from PDF.",
                ):
                    await handle_protocol_chat_request(
                        adapter=OpenAIProtocolAdapter(),
                        provider="demo",
                        request=_FakeRequest(
                            {
                                "model": "m1",
                                "stream": False,
                                "messages": [
                                    {
                                        "role": "user",
                                        "content": "pdf总结\npaper.pdf",
                                    }
                                ],
                            }
                        ),
                        handler=handler,  # type: ignore[arg-type]
                        stream_error_formatter=format_openai_stream_error,
                    )
            finally:
                os.chdir(previous_cwd)

        self.assertIsNotNone(handler.openai_req)
        content = handler.openai_req.messages[0].content
        self.assertIsInstance(content, list)
        text = "\n".join(part.text or "" for part in content if part.type == "text")
        self.assertIn("Thermal crosstalk extracted from PDF.", text)
        self.assertIn("不要再调用文件搜索工具", text)

    async def test_openai_chat_explicit_and_text_path_attachment_deduplicates(
        self,
    ) -> None:
        from core.protocol.openai import OpenAIProtocolAdapter

        with tempfile.TemporaryDirectory() as tmp:
            md_path = Path(tmp) / "Alma 介绍.md"
            md_path.write_text("# Alma\n", encoding="utf-8")
            handler = _CaptureHandler()

            await handle_protocol_chat_request(
                adapter=OpenAIProtocolAdapter(),
                provider="demo",
                request=_FakeRequest(
                    {
                        "model": "m1",
                        "stream": False,
                        "messages": [
                            {
                                "role": "user",
                                "content": f"阅读这个{md_path}",
                            }
                        ],
                        "attachments": [str(md_path)],
                    }
                ),
                handler=handler,  # type: ignore[arg-type]
                stream_error_formatter=format_openai_stream_error,
            )

        self.assertIsNotNone(handler.openai_req)
        self.assertEqual(len(handler.openai_req.attachment_files_last_user), 1)
        attachment = handler.openai_req.attachment_files_last_user[0]
        self.assertEqual(attachment.filename, "Alma 介绍.md")
        self.assertEqual(attachment.data, b"# Alma\n")

    async def test_anthropic_top_level_pdf_attachment_is_forwarded(self) -> None:
        from core.protocol.anthropic import AnthropicProtocolAdapter

        handler = _CaptureHandler()
        response = await handle_protocol_chat_request(
            adapter=AnthropicProtocolAdapter(),
            provider="demo",
            request=_FakeRequest(
                {
                    "model": "m1",
                    "stream": False,
                    "messages": [{"role": "user", "content": "总结这个 pdf"}],
                    "attachments": [
                        {
                            "filename": "paper.pdf",
                            "mime_type": "application/pdf",
                            "data": "data:application/pdf;base64,JVBERi0xLjQK",
                        }
                    ],
                }
            ),
            handler=handler,  # type: ignore[arg-type]
            stream_error_formatter=format_anthropic_stream_error,
        )

        self.assertEqual(response["content"][0]["text"], "ok")
        self.assertIsNotNone(handler.openai_req)
        attachment = handler.openai_req.attachment_files_last_user[0]
        self.assertEqual(attachment.filename, "paper.pdf")
        self.assertEqual(attachment.mime_type, "application/pdf")
        self.assertEqual(attachment.data, b"%PDF-1.4\n")

    async def test_anthropic_top_level_path_attachment_is_forwarded(self) -> None:
        from core.protocol.anthropic import AnthropicProtocolAdapter

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = (
                Path(tmp)
                / "Coenen 等 - 2022 - Analysis of Thermal Crosstalk.pdf"
            )
            pdf_path.write_bytes(b"%PDF-1.4\n")
            handler = _CaptureHandler()

            await handle_protocol_chat_request(
                adapter=AnthropicProtocolAdapter(),
                provider="demo",
                request=_FakeRequest(
                    {
                        "model": "m1",
                        "stream": False,
                        "messages": [{"role": "user", "content": "总结这个 pdf"}],
                        "attachments": [str(pdf_path)],
                    }
                ),
                handler=handler,  # type: ignore[arg-type]
                stream_error_formatter=format_anthropic_stream_error,
            )

        self.assertIsNotNone(handler.openai_req)
        attachment = handler.openai_req.attachment_files_last_user[0]
        self.assertEqual(
            attachment.filename,
            "Coenen 等 - 2022 - Analysis of Thermal Crosstalk.pdf",
        )
        self.assertEqual(attachment.mime_type, "application/pdf")
        self.assertEqual(attachment.data, b"%PDF-1.4\n")

    async def test_anthropic_bare_pdf_filename_attachment_is_forwarded(self) -> None:
        from core.protocol.anthropic import AnthropicProtocolAdapter

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = (
                Path(tmp)
                / "Coenen 等 - 2022 - Analysis of Thermal Crosstalk in Photonic Integrated Circuit Using Dynamic Compact Models.pdf"
            )
            pdf_path.write_bytes(b"%PDF-1.4\n")
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                handler = _CaptureHandler()

                await handle_protocol_chat_request(
                    adapter=AnthropicProtocolAdapter(),
                    provider="demo",
                    request=_FakeRequest(
                        {
                            "model": "m1",
                            "stream": False,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": (
                                        "总结这个pdf\n"
                                        "Coenen 等 - 2022 - Analysis of Thermal Crosstalk in Photonic Integrated Circuit Using Dynamic Compact Models.pdf"
                                    ),
                                }
                            ],
                        }
                    ),
                    handler=handler,  # type: ignore[arg-type]
                    stream_error_formatter=format_anthropic_stream_error,
                )
            finally:
                os.chdir(previous_cwd)

        self.assertIsNotNone(handler.openai_req)
        attachment = handler.openai_req.attachment_files_last_user[0]
        self.assertEqual(attachment.filename, pdf_path.name)
        self.assertEqual(attachment.mime_type, "application/pdf")
        self.assertEqual(attachment.data, b"%PDF-1.4\n")

    async def test_anthropic_bare_pdf_filename_injects_extracted_text(self) -> None:
        from core.protocol.anthropic import AnthropicProtocolAdapter

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                handler = _CaptureHandler()

                with patch.object(
                    file_store,
                    "_extract_pdf_text_from_path",
                    return_value="Anthropic PDF text extracted.",
                ):
                    await handle_protocol_chat_request(
                        adapter=AnthropicProtocolAdapter(),
                        provider="demo",
                        request=_FakeRequest(
                            {
                                "model": "m1",
                                "stream": False,
                                "messages": [
                                    {
                                        "role": "user",
                                        "content": "pdf总结\npaper.pdf",
                                    }
                                ],
                            }
                        ),
                        handler=handler,  # type: ignore[arg-type]
                        stream_error_formatter=format_anthropic_stream_error,
                    )
            finally:
                os.chdir(previous_cwd)

        self.assertIsNotNone(handler.openai_req)
        content = handler.openai_req.messages[0].content
        self.assertIsInstance(content, list)
        text = "\n".join(part.text or "" for part in content if part.type == "text")
        self.assertIn("Anthropic PDF text extracted.", text)
        self.assertIn("不要再调用文件搜索工具", text)

    async def test_anthropic_text_path_attachment_is_forwarded(self) -> None:
        from core.protocol.anthropic import AnthropicProtocolAdapter

        with tempfile.TemporaryDirectory() as tmp:
            md_path = Path(tmp) / "Alma 介绍.md"
            md_path.write_text("# Alma\n\n调用和检修方式\n", encoding="utf-8")
            handler = _CaptureHandler()

            await handle_protocol_chat_request(
                adapter=AnthropicProtocolAdapter(),
                provider="demo",
                request=_FakeRequest(
                    {
                        "model": "m1",
                        "stream": False,
                        "messages": [
                            {
                                "role": "user",
                                "content": f"阅读这个{md_path} 增加调用和检修方式",
                            }
                        ],
                    }
                ),
                handler=handler,  # type: ignore[arg-type]
                stream_error_formatter=format_anthropic_stream_error,
            )

        self.assertIsNotNone(handler.openai_req)
        attachment = handler.openai_req.attachment_files_last_user[0]
        self.assertEqual(attachment.filename, "Alma 介绍.md")
        self.assertEqual(attachment.mime_type, "text/markdown")
        self.assertEqual(attachment.data, "# Alma\n\n调用和检修方式\n".encode())

    async def test_anthropic_top_level_file_id_attachment_is_forwarded(self) -> None:
        from core.protocol.anthropic import AnthropicProtocolAdapter

        app = SimpleNamespace(state=SimpleNamespace())
        record = store_openai_file(
            app,
            filename="paper.pdf",
            mime_type="application/pdf",
            data=b"%PDF-1.4\n",
        )
        handler = _CaptureHandler()

        await handle_protocol_chat_request(
            adapter=AnthropicProtocolAdapter(),
            provider="demo",
            request=_FakeRequest(
                {
                    "model": "m1",
                    "stream": False,
                    "messages": [{"role": "user", "content": "总结这个 pdf"}],
                    "files": [{"file_id": record["id"]}],
                },
                app=app,
            ),
            handler=handler,  # type: ignore[arg-type]
            stream_error_formatter=format_anthropic_stream_error,
        )

        self.assertIsNotNone(handler.openai_req)
        attachment = handler.openai_req.attachment_files_last_user[0]
        self.assertEqual(attachment.filename, "paper.pdf")
        self.assertEqual(attachment.mime_type, "application/pdf")
        self.assertEqual(attachment.data, b"%PDF-1.4\n")

    async def test_anthropic_source_text_attachment_is_forwarded(self) -> None:
        from core.protocol.anthropic import AnthropicProtocolAdapter

        handler = _CaptureHandler()
        await handle_protocol_chat_request(
            adapter=AnthropicProtocolAdapter(),
            provider="demo",
            request=_FakeRequest(
                {
                    "model": "m1",
                    "stream": False,
                    "messages": [{"role": "user", "content": "总结这个文件"}],
                    "attachments": [
                        {
                            "title": "notes.txt",
                            "source": {"type": "text", "text": "hello"},
                        }
                    ],
                }
            ),
            handler=handler,  # type: ignore[arg-type]
            stream_error_formatter=format_anthropic_stream_error,
        )

        self.assertIsNotNone(handler.openai_req)
        attachment = handler.openai_req.attachment_files_last_user[0]
        self.assertEqual(attachment.filename, "notes.txt")
        self.assertEqual(attachment.mime_type, "text/plain")
        self.assertEqual(attachment.data, b"hello")

    async def test_handle_protocol_chat_request_openai_stream_error(self) -> None:
        adapter = _FakeAdapter(stream_raises=True)
        handler = _FakeHandler(
            [OpenAIStreamEvent(type="content_delta", content="hello world")]
        )

        response = await handle_protocol_chat_request(
            adapter=adapter,
            provider="demo",
            request=_FakeRequest({"model": "m1", "stream": True}),
            handler=handler,
            stream_error_formatter=format_openai_stream_error,
        )

        self.assertIsInstance(response, StreamingResponse)
        body = await _collect_streaming_response(response)
        self.assertEqual(body, 'data: {"error": "stream failed"}\n\n')

    async def test_handle_protocol_chat_request_anthropic_stream_error(self) -> None:
        adapter = _FakeAdapter(stream_raises=True)
        handler = _FakeHandler(
            [OpenAIStreamEvent(type="content_delta", content="hello world")]
        )

        response = await handle_protocol_chat_request(
            adapter=adapter,
            provider="demo",
            request=_FakeRequest({"model": "m1", "stream": True}),
            handler=handler,
            stream_error_formatter=format_anthropic_stream_error,
        )

        self.assertIsInstance(response, StreamingResponse)
        body = await _collect_streaming_response(response)
        self.assertEqual(body, 'event: error\ndata: {"error": "stream failed"}\n\n')

    async def test_parse_error_returns_json_response(self) -> None:
        class _ParseErrorAdapter(_FakeAdapter):
            def parse_request(
                self,
                provider: str,
                raw_body: dict[str, Any],
            ) -> CanonicalChatRequest:
                raise ValueError("bad request")

            def render_error(self, exc: Exception) -> tuple[int, dict[str, Any]]:
                return 400, {"error": str(exc)}

        response = await handle_protocol_chat_request(
            adapter=_ParseErrorAdapter(),
            provider="demo",
            request=_FakeRequest({"stream": False}),
            handler=_FakeHandler([]),
            stream_error_formatter=format_openai_stream_error,
        )

        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.body.decode(), '{"error":"bad request"}')


if __name__ == "__main__":
    unittest.main()
