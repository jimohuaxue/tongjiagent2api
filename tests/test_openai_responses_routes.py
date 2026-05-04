import json
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.api.conv_parser import session_id_suffix
from core.api import file_store
from core.api.file_store import (
    delete_openai_file,
    get_openai_file,
    list_openai_files,
    openai_file_metadata,
    parse_multipart_form,
    resolve_openai_file_references,
    store_openai_file,
)
from core.api.openai_routes import (
    _embeddings_payload,
    _extract_responses_session_id,
    _response_message_item,
    _response_payload,
    _lookup_responses_session,
    _responses_body_to_chat_body,
    _responses_render_output_items,
    _store_responses_session,
)
from core.protocol.openai import OpenAIProtocolAdapter
from core.protocol.service import CanonicalChatService


class _FakeApp:
    def __init__(self) -> None:
        self.state = SimpleNamespace()


class TestOpenAIFileStore(unittest.TestCase):
    def test_store_list_get_delete_file(self) -> None:
        app = _FakeApp()
        record = store_openai_file(
            app,
            filename="paper.pdf",
            data=b"%PDF-1.4\n",
            mime_type="application/pdf",
            purpose="assistants",
        )

        self.assertEqual(record["id"], get_openai_file(app, record["id"])["id"])
        self.assertEqual(list_openai_files(app)[0]["filename"], "paper.pdf")
        self.assertEqual(openai_file_metadata(record)["status"], "processed")
        self.assertTrue(delete_openai_file(app, record["id"]))
        self.assertIsNone(get_openai_file(app, record["id"]))

    def test_parse_multipart_file_upload(self) -> None:
        boundary = "web2api-boundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="purpose"\r\n'
            "\r\n"
            "assistants\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="paper.pdf"\r\n'
            "Content-Type: application/pdf\r\n"
            "\r\n"
            "%PDF-1.4\n\r\n"
            f"--{boundary}--\r\n"
        ).encode()

        fields, files = parse_multipart_form(
            f"multipart/form-data; boundary={boundary}",
            body,
        )

        self.assertEqual(fields["purpose"], "assistants")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].filename, "paper.pdf")
        self.assertEqual(files[0].mime_type, "application/pdf")
        self.assertEqual(files[0].data, b"%PDF-1.4\n")


class TestOpenAIResponsesRoutes(unittest.TestCase):
    def test_responses_developer_role_normalized_to_system(self) -> None:
        # alma / Codex 走 OpenAI Responses API 时会用 "developer" 角色（替代旧 system）。
        body = _responses_body_to_chat_body(
            {
                "model": "glm-5.1",
                "input": [
                    {"type": "message", "role": "developer", "content": "你是助手"},
                    {"type": "message", "role": "user", "content": "你好"},
                ],
            }
        )

        req = OpenAIProtocolAdapter().parse_request("tongji", body)
        # system 角色被分流到 system_blocks，messages 里只剩 user。
        self.assertEqual(len(req.messages), 1)
        self.assertEqual(req.messages[0].role, "user")
        self.assertEqual(req.system[0].text, "你是助手")

    def test_responses_string_input_maps_to_user_message(self) -> None:
        body = _responses_body_to_chat_body(
            {"model": "glm-5.1", "input": "你好", "stream": False}
        )

        self.assertEqual(
            body["messages"],
            [{"role": "user", "content": "你好"}],
        )

    def test_responses_previous_response_session_can_be_injected(self) -> None:
        body = _responses_body_to_chat_body(
            {"model": "glm-5.1", "input": "继续", "stream": False}
        )
        body["resume_session_id"] = "session-group-id-abc"

        req = OpenAIProtocolAdapter().parse_request("tongji", body)

        self.assertEqual(req.resume_session_id, "session-group-id-abc")

    def test_responses_preserves_client_conversation_id(self) -> None:
        body = _responses_body_to_chat_body(
            {
                "model": "glm-5.1",
                "conversation_id": "responses-chat-1",
                "input": "继续",
                "stream": False,
            }
        )

        req = OpenAIProtocolAdapter().parse_request("tongji", body)

        self.assertEqual(req.client_conversation_id, "responses-chat-1")

    def test_responses_top_level_function_tool_is_accepted(self) -> None:
        body = _responses_body_to_chat_body(
            {
                "model": "glm-5.1",
                "input": "读取系统信息",
                "tools": [
                    {
                        "type": "function",
                        "name": "Bash",
                        "description": "Run a shell command",
                        "parameters": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        },
                    }
                ],
            }
        )

        req = OpenAIProtocolAdapter().parse_request("tongji", body)

        self.assertEqual(req.tools[0].name, "Bash")
        self.assertEqual(req.tools[0].input_schema["required"], ["command"])

    def test_responses_image_input_maps_to_image_url_part(self) -> None:
        image_url = "data:image/png;base64,iVBORw0KGgo="
        body = _responses_body_to_chat_body(
            {
                "model": "glm-5.1",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "描述图片"},
                            {"type": "input_image", "image_url": image_url},
                        ],
                    }
                ],
            }
        )

        content = body["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "描述图片"})
        self.assertEqual(
            content[1],
            {"type": "image_url", "image_url": {"url": image_url}},
        )

    def test_responses_nested_image_url_dict_maps_to_image_block(self) -> None:
        image_url = "data:image/png;base64,iVBORw0KGgo="
        body = _responses_body_to_chat_body(
            {
                "model": "glm-5.1",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "描述图片"},
                            {
                                "type": "input_image",
                                "image_url": {"url": image_url, "detail": "auto"},
                            },
                        ],
                    }
                ],
            }
        )

        content = body["messages"][0]["content"]
        self.assertEqual(
            content[1],
            {"type": "image_url", "image_url": {"url": image_url}},
        )
        req = OpenAIProtocolAdapter().parse_request("tongji", body)
        blocks = req.messages[0].content
        self.assertEqual(blocks[1].type, "image")
        self.assertEqual(blocks[1].data, image_url)

    def test_responses_loose_input_image_dict_maps_to_image_url_part(self) -> None:
        image_url = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
        body = _responses_body_to_chat_body(
            {
                "model": "glm-5.1",
                "input": [
                    {"type": "input_text", "text": "看图"},
                    {"type": "input_image", "url": {"url": image_url}},
                ],
            }
        )

        self.assertEqual(
            body["messages"],
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看图"},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
        )


