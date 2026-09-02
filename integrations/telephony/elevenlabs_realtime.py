"""Low-latency ElevenLabs Scribe v2 Realtime adapter for phone audio.

Twilio Media Streams supplies headerless, mono 8 kHz mu-law bytes.  This
adapter preserves those bytes exactly, batches them into 100 ms messages, and
sends them directly to Scribe Realtime as ``ulaw_8000``.  It deliberately owns
no call, billing, persistence, or conversational state so provider selection
can be integrated independently by the phone media session.

Scribe does not currently emit a separate speech-start event.  For callers
that need a stable barge-in contract, :meth:`ElevenLabsRealtimeClient.events`
synthesizes one immediately before the first non-empty partial (or committed)
transcript in each segment.  A committed transcript is followed by an
utterance-end event.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
import inspect
import json
import re
import threading
from typing import Any, TypeAlias
from urllib.parse import urlencode

import aiohttp


ELEVENLABS_REALTIME_URL = (
    "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
)
ELEVENLABS_REALTIME_MODEL = "scribe_v2_realtime"
ELEVENLABS_AUDIO_FORMAT = "ulaw_8000"
ELEVENLABS_SAMPLE_RATE_HZ = 8_000
ELEVENLABS_COMMIT_STRATEGY = "vad"
ELEVENLABS_CONNECT_TIMEOUT_SECONDS = 5.0

# One byte per 8 kHz mu-law sample; 800 bytes therefore represents 100 ms.
ELEVENLABS_AUDIO_CHUNK_BYTES = 800

_LANGUAGE_PATTERN = re.compile(
    r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$"
)
_ERROR_MESSAGE_TYPES = frozenset(
    {
        "auth_error",
        "chunk_size_exceeded",
        "commit_throttled",
        "error",
        "input_error",
        "insufficient_audio_activity",
        "invalid_request",
        "queue_overflow",
        "quota_exceeded",
        "rate_limited",
        "resource_exhausted",
        "session_time_limit_exceeded",
        "transcriber_error",
        "unaccepted_terms",
    }
)

ApiKeyProvider: TypeAlias = Callable[[], str | Awaitable[str]]

_SYNC_API_KEY_PROBE_CAPACITY = 4
_SYNC_API_KEY_PROBE_EXECUTOR = ThreadPoolExecutor(
    max_workers=_SYNC_API_KEY_PROBE_CAPACITY,
    thread_name_prefix="elevenlabs-realtime-key",
)
_SYNC_API_KEY_PROBE_FLIGHTS: dict[
    int,
    tuple[ApiKeyProvider, Future[str | Awaitable[str]]],
] = {}
_SYNC_API_KEY_PROBE_GUARD = threading.Lock()
_SYNC_API_KEY_PROBE_POLL_SECONDS = 0.01


class ElevenLabsRealtimeError(RuntimeError):
    """Base error for the Scribe Realtime transport."""


class ElevenLabsCredentialsError(ElevenLabsRealtimeError):
    """Raised when a usable ElevenLabs API key cannot be resolved."""


class ElevenLabsProtocolError(ElevenLabsRealtimeError):
    """Raised for an invalid or unsupported provider message."""


class ElevenLabsProviderError(ElevenLabsRealtimeError):
    """Raised when Scribe reports an application-level failure.

    Provider descriptions are intentionally not retained or included in the
    exception string: upstream payloads must never become a path for exposing
    credentials or other sensitive request context.
    """

    def __init__(self, *, code: str) -> None:
        self.code = code
        super().__init__(f"ElevenLabs realtime transcription failed ({code})")


class ElevenLabsConnectionClosed(ElevenLabsRealtimeError):
    """Raised when an operation requires a healthy open connection."""


def _finish_sync_api_key_probe(
    provider_id: int,
    provider: ApiKeyProvider,
    future: Future[str | Awaitable[str]],
) -> None:
    with _SYNC_API_KEY_PROBE_GUARD:
        flight = _SYNC_API_KEY_PROBE_FLIGHTS.get(provider_id)
        if flight is not None and flight[0] is provider and flight[1] is future:
            del _SYNC_API_KEY_PROBE_FLIGHTS[provider_id]


def _sync_api_key_probe(
    provider: ApiKeyProvider,
) -> Future[str | Awaitable[str]]:
    """Return one bounded shared worker probe for a synchronous provider."""

    provider_id = id(provider)
    created = False
    with _SYNC_API_KEY_PROBE_GUARD:
        flight = _SYNC_API_KEY_PROBE_FLIGHTS.get(provider_id)
        if flight is not None and flight[0] is provider:
            future = flight[1]
        else:
            if (
                len(_SYNC_API_KEY_PROBE_FLIGHTS)
                >= _SYNC_API_KEY_PROBE_CAPACITY
            ):
                raise ElevenLabsCredentialsError(
                    "ElevenLabs API key resolution capacity is unavailable"
                )
            future = _SYNC_API_KEY_PROBE_EXECUTOR.submit(provider)
            _SYNC_API_KEY_PROBE_FLIGHTS[provider_id] = (provider, future)
            created = True
    if created:
        # A fast provider can finish before callback registration, so avoid
        # invoking its immediate callback while holding the non-reentrant lock.
        future.add_done_callback(
            lambda completed: _finish_sync_api_key_probe(
                provider_id,
                provider,
                completed,
            )
        )
    return future


async def _await_sync_api_key_probe(
    future: Future[str | Awaitable[str]],
) -> str | Awaitable[str]:
    # Polling avoids registering one persistent concurrent-future callback per
    # timed-out caller.  Only the single cleanup callback survives cancellation.
    while not future.done():
        await asyncio.sleep(_SYNC_API_KEY_PROBE_POLL_SECONDS)
    return future.result()


def _normalize_language(language: str) -> str | None:
    if not isinstance(language, str):
        raise ValueError("language must be 'auto', 'multi', or an ISO locale")
    normalized = language.strip()
    if normalized.lower() in {"auto", "multi"}:
        return None
    if not _LANGUAGE_PATTERN.fullmatch(normalized):
        raise ValueError("language must be 'auto', 'multi', or an ISO locale")
    # Scribe expects ISO-639-1/3, not a regional BCP-47 locale.  Only the
    # validated language subtag is sent (for example es-ES -> es).
    return re.split(r"[-_]", normalized, maxsplit=1)[0].lower()


@dataclass(frozen=True, slots=True)
class ElevenLabsRealtimeOptions:
    """Validated options fixed to Aurvek's raw phone-audio contract."""

    language: str = "multi"
    endpointing_ms: int = 700

    def __post_init__(self) -> None:
        language_code = _normalize_language(self.language)
        if (
            not isinstance(self.endpointing_ms, int)
            or isinstance(self.endpointing_ms, bool)
            or not 300 <= self.endpointing_ms <= 3_000
        ):
            raise ValueError("endpointing_ms must be between 300 and 3000")
        object.__setattr__(
            self,
            "language",
            (
                language_code
                if language_code is not None
                else self.language.strip().lower()
            ),
        )

    @property
    def language_code(self) -> str | None:
        return None if self.language in {"auto", "multi"} else self.language

    def query_items(self) -> tuple[tuple[str, str], ...]:
        silence_seconds = f"{self.endpointing_ms / 1000:.3f}".rstrip("0").rstrip(".")
        items: list[tuple[str, str]] = [
            ("model_id", ELEVENLABS_REALTIME_MODEL),
            ("audio_format", ELEVENLABS_AUDIO_FORMAT),
            ("commit_strategy", ELEVENLABS_COMMIT_STRATEGY),
            ("vad_silence_threshold_secs", silence_seconds),
        ]
        if self.language_code is not None:
            items.append(("language_code", self.language_code))
        return tuple(items)

    def websocket_url(self) -> str:
        return f"{ELEVENLABS_REALTIME_URL}?{urlencode(self.query_items())}"


