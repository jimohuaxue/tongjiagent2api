import json
import unittest

from core.hub.schemas import OpenAIStreamEvent
from core.protocol.anthropic import AnthropicProtocolAdapter
from core.protocol.openai import OpenAIProtocolAdapter
from core.protocol.schemas import CanonicalChatRequest, CanonicalToolSpec


def _tool_request(protocol: str, *, stream: bool = False) -> CanonicalChatRequest:
    return CanonicalChatRequest(
        protocol=protocol,
        provider="claude",
        model="test-model",
        stream=stream,
        tools=[
            CanonicalToolSpec(
                name="Read",
                description="Read a file",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            )
        ],
    )


def _edit_request(protocol: str, *, stream: bool = False) -> CanonicalChatRequest:
    return CanonicalChatRequest(
        protocol=protocol,
        provider="claude",
        model="test-model",
        stream=stream,
        tools=[
            CanonicalToolSpec(
                name="Edit",
                description="Edit a file",
                input_schema={
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                    },
                    "required": ["file_path", "old_string", "new_string"],
                },
            )
        ],
    )


def _read_offset_request(protocol: str, *, stream: bool = False) -> CanonicalChatRequest:
    return CanonicalChatRequest(
        protocol=protocol,
        provider="claude",
        model="test-model",
        stream=stream,
        tools=[
            CanonicalToolSpec(
                name="Read",
                description="Read a file",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "offset": {"type": "integer"},
                    },
                    "required": ["path"],
                },
            )
        ],
    )


def _plain_request(protocol: str, *, stream: bool = False) -> CanonicalChatRequest:
    return CanonicalChatRequest(
        protocol=protocol,
        provider="claude",
        model="test-model",
        stream=stream,
    )


def _bash_request(protocol: str, *, stream: bool = False) -> CanonicalChatRequest:
    return CanonicalChatRequest(
        protocol=protocol,
        provider="claude",
        model="test-model",
        stream=stream,
        tools=[
            CanonicalToolSpec(
                name="Bash",
                description="Run a command",
                input_schema={
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"],
                },
            )
        ],
    )


def _event_payload(chunk: str) -> dict:
    lines = chunk.strip().splitlines()
    return json.loads(lines[1][6:])


async def _stream_events(chunks: list[str]):
    for chunk in chunks:
        yield OpenAIStreamEvent(type="content_delta", content=chunk)
    yield OpenAIStreamEvent(type="finish")