class TestOpenAIResponsesImageAttachments(unittest.IsolatedAsyncioTestCase):
    async def test_responses_nested_image_url_becomes_last_user_attachment(self) -> None:
        image_url = "data:image/png;base64,iVBORw0KGgo="
        body = _responses_body_to_chat_body(
            {
                "model": "glm-5.1",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "描述图片"},
                            {
                                "type": "input_image",
                                "image_url": {"url": image_url, "detail": "auto"},
                            },
                        ],
                    }
                ],
            }
        )

        req = OpenAIProtocolAdapter().parse_request("tongji", body)
        openai_req = await CanonicalChatService(None)._to_openai_request(req)  # type: ignore[arg-type]

        self.assertEqual(len(openai_req.attachment_files_last_user), 1)
        self.assertEqual(openai_req.attachment_files_last_user[0].mime_type, "image/png")
        self.assertEqual(openai_req.attachment_files_last_user[0].data, b"\x89PNG\r\n\x1a\n")

    async def test_responses_nested_pdf_file_becomes_last_user_attachment(self) -> None:
        body = _responses_body_to_chat_body(
            {
                "model": "glm-5.1",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "总结这个 pdf"},
                            {
                                "type": "input_file",
                                "filename": "paper.pdf",
                                "mime_type": "application/pdf",
                                "file": {
                                    "file_data": {
                                        "data": "data:application/pdf;base64,JVBERi0xLjQK"
                                    }
                                },
                            },
                        ],
                    }
                ],
            }
        )

        req = OpenAIProtocolAdapter().parse_request("tongji", body)
        openai_req = await CanonicalChatService(None)._to_openai_request(req)  # type: ignore[arg-type]

        self.assertEqual(len(openai_req.attachment_files_last_user), 1)
        attachment = openai_req.attachment_files_last_user[0]
        self.assertEqual(attachment.filename, "paper.pdf")
        self.assertEqual(attachment.mime_type, "application/pdf")
        self.assertEqual(attachment.data, b"%PDF-1.4\n")

    async def test_responses_top_level_pdf_attachment_becomes_last_user_attachment(self) -> None:
        body = _responses_body_to_chat_body(
            {
                "model": "glm-5.1",
                "input": "总结这个 pdf",
                "attachments": [
                    {
                        "filename": "paper.pdf",
                        "mime_type": "application/pdf",
                        "data": "data:application/pdf;base64,JVBERi0xLjQK",
                    }
                ],
            }
        )

        req = OpenAIProtocolAdapter().parse_request("tongji", body)
        openai_req = await CanonicalChatService(None)._to_openai_request(req)  # type: ignore[arg-type]

        self.assertEqual(len(openai_req.attachment_files_last_user), 1)
        attachment = openai_req.attachment_files_last_user[0]
        self.assertEqual(attachment.filename, "paper.pdf")
        self.assertEqual(attachment.mime_type, "application/pdf")
        self.assertEqual(attachment.data, b"%PDF-1.4\n")

    async def test_responses_text_path_becomes_last_user_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            md_path = Path(tmp) / "Alma 介绍.md"
            md_path.write_text("# Alma\n\n调用和检修方式\n", encoding="utf-8")
            body = _responses_body_to_chat_body(
                {
                    "model": "glm-5.1",
                    "input": f"阅读这个{md_path} 增加调用和检修方式",
                }
            )

        req = OpenAIProtocolAdapter().parse_request("tongji", body)
        openai_req = await CanonicalChatService(None)._to_openai_request(req)  # type: ignore[arg-type]

        self.assertEqual(len(openai_req.attachment_files_last_user), 1)
        attachment = openai_req.attachment_files_last_user[0]
        self.assertEqual(attachment.filename, "Alma 介绍.md")
        self.assertEqual(attachment.mime_type, "text/markdown")
        self.assertEqual(attachment.data, "# Alma\n\n调用和检修方式\n".encode())

    async def test_responses_bare_pdf_filename_injects_extracted_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            previous_cwd = Path.cwd()
            try:
                import os

                os.chdir(tmp)
                with patch.object(
                    file_store,
                    "_extract_pdf_text_from_path",
                    return_value="Responses PDF text extracted.",
                ):
                    body = _responses_body_to_chat_body(
                        {
                            "model": "glm-5.1",
                            "input": "pdf总结\npaper.pdf",
                        }
                    )
            finally:
                os.chdir(previous_cwd)

        req = OpenAIProtocolAdapter().parse_request("tongji", body)
        blocks = req.messages[0].content
        text = "\n".join(block.text or "" for block in blocks if block.type == "text")
        self.assertIn("Responses PDF text extracted.", text)
        self.assertIn("不要再调用文件搜索工具", text)

    async def test_chat_nested_file_object_becomes_last_user_attachment(self) -> None:
        req = OpenAIProtocolAdapter().parse_request(
            "tongji",
            {
                "model": "glm-5.1",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "总结这个 pdf"},
                            {
                                "type": "input_file",
                                "file": {
                                    "filename": "paper.pdf",
                                    "mime_type": "application/pdf",
                                    "data": "data:application/pdf;base64,JVBERi0xLjQK",
                                },
                            },
                        ],
                    }
                ],
            },
        )

        openai_req = await CanonicalChatService(None)._to_openai_request(req)  # type: ignore[arg-type]

        self.assertEqual(len(openai_req.attachment_files_last_user), 1)
        attachment = openai_req.attachment_files_last_user[0]
        self.assertEqual(attachment.filename, "paper.pdf")
        self.assertEqual(attachment.mime_type, "application/pdf")
        self.assertEqual(attachment.data, b"%PDF-1.4\n")

    async def test_chat_file_id_reference_becomes_last_user_attachment(self) -> None:
        app = _FakeApp()
        record = store_openai_file(
            app,
            filename="paper.pdf",
            mime_type="application/pdf",
            data=b"%PDF-1.4\n",
        )
        body = resolve_openai_file_references(
            app,
            {
                "model": "glm-5.1",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "总结这个 pdf"},
                            {
                                "type": "input_file",
                                "file": {"file_id": record["id"]},
                            },
                        ],
                    }
                ],
            },
        )

        req = OpenAIProtocolAdapter().parse_request("tongji", body)
        openai_req = await CanonicalChatService(None)._to_openai_request(req)  # type: ignore[arg-type]

        self.assertEqual(len(openai_req.attachment_files_last_user), 1)
        attachment = openai_req.attachment_files_last_user[0]
        self.assertEqual(attachment.filename, "paper.pdf")
        self.assertEqual(attachment.mime_type, "application/pdf")
        self.assertEqual(attachment.data, b"%PDF-1.4\n")

    async def test_responses_top_level_file_id_attachment_becomes_last_user_attachment(self) -> None:
        app = _FakeApp()
        record = store_openai_file(
            app,
            filename="paper.pdf",
            mime_type="application/pdf",
            data=b"%PDF-1.4\n",
        )
        raw_body = resolve_openai_file_references(
            app,
            {
                "model": "glm-5.1",
                "input": "总结这个 pdf",
                "attachments": [{"file_id": record["id"]}],
            },
        )
        body = _responses_body_to_chat_body(raw_body)

        req = OpenAIProtocolAdapter().parse_request("tongji", body)
        openai_req = await CanonicalChatService(None)._to_openai_request(req)  # type: ignore[arg-type]

        self.assertEqual(len(openai_req.attachment_files_last_user), 1)
        attachment = openai_req.attachment_files_last_user[0]
        self.assertEqual(attachment.filename, "paper.pdf")
        self.assertEqual(attachment.mime_type, "application/pdf")
        self.assertEqual(attachment.data, b"%PDF-1.4\n")


