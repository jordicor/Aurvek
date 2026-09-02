from __future__ import annotations

import base64
from copy import deepcopy

import pytest

from integrations.telephony.media_streams import (
    MAX_MEDIA_PAYLOAD_BYTES,
    BargeInResult,
    ConservativePlaybackClock,
    ConnectedEvent,
    MarkEvent,
    MediaEvent,
    MediaStreamError,
    MediaStreamOverflowError,
    MediaStreamParser,
    MediaStreamProtocolError,
    PlaybackLedger,
    StartEvent,
    StopEvent,
    build_clear_message,
    build_mark_message,
    build_media_message,
)


ACCOUNT_SID = "AC" + ("1" * 32)
OTHER_ACCOUNT_SID = "AC" + ("2" * 32)
CALL_SID = "CA" + ("3" * 32)
STREAM_SID = "MZ" + ("4" * 32)


def connected_message():
    return {"event": "connected", "protocol": "Call", "version": "1.0.0"}


def start_message():
    return {
        "event": "start",
        "sequenceNumber": "1",
        "streamSid": STREAM_SID,
        "start": {
            "accountSid": ACCOUNT_SID,
            "callSid": CALL_SID,
            "streamSid": STREAM_SID,
            "tracks": ["inbound"],
            "mediaFormat": {
                "encoding": "audio/x-mulaw",
                "sampleRate": 8000,
                "channels": 1,
            },
            "customParameters": {
                "correlation_token": "opaque-session-token",
                "stream_attempt": "1",
            },
        },
    }


def media_message(*, sequence=2, chunk=1, timestamp=0, audio=b"\xff" * 160):
    return {
        "event": "media",
        "sequenceNumber": str(sequence),
        "streamSid": STREAM_SID,
        "media": {
            "track": "inbound",
            "chunk": str(chunk),
            "timestamp": str(timestamp),
            "payload": base64.b64encode(audio).decode("ascii"),
        },
    }


def ready_parser() -> MediaStreamParser:
    parser = MediaStreamParser(
        expected_account_sid=ACCOUNT_SID,
        expected_call_sid=CALL_SID,
        expected_correlation_token="opaque-session-token",
        expected_stream_attempt=1,
    )
    assert isinstance(parser.parse(connected_message()), ConnectedEvent)
    assert isinstance(parser.parse(start_message()), StartEvent)
    return parser


def test_parser_accepts_correlated_mulaw_start_and_normalizes_all_events():
    parser = ready_parser()

    connected = ConnectedEvent(protocol="Call", version="1.0.0")
    start = parser.start
    media = parser.parse(media_message())
    mark = parser.parse(
        {
            "event": "mark",
            "sequenceNumber": "3",
            "streamSid": STREAM_SID,
            "mark": {"name": "turn-1-frontier-1"},
        }
    )
    stop = parser.parse(
        {
            "event": "stop",
            "sequenceNumber": "4",
            "streamSid": STREAM_SID,
            "stop": {"accountSid": ACCOUNT_SID, "callSid": CALL_SID},
        }
    )

    assert connected == ConnectedEvent(protocol="Call", version="1.0.0")
    assert start is not None
    assert start.account_sid == ACCOUNT_SID
    assert start.call_sid == CALL_SID
    assert start.stream_sid == STREAM_SID
    assert start.correlation_token == "opaque-session-token"
    assert start.stream_attempt == 1
    assert media == MediaEvent(
        sequence_number=2,
        stream_sid=STREAM_SID,
        chunk=1,
        timestamp_ms=0,
        payload=b"\xff" * 160,
    )
    assert mark == MarkEvent(
        sequence_number=3,
        stream_sid=STREAM_SID,
        name="turn-1-frontier-1",
    )
    assert stop == StopEvent(
        sequence_number=4,
        account_sid=ACCOUNT_SID,
        call_sid=CALL_SID,
        stream_sid=STREAM_SID,
        reason="twilio_stop",
        media_chunks=1,
        media_bytes=160,
        last_media_timestamp_ms=0,
    )
    assert parser.stopped is True
    with pytest.raises(MediaStreamProtocolError, match="already stopped"):
        parser.parse(media_message(sequence=5, chunk=2, timestamp=20))