@dataclass(frozen=True, slots=True)
class ElevenLabsMetadataEvent:
    """Scribe session identity and negotiated media configuration."""

    session_id: str
    model_id: str
    audio_format: str
    sample_rate_hz: int
    language_code: str | None


@dataclass(frozen=True, slots=True)
class ElevenLabsSpeechStartedEvent:
    """Synthetic start marker emitted once per non-empty Scribe segment."""

    timestamp_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ElevenLabsTranscriptEvent:
    """Provider-neutral interim/final transcript shape for session wiring."""

    text: str
    is_final: bool
    speech_final: bool
    from_finalize: bool = False


@dataclass(frozen=True, slots=True)
class ElevenLabsUtteranceEndEvent:
    """Synthetic end marker emitted immediately after a committed transcript."""

    last_word_end_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ElevenLabsWarningEvent:
    """Non-fatal Scribe warning supplied to the owning phone session."""

    code: str
    description: str


ElevenLabsRealtimeEvent: TypeAlias = (
    ElevenLabsMetadataEvent
    | ElevenLabsSpeechStartedEvent
    | ElevenLabsTranscriptEvent
    | ElevenLabsUtteranceEndEvent
    | ElevenLabsWarningEvent
)


def parse_elevenlabs_message(
    payload: str | bytes | bytearray | Mapping[str, Any],
) -> ElevenLabsMetadataEvent | ElevenLabsTranscriptEvent | ElevenLabsWarningEvent:
    """Parse one provider JSON message, rejecting unknown protocol changes."""

    message = _decode_message(payload)
    message_type = message.get("message_type")
    if not isinstance(message_type, str) or not message_type:
        raise ElevenLabsProtocolError("ElevenLabs message has no message_type")

    if message_type == "session_started":
        return _parse_session_started(message)
    if message_type == "partial_transcript":
        return ElevenLabsTranscriptEvent(
            text=_required_text(message, "text"),
            is_final=False,
            speech_final=False,
        )
    if message_type == "committed_transcript":
        return ElevenLabsTranscriptEvent(
            text=_required_text(message, "text"),
            is_final=True,
            speech_final=True,
        )
    if message_type == "warning":
        return ElevenLabsWarningEvent(
            code="warning",
            description=_required_text(message, "warning"),
        )
    if message_type in _ERROR_MESSAGE_TYPES:
        raise ElevenLabsProviderError(code=message_type)
    raise ElevenLabsProtocolError("ElevenLabs sent an unsupported message type")


