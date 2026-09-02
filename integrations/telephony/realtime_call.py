"""Call-scoped OpenAI Realtime transport for native telephone audio.

Twilio PCMU is appended to one persistent OpenAI session.  OpenAI owns VAD
and input transcription, while each committed input item receives a small
turn handle consumed by Aurvek's existing canonical runtime and playback.
This module deliberately owns neither persistence nor tool execution.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
import math
from typing import Any, Literal, TypeAlias

from log_config import logger

from integrations.telephony.openai_realtime import (
    ApiKeyProvider,
    OpenAIFunctionCallEvent,
    OpenAIInputTranscriptFailedEvent,
    OpenAIInputTranscriptEvent,
    OpenAIOutputAudioEvent,
    OpenAIOutputTextEvent,
    OpenAIProviderErrorEvent,
    OpenAIRealtimeClient,
    OpenAIRealtimeError,
    OpenAIRealtimeOptions,
    OpenAIRealtimeUsage,
    OpenAIResponseDoneEvent,
    OpenAISpeechEvent,
    OpenAITranscriptionUsage,
    SemanticVadOptions,
)
from integrations.telephony.realtime_bridge import (
    BridgeEvent,
    RealtimeDoneEvent,
    RealtimeErrorEvent,
    RealtimeStatusEvent,
    RealtimeToolCallEvent,
    RealtimeTranscriptEvent,
    RealtimeUsage,
)


DEFAULT_INPUT_TRANSCRIPTION_MODEL = "gpt-live-transcribe"
DEFAULT_INPUT_TRANSCRIPTION_TIMEOUT_SECONDS = 20.0
DEFAULT_RESPONSE_INACTIVITY_SECONDS = 20.0
DEFAULT_PENDING_TOOL_TIMEOUT_SECONDS = 60.0
DEFAULT_CANCEL_ACCOUNTING_SECONDS = 2.0
MAX_TIMEOUT_SECONDS = 300.0
MAX_HISTORY_ITEMS = 100
MAX_HISTORY_CHARS = 64 * 1024
MAX_INSTRUCTIONS_CHARS = 128 * 1024
MAX_INPUT_ITEMS = 2_048
MAX_QUEUE_SIZE = 4_096
_PCMU_BYTES_PER_MILLISECOND = 8
_END = object()

ClientFactory: TypeAlias = Callable[..., OpenAIRealtimeClient]
UsageUncertainHandler: TypeAlias = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class RealtimeCallSpeechStartedEvent:
    """OpenAI has admitted the beginning of one caller utterance."""

    item_id: str
    audio_offset_ms: int


@dataclass(frozen=True, slots=True)
class RealtimeCallTranscriptEvent:
    """Structural STT event consumed by the telephone utterance assembler."""

    item_id: str
    text: str
    is_final: bool
    speech_final: bool
    turn_handle: "RealtimeCallTurnHandle | None" = field(
        default=None,
        repr=False,
        compare=False,
    )
    start_seconds: float | None = None
    duration_seconds: float | None = None
    confidence: float | None = None
    usage: OpenAITranscriptionUsage | None = None


RealtimeCallInputEvent: TypeAlias = (
    RealtimeCallSpeechStartedEvent | RealtimeCallTranscriptEvent
)


@dataclass(slots=True)
class _ResponseAttempt:
    response_id: str | None = None
    provider_done: asyncio.Event = field(default_factory=asyncio.Event)
    accounting_done: asyncio.Event = field(default_factory=asyncio.Event)


class RealtimeCallTurnHandle:
    """One canonical Aurvek turn inside a persistent Realtime call."""

    _aurvek_internal_realtime_bridge = True

    def __init__(
        self,
        owner: "OpenAIRealtimeCallBridge",
        *,
        item_id: str,
        captured_input_pcmu_bytes: int,
        transcription_usage: OpenAITranscriptionUsage | None,
    ) -> None:
        if not item_id:
            raise ValueError("item_id is required")
        if (
            isinstance(captured_input_pcmu_bytes, bool)
            or not isinstance(captured_input_pcmu_bytes, int)
            or captured_input_pcmu_bytes <= 0
        ):
            raise ValueError("captured_input_pcmu_bytes must be positive")
        self._owner = owner
        self.item_id = item_id
        self._captured_input_pcmu_bytes = captured_input_pcmu_bytes
        self.transcription_usage = transcription_usage
        self._audio_queue: asyncio.Queue[Any] = asyncio.Queue(
            maxsize=owner.audio_queue_size
        )
        self._runtime_queue: asyncio.Queue[Any] = asyncio.Queue(
            maxsize=owner.event_queue_size
        )
        self._terminal_lock = asyncio.Lock()
        self._done = asyncio.Event()
        self._started = False
        self._closed = False
        self._finished = False
        self._status = "input_final"
        self._transcript_parts: list[str] = []
        self._text_delta_keys: set[tuple[str, str, int]] = set()
        self._usage = RealtimeUsage()
        self._pending_calls: dict[str, RealtimeToolCallEvent] = {}
        self._response_done: dict[str, asyncio.Event] = {}
        self._response_status: dict[str, str] = {}
        self._active_attempt: _ResponseAttempt | None = None
        self._current_response_id: str | None = None
        self._current_output_item_id: str | None = None
        self._current_content_index = 0
        self._watchdog_task: asyncio.Task[None] | None = None
        self._watchdog_generation = 0
        self._pending_tool_watchdog_task: asyncio.Task[None] | None = None
        self._pending_tool_watchdog_generation = 0

    @property
    def captured_input_pcmu_bytes(self) -> int:
        return self._captured_input_pcmu_bytes

    @property
    def started(self) -> bool:
        return self._started

    def set_usage_uncertain_handler(
        self, handler: UsageUncertainHandler | None
    ) -> None:
        self._owner.set_usage_uncertain_handler(handler)

    async def start_turn(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        instructions: str = "",
        tools: Sequence[Mapping[str, Any]] = (),
        tool_choice: Literal["auto", "none", "required"] = "auto",
        reasoning_effort: str | None = None,
        max_output_tokens: int | Literal["inf"] = "inf",
    ) -> None:
        await self._owner._start_turn(
            self,
            messages,
            instructions=instructions,
            tools=tools,
            tool_choice=tool_choice,
            reasoning_effort=reasoning_effort,
            max_output_tokens=max_output_tokens,
        )

    async def runtime_events(self) -> AsyncIterator[BridgeEvent]:
        while True:
            event = await self._runtime_queue.get()
            if event is _END:
                return
            yield event

    async def output_pcmu(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self._audio_queue.get()
            if chunk is _END:
                return
            yield chunk

    async def continue_function_call(self, call_id: str, output: Any) -> None:
        await self._owner._continue_function_call(self, call_id, output)

    async def cancel_output(self) -> None:
        await self._owner._cancel_output(self)

    async def truncate_output(self, *, played_ms: int) -> None:
        await self._owner._truncate_output(self, played_ms=played_ms)

    async def finish_pending_output(self) -> bool:
        return await self._owner._finish_pending_output(self)

    async def close(self) -> None:
        """Close only this turn; the call-scoped socket remains connected."""

        if self._closed:
            return
        self._closed = True
        if not self._finished:
            try:
                await self._owner._cancel_output(self)
            except BaseException:
                pass
            await self._owner._finish_handle(self, status="cancelled")


class OpenAIRealtimeCallBridge:
    """One persistent OpenAI Realtime connection for one Twilio call."""

    _aurvek_internal_realtime_bridge = True

    def __init__(
        self,
        *,
        api_key_provider: ApiKeyProvider,
        model: str,
        voice: str = "marin",
        input_transcription_model: str = DEFAULT_INPUT_TRANSCRIPTION_MODEL,
        client_factory: ClientFactory = OpenAIRealtimeClient,
        input_queue_size: int = 128,
        audio_queue_size: int = 128,
        event_queue_size: int = 128,
        input_transcription_timeout_seconds: float = (
            DEFAULT_INPUT_TRANSCRIPTION_TIMEOUT_SECONDS
        ),
        response_inactivity_seconds: float = (
            DEFAULT_RESPONSE_INACTIVITY_SECONDS
        ),
        pending_tool_timeout_seconds: float = (
            DEFAULT_PENDING_TOOL_TIMEOUT_SECONDS
        ),
        cancel_accounting_seconds: float = DEFAULT_CANCEL_ACCOUNTING_SECONDS,
        usage_uncertain_handler: UsageUncertainHandler | None = None,
    ) -> None:
        if not callable(api_key_provider):
            raise ValueError("api_key_provider must be callable")
        for value, name in (
            (input_queue_size, "input_queue_size"),
            (audio_queue_size, "audio_queue_size"),
            (event_queue_size, "event_queue_size"),
        ):
            if not isinstance(value, int) or not 1 <= value <= MAX_QUEUE_SIZE:
                raise ValueError(f"{name} must be between 1 and {MAX_QUEUE_SIZE}")
        self._input_transcription_timeout_seconds = _positive_timeout(
            input_transcription_timeout_seconds,
            "input_transcription_timeout_seconds",
        )
        self._response_inactivity_seconds = _positive_timeout(
            response_inactivity_seconds, "response_inactivity_seconds"
        )
        self._pending_tool_timeout_seconds = _positive_timeout(
            pending_tool_timeout_seconds, "pending_tool_timeout_seconds"
        )
        self._cancel_accounting_seconds = _positive_timeout(
            cancel_accounting_seconds, "cancel_accounting_seconds"
        )
        if usage_uncertain_handler is not None and not callable(
            usage_uncertain_handler
        ):
            raise ValueError("usage_uncertain_handler must be callable")
        self.audio_queue_size = audio_queue_size
        self.event_queue_size = event_queue_size
        self.options = OpenAIRealtimeOptions(
            model=model,
            voice=voice,
            vad=SemanticVadOptions(
                eagerness="auto",
                create_response=False,
                interrupt_response=False,
            ),
            input_transcription_model=input_transcription_model,
            parallel_tool_calls=False,
        )
        self._client = client_factory(
            api_key_provider=api_key_provider,
            options=self.options,
        )
        self._input_queue: asyncio.Queue[Any] = asyncio.Queue(
            maxsize=input_queue_size
        )
        self._usage_uncertain_handler = usage_uncertain_handler
        self._connect_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._response_lock = asyncio.Lock()
        self._consumer_task: asyncio.Task[None] | None = None
        self._consumer_error: BaseException | None = None
        self._active_handle: RealtimeCallTurnHandle | None = None
        self._handles: dict[str, RealtimeCallTurnHandle] = {}
        self._response_owners: dict[
            str, tuple[RealtimeCallTurnHandle, _ResponseAttempt]
        ] = {}
        self._speech_starts_ms: dict[str, int] = {}
        self._speech_ends_ms: dict[str, int] = {}
        self._pending_finals: dict[str, OpenAIInputTranscriptEvent] = {}
        self._input_transcription_watchdogs: dict[
            str, asyncio.Task[None]
        ] = {}
        self._input_pcmu_bytes_sent = 0
        self._finalized_audio_end_ms: int | None = None
        self._history_seeded = False
        self._base_history = ""
        self._events_claimed = False
        self._input_terminal = False
        self._closed = False
        self._finalized = False

    @property
    def connected(self) -> bool:
        return self._client.connected

    def set_usage_uncertain_handler(
        self, handler: UsageUncertainHandler | None
    ) -> None:
        if handler is not None and not callable(handler):
            raise ValueError("usage uncertainty handler must be callable")
        self._usage_uncertain_handler = handler

    async def connect(self) -> "OpenAIRealtimeCallBridge":
        async with self._connect_lock:
            if self._closed:
                raise RuntimeError("Realtime call bridge is closed")
            if not self._client.connected:
                await self._client.connect()
            if self._consumer_task is None:
                self._consumer_task = asyncio.create_task(
                    self._consume_provider_events(),
                    name="openai-realtime-call-events",
                )
        return self

    async def send_audio(self, audio: bytes | bytearray | memoryview) -> None:
        if self._closed or not self._client.connected:
            raise RuntimeError("Realtime call bridge is not connected")
        await self._client.append_audio(audio)
        self._input_pcmu_bytes_sent += len(audio)

    async def finalize(self) -> None:
        """Commit a possible last partial utterance without closing the call."""

        if self._closed or not self._client.connected:
            return
        if self._finalized:
            return
        self._finalized = True
        self._finalized_audio_end_ms = (
            self._input_pcmu_bytes_sent + _PCMU_BYTES_PER_MILLISECOND - 1
        ) // _PCMU_BYTES_PER_MILLISECOND
        await self._publish_pending_finals_after_finalize()
        await self._client.commit_audio()

    async def events(self) -> AsyncIterator[RealtimeCallInputEvent]:
        if self._events_claimed:
            raise RuntimeError("Realtime call input events already have a consumer")
        self._events_claimed = True
        while True:
            event = await self._input_queue.get()
            if event is _END:
                if self._consumer_error is not None:
                    raise OpenAIRealtimeError(
                        "OpenAI Realtime call event stream failed"
                    ) from self._consumer_error
                return
            yield event

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            await self._cancel_input_transcription_watchdogs()
            active = self._active_handle
            if active is not None and not active._finished:
                try:
                    await self._cancel_output(active)
                except BaseException:
                    pass
                await self._finish_handle(active, status="cancelled")
            await self._client.close()
            task = self._consumer_task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await self._finish_input_stream()

    async def _start_turn(
        self,
        handle: RealtimeCallTurnHandle,
        messages: Sequence[Mapping[str, Any]],
        *,
        instructions: str,
        tools: Sequence[Mapping[str, Any]],
        tool_choice: str,
        reasoning_effort: str | None,
        max_output_tokens: int | Literal["inf"],
    ) -> None:
        async with self._response_lock:
            self._require_owned_handle(handle)
            if handle._started:
                raise RuntimeError("Realtime call turn has already started")
            if self._active_handle is not None and not self._active_handle._finished:
                raise RuntimeError("another Realtime call turn is still active")
            if self._closed or not self._client.connected:
                raise RuntimeError("Realtime call bridge is not connected")
            turn_instructions = str(instructions or "")
            if not self._history_seeded:
                self._base_history = _history_instructions(messages[:-1])
            turn_instructions = _join_instructions(
                turn_instructions, self._base_history
            )
            handle._started = True
            handle._status = "in_progress"
            self._active_handle = handle
            response_submission_attempted = False
            try:
                update: dict[str, Any] = {
                    "instructions": turn_instructions,
                    "tools": tools,
                    "tool_choice": tool_choice,
                    "max_output_tokens": max_output_tokens,
                }
                if reasoning_effort is not None:
                    update["reasoning_effort"] = reasoning_effort
                await self._client.update_session(**update)
                self._history_seeded = True
                # This durable reservation fence must complete before any
                # provider response can begin consuming billable resources.
                await self._mark_usage_uncertain()
                self._begin_response_attempt(handle)
                response_submission_attempted = True
                await self._client.create_response()
                self._arm_watchdog(handle)
            except BaseException:
                try:
                    if response_submission_attempted:
                        await self._invalidate_after_uncertain_submission()
                finally:
                    await self._fail_handle(
                        handle,
                        "realtime_start_failed",
                        "OpenAI Realtime turn could not be started",
                    )
                raise

    async def _continue_function_call(
        self,
        handle: RealtimeCallTurnHandle,
        call_id: str,
        output: Any,
    ) -> None:
        self._require_owned_handle(handle)
        call = handle._pending_calls.get(call_id)
        if call is None:
            raise ValueError("unknown or completed Realtime function call")
        done = handle._response_done.setdefault(
            call.response_id, asyncio.Event()
        )
        await done.wait()
        if handle._response_status.get(call.response_id) != "completed":
            raise RuntimeError("cannot continue an unsuccessful Realtime response")
        await self._disarm_pending_tool_watchdog(handle)
        if (
            handle._finished
            or self._active_handle is not handle
            or not self._client.connected
            or self._consumer_error is not None
            or handle._pending_calls.get(call_id) is not call
        ):
            await self._fail_handle(
                handle,
                "realtime_continuation_unavailable",
                "OpenAI Realtime function call is no longer active",
            )
            raise RuntimeError("Realtime function call is no longer active")
        continuation_submission_attempted = False
        try:
            # Fence the new billable provider attempt before mutating its
            # conversation with the tool output.
            await self._mark_usage_uncertain()
            continuation_submission_attempted = True
            await self._client.send_function_output(call_id, output)
            handle._pending_calls.pop(call_id, None)
            self._begin_response_attempt(handle)
            await self._client.create_response()
            self._arm_watchdog(handle)
        except BaseException:
            try:
                if continuation_submission_attempted:
                    await self._invalidate_after_uncertain_submission()
            finally:
                await self._fail_handle(
                    handle,
                    "realtime_continuation_failed",
                    "OpenAI Realtime continuation could not be started",
                )
            raise

    async def _cancel_output(self, handle: RealtimeCallTurnHandle) -> None:
        self._require_owned_handle(handle)
        if not self._client.connected:
            return
        attempt = handle._active_attempt
        if attempt is None or attempt.provider_done.is_set():
            # ``response.done`` ends generation, not Twilio playback.  The
            # completed assistant item can still need truncation, but sending
            # response.cancel now would target no in-progress response.
            return
        cancel_error: BaseException | None = None
        try:
            await self._client.cancel_response(attempt.response_id)
        except BaseException as exc:
            cancel_error = exc
        if not await self._wait_for_accounting(attempt):
            try:
                await self._mark_usage_uncertain()
            finally:
                try:
                    await self._invalidate_after_uncertain_cancel()
                finally:
                    await self._finish_handle(handle, status="cancelled")
        if cancel_error is not None:
            raise cancel_error

    async def _truncate_output(
        self, handle: RealtimeCallTurnHandle, *, played_ms: int
    ) -> None:
        self._require_owned_handle(handle)
        if not isinstance(played_ms, int) or played_ms < 0:
            raise ValueError("played_ms must be a non-negative integer")
        if (
            not self._client.connected
            or handle._current_output_item_id is None
        ):
            return
        await self._client.truncate_item(
            handle._current_output_item_id,
            played_ms,
            content_index=handle._current_content_index,
        )

    async def _finish_pending_output(
        self, handle: RealtimeCallTurnHandle
    ) -> bool:
        self._require_owned_handle(handle)
        if handle._finished or not handle._pending_calls:
            return False
        handle._pending_calls.clear()
        await self._finish_handle(handle, status="tool_handled_externally")
        return True

    async def _consume_provider_events(self) -> None:
        try:
            async for event in self._client.events():
                if isinstance(event, OpenAISpeechEvent):
                    await self._handle_speech_event(event)
                elif isinstance(event, OpenAIInputTranscriptEvent):
                    await self._handle_input_transcript(event)
                elif isinstance(event, OpenAIInputTranscriptFailedEvent):
                    if await self._handle_input_transcript_failure(event):
                        return
                elif isinstance(event, OpenAIOutputAudioEvent):
                    owner = self._owner_for_response(event.response_id)
                    if owner is None:
                        continue
                    handle, attempt = owner
                    if handle is None or handle._finished:
                        continue
                    self._note_progress(handle, attempt, event.response_id)
                    handle._current_output_item_id = event.item_id
                    handle._current_content_index = event.content_index
                    if event.audio:
                        await handle._audio_queue.put(event.audio)
                elif isinstance(event, OpenAIOutputTextEvent):
                    owner = self._owner_for_response(event.response_id)
                    if owner is None:
                        continue
                    handle, attempt = owner
                    if handle is None or handle._finished:
                        continue
                    self._note_progress(handle, attempt, event.response_id)
                    await self._handle_output_text(handle, event)
                elif isinstance(event, OpenAIFunctionCallEvent):
                    owner = self._owner_for_response(event.response_id)
                    if owner is None:
                        continue
                    handle, attempt = owner
                    if handle is None or handle._finished:
                        continue
                    self._note_progress(handle, attempt, event.response_id)
                    if event.is_final:
                        call = RealtimeToolCallEvent(
                            call_id=event.call_id,
                            name=event.name or "",
                            arguments=event.arguments,
                            response_id=event.response_id,
                            item_id=event.item_id,
                        )
                        handle._pending_calls[event.call_id] = call
                        handle._response_done.setdefault(
                            event.response_id, asyncio.Event()
                        )
                        await handle._runtime_queue.put(call)
                elif isinstance(event, OpenAIResponseDoneEvent):
                    owner = self._owner_for_response(event.response_id)
                    if owner is not None:
                        handle, attempt = owner
                        await self._handle_response_done(handle, attempt, event)
                elif isinstance(event, OpenAIProviderErrorEvent):
                    logger.warning(
                        "OpenAI Realtime call provider error (error_type=%s)",
                        event.error_type,
                    )
                    handle = self._active_handle
                    if handle is not None and not handle._finished:
                        await self._fail_handle(handle, event.code, event.message)
                    else:
                        # With no response owner there is nobody else who can
                        # surface a session-level provider failure.  End the
                        # STT-compatible stream so the phone supervisor fails
                        # closed instead of leaving an apparently live call.
                        self._consumer_error = OpenAIRealtimeError(
                            "OpenAI Realtime call provider failed"
                        )
                        return
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._consumer_error = exc
            logger.warning(
                "OpenAI Realtime call event stream failed (exception_type=%s)",
                type(exc).__name__,
            )
            handle = self._active_handle
            if handle is not None and not handle._finished:
                await self._fail_handle(
                    handle,
                    "realtime_transport_error",
                    "OpenAI Realtime call transport failed",
                )
        finally:
            await self._cancel_input_transcription_watchdogs()
            await self._finish_input_stream()

    async def _handle_speech_event(self, event: OpenAISpeechEvent) -> None:
        if event.started:
            self._speech_starts_ms.setdefault(event.item_id, event.audio_offset_ms)
            await self._input_queue.put(
                RealtimeCallSpeechStartedEvent(
                    item_id=event.item_id,
                    audio_offset_ms=event.audio_offset_ms,
                )
            )
            return
        self._speech_ends_ms[event.item_id] = event.audio_offset_ms
        pending = self._pending_finals.pop(event.item_id, None)
        if pending is not None:
            await self._publish_final_input(pending)
        else:
            self._arm_input_transcription_watchdog(event.item_id)

    async def _handle_input_transcript(
        self, event: OpenAIInputTranscriptEvent
    ) -> None:
        if event.is_final:
            await self._disarm_input_transcription_watchdog(event.item_id)
            if self._consumer_error is not None or self._input_terminal:
                return
            if event.item_id in self._handles:
                return
            if event.item_id not in self._speech_ends_ms:
                derived_end_ms = self._finalized_speech_end_ms(event.item_id)
                if derived_end_ms is None:
                    self._pending_finals[event.item_id] = event
                    return
                self._speech_ends_ms[event.item_id] = derived_end_ms
            await self._publish_final_input(event)
            return
        await self._input_queue.put(
            RealtimeCallTranscriptEvent(
                item_id=event.item_id,
                text=event.text,
                is_final=False,
                speech_final=False,
            )
        )

    async def _handle_input_transcript_failure(
        self, event: OpenAIInputTranscriptFailedEvent
    ) -> bool:
        await self._disarm_input_transcription_watchdog(event.item_id)
        if (
            event.item_id in self._handles
            or event.item_id in self._pending_finals
        ):
            return False
        logger.warning(
            "OpenAI Realtime input transcription failed (error_type=%s)",
            event.error_type,
        )
        await self._terminate_input_transcription_failure(
            "realtime_input_transcription_failed"
        )
        return True

    async def _publish_final_input(
        self, event: OpenAIInputTranscriptEvent
    ) -> None:
        await self._disarm_input_transcription_watchdog(event.item_id)
        if event.item_id in self._handles:
            return
        if len(self._handles) >= MAX_INPUT_ITEMS:
            raise RuntimeError("Realtime call has too many input items")
        start_ms = self._speech_starts_ms.get(event.item_id, 0)
        end_ms = self._speech_ends_ms.get(event.item_id, start_ms)
        duration_ms = max(0, end_ms - start_ms)
        captured_bytes = max(1, duration_ms * _PCMU_BYTES_PER_MILLISECOND)
        handle = RealtimeCallTurnHandle(
            self,
            item_id=event.item_id,
            captured_input_pcmu_bytes=captured_bytes,
            transcription_usage=event.usage,
        )
        self._handles[event.item_id] = handle
        await self._input_queue.put(
            RealtimeCallTranscriptEvent(
                item_id=event.item_id,
                text=event.text,
                is_final=True,
                speech_final=True,
                turn_handle=handle,
                start_seconds=start_ms / 1_000,
                duration_seconds=duration_ms / 1_000,
                usage=event.usage,
            )
        )

    async def _publish_pending_finals_after_finalize(self) -> None:
        """Close already-final transcripts at the admitted PCMU frontier.

        Twilio Stop ends the media stream immediately, so semantic VAD may
        never receive the silence needed to emit ``speech_stopped``.  A final
        provider transcript is nevertheless safe to admit.  Its missing end
        is bounded by the exact amount of PCMU successfully appended before
        Stop; items without a corresponding speech start remain untrusted.
        """

        for item_id in tuple(self._pending_finals):
            derived_end_ms = self._finalized_speech_end_ms(item_id)
            if derived_end_ms is None:
                continue
            event = self._pending_finals.pop(item_id)
            self._speech_ends_ms[item_id] = derived_end_ms
            await self._publish_final_input(event)

    def _finalized_speech_end_ms(self, item_id: str) -> int | None:
        start_ms = self._speech_starts_ms.get(item_id)
        admitted_end_ms = self._finalized_audio_end_ms
        if start_ms is None or admitted_end_ms is None:
            return None
        # A malformed provider offset cannot manufacture billable duration
        # beyond audio actually admitted by this call bridge.
        return max(start_ms, admitted_end_ms)

    async def _handle_output_text(
        self, handle: RealtimeCallTurnHandle, event: OpenAIOutputTextEvent
    ) -> None:
        key = (event.channel, event.item_id, event.content_index)
        if not event.is_final:
            handle._text_delta_keys.add(key)
            text = event.text
        elif key not in handle._text_delta_keys:
            text = event.text
        else:
            text = ""
        if not text:
            return
        handle._transcript_parts.append(text)
        await handle._runtime_queue.put(
            RealtimeTranscriptEvent(
                text=text,
                response_id=event.response_id,
                item_id=event.item_id,
                channel=event.channel,
            )
        )

    async def _handle_response_done(
        self,
        handle: RealtimeCallTurnHandle,
        attempt: _ResponseAttempt,
        event: OpenAIResponseDoneEvent,
    ) -> None:
        async with handle._terminal_lock:
            if handle._finished or attempt.provider_done.is_set():
                return
            if handle._active_attempt is attempt:
                self._disarm_watchdog(handle)
            handle._usage = handle._usage.plus(event.usage)
            handle._status = event.status
            handle._response_status[event.response_id] = event.status
            handle._response_done.setdefault(
                event.response_id, asyncio.Event()
            ).set()
            await handle._runtime_queue.put(
                RealtimeStatusEvent(
                    event.response_id,
                    event.status,
                    handle._usage,
                    event.usage,
                    attempt.accounting_done,
                )
            )
            attempt.provider_done.set()
            if event.status != "completed":
                await self._finish_handle_locked(handle, status=event.status)
            elif any(
                call.response_id == event.response_id
                for call in handle._pending_calls.values()
            ):
                await self._arm_pending_tool_watchdog(handle)
            else:
                await self._finish_handle_locked(handle, status=event.status)

    async def _fail_handle(
        self,
        handle: RealtimeCallTurnHandle,
        code: str,
        message: str,
    ) -> None:
        async with handle._terminal_lock:
            if handle._finished:
                return
            await handle._runtime_queue.put(RealtimeErrorEvent(code, message))
            await self._finish_handle_locked(handle, status="failed")

    async def _finish_handle(
        self, handle: RealtimeCallTurnHandle, *, status: str
    ) -> None:
        async with handle._terminal_lock:
            if handle._finished:
                return
            await self._finish_handle_locked(handle, status=status)

    async def _finish_handle_locked(
        self, handle: RealtimeCallTurnHandle, *, status: str
    ) -> None:
        self._disarm_watchdog(handle)
        await self._disarm_pending_tool_watchdog(handle)
        handle._finished = True
        handle._status = status
        await handle._runtime_queue.put(
            RealtimeDoneEvent(
                "".join(handle._transcript_parts),
                handle._usage,
                status,
            )
        )
        await handle._runtime_queue.put(_END)
        await handle._audio_queue.put(_END)
        handle._done.set()
        if self._active_handle is handle:
            self._active_handle = None

    def _begin_response_attempt(self, handle: RealtimeCallTurnHandle) -> None:
        handle._active_attempt = _ResponseAttempt()
        handle._current_response_id = None
        handle._current_output_item_id = None
        handle._current_content_index = 0

    def _note_progress(
        self,
        handle: RealtimeCallTurnHandle,
        attempt: _ResponseAttempt,
        response_id: str,
    ) -> None:
        handle._current_response_id = response_id
        if handle._active_attempt is attempt:
            self._arm_watchdog(handle)

    def _owner_for_response(
        self, response_id: str
    ) -> tuple[RealtimeCallTurnHandle, _ResponseAttempt] | None:
        owner = self._response_owners.get(response_id)
        if owner is not None:
            return owner
        handle = self._active_handle
        if handle is None or handle._finished:
            return None
        attempt = handle._active_attempt
        if attempt is None:
            return None
        if attempt.response_id is None:
            attempt.response_id = response_id
            owner = (handle, attempt)
            self._response_owners[response_id] = owner
            return owner
        if attempt.response_id == response_id:
            owner = (handle, attempt)
            self._response_owners[response_id] = owner
            return owner
        # A different response is already bound to the only in-flight attempt.
        # This is necessarily a stale or duplicate provider event.
        return None

    async def _invalidate_after_uncertain_cancel(self) -> None:
        if self._consumer_error is None:
            self._consumer_error = OpenAIRealtimeError(
                "OpenAI Realtime cancellation accounting timed out"
            )
        try:
            await self._client.close()
        finally:
            await self._finish_input_stream()

    async def _invalidate_after_uncertain_submission(self) -> None:
        if self._consumer_error is None:
            self._consumer_error = OpenAIRealtimeError(
                "OpenAI Realtime response submission outcome is uncertain"
            )
        try:
            await self._client.close()
        finally:
            await self._finish_input_stream()

    def _arm_watchdog(self, handle: RealtimeCallTurnHandle) -> None:
        self._disarm_watchdog(handle)
        if handle._finished or self._closed or self._active_handle is not handle:
            return
        handle._watchdog_generation += 1
        generation = handle._watchdog_generation
        handle._watchdog_task = asyncio.create_task(
            self._watch_response_inactivity(handle, generation),
            name="openai-realtime-call-response-watchdog",
        )

    @staticmethod
    def _disarm_watchdog(
        handle: RealtimeCallTurnHandle,
    ) -> asyncio.Task[None] | None:
        handle._watchdog_generation += 1
        task = handle._watchdog_task
        handle._watchdog_task = None
        if (
            task is not None
            and task is not asyncio.current_task()
            and not task.done()
        ):
            task.cancel()
        return task

    async def _watch_response_inactivity(
        self,
        handle: RealtimeCallTurnHandle,
        generation: int,
    ) -> None:
        try:
            await asyncio.sleep(self._response_inactivity_seconds)
        except asyncio.CancelledError:
            return
        if (
            generation != handle._watchdog_generation
            or handle._finished
            or self._closed
            or self._active_handle is not handle
        ):
            return
        handle._watchdog_task = None
        logger.warning("OpenAI Realtime call response became inactive")
        attempt = handle._active_attempt
        try:
            await self._client.cancel_response(handle._current_response_id)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            logger.warning(
                "OpenAI Realtime call timeout cancellation failed "
                "(exception_type=%s)",
                type(exc).__name__,
            )
        if await self._wait_for_accounting(attempt):
            return
        try:
            await self._mark_usage_uncertain()
        except BaseException as exc:
            logger.warning(
                "OpenAI Realtime uncertain-usage marker failed "
                "(exception_type=%s)",
                type(exc).__name__,
            )
        await self._invalidate_after_uncertain_cancel()
        await self._fail_handle(
            handle,
            "realtime_response_timeout",
            "OpenAI Realtime response timed out",
        )

    def _arm_input_transcription_watchdog(self, item_id: str) -> None:
        if (
            not item_id
            or item_id in self._input_transcription_watchdogs
            or item_id in self._handles
            or self._closed
        ):
            return
        task = asyncio.create_task(
            self._watch_input_transcription(item_id),
            name="openai-realtime-call-transcription-watchdog",
        )
        self._input_transcription_watchdogs[item_id] = task

    async def _disarm_input_transcription_watchdog(
        self, item_id: str
    ) -> None:
        task = self._input_transcription_watchdogs.pop(item_id, None)
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _cancel_input_transcription_watchdogs(self) -> None:
        current = asyncio.current_task()
        tasks = tuple(
            task
            for task in self._input_transcription_watchdogs.values()
            if task is not current
        )
        self._input_transcription_watchdogs.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch_input_transcription(self, item_id: str) -> None:
        try:
            await asyncio.sleep(self._input_transcription_timeout_seconds)
        except asyncio.CancelledError:
            return
        if (
            self._input_transcription_watchdogs.get(item_id)
            is not asyncio.current_task()
            or self._closed
            or item_id in self._handles
        ):
            return
        self._input_transcription_watchdogs.pop(item_id, None)
        logger.warning("OpenAI Realtime input transcription timed out")
        await self._terminate_input_transcription_failure(
            "realtime_input_transcription_timeout"
        )

    async def _terminate_input_transcription_failure(self, code: str) -> None:
        if self._consumer_error is None:
            self._consumer_error = OpenAIRealtimeError(
                "OpenAI Realtime input transcription failed"
            )
        await self._cancel_input_transcription_watchdogs()
        handle = self._active_handle
        if handle is not None and not handle._finished:
            await self._fail_handle(
                handle,
                code,
                "OpenAI Realtime input transcription failed",
            )
        try:
            await self._client.close()
        finally:
            await self._finish_input_stream()

    async def _arm_pending_tool_watchdog(
        self, handle: RealtimeCallTurnHandle
    ) -> None:
        await self._disarm_pending_tool_watchdog(handle)
        if handle._finished or not handle._pending_calls or self._closed:
            return
        handle._pending_tool_watchdog_generation += 1
        generation = handle._pending_tool_watchdog_generation
        handle._pending_tool_watchdog_task = asyncio.create_task(
            self._watch_pending_tool(handle, generation),
            name="openai-realtime-call-tool-watchdog",
        )

    @staticmethod
    async def _disarm_pending_tool_watchdog(
        handle: RealtimeCallTurnHandle,
    ) -> None:
        handle._pending_tool_watchdog_generation += 1
        task = handle._pending_tool_watchdog_task
        handle._pending_tool_watchdog_task = None
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _watch_pending_tool(
        self,
        handle: RealtimeCallTurnHandle,
        generation: int,
    ) -> None:
        try:
            await asyncio.sleep(self._pending_tool_timeout_seconds)
        except asyncio.CancelledError:
            return
        if (
            generation != handle._pending_tool_watchdog_generation
            or handle._finished
            or self._closed
            or not handle._pending_calls
        ):
            return
        handle._pending_tool_watchdog_task = None
        handle._pending_calls.clear()
        logger.warning("OpenAI Realtime function call timed out")
        if self._consumer_error is None:
            self._consumer_error = OpenAIRealtimeError(
                "OpenAI Realtime function call timed out"
            )
        await self._cancel_input_transcription_watchdogs()
        await self._fail_handle(
            handle,
            "realtime_tool_timeout",
            "OpenAI Realtime function call timed out",
        )
        try:
            # A provider conversation containing a function_call without its
            # output is no longer safe to reuse for a later caller turn.
            await self._client.close()
        finally:
            await self._finish_input_stream()

    async def _wait_for_accounting(
        self, attempt: _ResponseAttempt | None
    ) -> bool:
        if attempt is None:
            return False
        try:
            await asyncio.wait_for(
                attempt.provider_done.wait(),
                timeout=self._cancel_accounting_seconds,
            )
            await asyncio.wait_for(
                attempt.accounting_done.wait(),
                timeout=self._cancel_accounting_seconds,
            )
        except TimeoutError:
            return False
        return True

    async def _mark_usage_uncertain(self) -> None:
        callback = self._usage_uncertain_handler
        if callback is None:
            return
        result = callback()
        if hasattr(result, "__await__"):
            await result

    async def _finish_input_stream(self) -> None:
        if self._input_terminal:
            return
        self._input_terminal = True
        await self._input_queue.put(_END)

    def _require_owned_handle(self, handle: RealtimeCallTurnHandle) -> None:
        if not isinstance(handle, RealtimeCallTurnHandle) or handle._owner is not self:
            raise ValueError("Realtime call turn does not belong to this call")


def _positive_timeout(value: Any, name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not 0 < float(value) <= MAX_TIMEOUT_SECONDS
    ):
        raise ValueError(f"{name} must be between 0 and {MAX_TIMEOUT_SECONDS}")
    return float(value)


def _history_instructions(messages: Sequence[Mapping[str, Any]]) -> str:
    if isinstance(messages, (str, bytes, bytearray)) or not isinstance(
        messages, Sequence
    ):
        raise ValueError("messages must be a sequence")
    entries: list[str] = []
    characters = 0
    for message in reversed(messages[-MAX_HISTORY_ITEMS:]):
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        if role not in {"system", "user", "assistant"}:
            continue
        content = _message_text(message.get("content"))
        if not content:
            continue
        entry = f"{role}: {content}"
        additional = len(entry) + (1 if entries else 0)
        if characters + additional > MAX_HISTORY_CHARS:
            break
        entries.append(entry)
        characters += additional
    if not entries:
        return ""
    entries.reverse()
    return (
        "Canonical conversation history before the current caller audio:\n"
        + "\n".join(entries)
    )


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, Sequence) or isinstance(
        content, (str, bytes, bytearray)
    ):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") not in {"text", "input_text", "output_text"}:
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts).strip()


def _join_instructions(instructions: str, history: str) -> str:
    result = "\n\n".join(part for part in (instructions.strip(), history) if part)
    if len(result) > MAX_INSTRUCTIONS_CHARS:
        raise ValueError("Realtime call instructions are too large")
    return result


__all__ = [
    "OpenAIRealtimeCallBridge",
    "RealtimeCallInputEvent",
    "RealtimeCallSpeechStartedEvent",
    "RealtimeCallTranscriptEvent",
    "RealtimeCallTurnHandle",
]