@pytest.mark.parametrize(
    ("mutate", "error_code"),
    [
        (lambda value: value["start"].update(accountSid=OTHER_ACCOUNT_SID), "account_mismatch"),
        (lambda value: value["start"].update(callSid="CA" + ("9" * 32)), "call_mismatch"),
        (lambda value: value.update(streamSid="MZ" + ("5" * 32)), "stream_mismatch"),
        (
            lambda value: value["start"]["mediaFormat"].update(
                encoding="audio/pcm"
            ),
            "media_format_mismatch",
        ),
        (
            lambda value: value["start"]["customParameters"].update(extra="no"),
            "parameter_mismatch",
        ),
        (
            lambda value: value["start"]["customParameters"].update(
                correlation_token="another-opaque-token"
            ),
            "correlation_mismatch",
        ),
        (
            lambda value: value["start"]["customParameters"].update(
                stream_attempt="2"
            ),
            "attempt_mismatch",
        ),
    ],
)
def test_start_mismatch_is_fail_closed_without_advancing_parser(mutate, error_code):
    parser = MediaStreamParser(
        expected_account_sid=ACCOUNT_SID,
        expected_call_sid=CALL_SID,
        expected_correlation_token="opaque-session-token",
        expected_stream_attempt=1,
    )
    parser.parse(connected_message())
    invalid = deepcopy(start_message())
    mutate(invalid)

    with pytest.raises(MediaStreamProtocolError) as exc:
        parser.parse(invalid)

    assert exc.value.code == error_code
    assert parser.start is None
    with pytest.raises(MediaStreamProtocolError) as poisoned:
        parser.parse(start_message())
    assert poisoned.value.code == "stream_failed"


def test_media_rejects_invalid_audio_and_non_monotonic_sequence_chunk_or_timestamp():
    parser = ready_parser()
    parser.parse(media_message())

    invalid_audio = media_message(sequence=3, chunk=2, timestamp=20)
    invalid_audio["media"]["payload"] = "not base64!"
    with pytest.raises(MediaStreamProtocolError) as exc:
        parser.parse(invalid_audio)
    assert exc.value.code == "invalid_audio"

    # Any protocol violation poisons the WebSocket; callers must close it.
    with pytest.raises(MediaStreamProtocolError) as poisoned:
        parser.parse(media_message(sequence=3, chunk=2, timestamp=20))
    assert poisoned.value.code == "stream_failed"


@pytest.mark.parametrize(
    "invalid",
    [
        media_message(sequence=4, chunk=2, timestamp=20),
        media_message(sequence=3, chunk=3, timestamp=20),
        media_message(sequence=3, chunk=2, timestamp=0),
    ],
)
def test_media_rejects_sequence_or_media_gaps_and_regressions(invalid):
    parser = ready_parser()
    parser.parse(media_message())
    with pytest.raises(MediaStreamProtocolError) as exc:
        parser.parse(invalid)
    assert exc.value.code in {"sequence_order", "media_order"}


def test_media_payload_has_a_decoded_and_encoded_size_limit():
    parser = ready_parser()

    with pytest.raises(MediaStreamProtocolError) as exc:
        parser.parse(media_message(audio=b"\xff" * (MAX_MEDIA_PAYLOAD_BYTES + 1)))

    assert exc.value.code == "audio_overflow"


def test_outbound_media_mark_and_clear_have_exact_twilio_framing():
    audio = bytes(range(160))

    assert build_media_message(stream_sid=STREAM_SID, audio=audio) == {
        "event": "media",
        "streamSid": STREAM_SID,
        "media": {"payload": base64.b64encode(audio).decode("ascii")},
    }
    assert build_mark_message(stream_sid=STREAM_SID, name="frontier-7") == {
        "event": "mark",
        "streamSid": STREAM_SID,
        "mark": {"name": "frontier-7"},
    }
    assert build_clear_message(stream_sid=STREAM_SID) == {
        "event": "clear",
        "streamSid": STREAM_SID,
    }
    with pytest.raises(MediaStreamError, match="payload limit"):
        build_media_message(
            stream_sid=STREAM_SID,
            audio=b"x" * (MAX_MEDIA_PAYLOAD_BYTES + 1),
        )


