from dataclasses import dataclass

import pytest

from integrations.telephony.elevenlabs_realtime import (
    ElevenLabsSpeechStartedEvent,
    ElevenLabsTranscriptEvent,
    ElevenLabsUtteranceEndEvent,
)
from integrations.telephony.transcription import (
    PhoneUtteranceAssembler,
    PhoneTranscriptionError,
)


@dataclass(frozen=True)
class _TimedTranscript:
    text: str
    is_final: bool
    speech_final: bool
    start_seconds: float | None = 0.0
    duration_seconds: float | None = 1.0
    confidence: float | None = 0.9


def _result(
    text,
    *,
    is_final,
    speech_final=False,
    start=0.0,
    duration=1.0,
    confidence=0.9,
):
    return _TimedTranscript(
        text=text,
        is_final=is_final,
        speech_final=speech_final,
        start_seconds=start,
        duration_seconds=duration,
        confidence=confidence,
    )


def test_assembler_ignores_interims_and_concatenates_all_final_segments():
    assembler = PhoneUtteranceAssembler()

    assert assembler.feed(_result("hel", is_final=False)) is None
    assert assembler.feed(_result("hello", is_final=True)) is None
    utterance = assembler.feed(
        _result(
            "world.",
            is_final=True,
            speech_final=True,
            start=1.0,
            duration=0.5,
            confidence=0.7,
        )
    )
    assert utterance is not None
    assert utterance.text == "hello world."
    assert utterance.segment_count == 2
    assert utterance.start_seconds == 0.0
    assert utterance.end_seconds == 1.5
    assert utterance.confidence == pytest.approx(0.8)
    assert assembler.pending_segment_count == 0


def test_assembler_deduplicates_replayed_final_segment():
    assembler = PhoneUtteranceAssembler()
    segment = _result("same", is_final=True, start=3.0)
    assembler.feed(segment)
    assembler.feed(segment)

    utterance = assembler.feed(
        ElevenLabsUtteranceEndEvent(last_word_end_seconds=4.0)
    )
    assert utterance is not None
    assert utterance.text == "same"
    assert utterance.segment_count == 1


def test_speech_final_on_interim_flushes_only_previous_final_text():
    assembler = PhoneUtteranceAssembler()
    assembler.feed(_result("stable", is_final=True))

    utterance = assembler.feed(
        _result("unstable partial", is_final=False, speech_final=True)
    )
    assert utterance is not None
    assert utterance.text == "stable"


def test_non_transcript_events_do_not_create_a_turn():
    assembler = PhoneUtteranceAssembler()
    assert assembler.feed(ElevenLabsSpeechStartedEvent(0.1)) is None
    assert assembler.feed(ElevenLabsUtteranceEndEvent(0.2)) is None


def test_scribe_committed_transcript_closes_exactly_one_caller_turn():
    assembler = PhoneUtteranceAssembler()

    assert assembler.feed(
        ElevenLabsTranscriptEvent(
            text="hola",
            is_final=False,
            speech_final=False,
        )
    ) is None
    utterance = assembler.feed(
        ElevenLabsTranscriptEvent(
            text="hola Jordi",
            is_final=True,
            speech_final=True,
        )
    )

    assert utterance is not None
    assert utterance.text == "hola Jordi"
    assert utterance.segment_count == 1
    assert assembler.feed(ElevenLabsUtteranceEndEvent()) is None


def test_native_realtime_final_preserves_provider_turn_handle():
    assembler = PhoneUtteranceAssembler()
    handle = object()

    utterance = assembler.feed(
        type(
            "RealtimeFinal",
            (),
            {
                "text": "native caller turn",
                "is_final": True,
                "speech_final": True,
                "start_seconds": 1.0,
                "duration_seconds": 0.5,
                "confidence": None,
                "turn_handle": handle,
            },
        )()
    )

    assert utterance is not None
    assert utterance.turn_handle is handle
    assert utterance.input_audio_pcmu == b""


def test_assembler_rejects_mixed_native_turn_handles():
    assembler = PhoneUtteranceAssembler()

    first = _result("first", is_final=True)
    object.__setattr__(first, "turn_handle", object())
    assembler.feed(first)
    second = _result("second", is_final=True, speech_final=True, start=1.0)
    object.__setattr__(second, "turn_handle", object())

    with pytest.raises(PhoneTranscriptionError, match="turn handles"):
        assembler.feed(second)


def test_invalid_provider_timing_fails_closed():
    assembler = PhoneUtteranceAssembler()
    with pytest.raises(PhoneTranscriptionError, match="timing"):
        assembler.feed(
            _result("bad", is_final=True, start=float("nan"))
        )
