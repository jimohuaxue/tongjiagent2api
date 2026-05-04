import json
import unittest
from unittest.mock import AsyncMock, patch

from core.api.schemas import InputAttachment
from core.plugin.tongji import _parse_tongji_sse_chunk
from core.plugin.tongji import _should_inline_attachment
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


class TestTongjiPdfUpload(unittest.IsolatedAsyncioTestCase):
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
        fake_page = object()
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
            ),
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
        batch_body = mock_request.await_args.kwargs["body"]
        message = json.loads(batch_body)["MessageList"][0]
        self.assertIn("总结这个 pdf", message["Content"])
        self.assertNotIn("Extracted text from", message["Content"])
        self.assertEqual(message["ExtendsInfo"]["Files"][0]["Name"], "paper.pdf")


async def _fake_tongji_stream():
    yield 'data:{"choices":[{"delta":{"content":"已总结"},"finish_reason":null}]}\n'
    yield "data:[DONE]\n"


if __name__ == "__main__":
    unittest.main()
