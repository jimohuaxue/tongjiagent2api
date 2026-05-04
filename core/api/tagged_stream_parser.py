"""
Streaming parser for the tagged tool protocol.

Accepted model output:

    <think>...</think>
    <tool_calls>[{"name":"Read","arguments":{"path":"..."}}]</tool_calls>

or:

    <think>...</think>
    <final_answer>...</final_answer>
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Literal

from core.api.tagged_output import (
    TaggedOutputError,
    _parse_tool_call_block,
    _parse_tool_calls_block,
)


class _State(Enum):
    OUTSIDE = auto()
    IN_TAG = auto()
    IN_THINK = auto()
    IN_TOOL_CALL = auto()
    IN_TOOL_CALLS = auto()
    IN_FINAL_ANSWER = auto()


@dataclass(slots=True)
class TaggedStreamEvent:
    type: Literal[
        "message_start",
        "block_start",
        "block_delta",
        "block_end",
        "tool_call",
        "message_stop",
        "error",
    ]
    block_type: Literal["thinking", "text"] | None = None
    text: str | None = None
    call_index: int | None = None
    name: str | None = None
    arguments: dict[str, Any] | None = None
    stop_reason: Literal["tool_use", "end_turn"] | None = None
    error: str | None = None


class TaggedStreamParser:
    def __init__(self) -> None:
        self._state = _State.OUTSIDE
        self._state_before_tag = _State.OUTSIDE
        self._tag_buf = ""
        self._text_buf = ""
        self._message_started = False
        # Best-effort compatibility for non-conforming upstreams that emit some
        # plain text before switching to tagged protocol.
        self._preamble_open = False
        self._terminal_kind: Literal["tool_use", "end_turn"] | None = None
        self._saw_terminal = False
        self._terminal_closed = False
        self._message_stopped = False

    def feed(self, chunk: str) -> list[TaggedStreamEvent]:
        events: list[TaggedStreamEvent] = []
        for char in chunk:
            events.extend(self._on_char(char))
        if self._state in {_State.IN_THINK, _State.IN_FINAL_ANSWER} and self._text_buf:
            events.extend(self._flush_text_buffer())
        return events

    def finish(self) -> list[TaggedStreamEvent]:
        events: list[TaggedStreamEvent] = []
        if self._state == _State.IN_TAG:
            raise TaggedOutputError("incomplete tag at end of stream")
        if self._state == _State.IN_THINK:
            raise TaggedOutputError("missing </think>")
        if self._state == _State.IN_TOOL_CALL:
            raise TaggedOutputError("missing </tool_call>")
        if self._state == _State.IN_TOOL_CALLS:
            raise TaggedOutputError("missing </tool_calls>")
        if self._state == _State.IN_FINAL_ANSWER and not self._terminal_closed:
            # Allow plain-text fallback: treat unterminated final answer as an
            # implicitly closed text block if we already marked a terminal.
            if self._saw_terminal and self._terminal_kind == "end_turn":
                events.extend(self._flush_text_buffer())
                self._state = _State.OUTSIDE
                events.append(TaggedStreamEvent(type="block_end", block_type="text"))
            else:
                # If we were streaming a plain-text preamble, close it on EOF.
                if self._preamble_open:
                    events.extend(self._flush_text_buffer())
                    self._preamble_open = False
                    self._state = _State.OUTSIDE
                    events.append(
                        TaggedStreamEvent(type="block_end", block_type="text")
                    )
                else:
                    raise TaggedOutputError("missing </final_answer>")
        if not self._saw_terminal:
            # Tolerate non-conforming upstreams: if the stream ends without a
            # terminal block (<tool_call(s)> or <final_answer>) but we already
            # started emitting a message, downgrade to an end_turn stop.
            if self._message_started:
                self._saw_terminal = True
                self._terminal_kind = self._terminal_kind or "end_turn"
            else:
                raise TaggedOutputError("missing terminal block")
        if self._message_stopped:
            return events
        if self._terminal_kind is None:
            raise TaggedOutputError("missing stop reason")
        events.append(self._message_stop_event())
        return events

    def _on_char(self, char: str) -> list[TaggedStreamEvent]:
        if self._terminal_closed:
            return []

        if self._state == _State.OUTSIDE:
            if char.isspace():
                return []
            if char == "<":
                self._state_before_tag = self._state
                self._state = _State.IN_TAG
                self._tag_buf = "<"
                return []
            # Plain-text fallback: if the model doesn't follow the tagged
            # protocol and starts emitting bare text, start a best-effort
            # "preamble" text block. If tags appear later, we'll close this
            # block and continue parsing tagged protocol.
            events: list[TaggedStreamEvent] = []
            self._ensure_message_started(events)
            self._state = _State.IN_FINAL_ANSWER
            self._preamble_open = True
            self._text_buf = char
            events.append(TaggedStreamEvent(type="block_start", block_type="text"))
            return events

        if self._state == _State.IN_TAG:
            self._tag_buf += char
            if char != ">":
                return []
            tag = self._tag_buf
            self._tag_buf = ""
            return self._handle_tag(tag)

        if self._state in {_State.IN_THINK, _State.IN_FINAL_ANSWER}:
            if char == "<":
                events = self._flush_text_buffer()
                if (
                    self._preamble_open
                    and self._state == _State.IN_FINAL_ANSWER
                    and not self._saw_terminal
                ):
                    self._preamble_open = False
                    events.append(
                        TaggedStreamEvent(type="block_end", block_type="text")
                    )
                    # Preamble is outside; parse following tags at top-level.
                    self._state = _State.OUTSIDE
                self._state_before_tag = self._state
                self._state = _State.IN_TAG
                self._tag_buf = "<"
                return events
            self._text_buf += char
            return []

        if self._state in {_State.IN_TOOL_CALL, _State.IN_TOOL_CALLS}:
            if char == "<":
                self._state_before_tag = self._state
                self._state = _State.IN_TAG
                self._tag_buf = "<"
                return []
            self._text_buf += char
            return []

        raise TaggedOutputError(f"unexpected parser state: {self._state}")

    def _handle_tag(self, tag: str) -> list[TaggedStreamEvent]:
        events: list[TaggedStreamEvent] = []

        if tag == "<think>":
            self._ensure_message_started(events)
            self._state = _State.IN_THINK
            self._text_buf = ""
            events.append(TaggedStreamEvent(type="block_start", block_type="thinking"))
            return events

        if tag == "</think>":
            if self._state_before_tag != _State.IN_THINK:
                # 容错：上游/粘贴可能带进上一轮残留的 </think>，忽略不报错
                if self._state_before_tag == _State.OUTSIDE:
                    return []
                raise TaggedOutputError("unexpected </think>")
            events.extend(self._flush_text_buffer())
            self._state = _State.OUTSIDE
            events.append(TaggedStreamEvent(type="block_end", block_type="thinking"))
            return events

        if tag == "<tool_call>":
            if self._saw_terminal:
                raise TaggedOutputError("only one terminal block is allowed")
            self._ensure_message_started(events)
            self._saw_terminal = True
            self._terminal_kind = "tool_use"
            self._state = _State.IN_TOOL_CALL
            self._text_buf = ""
            return events

        if tag == "<tool_calls>":
            if self._saw_terminal:
                raise TaggedOutputError("only one terminal block is allowed")
            self._ensure_message_started(events)
            self._saw_terminal = True
            self._terminal_kind = "tool_use"
            self._state = _State.IN_TOOL_CALLS
            self._text_buf = ""
            return events

        if tag == "</tool_call>":
            if self._state_before_tag != _State.IN_TOOL_CALL:
                raise TaggedOutputError("unexpected </tool_call>")
            raw_json = self._text_buf.strip()
            self._text_buf = ""
            tool_calls = _parse_tool_call_block(raw_json)
            self._state = _State.OUTSIDE
            self._terminal_closed = True
            for index, tool_call in enumerate(tool_calls):
                events.append(
                    TaggedStreamEvent(
                        type="tool_call",
                        call_index=index,
                        name=tool_call.name,
                        arguments=tool_call.arguments,
                    )
                )
            return events

        if tag == "</tool_calls>":
            if self._state_before_tag != _State.IN_TOOL_CALLS:
                raise TaggedOutputError("unexpected </tool_calls>")
            raw_json = self._text_buf.strip()
            self._text_buf = ""
            tool_calls = _parse_tool_calls_block(raw_json)
            self._state = _State.OUTSIDE
            self._terminal_closed = True
            for index, tool_call in enumerate(tool_calls):
                events.append(
                    TaggedStreamEvent(
                        type="tool_call",
                        call_index=index,
                        name=tool_call.name,
                        arguments=tool_call.arguments,
                    )
                )
            return events

        if tag == "<final_answer>":
            if self._saw_terminal:
                raise TaggedOutputError("only one terminal block is allowed")
            self._ensure_message_started(events)
            self._saw_terminal = True
            self._terminal_kind = "end_turn"
            self._state = _State.IN_FINAL_ANSWER
            self._text_buf = ""
            events.append(TaggedStreamEvent(type="block_start", block_type="text"))
            return events

        if tag == "</final_answer>":
            if self._state_before_tag != _State.IN_FINAL_ANSWER:
                raise TaggedOutputError("unexpected </final_answer>")
            events.extend(self._flush_text_buffer())
            self._state = _State.OUTSIDE
            self._terminal_closed = True
            events.append(TaggedStreamEvent(type="block_end", block_type="text"))
            return events

        if self._state_before_tag in {
            _State.IN_THINK,
            _State.IN_TOOL_CALL,
            _State.IN_TOOL_CALLS,
            _State.IN_FINAL_ANSWER,
        }:
            self._state = self._state_before_tag
            self._text_buf += tag
            return events

        raise TaggedOutputError(f"unsupported tag: {tag}")

    def _ensure_message_started(self, events: list[TaggedStreamEvent]) -> None:
        if self._message_started:
            return
        self._message_started = True
        events.append(TaggedStreamEvent(type="message_start"))

    def _flush_text_buffer(self) -> list[TaggedStreamEvent]:
        if not self._text_buf:
            return []
        if self._state_before_tag == _State.IN_THINK or self._state == _State.IN_THINK:
            block_type: Literal["thinking", "text"] = "thinking"
        elif (
            self._state_before_tag == _State.IN_FINAL_ANSWER
            or self._state == _State.IN_FINAL_ANSWER
        ):
            block_type = "text"
        else:
            return []
        text = self._text_buf
        self._text_buf = ""
        return [
            TaggedStreamEvent(
                type="block_delta",
                block_type=block_type,
                text=text,
            )
        ]

    def _message_stop_event(self) -> TaggedStreamEvent:
        if self._terminal_kind is None:
            raise TaggedOutputError("missing stop reason")
        self._message_stopped = True
        return TaggedStreamEvent(
            type="message_stop",
            stop_reason=self._terminal_kind,
        )
