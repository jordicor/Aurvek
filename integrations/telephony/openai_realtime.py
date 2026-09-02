"""Server-to-server OpenAI Realtime transport for 8 kHz phone audio.

The adapter deliberately owns no Twilio call, Aurvek conversation, tools,
billing, persistence, or hangup policy.  It exposes the GA Realtime WebSocket
protocol as bounded commands and small provider-neutral events so those
concerns can be wired by the phone session without duplicating them here.

Twilio Media Streams and the Realtime API both support headerless G.711
mu-law.  Input and output are therefore fixed to ``audio/pcmu`` and no audio
is decoded, transcoded, retained, or logged by this module.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
import inspect
import json
import math
import re
from typing import Any, Literal, TypeAlias
from urllib.parse import urlencode

import aiohttp


OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"
OPENAI_REALTIME_MODELS = frozenset(
    {"gpt-realtime-2.1", "gpt-realtime-2.1-mini"}
)
OPENAI_REALTIME_VOICES = frozenset(
    {
        "alloy",
        "ash",
        "ballad",
        "coral",
        "echo",
        "sage",
        "shimmer",
        "verse",
        "marin",
        "cedar",
    }
)
OPENAI_REALTIME_REASONING_EFFORTS = frozenset(
    {"minimal", "low", "medium", "high", "xhigh"}
)
OPENAI_REALTIME_AUDIO_FORMAT = "audio/pcmu"

OPENAI_REALTIME_MAX_AUDIO_CHUNK_BYTES = 256 * 1024
OPENAI_REALTIME_MAX_MESSAGE_BYTES = 4 * 1024 * 1024
OPENAI_REALTIME_MAX_AUDIO_DELTA_BYTES = 1024 * 1024
OPENAI_REALTIME_MAX_TEXT_CHARS = 128 * 1024
OPENAI_REALTIME_MAX_INSTRUCTIONS_CHARS = 128 * 1024
OPENAI_REALTIME_MAX_FUNCTION_ARGUMENTS_CHARS = 256 * 1024
OPENAI_REALTIME_MAX_FUNCTION_OUTPUT_CHARS = 256 * 1024
OPENAI_REALTIME_MAX_TOOLS_BYTES = 256 * 1024
OPENAI_REALTIME_MAX_TOOLS = 64

_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

ApiKeyProvider: TypeAlias = Callable[[], str | Awaitable[str]]
ReasoningEffort: TypeAlias = Literal[
    "minimal", "low", "medium", "high", "xhigh"
]


class _Unset:
    __slots__ = ()


_UNSET = _Unset()


class OpenAIRealtimeError(RuntimeError):
    """Base error for the OpenAI Realtime transport."""


class OpenAIRealtimeCredentialsError(OpenAIRealtimeError):
    """Raised when a usable API key cannot be resolved."""


class OpenAIRealtimeProtocolError(OpenAIRealtimeError):
    """Raised when the server sends malformed or unsupported data."""


class OpenAIRealtimeConnectionClosed(OpenAIRealtimeError):
    """Raised when an operation requires an open Realtime connection."""


@dataclass(frozen=True, slots=True)
class ServerVadOptions:
    """GA server VAD settings for amplitude-based endpointing."""

    threshold: float = 0.5
    prefix_padding_ms: int = 300
    silence_duration_ms: int = 500
    create_response: bool = False
    interrupt_response: bool = True
    idle_timeout_ms: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.threshold, (int, float))
            or isinstance(self.threshold, bool)
            or not math.isfinite(float(self.threshold))
            or not 0 <= float(self.threshold) <= 1
        ):
            raise ValueError("threshold must be between 0 and 1")
        _validate_int_range(
            self.prefix_padding_ms,
            "prefix_padding_ms",
            minimum=0,
            maximum=5_000,
        )
        _validate_int_range(
            self.silence_duration_ms,
            "silence_duration_ms",
            minimum=100,
            maximum=10_000,
        )
        if self.idle_timeout_ms is not None:
            _validate_int_range(
                self.idle_timeout_ms,
                "idle_timeout_ms",
                minimum=5_000,
                maximum=30_000,
            )
        _validate_bool(self.create_response, "create_response")
        _validate_bool(self.interrupt_response, "interrupt_response")

    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": "server_vad",
            "threshold": float(self.threshold),
            "prefix_padding_ms": self.prefix_padding_ms,
            "silence_duration_ms": self.silence_duration_ms,
            "create_response": self.create_response,
            "interrupt_response": self.interrupt_response,
        }
        if self.idle_timeout_ms is not None:
            result["idle_timeout_ms"] = self.idle_timeout_ms
        return result


@dataclass(frozen=True, slots=True)
class SemanticVadOptions:
    """GA semantic VAD settings for meaning-aware endpointing."""

    eagerness: Literal["low", "medium", "high", "auto"] = "auto"
    create_response: bool = False
    interrupt_response: bool = True

    def __post_init__(self) -> None:
        if self.eagerness not in {"low", "medium", "high", "auto"}:
            raise ValueError("eagerness must be low, medium, high, or auto")
        _validate_bool(self.create_response, "create_response")
        _validate_bool(self.interrupt_response, "interrupt_response")

    def payload(self) -> dict[str, Any]:
        return {
            "type": "semantic_vad",
            "eagerness": self.eagerness,
            "create_response": self.create_response,
            "interrupt_response": self.interrupt_response,
        }


VadOptions: TypeAlias = ServerVadOptions | SemanticVadOptions | None


@dataclass(frozen=True, slots=True)
class OpenAIRealtimeOptions:
    """Validated session configuration for the two enabled Realtime models."""

    model: str = "gpt-realtime-2.1-mini"
    voice: str = "marin"
    instructions: str = ""
    reasoning_effort: ReasoningEffort | None = "minimal"
    vad: VadOptions = field(default_factory=SemanticVadOptions)
    input_transcription_model: str | None = None
    tools: Sequence[Mapping[str, Any]] = ()
    tool_choice: str = "auto"
    parallel_tool_calls: bool = True
    max_output_tokens: int | Literal["inf"] = "inf"

    def __post_init__(self) -> None:
        if self.model not in OPENAI_REALTIME_MODELS:
            raise ValueError("unsupported OpenAI Realtime model")
        if self.voice not in OPENAI_REALTIME_VOICES:
            raise ValueError("unsupported OpenAI Realtime voice")
        if not isinstance(self.instructions, str):
            raise ValueError("instructions must be text")
        if len(self.instructions) > OPENAI_REALTIME_MAX_INSTRUCTIONS_CHARS:
            raise ValueError("instructions are too large")
        if (
            self.reasoning_effort is not None
            and self.reasoning_effort not in OPENAI_REALTIME_REASONING_EFFORTS
        ):
            raise ValueError("unsupported OpenAI Realtime reasoning effort")
        if self.vad is not None and not isinstance(
            self.vad, (ServerVadOptions, SemanticVadOptions)
        ):
            raise ValueError("vad must be server, semantic, or disabled")
        if self.input_transcription_model is not None:
            _validate_bounded_text(
                self.input_transcription_model,
                "input_transcription_model",
                maximum=128,
                allow_empty=False,
            )
        if self.tool_choice not in {"auto", "none", "required"}:
            raise ValueError("tool_choice must be auto, none, or required")
        _validate_bool(self.parallel_tool_calls, "parallel_tool_calls")
        if self.max_output_tokens != "inf":
            _validate_int_range(
                self.max_output_tokens,
                "max_output_tokens",
                minimum=1,
                maximum=32_000,
            )
        normalized_tools = _normalize_tools(self.tools)
        object.__setattr__(self, "tools", normalized_tools)

    @property
    def websocket_url(self) -> str:
        return f"{OPENAI_REALTIME_URL}?{urlencode({'model': self.model})}"

    def session_update(self) -> dict[str, Any]:
        audio_input: dict[str, Any] = {
            "format": {"type": OPENAI_REALTIME_AUDIO_FORMAT},
            "turn_detection": self.vad.payload() if self.vad is not None else None,
        }
        if self.input_transcription_model is not None:
            audio_input["transcription"] = {
                "model": self.input_transcription_model
            }
        session: dict[str, Any] = {
            "type": "realtime",
            "model": self.model,
            "output_modalities": ["audio"],
            "audio": {
                "input": audio_input,
                "output": {
                    "format": {"type": OPENAI_REALTIME_AUDIO_FORMAT},
                    "voice": self.voice,
                },
            },
            "instructions": self.instructions,
            "max_output_tokens": self.max_output_tokens,
            "parallel_tool_calls": self.parallel_tool_calls,
            "tool_choice": self.tool_choice,
            "tools": list(self.tools),
        }
        if self.reasoning_effort is not None:
            session["reasoning"] = {"effort": self.reasoning_effort}
        return {"type": "session.update", "session": session}


@dataclass(frozen=True, slots=True)
class OpenAISessionEvent:
    event_type: Literal["session.created", "session.updated"]
    session_id: str
    model: str
    expires_at: int | None = None


@dataclass(frozen=True, slots=True)
class OpenAISpeechEvent:
    started: bool
    item_id: str
    audio_offset_ms: int


@dataclass(frozen=True, slots=True)
class OpenAIInputTranscriptEvent:
    item_id: str
    text: str
    is_final: bool
    content_index: int = 0
    usage: OpenAITranscriptionUsage | None = None


@dataclass(frozen=True, slots=True)
class OpenAIInputTranscriptFailedEvent:
    """Terminal failure of the auxiliary transcript for one input item."""

    item_id: str
    content_index: int
    code: str
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class OpenAITranscriptionUsage:
    billing_unit: Literal["tokens", "duration"]
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    text_input_tokens: int = 0
    audio_input_tokens: int = 0
    seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class OpenAIOutputAudioEvent:
    response_id: str
    item_id: str
    content_index: int
    audio: bytes
    is_final: bool


@dataclass(frozen=True, slots=True)
class OpenAIOutputTextEvent:
    channel: Literal["text", "audio_transcript"]
    response_id: str
    item_id: str
    content_index: int
    text: str
    is_final: bool


@dataclass(frozen=True, slots=True)
class OpenAIFunctionCallEvent:
    response_id: str
    item_id: str
    call_id: str
    name: str | None
    arguments: str
    is_final: bool


@dataclass(frozen=True, slots=True)
class OpenAIRealtimeUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int
    text_input_tokens: int
    audio_input_tokens: int
    text_output_tokens: int
    audio_output_tokens: int
    reasoning_output_tokens: int
    cached_text_input_tokens: int = 0
    cached_audio_input_tokens: int = 0


@dataclass(frozen=True, slots=True)
class OpenAIResponseDoneEvent:
    response_id: str
    status: str
    usage: OpenAIRealtimeUsage | None


@dataclass(frozen=True, slots=True)
class OpenAIProviderErrorEvent:
    code: str
    error_type: str
    message: str
    param: str | None
    event_id: str | None


@dataclass(frozen=True, slots=True)
class OpenAIControlEvent:
    """A valid GA event which does not carry media or application output."""

    event_type: str
    event_id: str | None


OpenAIRealtimeEvent: TypeAlias = (
    OpenAISessionEvent
    | OpenAISpeechEvent
    | OpenAIInputTranscriptEvent
    | OpenAIInputTranscriptFailedEvent
    | OpenAIOutputAudioEvent
    | OpenAIOutputTextEvent
    | OpenAIFunctionCallEvent
    | OpenAIResponseDoneEvent
    | OpenAIProviderErrorEvent
    | OpenAIControlEvent
)


def parse_openai_realtime_message(
    payload: str | bytes | bytearray | Mapping[str, Any],
) -> OpenAIRealtimeEvent:
    """Translate one GA server event without retaining the provider payload."""

    message = _decode_message(payload)
    event_type = _required_text(message, "type", maximum=128)

    if event_type in {"session.created", "session.updated"}:
        session = _required_mapping(message, "session")
        return OpenAISessionEvent(
            event_type=event_type,
            session_id=_required_text(session, "id", maximum=256),
            model=_required_text(session, "model", maximum=128),
            expires_at=_optional_nonnegative_int(session.get("expires_at")),
        )

    if event_type in {
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
    }:
        offset_key = (
            "audio_start_ms"
            if event_type.endswith("speech_started")
            else "audio_end_ms"
        )
        return OpenAISpeechEvent(
            started=event_type.endswith("speech_started"),
            item_id=_required_text(message, "item_id", maximum=256),
            audio_offset_ms=_required_nonnegative_int(message, offset_key),
        )

    if event_type in {
        "conversation.item.input_audio_transcription.delta",
        "conversation.item.input_audio_transcription.completed",
    }:
        is_final = event_type.endswith("completed")
        text_key = "transcript" if is_final else "delta"
        return OpenAIInputTranscriptEvent(
            item_id=_required_text(message, "item_id", maximum=256),
            content_index=_optional_nonnegative_int(
                message.get("content_index")
            )
            or 0,
            text=(
                _optional_event_text(
                    message.get(text_key),
                    text_key,
                    maximum=OPENAI_REALTIME_MAX_TEXT_CHARS,
                )
                if not is_final
                else _required_text(
                    message,
                    text_key,
                    maximum=OPENAI_REALTIME_MAX_TEXT_CHARS,
                    allow_empty=True,
                )
            ),
            is_final=is_final,
            usage=(
                _parse_transcription_usage(message.get("usage"))
                if is_final
                else None
            ),
        )

    if event_type == "conversation.item.input_audio_transcription.failed":
        error = _required_mapping(message, "error")
        return OpenAIInputTranscriptFailedEvent(
            item_id=_required_text(message, "item_id", maximum=256),
            content_index=(
                _optional_nonnegative_int(message.get("content_index")) or 0
            ),
            code=(
                _safe_optional_text(error.get("code"), maximum=128)
                or "input_transcription_failed"
            ),
            error_type=(
                _safe_optional_text(error.get("type"), maximum=128)
                or "transcription_error"
            ),
            message=(
                _safe_optional_text(error.get("message"), maximum=2_048)
                or "OpenAI Realtime input transcription failed"
            ),
        )

    if event_type in {
        "response.output_audio.delta",
        "response.output_audio.done",
    }:
        is_final = event_type.endswith("done")
        audio = b"" if is_final else _decode_audio_delta(message)
        return OpenAIOutputAudioEvent(
            response_id=_required_text(message, "response_id", maximum=256),
            item_id=_required_text(message, "item_id", maximum=256),
            content_index=_required_nonnegative_int(message, "content_index"),
            audio=audio,
            is_final=is_final,
        )

    text_event_channels = {
        "response.output_text.delta": ("text", False, "delta"),
        "response.output_text.done": ("text", True, "text"),
        "response.output_audio_transcript.delta": (
            "audio_transcript",
            False,
            "delta",
        ),
        "response.output_audio_transcript.done": (
            "audio_transcript",
            True,
            "transcript",
        ),
    }
    if event_type in text_event_channels:
        channel, is_final, text_key = text_event_channels[event_type]
        return OpenAIOutputTextEvent(
            channel=channel,
            response_id=_required_text(message, "response_id", maximum=256),
            item_id=_required_text(message, "item_id", maximum=256),
            content_index=_required_nonnegative_int(message, "content_index"),
            text=_required_text(
                message,
                text_key,
                maximum=OPENAI_REALTIME_MAX_TEXT_CHARS,
                allow_empty=True,
            ),
            is_final=is_final,
        )

    if event_type in {
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
    }:
        is_final = event_type.endswith("done")
        arguments_key = "arguments" if is_final else "delta"
        return OpenAIFunctionCallEvent(
            response_id=_required_text(message, "response_id", maximum=256),
            item_id=_required_text(message, "item_id", maximum=256),
            call_id=_required_text(message, "call_id", maximum=256),
            name=(
                _required_text(message, "name", maximum=128)
                if is_final
                else _optional_event_text(
                    message.get("name"), "name", maximum=128
                )
                or None
            ),
            arguments=_required_text(
                message,
                arguments_key,
                maximum=OPENAI_REALTIME_MAX_FUNCTION_ARGUMENTS_CHARS,
                allow_empty=True,
            ),
            is_final=is_final,
        )

    if event_type == "response.done":
        response = _required_mapping(message, "response")
        usage_payload = response.get("usage")
        usage = (
            _parse_usage(usage_payload)
            if isinstance(usage_payload, Mapping)
            else None
        )
        return OpenAIResponseDoneEvent(
            response_id=_required_text(response, "id", maximum=256),
            status=_required_text(response, "status", maximum=64),
            usage=usage,
        )

    if event_type == "error":
        error = _required_mapping(message, "error")
        return OpenAIProviderErrorEvent(
            code=_safe_optional_text(error.get("code"), maximum=128)
            or "unknown_error",
            error_type=_safe_optional_text(error.get("type"), maximum=128)
            or "unknown_error",
            message=_safe_optional_text(error.get("message"), maximum=2_048)
            or "OpenAI Realtime request failed",
            param=_safe_optional_text(error.get("param"), maximum=256),
            event_id=_safe_optional_text(message.get("event_id"), maximum=256),
        )

    return OpenAIControlEvent(
        event_type=event_type,
        event_id=_safe_optional_text(message.get("event_id"), maximum=256),
    )


class OpenAIRealtimeClient:
    """Bounded, injectable aiohttp client for the GA Realtime WebSocket."""

    def __init__(
        self,
        *,
        api_key_provider: ApiKeyProvider,
        options: OpenAIRealtimeOptions | None = None,
        session: Any | None = None,
        session_factory: Callable[[], Any] = aiohttp.ClientSession,
        connect_timeout_seconds: float = 10.0,
        send_timeout_seconds: float = 5.0,
        receive_timeout_seconds: float | None = None,
        close_timeout_seconds: float = 2.0,
    ) -> None:
        if not callable(api_key_provider):
            raise ValueError("api_key_provider must be callable")
        for value, name in (
            (connect_timeout_seconds, "connect_timeout_seconds"),
            (send_timeout_seconds, "send_timeout_seconds"),
            (close_timeout_seconds, "close_timeout_seconds"),
        ):
            _validate_positive_number(value, name)
        if receive_timeout_seconds is not None:
            _validate_positive_number(
                receive_timeout_seconds, "receive_timeout_seconds"
            )

        self.options = options or OpenAIRealtimeOptions()
        self._api_key_provider = api_key_provider
        self._session = session
        self._session_factory = session_factory
        self._owns_session = session is None
        self._websocket: Any | None = None
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._send_timeout_seconds = float(send_timeout_seconds)
        self._receive_timeout_seconds = (
            float(receive_timeout_seconds)
            if receive_timeout_seconds is not None
            else None
        )
        self._close_timeout_seconds = float(close_timeout_seconds)
        self._send_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False

    @property
    def connected(self) -> bool:
        websocket = self._websocket
        return bool(
            websocket is not None
            and not self._closed
            and not getattr(websocket, "closed", False)
        )

    @property
    def websocket_url(self) -> str:
        return self.options.websocket_url

    async def connect(self) -> "OpenAIRealtimeClient":
        async with self._connect_lock:
            if self._closed:
                raise OpenAIRealtimeConnectionClosed(
                    "OpenAI Realtime client is already closed"
                )
            if self.connected:
                return self
            try:
                await asyncio.wait_for(
                    self._open_and_configure(),
                    timeout=self._connect_timeout_seconds,
                )
            except asyncio.CancelledError:
                await self._cleanup_after_connect_failure()
                raise
            except TimeoutError:
                await self._cleanup_after_connect_failure()
                raise OpenAIRealtimeConnectionClosed(
                    "OpenAI Realtime connection timed out"
                ) from None
            except OpenAIRealtimeError:
                await self._cleanup_after_connect_failure()
                raise
            except Exception:
                await self._cleanup_after_connect_failure()
                raise OpenAIRealtimeConnectionClosed(
                    "Unable to open OpenAI Realtime connection"
                ) from None
        return self

    async def _open_and_configure(self) -> None:
        api_key = await self._resolve_api_key()
        if self._session is None:
            self._session = self._session_factory()
        try:
            self._websocket = await self._session.ws_connect(
                self.websocket_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": "Aurvek-Telephony/1.0",
                },
                autoping=True,
                heartbeat=20.0,
                max_msg_size=OPENAI_REALTIME_MAX_MESSAGE_BYTES,
            )
            await self._send_json(self.options.session_update())
        finally:
            # The resolved key must not be retained after the handshake.
            api_key = ""

    async def append_audio(
        self, audio: bytes | bytearray | memoryview
    ) -> None:
        if not isinstance(audio, (bytes, bytearray, memoryview)):
            raise ValueError("audio must be bytes-like")
        raw = bytes(audio)
        if not raw:
            raise ValueError("audio cannot be empty")
        if len(raw) > OPENAI_REALTIME_MAX_AUDIO_CHUNK_BYTES:
            raise ValueError("audio chunk is too large")
        await self._send_json(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(raw).decode("ascii"),
            }
        )

    async def commit_audio(self) -> None:
        await self._send_json({"type": "input_audio_buffer.commit"})

    async def clear_audio(self) -> None:
        await self._send_json({"type": "input_audio_buffer.clear"})

    async def update_session(
        self,
        *,
        instructions: str | _Unset = _UNSET,
        tools: Sequence[Mapping[str, Any]] | _Unset = _UNSET,
        tool_choice: str | _Unset = _UNSET,
        max_output_tokens: int | Literal["inf"] | _Unset = _UNSET,
        reasoning_effort: ReasoningEffort | _Unset = _UNSET,
    ) -> None:
        """Apply a bounded partial GA session update.

        An omitted argument leaves the current provider value untouched.
        Empty instructions and an empty tool list intentionally clear those
        fields.
        """

        update: dict[str, Any] = {"type": "realtime"}
        if not isinstance(instructions, _Unset):
            _validate_bounded_text(
                instructions,
                "instructions",
                maximum=OPENAI_REALTIME_MAX_INSTRUCTIONS_CHARS,
                allow_empty=True,
            )
            update["instructions"] = instructions
        if not isinstance(tools, _Unset):
            update["tools"] = list(_normalize_tools(tools))
        if not isinstance(tool_choice, _Unset):
            _validate_tool_choice(tool_choice)
            update["tool_choice"] = tool_choice
        if not isinstance(max_output_tokens, _Unset):
            _validate_max_output_tokens(max_output_tokens)
            update["max_output_tokens"] = max_output_tokens
        if not isinstance(reasoning_effort, _Unset):
            if reasoning_effort not in OPENAI_REALTIME_REASONING_EFFORTS:
                raise ValueError(
                    "unsupported OpenAI Realtime reasoning effort"
                )
            update["reasoning"] = {"effort": reasoning_effort}
        if len(update) == 1:
            raise ValueError("at least one session field must be updated")
        await self._send_json({"type": "session.update", "session": update})

    async def create_conversation_item(
        self,
        text: str,
        *,
        role: Literal["system", "user", "assistant"] = "user",
        previous_item_id: str | None = None,
    ) -> None:
        """Append or insert one text Item for current context or history."""

        _validate_bounded_text(
            text,
            "text",
            maximum=OPENAI_REALTIME_MAX_TEXT_CHARS,
            allow_empty=False,
        )
        if role not in {"system", "user", "assistant"}:
            raise ValueError("role must be system, user, or assistant")
        content_type = "output_text" if role == "assistant" else "input_text"
        message: dict[str, Any] = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": role,
                "content": [{"type": content_type, "text": text}],
            },
        }
        if previous_item_id is not None:
            _validate_bounded_text(
                previous_item_id,
                "previous_item_id",
                maximum=256,
                allow_empty=False,
            )
            message["previous_item_id"] = previous_item_id
        await self._send_json(message)

    async def send_function_output(self, call_id: str, output: Any) -> None:
        """Add a tool result Item; caller triggers the next response separately."""

        _validate_bounded_text(
            call_id, "call_id", maximum=256, allow_empty=False
        )
        encoded_output = _encode_function_output(output)
        await self._send_json(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": encoded_output,
                },
            }
        )

    async def delete_item(self, item_id: str) -> None:
        """Delete one conversation Item by its provider identifier."""

        _validate_bounded_text(
            item_id, "item_id", maximum=256, allow_empty=False
        )
        await self._send_json(
            {"type": "conversation.item.delete", "item_id": item_id}
        )

    async def truncate_item(
        self,
        item_id: str,
        audio_end_ms: int,
        *,
        content_index: int = 0,
    ) -> None:
        _validate_bounded_text(item_id, "item_id", maximum=256, allow_empty=False)
        _validate_int_range(
            audio_end_ms, "audio_end_ms", minimum=0, maximum=86_400_000
        )
        _validate_int_range(
            content_index, "content_index", minimum=0, maximum=1_000
        )
        await self._send_json(
            {
                "type": "conversation.item.truncate",
                "item_id": item_id,
                "content_index": content_index,
                "audio_end_ms": audio_end_ms,
            }
        )

    async def cancel_response(self, response_id: str | None = None) -> None:
        message: dict[str, Any] = {"type": "response.cancel"}
        if response_id is not None:
            _validate_bounded_text(
                response_id, "response_id", maximum=256, allow_empty=False
            )
            message["response_id"] = response_id
        await self._send_json(message)

    async def create_response(self, instructions: str | None = None) -> None:
        message: dict[str, Any] = {"type": "response.create"}
        if instructions is not None:
            _validate_bounded_text(
                instructions,
                "instructions",
                maximum=OPENAI_REALTIME_MAX_INSTRUCTIONS_CHARS,
                allow_empty=True,
            )
            message["response"] = {"instructions": instructions}
        await self._send_json(message)

    async def events(self) -> AsyncIterator[OpenAIRealtimeEvent]:
        websocket = self._require_websocket()
        while True:
            try:
                receive = websocket.receive()
                if self._receive_timeout_seconds is None:
                    message = await receive
                else:
                    message = await asyncio.wait_for(
                        receive, timeout=self._receive_timeout_seconds
                    )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                raise OpenAIRealtimeConnectionClosed(
                    "OpenAI Realtime receive timed out"
                ) from None
            except (aiohttp.ClientError, RuntimeError):
                raise OpenAIRealtimeConnectionClosed(
                    "OpenAI Realtime WebSocket failed"
                ) from None

            if message.type in {aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY}:
                yield parse_openai_realtime_message(message.data)
                continue
            if message.type == aiohttp.WSMsgType.ERROR:
                raise OpenAIRealtimeConnectionClosed(
                    "OpenAI Realtime WebSocket failed"
                ) from None
            if message.type in {
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
            }:
                if self._closed:
                    return
                raise OpenAIRealtimeConnectionClosed(
                    "OpenAI Realtime WebSocket closed unexpectedly"
                ) from None
            if message.type not in {
                aiohttp.WSMsgType.PING,
                aiohttp.WSMsgType.PONG,
            }:
                raise OpenAIRealtimeProtocolError(
                    "OpenAI Realtime sent an unsupported WebSocket frame"
                )

    async def close(self) -> None:
        """Close transport resources within a bounded, idempotent operation."""

        async with self._connect_lock:
            async with self._close_lock:
                if self._closed:
                    return
                self._closed = True
                websocket = self._websocket
                self._websocket = None
                try:
                    if websocket is not None and not getattr(
                        websocket, "closed", False
                    ):
                        await self._bounded_close(websocket.close)
                finally:
                    if self._owns_session and self._session is not None:
                        await self._bounded_close(self._session.close)
                    self._session = None

    async def __aenter__(self) -> "OpenAIRealtimeClient":
        return await self.connect()

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    async def _send_json(self, payload: Mapping[str, Any]) -> None:
        async with self._send_lock:
            websocket = self._require_websocket()
            try:
                await asyncio.wait_for(
                    websocket.send_json(dict(payload)),
                    timeout=self._send_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                raise OpenAIRealtimeConnectionClosed(
                    "OpenAI Realtime send timed out"
                ) from None
            except (aiohttp.ClientError, RuntimeError):
                raise OpenAIRealtimeConnectionClosed(
                    "OpenAI Realtime WebSocket failed"
                ) from None

    def _require_websocket(self) -> Any:
        if not self.connected:
            raise OpenAIRealtimeConnectionClosed(
                "OpenAI Realtime connection is not open"
            )
        return self._websocket

    async def _resolve_api_key(self) -> str:
        try:
            provider = self._api_key_provider
            if inspect.iscoroutinefunction(provider):
                resolved = provider()
            else:
                resolved = await asyncio.to_thread(provider)
            if inspect.isawaitable(resolved):
                resolved = await resolved
            return _validate_api_key(resolved)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise OpenAIRealtimeCredentialsError(
                "Unable to resolve OpenAI API key"
            ) from None

    async def _cleanup_after_connect_failure(self) -> None:
        websocket = self._websocket
        self._websocket = None
        if websocket is not None and not getattr(websocket, "closed", False):
            await self._bounded_close(websocket.close)
        if self._owns_session and self._session is not None:
            session = self._session
            self._session = None
            await self._bounded_close(session.close)

    async def _bounded_close(
        self, close_callback: Callable[[], Awaitable[Any]]
    ) -> None:
        try:
            await asyncio.wait_for(
                close_callback(), timeout=self._close_timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, aiohttp.ClientError, RuntimeError):
            pass


def _decode_message(
    payload: str | bytes | bytearray | Mapping[str, Any],
) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, str):
        if len(payload.encode("utf-8")) > OPENAI_REALTIME_MAX_MESSAGE_BYTES:
            raise OpenAIRealtimeProtocolError("OpenAI Realtime message is too large")
        encoded: str | bytes = payload
    elif isinstance(payload, (bytes, bytearray)):
        if len(payload) > OPENAI_REALTIME_MAX_MESSAGE_BYTES:
            raise OpenAIRealtimeProtocolError("OpenAI Realtime message is too large")
        encoded = bytes(payload)
    else:
        raise OpenAIRealtimeProtocolError("OpenAI Realtime message is not JSON")
    try:
        decoded = json.loads(encoded)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        raise OpenAIRealtimeProtocolError(
            "OpenAI Realtime message is not valid JSON"
        ) from None
    if not isinstance(decoded, Mapping):
        raise OpenAIRealtimeProtocolError("OpenAI Realtime message is not an object")
    return decoded


def _decode_audio_delta(message: Mapping[str, Any]) -> bytes:
    delta = _required_text(
        message,
        "delta",
        maximum=((OPENAI_REALTIME_MAX_AUDIO_DELTA_BYTES + 2) // 3) * 4,
        allow_empty=True,
    )
    try:
        audio = base64.b64decode(delta, validate=True)
    except (ValueError, TypeError):
        raise OpenAIRealtimeProtocolError(
            "OpenAI Realtime audio delta is not valid base64"
        ) from None
    if len(audio) > OPENAI_REALTIME_MAX_AUDIO_DELTA_BYTES:
        raise OpenAIRealtimeProtocolError(
            "OpenAI Realtime audio delta is too large"
        )
    return audio


def _parse_usage(payload: Mapping[str, Any]) -> OpenAIRealtimeUsage:
    input_details = payload.get("input_token_details")
    output_details = payload.get("output_token_details")
    if not isinstance(input_details, Mapping):
        input_details = {}
    if not isinstance(output_details, Mapping):
        output_details = {}
    cached_input_details = input_details.get("cached_tokens_details")
    if not isinstance(cached_input_details, Mapping):
        cached_input_details = {}
    return OpenAIRealtimeUsage(
        input_tokens=_optional_nonnegative_int(payload.get("input_tokens")) or 0,
        output_tokens=_optional_nonnegative_int(payload.get("output_tokens")) or 0,
        total_tokens=_optional_nonnegative_int(payload.get("total_tokens")) or 0,
        cached_input_tokens=(
            _optional_nonnegative_int(input_details.get("cached_tokens")) or 0
        ),
        text_input_tokens=(
            _optional_nonnegative_int(input_details.get("text_tokens")) or 0
        ),
        audio_input_tokens=(
            _optional_nonnegative_int(input_details.get("audio_tokens")) or 0
        ),
        text_output_tokens=(
            _optional_nonnegative_int(output_details.get("text_tokens")) or 0
        ),
        audio_output_tokens=(
            _optional_nonnegative_int(output_details.get("audio_tokens")) or 0
        ),
        reasoning_output_tokens=(
            _optional_nonnegative_int(output_details.get("reasoning_tokens"))
            or 0
        ),
        cached_text_input_tokens=(
            _optional_nonnegative_int(cached_input_details.get("text_tokens"))
            or 0
        ),
        cached_audio_input_tokens=(
            _optional_nonnegative_int(cached_input_details.get("audio_tokens"))
            or 0
        ),
    )


def _parse_transcription_usage(value: Any) -> OpenAITranscriptionUsage | None:
    if not isinstance(value, Mapping):
        return None
    billing_unit = value.get("type")
    if billing_unit == "duration":
        seconds = value.get("seconds")
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(float(seconds))
            or seconds < 0
        ):
            seconds = 0.0
        return OpenAITranscriptionUsage(
            billing_unit="duration", seconds=float(seconds)
        )
    if billing_unit == "tokens":
        details = value.get("input_token_details")
        if not isinstance(details, Mapping):
            details = {}
        return OpenAITranscriptionUsage(
            billing_unit="tokens",
            input_tokens=_optional_nonnegative_int(value.get("input_tokens"))
            or 0,
            output_tokens=_optional_nonnegative_int(value.get("output_tokens"))
            or 0,
            total_tokens=_optional_nonnegative_int(value.get("total_tokens"))
            or 0,
            text_input_tokens=(
                _optional_nonnegative_int(details.get("text_tokens")) or 0
            ),
            audio_input_tokens=(
                _optional_nonnegative_int(details.get("audio_tokens")) or 0
            ),
        )
    return None


def _validate_tool(tool: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(tool, Mapping):
        raise ValueError("each Realtime tool must be an object")
    copied = dict(tool)
    if copied.get("type") != "function":
        raise ValueError("only function Realtime tools are supported")
    name = copied.get("name")
    if not isinstance(name, str) or not _TOOL_NAME_PATTERN.fullmatch(name):
        raise ValueError("Realtime function tool has an invalid name")
    parameters = copied.get("parameters")
    if parameters is not None and not isinstance(parameters, Mapping):
        raise ValueError("Realtime function parameters must be an object")
    description = copied.get("description")
    if description is not None:
        _validate_bounded_text(
            description,
            "tool description",
            maximum=4_096,
            allow_empty=True,
        )
    return copied


def _normalize_tools(
    tools: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    if isinstance(tools, (str, bytes, bytearray)) or not isinstance(
        tools, Sequence
    ):
        raise ValueError("Realtime tools must be a sequence")
    normalized = tuple(_validate_tool(tool) for tool in tools)
    if len(normalized) > OPENAI_REALTIME_MAX_TOOLS:
        raise ValueError("too many Realtime tools")
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("Realtime tools must be JSON serializable") from None
    if len(encoded) > OPENAI_REALTIME_MAX_TOOLS_BYTES:
        raise ValueError("Realtime tools are too large")
    return normalized


def _validate_tool_choice(value: Any) -> None:
    if value not in {"auto", "none", "required"}:
        raise ValueError("tool_choice must be auto, none, or required")


def _validate_max_output_tokens(value: Any) -> None:
    if value == "inf":
        return
    _validate_int_range(
        value, "max_output_tokens", minimum=1, maximum=32_000
    )


def _encode_function_output(output: Any) -> str:
    if isinstance(output, str):
        encoded = output
    else:
        try:
            encoded = json.dumps(
                output,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            raise ValueError("function output must be JSON serializable") from None
    _validate_bounded_text(
        encoded,
        "function output",
        maximum=OPENAI_REALTIME_MAX_FUNCTION_OUTPUT_CHARS,
        allow_empty=True,
    )
    return encoded


def _required_mapping(
    payload: Mapping[str, Any], key: str
) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise OpenAIRealtimeProtocolError(
            f"OpenAI Realtime event has no valid {key}"
        )
    return value


def _required_text(
    payload: Mapping[str, Any],
    key: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise OpenAIRealtimeProtocolError(
            f"OpenAI Realtime event has no valid {key}"
        )
    if len(value) > maximum:
        raise OpenAIRealtimeProtocolError(
            f"OpenAI Realtime event {key} is too large"
        )
    return value


def _safe_optional_text(value: Any, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    sanitized = "".join(character for character in value if character.isprintable())
    return sanitized[:maximum] or None


def _optional_event_text(value: Any, key: str, *, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise OpenAIRealtimeProtocolError(
            f"OpenAI Realtime event has no valid {key}"
        )
    if len(value) > maximum:
        raise OpenAIRealtimeProtocolError(
            f"OpenAI Realtime event {key} is too large"
        )
    return value


def _required_nonnegative_int(payload: Mapping[str, Any], key: str) -> int:
    value = _optional_nonnegative_int(payload.get(key))
    if value is None:
        raise OpenAIRealtimeProtocolError(
            f"OpenAI Realtime event has no valid {key}"
        )
    return value


def _optional_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _validate_api_key(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\r" in value
        or "\n" in value
    ):
        raise ValueError("OpenAI API key is required")
    return value.strip()


def _validate_positive_number(value: Any, name: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError(f"{name} must be positive")


def _validate_bool(value: Any, name: str) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be boolean")


def _validate_int_range(
    value: Any, name: str, *, minimum: int, maximum: int
) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


def _validate_bounded_text(
    value: Any,
    name: str,
    *,
    maximum: int,
    allow_empty: bool,
) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{name} must be text")
    if len(value) > maximum:
        raise ValueError(f"{name} is too large")
