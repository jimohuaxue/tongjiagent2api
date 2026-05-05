import json
import unittest
from unittest.mock import AsyncMock, patch

from core.api.schemas import InputAttachment
from core.plugin.helpers import stream_raw_via_page_fetch
from core.plugin.tongji import _detect_workspace_id
from core.plugin.tongji import _parse_tongji_sse_chunk
from core.plugin.tongji import _should_inline_attachment
from core.plugin.tongji import _workspace_id_from_url
from core.plugin.tongji import TongjiPlugin


class TestTongjiPlugin(unittest.TestCase):
    def test_parse_tongji_sse_content_chunk(self) -> None:
        texts, finish, error = _parse_tongji_sse_chunk(
            'data:{"choices":[{"delta":{"content":"你好"},"finish_reason":null}]}'
        )

        self.assertEqual(texts, ["你好"])
        self.assertIsNone(finish)
        self.assertIsNone(error)

    def test_parse_tongji_sse_done_chunk(self) -> None:
        texts, finish, error = _parse_tongji_sse_chunk("data:[DONE]")

        self.assertEqual(texts, [])
        self.assertEqual(finish, "done")
        self.assertIsNone(error)

    def test_parse_tongji_sse_error_chunk(self) -> None:
        texts, finish, error = _parse_tongji_sse_chunk(
            'data:{"error":{"code":"overloaded","message":"busy"}}'
        )

        self.assertEqual(texts, [])
        self.assertIsNone(finish)
        self.assertEqual(error, "overloaded: busy")

    def test_pdf_attachment_is_not_inlined(self) -> None:
        attachment = InputAttachment(
            filename="paper.pdf",
            mime_type="application/pdf",
            data=b"%PDF-1.4\n",
        )

        self.assertFalse(_should_inline_attachment(attachment))

    def test_text_attachment_is_inlined(self) -> None:
        attachment = InputAttachment(
            filename="notes.md",
            mime_type="text/markdown",
            data=b"# Notes\n",
        )

        self.assertTrue(_should_inline_attachment(attachment))

    def test_usage_model_falls_back_to_actual_default_model(self) -> None:
        plugin = TongjiPlugin()

        self.assertEqual(plugin.resolve_usage_model("gpt-5.5"), "glm-5.1")

    def test_usage_model_keeps_known_model_key(self) -> None:
        plugin = TongjiPlugin()

        self.assertEqual(plugin.resolve_usage_model("qwen3-vl-235b"), "qwen3-vl-235b")

    def test_workspace_id_is_extracted_from_platform_url(self) -> None:
        self.assertEqual(
            _workspace_id_from_url(
                "https://agent.tongji.edu.cn/product/llm/personal/personal-5724/application"
            ),
            "personal-5724",
        )
        self.assertEqual(_workspace_id_from_url("https://agent.tongji.edu.cn/login"), "")