def test_normal_marks_confirm_only_the_frontier_they_were_bound_to():
    ledger = PlaybackLedger()
    first = ledger.append_fragment(text="Hello ", audio=b"a" * 160)
    first_mark = ledger.bind_mark("m-1")
    second = ledger.append_fragment(text="world", audio=b"b" * 160)
    second_mark = ledger.bind_mark("m-2")

    assert (first.start_ms, first.end_ms, first.duration_ms) == (0.0, 20.0, 20.0)
    assert (second.start_ms, second.end_ms) == (20.0, 40.0)
    assert first_mark.byte_frontier == 160
    assert second_mark.byte_frontier == 320

    confirmation = ledger.acknowledge_mark("m-1")
    assert confirmation.text_prefix == "Hello "
    assert confirmation.played_ms == 20
    assert confirmation.advanced is True
    assert confirmation.drained_after_clear is False
    assert ledger.backpressure.pending_bytes == 160

    confirmation = ledger.acknowledge_mark("m-2")
    assert confirmation.text_prefix == "Hello world"
    assert confirmation.played_ms == 40
    assert ledger.backpressure.pending_bytes == 0


def test_marks_drained_after_clear_never_confirm_the_cancelled_remainder():
    ledger = PlaybackLedger()
    ledger.append_fragment(text="one ", audio=b"a" * 160)
    ledger.bind_mark("m-1")
    ledger.append_fragment(text="two", audio=b"b" * 160)
    ledger.bind_mark("m-2")

    clock = ConservativePlaybackClock(safety_lag_ms=0)
    clock.note_audio_sent(b"a" * 320, sent_at=10.0)
    interrupted = ledger.barge_in(
        stream_sid=STREAM_SID,
        playback_clock=clock,
        observed_at=10.025,
    )
    assert interrupted == BargeInResult(
        text_prefix="one ",
        played_ms=25,
        clear_message={"event": "clear", "streamSid": STREAM_SID},
    )

    first = ledger.acknowledge_mark("m-1")
    second = ledger.acknowledge_mark("m-2")
    for drained in (first, second):
        assert drained.text_prefix == "one "
        assert drained.played_ms == 25
        assert drained.advanced is False
        assert drained.drained_after_clear is True


@pytest.mark.parametrize(
    ("observed_at", "expected_text", "expected_ms"),
    [
        (10.0, "", 0),
        (10.025, "one ", 25),
        (20.0, "one two three", 60),
    ],
)
def test_barge_in_returns_conservative_word_aligned_prefix_and_clear(
    observed_at,
    expected_text,
    expected_ms,
):
    ledger = PlaybackLedger()
    ledger.append_fragment(text="one ", audio=b"a" * 160)
    ledger.append_fragment(text="two ", audio=b"b" * 160)
    ledger.append_fragment(text="three", audio=b"c" * 160)
    clock = ConservativePlaybackClock(safety_lag_ms=0)
    clock.note_audio_sent(b"x" * 480, sent_at=10.0)

    result = ledger.barge_in(
        stream_sid=STREAM_SID,
        playback_clock=clock,
        observed_at=observed_at,
    )

    assert result.text_prefix == expected_text
    assert result.played_ms == expected_ms
    assert result.clear_message == {"event": "clear", "streamSid": STREAM_SID}


def test_playback_clock_never_credits_unsent_or_safety_lag_audio():
    clock = ConservativePlaybackClock(safety_lag_ms=80)
    clock.note_audio_sent(b"x" * 800, sent_at=100.0)

    assert clock.estimate(observed_at=100.050, maximum_ms=1_000) == 0
    assert clock.estimate(observed_at=100.180, maximum_ms=1_000) == 100
    assert clock.estimate(observed_at=110.0, maximum_ms=1_000) == 100
    clock.note_mark_confirmed(90)
    assert clock.estimate(observed_at=100.050, maximum_ms=1_000) == 90


def test_playback_overflow_is_visible_bounded_and_does_not_mutate_ledger():
    ledger = PlaybackLedger(max_buffered_bytes=200, max_buffered_fragments=2)
    ledger.append_fragment(text="safe", audio=b"a" * 160)

    with pytest.raises(MediaStreamOverflowError) as exc:
        ledger.append_fragment(text="overflow", audio=b"b" * 80)

    assert exc.value.state.pending_bytes == 240
    assert exc.value.state.max_bytes == 200
    assert len(ledger.fragments) == 1
    assert ledger.backpressure.pending_bytes == 160
    assert ledger.backpressure.overflowed is True