class TestTaggedProtocolAdapters(unittest.IsolatedAsyncioTestCase):
    def test_openai_non_stream_tool_call(self) -> None:
        adapter = OpenAIProtocolAdapter()
        req = _tool_request("openai")
        raw_events = [
            OpenAIStreamEvent(
                type="content_delta",
                content=(
                    "<think>Need file</think>"
                    '<tool_calls>[{"name":"Read","arguments":{"path":"a.py"}},'
                    '{"name":"Read","arguments":{"path":"b.py"}}]</tool_calls>'
                ),
            )
        ]

        result = adapter.render_non_stream(req, raw_events)

        choice = result["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(choice["message"]["content"], "<think>Need file</think>")
        tool_calls = choice["message"]["tool_calls"]
        self.assertEqual(len(tool_calls), 2)
        self.assertEqual(tool_calls[0]["function"]["name"], "Read")
        self.assertEqual(
            json.loads(tool_calls[0]["function"]["arguments"]),
            {"path": "a.py"},
        )
        self.assertEqual(
            json.loads(tool_calls[1]["function"]["arguments"]),
            {"path": "b.py"},
        )

    def test_openai_non_stream_tool_call_coerces_numeric_arguments(self) -> None:
        adapter = OpenAIProtocolAdapter()
        req = _read_offset_request("openai")
        raw_events = [
            OpenAIStreamEvent(
                type="content_delta",
                content=(
                    '<tool_calls>[{"name":"Read","arguments":'
                    '{"path":"a.py","offset":"90"}}]</tool_calls>'
                ),
            )
        ]

        result = adapter.render_non_stream(req, raw_events)

        tool_calls = result["choices"][0]["message"]["tool_calls"]
        self.assertEqual(
            json.loads(tool_calls[0]["function"]["arguments"]),
            {"path": "a.py", "offset": 90},
        )

    def test_openai_non_stream_incomplete_tool_call_falls_back_to_text(self) -> None:
        adapter = OpenAIProtocolAdapter()
        req = _edit_request("openai")
        raw_events = [
            OpenAIStreamEvent(
                type="content_delta",
                content=(
                    '<tool_calls>[{"name":"Edit","arguments":'
                    '{"file_path":"a.md"}}]</tool_calls>'
                ),
            )
        ]

        result = adapter.render_non_stream(req, raw_events)

        choice = result["choices"][0]
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertNotIn("tool_calls", choice["message"])

    def test_openai_non_stream_plain_final_answer_wrapper_is_stripped(self) -> None:
        adapter = OpenAIProtocolAdapter()
        req = _plain_request("openai")
        raw_events = [
            OpenAIStreamEvent(
                type="content_delta",
                content="<final_answer>Hello world</final_answer>",
            )
        ]

        result = adapter.render_non_stream(req, raw_events)

        self.assertEqual(
            result["choices"][0]["message"]["content"],
            "Hello world",
        )

    def test_openai_non_stream_unclosed_plain_final_answer_wrapper_is_stripped(
        self,
    ) -> None:
        adapter = OpenAIProtocolAdapter()
        req = _plain_request("openai")
        raw_events = [
            OpenAIStreamEvent(
                type="content_delta",
                content="<final_answer>Hello world",
            )
        ]

        result = adapter.render_non_stream(req, raw_events)

        self.assertEqual(
            result["choices"][0]["message"]["content"],
            "Hello world",
        )

    async def test_openai_stream_plain_final_answer_wrapper_is_stripped(self) -> None:
        adapter = OpenAIProtocolAdapter()
        req = _plain_request("openai", stream=True)

        chunks = []
        async for item in adapter.render_stream(
            req,
            _stream_events(
                ["<final_", "answer>Hello", " world</final", "_answer>ignored"]
            ),
        ):
            chunks.append(item)

        content_parts: list[str] = []
        for chunk in chunks[:-1]:
            if not chunk.startswith("data: "):
                continue
            payload = json.loads(chunk[6:])
            delta = payload["choices"][0]["delta"]
            if "content" in delta:
                content_parts.append(delta["content"])

        self.assertEqual("".join(content_parts), "Hello world")

    def test_openai_non_stream_tool_call_normalizes_schema_aliases(self) -> None:
        adapter = OpenAIProtocolAdapter()
        req = _bash_request("openai")
        raw_events = [
            OpenAIStreamEvent(
                type="content_delta",
                content=(
                    '<tool_calls>[{"name":"Bash","arguments":'
                    '{"command":"ls /tmp/web2api"}}]</tool_calls>'
                ),
            )
        ]

        result = adapter.render_non_stream(req, raw_events)

        tool_calls = result["choices"][0]["message"]["tool_calls"]
        self.assertEqual(
            json.loads(tool_calls[0]["function"]["arguments"]),
            {"cmd": "ls /tmp/web2api"},
        )

    async def test_openai_stream_final_answer(self) -> None:
        adapter = OpenAIProtocolAdapter()
        req = _tool_request("openai", stream=True)

        chunks = []
        async for item in adapter.render_stream(
            req,
            _stream_events(
                [
                    "<think>Analyze</think>",
                    "<final_answer>Hello <b>world</b></final_answer>",
                ]
            ),
        ):
            chunks.append(item)

        self.assertEqual(chunks[-1], "data: [DONE]\n\n")
        content_parts: list[str] = []
        finish_reason = None
        for chunk in chunks[:-1]:
            if not chunk.startswith("data: "):
                continue
            payload = json.loads(chunk[6:])
            delta = payload["choices"][0]["delta"]
            if "content" in delta:
                content_parts.append(delta["content"])
            if payload["choices"][0]["finish_reason"] is not None:
                finish_reason = payload["choices"][0]["finish_reason"]

        self.assertEqual(
            "".join(content_parts), "<think>Analyze</think>Hello <b>world</b>"
        )
        self.assertEqual(finish_reason, "stop")

    async def test_openai_stream_tool_call_stops_on_first_terminal_block(self) -> None:
        adapter = OpenAIProtocolAdapter()
        req = _tool_request("openai", stream=True)

        chunks = []
        async for item in adapter.render_stream(
            req,
            _stream_events(
                [
                    (
                        "<think>Need file</think>"
                        '<tool_calls>[{"name":"Read","arguments":{"path":"a.py"}},'
                        '{"name":"Read","arguments":{"path":"b.py"}}]</tool_calls>'
                        "Observation: ignored"
                        "<final_answer>ignored</final_answer>"
                    ),
                ]
            ),
        ):
            chunks.append(item)

        finish_reasons = []
        for chunk in chunks:
            if chunk == "data: [DONE]\n\n":
                continue
            payload = json.loads(chunk[6:])
            finish_reason = payload["choices"][0]["finish_reason"]
            if finish_reason is not None:
                finish_reasons.append(finish_reason)

        self.assertEqual(finish_reasons, ["tool_calls"])
        payloads = [
            json.loads(chunk[6:])
            for chunk in chunks
            if chunk.startswith("data: ") and chunk != "data: [DONE]\n\n"
        ]
        tool_deltas = [
            payload["choices"][0]["delta"]["tool_calls"]
            for payload in payloads
            if "tool_calls" in payload["choices"][0]["delta"]
        ]
        self.assertEqual(tool_deltas[0][0]["index"], 0)
        self.assertEqual(tool_deltas[2][0]["index"], 1)

    async def test_openai_stream_tool_call_normalizes_schema_aliases(self) -> None:
        adapter = OpenAIProtocolAdapter()
        req = _bash_request("openai", stream=True)

        chunks = []
        async for item in adapter.render_stream(
            req,
            _stream_events(
                [
                    (
                        '<tool_calls>[{"name":"Bash","arguments":'
                        '{"command":"pwd"}}]</tool_calls>'
                    ),
                ]
            ),
        ):
            chunks.append(item)

        payloads = [
            json.loads(chunk[6:])
            for chunk in chunks
            if chunk.startswith("data: ") and chunk != "data: [DONE]\n\n"
        ]
        argument_delta = next(
            payload["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
            for payload in payloads
            if "tool_calls" in payload["choices"][0]["delta"]
            and "arguments" in payload["choices"][0]["delta"]["tool_calls"][0]["function"]
            and payload["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
        )
        self.assertEqual(json.loads(argument_delta), {"cmd": "pwd"})

    def test_anthropic_non_stream_final_answer(self) -> None:
        adapter = AnthropicProtocolAdapter()
        req = _tool_request("anthropic")
        raw_events = [
            OpenAIStreamEvent(
                type="content_delta",
                content=("<think>Done</think><final_answer>Hello world</final_answer>"),
            )
        ]

        result = adapter.render_non_stream(req, raw_events)

        self.assertEqual(result["stop_reason"], "end_turn")
        self.assertEqual(
            result["content"],
            [
                {"type": "thinking", "thinking": "Done"},
                {"type": "text", "text": "Hello world"},
            ],
        )

    def test_anthropic_non_stream_plain_final_answer_wrapper_is_stripped(self) -> None:
        adapter = AnthropicProtocolAdapter()
        req = _plain_request("anthropic")
        raw_events = [
            OpenAIStreamEvent(
                type="content_delta",
                content="<final_answer>Hello world</final_answer>",
            )
        ]

        result = adapter.render_non_stream(req, raw_events)

        self.assertEqual(result["stop_reason"], "end_turn")
        self.assertEqual(
            result["content"],
            [{"type": "text", "text": "Hello world"}],
        )

    def test_anthropic_non_stream_tool_call_normalizes_schema_aliases(self) -> None:
        adapter = AnthropicProtocolAdapter()
        req = _bash_request("anthropic")
        raw_events = [
            OpenAIStreamEvent(
                type="content_delta",
                content=(
                    '<tool_calls>[{"name":"Bash","arguments":'
                    '{"command":"pwd"}}]</tool_calls>'
                ),
            )
        ]

        result = adapter.render_non_stream(req, raw_events)

        self.assertEqual(result["content"][0]["type"], "tool_use")
        self.assertEqual(result["content"][0]["input"], {"cmd": "pwd"})

    def test_anthropic_non_stream_incomplete_tool_call_falls_back_to_text(self) -> None:
        adapter = AnthropicProtocolAdapter()
        req = _edit_request("anthropic")
        raw_events = [
            OpenAIStreamEvent(
                type="content_delta",
                content=(
                    '<tool_calls>[{"name":"Edit","arguments":'
                    '{"file_path":"a.md"}}]</tool_calls>'
                ),
            )
        ]

        result = adapter.render_non_stream(req, raw_events)

        self.assertEqual(result["stop_reason"], "end_turn")
        self.assertFalse(any(item["type"] == "tool_use" for item in result["content"]))

    async def test_anthropic_stream_tool_call(self) -> None:
        adapter = AnthropicProtocolAdapter()
        req = _tool_request("anthropic", stream=True)

        chunks = []
        async for item in adapter.render_stream(
            req,
            _stream_events(
                [
                    "<think>Need file</think>",
                    '<tool_calls>[{"name":"Read","arguments":{"path":"a.py"}},'
                    '{"name":"Read","arguments":{"path":"b.py"}}]</tool_calls>',
                ]
            ),
        ):
            chunks.append(item)

        event_names = [chunk.splitlines()[0][7:] for chunk in chunks]
        self.assertEqual(
            event_names,
            [
                "message_start",
                "content_block_start",
                "content_block_delta",
                "content_block_stop",
                "content_block_start",
                "content_block_delta",
                "content_block_stop",
                "content_block_start",
                "content_block_delta",
                "content_block_stop",
                "message_delta",
                "message_stop",
            ],
        )

        thinking_delta = _event_payload(chunks[2])
        self.assertEqual(thinking_delta["delta"]["type"], "thinking_delta")
        self.assertEqual(thinking_delta["delta"]["thinking"], "Need file")

        tool_delta = _event_payload(chunks[5])
        self.assertEqual(tool_delta["delta"]["type"], "input_json_delta")
        self.assertEqual(
            json.loads(tool_delta["delta"]["partial_json"]),
            {"path": "a.py"},
        )

        tool_delta_2 = _event_payload(chunks[8])
        self.assertEqual(tool_delta_2["delta"]["type"], "input_json_delta")
        self.assertEqual(
            json.loads(tool_delta_2["delta"]["partial_json"]),
            {"path": "b.py"},
        )

        message_delta = _event_payload(chunks[10])
        self.assertEqual(message_delta["delta"]["stop_reason"], "tool_use")


if __name__ == "__main__":
    unittest.main()