class TestOpenAIResponsesFileInput(unittest.TestCase):
    def test_responses_file_input_maps_to_document_block(self) -> None:
        body = _responses_body_to_chat_body(
            {
                "model": "glm-5.1",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "解释代码"},
                            {
                                "type": "input_file",
                                "filename": "hello.py",
                                "mime_type": "text/x-python",
                                "file_data": "data:text/x-python;base64,cHJpbnQoJ2hpJykK",
                            },
                        ],
                    }
                ],
            }
        )

        req = OpenAIProtocolAdapter().parse_request("tongji", body)
        blocks = req.messages[0].content
        self.assertEqual(blocks[0].type, "text")
        self.assertEqual(blocks[1].type, "document")
        self.assertEqual(blocks[1].filename, "hello.py")
        self.assertEqual(blocks[1].mime_type, "text/x-python")
        self.assertEqual(blocks[1].data, "data:text/x-python;base64,cHJpbnQoJ2hpJykK")

    def test_stream_response_events_have_responses_api_shape(self) -> None:
        response = _response_payload("resp_1", "glm-5.1", "你好", 123)
        message = _response_message_item("msg_1", "你好", status="completed")

        created = {"type": "response.created", "response": response}
        delta = {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "delta": "你",
        }
        item_done = {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": message,
        }
        completed = {"type": "response.completed", "response": response}

        self.assertEqual(created["response"]["object"], "response")
        self.assertEqual(delta["item_id"], "msg_1")
        self.assertEqual(item_done["item"]["type"], "message")
        self.assertEqual(completed["response"]["status"], "completed")

    def test_responses_strips_hidden_session_marker_from_plain_text(self) -> None:
        raw_text = "你好" + session_id_suffix("session-group-id-1")

        output_text, output_items = _responses_render_output_items(raw_text)

        self.assertEqual(_extract_responses_session_id(raw_text), "session-group-id-1")
        self.assertEqual(output_text, "你好")
        self.assertEqual(output_items[0]["content"][0]["text"], "你好")
        self.assertNotIn(session_id_suffix("session-group-id-1"), output_text)

    def test_responses_strips_hidden_session_marker_from_tool_call(self) -> None:
        raw_text = (
            '<think>Need shell</think><tool_calls>[{"name":"BashTool",'
            '"arguments":{"cmd":"pwd"}}]</tool_calls>'
            + session_id_suffix("session-group-id-2")
        )

        output_text, output_items = _responses_render_output_items(raw_text)

        self.assertEqual(output_text, "Need shell")
        self.assertEqual(output_items[0]["type"], "message")
        self.assertEqual(output_items[0]["content"][0]["text"], "Need shell")
        self.assertEqual(output_items[1]["type"], "function_call")
        self.assertNotIn(session_id_suffix("session-group-id-2"), output_text)

    def test_responses_session_store_maps_response_id(self) -> None:
        app = SimpleNamespace(state=SimpleNamespace())

        _store_responses_session(app, "resp_1", "session-group-id-1")

        self.assertEqual(
            _lookup_responses_session(app, "resp_1"),
            "session-group-id-1",
        )

    def test_tagged_tool_call_maps_to_responses_function_call(self) -> None:
        output_text, output_items = _responses_render_output_items(
            '<think>Need shell</think><tool_calls>[{"name":"BashTool","arguments":{"cmd":"pwd"}}]</tool_calls>'
        )

        self.assertEqual(output_text, "Need shell")
        self.assertEqual(output_items[0]["type"], "message")
        self.assertEqual(output_items[1]["type"], "function_call")
        self.assertEqual(output_items[1]["name"], "BashTool")
        self.assertEqual(output_items[1]["arguments"], '{"cmd": "pwd"}')

    def test_malformed_tool_tag_stays_text(self) -> None:
        text = "<tool_call>BashTool> cat ~/.config/alma/USER.md</tool_call>"
        output_text, output_items = _responses_render_output_items(text)

        self.assertEqual(output_text, text)
        self.assertEqual(output_items[0]["type"], "message")

    def test_loose_tool_tag_maps_when_tool_is_declared(self) -> None:
        text = "<tool_call>BashTool> cat ~/.config/alma/USER.md</tool_call>"
        output_text, output_items = _responses_render_output_items(
            text,
            tool_names={"BashTool"},
        )

        self.assertEqual(output_text, "")
        self.assertEqual(output_items[0]["type"], "function_call")
        self.assertEqual(output_items[0]["name"], "BashTool")
        self.assertEqual(
            output_items[0]["arguments"],
            '{"cmd": "cat ~/.config/alma/USER.md"}',
        )

    def test_function_call_output_input_maps_to_tool_message(self) -> None:
        body = _responses_body_to_chat_body(
            {
                "model": "glm-5.1",
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "BashTool",
                        "arguments": {"cmd": "pwd"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": "/tmp/web2api",
                    },
                ],
            }
        )

        self.assertEqual(body["messages"][0]["role"], "assistant")
        self.assertEqual(body["messages"][0]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(body["messages"][1]["role"], "tool")
        self.assertEqual(body["messages"][1]["tool_call_id"], "call_1")

    def test_alma_tool_tag_maps_to_responses_function_call(self) -> None:
        text = (
            '<tool_call>Bash<arg_key>arguments":{"command":"uname -a && '
            'cat /etc/os-release 2>/dev/null || cat /etc/lsb-release 2>/dev/null'
            "</arg_value>}</arg_value></tool_call>"
        )

        output_text, output_items = _responses_render_output_items(
            text,
            tool_names={"Bash"},
        )

        self.assertEqual(output_text, "")
        self.assertEqual(output_items[0]["type"], "function_call")
        self.assertEqual(output_items[0]["name"], "Bash")
        self.assertEqual(
            output_items[0]["arguments"],
            '{"command": "uname -a && cat /etc/os-release 2>/dev/null || cat /etc/lsb-release 2>/dev/null"}',
        )

    def test_alma_corrupted_close_tag_still_parses(self) -> None:
        # 实际 GLM-5.1 输出：闭合标签是 "tool_calls>"（多了 s，没有 </），
        # 且 <arg_key>command":"... 缺少首引号。
        text = (
            '<tool_call>Bash<arg_key>command":"uname -a && '
            "cat /etc/os-release 2>/dev/null || cat /etc/lsb-release 2>/dev/null"
            '</arg_value>lazy":false}</arg_value>tool_calls>'
        )

        output_text, output_items = _responses_render_output_items(
            text,
            tool_names={"Bash"},
        )

        self.assertEqual(output_text, "")
        self.assertEqual(output_items[0]["type"], "function_call")
        self.assertEqual(output_items[0]["name"], "Bash")
        self.assertEqual(
            output_items[0]["arguments"],
            '{"command": "uname -a && cat /etc/os-release 2>/dev/null || cat /etc/lsb-release 2>/dev/null"}',
        )

    def test_alma_truncated_no_close_tag_still_parses(self) -> None:
        # 完全没有闭合标签（截断的输出）。
        text = (
            '<tool_call>Bash<arg_key>command":"cat /etc/os-release 2>/dev/null '
            '|| cat /etc/lsb-release 2>/dev/null; uname -r</arg_value>}</arg_value>'
        )

        output_text, output_items = _responses_render_output_items(
            text,
            tool_names={"Bash"},
        )

        self.assertEqual(output_text, "")
        self.assertEqual(output_items[0]["type"], "function_call")
        self.assertEqual(output_items[0]["name"], "Bash")
        self.assertEqual(
            output_items[0]["arguments"],
            '{"command": "cat /etc/os-release 2>/dev/null '
            '|| cat /etc/lsb-release 2>/dev/null; uname -r"}',
        )

    def test_alma_pb_exec_tool_name_with_json_args_parses(self) -> None:
        text = (
            '<tool_call>Bash_PB-exec</arg_key>{"command":"test -f '
            '~/.config/alma/USER.md && echo exists || echo missing"}'
        )

        output_text, output_items = _responses_render_output_items(
            text,
            tool_names={"Bash"},
        )

        self.assertEqual(output_text, "")
        self.assertEqual(output_items[0]["type"], "function_call")
        self.assertEqual(output_items[0]["name"], "Bash")
        self.assertEqual(
            output_items[0]["arguments"],
            '{"command": "test -f ~/.config/alma/USER.md && echo exists || echo missing"}',
        )

    def test_jsonish_tool_args_are_normalized_by_schema(self) -> None:
        text = (
            '<tool_call>Bash<arg_key>command":"echo python --version",'
            '"description":"Check Python environment"}</tool_call>'
        )

        output_text, output_items = _responses_render_output_items(
            text,
            tool_schemas={
                "Bash": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["command", "description"],
                }
            },
        )

        self.assertEqual(output_text, "")
        self.assertEqual(output_items[0]["type"], "function_call")
        self.assertEqual(output_items[0]["name"], "Bash")
        self.assertEqual(
            output_items[0]["arguments"],
            '{"command": "echo python --version", "description": "Check Python environment"}',
        )

    def test_bash_description_is_defaulted_when_schema_requires_it(self) -> None:
        text = (
            '<tool_call>Bash</arg_key><arg_value>'
            '{"command":"head -c 30000 /tmp/paper.txt | head -200"}'
            "</tool_call>"
        )

        output_text, output_items = _responses_render_output_items(
            text,
            tool_schemas={
                "Bash": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["command", "description"],
                }
            },
        )

        self.assertEqual(output_text, "")
        self.assertEqual(output_items[0]["type"], "function_call")
        self.assertEqual(output_items[0]["name"], "Bash")
        self.assertEqual(
            output_items[0]["arguments"],
            '{"command": "head -c 30000 /tmp/paper.txt | head -200", '
            '"description": "Run: head -c 30000 /tmp/paper.txt | head -200"}',
        )

    def test_repeated_tool_arg_name_is_normalized_by_schema(self) -> None:
        text = (
            '<tool_calls>[{"name":"Shell","arguments":'
            '{"commandcommand":"echo ok","description":"Run check"}}]</tool_calls>'
        )

        output_text, output_items = _responses_render_output_items(
            text,
            tool_schemas={
                "Shell": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["command", "description"],
                }
            },
        )

        self.assertEqual(output_text, "")
        self.assertEqual(output_items[0]["type"], "function_call")
        self.assertEqual(output_items[0]["name"], "Shell")
        self.assertEqual(
            output_items[0]["arguments"],
            '{"command": "echo ok", "description": "Run check"}',
        )

    def test_bare_attribute_tool_call_parses_generically(self) -> None:
        text = (
            '<tool_call>Bash command="which grim scrot gnome-screenshot '
            'xfce4-screenshooter 2>/dev/null; echo ---" '
            'description="Find available screenshot tools" '
            'timeout="10000"</tool_call>*'
        )

        output_text, output_items = _responses_render_output_items(
            text,
            tool_schemas={
                "Bash": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "description": {"type": "string"},
                        "timeout": {"type": "number"},
                    },
                    "required": ["command", "description"],
                }
            },
        )

        self.assertEqual(output_text, "")
        self.assertEqual(output_items[0]["type"], "function_call")
        self.assertEqual(output_items[0]["name"], "Bash")
        self.assertEqual(
            output_items[0]["arguments"],
            '{"command": "which grim scrot gnome-screenshot xfce4-screenshooter 2>/dev/null; echo ---", '
            '"description": "Find available screenshot tools", "timeout": 10000}',
        )

    def test_dotted_attribute_tool_calls_parse_generically(self) -> None:
        text = (
            '<tool_call>Bash.command="alma help 2>&1 | head -200", '
            'description="Get help", timeout=10000</arg_value>'
            '<tool_call>Grep.command="rg -ri shortcut ~/.config/alma", '
            'description="Search shortcuts", timeout=10000</arg_value>'
        )

        output_text, output_items = _responses_render_output_items(
            text,
            tool_schemas={
                "Bash": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "description": {"type": "string"},
                        "timeout": {"type": "number"},
                    },
                    "required": ["command"],
                },
                "Grep": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "description": {"type": "string"},
                        "timeout": {"type": "number"},
                    },
                    "required": ["command"],
                },
            },
        )

        self.assertEqual(output_text, "")
        self.assertEqual(len(output_items), 2)
        self.assertEqual(output_items[0]["type"], "function_call")
        self.assertEqual(output_items[0]["name"], "Bash")
        self.assertEqual(
            output_items[0]["arguments"],
            '{"command": "alma help 2>&1 | head -200", "description": "Get help", "timeout": 10000}',
        )
        self.assertEqual(output_items[1]["type"], "function_call")
        self.assertEqual(output_items[1]["name"], "Grep")
        self.assertEqual(
            output_items[1]["arguments"],
            '{"command": "rg -ri shortcut ~/.config/alma", "description": "Search shortcuts", "timeout": 10000}',
        )

    def test_arguments_prefix_tool_call_parses_generically(self) -> None:
        text = (
            '<tool_call>Bash_arguments__command="find /home/user -maxdepth 4 '
            '-name \'.obsidian\' -type d 2>/dev/null | head -5"</arg_value>'
        )

        output_text, output_items = _responses_render_output_items(
            text,
            tool_schemas={
                "Bash": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                }
            },
        )

        self.assertEqual(output_text, "")
        self.assertEqual(output_items[0]["type"], "function_call")
        self.assertEqual(output_items[0]["name"], "Bash")
        self.assertEqual(
            output_items[0]["arguments"],
            '{"command": "find /home/user -maxdepth 4 -name \'.obsidian\' -type d 2>/dev/null | head -5"}',
        )

    def test_arg_key_values_continue_after_spurious_tool_call_tag(self) -> None:
        text = (
            "<tool_call>TaskOutput<arg_key>task_id</arg_key>"
            "<arg_value>b5352e96-3d04</arg_value>"
            "<tool_call>block</arg_key><arg_value>false</arg_value>"
            "<arg_key>timeout</arg_key><arg_value>5</arg_value></tool_call>"
        )

        output_text, output_items = _responses_render_output_items(
            text,
            tool_schemas={
                "TaskOutput": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "block": {"type": "string"},
                        "timeout": {"type": "string"},
                    },
                    "required": ["task_id"],
                }
            },
        )

        self.assertEqual(output_text, "")
        self.assertEqual(output_items[0]["type"], "function_call")
        self.assertEqual(output_items[0]["name"], "TaskOutput")
        self.assertEqual(
            output_items[0]["arguments"],
            '{"task_id": "b5352e96-3d04", "block": "false", "timeout": "5"}',
        )

    def test_adjacent_arg_key_values_are_recovered_by_schema(self) -> None:
        text = (
            "<tool_call>Write<arg_key>file_path</arg_key>"
            "/tmp/notes/Alma 介绍.md"
            "<arg_key>content# Alma 介绍\n\n"
            "## 什么是 Alma？\n\n"
            'Alma 是一个有性格、有情绪、有记忆的"数字伙伴"。\n\n'
            "```bash\n"
            'alma run "你的问题"\n'
            "```\n\n"
            "| 文件/目录 | 用途 |\n"
            "|-----------|------|\n"
            "| `SOUL.md` | 性格与自我认知 |\n"
            "</arg_value></tool_call>"
        )

        output_text, output_items = _responses_render_output_items(
            text,
            tool_schemas={
                "Write": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["file_path", "content"],
                }
            },
        )

        self.assertEqual(output_text, "")
        self.assertEqual(output_items[0]["type"], "function_call")
        self.assertEqual(output_items[0]["name"], "Write")
        arguments = json.loads(output_items[0]["arguments"])
        self.assertEqual(
            arguments["file_path"],
            "/tmp/notes/Alma 介绍.md",
        )
        self.assertTrue(arguments["content"].startswith("# Alma 介绍"))
        self.assertIn('alma run "你的问题"', arguments["content"])
        self.assertIn("| `SOUL.md` | 性格与自我认知 |", arguments["content"])

    def test_loose_final_answer_tool_call_renders_as_message(self) -> None:
        text = "<tool_call>final_answer>hello</final_answer>"

        output_text, output_items = _responses_render_output_items(
            text,
            tool_schemas={"Bash": {"type": "object", "properties": {}}},
        )

        self.assertEqual(output_text, "hello")
        self.assertEqual(output_items[0]["type"], "message")
        self.assertEqual(output_items[0]["content"][0]["text"], "hello")

    def test_plain_final_answer_wrapper_renders_without_protocol_tag(self) -> None:
        text = "<final_answer>hello"

        output_text, output_items = _responses_render_output_items(text)

        self.assertEqual(output_text, "hello")
        self.assertEqual(output_items[0]["type"], "message")
        self.assertEqual(output_items[0]["content"][0]["text"], "hello")

    def test_colon_style_consecutive_tool_calls_parse_generically(self) -> None:
        text = (
            '<tool_call>Glob(pattern: ".md", path: '
            '"/tmp/alma/workspaces/temp")<tool_call>'
            'Glob(pattern: ".md", path: "/home/user")</tool_call>'
        )

        output_text, output_items = _responses_render_output_items(
            text,
            tool_schemas={
                "Glob": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {"type": "string"},
                    },
                    "required": ["pattern"],
                }
            },
        )

        self.assertEqual(output_text, "")
        self.assertEqual(len(output_items), 2)
        self.assertEqual(output_items[0]["type"], "function_call")
        self.assertEqual(output_items[0]["name"], "Glob")
        self.assertEqual(
            output_items[0]["arguments"],
            '{"pattern": ".md", "path": "/tmp/alma/workspaces/temp"}',
        )
        self.assertEqual(output_items[1]["type"], "function_call")
        self.assertEqual(output_items[1]["name"], "Glob")
        self.assertEqual(
            output_items[1]["arguments"],
            '{"pattern": ".md", "path": "/home/user"}',
        )

    def test_single_required_tool_arg_is_recovered_generically(self) -> None:
        text = '<tool_call>Search<arg_key>queryquery":"linux version"}</tool_call>'

        output_text, output_items = _responses_render_output_items(
            text,
            tool_schemas={
                "Search": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                }
            },
        )

        self.assertEqual(output_text, "")
        self.assertEqual(output_items[0]["type"], "function_call")
        self.assertEqual(output_items[0]["name"], "Search")
        self.assertEqual(output_items[0]["arguments"], '{"query": "linux version"}')

    def test_function_style_skill_tool_call_parses(self) -> None:
        text = (
            '<tool_call><Skill(skill="system-info")</arg_value></tool_call>'
        )

        output_text, output_items = _responses_render_output_items(
            text,
            tool_names={"Skill"},
        )

        self.assertEqual(output_text, "")
        self.assertEqual(output_items[0]["type"], "function_call")
        self.assertEqual(output_items[0]["name"], "Skill")
        self.assertEqual(output_items[0]["arguments"], '{"skill": "system-info"}')

    def test_tool_arguments_alias_command_to_cmd(self) -> None:
        text = (
            '<tool_calls>[{"name":"Bash","arguments":'
            '{"command":"ls /tmp/web2api"}}]</tool_calls>'
        )

        output_text, output_items = _responses_render_output_items(
            text,
            tool_schemas={
                "Bash": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"],
                }
            },
        )

        self.assertEqual(output_text, "")
        self.assertEqual(output_items[0]["name"], "Bash")
        self.assertEqual(
            output_items[0]["arguments"],
            '{"cmd": "ls /tmp/web2api"}',
        )

    def test_tool_arguments_alias_cmd_to_command(self) -> None:
        text = '<tool_call>Bash> pwd</tool_call>'

        output_text, output_items = _responses_render_output_items(
            text,
            tool_schemas={
                "Bash": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                }
            },
        )

        self.assertEqual(output_text, "")
        self.assertEqual(output_items[0]["name"], "Bash")
        self.assertEqual(output_items[0]["arguments"], '{"command": "pwd"}')

    def test_bash_json_string_command_is_unwrapped(self) -> None:
        text = (
            '<tool_call>Bash<arg_key>command</arg_key><arg_value>'
            '{"command":"find /home/user -maxdepth 5 -name \'*.pdf\' -mmin -60 2>/dev/null | head -20",'
            '"description":"Find recently modified PDFs in home directory"}'
            "</arg_value></tool_call>"
        )

        output_text, output_items = _responses_render_output_items(
            text,
            tool_schemas={
                "Bash": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["command"],
                }
            },
        )

        self.assertEqual(output_text, "")
        self.assertEqual(output_items[0]["name"], "Bash")
        self.assertEqual(
            json.loads(output_items[0]["arguments"]),
            {
                "command": "find /home/user -maxdepth 5 -name '*.pdf' -mmin -60 2>/dev/null | head -20",
                "description": "Find recently modified PDFs in home directory",
            },
        )

    def test_read_offset_string_is_coerced_to_integer(self) -> None:
        text = (
            '<tool_calls>[{"name":"Read","arguments":'
            '{"file_path":"/tmp/notes/Alma 介绍.md",'
            '"offset":"90"}}]</tool_calls>'
        )

        output_text, output_items = _responses_render_output_items(
            text,
            tool_schemas={
                "Read": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "offset": {"type": "integer"},
                    },
                    "required": ["path"],
                }
            },
        )

        self.assertEqual(output_text, "")
        self.assertEqual(output_items[0]["name"], "Read")
        self.assertEqual(
            json.loads(output_items[0]["arguments"]),
            {"path": "/tmp/notes/Alma 介绍.md", "offset": 90},
        )

    def test_loose_edit_missing_required_args_is_not_emitted(self) -> None:
        text = (
            '<tool_call>Edit<arg_key>file_path</arg_key>'
            '<arg_value>/tmp/notes/Alma 介绍.md<</arg_value>'
            '</tool_call>'
        )

        output_text, output_items = _responses_render_output_items(
            text,
            tool_schemas={
                "Edit": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                    },
                    "required": ["file_path", "old_string", "new_string"],
                }
            },
        )

        self.assertIn("Edit", output_text)
        self.assertEqual(output_items[0]["type"], "message")

    def test_embeddings_payload_is_openai_compatible(self) -> None:
        payload = _embeddings_payload(
            {
                "model": "text-embedding-3-small",
                "input": ["hello", "world"],
                "dimensions": 8,
            }
        )

        self.assertEqual(payload["object"], "list")
        self.assertEqual(payload["model"], "text-embedding-3-small")
        self.assertEqual(len(payload["data"]), 2)
        self.assertEqual(payload["data"][0]["object"], "embedding")
        self.assertEqual(len(payload["data"][0]["embedding"]), 8)
        self.assertEqual(payload["usage"]["total_tokens"], 2)


if __name__ == "__main__":
    unittest.main()
