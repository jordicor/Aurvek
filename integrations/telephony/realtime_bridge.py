"""Per-turn orchestration for native OpenAI Realtime phone audio.

The bridge is intentionally below Aurvek's conversation runtime.  It moves
captured PCMU to one Realtime session, fans the single provider event stream
out to playback and the provider adapter, and supports tool continuations.
It does not own prompts, persistence, billing, memory, watchdogs, or hangup
policy.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import math
from typing import Any, Literal, TypeAlias

from log_config import logger

from integrations.telephony.openai_realtime import (
    ApiKeyProvider,
    OpenAIFunctionCallEvent,
    OpenAIOutputAudioEvent,
    OpenAIOutputTextEvent,
    OpenAIProviderErrorEvent,
    OpenAIRealtimeClient,
    OpenAIRealtimeError,
    OpenAIRealtimeOptions,
    OpenAIRealtimeUsage,
    OpenAIResponseDoneEvent,
)


MAX_HISTORY_ITEMS = 100
MAX_HISTORY_CHARS = 512 * 1024
MAX_INPUT_AUDIO_BYTES = 64 * 1024 * 1024
DEFAULT_RESPONSE_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_TIMEOUT_SECONDS = 300.0
DEFAULT_CANCEL_ACCOUNTING_TIMEOUT_SECONDS = 0.5

_END = object()
AudioInput: TypeAlias = (
    bytes | bytearray | memoryview | Iterable[bytes] | AsyncIterable[bytes]
)
ClientFactory: TypeAlias = Callable[..., OpenAIRealtimeClient]
UsageUncertainHandler: TypeAlias = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class RealtimeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    text_input_tokens: int = 0
    audio_input_tokens: int = 0
    text_output_tokens: int = 0
    audio_output_tokens: int = 0
    reasoning_output_tokens: int = 0
    cached_text_input_tokens: int = 0
    cached_audio_input_tokens: int = 0

    def plus(self, value: OpenAIRealtimeUsage | None) -> "RealtimeUsage":
        if value is None:
            return self
        return RealtimeUsage(
            **{
                name: getattr(self, name) + getattr(value, name)
                for name in self.__dataclass_fields__
            }
        )


@dataclass(frozen=True, slots=True)
class RealtimeTranscriptEvent:
    text: str
    response_id: str
    item_id: str
    channel: Literal["text", "audio_transcript"]


@dataclass(frozen=True, slots=True)
class RealtimeToolCallEvent:
    call_id: str
    name: str
    arguments: str
    response_id: str
    item_id: str


@dataclass(frozen=True, slots=True)
class RealtimeStatusEvent:
    response_id: str
    status: str
    usage: RealtimeUsage
    response_usage: OpenAIRealtimeUsage | None = None
    accounting_done: asyncio.Event | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def acknowledge_accounting(self) -> None:
        if self.accounting_done is not None:
            self.accounting_done.set()


@dataclass(frozen=True, slots=True)
class RealtimeErrorEvent:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class RealtimeDoneEvent:
    transcript: str
    usage: RealtimeUsage
    status: str


BridgeEvent: TypeAlias = (
    RealtimeTranscriptEvent
    | RealtimeToolCallEvent
    | RealtimeStatusEvent
    | RealtimeErrorEvent
    | RealtimeDoneEvent
)


@dataclass(slots=True)
class _ResponseAttempt:
    response_id: str | None = None
    provider_done: asyncio.Event = field(default_factory=asyncio.Event)
    accounting_done: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class _Cycle:
    publish_runtime: bool
    audio_queue: asyncio.Queue[Any]
    runtime_queue: asyncio.Queue[Any]
    done: asyncio.Event = field(default_factory=asyncio.Event)
    transcript_parts: list[str] = field(default_factory=list)
    usage: RealtimeUsage = field(default_factory=RealtimeUsage)
    status: str = "in_progress"
    text_delta_keys: set[tuple[str, str, int]] = field(default_factory=set)
    pending_calls: dict[str, RealtimeToolCallEvent] = field(default_factory=dict)
    response_done: dict[str, asyncio.Event] = field(default_factory=dict)
    response_status: dict[str, str] = field(default_factory=dict)
    active_attempt: _ResponseAttempt | None = None
    terminal_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    finished: bool = False


class RealtimeTurnBridge:
    """One native speech-to-speech model turn over one Realtime connection."""

    _aurvek_internal_realtime_bridge = True

    def __init__(
        self,
        *,
        api_key_provider: ApiKeyProvider,
        audio_input: AudioInput,
        model: str,
        voice: str = "marin",
        client_factory: ClientFactory = OpenAIRealtimeClient,
        audio_queue_size: int = 128,
        event_queue_size: int = 128,
        response_timeout_seconds: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS,
        cancel_accounting_timeout_seconds: float = (
            DEFAULT_CANCEL_ACCOUNTING_TIMEOUT_SECONDS
        ),
    ) -> None:
        if not callable(api_key_provider):
            raise ValueError("api_key_provider must be callable")
        if not isinstance(audio_queue_size, int) or not 1 <= audio_queue_size <= 4096:
            raise ValueError("audio_queue_size must be between 1 and 4096")
        if not isinstance(event_queue_size, int) or not 1 <= event_queue_size <= 4096:
            raise ValueError("event_queue_size must be between 1 and 4096")
        if (
            not isinstance(response_timeout_seconds, (int, float))
            or isinstance(response_timeout_seconds, bool)
            or not math.isfinite(float(response_timeout_seconds))
            or not 0 < float(response_timeout_seconds) <= MAX_RESPONSE_TIMEOUT_SECONDS
        ):
            raise ValueError("response_timeout_seconds must be between 0 and 300")
        if (
            not isinstance(cancel_accounting_timeout_seconds, (int, float))
            or isinstance(cancel_accounting_timeout_seconds, bool)
            or not math.isfinite(float(cancel_accounting_timeout_seconds))
            or float(cancel_accounting_timeout_seconds) <= 0
        ):
            raise ValueError("cancel_accounting_timeout_seconds must be positive")
        self._audio_input = audio_input
        self._audio_queue_size = audio_queue_size
        self._event_queue_size = event_queue_size
        self._response_timeout_seconds = float(response_timeout_seconds)
        self._cancel_accounting_timeout_seconds = float(
            cancel_accounting_timeout_seconds
        )
        self._client = client_factory(
            api_key_provider=api_key_provider,
            options=OpenAIRealtimeOptions(
                model=model,
                voice=voice,
                vad=None,
                reasoning_effort=None,
                input_transcription_model=None,
                parallel_tool_calls=False,
            ),
        )
        self._connect_lock = asyncio.Lock()
        self._response_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._consumer_task: asyncio.Task[None] | None = None
        self._response_watchdog_task: asyncio.Task[None] | None = None
        self._response_watchdog_generation = 0
        self._response_progress_generation = 0
        self._response_progress_event = asyncio.Event()
        self._usage_uncertain_handler: UsageUncertainHandler | None = None
        self._cycle: _Cycle | None = None
        self._primary_cycle: _Cycle | None = None
        self._started = False
        self._turn_ready = asyncio.Event()
        self._closed = False
        self._current_response_id: str | None = None
        self._current_item_id: str | None = None
        self._current_content_index = 0

    @property
    def connected(self) -> bool:
        return self._client.connected

    @property
    def started(self) -> bool:
        return self._started

    def set_usage_uncertain_handler(
        self, handler: UsageUncertainHandler | None
    ) -> None:
        if handler is not None and not callable(handler):
            raise ValueError("usage uncertainty handler must be callable")
        self._usage_uncertain_handler = handler

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
        """Load textual history, append captured PCMU, and request a response."""

        async with self._response_lock:
            if self._started:
                raise RuntimeError("Realtime turn has already started")
            self._started = True
            await self._ensure_connected()
            cycle = self._new_cycle(publish_runtime=True)
            self._cycle = cycle
            self._primary_cycle = cycle
            self._turn_ready.set()
            try:
                update: dict[str, Any] = {
                    "instructions": instructions,
                    "tools": tools,
                    "tool_choice": tool_choice,
                    "max_output_tokens": max_output_tokens,
                }
                if reasoning_effort is not None:
                    update["reasoning_effort"] = reasoning_effort
                await self._client.update_session(**update)
                for role, text in _text_history(messages[:-1]):
                    await self._client.create_conversation_item(text, role=role)

                sent = 0
                async for chunk in _iter_audio(self._audio_input):
                    sent += len(chunk)
                    if sent > MAX_INPUT_AUDIO_BYTES:
                        raise ValueError("captured phone audio is too large")
                    await self._client.append_audio(chunk)
                if sent == 0:
                    raise ValueError("captured phone audio is empty")
                await self._client.commit_audio()
                # The durable billing fence must exist before the provider is
                # allowed to begin any billable response generation.
                await self._mark_usage_uncertain()
                self._begin_response_attempt(cycle)
                await self._client.create_response()
                self._arm_response_watchdog(cycle)
            except BaseException:
                await self._fail_cycle(
                    cycle,
                    "realtime_start_failed",
                    "OpenAI Realtime turn could not be started",
                )
                raise

    async def runtime_events(self) -> AsyncIterator[BridgeEvent]:
        await self._turn_ready.wait()
        cycle = self._primary_cycle
        if cycle is None:
            return
        while True:
            event = await cycle.runtime_queue.get()
            if event is _END:
                return
            yield event

    async def output_pcmu(self) -> AsyncIterator[bytes]:
        await self._turn_ready.wait()
        cycle = self._primary_cycle
        if cycle is None:
            return
        async for chunk in self._cycle_audio(cycle):
            yield chunk

    async def continue_function_call(self, call_id: str, output: Any) -> None:
        cycle = self._primary_cycle
        if cycle is None or call_id not in cycle.pending_calls:
            raise ValueError("unknown or completed Realtime function call")
        call = cycle.pending_calls[call_id]
        done = cycle.response_done.setdefault(call.response_id, asyncio.Event())
        await done.wait()
        if cycle.response_status.get(call.response_id) != "completed":
            raise RuntimeError("cannot continue an unsuccessful Realtime response")
        try:
            await self._mark_usage_uncertain()
            await self._client.send_function_output(call_id, output)
            cycle.pending_calls.pop(call_id, None)
            self._begin_response_attempt(cycle)
            await self._client.create_response()
            self._arm_response_watchdog(cycle)
        except BaseException:
            await self._fail_cycle(
                cycle,
                "realtime_continuation_failed",
                "OpenAI Realtime continuation could not be started",
            )
            raise

    async def finish_pending_output(self) -> bool:
        """End an audio stream whose tool is completed outside Realtime.

        Direct phone tools such as hangup do not make a second provider call.
        Closing this cycle lets playback fall back to canonical deterministic TTS
        without inventing a function result or another model response.
        """

        cycle = self._primary_cycle
        if cycle is None or cycle.finished or not cycle.pending_calls:
            return False
        cycle.pending_calls.clear()
        await self._finish_cycle(cycle, status="tool_handled_externally")
        return True

    async def cancel_output(self) -> None:
        if self._closed or not self._client.connected:
            return
        cycle = self._cycle
        attempt = cycle.active_attempt if cycle is not None else None
        cancel_error: BaseException | None = None
        try:
            await self._client.cancel_response(self._current_response_id)
        except BaseException as exc:
            cancel_error = exc
        accounted = await self._wait_for_cancel_accounting(attempt)
        if not accounted:
            await self._mark_usage_uncertain()
        if cancel_error is not None:
            raise cancel_error

    async def truncate_output(self, *, played_ms: int) -> None:
        if not isinstance(played_ms, int) or played_ms < 0:
            raise ValueError("played_ms must be a non-negative integer")
        if self._closed or not self._client.connected or self._current_item_id is None:
            return
        await self._client.truncate_item(
            self._current_item_id,
            played_ms,
            content_index=self._current_content_index,
        )

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._turn_ready.set()
            watchdog = self._disarm_response_watchdog()
            if watchdog is not None:
                await asyncio.gather(watchdog, return_exceptions=True)
            await self._client.close()
            task = self._consumer_task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if self._cycle is not None and not self._cycle.finished:
                self._abort_cycle(self._cycle, status="cancelled")

    async def _ensure_connected(self) -> None:
        if self._closed:
            raise RuntimeError("Realtime bridge is closed")
        async with self._connect_lock:
            if not self._client.connected:
                await self._client.connect()
            if self._consumer_task is None:
                self._consumer_task = asyncio.create_task(
                    self._consume_events(), name="openai-realtime-turn-events"
                )

    def _new_cycle(self, *, publish_runtime: bool) -> _Cycle:
        return _Cycle(
            publish_runtime=publish_runtime,
            audio_queue=asyncio.Queue(maxsize=self._audio_queue_size),
            runtime_queue=asyncio.Queue(maxsize=self._event_queue_size),
        )

    async def _cycle_audio(self, cycle: _Cycle) -> AsyncIterator[bytes]:
        while True:
            chunk = await cycle.audio_queue.get()
            if chunk is _END:
                return
            yield chunk

    async def _consume_events(self) -> None:
        try:
            async for event in self._client.events():
                cycle = self._cycle
                if cycle is None or cycle.finished:
                    continue
                if isinstance(event, OpenAIOutputAudioEvent):
                    self._note_response_progress(cycle)
                    self._note_response_id(cycle, event.response_id)
                    self._current_response_id = event.response_id
                    self._current_item_id = event.item_id
                    self._current_content_index = event.content_index
                    if event.audio:
                        await cycle.audio_queue.put(event.audio)
                elif isinstance(event, OpenAIOutputTextEvent):
                    self._note_response_progress(cycle)
                    self._note_response_id(cycle, event.response_id)
                    await self._handle_text(cycle, event)
                elif isinstance(event, OpenAIFunctionCallEvent):
                    self._note_response_progress(cycle)
                    self._note_response_id(cycle, event.response_id)
                    if not event.is_final:
                        continue
                    call = RealtimeToolCallEvent(
                        call_id=event.call_id,
                        name=event.name or "",
                        arguments=event.arguments,
                        response_id=event.response_id,
                        item_id=event.item_id,
                    )
                    cycle.pending_calls[event.call_id] = call
                    cycle.response_done.setdefault(event.response_id, asyncio.Event())
                    if cycle.publish_runtime:
                        await cycle.runtime_queue.put(call)
                elif isinstance(event, OpenAIResponseDoneEvent):
                    await self._handle_response_done(cycle, event)
                elif isinstance(event, OpenAIProviderErrorEvent):
                    logger.warning(
                        "OpenAI Realtime provider reported an error "
                        "(error_type=%s)",
                        event.error_type,
                    )
                    await self._fail_cycle(cycle, event.code, event.message)
        except asyncio.CancelledError:
            raise
        except OpenAIRealtimeError as exc:
            logger.warning(
                "OpenAI Realtime transport failed (exception_type=%s)",
                type(exc).__name__,
            )
            if self._cycle is not None and not self._cycle.finished:
                await self._fail_cycle(
                    self._cycle, "realtime_transport_error", str(exc)
                )
        except Exception as exc:
            logger.warning(
                "OpenAI Realtime event processing failed (exception_type=%s)",
                type(exc).__name__,
            )
            if self._cycle is not None and not self._cycle.finished:
                await self._fail_cycle(
                    self._cycle,
                    "realtime_bridge_error",
                    "OpenAI Realtime processing failed",
                )
        else:
            if not self._closed and self._cycle is not None and not self._cycle.finished:
                await self._fail_cycle(
                    self._cycle,
                    "realtime_connection_closed",
                    "OpenAI Realtime connection closed before the response finished",
                )

    async def _handle_text(self, cycle: _Cycle, event: OpenAIOutputTextEvent) -> None:
        key = (event.channel, event.item_id, event.content_index)
        if not event.is_final:
            cycle.text_delta_keys.add(key)
            text = event.text
        elif key not in cycle.text_delta_keys:
            text = event.text
        else:
            text = ""
        if not text or not cycle.publish_runtime:
            return
        cycle.transcript_parts.append(text)
        await cycle.runtime_queue.put(
            RealtimeTranscriptEvent(
                text=text,
                response_id=event.response_id,
                item_id=event.item_id,
                channel=event.channel,
            )
        )

    async def _handle_response_done(
        self, cycle: _Cycle, event: OpenAIResponseDoneEvent
    ) -> None:
        async with cycle.terminal_lock:
            if cycle.finished:
                return
            self._disarm_response_watchdog()
            attempt = cycle.active_attempt
            if attempt is None:
                attempt = _ResponseAttempt(response_id=event.response_id)
                cycle.active_attempt = attempt
            elif attempt.response_id is None:
                attempt.response_id = event.response_id
            cycle.usage = cycle.usage.plus(event.usage)
            cycle.status = event.status
            cycle.response_status[event.response_id] = event.status
            cycle.response_done.setdefault(event.response_id, asyncio.Event()).set()
            if cycle.publish_runtime:
                await cycle.runtime_queue.put(
                    RealtimeStatusEvent(
                        event.response_id,
                        event.status,
                        cycle.usage,
                        event.usage,
                        attempt.accounting_done,
                    )
                )
            else:
                attempt.accounting_done.set()
            # Publish usage before releasing cancel/timeout waiters.  They in
            # turn wait for the adapter's durable accounting acknowledgement.
            attempt.provider_done.set()
            if event.status != "completed":
                await self._finish_cycle_locked(cycle, status=event.status)
                return
            if any(
                call.response_id == event.response_id
                for call in cycle.pending_calls.values()
            ):
                return
            await self._finish_cycle_locked(cycle, status=event.status)

    async def _fail_cycle(self, cycle: _Cycle, code: str, message: str) -> None:
        async with cycle.terminal_lock:
            if cycle.finished:
                return
            if cycle.publish_runtime:
                await cycle.runtime_queue.put(RealtimeErrorEvent(code, message))
            await self._finish_cycle_locked(cycle, status="failed")

    async def _finish_cycle(self, cycle: _Cycle, *, status: str) -> None:
        async with cycle.terminal_lock:
            if cycle.finished:
                return
            await self._finish_cycle_locked(cycle, status=status)

    async def _finish_cycle_locked(self, cycle: _Cycle, *, status: str) -> None:
        self._disarm_response_watchdog()
        cycle.finished = True
        cycle.status = status
        if cycle.publish_runtime:
            await cycle.runtime_queue.put(
                RealtimeDoneEvent("".join(cycle.transcript_parts), cycle.usage, status)
            )
            await cycle.runtime_queue.put(_END)
        await cycle.audio_queue.put(_END)
        cycle.done.set()

    def _arm_response_watchdog(self, cycle: _Cycle) -> None:
        self._disarm_response_watchdog()
        # A cancelled asyncio.sleep task has no exception to retrieve.  The
        # generation check still protects against a task already waking up.
        if self._closed or cycle.finished or cycle is not self._cycle:
            return
        self._response_watchdog_generation += 1
        generation = self._response_watchdog_generation
        progress_generation = self._response_progress_generation
        self._response_watchdog_task = asyncio.create_task(
            self._watch_response_inactivity(
                cycle,
                generation,
                progress_generation,
            ),
            name="openai-realtime-response-inactivity",
        )

    def _disarm_response_watchdog(self) -> asyncio.Task[None] | None:
        self._response_watchdog_generation += 1
        task = self._response_watchdog_task
        self._response_watchdog_task = None
        if (
            task is not None
            and task is not asyncio.current_task()
            and not task.done()
        ):
            task.cancel()
        return task

    async def _watch_response_inactivity(
        self,
        cycle: _Cycle,
        generation: int,
        progress_generation: int,
    ) -> None:
        while True:
            self._response_progress_event.clear()
            if progress_generation != self._response_progress_generation:
                progress_generation = self._response_progress_generation
                continue
            try:
                await asyncio.wait_for(
                    self._response_progress_event.wait(),
                    timeout=self._response_timeout_seconds,
                )
            except asyncio.CancelledError:
                return
            except TimeoutError:
                if progress_generation == self._response_progress_generation:
                    break
            progress_generation = self._response_progress_generation
        if (
            generation != self._response_watchdog_generation
            or self._closed
            or cycle.finished
            or cycle is not self._cycle
        ):
            return
        self._response_watchdog_task = None
        logger.warning("OpenAI Realtime response inactivity timeout exceeded")
        try:
            # Omitting a provider response ID also covers a response which has
            # not emitted its first event yet.
            await self._client.cancel_response()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "OpenAI Realtime timeout cancellation failed "
                "(exception_type=%s)",
                type(exc).__name__,
            )
        accounted = await self._wait_for_cancel_accounting(
            cycle.active_attempt
        )
        if not accounted:
            await self._mark_usage_uncertain()
        if cycle.finished:
            return
        await self._fail_cycle(
            cycle,
            "realtime_response_timeout",
            "OpenAI Realtime response timed out",
        )

    def _begin_response_attempt(self, cycle: _Cycle) -> None:
        cycle.active_attempt = _ResponseAttempt()
        self._current_response_id = None
        self._current_item_id = None
        self._current_content_index = 0

    def _note_response_id(self, cycle: _Cycle, response_id: str) -> None:
        attempt = cycle.active_attempt
        if attempt is not None and attempt.response_id is None:
            attempt.response_id = response_id

    def _note_response_progress(self, cycle: _Cycle) -> None:
        if not cycle.finished and cycle is self._cycle:
            self._response_progress_generation += 1
            self._response_progress_event.set()

    async def _wait_for_cancel_accounting(
        self, attempt: _ResponseAttempt | None
    ) -> bool:
        if attempt is None:
            return False
        deadline = (
            asyncio.get_running_loop().time()
            + self._cancel_accounting_timeout_seconds
        )
        for event in (attempt.provider_done, attempt.accounting_done):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except TimeoutError:
                return False
        return True

    async def _mark_usage_uncertain(self) -> None:
        handler = self._usage_uncertain_handler
        if handler is None:
            return
        result = handler()
        if hasattr(result, "__await__"):
            await result

    @staticmethod
    def _abort_cycle(cycle: _Cycle, *, status: str) -> None:
        """Terminate bounded queues without waiting for absent consumers."""

        if cycle.finished:
            return
        cycle.finished = True
        cycle.status = status
        for queue in (cycle.runtime_queue, cycle.audio_queue):
            while queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                queue.put_nowait(_END)
            except asyncio.QueueFull:
                pass
        cycle.done.set()


class RealtimeBridgeHandle:
    """Early playback handle, bound once the runtime resolves the canonical key."""

    _aurvek_internal_realtime_bridge = True

    def __init__(
        self,
        *,
        audio_input: AudioInput,
        voice: str = "marin",
        client_factory: ClientFactory = OpenAIRealtimeClient,
        ready_timeout_seconds: float = 30.0,
        response_timeout_seconds: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS,
        cancel_accounting_timeout_seconds: float = (
            DEFAULT_CANCEL_ACCOUNTING_TIMEOUT_SECONDS
        ),
    ) -> None:
        if not isinstance(ready_timeout_seconds, (int, float)) or ready_timeout_seconds <= 0:
            raise ValueError("ready_timeout_seconds must be positive")
        if (
            not isinstance(response_timeout_seconds, (int, float))
            or isinstance(response_timeout_seconds, bool)
            or not math.isfinite(float(response_timeout_seconds))
            or not 0 < float(response_timeout_seconds) <= MAX_RESPONSE_TIMEOUT_SECONDS
        ):
            raise ValueError("response_timeout_seconds must be between 0 and 300")
        if (
            not isinstance(cancel_accounting_timeout_seconds, (int, float))
            or isinstance(cancel_accounting_timeout_seconds, bool)
            or not math.isfinite(float(cancel_accounting_timeout_seconds))
            or float(cancel_accounting_timeout_seconds) <= 0
        ):
            raise ValueError("cancel_accounting_timeout_seconds must be positive")
        self._audio_input = audio_input
        self._captured_input_pcmu_bytes = _known_audio_input_size(audio_input)
        self._voice = voice
        self._client_factory = client_factory
        self._ready_timeout_seconds = float(ready_timeout_seconds)
        self._response_timeout_seconds = float(response_timeout_seconds)
        self._cancel_accounting_timeout_seconds = float(
            cancel_accounting_timeout_seconds
        )
        self._bridge: RealtimeTurnBridge | None = None
        self._bound_model: str | None = None
        self._ready = asyncio.Event()
        self._bind_lock = asyncio.Lock()
        self._closed = False

    @property
    def captured_input_pcmu_bytes(self) -> int | None:
        """Captured PCMU size, when known without consuming an async stream."""

        return self._captured_input_pcmu_bytes

    async def bind_provider(
        self, *, api_key_provider: ApiKeyProvider, model: str
    ) -> RealtimeTurnBridge:
        async with self._bind_lock:
            if self._closed:
                raise RuntimeError("Realtime bridge handle is closed")
            if self._bridge is None:
                self._bridge = RealtimeTurnBridge(
                    api_key_provider=api_key_provider,
                    audio_input=self._audio_input,
                    model=model,
                    voice=self._voice,
                    client_factory=self._client_factory,
                    response_timeout_seconds=self._response_timeout_seconds,
                    cancel_accounting_timeout_seconds=(
                        self._cancel_accounting_timeout_seconds
                    ),
                )
                self._bound_model = model
                self._ready.set()
            elif self._bound_model != model:
                raise RuntimeError("Realtime bridge handle is already bound")
            return self._bridge

    async def output_pcmu(self) -> AsyncIterator[bytes]:
        bridge = await self._wait_bridge()
        if bridge is None:
            return
        async for chunk in bridge.output_pcmu():
            yield chunk

    async def cancel_output(self) -> None:
        bridge = await self._wait_bridge(allow_timeout=True)
        if bridge is not None:
            await bridge.cancel_output()

    async def truncate_output(self, *, played_ms: int) -> None:
        bridge = await self._wait_bridge(allow_timeout=True)
        if bridge is not None:
            await bridge.truncate_output(played_ms=played_ms)

    async def finish_pending_output(self) -> bool:
        bridge = await self._wait_bridge(allow_timeout=True)
        if bridge is not None:
            return await bridge.finish_pending_output()
        return False

    async def close(self) -> None:
        async with self._bind_lock:
            if self._closed:
                return
            self._closed = True
            self._ready.set()
            bridge = self._bridge
        if bridge is not None:
            await bridge.close()

    async def _wait_bridge(
        self, *, allow_timeout: bool = False
    ) -> RealtimeTurnBridge | None:
        if not self._ready.is_set():
            if allow_timeout:
                return None
            try:
                await asyncio.wait_for(
                    self._ready.wait(), timeout=self._ready_timeout_seconds
                )
            except TimeoutError:
                raise RuntimeError("Realtime provider was not bound in time") from None
        if self._closed and self._bridge is None:
            return None
        return self._bridge


def _text_history(
    messages: Sequence[Mapping[str, Any]],
) -> list[tuple[Literal["system", "user", "assistant"], str]]:
    if isinstance(messages, (str, bytes, bytearray)) or not isinstance(messages, Sequence):
        raise ValueError("messages must be a sequence")
    result: list[tuple[Literal["system", "user", "assistant"], str]] = []
    total_chars = 0
    for message in messages[-MAX_HISTORY_ITEMS:]:
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        if role not in {"system", "user", "assistant"}:
            continue
        text = _message_text(message.get("content"))
        if not text:
            continue
        total_chars += len(text)
        if total_chars > MAX_HISTORY_CHARS:
            raise ValueError("Realtime textual history is too large")
        result.append((role, text))
    return result


def _known_audio_input_size(audio_input: AudioInput) -> int | None:
    if isinstance(audio_input, (bytes, bytearray, memoryview)):
        return len(audio_input)
    if isinstance(audio_input, Sequence) and not isinstance(
        audio_input, (str, bytes, bytearray)
    ):
        return sum(len(_audio_chunk(chunk)) for chunk in audio_input)
    return None


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes, bytearray)):
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


async def _iter_audio(audio_input: AudioInput) -> AsyncIterator[bytes]:
    if isinstance(audio_input, (bytes, bytearray, memoryview)):
        values: Any = (audio_input,)
    else:
        values = audio_input
    if isinstance(values, AsyncIterable):
        async for value in values:
            yield _audio_chunk(value)
    elif isinstance(values, Iterable):
        for value in values:
            yield _audio_chunk(value)
    else:
        raise ValueError("audio_input must be bytes or an iterable of bytes")


def _audio_chunk(value: Any) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError("audio_input chunks must be bytes-like")
    chunk = bytes(value)
    if not chunk:
        raise ValueError("audio_input chunks cannot be empty")
    return chunk
