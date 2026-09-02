"""Small injectable Deepgram Nova-3 live transcription adapter.

The adapter accepts the same headerless 8 kHz mono mu-law bytes received from
Twilio Media Streams. It owns no conversational state and persists nothing;
the phone media session decides how final segments form a canonical turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
import json
import re
from typing import Any, TypeAlias
from urllib.parse import urlencode

import aiohttp


DEEPGRAM_LIVE_URL = "wss://api.deepgram.com/v1/listen"
DEEPGRAM_MODEL = "nova-3"
DEEPGRAM_ENCODING = "mulaw"
DEEPGRAM_SAMPLE_RATE_HZ = 8_000
DEEPGRAM_CHANNELS = 1

_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_NORMAL_WEBSOCKET_CLOSE_CODES = frozenset(
    {
        int(aiohttp.WSCloseCode.OK),
        int(aiohttp.WSCloseCode.GOING_AWAY),
    }
)


class DeepgramLiveError(RuntimeError):
    """Base error for the live STT transport."""


class DeepgramProtocolError(DeepgramLiveError):
    """Raised for an invalid or unusable provider message."""


class DeepgramProviderError(DeepgramLiveError):
    """Raised when Deepgram reports an application-level error."""

    def __init__(self, *, code: str | None, description: str | None) -> None:
        self.code = code
        self.description = description
        message = "Deepgram live transcription failed"
        if code:
            message += f" ({code})"
        super().__init__(message)


class DeepgramConnectionClosed(DeepgramLiveError):
    """Raised when an operation requires an open live connection."""


@dataclass(frozen=True, slots=True)
class DeepgramLiveOptions:
    """Validated live STT options fixed to Aurvek's product contract."""

    language: str = "multi"
    endpointing_ms: int = 300

    def __post_init__(self) -> None:
        if not isinstance(self.language, str):
            raise ValueError("language must be 'multi' or a fixed locale")
        language = self.language.strip()
        if language != "multi" and not _LANGUAGE_PATTERN.fullmatch(language):
            raise ValueError("language must be 'multi' or a fixed locale")
        if not 10 <= self.endpointing_ms <= 5_000:
            raise ValueError("endpointing_ms must be between 10 and 5000")
        object.__setattr__(self, "language", language)

    def query_items(self) -> tuple[tuple[str, str], ...]:
        return (
            ("model", DEEPGRAM_MODEL),
            ("encoding", DEEPGRAM_ENCODING),
            ("sample_rate", str(DEEPGRAM_SAMPLE_RATE_HZ)),
            ("channels", str(DEEPGRAM_CHANNELS)),
            ("language", self.language),
            ("interim_results", "true"),
            ("vad_events", "true"),
            ("endpointing", str(self.endpointing_ms)),
            ("punctuate", "true"),
        )

    def websocket_url(self) -> str:
        return f"{DEEPGRAM_LIVE_URL}?{urlencode(self.query_items())}"


@dataclass(frozen=True, slots=True)
class DeepgramWord:
    text: str
    start_seconds: float | None
    end_seconds: float | None
    confidence: float | None


@dataclass(frozen=True, slots=True)
class DeepgramTranscriptEvent:
    """An interim or final transcript segment from a ``Results`` message."""

    text: str
    is_final: bool
    speech_final: bool
    from_finalize: bool
    start_seconds: float | None
    duration_seconds: float | None
    confidence: float | None
    words: tuple[DeepgramWord, ...]


@dataclass(frozen=True, slots=True)
class DeepgramSpeechStartedEvent:
    timestamp_seconds: float | None


@dataclass(frozen=True, slots=True)
class DeepgramUtteranceEndEvent:
    last_word_end_seconds: float | None


@dataclass(frozen=True, slots=True)
class DeepgramMetadataEvent:
    request_id: str | None
    duration_seconds: float | None


@dataclass(frozen=True, slots=True)
class DeepgramWarningEvent:
    code: str | None
    description: str | None


DeepgramLiveEvent: TypeAlias = (
    DeepgramTranscriptEvent
    | DeepgramSpeechStartedEvent
    | DeepgramUtteranceEndEvent
    | DeepgramMetadataEvent
    | DeepgramWarningEvent
)