class TestTongjiPdfUpload(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_detection_ignores_placeholder_url(self) -> None:
        fake_page = AsyncMock()
        fake_page.url = (
            "https://agent.tongji.edu.cn/product/maas/personal/personal-example/experience"
        )
        fake_page.evaluate = AsyncMock(return_value="personal-5724")

        self.assertEqual(await _detect_workspace_id(fake_page), "personal-5724")

    async def test_create_conversation_uses_csrf_headers(self) -> None:
        plugin = TongjiPlugin()
        fake_page = AsyncMock()
        fake_page.evaluate = AsyncMock(return_value="csrf-token")
        fake_context = object()
        plugin._context_config[id(fake_context)] = {  # noqa: SLF001
            "workspace_id": "workspace-1"
        }

        with patch(
            "core.plugin.tongji.request_json_via_page_fetch",
            new=AsyncMock(
                return_value={
                    "json": {
                        "Result": {
                            "ConversationInfo": {
                                "ConversationID": "conv-1",
                                "ProjectList": [{"SessionID": "session-1"}],
                            }
                        }
                    }
                }
            ),
        ) as mock_request:
            conv_id = await plugin.create_conversation(
                fake_context,  # type: ignore[arg-type]
                fake_page,  # type: ignore[arg-type]
                model="glm-5.1",
            )

        self.assertEqual(conv_id, "conv-1")
        headers = mock_request.await_args.kwargs["headers"]
        self.assertEqual(headers["X-CSRF-Token"], "csrf-token")
        self.assertEqual(headers["X-Top-Region"], "cn-north-1")

    async def test_pdf_attachment_is_uploaded_as_file(self) -> None:
        plugin = TongjiPlugin()
        plugin._session_state["conv-1"] = {  # noqa: SLF001
            "session_id": "session-1",
            "workspace_id": "workspace-1",
        }
        attachment = InputAttachment(
            filename="paper.pdf",
            mime_type="application/pdf",
            data=b"%PDF-1.4\n",
        )
        fake_page = AsyncMock()
        fake_page.evaluate = AsyncMock(return_value="csrf-token")
        fake_context = object()

        with (
            patch.object(
                plugin,
                "_upload_attachment",
                new=AsyncMock(
                    return_value={
                        "Path": "upload/full/paper",
                        "Name": "paper.pdf",
                        "Size": len(attachment.data),
                        "Url": "https://agent.tongji.edu.cn/api/proxy/down?Path=paper",
                    }
                ),
            ) as mock_upload,
            patch(
                "core.plugin.tongji.request_json_via_page_fetch",
                new=AsyncMock(
                    return_value={
                        "json": {
                            "Result": {
                                "MessageList": [{"MessageID": "message-1"}]
                            }
                        }
                    }
                ),
            ) as mock_request,
            patch(
                "core.plugin.tongji.stream_raw_via_page_fetch",
                return_value=_fake_tongji_stream(),
            ) as mock_stream,
        ):
            chunks = [
                chunk
                async for chunk in plugin.stream_completion(
                    fake_context,  # type: ignore[arg-type]
                    fake_page,  # type: ignore[arg-type]
                    "conv-1",
                    "总结这个 pdf",
                    attachments=[attachment],
                )
            ]

        self.assertEqual(chunks, ["已总结"])
        mock_upload.assert_awaited_once()
        headers = mock_request.await_args.kwargs["headers"]
        self.assertEqual(headers["X-CSRF-Token"], "csrf-token")
        self.assertEqual(headers["X-Top-Region"], "cn-north-1")
        stream_headers = mock_stream.call_args.kwargs["headers"]
        self.assertEqual(stream_headers["X-CSRF-Token"], "csrf-token")
        self.assertEqual(stream_headers["X-Top-Region"], "cn-north-1")
        self.assertEqual(stream_headers["Accept"], "text/event-stream")
        batch_body = mock_request.await_args.kwargs["body"]
        message = json.loads(batch_body)["MessageList"][0]
        self.assertIn("总结这个 pdf", message["Content"])
        self.assertNotIn("Extracted text from", message["Content"])
        self.assertEqual(message["ExtendsInfo"]["Files"][0]["Name"], "paper.pdf")

    async def test_stream_fetch_preserves_request_headers_after_response_headers(self) -> None:
        fake_context = _FakeContext()
        fake_page = _FakePage(
            ["__headers__:{\"content-type\":\"text/event-stream\"}", "data: ok\n", "__done__"]
        )

        chunks = [
            chunk
            async for chunk in stream_raw_via_page_fetch(
                fake_context,  # type: ignore[arg-type]
                fake_page,  # type: ignore[arg-type]
                "https://agent.tongji.edu.cn/api/bypass/aigw?Action=Chat",
                "{}",
                "request-1",
                headers={"X-CSRF-Token": "csrf-token"},
                read_timeout=1.0,
            )
        ]

        self.assertEqual(chunks, ["data: ok\n"])
        self.assertEqual(fake_page.evaluate_payload["headers"]["X-CSRF-Token"], "csrf-token")


async def _fake_tongji_stream():
    yield 'data:{"choices":[{"delta":{"content":"已总结"},"finish_reason":null}]}\n'
    yield "data:[DONE]\n"


class _FakeCdpSession:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks
        self._callback = None

    def on(self, event: str, callback) -> None:
        if event == "Runtime.bindingCalled":
            self._callback = callback

    async def send(self, method: str, params: dict[str, str]) -> None:
        if method != "Runtime.addBinding" or self._callback is None:
            return
        name = params["name"]
        for chunk in self._chunks:
            self._callback({"name": name, "payload": chunk})

    async def detach(self) -> None:
        return None


class _FakeContext:
    def __init__(self) -> None:
        self.session: _FakeCdpSession | None = None

    async def new_cdp_session(self, page) -> _FakeCdpSession:
        self.session = _FakeCdpSession(page.chunks)
        return self.session


class _FakePage:
    url = "https://agent.tongji.edu.cn/product/llm/personal/personal-5724/application"

    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.evaluate_payload: dict[str, object] = {}

    async def evaluate(self, script: str, payload: dict[str, object]) -> None:
        self.evaluate_payload = payload
        return None


if __name__ == "__main__":
    unittest.main()