class ElevenLabsRealtimeClient:
    """Injectable aiohttp client for Scribe v2 Realtime.

    Exactly one credential source is required.  Synchronous providers run in
    a worker thread and async providers are awaited, so resolving a key cannot
    block the media event loop.  A supplied HTTP session remains caller-owned;
    a session built by ``session_factory`` is closed by this client.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        api_key_provider: ApiKeyProvider | None = None,
        options: ElevenLabsRealtimeOptions | None = None,
        session: Any | None = None,
        session_factory: Callable[[], Any] = aiohttp.ClientSession,
        connect_timeout_seconds: float = ELEVENLABS_CONNECT_TIMEOUT_SECONDS,
        close_timeout_seconds: float = 2.0,
    ) -> None:
        if (api_key is None) == (api_key_provider is None):
            raise ValueError("provide exactly one ElevenLabs credential source")
        if api_key is not None:
            _validate_api_key(api_key)
            self._api_key = api_key.strip()
        else:
            self._api_key = None
        if not callable(api_key_provider) and api_key_provider is not None:
            raise ValueError("api_key_provider must be callable")
        for value, name in (
            (connect_timeout_seconds, "connect_timeout_seconds"),
            (close_timeout_seconds, "close_timeout_seconds"),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value <= 0
            ):
                raise ValueError(f"{name} must be positive")

        self._api_key_provider = api_key_provider
        self.options = options or ElevenLabsRealtimeOptions()
        self._session = session
        self._session_factory = session_factory
        self._owns_session = session is None
        self._websocket: Any | None = None
        self._audio_buffer = bytearray()
        self._send_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._close_timeout_seconds = float(close_timeout_seconds)
        self._closed = False
        self._session_started = False
        self._pending_session_started: ElevenLabsMetadataEvent | None = None
        self._segment_started = False
        self._finalize_requested = False

    @property
    def connected(self) -> bool:
        websocket = self._websocket
        return bool(
            websocket is not None
            and self._session_started
            and not self._closed
            and not getattr(websocket, "closed", False)
        )

    @property
    def websocket_url(self) -> str:
        return self.options.websocket_url()

    @property
    def pending_audio_bytes(self) -> int:
        return len(self._audio_buffer)

    async def connect(self) -> "ElevenLabsRealtimeClient":
        async with self._connect_lock:
            if self._closed:
                raise ElevenLabsConnectionClosed(
                    "ElevenLabs realtime client is already closed"
                )
            if self.connected:
                return self
            try:
                await asyncio.wait_for(
                    self._open_websocket_and_wait_for_session_started(),
                    timeout=self._connect_timeout_seconds,
                )
            except asyncio.CancelledError:
                await self._cleanup_after_connect_failure()
                raise
            except TimeoutError:
                await self._cleanup_after_connect_failure()
                raise ElevenLabsConnectionClosed(
                    "ElevenLabs realtime connection timed out"
                ) from None
            except ElevenLabsRealtimeError:
                await self._cleanup_after_connect_failure()
                raise
            except Exception:
                await self._cleanup_after_connect_failure()
                raise ElevenLabsConnectionClosed(
                    "Unable to open ElevenLabs realtime connection"
                ) from None
            return self

    async def _open_websocket_and_wait_for_session_started(self) -> None:
        """Resolve credentials and finish the handshake inside one deadline."""

        api_key = await self._resolve_api_key()
        if self._session is None:
            self._session = self._session_factory()
        try:
            self._websocket = await self._session.ws_connect(
                self.websocket_url,
                headers={
                    "xi-api-key": api_key,
                    "User-Agent": "Aurvek-Telephony/1.0",
                },
                autoping=True,
                heartbeat=20.0,
                max_msg_size=1024 * 1024,
            )
            await self._wait_for_session_started(self._websocket)
        finally:
            # Do not retain a provider-resolved secret beyond the handshake.
            api_key = ""

    async def _wait_for_session_started(self, websocket: Any) -> None:
        """Validate and retain Scribe's required application handshake."""

        while True:
            try:
                message = await websocket.receive()
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, RuntimeError):
                raise ElevenLabsConnectionClosed(
                    "ElevenLabs realtime WebSocket failed during handshake"
                ) from None
            if message.type in {aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY}:
                parsed = parse_elevenlabs_message(message.data)
                if not isinstance(parsed, ElevenLabsMetadataEvent):
                    raise ElevenLabsProtocolError(
                        "ElevenLabs sent data before session_started"
                    )
                self._session_started = True
                self._pending_session_started = parsed
                return
            if message.type == aiohttp.WSMsgType.ERROR:
                raise ElevenLabsConnectionClosed(
                    "ElevenLabs realtime WebSocket failed during handshake"
                ) from None
            if message.type in {
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
            }:
                raise ElevenLabsConnectionClosed(
                    "ElevenLabs realtime WebSocket closed during handshake"
                ) from None
            # Autoping normally consumes ping/pong.  Injectable transports may
            # expose them, so ignore only those control frames while waiting.
            if message.type not in {
                aiohttp.WSMsgType.PING,
                aiohttp.WSMsgType.PONG,
            }:
                raise ElevenLabsProtocolError(
                    "ElevenLabs sent an unsupported WebSocket frame"
                )

    async def send_audio(self, audio: bytes | bytearray | memoryview) -> None:
        """Buffer raw mu-law and send complete 100 ms chunks unchanged."""

        if not isinstance(audio, (bytes, bytearray, memoryview)):
            raise ValueError("audio must be bytes-like")
        raw = bytes(audio)
        if not raw:
            raise ValueError("audio cannot be empty")

        async with self._send_lock:
            websocket = self._require_websocket()
            self._audio_buffer.extend(raw)
            while len(self._audio_buffer) >= ELEVENLABS_AUDIO_CHUNK_BYTES:
                chunk = bytes(self._audio_buffer[:ELEVENLABS_AUDIO_CHUNK_BYTES])
                await websocket.send_json(_audio_message(chunk, commit=False))
                del self._audio_buffer[:ELEVENLABS_AUDIO_CHUNK_BYTES]

    async def finalize(self) -> None:
        """Flush buffered audio and explicitly commit the current segment."""

        async with self._send_lock:
            websocket = self._require_websocket()
            remainder = bytes(self._audio_buffer)
            await websocket.send_json(_audio_message(remainder, commit=True))
            self._audio_buffer.clear()
            self._finalize_requested = True

    async def events(self) -> AsyncIterator[ElevenLabsRealtimeEvent]:
        """Yield typed events until locally closed or a transport error occurs."""

        websocket = self._require_websocket()
        pending_session_started = self._pending_session_started
        if pending_session_started is not None:
            self._pending_session_started = None
            yield pending_session_started
        while True:
            try:
                message = await websocket.receive()
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, RuntimeError):
                raise ElevenLabsConnectionClosed(
                    "ElevenLabs realtime WebSocket failed"
                ) from None
            if message.type in {aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY}:
                parsed = parse_elevenlabs_message(message.data)
                if isinstance(parsed, ElevenLabsMetadataEvent):
                    if self._session_started:
                        raise ElevenLabsProtocolError(
                            "ElevenLabs started the realtime session twice"
                        )
                    self._session_started = True
                    yield parsed
                    continue
                if not self._session_started:
                    raise ElevenLabsProtocolError(
                        "ElevenLabs sent data before session_started"
                    )
                if isinstance(parsed, ElevenLabsTranscriptEvent):
                    if parsed.is_final and self._finalize_requested:
                        parsed = replace(parsed, from_finalize=True)
                        self._finalize_requested = False
                    if parsed.text.strip() and not self._segment_started:
                        self._segment_started = True
                        yield ElevenLabsSpeechStartedEvent()
                    yield parsed
                    if parsed.is_final:
                        yield ElevenLabsUtteranceEndEvent()
                        self._segment_started = False
                    continue
                yield parsed
                continue

            if message.type == aiohttp.WSMsgType.ERROR:
                raise ElevenLabsConnectionClosed(
                    "ElevenLabs realtime WebSocket failed"
                ) from None
            if message.type in {
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
            }:
                if self._closed:
                    return
                # A provider-initiated close is a failed STT stream even when
                # its WebSocket code is nominal; the phone session must not
                # continue without transcription.
                raise ElevenLabsConnectionClosed(
                    "ElevenLabs realtime WebSocket closed unexpectedly"
                ) from None
            # aiohttp normally consumes ping/pong via autoping.  Ignore those
            # frames if an injectable test/session implementation exposes them.

    async def close(self) -> None:
        """Close transport resources within a bounded, idempotent operation."""

        # Serializing against connect prevents a concurrent close from leaving
        # a newly-opened WebSocket behind after the client has become unusable.
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
                    self._session_started = False
                    self._pending_session_started = None
                    self._audio_buffer.clear()

    async def __aenter__(self) -> "ElevenLabsRealtimeClient":
        return await self.connect()

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    def _require_websocket(self) -> Any:
        if not self.connected:
            raise ElevenLabsConnectionClosed(
                "ElevenLabs realtime connection is not open"
            )
        return self._websocket

    async def _resolve_api_key(self) -> str:
        if self._api_key is not None:
            return self._api_key
        provider = self._api_key_provider
        if provider is None:  # Defensive: constructor enforces this invariant.
            raise ElevenLabsCredentialsError(
                "ElevenLabs API key is unavailable"
            )
        try:
            if inspect.iscoroutinefunction(provider):
                resolved = provider()
            else:
                resolved = await _await_sync_api_key_probe(
                    _sync_api_key_probe(provider)
                )
            if inspect.isawaitable(resolved):
                resolved = await resolved
            return _validate_api_key(resolved)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ElevenLabsCredentialsError(
                "Unable to resolve ElevenLabs API key"
            ) from None

    async def _cleanup_after_connect_failure(self) -> None:
        websocket = self._websocket
        self._websocket = None
        self._session_started = False
        self._pending_session_started = None
        if websocket is not None and not getattr(websocket, "closed", False):
            await self._bounded_close(websocket.close)
        if self._owns_session and self._session is not None:
            session = self._session
            self._session = None
            await self._bounded_close(session.close)

    async def _bounded_close(self, close_callback: Callable[[], Awaitable[Any]]) -> None:
        try:
            await asyncio.wait_for(
                close_callback(), timeout=self._close_timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, aiohttp.ClientError, RuntimeError):
            # Close is best-effort; the client has already been made unusable.
            pass