def parse_deepgram_message(
    payload: str | bytes | bytearray | Mapping[str, Any],
) -> DeepgramLiveEvent | None:
    """Parse one Deepgram JSON message into a provider-neutral event shape."""

    message = _decode_message(payload)
    message_type = message.get("type")
    if not isinstance(message_type, str):
        raise DeepgramProtocolError("Deepgram message has no type")

    if message_type == "Results":
        return _parse_results(message)
    if message_type == "SpeechStarted":
        return DeepgramSpeechStartedEvent(
            timestamp_seconds=_optional_float(message.get("timestamp"))
        )
    if message_type == "UtteranceEnd":
        return DeepgramUtteranceEndEvent(
            last_word_end_seconds=_optional_float(message.get("last_word_end"))
        )
    if message_type == "Metadata":
        request_id = message.get("request_id")
        return DeepgramMetadataEvent(
            request_id=request_id if isinstance(request_id, str) else None,
            duration_seconds=_optional_float(message.get("duration")),
        )
    if message_type == "Warning":
        return DeepgramWarningEvent(
            code=_optional_string(message.get("warn_code") or message.get("code")),
            description=_optional_string(
                message.get("warn_msg") or message.get("description")
            ),
        )
    if message_type == "Error":
        raise DeepgramProviderError(
            code=_optional_string(message.get("code") or message.get("err_code")),
            description=_optional_string(
                message.get("description") or message.get("err_msg")
            ),
        )
    return None


class DeepgramLiveClient:
    """Direct aiohttp WebSocket client with an injectable HTTP session.

    The caller sends audio, consumes :meth:`events`, and owns any keep-alive
    scheduling. Supplying an existing session makes the caller responsible for
    closing it; sessions created by ``session_factory`` are closed here.
    """

    def __init__(
        self,
        api_key: str,
        *,
        options: DeepgramLiveOptions | None = None,
        session: Any | None = None,
        session_factory: Callable[[], Any] = aiohttp.ClientSession,
    ) -> None:
        if (
            not isinstance(api_key, str)
            or not api_key.strip()
            or "\r" in api_key
            or "\n" in api_key
        ):
            raise ValueError("Deepgram API key is required")
        self._api_key = api_key.strip()
        self.options = options or DeepgramLiveOptions()
        self._session = session
        self._session_factory = session_factory
        self._owns_session = session is None
        self._websocket: Any | None = None
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
        return self.options.websocket_url()

    async def connect(self) -> "DeepgramLiveClient":
        if self._closed:
            raise DeepgramConnectionClosed("Deepgram client is already closed")
        if self.connected:
            return self
        if self._session is None:
            self._session = self._session_factory()
        try:
            self._websocket = await self._session.ws_connect(
                self.websocket_url,
                headers={
                    "Authorization": f"Token {self._api_key}",
                    "User-Agent": "Aurvek-Telephony/1.0",
                },
                autoping=True,
                heartbeat=20.0,
                max_msg_size=1024 * 1024,
            )
        except asyncio.CancelledError:
            try:
                await self._close_owned_session_after_connect_failure()
            except Exception:
                # Connection cancellation remains authoritative; cleanup must
                # not turn it into an unrelated session-close failure.
                pass
            raise
        except Exception:
            await self._close_owned_session_after_connect_failure()
            raise
        return self

    async def send_audio(self, audio: bytes | bytearray | memoryview) -> None:
        websocket = self._require_websocket()
        if not isinstance(audio, (bytes, bytearray, memoryview)):
            raise ValueError("audio must be bytes-like")
        raw = bytes(audio)
        if not raw:
            raise ValueError("audio cannot be empty")
        await websocket.send_bytes(raw)

    async def finalize(self) -> None:
        """Ask Deepgram to flush the current utterance without disconnecting."""

        await self._require_websocket().send_json({"type": "Finalize"})

    async def keep_alive(self) -> None:
        await self._require_websocket().send_json({"type": "KeepAlive"})

    async def events(self) -> AsyncIterator[DeepgramLiveEvent]:
        """Yield parsed provider events until the WebSocket closes."""

        websocket = self._require_websocket()
        while True:
            message = await websocket.receive()
            if message.type in {aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY}:
                event = parse_deepgram_message(message.data)
                if event is not None:
                    yield event
                continue
            if message.type == aiohttp.WSMsgType.ERROR:
                raise DeepgramConnectionClosed(
                    "Deepgram WebSocket closed with an error"
                ) from None
            if message.type in {
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
            }:
                close_code = _websocket_close_code(message, websocket)
                close_reason = getattr(message, "extra", None)
                provider_error = websocket.exception()
                closed_after_own_close = (
                    message.type == aiohttp.WSMsgType.CLOSED
                    and self._closed
                    and close_code is None
                )
                if (
                    provider_error is None
                    and not close_reason
                    and (
                        close_code in _NORMAL_WEBSOCKET_CLOSE_CODES
                        or closed_after_own_close
                    )
                ):
                    return
                raise DeepgramConnectionClosed(
                    "Deepgram WebSocket closed unexpectedly"
                ) from None
            # aiohttp handles ping/pong automatically. Ignore those frames if
            # a custom session still exposes them to the consumer.

    async def close(self) -> None:
        """Close the Deepgram stream and any internally-created HTTP session."""

        if self._closed:
            return
        self._closed = True
        websocket = self._websocket
        self._websocket = None
        try:
            if websocket is not None and not getattr(websocket, "closed", False):
                try:
                    await websocket.send_json({"type": "CloseStream"})
                except asyncio.CancelledError:
                    raise
                except aiohttp.ClientError:
                    pass
                finally:
                    await websocket.close()
        finally:
            if self._owns_session and self._session is not None:
                await self._session.close()
            self._session = None

    async def __aenter__(self) -> "DeepgramLiveClient":
        return await self.connect()

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    def _require_websocket(self) -> Any:
        if not self.connected:
            raise DeepgramConnectionClosed("Deepgram live connection is not open")
        return self._websocket

    async def _close_owned_session_after_connect_failure(self) -> None:
        if not self._owns_session or self._session is None:
            return
        session = self._session
        self._session = None
        await session.close()


