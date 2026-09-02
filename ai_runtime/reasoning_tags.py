"""Normalize textual reasoning tags leaked by model providers.

Native provider reasoning arrives as structured events.  Some model/provider
combinations can instead put an internal ``antml:thinking>`` block in the text
channel.  This module keeps that block out of the assistant answer while still
allowing the live UI to render it as transient thinking.
"""

from __future__ import annotations

from typing import Literal


ReasoningEvent = tuple[
    Literal["thinking_start", "thinking", "thinking_end", "content"],
    str,
]

_OPENING_MARKERS = (
    "<antml:thinking>",
    "antml:thinking>",
    "<thinking>",
)
_CLOSING_MARKERS = (
    "</antml:thinking>",
    "</thinking>",
)


def split_tagged_thinking_prefix(content: str | None) -> tuple[str, str] | None:
    """Return ``(thinking, answer)`` for a complete leading tagged block."""
    if not isinstance(content, str) or not content:
        return None

    stripped = content.lstrip()
    lowered = stripped.lower()
    opening = next(
        (marker for marker in _OPENING_MARKERS if lowered.startswith(marker)),
        None,
    )
    if opening is None:
        return None

    body_start = len(opening)
    closing_candidates = [
        (lowered.find(marker, body_start), marker)
        for marker in _CLOSING_MARKERS
    ]
    closing_candidates = [item for item in closing_candidates if item[0] >= 0]
    if not closing_candidates:
        return None

    closing_at, closing = min(closing_candidates, key=lambda item: item[0])
    thinking = stripped[body_start:closing_at].lstrip("\r\n")
    answer = stripped[closing_at + len(closing):].lstrip()
    return thinking, answer


def strip_tagged_thinking_prefix(content: str | None) -> str | None:
    """Remove complete leading reasoning blocks, leaving the visible answer."""
    if not isinstance(content, str):
        return content

    answer = content
    while True:
        split = split_tagged_thinking_prefix(answer)
        if split is None:
            return answer
        _thinking, answer = split


def _partial_marker_suffix_length(value: str, markers: tuple[str, ...]) -> int:
    lowered = value.lower()
    max_length = min(len(value), max(len(marker) for marker in markers) - 1)
    for length in range(max_length, 0, -1):
        suffix = lowered[-length:]
        if any(marker.startswith(suffix) for marker in markers):
            return length
    return 0


class TaggedThinkingStreamParser:
    """Split a leading textual thinking block across arbitrary stream chunks."""

    def __init__(self) -> None:
        self._state: Literal["prefix", "thinking", "content"] = "prefix"
        self._buffer = ""

    def feed(self, chunk: str | None) -> list[ReasoningEvent]:
        if not chunk:
            return []
        if self._state == "content":
            return [("content", str(chunk))]

        self._buffer += str(chunk)
        if self._state == "prefix":
            return self._drain_prefix()
        return self._drain_thinking()

    def finalize(self) -> list[ReasoningEvent]:
        """Flush a partial normal prefix or close an unterminated thinking UI."""
        if self._state == "prefix":
            buffered, self._buffer = self._buffer, ""
            self._state = "content"
            return [("content", buffered)] if buffered else []
        if self._state == "thinking":
            buffered, self._buffer = self._buffer, ""
            self._state = "content"
            events: list[ReasoningEvent] = []
            if buffered:
                events.append(("thinking", buffered))
            events.append(("thinking_end", ""))
            return events
        return []

    def _drain_prefix(self) -> list[ReasoningEvent]:
        stripped = self._buffer.lstrip()
        lowered = stripped.lower()
        opening = next(
            (marker for marker in _OPENING_MARKERS if lowered.startswith(marker)),
            None,
        )
        if opening is not None:
            self._buffer = stripped[len(opening):].lstrip("\r\n")
            self._state = "thinking"
            return [("thinking_start", ""), *self._drain_thinking()]

        if not stripped or any(marker.startswith(lowered) for marker in _OPENING_MARKERS):
            return []

        buffered, self._buffer = self._buffer, ""
        self._state = "content"
        return [("content", buffered)]

    def _drain_thinking(self) -> list[ReasoningEvent]:
        lowered = self._buffer.lower()
        closing_candidates = [
            (lowered.find(marker), marker)
            for marker in _CLOSING_MARKERS
        ]
        closing_candidates = [item for item in closing_candidates if item[0] >= 0]
        if closing_candidates:
            closing_at, closing = min(closing_candidates, key=lambda item: item[0])
            thinking = self._buffer[:closing_at]
            answer = self._buffer[closing_at + len(closing):].lstrip()
            self._buffer = ""
            self._state = "content"
            events: list[ReasoningEvent] = []
            if thinking:
                events.append(("thinking", thinking))
            events.append(("thinking_end", ""))
            if answer:
                events.append(("content", answer))
            return events

        suffix_length = _partial_marker_suffix_length(
            self._buffer,
            _CLOSING_MARKERS,
        )
        emit_length = len(self._buffer) - suffix_length
        if emit_length <= 0:
            return []

        thinking = self._buffer[:emit_length]
        self._buffer = self._buffer[emit_length:]
        return [("thinking", thinking)]
