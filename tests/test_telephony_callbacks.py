import pytest

from integrations.telephony.callbacks import (
    TwilioCallbackError,
    answered_by_is_machine,
    callback_dedupe_key,
    normalize_call_status_callback,
    normalize_recording_status_callback,
    normalize_stream_status_callback,
    require_recording_sid,
    require_stream_sid,
    sanitize_twilio_callback,
)
from integrations.telephony.schemas import PhoneCallStatus


CALL_SID = "CA0123456789abcdefABCDEF0123456789"
STREAM_SID = "MZ" + "a" * 32
RECORDING_SID = "RE" + "B" * 32


@pytest.mark.parametrize(
    ("provider_status", "domain_status"),
    [
        ("queued", PhoneCallStatus.QUEUED),
        ("initiated", PhoneCallStatus.INITIATED),
        ("ringing", PhoneCallStatus.RINGING),
        ("in-progress", PhoneCallStatus.IN_PROGRESS),
        ("completed", PhoneCallStatus.COMPLETED),
        ("busy", PhoneCallStatus.BUSY),
        ("no-answer", PhoneCallStatus.NO_ANSWER),
        ("failed", PhoneCallStatus.FAILED),
        ("canceled", PhoneCallStatus.CANCELED),
    ],
)
def test_status_callback_maps_only_supported_call_states(provider_status, domain_status):
    result = normalize_call_status_callback(
        {
            "CallSid": CALL_SID,
            "CallStatus": provider_status,
            "SequenceNumber": "3",
            "CallDuration": "19",
        }
    )

    assert result.call_sid == CALL_SID
    assert result.status is domain_status
    assert result.sequence_number == 3
    assert result.duration_seconds == 19
    assert result.dedupe_key.startswith("twilio:")


def test_terminal_provider_result_preserves_a_nonsensitive_reason():
    result = normalize_call_status_callback(
        {"CallSid": CALL_SID, "CallStatus": "no-answer"}
    )

    assert result.termination_reason == "no-answer"


@pytest.mark.parametrize("value", ["machine_start", "machine_end_beep", "fax"])
def test_answered_by_machine_variants(value):
    assert answered_by_is_machine(value) is True


def test_human_or_missing_answered_by_is_not_machine():
    assert answered_by_is_machine("human") is False
    assert answered_by_is_machine(None) is False


def test_callback_payload_is_allowlisted_and_bounded():
    payload = sanitize_twilio_callback(
        {
            "CallSid": CALL_SID,
            "CallStatus": "completed",
            "AuthToken": "must-not-survive",
            "RecordingUrl": "https://signed.example/secret",
            "StreamError": "x" * 700,
        }
    )

    assert payload["CallSid"] == CALL_SID
    assert "AuthToken" not in payload
    assert "RecordingUrl" not in payload
    assert len(payload["StreamError"]) == 500


def test_callback_payload_removes_controls_before_storage():
    payload = sanitize_twilio_callback(
        {"CallSid": CALL_SID, "StreamError": "bad\r\nerror\x00detail"}
    )

    assert payload["StreamError"] == "bad error detail"


def test_dedupe_key_is_order_independent_but_event_specific():
    first = callback_dedupe_key("voice_status", {"b": 2, "a": 1})
    second = callback_dedupe_key("voice_status", {"a": 1, "b": 2})

    assert first == second
    assert callback_dedupe_key("stream_status", {"a": 1, "b": 2}) != first


@pytest.mark.parametrize(
    "payload",
    [
        {"CallSid": "invalid", "CallStatus": "queued"},
        {"CallSid": CALL_SID, "CallStatus": "mystery"},
        {"CallSid": CALL_SID, "CallStatus": "queued", "SequenceNumber": "-1"},
        {"CallSid": CALL_SID, "CallStatus": "queued", "CallDuration": "NaN"},
    ],
)
def test_invalid_signed_callback_still_fails_closed(payload):
    with pytest.raises(TwilioCallbackError):
        normalize_call_status_callback(payload)


def test_stream_and_recording_sids_are_typed():
    assert require_stream_sid(STREAM_SID) == STREAM_SID
    assert require_recording_sid(RECORDING_SID) == RECORDING_SID
    with pytest.raises(TwilioCallbackError):
        require_stream_sid("RE" + "a" * 32)


@pytest.mark.parametrize(
    "event",
    ["stream-started", "stream-stopped", "stream-error"],
)
def test_stream_callbacks_have_typed_identity_time_and_dedupe(event):
    normalized = normalize_stream_status_callback(
        {
            "CallSid": CALL_SID,
            "StreamSid": STREAM_SID,
            "StreamEvent": event,
            "StreamError": "provider detail" if event == "stream-error" else "",
            "Timestamp": "Tue, 31 Aug 2026 03:04:05 +0000",
            "EventSid": "EV" + "c" * 32,
        }
    )

    assert normalized.call_sid == CALL_SID
    assert normalized.stream_sid == STREAM_SID
    assert normalized.event == event
    assert normalized.provider_occurred_at == "2026-08-31T03:04:05Z"
    assert normalized.provider_event_id == "EV" + "c" * 32
    assert normalized.dedupe_key.startswith("twilio:")


@pytest.mark.parametrize(
    "status",
    ["in-progress", "completed", "absent", "failed"],
)
def test_recording_callbacks_have_typed_identity_and_bounded_metadata(status):
    normalized = normalize_recording_status_callback(
        {
            "CallSid": CALL_SID,
            "RecordingSid": RECORDING_SID,
            "RecordingStatus": status,
            "RecordingDuration": "3600",
            "RecordingChannels": "2",
            "RecordingTrack": "both",
            "RecordingSource": "DialVerb",
            "Timestamp": "2026-08-31T03:04:05Z",
        }
    )

    assert normalized.recording_sid == RECORDING_SID
    assert normalized.status == status
    assert normalized.duration_seconds == 3600
    assert normalized.channels == 2
    assert normalized.provider_event_id == f"{RECORDING_SID}:status:{status}"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "CallSid": CALL_SID,
            "StreamSid": STREAM_SID,
            "StreamEvent": "mystery",
        },
        {
            "CallSid": CALL_SID,
            "RecordingSid": RECORDING_SID,
            "RecordingStatus": "completed",
            "RecordingDuration": "86401",
        },
        {
            "CallSid": CALL_SID,
            "CallStatus": "queued",
            "SequenceNumber": str(2**31),
        },
    ],
)
def test_callback_counters_and_event_names_are_fail_closed(payload):
    normalizer = (
        normalize_stream_status_callback
        if "StreamEvent" in payload
        else normalize_recording_status_callback
        if "RecordingStatus" in payload
        else normalize_call_status_callback
    )
    with pytest.raises(TwilioCallbackError):
        normalizer(payload)


def test_dedupe_hashes_full_diagnostic_value_before_storage_truncation():
    prefix = "x" * 500
    first = {"CallSid": CALL_SID, "StreamError": prefix + "a"}
    second = {"CallSid": CALL_SID, "StreamError": prefix + "b"}

    assert sanitize_twilio_callback(first) == sanitize_twilio_callback(second)
    assert callback_dedupe_key("stream_status", first) != callback_dedupe_key(
        "stream_status", second
    )