def _decode_message(
    payload: str | bytes | bytearray | Mapping[str, Any],
) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return payload
    try:
        decoded = json.loads(payload)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeepgramProtocolError("Deepgram sent invalid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise DeepgramProtocolError("Deepgram message must be a JSON object")
    return decoded


def _websocket_close_code(message: Any, websocket: Any) -> int | None:
    message_code = getattr(message, "data", None)
    if isinstance(message_code, int) and not isinstance(message_code, bool):
        return int(message_code)
    websocket_code = getattr(websocket, "close_code", None)
    if isinstance(websocket_code, int) and not isinstance(websocket_code, bool):
        return int(websocket_code)
    return None


def _parse_results(message: Mapping[str, Any]) -> DeepgramTranscriptEvent:
    channel = message.get("channel")
    if not isinstance(channel, Mapping):
        raise DeepgramProtocolError("Deepgram Results message has no channel")
    alternatives = channel.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        raise DeepgramProtocolError("Deepgram Results message has no alternatives")
    alternative = alternatives[0]
    if not isinstance(alternative, Mapping):
        raise DeepgramProtocolError("Deepgram transcript alternative is invalid")
    transcript = alternative.get("transcript")
    if not isinstance(transcript, str):
        raise DeepgramProtocolError("Deepgram transcript is not text")

    raw_words = alternative.get("words", [])
    if not isinstance(raw_words, list):
        raise DeepgramProtocolError("Deepgram transcript words are invalid")
    words: list[DeepgramWord] = []
    for raw_word in raw_words:
        if not isinstance(raw_word, Mapping):
            raise DeepgramProtocolError("Deepgram word is invalid")
        text = raw_word.get("punctuated_word") or raw_word.get("word")
        if not isinstance(text, str):
            raise DeepgramProtocolError("Deepgram word has no text")
        words.append(
            DeepgramWord(
                text=text,
                start_seconds=_optional_float(raw_word.get("start")),
                end_seconds=_optional_float(raw_word.get("end")),
                confidence=_optional_float(raw_word.get("confidence")),
            )
        )

    return DeepgramTranscriptEvent(
        text=transcript,
        is_final=_strict_bool(message.get("is_final"), default=False),
        speech_final=_strict_bool(message.get("speech_final"), default=False),
        from_finalize=_strict_bool(message.get("from_finalize"), default=False),
        start_seconds=_optional_float(message.get("start")),
        duration_seconds=_optional_float(message.get("duration")),
        confidence=_optional_float(alternative.get("confidence")),
        words=tuple(words),
    )


def _strict_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise DeepgramProtocolError("Deepgram boolean field is invalid")
    return value


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise DeepgramProtocolError("Deepgram numeric field is invalid")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise DeepgramProtocolError("Deepgram numeric field is invalid") from exc


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


__all__ = [
    "DEEPGRAM_CHANNELS",
    "DEEPGRAM_ENCODING",
    "DEEPGRAM_MODEL",
    "DEEPGRAM_SAMPLE_RATE_HZ",
    "DeepgramConnectionClosed",
    "DeepgramLiveClient",
    "DeepgramLiveError",
    "DeepgramLiveEvent",
    "DeepgramLiveOptions",
    "DeepgramMetadataEvent",
    "DeepgramProtocolError",
    "DeepgramProviderError",
    "DeepgramSpeechStartedEvent",
    "DeepgramTranscriptEvent",
    "DeepgramUtteranceEndEvent",
    "DeepgramWarningEvent",
    "DeepgramWord",
    "parse_deepgram_message",
]
