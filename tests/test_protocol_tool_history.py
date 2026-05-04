import json
import unittest

from core.api.schemas import extract_user_content
from core.protocol.anthropic import AnthropicProtocolAdapter
from core.protocol.openai import OpenAIProtocolAdapter
from core.protocol.service import CanonicalChatService


class TestProtocolToolHistory(unittest.IsolatedAsyncioTestCase):
    async def test_openai_round_trips_assistant_tool_calls_and_tool_call_id(self) -> None:
        adapter = OpenAIProtocolAdapter()
        canonical = adapter.parse_request(
            "claude",
            {
                "model": "test-model",
                "messages": [
                    {"role": "user", "content": "Find the answer"},
                    {
                        "role": "assistant",
                        "content": "<think>Need to inspect the file</think>",
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": "Read",
                                    "arguments": '{"path":"a.py"}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_123",
                        "content": "file contents",
                    },
                    {"role": "user", "content": "continue"},
                ],
                "tools": [],
            },
        )

        service = CanonicalChatService(None)  # type: ignore[arg-type]
        openai_req = await service._to_openai_request(canonical)

        assistant_msg = openai_req.messages[1]
        tool_msg = openai_req.messages[2]
        self.assertEqual(assistant_msg.tool_calls[0]["id"], "call_123")
        self.assertEqual(
            json.loads(assistant_msg.tool_calls[0]["function"]["arguments"]),
            {"path": "a.py"},
        )
        self.assertEqual(tool_msg.tool_call_id, "call_123")

        prompt = extract_user_content(
            openai_req.messages,
            has_tools=True,
            tagged_prompt_prefix="PROMPT",
            full_history=True,
        )
        self.assertIn(
            '<tool_calls>[{"name": "Read", "arguments": {"path": "a.py"}}]</tool_calls>',
            prompt,
        )
        self.assertIn("Tool result for call_id=call_123:", prompt)
        self.assertIn("<tool_result>\nfile contents\n</tool_result>", prompt)
        self.assertIn("Do not output Observation.", prompt)
        self.assertIn("<tool_calls>[...]</tool_calls>", prompt)

    async def test_openai_extracts_client_conversation_id(self) -> None:
        adapter = OpenAIProtocolAdapter()
        canonical = adapter.parse_request(
            "claude",
            {
                "model": "test-model",
                "conversation_id": "chat-123",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        service = CanonicalChatService(None)  # type: ignore[arg-type]
        openai_req = await service._to_openai_request(canonical)

        self.assertEqual(canonical.client_conversation_id, "chat-123")
        self.assertEqual(openai_req.client_conversation_id, "chat-123")

    async def test_openai_extracts_client_conversation_id_from_metadata(self) -> None:
        adapter = OpenAIProtocolAdapter()
        canonical = adapter.parse_request(
            "claude",
            {
                "model": "test-model",
                "metadata": {"thread_id": "thread-456"},
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        self.assertEqual(canonical.client_conversation_id, "thread-456")

    async def test_anthropic_round_trips_tool_use_and_tool_result(self) -> None:
        adapter = AnthropicProtocolAdapter()
        canonical = adapter.parse_request(
            "claude",
            {
                "model": "test-model",
                "messages": [
                    {"role": "user", "content": "Find the answer"},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "Need to inspect"},
                            {
                                "type": "tool_use",
                                "id": "toolu_123",
                                "name": "Read",
                                "input": {"path": "a.py"},
                            },
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_123",
                                "content": "file contents",
                            }
                        ],
                    },
                    {"role": "user", "content": "continue"},
                ],
                "tools": [],
            },
        )

        service = CanonicalChatService(None)  # type: ignore[arg-type]
        openai_req = await service._to_openai_request(canonical)

        assistant_msg = openai_req.messages[1]
        tool_msg = openai_req.messages[2]
        self.assertEqual(assistant_msg.tool_calls[0]["id"], "toolu_123")
        self.assertEqual(assistant_msg.tool_calls[0]["function"]["name"], "Read")
        self.assertEqual(
            json.loads(assistant_msg.tool_calls[0]["function"]["arguments"]),
            {"path": "a.py"},
        )
        self.assertEqual(tool_msg.role, "tool")
        self.assertEqual(tool_msg.tool_call_id, "toolu_123")
        self.assertEqual(tool_msg.content, "file contents")

    async def test_anthropic_extracts_client_conversation_id(self) -> None:
        adapter = AnthropicProtocolAdapter()
        canonical = adapter.parse_request(
            "claude",
            {
                "model": "test-model",
                "thread_id": "anthropic-thread-1",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "hello"}],
                    }
                ],
            },
        )

        self.assertEqual(canonical.client_conversation_id, "anthropic-thread-1")


if __name__ == "__main__":
    unittest.main()
