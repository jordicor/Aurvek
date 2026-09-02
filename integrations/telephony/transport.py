"""Canonical runtime bridge for Twilio Media Streams phone turns.

This module intentionally stops at the conversational boundary.  The WebSocket
session supplies final caller text and owns playback; Aurvek's existing
``process_save_message`` path continues to own prompt/model selection, tools,
watchdog, billing, persistence and Atagia.  The bridge exposes provisional text
only until the media session confirms the exact audible prefix.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import orjson
from fastapi.responses import StreamingResponse

from ai_runtime.channel_turns import (
    ChannelDraft,
    ChannelTurnHandle,
    TurnKey,
    channel_turn_registry,
)
from integrations.telephony.phone_context import PhoneChannelTurn


MAX_PHONE_TRANSCRIPT_CHARS = 100_000
MAX_SSE_BUFFER_BYTES = 1_048_576


class PhoneRuntimeBridgeError(RuntimeError):
    """The canonical runtime could not start or finish a phone turn."""


@dataclass(frozen=True, slots=True)
class PhoneRuntimeEvent:
    """One relevant event emitted by the canonical SSE response."""

    content: str | None = None
    terminal: str | None = None
    message_ids: Mapping[str, Any] | None = None
    persistence_error: bool = False


RuntimeInvoker = Callable[..., Awaitable[Any]]


class CanonicalPhoneTurn:
    """Live owner of one deferred canonical phone turn.

    Consuming the response body drives the normal runtime.  It remains open at
    the durable boundary until :meth:`confirm_audible` or :meth:`interrupt`
    resolves the deferred commit.
    """

    def __init__(
        self,
        *,
        key: TurnKey,
        handle: ChannelTurnHandle,
        response: StreamingResponse,
    ) -> None:
        self.key = key
        self.handle = handle
        self.response = response
        self._events: asyncio.Queue[PhoneRuntimeEvent | object] = asyncio.Queue()
        self._sentinel = object()
        self._draft_sentinel = object()
        self._error: BaseException | None = None
        self._consumer_task = asyncio.create_task(
            self._consume_response(),
            name=f"phone-runtime-{key.call_id}-{key.turn_id}",
        )
        self._draft_task = asyncio.create_task(
            self._notify_draft(),
            name=f"phone-draft-{key.call_id}-{key.turn_id}",
        )

    @property
    def draft(self) -> ChannelDraft | None:
        return self.handle.draft

    async def events_until_draft(self) -> AsyncIterator[PhoneRuntimeEvent]:
        """Yield provisional text/events until the final draft is published.

        The response body deliberately does not finish at this point: the
        canonical runtime is waiting for playback confirmation before writing
        assistant text, billing settlement and memory.
        """

        while True:
            item = await self._events.get()
            if item is self._draft_sentinel:
                await self._draft_task
                return
            if item is self._sentinel:
                if self._error is not None:
                    raise PhoneRuntimeBridgeError(
                        "Canonical phone response failed"
                    ) from self._error
                raise PhoneRuntimeBridgeError(
                    "Canonical phone response ended before a draft"
                )
            assert isinstance(item, PhoneRuntimeEvent)
            yield item

    async def wait_for_draft(self) -> ChannelDraft:
        return await self.handle.wait_for_draft()

    async def confirm_audible(
        self,
        text_prefix: str,
        *,
        played_ms: int,
    ) -> tuple[int | None, int | None]:
        """Confirm the final audible prefix and wait for canonical persistence."""

        self.handle.confirm_audible_prefix(text_prefix, played_ms=played_ms)
        await self._await_consumer()
        if not self.handle.committed:
            raise PhoneRuntimeBridgeError(
                "Canonical phone turn ended without a durable commit"
            )
        return await self.handle.wait_for_commit()

    async def interrupt(
        self,
        text_prefix: str,
        *,
        played_ms: int,
        reason: str = "barge_in",
    ) -> tuple[int | None, int | None]:
        """Persist an exact heard prefix (or caller-only at 0 ms) once."""

        result = await self.handle.interrupt_and_commit(
            text_prefix,
            played_ms=played_ms,
            reason=reason,
        )
        await self._await_consumer(allow_cancelled=True)
        await self._stop_draft_waiter()
        return result

    async def abort(self, reason: str) -> None:
        """Close a transport that cannot provide a trustworthy confirmation."""

        await self.handle.close_unfinished(str(reason or "phone_transport_failed"))
        if not self._consumer_task.done():
            self._consumer_task.cancel("phone_transport_failed")
        await self._await_consumer(allow_cancelled=True)
        await self._stop_draft_waiter()

    async def _consume_response(self) -> None:
        try:
            async for payload in iter_sse_payloads(self.response.body_iterator):
                event = _runtime_event(payload)
                if event is not None:
                    await self._events.put(event)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._error = exc
        finally:
            await self._events.put(self._sentinel)

    async def _notify_draft(self) -> None:
        await self.handle.wait_for_draft()
        await self._events.put(self._draft_sentinel)

    async def _await_consumer(self, *, allow_cancelled: bool = False) -> None:
        try:
            await self._consumer_task
        except asyncio.CancelledError:
            if not allow_cancelled:
                raise
        if self._error is not None:
            raise PhoneRuntimeBridgeError(
                "Canonical phone response failed"
            ) from self._error

    async def _stop_draft_waiter(self) -> None:
        if self._draft_task.done():
            return
        self._draft_task.cancel("phone_turn_finished_without_draft")
        try:
            await self._draft_task
        except asyncio.CancelledError:
            pass


async def start_canonical_phone_turn(
    *,
    conversation_id: int,
    current_user: Any,
    caller_text: str,
    phone_turn: PhoneChannelTurn,
    expected_llm_id: int,
    runtime_llm_id: int | None = None,
    reasoning_selection: Mapping[str, Any] | str | None = None,
    runtime_invoker: RuntimeInvoker | None = None,
) -> CanonicalPhoneTurn:
    """Start one final-STT phone turn through the normal Aurvek runtime."""

    text = str(caller_text or "").strip()
    if not text:
        raise ValueError("caller_text must contain a final transcript")
    if len(text) > MAX_PHONE_TRANSCRIPT_CHARS:
        raise ValueError("caller_text exceeds the phone transcript limit")
    resolved_runtime_llm_id = (
        int(expected_llm_id) if runtime_llm_id is None else int(runtime_llm_id)
    )
    if (
        int(conversation_id) <= 0
        or int(expected_llm_id) <= 0
        or resolved_runtime_llm_id <= 0
    ):
        raise ValueError("conversation and model identifiers must be positive")
    context = phone_turn.context
    if context.channel != "phone" or context.persistence != "deferred":
        raise ValueError("phone_turn must use deferred phone persistence")
    if context.turn_key is None:
        raise ValueError("phone_turn has no turn key")

    if runtime_invoker is None:
        from ai_runtime.messages import process_save_message

        runtime_invoker = process_save_message

    response = await runtime_invoker(
        None,
        int(conversation_id),
        current_user,
        text_plain=text,
        files=[],
        is_whatsapp=True,
        # Phone is an authenticated external channel, so request=None is
        # expected.  It must still pass wellbeing, AI rate-limit and activity
        # guards just like WhatsApp/Telegram.
        prevalidated=False,
        expected_llm_id=int(expected_llm_id),
        runtime_llm_id=resolved_runtime_llm_id,
        reasoning_selection=reasoning_selection,
        channel_context=context,
    )
    if not isinstance(response, StreamingResponse):
        detail = _response_error_detail(response)
        raise PhoneRuntimeBridgeError(
            f"Canonical phone turn was rejected{': ' + detail if detail else ''}"
        )
    handle = await channel_turn_registry.get(context.turn_key)
    if handle is None:
        raise PhoneRuntimeBridgeError(
            "Canonical runtime did not register the deferred phone turn"
        )
    return CanonicalPhoneTurn(
        key=context.turn_key,
        handle=handle,
        response=response,
    )


async def persist_canonical_phone_caller_turn(
    *,
    conversation_id: int,
    current_user: Any,
    caller_text: str,
    phone_turn: PhoneChannelTurn,
    expected_llm_id: int,
    runtime_llm_id: int | None = None,
    reasoning_selection: Mapping[str, Any] | str | None = None,
    runtime_invoker: RuntimeInvoker | None = None,
) -> int:
    """Persist one stopped-wire caller turn without starting model output."""

    text = str(caller_text or "").strip()
    if not text:
        raise ValueError("caller_text must contain a final transcript")
    if len(text) > MAX_PHONE_TRANSCRIPT_CHARS:
        raise ValueError("caller_text exceeds the phone transcript limit")
    resolved_runtime_llm_id = (
        int(expected_llm_id) if runtime_llm_id is None else int(runtime_llm_id)
    )
    if (
        int(conversation_id) <= 0
        or int(expected_llm_id) <= 0
        or resolved_runtime_llm_id <= 0
    ):
        raise ValueError("conversation and model identifiers must be positive")
    context = phone_turn.context
    if context.channel != "phone" or not context.ingest_only:
        raise ValueError("phone_turn must use ingest-only phone persistence")

    if runtime_invoker is None:
        from ai_runtime.messages import process_save_message

        runtime_invoker = process_save_message
    response = await runtime_invoker(
        None,
        int(conversation_id),
        current_user,
        text_plain=text,
        files=[],
        is_whatsapp=True,
        prevalidated=False,
        expected_llm_id=int(expected_llm_id),
        runtime_llm_id=resolved_runtime_llm_id,
        reasoning_selection=reasoning_selection,
        channel_context=context,
    )
    if not isinstance(response, StreamingResponse):
        detail = _response_error_detail(response)
        raise PhoneRuntimeBridgeError(
            f"Canonical phone ingest was rejected{': ' + detail if detail else ''}"
        )
    user_message_id: int | None = None
    async for payload in iter_sse_payloads(response.body_iterator):
        if payload.get("persistence_error"):
            raise PhoneRuntimeBridgeError("Canonical phone ingest persistence failed")
        if payload.get("terminal") == "queued_for_active_phone":
            try:
                user_message_id = int(payload["message_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise PhoneRuntimeBridgeError(
                    "Canonical phone ingest returned no message ID"
                ) from exc
    if user_message_id is None:
        raise PhoneRuntimeBridgeError("Canonical phone ingest did not complete")
    return user_message_id


async def iter_sse_payloads(body_iterator: Any) -> AsyncIterator[Mapping[str, Any]]:
    """Incrementally parse JSON ``data:`` events across arbitrary chunks."""

    buffer = b""
    data_lines: list[bytes] = []
    async for chunk in body_iterator:
        if chunk is None:
            continue
        if isinstance(chunk, str):
            raw = chunk.encode("utf-8")
        elif isinstance(chunk, (bytes, bytearray, memoryview)):
            raw = bytes(chunk)
        else:
            raw = str(chunk).encode("utf-8")
        buffer += raw
        if len(buffer) > MAX_SSE_BUFFER_BYTES:
            raise PhoneRuntimeBridgeError("Canonical SSE event exceeds its limit")
        while True:
            extracted = _pop_sse_line(buffer, final=False)
            if extracted is None:
                break
            line, buffer = extracted
            if line == b"":
                payload = _decode_sse_data(data_lines)
                data_lines.clear()
                if payload is not None:
                    yield payload
                continue
            if line.startswith(b":"):
                continue
            if line == b"data":
                data_lines.append(b"")
            elif line.startswith(b"data:"):
                value = line[5:]
                if value.startswith(b" "):
                    value = value[1:]
                data_lines.append(value)
    if buffer:
        extracted = _pop_sse_line(buffer, final=True)
        assert extracted is not None
        line, remainder = extracted
        if remainder:
            raise PhoneRuntimeBridgeError("Canonical SSE framing is invalid")
        if line == b"data":
            data_lines.append(b"")
        elif line.startswith(b"data:"):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(b" ") else value)
    payload = _decode_sse_data(data_lines)
    if payload is not None:
        yield payload


def _decode_sse_data(lines: list[bytes]) -> Mapping[str, Any] | None:
    if not lines:
        return None
    raw = b"\n".join(lines)
    try:
        decoded = orjson.loads(raw)
    except orjson.JSONDecodeError as exc:
        raise PhoneRuntimeBridgeError("Canonical runtime emitted invalid SSE JSON") from exc
    if not isinstance(decoded, Mapping):
        return None
    return decoded


def _pop_sse_line(
    buffer: bytes,
    *,
    final: bool,
) -> tuple[bytes, bytes] | None:
    """Extract one SSE line while preserving a CRLF split across chunks."""

    lf = buffer.find(b"\n")
    cr = buffer.find(b"\r")
    positions = [position for position in (lf, cr) if position >= 0]
    if not positions:
        return (buffer, b"") if final and buffer else None
    position = min(positions)
    if buffer[position : position + 1] == b"\r":
        if position + 1 == len(buffer) and not final:
            return None
        delimiter = 2 if buffer[position + 1 : position + 2] == b"\n" else 1
    else:
        delimiter = 1
    return buffer[:position], buffer[position + delimiter :]


def _runtime_event(payload: Mapping[str, Any]) -> PhoneRuntimeEvent | None:
    content = payload.get("content")
    terminal = payload.get("terminal")
    message_ids = payload.get("message_ids")
    persistence_error = payload.get("persistence_error") is True
    if not isinstance(content, str):
        content = None
    if not isinstance(terminal, str):
        terminal = None
    if not isinstance(message_ids, Mapping):
        message_ids = None
    if content is None and terminal is None and message_ids is None and not persistence_error:
        return None
    return PhoneRuntimeEvent(
        content=content,
        terminal=terminal,
        message_ids=message_ids,
        persistence_error=persistence_error,
    )


def _response_error_detail(response: Any) -> str | None:
    body = getattr(response, "body", None)
    if not isinstance(body, (bytes, bytearray)) or len(body) > 16_384:
        return None
    try:
        payload = orjson.loads(body)
    except orjson.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping):
        return None
    detail = payload.get("message") or payload.get("detail")
    return str(detail)[:500] if detail else None


__all__ = [
    "CanonicalPhoneTurn",
    "PhoneRuntimeBridgeError",
    "PhoneRuntimeEvent",
    "iter_sse_payloads",
    "persist_canonical_phone_caller_turn",
    "start_canonical_phone_turn",
]
