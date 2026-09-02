"""Pure protocol and playback primitives for Twilio Media Streams.

This module deliberately owns no WebSocket, database, TTS, STT, or LLM
integration.  It validates the untrusted Media Streams wire protocol, builds
the three server-to-Twilio messages Aurvek needs, and accounts for which
aligned audio/text fragments may safely be considered audible.
"""

from __future__ import annotations

import base64
import binascii
from collections import OrderedDict
from dataclasses import dataclass
import math
import re
from typing import Any, Literal, Mapping, TypeAlias

import orjson

from integrations.telephony.audio import (
    PCMU_CHANNELS,
    PCMU_SAMPLE_RATE_HZ,
    encode_twilio_media_payload,
    pcmu_duration_ms,
)


MEDIA_ENCODING = "audio/x-mulaw"
MAX_STREAM_ATTEMPT = 2
MAX_CORRELATION_TOKEN_CHARS = 400
MAX_WIRE_MESSAGE_BYTES = 262_144
MAX_MEDIA_PAYLOAD_BYTES = 4_096
MAX_MEDIA_PAYLOAD_ENCODED_CHARS = ((MAX_MEDIA_PAYLOAD_BYTES + 2) // 3) * 4
MAX_MARK_NAME_CHARS = 128
MAX_SEQUENCE_NUMBER = 2_147_483_647
MAX_MEDIA_TIMESTAMP_MS = 86_400_000
DEFAULT_MAX_BUFFERED_BYTES = 256_000
DEFAULT_MAX_BUFFERED_FRAGMENTS = 2_048
HARD_MAX_BUFFERED_BYTES = 1_048_576
HARD_MAX_BUFFERED_FRAGMENTS = 8_192

_SID_PATTERNS = {
    "account_sid": re.compile(r"^AC[0-9A-Fa-f]{32}$"),
    "call_sid": re.compile(r"^CA[0-9A-Fa-f]{32}$"),
    "stream_sid": re.compile(r"^MZ[0-9A-Fa-f]{32}$"),
}


class MediaStreamError(ValueError):
    """Base error for a malformed or unsafe Media Streams operation."""


class MediaStreamProtocolError(MediaStreamError):
    """An inbound WebSocket message violated the expected protocol."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MediaStreamOverflowError(MediaStreamError):
    """The bounded outbound playback ledger cannot accept another fragment."""

    def __init__(self, state: "BackpressureState") -> None:
        super().__init__(
            "Media Streams playback buffer limit exceeded "
            f"({state.pending_bytes}/{state.max_bytes} bytes, "
            f"{state.pending_fragments}/{state.max_fragments} fragments)"
        )
        self.state = state


@dataclass(frozen=True, slots=True)
class ConnectedEvent:
    protocol: str
    version: str


@dataclass(frozen=True, slots=True)
class StartEvent:
    sequence_number: int
    account_sid: str
    call_sid: str
    stream_sid: str
    correlation_token: str
    stream_attempt: int
    encoding: str = MEDIA_ENCODING
    sample_rate: int = PCMU_SAMPLE_RATE_HZ
    channels: int = PCMU_CHANNELS


@dataclass(frozen=True, slots=True)
class MediaEvent:
    sequence_number: int
    stream_sid: str
    chunk: int
    timestamp_ms: int
    payload: bytes
    track: str = "inbound"


@dataclass(frozen=True, slots=True)
class MarkEvent:
    sequence_number: int
    stream_sid: str
    name: str


@dataclass(frozen=True, slots=True)
class StopEvent:
    sequence_number: int
    account_sid: str
    call_sid: str
    stream_sid: str
    reason: Literal["twilio_stop"]
    media_chunks: int
    media_bytes: int
    last_media_timestamp_ms: int | None


InboundEvent: TypeAlias = (
    ConnectedEvent | StartEvent | MediaEvent | MarkEvent | StopEvent
)


def _protocol_error(code: str, message: str) -> MediaStreamProtocolError:
    return MediaStreamProtocolError(code, message)


def _as_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _protocol_error("invalid_shape", f"{field} must be an object")
    return value


def _require_sid(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _SID_PATTERNS[field].fullmatch(value):
        raise _protocol_error("invalid_sid", f"{field} is invalid")
    return value


def _require_opaque(
    value: Any,
    *,
    field: str,
    max_chars: int,
) -> str:
    if not isinstance(value, str):
        raise _protocol_error("invalid_parameter", f"{field} must be a string")
    if (
        not value
        or value != value.strip()
        or len(value) > max_chars
        or any(ord(character) < 33 for character in value)
    ):
        raise _protocol_error("invalid_parameter", f"{field} is invalid")
    return value


def _parse_uint(
    value: Any,
    *,
    field: str,
    maximum: int,
    allow_zero: bool,
) -> int:
    if isinstance(value, bool):
        raise _protocol_error("invalid_number", f"{field} is invalid")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
    else:
        raise _protocol_error("invalid_number", f"{field} is invalid")
    minimum = 0 if allow_zero else 1
    if not minimum <= parsed <= maximum:
        raise _protocol_error("invalid_number", f"{field} is outside its bounds")
    return parsed


def _decode_media_payload(value: Any) -> bytes:
    if not isinstance(value, str) or not value:
        raise _protocol_error("invalid_audio", "media.payload must be non-empty")
    if len(value) > MAX_MEDIA_PAYLOAD_ENCODED_CHARS:
        raise _protocol_error("audio_overflow", "media.payload exceeds its limit")
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise _protocol_error("invalid_audio", "media.payload is invalid base64") from exc
    if not decoded:
        raise _protocol_error("invalid_audio", "media.payload decoded to no audio")
    if len(decoded) > MAX_MEDIA_PAYLOAD_BYTES:
        raise _protocol_error("audio_overflow", "decoded media exceeds its limit")
    return decoded


class MediaStreamParser:
    """Stateful, fail-closed parser for one Twilio WebSocket stream."""

    def __init__(
        self,
        *,
        expected_account_sid: str,
        expected_call_sid: str,
        expected_correlation_token: str,
        expected_stream_attempt: int,
    ) -> None:
        try:
            self.expected_account_sid = _require_sid(
                expected_account_sid,
                field="account_sid",
            )
            self.expected_call_sid = _require_sid(
                expected_call_sid,
                field="call_sid",
            )
            self.expected_correlation_token = _require_opaque(
                expected_correlation_token,
                field="correlation_token",
                max_chars=MAX_CORRELATION_TOKEN_CHARS,
            )
        except MediaStreamProtocolError as exc:
            raise ValueError("expected Media Streams correlation is invalid") from exc
        if (
            isinstance(expected_stream_attempt, bool)
            or not isinstance(expected_stream_attempt, int)
            or not 0 <= expected_stream_attempt <= MAX_STREAM_ATTEMPT
        ):
            raise ValueError("expected_stream_attempt is invalid")
        self.expected_stream_attempt = expected_stream_attempt
        self._connected = False
        self._start: StartEvent | None = None
        self._stopped = False
        self._failed = False
        self._last_sequence: int | None = None
        self._last_media_chunk: int | None = None
        self._last_media_timestamp: int | None = None
        self._media_chunks = 0
        self._media_bytes = 0

    @property
    def start(self) -> StartEvent | None:
        return self._start

    @property
    def stopped(self) -> bool:
        return self._stopped

    def parse(self, message: str | bytes | bytearray | Mapping[str, Any]) -> InboundEvent:
        if self._failed:
            raise _protocol_error(
                "stream_failed",
                "stream was closed after a previous protocol violation",
            )
        try:
            payload = self._decode_message(message)
            event_name = payload.get("event")
            if event_name == "connected":
                return self._parse_connected(payload)
            if event_name == "start":
                return self._parse_start(payload)
            if event_name == "media":
                return self._parse_media(payload)
            if event_name == "mark":
                return self._parse_mark(payload)
            if event_name == "stop":
                return self._parse_stop(payload)
            raise _protocol_error("unsupported_event", "Media Streams event is unsupported")
        except MediaStreamProtocolError:
            # A protocol violation poisons this WebSocket. Continuing after a
            # correlation or ordering failure could splice streams together.
            self._failed = True
            raise

    @staticmethod
    def _decode_message(
        message: str | bytes | bytearray | Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if isinstance(message, Mapping):
            return message
        if isinstance(message, str):
            try:
                raw = message.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise _protocol_error("invalid_json", "message is not UTF-8") from exc
        elif isinstance(message, (bytes, bytearray)):
            raw = bytes(message)
        else:
            raise _protocol_error("invalid_shape", "message must be JSON or an object")
        if not raw or len(raw) > MAX_WIRE_MESSAGE_BYTES:
            raise _protocol_error("message_overflow", "message size is invalid")
        try:
            decoded = orjson.loads(raw)
        except orjson.JSONDecodeError as exc:
            raise _protocol_error("invalid_json", "message is not valid JSON") from exc
        return _as_mapping(decoded, field="message")

    def _ensure_open(self, *, require_start: bool) -> None:
        if self._stopped:
            raise _protocol_error("stream_closed", "stream has already stopped")
        if not self._connected:
            raise _protocol_error("event_order", "connected event is required first")
        if require_start and self._start is None:
            raise _protocol_error("event_order", "start event is required first")

    def _parse_connected(self, payload: Mapping[str, Any]) -> ConnectedEvent:
        if self._connected or self._start is not None or self._stopped:
            raise _protocol_error("event_order", "connected event is out of order")
        if payload.get("protocol") != "Call" or payload.get("version") != "1.0.0":
            raise _protocol_error("protocol_mismatch", "unsupported Media Streams protocol")
        event = ConnectedEvent(protocol="Call", version="1.0.0")
        self._connected = True
        return event

    def _parse_start(self, payload: Mapping[str, Any]) -> StartEvent:
        self._ensure_open(require_start=False)
        if self._start is not None:
            raise _protocol_error("event_order", "start event was already received")
        sequence = self._validated_sequence(payload)
        start = _as_mapping(payload.get("start"), field="start")
        account_sid = _require_sid(start.get("accountSid"), field="account_sid")
        if account_sid != self.expected_account_sid:
            raise _protocol_error("account_mismatch", "start account does not match")
        call_sid = _require_sid(start.get("callSid"), field="call_sid")
        if call_sid != self.expected_call_sid:
            raise _protocol_error("call_mismatch", "start call does not match")
        nested_stream_sid = _require_sid(start.get("streamSid"), field="stream_sid")
        stream_sid = _require_sid(payload.get("streamSid"), field="stream_sid")
        if stream_sid != nested_stream_sid:
            raise _protocol_error("stream_mismatch", "start stream identifiers differ")

        tracks = start.get("tracks")
        if tracks != ["inbound"]:
            raise _protocol_error("track_mismatch", "start tracks must be inbound only")
        media_format = _as_mapping(start.get("mediaFormat"), field="start.mediaFormat")
        if (
            media_format.get("encoding") != MEDIA_ENCODING
            or media_format.get("sampleRate") != PCMU_SAMPLE_RATE_HZ
            or isinstance(media_format.get("sampleRate"), bool)
            or media_format.get("channels") != PCMU_CHANNELS
            or isinstance(media_format.get("channels"), bool)
        ):
            raise _protocol_error("media_format_mismatch", "unsupported media format")

        custom = _as_mapping(
            start.get("customParameters"),
            field="start.customParameters",
        )
        if set(custom) != {"correlation_token", "stream_attempt"}:
            raise _protocol_error(
                "parameter_mismatch",
                "start custom parameters do not match the contract",
            )
        correlation_token = _require_opaque(
            custom.get("correlation_token"),
            field="correlation_token",
            max_chars=MAX_CORRELATION_TOKEN_CHARS,
        )
        attempt_raw = custom.get("stream_attempt")
        if not isinstance(attempt_raw, str) or attempt_raw not in {"0", "1", "2"}:
            raise _protocol_error("invalid_parameter", "stream_attempt is invalid")
        stream_attempt = int(attempt_raw)
        if correlation_token != self.expected_correlation_token:
            raise _protocol_error(
                "correlation_mismatch",
                "start correlation token does not match",
            )
        if stream_attempt != self.expected_stream_attempt:
            raise _protocol_error(
                "attempt_mismatch",
                "start stream attempt does not match",
            )

        event = StartEvent(
            sequence_number=sequence,
            account_sid=account_sid,
            call_sid=call_sid,
            stream_sid=stream_sid,
            correlation_token=correlation_token,
            stream_attempt=stream_attempt,
        )
        self._last_sequence = sequence
        self._start = event
        return event

    def _parse_media(self, payload: Mapping[str, Any]) -> MediaEvent:
        self._ensure_open(require_start=True)
        assert self._start is not None
        sequence = self._validated_sequence(payload)
        stream_sid = self._validated_stream_sid(payload)
        media = _as_mapping(payload.get("media"), field="media")
        if media.get("track") != "inbound":
            raise _protocol_error("track_mismatch", "media track must be inbound")
        chunk = _parse_uint(
            media.get("chunk"),
            field="media.chunk",
            maximum=MAX_SEQUENCE_NUMBER,
            allow_zero=False,
        )
        timestamp = _parse_uint(
            media.get("timestamp"),
            field="media.timestamp",
            maximum=MAX_MEDIA_TIMESTAMP_MS,
            allow_zero=True,
        )
        if self._last_media_chunk is not None and chunk != self._last_media_chunk + 1:
            raise _protocol_error("media_order", "media chunk is not contiguous")
        if (
            self._last_media_timestamp is not None
            and timestamp <= self._last_media_timestamp
        ):
            raise _protocol_error("media_order", "media timestamp is not monotonic")
        audio = _decode_media_payload(media.get("payload"))

        event = MediaEvent(
            sequence_number=sequence,
            stream_sid=stream_sid,
            chunk=chunk,
            timestamp_ms=timestamp,
            payload=audio,
        )
        self._last_sequence = sequence
        self._last_media_chunk = chunk
        self._last_media_timestamp = timestamp
        self._media_chunks += 1
        self._media_bytes += len(audio)
        return event

    def _parse_mark(self, payload: Mapping[str, Any]) -> MarkEvent:
        self._ensure_open(require_start=True)
        sequence = self._validated_sequence(payload)
        stream_sid = self._validated_stream_sid(payload)
        mark = _as_mapping(payload.get("mark"), field="mark")
        name = _require_opaque(
            mark.get("name"),
            field="mark.name",
            max_chars=MAX_MARK_NAME_CHARS,
        )
        event = MarkEvent(
            sequence_number=sequence,
            stream_sid=stream_sid,
            name=name,
        )
        self._last_sequence = sequence
        return event

    def _parse_stop(self, payload: Mapping[str, Any]) -> StopEvent:
        self._ensure_open(require_start=True)
        assert self._start is not None
        sequence = self._validated_sequence(payload)
        stream_sid = self._validated_stream_sid(payload)
        stop = _as_mapping(payload.get("stop"), field="stop")
        account_sid = _require_sid(stop.get("accountSid"), field="account_sid")
        call_sid = _require_sid(stop.get("callSid"), field="call_sid")
        if account_sid != self._start.account_sid or call_sid != self._start.call_sid:
            raise _protocol_error("stop_mismatch", "stop identifiers do not match start")
        event = StopEvent(
            sequence_number=sequence,
            account_sid=account_sid,
            call_sid=call_sid,
            stream_sid=stream_sid,
            reason="twilio_stop",
            media_chunks=self._media_chunks,
            media_bytes=self._media_bytes,
            last_media_timestamp_ms=self._last_media_timestamp,
        )
        self._last_sequence = sequence
        self._stopped = True
        return event

    def _validated_sequence(self, payload: Mapping[str, Any]) -> int:
        sequence = _parse_uint(
            payload.get("sequenceNumber"),
            field="sequenceNumber",
            maximum=MAX_SEQUENCE_NUMBER,
            allow_zero=False,
        )
        if self._last_sequence is not None and sequence != self._last_sequence + 1:
            raise _protocol_error("sequence_order", "sequenceNumber is not contiguous")
        return sequence

    def _validated_stream_sid(self, payload: Mapping[str, Any]) -> str:
        assert self._start is not None
        stream_sid = _require_sid(payload.get("streamSid"), field="stream_sid")
        if stream_sid != self._start.stream_sid:
            raise _protocol_error("stream_mismatch", "event belongs to another stream")
        return stream_sid


def build_media_message(
    *,
    stream_sid: str,
    audio: bytes | bytearray | memoryview,
) -> dict[str, Any]:
    """Build one bounded server-to-Twilio raw PCMU media message."""

    sid = _require_sid(stream_sid, field="stream_sid")
    if not isinstance(audio, (bytes, bytearray, memoryview)):
        raise MediaStreamError("audio must be bytes-like")
    raw = bytes(audio)
    if not raw:
        raise MediaStreamError("audio cannot be empty")
    if len(raw) > MAX_MEDIA_PAYLOAD_BYTES:
        raise MediaStreamError("audio exceeds the outbound payload limit")
    return {
        "event": "media",
        "streamSid": sid,
        "media": {"payload": encode_twilio_media_payload(raw)},
    }


def build_mark_message(*, stream_sid: str, name: str) -> dict[str, Any]:
    """Build a mark whose name can be bound to a playback frontier."""

    sid = _require_sid(stream_sid, field="stream_sid")
    mark_name = _require_opaque(
        name,
        field="mark.name",
        max_chars=MAX_MARK_NAME_CHARS,
    )
    return {"event": "mark", "streamSid": sid, "mark": {"name": mark_name}}


def build_clear_message(*, stream_sid: str) -> dict[str, Any]:
    """Build the Twilio command that drops all not-yet-played outbound audio."""

    sid = _require_sid(stream_sid, field="stream_sid")
    return {"event": "clear", "streamSid": sid}


@dataclass(frozen=True, slots=True)
class PlaybackFragment:
    """One indivisible text/audio alignment unit in the playback timeline.

    Callers must append fragments on safe text boundaries.  The ledger never
    returns part of a fragment, which makes interruption persistence
    conservative and prevents cutting a word merely because its audio began.
    """

    index: int
    text: str
    byte_length: int
    start_byte: int
    end_byte: int
    start_ms: float
    end_ms: float

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms


@dataclass(frozen=True, slots=True)
class MarkBoundary:
    name: str
    fragment_count: int
    byte_frontier: int
    timeline_ms: float


@dataclass(frozen=True, slots=True)
class MarkConfirmation:
    name: str
    text_prefix: str
    played_ms: int
    advanced: bool
    drained_after_clear: bool


@dataclass(frozen=True, slots=True)
class BargeInResult:
    text_prefix: str
    played_ms: int
    clear_message: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BackpressureState:
    pending_bytes: int
    max_bytes: int
    pending_fragments: int
    max_fragments: int
    overflowed: bool


@dataclass(slots=True)
class ConservativePlaybackClock:
    """Estimate Twilio playback from local sends with an explicit safety lag.

    Twilio does not report an outbound playhead during barge-in. Marks are
    definitive once returned; between marks Aurvek only credits audio that
    could have played since the first sent byte, less a safety margin.
    """

    safety_lag_ms: int = 80
    first_audio_sent_at: float | None = None
    audio_bytes_sent: int = 0
    confirmed_ms: int = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.safety_lag_ms, bool)
            or not isinstance(self.safety_lag_ms, int)
            or not 0 <= self.safety_lag_ms <= 2_000
        ):
            raise ValueError("safety_lag_ms must be between 0 and 2000")

    def note_audio_sent(
        self,
        audio: bytes | bytearray | memoryview,
        *,
        sent_at: float,
    ) -> None:
        if not isinstance(audio, (bytes, bytearray, memoryview)) or not audio:
            raise MediaStreamError("sent audio must be non-empty bytes")
        if isinstance(sent_at, bool) or not isinstance(sent_at, (int, float)):
            raise MediaStreamError("sent_at must be monotonic seconds")
        timestamp = float(sent_at)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise MediaStreamError("sent_at must be finite and non-negative")
        if self.first_audio_sent_at is None:
            self.first_audio_sent_at = timestamp
        elif timestamp < self.first_audio_sent_at:
            raise MediaStreamError("sent_at cannot precede first audio")
        self.audio_bytes_sent += len(audio)

    def note_mark_confirmed(self, played_ms: int) -> None:
        if (
            isinstance(played_ms, bool)
            or not isinstance(played_ms, int)
            or played_ms < 0
        ):
            raise MediaStreamError("confirmed played_ms must be non-negative")
        self.confirmed_ms = max(self.confirmed_ms, played_ms)

    def estimate(self, *, observed_at: float, maximum_ms: float) -> int:
        if isinstance(observed_at, bool) or not isinstance(observed_at, (int, float)):
            raise MediaStreamError("observed_at must be monotonic seconds")
        observed = float(observed_at)
        if not math.isfinite(observed) or observed < 0:
            raise MediaStreamError("observed_at must be finite and non-negative")
        maximum = max(
            0.0,
            min(float(maximum_ms), pcmu_duration_ms(self.audio_bytes_sent)),
        )
        if self.first_audio_sent_at is None:
            return min(self.confirmed_ms, int(math.floor(maximum)))
        elapsed_ms = max(
            0.0,
            (observed - self.first_audio_sent_at) * 1_000,
        )
        conservative = max(
            float(self.confirmed_ms),
            elapsed_ms - self.safety_lag_ms,
        )
        return int(math.floor(min(conservative, maximum)))


class PlaybackLedger:
    """Bounded ledger of aligned outbound audio and Twilio mark frontiers."""

    def __init__(
        self,
        *,
        max_buffered_bytes: int = DEFAULT_MAX_BUFFERED_BYTES,
        max_buffered_fragments: int = DEFAULT_MAX_BUFFERED_FRAGMENTS,
    ) -> None:
        if (
            isinstance(max_buffered_bytes, bool)
            or not isinstance(max_buffered_bytes, int)
            or not 1 <= max_buffered_bytes <= HARD_MAX_BUFFERED_BYTES
        ):
            raise ValueError("max_buffered_bytes is outside its hard bounds")
        if (
            isinstance(max_buffered_fragments, bool)
            or not isinstance(max_buffered_fragments, int)
            or not 1 <= max_buffered_fragments <= HARD_MAX_BUFFERED_FRAGMENTS
        ):
            raise ValueError("max_buffered_fragments is outside its hard bounds")
        self.max_buffered_bytes = max_buffered_bytes
        self.max_buffered_fragments = max_buffered_fragments
        self._fragments: list[PlaybackFragment] = []
        self._marks: OrderedDict[str, MarkBoundary] = OrderedDict()
        self._confirmed_count = 0
        self._cleared_result: BargeInResult | None = None
        self._overflowed = False

    @property
    def fragments(self) -> tuple[PlaybackFragment, ...]:
        return tuple(self._fragments)

    @property
    def duration_ms(self) -> float:
        if not self._fragments:
            return 0.0
        return self._fragments[-1].end_ms

    @property
    def backpressure(self) -> BackpressureState:
        confirmed_bytes = self._byte_frontier(self._confirmed_count)
        total_bytes = self._byte_frontier(len(self._fragments))
        return BackpressureState(
            pending_bytes=total_bytes - confirmed_bytes,
            max_bytes=self.max_buffered_bytes,
            pending_fragments=len(self._fragments) - self._confirmed_count,
            max_fragments=self.max_buffered_fragments,
            overflowed=self._overflowed,
        )

    def append_fragment(
        self,
        *,
        text: str,
        audio: bytes | bytearray | memoryview,
    ) -> PlaybackFragment:
        """Append one atomic alignment unit without exceeding queue bounds."""

        if self._cleared_result is not None:
            raise MediaStreamError("cannot append audio after playback was cleared")
        if not isinstance(text, str):
            raise MediaStreamError("fragment text must be a string")
        if any(character in text for character in ("\x00", "\r")):
            raise MediaStreamError("fragment text contains unsupported controls")
        if not isinstance(audio, (bytes, bytearray, memoryview)):
            raise MediaStreamError("fragment audio must be bytes-like")
        raw_length = len(audio)
        if raw_length <= 0:
            raise MediaStreamError("fragment audio cannot be empty")

        state = self.backpressure
        attempted = BackpressureState(
            pending_bytes=state.pending_bytes + raw_length,
            max_bytes=state.max_bytes,
            pending_fragments=state.pending_fragments + 1,
            max_fragments=state.max_fragments,
            overflowed=True,
        )
        if (
            attempted.pending_bytes > attempted.max_bytes
            or attempted.pending_fragments > attempted.max_fragments
        ):
            self._overflowed = True
            raise MediaStreamOverflowError(attempted)

        start_byte = self._byte_frontier(len(self._fragments))
        end_byte = start_byte + raw_length
        fragment = PlaybackFragment(
            index=len(self._fragments),
            text=text,
            byte_length=raw_length,
            start_byte=start_byte,
            end_byte=end_byte,
            start_ms=pcmu_duration_ms(start_byte),
            end_ms=pcmu_duration_ms(end_byte),
        )
        self._fragments.append(fragment)
        return fragment

    def bind_mark(self, name: str) -> MarkBoundary:
        """Bind a unique mark name to exactly the current audio frontier."""

        if self._cleared_result is not None:
            raise MediaStreamError("cannot bind a mark after playback was cleared")
        mark_name = _require_opaque(
            name,
            field="mark.name",
            max_chars=MAX_MARK_NAME_CHARS,
        )
        if mark_name in self._marks:
            raise MediaStreamError("mark name is already pending")
        if len(self._fragments) <= self._confirmed_count:
            raise MediaStreamError("mark requires unconfirmed audio")
        if self._marks:
            previous = next(reversed(self._marks.values()))
            if previous.fragment_count == len(self._fragments):
                raise MediaStreamError("a mark is already bound to this frontier")
        boundary = MarkBoundary(
            name=mark_name,
            fragment_count=len(self._fragments),
            byte_frontier=self._byte_frontier(len(self._fragments)),
            timeline_ms=self.duration_ms,
        )
        self._marks[mark_name] = boundary
        return boundary

    def acknowledge_mark(self, name: str) -> MarkConfirmation:
        """Confirm a drained frontier, unless the mark was returned by clear."""

        if not self._marks:
            raise MediaStreamError("mark is unknown or was already acknowledged")
        expected_name = next(iter(self._marks))
        if name != expected_name:
            if name not in self._marks:
                raise MediaStreamError("mark is unknown or was already acknowledged")
            raise MediaStreamError("marks were acknowledged out of order")
        boundary = self._marks.pop(name)

        if self._cleared_result is not None:
            return MarkConfirmation(
                name=name,
                text_prefix=self._cleared_result.text_prefix,
                played_ms=self._cleared_result.played_ms,
                advanced=False,
                drained_after_clear=True,
            )

        previous_count = self._confirmed_count
        self._confirmed_count = max(self._confirmed_count, boundary.fragment_count)
        return MarkConfirmation(
            name=name,
            text_prefix=self._text_prefix(self._confirmed_count),
            played_ms=int(math.floor(boundary.timeline_ms)),
            advanced=self._confirmed_count > previous_count,
            drained_after_clear=False,
        )

    def barge_in(
        self,
        *,
        stream_sid: str,
        playback_clock: ConservativePlaybackClock,
        observed_at: float,
    ) -> BargeInResult:
        """Freeze a conservative audible prefix and emit Twilio ``clear``.

        The playhead is derived from local send time and acknowledged marks.
        Partially heard alignment units are intentionally omitted from text,
        while the conservative audio position is retained.
        """

        if self._cleared_result is not None:
            raise MediaStreamError("playback was already cleared")
        if not isinstance(playback_clock, ConservativePlaybackClock):
            raise MediaStreamError("a conservative playback clock is required")

        confirmed_ms = self._timeline_frontier(self._confirmed_count)
        estimated_ms = playback_clock.estimate(
            observed_at=observed_at,
            maximum_ms=self.duration_ms,
        )
        observed_ms = min(max(float(estimated_ms), confirmed_ms), self.duration_ms)
        audible_count = self._confirmed_count
        for fragment in self._fragments[self._confirmed_count :]:
            if fragment.end_ms <= observed_ms:
                audible_count = fragment.index + 1
            else:
                break
        result = BargeInResult(
            text_prefix=self._text_prefix(audible_count),
            played_ms=int(math.floor(observed_ms)),
            clear_message=build_clear_message(stream_sid=stream_sid),
        )
        self._confirmed_count = audible_count
        self._cleared_result = result
        return result

    def _byte_frontier(self, fragment_count: int) -> int:
        if fragment_count <= 0:
            return 0
        return self._fragments[fragment_count - 1].end_byte

    def _timeline_frontier(self, fragment_count: int) -> float:
        if fragment_count <= 0:
            return 0.0
        return self._fragments[fragment_count - 1].end_ms

    def _text_prefix(self, fragment_count: int) -> str:
        return "".join(fragment.text for fragment in self._fragments[:fragment_count])