def _audio_message(audio: bytes, *, commit: bool) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_type": "input_audio_chunk",
        "audio_base_64": base64.b64encode(audio).decode("ascii"),
    }
    if commit:
        message["commit"] = True
    return message


def _validate_api_key(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\r" in value
        or "\n" in value
    ):
        raise ValueError("ElevenLabs API key is required")
    return value.strip()


def _decode_message(
    payload: str | bytes | bytearray | Mapping[str, Any],
) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return payload
    try:
        decoded = json.loads(payload)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ElevenLabsProtocolError("ElevenLabs sent invalid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise ElevenLabsProtocolError("ElevenLabs message must be a JSON object")
    return decoded


def _parse_session_started(message: Mapping[str, Any]) -> ElevenLabsMetadataEvent:
    session_id = _required_text(message, "session_id").strip()
    if not session_id:
        raise ElevenLabsProtocolError("ElevenLabs session_id cannot be empty")
    config = message.get("config")
    if not isinstance(config, Mapping):
        raise ElevenLabsProtocolError("ElevenLabs session has no config")
    model_id = _required_text(config, "model_id")
    audio_format = _required_text(config, "audio_format")
    sample_rate = config.get("sample_rate")
    if (
        not isinstance(sample_rate, int)
        or isinstance(sample_rate, bool)
        or sample_rate != ELEVENLABS_SAMPLE_RATE_HZ
    ):
        raise ElevenLabsProtocolError(
            "ElevenLabs negotiated an unsupported sample rate"
        )
    if model_id != ELEVENLABS_REALTIME_MODEL:
        raise ElevenLabsProtocolError("ElevenLabs negotiated an unsupported model")
    if audio_format != ELEVENLABS_AUDIO_FORMAT:
        raise ElevenLabsProtocolError(
            "ElevenLabs negotiated an unsupported audio format"
        )
    language_code = config.get("language_code")
    if language_code is not None and not isinstance(language_code, str):
        raise ElevenLabsProtocolError("ElevenLabs language code is invalid")
    return ElevenLabsMetadataEvent(
        session_id=session_id,
        model_id=model_id,
        audio_format=audio_format,
        sample_rate_hz=sample_rate,
        language_code=language_code,
    )


def _required_text(message: Mapping[str, Any], key: str) -> str:
    value = message.get(key)
    if not isinstance(value, str):
        raise ElevenLabsProtocolError(f"ElevenLabs {key} is not text")
    return value


__all__ = [
    "ApiKeyProvider",
    "ELEVENLABS_AUDIO_CHUNK_BYTES",
    "ELEVENLABS_AUDIO_FORMAT",
    "ELEVENLABS_COMMIT_STRATEGY",
    "ELEVENLABS_CONNECT_TIMEOUT_SECONDS",
    "ELEVENLABS_REALTIME_MODEL",
    "ELEVENLABS_REALTIME_URL",
    "ELEVENLABS_SAMPLE_RATE_HZ",
    "ElevenLabsConnectionClosed",
    "ElevenLabsCredentialsError",
    "ElevenLabsMetadataEvent",
    "ElevenLabsProtocolError",
    "ElevenLabsProviderError",
    "ElevenLabsRealtimeClient",
    "ElevenLabsRealtimeError",
    "ElevenLabsRealtimeEvent",
    "ElevenLabsRealtimeOptions",
    "ElevenLabsSpeechStartedEvent",
    "ElevenLabsTranscriptEvent",
    "ElevenLabsUtteranceEndEvent",
    "ElevenLabsWarningEvent",
    "parse_elevenlabs_message",
]
