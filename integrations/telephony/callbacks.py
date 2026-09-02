"""Provider callback normalization for Twilio Programmable Voice.

HTTP routes validate signatures; this module then converts the small set of
provider fields Aurvek needs into deterministic domain events.  Raw webhook
payloads are never stored wholesale, which keeps credentials, URLs and future
Twilio fields out of the operational event ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import hashlib
import json
import re
from typing import Any, Mapping

from integrations.telephony.schemas import PhoneCallStatus


_CALL_SID = re.compile(r"^CA[0-9a-fA-F]{32}$")
_STREAM_SID = re.compile(r"^MZ[0-9a-fA-F]{32}$")
_RECORDING_SID = re.compile(r"^RE[0-9a-fA-F]{32}$")

_STATUS_MAP = {
    "queued": PhoneCallStatus.QUEUED,
    "initiated": PhoneCallStatus.INITIATED,
    "ringing": PhoneCallStatus.RINGING,
    "in-progress": PhoneCallStatus.IN_PROGRESS,
    "answered": PhoneCallStatus.IN_PROGRESS,
    "completed": PhoneCallStatus.COMPLETED,
    "busy": PhoneCallStatus.BUSY,
    "no-answer": PhoneCallStatus.NO_ANSWER,
    "failed": PhoneCallStatus.FAILED,
    "canceled": PhoneCallStatus.CANCELED,
}

_SAFE_EVENT_FIELDS = (
    "AccountSid",
    "CallSid",
    "ParentCallSid",
    "CallStatus",
    "SequenceNumber",
    "Timestamp",
    "EventSid",
    "AnsweredBy",
    "CallDuration",
    "SipResponseCode",
    "StreamSid",
    "StreamName",
    "StreamEvent",
    "StreamError",
    "RecordingSid",
    "RecordingStatus",
    "RecordingDuration",
    "RecordingTrack",
    "RecordingChannels",
    "RecordingSource",
)

_MAX_PROVIDER_FIELD_CHARS = 4_096
_MAX_PROVIDER_COUNTER = 2_147_483_647
_MAX_PROVIDER_DURATION_SECONDS = 86_400
_STREAM_EVENTS = frozenset({"stream-started", "stream-stopped", "stream-error"})
_RECORDING_STATUSES = frozenset({"in-progress", "completed", "absent", "failed"})


class TwilioCallbackError(ValueError):
    """A signed callback still failed the provider-domain contract."""


@dataclass(frozen=True, slots=True)
class NormalizedCallStatus:
    call_sid: str
    status: PhoneCallStatus
    answered_by: str | None
    duration_seconds: int | None
    sequence_number: int | None
    termination_reason: str | None
    sanitized_payload: dict[str, str]
    dedupe_key: str
    provider_occurred_at: str | None = None
    provider_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedStreamStatus:
    call_sid: str
    stream_sid: str
    event: str
    error: str | None
    provider_occurred_at: str | None
    provider_event_id: str
    sanitized_payload: dict[str, str]
    dedupe_key: str


@dataclass(frozen=True, slots=True)
class NormalizedRecordingStatus:
    call_sid: str
    recording_sid: str
    status: str
    duration_seconds: int | None
    channels: int | None
    track: str | None
    source: str | None
    provider_occurred_at: str | None
    provider_event_id: str
    sanitized_payload: dict[str, str]
    dedupe_key: str


def normalize_call_status_callback(
    params: Mapping[str, Any],
) -> NormalizedCallStatus:
    full_payload = _full_safe_payload(params)
    payload = sanitize_twilio_callback(params)
    call_sid = _require_sid(payload.get("CallSid"), _CALL_SID, "CallSid")
    raw_status = str(payload.get("CallStatus") or "").strip().lower()
    status = _STATUS_MAP.get(raw_status)
    if status is None:
        raise TwilioCallbackError("Unsupported Twilio CallStatus")

    answered_by = _optional_value(payload.get("AnsweredBy"))
    duration = _optional_nonnegative_int(
        payload.get("CallDuration"),
        "CallDuration",
        maximum=_MAX_PROVIDER_DURATION_SECONDS,
    )
    sequence = _optional_nonnegative_int(
        payload.get("SequenceNumber"),
        "SequenceNumber",
        maximum=_MAX_PROVIDER_COUNTER,
    )
    occurred_at = _optional_provider_timestamp(payload.get("Timestamp"))
    event_id = _provider_event_id(
        payload.get("EventSid"),
        fallback=(
            f"{call_sid}:sequence:{sequence}"
            if sequence is not None
            else f"{call_sid}:status:{raw_status}"
        ),
    )
    termination_reason = (
        raw_status
        if status
        in {
            PhoneCallStatus.BUSY,
            PhoneCallStatus.NO_ANSWER,
            PhoneCallStatus.FAILED,
            PhoneCallStatus.CANCELED,
        }
        else None
    )
    return NormalizedCallStatus(
        call_sid=call_sid,
        status=status,
        answered_by=answered_by,
        duration_seconds=duration,
        sequence_number=sequence,
        termination_reason=termination_reason,
        sanitized_payload=payload,
        dedupe_key=callback_dedupe_key("voice_status", full_payload),
        provider_occurred_at=occurred_at,
        provider_event_id=event_id,
    )


def normalize_stream_status_callback(
    params: Mapping[str, Any],
) -> NormalizedStreamStatus:
    full_payload = _full_safe_payload(params)
    payload = sanitize_twilio_callback(params)
    call_sid = _require_sid(payload.get("CallSid"), _CALL_SID, "CallSid")
    stream_sid = require_stream_sid(payload.get("StreamSid"))
    event = str(payload.get("StreamEvent") or "").strip().lower()
    if event not in _STREAM_EVENTS:
        raise TwilioCallbackError("Unsupported Twilio StreamEvent")
    error = _optional_value(payload.get("StreamError"))
    if event == "stream-error" and error is None:
        error = "provider_stream_error"
    occurred_at = _optional_provider_timestamp(payload.get("Timestamp"))
    event_id = _provider_event_id(
        payload.get("EventSid"),
        fallback=f"{stream_sid}:{event}:{occurred_at or 'unknown-time'}",
    )
    return NormalizedStreamStatus(
        call_sid=call_sid,
        stream_sid=stream_sid,
        event=event,
        error=error,
        provider_occurred_at=occurred_at,
        provider_event_id=event_id,
        sanitized_payload=payload,
        dedupe_key=callback_dedupe_key("stream_status", full_payload),
    )


def normalize_recording_status_callback(
    params: Mapping[str, Any],
) -> NormalizedRecordingStatus:
    full_payload = _full_safe_payload(params)
    payload = sanitize_twilio_callback(params)
    call_sid = _require_sid(payload.get("CallSid"), _CALL_SID, "CallSid")
    recording_sid = require_recording_sid(payload.get("RecordingSid"))
    status = str(payload.get("RecordingStatus") or "").strip().lower()
    if status not in _RECORDING_STATUSES:
        raise TwilioCallbackError("Unsupported Twilio RecordingStatus")
    duration = _optional_nonnegative_int(
        payload.get("RecordingDuration"),
        "RecordingDuration",
        maximum=_MAX_PROVIDER_DURATION_SECONDS,
    )
    channels = _optional_nonnegative_int(
        payload.get("RecordingChannels"),
        "RecordingChannels",
        maximum=2,
    )
    if channels == 0:
        raise TwilioCallbackError("Invalid Twilio RecordingChannels")
    occurred_at = _optional_provider_timestamp(payload.get("Timestamp"))
    event_id = _provider_event_id(
        payload.get("EventSid"),
        fallback=f"{recording_sid}:status:{status}",
    )
    return NormalizedRecordingStatus(
        call_sid=call_sid,
        recording_sid=recording_sid,
        status=status,
        duration_seconds=duration,
        channels=channels,
        track=_optional_value(payload.get("RecordingTrack")),
        source=_optional_value(payload.get("RecordingSource")),
        provider_occurred_at=occurred_at,
        provider_event_id=event_id,
        sanitized_payload=payload,
        dedupe_key=callback_dedupe_key("recording_status", full_payload),
    )


def answered_by_is_machine(value: str | None) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized == "fax" or normalized.startswith("machine")


def sanitize_twilio_callback(params: Mapping[str, Any]) -> dict[str, str]:
    """Keep an allowlisted, size-bounded diagnostic payload."""

    return {
        key: value[:500]
        for key, value in _full_safe_payload(params).items()
    }


def _full_safe_payload(params: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in _SAFE_EVENT_FIELDS:
        value = params.get(key)
        if value is None:
            continue
        raw = str(value)
        if len(raw) > _MAX_PROVIDER_FIELD_CHARS:
            raise TwilioCallbackError(f"Twilio {key} exceeds its size limit")
        normalized = " ".join(
            "".join(
                character if ord(character) >= 32 and ord(character) != 127 else " "
                for character in raw
            ).split()
        )
        if normalized:
            result[key] = normalized
    return result


def callback_dedupe_key(event_type: str, payload: Mapping[str, Any]) -> str:
    normalized_type = str(event_type or "").strip().lower()
    if not normalized_type:
        raise TwilioCallbackError("event_type is required")
    canonical = json.dumps(
        {str(key): str(value) for key, value in sorted(payload.items())},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "twilio:" + hashlib.sha256(
        f"{normalized_type}\n{canonical}".encode("utf-8")
    ).hexdigest()


def require_stream_sid(value: Any) -> str:
    return _require_sid(value, _STREAM_SID, "StreamSid")


def require_recording_sid(value: Any) -> str:
    return _require_sid(value, _RECORDING_SID, "RecordingSid")


def _require_sid(value: Any, pattern: re.Pattern[str], field: str) -> str:
    normalized = str(value or "")
    if pattern.fullmatch(normalized) is None:
        raise TwilioCallbackError(f"Invalid Twilio {field}")
    return normalized


def _optional_value(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _optional_nonnegative_int(
    value: Any,
    field: str,
    *,
    maximum: int,
) -> int | None:
    if value in {None, ""}:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TwilioCallbackError(f"Invalid Twilio {field}") from exc
    if not 0 <= parsed <= maximum:
        raise TwilioCallbackError(f"Invalid Twilio {field}")
    return parsed


def _optional_provider_timestamp(value: Any) -> str | None:
    normalized = _optional_value(value)
    if normalized is None:
        return None
    try:
        parsed = parsedate_to_datetime(normalized)
        if parsed is None:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise TwilioCallbackError("Invalid Twilio Timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TwilioCallbackError("Invalid Twilio Timestamp")
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _provider_event_id(value: Any, *, fallback: str) -> str:
    normalized = _optional_value(value) or fallback
    if len(normalized) > 500 or any(ord(character) < 33 for character in normalized):
        raise TwilioCallbackError("Invalid Twilio provider event identifier")
    return normalized


__all__ = [
    "NormalizedCallStatus",
    "NormalizedRecordingStatus",
    "NormalizedStreamStatus",
    "TwilioCallbackError",
    "answered_by_is_machine",
    "callback_dedupe_key",
    "normalize_call_status_callback",
    "normalize_recording_status_callback",
    "normalize_stream_status_callback",
    "require_recording_sid",
    "require_stream_sid",
    "sanitize_twilio_callback",
]
