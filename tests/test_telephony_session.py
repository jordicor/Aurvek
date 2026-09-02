from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from integrations.telephony import session as session_module
from integrations.telephony.billing import PhoneBillingError, PhoneBillingExhausted
from integrations.telephony.clock import EndCallDirective, EndCallReason
from integrations.telephony.deepgram_live import (
    DeepgramSpeechStartedEvent,
    DeepgramTranscriptEvent,
    DeepgramUtteranceEndEvent,
)
from integrations.telephony.elevenlabs_realtime import (
    ElevenLabsMetadataEvent,
    ElevenLabsSpeechStartedEvent,
    ElevenLabsTranscriptEvent,
    ElevenLabsUtteranceEndEvent,
)
from integrations.telephony.realtime_call import (
    RealtimeCallSpeechStartedEvent,
    RealtimeCallTranscriptEvent,
)
from integrations.telephony.phone_context import PhoneTurnLinkState
from integrations.telephony.repository import PhoneHangupAttemptClaim
from integrations.telephony.session import (
    PhoneMediaSession,
    PhoneMediaSessionContext,
    PhoneMediaSessionError,
)
from integrations.telephony.recording import LocalRecordingAsset
from integrations.telephony.transcription import FinalPhoneUtterance


ACCOUNT_SID = "AC" + "1" * 32
CALL_SID = "CA" + "2" * 32
STREAM_SID = "MZ" + "3" * 32
TOKEN = "a" * 43


def snapshot(*, maximum: int = 3_600) -> dict:
    return {
        "conversation_id": 10,
        "owner_user_id": 1,
        "prompt_id": 2,
        "llm_id": 3,
        "stt_locale": "multi",
        "max_duration_seconds": maximum,
        "warning_milestones_seconds": [900, 300, 180, 60]
        if maximum > 900
        else [],
        "silence_prompt_seconds": None,
        "silence_hangup_seconds": None,
        "ai_initiation_mode": "on_request",
        "inbound_greeting_mode": "inherit",
        "outbound_greeting_mode": "inherit",
        "recording_default": False,
        "amd_default": False,
    }


def context(
    *,
    maximum: int = 3_600,
    started_at: datetime | None = None,
    stream_attempt: int = 0,
):
    return PhoneMediaSessionContext(
        call_id="call-session-1",
        provider_call_sid=CALL_SID,
        account_sid=ACCOUNT_SID,
        dispatch_token=TOKEN,
        stream_attempt=stream_attempt,
        owner_user_id=1,
        conversation_id=10,
        foreground_epoch=7,
        foreground_lease_owner="media:call-session-1",
        call_snapshot=snapshot(maximum=maximum),
        recording_enabled=False,
        direction="inbound",
        started_at=started_at or datetime.now(UTC),
    )


class FakeDeepgram:
    def __init__(self):
        self.audio = []
        self.connected = False
        self.finalized = 0

    async def connect(self):
        self.connected = True
        return self

    async def send_audio(self, audio):
        self.audio.append(bytes(audio))

    async def events(self):
        if False:
            yield None

    async def finalize(self):
        self.finalized += 1

    async def close(self):
        return None


class FakeRepository:
    def __init__(self):
        self.attached = []
        self.delivered_milestones = ()
        self.milestone_records = []
        self.stream_attempt_results = []

    async def attach_stream(self, **values):
        self.attached.append(values)
        return {
            "foreground_fencing_token": 7,
            "answered_at": datetime.now(UTC).isoformat(),
        }

    async def delivered_call_milestones(self, **_values):
        return self.delivered_milestones

    async def record_delivered_call_milestones(self, **values):
        self.milestone_records.append(values)

    async def persist_local_recording(self, **values):
        raise AssertionError("disabled recording must not persist")

    async def count_caller_turns(self, **_values):
        return 0

    async def renew_session_foreground(self, **_values):
        return True

    async def record_stream_attempt_result(self, **values):
        if values not in self.stream_attempt_results:
            self.stream_attempt_results.append(values)
            return True
        return False

    async def get_stream_attempt_result(self, *, call_id, stream_attempt):
        for result in self.stream_attempt_results:
            if (
                result["call_id"] == call_id
                and result["stream_attempt"] == stream_attempt
            ):
                return {
                    "stream_attempt": stream_attempt,
                    "reason": result["reason"],
                    "reconnectable": result["reconnectable"],
                    "internal_failure": result["internal_failure"],
                }
        return None

    async def record_hangup_requested(self, **_values):
        return PhoneHangupAttemptClaim(
            call_id="call-session-1",
            provider_call_sid=CALL_SID,
            state="in_flight",
            attempt_count=1,
            attempt_token="attempt-token",
            lease_until="2030-01-01T00:01:00Z",
            reason=str(_values["reason"]),
            target_status=_values["target_status"].value,
            origin=str(_values["origin"]),
            claimed=True,
        )

    async def mark_hangup_unresolved(self, **_values):
        return True

    async def mark_hangup_accepted(self, **_values):
        return True


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, value):
        self.sent.append(value)


def make_session(*, call_context=None, **overrides):
    values = {
        "deepgram": FakeDeepgram(),
        "repository": FakeRepository(),
        "current_user_loader": _user,
        "hangup_call": _noop,
        "notice_loader": _noop_notice_loader,
        "caller_turn_persister": _noop_caller_turn_persister,
    }
    values.update(overrides)
    return PhoneMediaSession(call_context or context(), **values)


def realtime_context():
    call_context = context()
    return replace(
        call_context,
        call_snapshot={
            **call_context.call_snapshot,
            "runtime_llm_id": 3,
            "runtime_kind": "openai_realtime",
            "runtime_model": "gpt-realtime-2.1-mini",
            "reasoning_selection": {"mode": "off"},
            "phone_realtime_voice": "marin",
        },
    )


def test_session_uses_snapshot_endpointing_for_elevenlabs_scribe() -> None:
    call_context = context()
    call_context = replace(
        call_context,
        call_snapshot={**call_context.call_snapshot, "endpointing_ms": 1_600},
    )
    session = PhoneMediaSession.with_elevenlabs_key_provider(
        call_context,
        elevenlabs_api_key_provider=lambda: "synthetic-key",
        repository=FakeRepository(),
        current_user_loader=_user,
        hangup_call=_noop,
        notice_loader=_noop_notice_loader,
    )

    assert session.stt.options.endpointing_ms == 1_600


def test_realtime_factory_uses_snapshot_model_voice_without_scribe_billing() -> None:
    session = PhoneMediaSession.with_openai_realtime_key_provider(
        realtime_context(),
        openai_api_key_provider=lambda: "synthetic-openai-key",
        repository=FakeRepository(),
        current_user_loader=_user,
        hangup_call=_noop,
        notice_loader=_noop_notice_loader,
    )

    assert isinstance(session.stt, session_module.OpenAIRealtimeCallBridge)
    assert session.stt.options.model == "gpt-realtime-2.1-mini"
    assert session.stt.options.voice == "marin"
    assert session._billing_meter.include_stt is True
    assert session._billing_meter.stt_provider == "openai"


def test_pcmu_activity_filter_rejects_silence_and_single_sample_click() -> None:
    assert session_module._pcmu_has_speech_activity(b"\xff" * 160) is False
    assert session_module._pcmu_has_speech_activity(
        b"\x00" + b"\xff" * 159
    ) is False
    assert session_module._pcmu_has_speech_activity(
        b"\x00" * 16 + b"\xff" * 144
    ) is True


async def _user(_user_id):
    return type("User", (), {"is_enabled": True})()


async def _noop(_value):
    return True


async def _noop_caller_turn_persister(**_values):
    return 101


class ImmediateCachedPlayback:
    def __init__(self, action=None):
        self.action = action

    async def run(self, **_values):
        if self.action is not None:
            self.action()

    async def acknowledge_mark(self, _name):
        return None

    def owns_mark(self, _name):
        return False

    async def barge_in(self):
        return None

    async def disconnect(self):
        return None


async def _noop_notice_loader(_context, _kind):
    return ImmediateCachedPlayback()


def media_messages(payload: bytes = b"\xff" * 160, *, stream_attempt: int = 0):
    import base64

    connected = {"event": "connected", "protocol": "Call", "version": "1.0.0"}
    start = {
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
                "correlation_token": TOKEN,
                "stream_attempt": str(stream_attempt),
            },
        },
    }
    media = {
        "event": "media",
        "sequenceNumber": "2",
        "streamSid": STREAM_SID,
        "media": {
            "track": "inbound",
            "chunk": "1",
            "timestamp": "0",
            "payload": base64.b64encode(payload).decode("ascii"),
        },
    }
    return connected, start, media


@pytest.mark.asyncio
async def test_stream_is_durably_attached_before_forwarding_raw_pcmu_to_stt() -> None:
    session = make_session()
    websocket = FakeWebSocket()
    connected, start, media = media_messages()

    await session.feed_twilio_message(json.dumps(connected), websocket)
    await session.feed_twilio_message(json.dumps(start), websocket)
    await session.feed_twilio_message(json.dumps(media), websocket)

    assert session._stream_sid == STREAM_SID
    assert session.repository.attached == [
        {
            "call_id": "call-session-1",
            "provider_call_sid": CALL_SID,
            "provider_stream_sid": STREAM_SID,
            "stream_attempt": 0,
        }
    ]
    assert session.stt.audio == [b"\xff" * 160]


@pytest.mark.asyncio
async def test_scribe_partial_barges_active_playback_before_new_turn() -> None:
    session = make_session()
    calls = []

    class Playback:
        output_started = True

        async def barge_in(self):
            calls.append("barge")

    session._active_playback = Playback()
    await session.feed_stt_event(ElevenLabsSpeechStartedEvent(), FakeWebSocket())

    assert calls == []
    await session.feed_stt_event(
        ElevenLabsTranscriptEvent(
            text="espera, eso no es correcto",
            is_final=False,
            speech_final=False,
        ),
        FakeWebSocket(),
    )

    assert calls == ["barge"]
    assert session._utterances.empty()


@pytest.mark.asyncio
async def test_scribe_barges_published_playback_before_output_starts() -> None:
    session = make_session()
    calls = []

    class Playback:
        output_started = False

        async def barge_in(self):
            calls.append("barge")

    playback = Playback()
    session._active_playback = playback
    initial_generation = session._speech_generation
    await session.feed_stt_event(ElevenLabsSpeechStartedEvent(), FakeWebSocket())
    await session.feed_stt_event(
        ElevenLabsTranscriptEvent(
            text="espera, todavía estoy hablando",
            is_final=True,
            speech_final=True,
        ),
        FakeWebSocket(),
    )

    assert calls == ["barge"]
    assert session._pending_barge_in.armed is False
    assert session._speech_generation == initial_generation + 1
    assert session._utterances.get_nowait().text == (
        "espera, todavía estoy hablando"
    )


@pytest.mark.asyncio
async def test_short_scribe_backchannel_uses_observed_pcmu_and_does_not_barge() -> None:
    calls = []

    class Playback:
        async def barge_in(self):
            calls.append("barge")

    session = make_session()
    websocket = FakeWebSocket()
    connected, start, media = media_messages(payload=b"\x00" * 800)
    await session.feed_twilio_message(connected, websocket)
    await session.feed_twilio_message(start, websocket)
    await session.feed_twilio_message(media, websocket)
    session._active_playback = Playback()

    await session.feed_stt_event(ElevenLabsSpeechStartedEvent(), websocket)
    await session.feed_stt_event(
        ElevenLabsTranscriptEvent(
            text="sí",
            is_final=True,
            speech_final=True,
        ),
        websocket,
    )

    assert calls == []
    assert session._utterances.empty()


@pytest.mark.asyncio
async def test_short_un_momento_is_an_interrupting_scribe_turn() -> None:
    calls = []

    class Playback:
        async def barge_in(self):
            calls.append("barge")

    session = make_session()
    websocket = FakeWebSocket()
    connected, start, media = media_messages(payload=b"\x00" * 800)
    await session.feed_twilio_message(connected, websocket)
    await session.feed_twilio_message(start, websocket)
    await session.feed_twilio_message(media, websocket)
    session._active_playback = Playback()

    await session.feed_stt_event(ElevenLabsSpeechStartedEvent(), websocket)
    await session.feed_stt_event(
        ElevenLabsTranscriptEvent(
            text="un momento",
            is_final=True,
            speech_final=True,
        ),
        websocket,
    )

    assert calls == ["barge"]
    assert session._utterances.get_nowait().text == "un momento"
    assert session._utterances.empty()


@pytest.mark.asyncio
async def test_sustained_scribe_backchannel_barges_from_observed_pcmu_duration() -> None:
    calls = []

    class Playback:
        async def barge_in(self):
            calls.append("barge")

    session = make_session()
    websocket = FakeWebSocket()
    connected, start, _ = media_messages()
    await session.feed_twilio_message(connected, websocket)
    await session.feed_twilio_message(start, websocket)
    session._active_playback = Playback()
    for index in range(5):
        _, _, media = media_messages(payload=b"\x00" * 160)
        media["sequenceNumber"] = str(index + 2)
        media["media"]["chunk"] = str(index + 1)
        media["media"]["timestamp"] = str(index * 20)
        await session.feed_twilio_message(media, websocket)

    await session.feed_stt_event(ElevenLabsSpeechStartedEvent(), websocket)
    await session.feed_stt_event(
        ElevenLabsTranscriptEvent(
            text="sí",
            is_final=False,
            speech_final=False,
        ),
        websocket,
    )
    assert calls == []

    # Scribe need not emit another hypothesis while the same word is held.
    # Continued observed voice crosses the safe backchannel window and clears
    # output from the media receive path itself.
    for index in range(5, 40):
        _, _, media = media_messages(payload=b"\x00" * 160)
        media["sequenceNumber"] = str(index + 2)
        media["media"]["chunk"] = str(index + 1)
        media["media"]["timestamp"] = str(index * 20)
        await session.feed_twilio_message(media, websocket)

    assert calls == ["barge"]
    assert session._utterances.empty()


@pytest.mark.asyncio
async def test_scribe_committed_transcript_admits_one_final_utterance() -> None:
    session = make_session()
    websocket = FakeWebSocket()

    await session.feed_stt_event(ElevenLabsSpeechStartedEvent(), websocket)
    await session.feed_stt_event(
        ElevenLabsTranscriptEvent(
            text="Esta es la frase final.",
            is_final=True,
            speech_final=True,
        ),
        websocket,
    )
    # The adapter emits an end marker after every committed transcript; it
    # must not duplicate the caller turn already closed by speech_final.
    await session.feed_stt_event(ElevenLabsUtteranceEndEvent(), websocket)

    utterance = session._utterances.get_nowait()
    assert utterance.text == "Esta es la frase final."
    assert session._utterances.empty()


@pytest.mark.asyncio
async def test_realtime_final_utterance_keeps_provider_turn_handle() -> None:
    session = make_session(call_context=realtime_context())
    websocket = FakeWebSocket()
    before_speech = b"\x10" * 160
    during_speech = b"\x20" * 160
    connected, start, first_media = media_messages(payload=before_speech)
    _, _, second_media = media_messages(payload=during_speech)
    second_media["sequenceNumber"] = "3"
    second_media["media"]["chunk"] = "2"
    second_media["media"]["timestamp"] = "20"

    await session.feed_twilio_message(connected, websocket)
    await session.feed_twilio_message(start, websocket)
    await session.feed_twilio_message(first_media, websocket)
    await session.feed_stt_event(
        RealtimeCallSpeechStartedEvent("item-1", 0), websocket
    )
    await session.feed_twilio_message(second_media, websocket)
    await session.feed_stt_event(
        RealtimeCallTranscriptEvent(
            item_id="item-1",
            text="Esta es la frase final.",
            is_final=True,
            speech_final=True,
            turn_handle=(turn_handle := object()),
        ),
        websocket,
    )

    utterance = session._utterances.get_nowait()
    assert utterance.text == "Esta es la frase final."
    assert utterance.input_audio_pcmu == b""
    assert utterance.turn_handle is turn_handle
    assert session.stt.audio == [before_speech, during_speech]
    assert session._utterances.empty()


@pytest.mark.asyncio
async def test_scribe_session_id_is_reported_through_generic_stt_billing() -> None:
    captured = []

    class Meter:
        def note_stt_metadata(self, **values):
            captured.append(values)

    session = make_session(billing_meter=Meter())
    await session.feed_stt_event(
        ElevenLabsMetadataEvent(
            session_id="scribe-session",
            model_id="scribe_v2_realtime",
            audio_format="ulaw_8000",
            sample_rate_hz=8_000,
            language_code="es",
        ),
        FakeWebSocket(),
    )

    assert captured == [
        {"duration_seconds": None, "session_id": "scribe-session"}
    ]


@pytest.mark.asyncio
async def test_real_speech_barges_cached_notice() -> None:
    calls = []

    class Notice:
        async def barge_in(self):
            calls.append("notice-barge")

    session = make_session()
    session._active_notice = Notice()

    await session.feed_deepgram_event(
        DeepgramSpeechStartedEvent(0.25), FakeWebSocket()
    )

    await session.feed_deepgram_event(
        DeepgramTranscriptEvent(
            text="please stop there",
            is_final=False,
            speech_final=False,
            from_finalize=False,
            start_seconds=0.25,
            duration_seconds=0.4,
            confidence=0.95,
            words=(),
        ),
        FakeWebSocket(),
    )

    assert calls == ["notice-barge"]


@pytest.mark.asyncio
@pytest.mark.parametrize("backchannel", ["sí", "ajá"])
async def test_brief_backchannel_does_not_cut_active_playback(
    backchannel: str,
) -> None:
    calls = []

    class Playback:
        async def barge_in(self):
            calls.append("barge")

    session = make_session()
    session._active_playback = Playback()
    await session.feed_deepgram_event(DeepgramSpeechStartedEvent(1.0), FakeWebSocket())
    await session.feed_deepgram_event(
        DeepgramTranscriptEvent(
            text=backchannel,
            is_final=True,
            speech_final=True,
            from_finalize=False,
            start_seconds=1.0,
            duration_seconds=0.45,
            confidence=0.95,
            words=(),
        ),
        FakeWebSocket(),
    )

    assert calls == []
    assert session._utterances.empty()
    assert session._assembler.pending_segment_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("backchannel", ["sí", "ajá", "un momento"])
async def test_brief_backchannel_without_active_output_remains_a_user_turn(
    backchannel: str,
) -> None:
    session = make_session()

    await session.feed_deepgram_event(
        DeepgramSpeechStartedEvent(1.0), FakeWebSocket()
    )
    await session.feed_deepgram_event(
        DeepgramTranscriptEvent(
            text=backchannel,
            is_final=True,
            speech_final=True,
            from_finalize=False,
            start_seconds=1.0,
            duration_seconds=0.45,
            confidence=0.95,
            words=(),
        ),
        FakeWebSocket(),
    )

    utterance = session._utterances.get_nowait()
    assert utterance.text == backchannel


@pytest.mark.asyncio
async def test_suppressed_final_backchannel_does_not_contaminate_next_utterance() -> None:
    class Playback:
        async def barge_in(self):
            raise AssertionError("brief backchannel must not barge")

    session = make_session()
    session._active_playback = Playback()
    await session.feed_deepgram_event(
        DeepgramSpeechStartedEvent(1.0), FakeWebSocket()
    )
    await session.feed_deepgram_event(
        DeepgramTranscriptEvent(
            text="sí",
            is_final=True,
            speech_final=False,
            from_finalize=False,
            start_seconds=1.0,
            duration_seconds=0.25,
            confidence=0.95,
            words=(),
        ),
        FakeWebSocket(),
    )
    await session.feed_deepgram_event(
        DeepgramUtteranceEndEvent(last_word_end_seconds=1.25),
        FakeWebSocket(),
    )

    assert session._utterances.empty()
    assert session._assembler.pending_segment_count == 0

    session._active_playback = None
    await session.feed_deepgram_event(
        DeepgramSpeechStartedEvent(2.0), FakeWebSocket()
    )
    await session.feed_deepgram_event(
        DeepgramTranscriptEvent(
            text="ahora sí quiero responder",
            is_final=True,
            speech_final=True,
            from_finalize=False,
            start_seconds=2.0,
            duration_seconds=0.8,
            confidence=0.95,
            words=(),
        ),
        FakeWebSocket(),
    )

    utterance = session._utterances.get_nowait()
    assert utterance.text == "ahora sí quiero responder"


@pytest.mark.asyncio
async def test_long_same_words_are_not_treated_as_brief_backchannel() -> None:
    calls = []

    class Playback:
        async def barge_in(self):
            calls.append("barge")

    session = make_session()
    session._active_playback = Playback()
    await session.feed_deepgram_event(DeepgramSpeechStartedEvent(1.0), FakeWebSocket())
    await session.feed_deepgram_event(
        DeepgramTranscriptEvent(
            text="un momento",
            is_final=True,
            speech_final=True,
            from_finalize=False,
            start_seconds=1.0,
            duration_seconds=1.5,
            confidence=0.95,
            words=(),
        ),
        FakeWebSocket(),
    )

    assert calls == ["barge"]
    utterance = session._utterances.get_nowait()
    assert utterance.text == "un momento"


@pytest.mark.asyncio
async def test_interrupt_sensitivity_changes_confirmation_window() -> None:
    event = DeepgramTranscriptEvent(
        text="wait, that is incorrect",
        is_final=False,
        speech_final=False,
        from_finalize=False,
        start_seconds=1.0,
        duration_seconds=0.25,
        confidence=0.95,
        words=(),
    )
    high_calls = []
    low_calls = []

    class Playback:
        def __init__(self, calls):
            self.calls = calls

        async def barge_in(self):
            self.calls.append("barge")

    high = make_session()
    high.settings = replace(
        high.settings,
        interrupt_sensitivity="high",
        barge_in_confirmation_ms=175,
    )
    high._active_playback = Playback(high_calls)
    low = make_session()
    low.settings = replace(
        low.settings,
        interrupt_sensitivity="low",
        barge_in_confirmation_ms=612,
    )
    low._active_playback = Playback(low_calls)

    for session in (high, low):
        await session.feed_deepgram_event(
            DeepgramSpeechStartedEvent(1.0), FakeWebSocket()
        )
        await session.feed_deepgram_event(event, FakeWebSocket())

    assert high_calls == ["barge"]
    assert low_calls == []


@pytest.mark.asyncio
async def test_non_interruptible_prompt_still_transcribes_without_barge() -> None:
    calls = []

    class Playback:
        async def barge_in(self):
            calls.append("barge")

    session = make_session()
    session.settings = replace(session.settings, interruptible=False)
    session._active_playback = Playback()
    await session.feed_deepgram_event(DeepgramSpeechStartedEvent(1.0), FakeWebSocket())
    await session.feed_deepgram_event(
        DeepgramTranscriptEvent(
            text="stop, I need to correct that",
            is_final=True,
            speech_final=True,
            from_finalize=False,
            start_seconds=1.0,
            duration_seconds=0.8,
            confidence=0.95,
            words=(),
        ),
        FakeWebSocket(),
    )

    assert calls == []
    assert not session._utterances.empty()


@pytest.mark.asyncio
async def test_final_utterance_uses_canonical_runtime_and_fenced_context(monkeypatch) -> None:
    captured = {}

    class Runtime:
        def __init__(self, key):
            self.key = key

    async def starter(**values):
        captured.update(values)
        return Runtime(values["phone_turn"].context.turn_key)

    class Playback:
        def __init__(self, **values):
            captured["playback"] = values

        async def run(self):
            return type(
                "Result",
                (),
                {"interrupted": False, "confirmed_text": "heard", "played_ms": 100},
            )()

    class ForbiddenRealtimePlayback:
        def __init__(self, **_values):
            raise AssertionError("standard phone turns must not use Realtime playback")

    monkeypatch.setattr("integrations.telephony.session.PhoneTurnPlayback", Playback)
    monkeypatch.setattr(
        "integrations.telephony.session.RealtimeTurnPlayback",
        ForbiddenRealtimePlayback,
    )
    session = make_session(runtime_starter=starter)
    session._current_user = type("User", (), {"is_enabled": True})()
    session._stream_sid = STREAM_SID
    utterance = FinalPhoneUtterance("hello", 1, 1.0, 1.5, 0.9)

    await session._run_turn(utterance, FakeWebSocket())

    assert captured["caller_text"] == "hello"
    assert captured["conversation_id"] == 10
    assert captured["expected_llm_id"] == 3
    assert captured["runtime_llm_id"] == 3
    assert captured["reasoning_selection"] == {"mode": "default"}
    phone_turn = captured["phone_turn"]
    assert phone_turn.context.channel == "phone"
    assert phone_turn.context.persistence == "deferred"
    assert phone_turn.context.turn_key.turn_id == "stt-1"
    assert "openai_realtime_bridge" not in phone_turn.context.provenance
    assert "remaining_seconds=" in phone_turn.context.provenance["internal_turn_context"]
    assert session._caller_turns == 1


@pytest.mark.asyncio
async def test_realtime_turn_passes_bridge_and_uses_realtime_playback(monkeypatch) -> None:
    captured = {}

    class Bridge:
        _aurvek_internal_realtime_bridge = True

        def __init__(self):
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1

    bridge = Bridge()

    class Runtime:
        def __init__(self, key):
            self.key = key

    async def starter(**values):
        captured.update(values)
        return Runtime(values["phone_turn"].context.turn_key)

    class RealtimePlayback:
        def __init__(self, **values):
            captured["realtime_playback"] = values

        async def run(self):
            return type(
                "Result",
                (),
                {"interrupted": False, "confirmed_text": "heard", "played_ms": 100},
            )()

    class ForbiddenStandardPlayback:
        def __init__(self, **_values):
            raise AssertionError("Realtime phone turns must not use standard playback")

    monkeypatch.setattr(
        "integrations.telephony.session.RealtimeTurnPlayback",
        RealtimePlayback,
    )
    monkeypatch.setattr(
        "integrations.telephony.session.PhoneTurnPlayback",
        ForbiddenStandardPlayback,
    )
    session = make_session(
        call_context=realtime_context(),
        runtime_starter=starter,
    )
    session._current_user = type("User", (), {"is_enabled": True})()
    session._stream_sid = STREAM_SID

    await session._run_turn(
        FinalPhoneUtterance(
            "hello",
            1,
            1.0,
            1.5,
            0.9,
            turn_handle=bridge,
        ),
        FakeWebSocket(),
    )

    phone_turn = captured["phone_turn"]
    assert phone_turn.context.provenance["openai_realtime_bridge"] is bridge
    assert captured["realtime_playback"]["bridge"] is bridge
    assert captured["realtime_playback"]["phone_turn"] is phone_turn
    assert bridge.close_calls == 1
    assert session._caller_turns == 1


@pytest.mark.asyncio
async def test_realtime_turn_never_falls_back_to_canonical_tts(
    monkeypatch,
) -> None:
    captured = {"playbacks": []}

    class Bridge:
        _aurvek_internal_realtime_bridge = True

        async def close(self):
            captured["bridge_closed"] = True

    bridge = Bridge()

    class Runtime:
        def __init__(self, key):
            self.key = key

    async def starter(**values):
        return Runtime(values["phone_turn"].context.turn_key)

    class RealtimePlayback:
        def __init__(self, **_values):
            captured["playbacks"].append("realtime")

        async def run(self):
            raise session_module.RealtimePlaybackError(
                "native audio unavailable"
            )

    class StandardPlayback:
        def __init__(self, **_values):
            raise AssertionError(
                "Realtime phone turns must never use ElevenLabs TTS"
            )

    monkeypatch.setattr(
        "integrations.telephony.session.RealtimeTurnPlayback",
        RealtimePlayback,
    )
    monkeypatch.setattr(
        "integrations.telephony.session.PhoneTurnPlayback",
        StandardPlayback,
    )
    session = make_session(
        call_context=realtime_context(),
        runtime_starter=starter,
    )
    session._current_user = type("User", (), {"is_enabled": True})()
    session._stream_sid = STREAM_SID

    with pytest.raises(session_module.RealtimePlaybackError):
        await session._run_turn(
            FinalPhoneUtterance(
                "hello",
                1,
                1.0,
                1.5,
                0.9,
                turn_handle=bridge,
            ),
            FakeWebSocket(),
        )

    assert captured["playbacks"] == ["realtime"]
    assert captured["bridge_closed"] is True
    assert session._caller_turns == 0


@pytest.mark.asyncio
async def test_expired_deadline_plays_notice_then_hangs_up() -> None:
    actions = []

    async def notice(_context, kind):
        return ImmediateCachedPlayback(
            lambda: actions.append(("notice", kind, STREAM_SID))
        )

    async def hangup(call_sid):
        actions.append(("hangup", call_sid))
        return True

    expired = context(
        maximum=1,
        started_at=datetime.now(UTC) - timedelta(seconds=5),
    )
    session = PhoneMediaSession(
        expired,
        deepgram=FakeDeepgram(),
        repository=FakeRepository(),
        current_user_loader=_user,
        hangup_call=hangup,
        notice_loader=notice,
    )
    session._stream_sid = STREAM_SID

    await session._timer_loop(FakeWebSocket())

    assert actions == [
        ("notice", "deadline", STREAM_SID),
        ("hangup", CALL_SID),
    ]
    assert session._stopping.is_set()


@pytest.mark.asyncio
async def test_initial_greeting_backend_is_required_before_provider_streams() -> None:
    deepgram = FakeDeepgram()
    session = make_session(deepgram=deepgram)

    with pytest.raises(PhoneMediaSessionError, match="greeting cache backend"):
        await session.run(FakeWebSocket())

    assert deepgram.connected is False


@pytest.mark.asyncio
async def test_stream_start_reanchors_clock_to_durable_answer_time() -> None:
    answered_at = datetime.now(UTC) - timedelta(minutes=7)

    class Repository(FakeRepository):
        async def attach_stream(self, **values):
            self.attached.append(values)
            return {
                "foreground_fencing_token": 7,
                "answered_at": answered_at.isoformat(),
            }

    session = make_session(repository=Repository())
    connected, start, _ = media_messages()

    await session.feed_twilio_message(connected, FakeWebSocket())
    await session.feed_twilio_message(start, FakeWebSocket())

    assert session.clock.started_at == answered_at
    assert 419 <= session.clock.peek_safe_point().elapsed_seconds <= 421


@pytest.mark.asyncio
async def test_stop_event_finalizes_and_bounded_drains_confirmed_phrase_before_shutdown(
    monkeypatch,
) -> None:
    persisted = []

    async def persist_caller(**values):
        persisted.append(("caller", values["caller_text"]))
        assert values["phone_turn"].context.persistence == "ingest_only"
        return 101

    deepgram = FakeDeepgram()
    monkeypatch.setattr(
        "integrations.telephony.session._STOP_FINAL_DRAIN_TOTAL_SECONDS", 0.05
    )
    session = make_session(
        deepgram=deepgram, caller_turn_persister=persist_caller
    )
    session._current_user = await _user(1)
    websocket = FakeWebSocket()
    connected, start, _ = media_messages()
    await session.feed_twilio_message(connected, websocket)
    await session.feed_twilio_message(start, websocket)
    stop = {
        "event": "stop",
        "sequenceNumber": "2",
        "streamSid": STREAM_SID,
        "stop": {"accountSid": ACCOUNT_SID, "callSid": CALL_SID},
    }

    stop_task = asyncio.create_task(
        session.feed_twilio_message(stop, websocket)
    )
    for _ in range(10):
        if deepgram.finalized:
            break
        await asyncio.sleep(0)
    assert deepgram.finalized == 1
    await session.feed_deepgram_event(
        DeepgramTranscriptEvent(
            text="the final phrase",
            is_final=True,
            speech_final=True,
            from_finalize=True,
            start_seconds=4.0,
            duration_seconds=0.8,
            confidence=0.97,
            words=(),
        ),
        websocket,
    )
    await asyncio.wait_for(stop_task, timeout=1.0)

    assert persisted == [("caller", "the final phrase")]
    assert session._caller_turns == 1
    assert session._stop_reason == "twilio_stop"
    assert session._stopping.is_set()


@pytest.mark.asyncio
async def test_stop_quiescence_admits_two_committed_scribe_segments(
    monkeypatch,
) -> None:
    persisted = []

    async def persist_caller(**values):
        persisted.append(values["caller_text"])
        return 100 + len(persisted)

    monkeypatch.setattr(
        "integrations.telephony.session._STOP_FINAL_DRAIN_TOTAL_SECONDS", 1.0
    )
    stt = FakeDeepgram()
    session = make_session(deepgram=stt, caller_turn_persister=persist_caller)
    session._current_user = await _user(1)
    session._stream_sid = STREAM_SID
    websocket = FakeWebSocket()

    stop_task = asyncio.create_task(session._drain_twilio_stop(websocket))
    for _ in range(10):
        if stt.finalized:
            break
        await asyncio.sleep(0)
    assert stt.finalized == 1

    await session.feed_stt_event(
        ElevenLabsTranscriptEvent(
            text="final VAD en vuelo",
            is_final=True,
            speech_final=True,
        ),
        websocket,
    )
    # Arrive after the retired 750ms quiet window but before the absolute drain
    # deadline.  With no provider correlation ID this final must still count.
    await asyncio.sleep(0.8)
    await session.feed_stt_event(
        ElevenLabsTranscriptEvent(
            text="final del commit manual",
            is_final=True,
            speech_final=True,
            from_finalize=True,
        ),
        websocket,
    )
    await asyncio.wait_for(stop_task, timeout=1.5)

    assert persisted == ["final VAD en vuelo", "final del commit manual"]
    assert session._caller_turns == 2
    assert session._stop_final_utterances.empty()
    assert session._stop_final_admission_closed


@pytest.mark.asyncio
async def test_stop_publishes_terminal_attempt_before_provider_settlement() -> None:
    record_entered = asyncio.Event()
    release_record = asyncio.Event()
    persisted = []

    class Repository(FakeRepository):
        async def record_stream_attempt_result(self, **values):
            result = await super().record_stream_attempt_result(**values)
            record_entered.set()
            await release_record.wait()
            return result

    repository = Repository()

    class Stt(FakeDeepgram):
        async def finalize(self):
            assert repository.stream_attempt_results == [
                {
                    "call_id": "call-session-1",
                    "provider_call_sid": CALL_SID,
                    "stream_attempt": 0,
                    "reason": "twilio_stop",
                    "reconnectable": False,
                    "internal_failure": False,
                }
            ]
            await super().finalize()

    async def persist_caller(**values):
        persisted.append(values["caller_text"])
        return 101

    stt = Stt()
    session = make_session(
        deepgram=stt,
        repository=repository,
        caller_turn_persister=persist_caller,
    )
    session._current_user = await _user(1)
    session._stream_sid = STREAM_SID
    session._utterances.put_nowait(
        FinalPhoneUtterance("captured before outcome write", 1, 1.0, 1.2, 0.9)
    )

    stop_task = asyncio.create_task(session._drain_twilio_stop(FakeWebSocket()))
    await record_entered.wait()

    assert session._twilio_stop_observed is True
    assert session._stop_reason == "twilio_stop"
    assert session._stop_drain_active is True
    assert session._utterances.empty()
    assert stt.finalized == 0
    assert persisted == []

    release_record.set()
    await stop_task

    assert persisted == ["captured before outcome write"]
    assert session._stop_drain_active is False


@pytest.mark.asyncio
async def test_stop_finalize_with_no_scribe_final_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(
        "integrations.telephony.session._STOP_FINAL_DRAIN_TOTAL_SECONDS", 0.03
    )
    stt = FakeDeepgram()
    session = make_session(deepgram=stt)
    session._current_user = await _user(1)
    session._stream_sid = STREAM_SID

    await asyncio.wait_for(
        session._drain_twilio_stop(FakeWebSocket()),
        timeout=0.2,
    )

    assert stt.finalized == 1
    assert session._caller_turns == 0
    assert session._stop_final_utterances.empty()
    assert session._stop_final_admission_closed


@pytest.mark.asyncio
async def test_stop_atomically_drains_all_prequeued_finals_caller_only_once(
    monkeypatch,
) -> None:
    persisted = []

    async def forbidden_runtime(**_values):
        raise AssertionError("stopped-wire final must not start LLM or TTS")

    async def persist_caller(**values):
        persisted.append(values["caller_text"])
        assert values["phone_turn"].context.persistence == "ingest_only"
        return 100 + len(persisted)

    monkeypatch.setattr(
        "integrations.telephony.session._STOP_FINAL_DRAIN_TOTAL_SECONDS", 0.01
    )
    session = make_session(
        runtime_starter=forbidden_runtime,
        caller_turn_persister=persist_caller,
    )
    session._current_user = await _user(1)
    session._stream_sid = STREAM_SID
    session._utterances.put_nowait(
        FinalPhoneUtterance("first queued", 1, 1.0, 1.2, 0.9)
    )
    session._utterances.put_nowait(
        FinalPhoneUtterance("second queued", 1, 2.0, 2.2, 0.9)
    )

    await session._drain_twilio_stop(FakeWebSocket())

    assert persisted == ["first queued", "second queued"]
    assert session._caller_turns == 2
    assert session._utterances.empty()
    assert session._stop_final_utterances.empty()


def test_stop_persistence_budget_scales_with_actual_batch(monkeypatch) -> None:
    monkeypatch.setattr(
        "integrations.telephony.session._STOP_PERSIST_TIMEOUT_SECONDS", 2.0
    )
    monkeypatch.setattr(
        "integrations.telephony.session._OWNED_TASK_CANCEL_GRACE_SECONDS", 0.25
    )

    assert session_module._stop_persistence_budget_seconds(0) == 0.0
    assert session_module._stop_persistence_budget_seconds(1) == 2.25
    assert session_module._stop_persistence_budget_seconds(5) == 11.25


@pytest.mark.asyncio
async def test_stop_persists_prequeued_turn_and_two_scribe_commits_with_full_windows(
    monkeypatch,
) -> None:
    persisted = []

    async def persist_caller(**values):
        # Exercise actual asynchronous persistence without a scheduler-tight
        # threshold; the adjacent budget test independently proves the
        # production batch reserves one complete window per admitted item.
        await asyncio.sleep(0.1)
        persisted.append(values["caller_text"])
        return 100 + len(persisted)

    monkeypatch.setattr(
        "integrations.telephony.session._STOP_FINAL_DRAIN_TOTAL_SECONDS", 0.05
    )
    monkeypatch.setattr(
        "integrations.telephony.session._STOP_PERSIST_TIMEOUT_SECONDS", 0.5
    )
    stt = FakeDeepgram()
    session = make_session(
        deepgram=stt,
        caller_turn_persister=persist_caller,
    )
    session._current_user = await _user(1)
    session._stream_sid = STREAM_SID
    session._utterances.put_nowait(
        FinalPhoneUtterance("already queued", 1, 1.0, 1.2, 0.9)
    )
    websocket = FakeWebSocket()

    stop_task = asyncio.create_task(session._drain_twilio_stop(websocket))
    for _ in range(10):
        if stt.finalized:
            break
        await asyncio.sleep(0)
    assert stt.finalized == 1
    await session.feed_stt_event(
        ElevenLabsTranscriptEvent(
            text="final VAD en vuelo",
            is_final=True,
            speech_final=True,
        ),
        websocket,
    )
    await asyncio.sleep(0.02)
    await session.feed_stt_event(
        ElevenLabsTranscriptEvent(
            text="final del commit manual",
            is_final=True,
            speech_final=True,
            from_finalize=True,
        ),
        websocket,
    )

    await asyncio.wait_for(stop_task, timeout=1.0)

    assert persisted == [
        "already queued",
        "final VAD en vuelo",
        "final del commit manual",
    ]
    assert session._caller_turns == 3
    assert session._utterances.empty()
    assert session._stop_final_utterances.empty()
    assert all(
        not task.get_name().startswith("phone-stop-persist-")
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_stop_dynamic_batch_persists_more_than_three_delayed_finals(
    monkeypatch,
) -> None:
    persisted = []

    async def persist_caller(**values):
        # Keep the write genuinely asynchronous while leaving a generous,
        # deterministic margin from the per-item timeout.
        await asyncio.sleep(0.01)
        persisted.append(values["caller_text"])
        return 100 + len(persisted)

    monkeypatch.setattr(
        "integrations.telephony.session._STOP_FINAL_DRAIN_TOTAL_SECONDS", 0.005
    )
    monkeypatch.setattr(
        "integrations.telephony.session._STOP_PERSIST_TIMEOUT_SECONDS", 0.25
    )
    session = make_session(caller_turn_persister=persist_caller)
    session._current_user = await _user(1)
    session._stream_sid = STREAM_SID
    expected = [f"delayed final {index}" for index in range(1, 6)]
    for index, text in enumerate(expected, start=1):
        session._utterances.put_nowait(
            FinalPhoneUtterance(
                text,
                index,
                float(index),
                float(index) + 0.2,
                0.9,
            )
        )

    await asyncio.wait_for(
        session._drain_twilio_stop(FakeWebSocket()),
        timeout=1.0,
    )

    assert persisted == expected
    assert session._caller_turns == len(expected)
    assert session._utterances.empty()
    assert session._stop_final_utterances.empty()
    _assert_no_owned_phone_tasks()


@pytest.mark.asyncio
async def test_stop_shared_budget_cancels_every_started_write_at_exhaustion(
    monkeypatch,
) -> None:
    started = []
    canceled = []

    async def persist_caller(**values):
        text = values["caller_text"]
        started.append(text)
        try:
            await asyncio.Event().wait()
        finally:
            canceled.append(text)

    monkeypatch.setattr(
        "integrations.telephony.session._STOP_FINAL_DRAIN_TOTAL_SECONDS", 0.001
    )
    monkeypatch.setattr(
        "integrations.telephony.session._STOP_PERSIST_TIMEOUT_SECONDS", 0.03
    )
    session = make_session(caller_turn_persister=persist_caller)
    session._current_user = await _user(1)
    session._stream_sid = STREAM_SID
    for index in range(3):
        session._utterances.put_nowait(
            FinalPhoneUtterance(
                f"queued {index + 1}",
                1,
                float(index),
                float(index) + 0.2,
                0.9,
            )
        )

    with pytest.raises(PhoneMediaSessionError, match="shutdown budget"):
        await asyncio.wait_for(
            session._drain_twilio_stop(FakeWebSocket()),
            timeout=0.3,
        )

    assert started == ["queued 1", "queued 2", "queued 3"]
    assert canceled == started
    assert all(
        not task.get_name().startswith("phone-stop-persist-")
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_cancelled_stop_cancels_and_joins_owned_canonical_persistence(
    monkeypatch,
) -> None:
    entered = asyncio.Event()
    canceled = asyncio.Event()
    started = []

    async def persist_caller(**values):
        started.append(values["caller_text"])
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    monkeypatch.setattr(
        "integrations.telephony.session._STOP_FINAL_DRAIN_TOTAL_SECONDS", 0.01
    )
    session = make_session(caller_turn_persister=persist_caller)
    session._current_user = await _user(1)
    session._stream_sid = STREAM_SID
    session._utterances.put_nowait(
        FinalPhoneUtterance("durable final", 1, 1.0, 1.2, 0.9)
    )
    session._utterances.put_nowait(
        FinalPhoneUtterance("must not start", 1, 2.0, 2.2, 0.9)
    )
    drain = asyncio.create_task(session._drain_twilio_stop(FakeWebSocket()))
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    drain.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(drain, timeout=1.0)

    assert canceled.is_set()
    assert started == ["durable final"]
    assert session._caller_turns == 0
    assert session._stopping.is_set()


@pytest.mark.asyncio
async def test_stop_persistence_timeout_cancels_child_and_finishes_shutdown(
    monkeypatch,
) -> None:
    entered = asyncio.Event()
    canceled = asyncio.Event()

    async def persist_caller(**_values):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    monkeypatch.setattr(
        "integrations.telephony.session._STOP_FINAL_DRAIN_TOTAL_SECONDS", 0.01
    )
    monkeypatch.setattr(
        "integrations.telephony.session._STOP_PERSIST_TIMEOUT_SECONDS", 0.02
    )
    monkeypatch.setattr(
        "integrations.telephony.session._OWNED_TASK_CANCEL_GRACE_SECONDS", 0.1
    )
    session = make_session(caller_turn_persister=persist_caller)
    session._current_user = await _user(1)
    session._stream_sid = STREAM_SID
    session._utterances.put_nowait(
        FinalPhoneUtterance("bounded final", 1, 1.0, 1.2, 0.9)
    )

    with pytest.raises(
        PhoneMediaSessionError,
        match="persistence exceeded its shutdown budget",
    ):
        await asyncio.wait_for(
            session._drain_twilio_stop(FakeWebSocket()), timeout=0.5
        )

    assert entered.is_set()
    assert canceled.is_set()
    assert session._caller_turns == 0
    assert session._stopping.is_set()


@pytest.mark.asyncio
async def test_stop_total_budget_cancels_hung_deepgram_finalize(monkeypatch) -> None:
    canceled = asyncio.Event()

    class Deepgram(FakeDeepgram):
        async def finalize(self):
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

    monkeypatch.setattr(
        "integrations.telephony.session._STOP_SETTLEMENT_TIMEOUT_SECONDS", 0.02
    )
    session = make_session(deepgram=Deepgram())
    session._current_user = await _user(1)
    session._stream_sid = STREAM_SID

    with pytest.raises(PhoneMediaSessionError, match="total shutdown budget"):
        await asyncio.wait_for(
            session._drain_twilio_stop(FakeWebSocket()), timeout=0.5
        )

    assert canceled.is_set()
    assert session._stop_final_admission_closed
    assert session._stopping.is_set()


@pytest.mark.asyncio
async def test_stop_total_budget_cancels_hung_playback_disconnect(monkeypatch) -> None:
    canceled = asyncio.Event()

    class Playback:
        async def disconnect(self):
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

    monkeypatch.setattr(
        "integrations.telephony.session._STOP_SETTLEMENT_TIMEOUT_SECONDS", 0.02
    )
    session = make_session()
    session._current_user = await _user(1)
    session._stream_sid = STREAM_SID
    session._active_playback = Playback()

    with pytest.raises(PhoneMediaSessionError, match="total shutdown budget"):
        await asyncio.wait_for(
            session._drain_twilio_stop(FakeWebSocket()), timeout=0.5
        )

    assert canceled.is_set()
    assert session._active_playback is None
    assert session._stop_final_admission_closed
    assert session._stopping.is_set()


@pytest.mark.asyncio
async def test_run_keeps_stop_receive_task_alive_until_caller_only_persistence(
    monkeypatch,
) -> None:
    persisted = []

    class StopWebSocket(FakeWebSocket):
        async def receive_text(self):
            return json.dumps(
                {
                    "event": "stop",
                    "sequenceNumber": "2",
                    "streamSid": STREAM_SID,
                    "stop": {"accountSid": ACCOUNT_SID, "callSid": CALL_SID},
                }
            )

    class Deepgram(FakeDeepgram):
        async def events(self):
            await asyncio.Event().wait()
            if False:
                yield None

    class BlockingGreeting(ImmediateCachedPlayback):
        def __init__(self):
            super().__init__()
            self.finished = asyncio.Event()

        async def run(self, **_values):
            await self.finished.wait()

        async def disconnect(self):
            self.finished.set()

    async def greeting(_context):
        return BlockingGreeting()

    async def persist_caller(**values):
        await asyncio.sleep(0.02)
        persisted.append(values["caller_text"])
        return 101

    monkeypatch.setattr(
        "integrations.telephony.session._STOP_FINAL_DRAIN_TOTAL_SECONDS", 0.01
    )
    session = PhoneMediaSession(
        context(),
        deepgram=Deepgram(),
        repository=FakeRepository(),
        current_user_loader=_user,
        hangup_call=_noop,
        notice_loader=_noop_notice_loader,
        greeting_loader=greeting,
        caller_turn_persister=persist_caller,
    )
    session._utterances.put_nowait(
        FinalPhoneUtterance("persist before stop completes", 1, 1.0, 1.2, 0.9)
    )
    connected, start, _ = media_messages()

    result = await session.run(
        StopWebSocket(), initial_messages=(connected, start)
    )

    assert result.reason == "twilio_stop"
    assert persisted == ["persist before stop completes"]
    assert session._caller_turns == 1


class _StopWebSocket(FakeWebSocket):
    async def receive_text(self):
        return json.dumps(
            {
                "event": "stop",
                "sequenceNumber": "2",
                "streamSid": STREAM_SID,
                "stop": {"accountSid": ACCOUNT_SID, "callSid": CALL_SID},
            }
        )


@pytest.mark.asyncio
async def test_run_keeps_published_stop_when_provider_settlement_fails() -> None:
    actions = []

    class Stt(FakeDeepgram):
        async def events(self):
            await asyncio.Event().wait()
            if False:
                yield None

        async def finalize(self):
            raise RuntimeError("provider finalization failed")

    async def notice(_context, kind):
        return ImmediateCachedPlayback(
            lambda: actions.append(("notice", kind))
        )

    async def hangup(_call_sid):
        actions.append(("hangup",))
        return True

    repository = FakeRepository()
    session = PhoneMediaSession(
        context(),
        deepgram=Stt(),
        repository=repository,
        current_user_loader=_user,
        hangup_call=hangup,
        notice_loader=notice,
        greeting_loader=lambda _context: _immediate_playback(),
        caller_turn_persister=_noop_caller_turn_persister,
    )
    connected, start, _ = media_messages()

    result = await session.run(
        _StopWebSocket(), initial_messages=(connected, start)
    )

    assert result.reason == "twilio_stop"
    assert result.reconnectable is False
    assert result.internal_failure is False
    assert result.attempt_result_published is True
    assert actions == []
    assert session._fatal_cleanup_started is False
    assert len(repository.stream_attempt_results) == 1
    duplicate = await repository.record_stream_attempt_result(
        call_id=session.context.call_id,
        provider_call_sid=session.context.provider_call_sid,
        stream_attempt=session.context.stream_attempt,
        reason=result.reason,
        reconnectable=result.reconnectable,
        internal_failure=result.internal_failure,
    )
    assert duplicate is False
    assert len(repository.stream_attempt_results) == 1


@pytest.mark.asyncio
async def test_post_stop_sibling_failure_preserves_receiver_settlement() -> None:
    outcome_published = asyncio.Event()
    sibling_failed = asyncio.Event()
    finalize_entered = asyncio.Event()
    release_finalize = asyncio.Event()
    finalize_canceled = asyncio.Event()
    persisted = []

    class Repository(FakeRepository):
        async def record_stream_attempt_result(self, **values):
            result = await super().record_stream_attempt_result(**values)
            outcome_published.set()
            return result

    class Stt(FakeDeepgram):
        def __init__(self):
            super().__init__()
            self.final_events = asyncio.Queue()
            self.final_consumed = asyncio.Event()

        async def events(self):
            while True:
                event = await self.final_events.get()
                yield event
                self.final_consumed.set()

        async def finalize(self):
            finalize_entered.set()
            try:
                await release_finalize.wait()
                await self.final_events.put(
                    ElevenLabsTranscriptEvent(
                        text="must survive sibling failure",
                        is_final=True,
                        speech_final=True,
                        from_finalize=True,
                    )
                )
                await self.final_consumed.wait()
            except asyncio.CancelledError:
                finalize_canceled.set()
                raise

    class Greeting(ImmediateCachedPlayback):
        async def run(self, **_values):
            await outcome_published.wait()
            sibling_failed.set()
            raise RuntimeError("concurrent greeting failure")

    async def persist_caller(**values):
        persisted.append(values["caller_text"])
        return 101

    repository = Repository()
    session = PhoneMediaSession(
        context(),
        deepgram=Stt(),
        repository=repository,
        current_user_loader=_user,
        hangup_call=_noop,
        notice_loader=_noop_notice_loader,
        greeting_loader=lambda _context: asyncio.sleep(0, result=Greeting()),
        caller_turn_persister=persist_caller,
    )
    connected, start, _ = media_messages()

    run_task = asyncio.create_task(
        session.run(_StopWebSocket(), initial_messages=(connected, start))
    )
    await finalize_entered.wait()
    await sibling_failed.wait()
    for _ in range(5):
        await asyncio.sleep(0)

    assert run_task.done() is False
    assert finalize_canceled.is_set() is False
    assert persisted == []

    release_finalize.set()
    result = await run_task

    assert result.reason == "twilio_stop"
    assert persisted == ["must survive sibling failure"]
    assert finalize_canceled.is_set() is False
    _assert_no_owned_phone_tasks()


def _assert_no_owned_phone_tasks() -> None:
    leaked = [
        task.get_name()
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and task.get_name().startswith("phone-")
    ]
    assert leaked == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hung_dependency",
    ["deepgram_finalize", "greeting_disconnect", "canonical_persistence"],
)
async def test_run_stop_budget_cancels_hung_settlement_and_all_owned_tasks(
    monkeypatch,
    hung_dependency,
) -> None:
    canceled = asyncio.Event()

    class Deepgram(FakeDeepgram):
        async def events(self):
            await asyncio.Event().wait()
            if False:
                yield None

        async def finalize(self):
            if hung_dependency != "deepgram_finalize":
                return await super().finalize()
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

    class Greeting(ImmediateCachedPlayback):
        async def run(self, **_values):
            if hung_dependency == "greeting_disconnect":
                await asyncio.Event().wait()

        async def disconnect(self):
            if hung_dependency != "greeting_disconnect":
                return
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

    async def persist_caller(**_values):
        if hung_dependency != "canonical_persistence":
            return 101
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    monkeypatch.setattr(
        "integrations.telephony.session._STOP_FINAL_DRAIN_TOTAL_SECONDS", 0.005
    )
    monkeypatch.setattr(
        "integrations.telephony.session._STOP_SETTLEMENT_TIMEOUT_SECONDS", 0.02
    )
    monkeypatch.setattr(
        "integrations.telephony.session._STOP_PERSIST_TIMEOUT_SECONDS", 0.02
    )
    monkeypatch.setattr(
        "integrations.telephony.session._STOP_POST_SETTLEMENT_CLEANUP_SECONDS", 0.05
    )
    session = PhoneMediaSession(
        context(),
        deepgram=Deepgram(),
        repository=FakeRepository(),
        current_user_loader=_user,
        hangup_call=_noop,
        notice_loader=_noop_notice_loader,
        greeting_loader=lambda _context: asyncio.sleep(0, result=Greeting()),
        caller_turn_persister=persist_caller,
    )
    if hung_dependency == "canonical_persistence":
        session._utterances.put_nowait(
            FinalPhoneUtterance("bounded final", 1, 1.0, 1.2, 0.9)
        )
    connected, start, _ = media_messages()

    result = await asyncio.wait_for(
        session.run(_StopWebSocket(), initial_messages=(connected, start)),
        timeout=0.5,
    )

    assert result.reason == "twilio_stop"
    assert canceled.is_set()
    assert session._stopping.is_set()
    _assert_no_owned_phone_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("hung_dependency", ["deepgram_close", "recording_finalize"])
async def test_run_stop_budget_bounds_final_cleanup_dependencies(
    monkeypatch,
    hung_dependency,
) -> None:
    async_canceled = asyncio.Event()
    recorder_entered = asyncio.Event()

    class Deepgram(FakeDeepgram):
        async def events(self):
            await asyncio.Event().wait()
            if False:
                yield None

        async def close(self):
            if hung_dependency != "deepgram_close":
                return
            try:
                await asyncio.Event().wait()
            finally:
                async_canceled.set()

    class Recorder:
        def finalize_raw(self):
            return LocalRecordingAsset(
                participant_path=Path("participant.mulaw"),
                assistant_path=None,
                mixed_path=None,
                duration_ms=20.0,
            )

        async def finalize_async(self):
            recorder_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                async_canceled.set()

    monkeypatch.setattr(
        "integrations.telephony.session._STOP_FINAL_DRAIN_TOTAL_SECONDS", 0.005
    )
    monkeypatch.setattr(
        "integrations.telephony.session._STOP_SETTLEMENT_TIMEOUT_SECONDS", 0.02
    )
    monkeypatch.setattr(
        "integrations.telephony.session._STOP_POST_SETTLEMENT_CLEANUP_SECONDS", 0.04
    )
    session_context = context()
    recorder = None
    repository = FakeRepository()
    if hung_dependency == "recording_finalize":
        session_context = replace(session_context, recording_enabled=True)
        recorder = Recorder()

        async def persist_local_recording(**_values):
            return 1

        repository.persist_local_recording = persist_local_recording
    session = PhoneMediaSession(
        session_context,
        deepgram=Deepgram(),
        repository=repository,
        current_user_loader=_user,
        hangup_call=_noop,
        notice_loader=_noop_notice_loader,
        greeting_loader=lambda _context: asyncio.sleep(
            0, result=ImmediateCachedPlayback()
        ),
        caller_turn_persister=_noop_caller_turn_persister,
        recorder=recorder,  # type: ignore[arg-type]
    )
    connected, start, _ = media_messages()
    result = await asyncio.wait_for(
        session.run(_StopWebSocket(), initial_messages=(connected, start)),
        timeout=0.5,
    )

    assert result.reason == "twilio_stop"
    if hung_dependency == "deepgram_close":
        assert async_canceled.is_set()
    else:
        assert recorder_entered.is_set()
        assert async_canceled.is_set()
    _assert_no_owned_phone_tasks()


@pytest.mark.asyncio
async def test_zero_residual_stop_budget_persists_raw_without_starting_mix() -> None:
    persisted = []
    mix_started = 0

    class Recorder:
        def finalize_raw(self):
            return LocalRecordingAsset(
                participant_path=Path("private/participant.mulaw"),
                assistant_path=Path("private/assistant.mulaw"),
                mixed_path=None,
                duration_ms=61_000.0,
            )

        async def finalize_async(self):
            nonlocal mix_started
            mix_started += 1
            raise AssertionError("zero residual budget must not start ffmpeg")

    repository = FakeRepository()

    async def persist_local_recording(**values):
        persisted.append(values)
        return 1

    repository.persist_local_recording = persist_local_recording
    session = PhoneMediaSession(
        replace(context(), recording_enabled=True),
        deepgram=FakeDeepgram(),
        repository=repository,
        current_user_loader=_user,
        hangup_call=_noop,
        notice_loader=_noop_notice_loader,
        caller_turn_persister=_noop_caller_turn_persister,
        recorder=Recorder(),  # type: ignore[arg-type]
    )
    session._shutdown_deadline_loop_time = asyncio.get_running_loop().time()

    await session._finalize_session(())

    assert persisted == [
        {
            "call_id": "call-session-1",
            "participant_path": str(Path("private/participant.mulaw")),
            "assistant_path": str(Path("private/assistant.mulaw")),
            "mixed_path": None,
            "duration_seconds": 61,
            "mix_error": "mixed_audio_pending",
        }
    ]
    assert mix_started == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("hung_dependency", ["technical_notice", "hangup"])
async def test_run_stop_timeout_skips_fatal_notice_and_hangup(
    monkeypatch,
    hung_dependency,
) -> None:
    canceled = asyncio.Event()

    class Deepgram(FakeDeepgram):
        async def events(self):
            await asyncio.Event().wait()
            if False:
                yield None

        async def finalize(self):
            await asyncio.Event().wait()

    class Notice(ImmediateCachedPlayback):
        async def run(self, **_values):
            if hung_dependency != "technical_notice":
                return
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

    async def notice_loader(_context, _kind):
        return Notice()

    async def hangup(_call_sid):
        if hung_dependency != "hangup":
            return True
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    monkeypatch.setattr(
        "integrations.telephony.session._STOP_SETTLEMENT_TIMEOUT_SECONDS", 0.02
    )
    monkeypatch.setattr(
        "integrations.telephony.session._STOP_POST_SETTLEMENT_CLEANUP_SECONDS", 0.04
    )
    monkeypatch.setattr(
        "integrations.telephony.session._SESSION_DURABLE_HANGUP_TIMEOUT_SECONDS",
        0.04,
    )
    monkeypatch.setattr(
        "integrations.telephony.session._SESSION_PROVIDER_CLOSE_TIMEOUT_SECONDS",
        0.04,
    )
    session = PhoneMediaSession(
        context(),
        deepgram=Deepgram(),
        repository=FakeRepository(),
        current_user_loader=_user,
        hangup_call=hangup,
        notice_loader=notice_loader,
        greeting_loader=lambda _context: asyncio.sleep(
            0, result=ImmediateCachedPlayback()
        ),
        caller_turn_persister=_noop_caller_turn_persister,
    )
    connected, start, _ = media_messages()

    result = await asyncio.wait_for(
        session.run(_StopWebSocket(), initial_messages=(connected, start)),
        timeout=0.5,
    )

    assert result.reason == "twilio_stop"
    # Twilio Stop is already the immutable terminal wire outcome. Neither a
    # failure notice nor a REST hangup is started after settlement times out.
    assert canceled.is_set() is False
    _assert_no_owned_phone_tasks()


@pytest.mark.asyncio
async def test_milestone_is_persisted_only_after_runtime_receives_context(
    monkeypatch,
) -> None:
    captured_contexts = []

    class Runtime:
        pass

    async def starter(**values):
        captured_contexts.append(
            values["phone_turn"].context.provenance["internal_turn_context"]
        )
        return Runtime()

    class Playback:
        def __init__(self, **_values):
            pass

        async def run(self):
            return type("Result", (), {"interrupted": True})()

    repository = FakeRepository()
    monkeypatch.setattr("integrations.telephony.session.PhoneTurnPlayback", Playback)
    session = PhoneMediaSession(
        context(
            maximum=1_200,
            started_at=datetime.now(UTC) - timedelta(seconds=301),
        ),
        deepgram=FakeDeepgram(),
        repository=repository,
        current_user_loader=_user,
        hangup_call=_noop,
        notice_loader=_noop_notice_loader,
        runtime_starter=starter,
    )
    session._current_user = await _user(1)
    session._stream_sid = STREAM_SID

    await session._run_turn(
        FinalPhoneUtterance("first", 1, 1.0, 1.5, 0.9), FakeWebSocket()
    )
    await session._run_turn(
        FinalPhoneUtterance("second", 2, 2.0, 2.5, 0.9), FakeWebSocket()
    )

    assert "new_milestones_seconds=900" in captured_contexts[0]
    assert "new_milestones_seconds" not in captured_contexts[1]
    assert len(repository.milestone_records) == 1
    assert repository.milestone_records[0]["milestones_seconds"] == (900,)


class BlockingDeepgram(FakeDeepgram):
    async def events(self):
        await asyncio.Event().wait()
        if False:
            yield None


class BlockingWebSocket(FakeWebSocket):
    async def receive_text(self):
        await asyncio.Event().wait()


class ClosingWebSocket(FakeWebSocket):
    async def receive_text(self):
        raise ConnectionError("provider websocket closed")


@pytest.mark.asyncio
async def test_only_twilio_websocket_disconnect_is_reconnectable() -> None:
    repository = FakeRepository()
    repository.delivered_milestones = (900,)
    session = PhoneMediaSession(
        context(maximum=1_200, started_at=datetime.now(UTC) - timedelta(seconds=301)),
        deepgram=BlockingDeepgram(),
        repository=repository,
        current_user_loader=_user,
        hangup_call=_noop,
        notice_loader=_noop_notice_loader,
        greeting_loader=lambda _context: _immediate_playback(),
    )
    connected, start, _ = media_messages()

    result = await session.run(
        ClosingWebSocket(), initial_messages=(connected, start)
    )

    assert result.reason == "websocket_closed"
    assert result.reconnectable is True
    assert result.internal_failure is True
    assert result.attempt_result_published is True
    assert len(repository.stream_attempt_results) == 1
    assert repository.stream_attempt_results[0]["reconnectable"] is True
    assert session.clock.fired_milestones_seconds == (900,)


@pytest.mark.asyncio
async def test_final_stream_attempt_disconnect_returns_to_connect_action_without_rest_hangup(
) -> None:
    hangups = []

    async def hangup(call_sid):
        hangups.append(call_sid)
        return True

    session = PhoneMediaSession(
        context(stream_attempt=2),
        deepgram=BlockingDeepgram(),
        repository=FakeRepository(),
        current_user_loader=_user,
        hangup_call=hangup,
        notice_loader=_noop_notice_loader,
        caller_turn_persister=_noop_caller_turn_persister,
    )
    connected, start, _ = media_messages(stream_attempt=2)

    result = await session.run(
        ClosingWebSocket(), initial_messages=(connected, start)
    )

    assert result.reason == "websocket_closed"
    assert result.reconnectable is False
    assert result.internal_failure is True
    assert result.attempt_result_published is True
    assert len(session.repository.stream_attempt_results) == 1
    assert session.repository.stream_attempt_results[0]["reconnectable"] is False
    assert hangups == []


@pytest.mark.asyncio
@pytest.mark.parametrize("hangup_latch", ["accepted", "confirmed"])
async def test_disconnect_publishes_non_reconnectable_after_durable_hangup(
    hangup_latch,
) -> None:
    repository = FakeRepository()
    session = PhoneMediaSession(
        context(),
        deepgram=BlockingDeepgram(),
        repository=repository,
        current_user_loader=_user,
        hangup_call=_noop,
        notice_loader=_noop_notice_loader,
        greeting_loader=lambda _context: _immediate_playback(),
    )
    if hangup_latch == "accepted":
        session._hangup_accepted = True
    else:
        session._hangup_confirmed = True
    connected, start, _ = media_messages()

    result = await session.run(
        ClosingWebSocket(), initial_messages=(connected, start)
    )

    assert result.reason == "websocket_closed"
    assert result.reconnectable is False
    assert result.internal_failure is True
    assert result.attempt_result_published is True
    assert repository.stream_attempt_results == [
        {
            "call_id": "call-session-1",
            "provider_call_sid": CALL_SID,
            "stream_attempt": 0,
            "reason": "websocket_closed",
            "reconnectable": False,
            "internal_failure": True,
        }
    ]


@pytest.mark.asyncio
async def test_bootstrap_failure_result_has_no_published_attempt_outcome() -> None:
    class Stt(FakeDeepgram):
        async def connect(self):
            raise RuntimeError("bootstrap failed")

    session = PhoneMediaSession(
        context(),
        deepgram=Stt(),
        repository=FakeRepository(),
        current_user_loader=_user,
        hangup_call=_noop,
        notice_loader=_noop_notice_loader,
        greeting_loader=lambda _context: _immediate_playback(),
    )
    connected, start, _ = media_messages()

    result = await session.run(
        BlockingWebSocket(), initial_messages=(connected, start)
    )

    assert result.attempt_result_published is False


@pytest.mark.asyncio
async def test_disconnect_outcome_is_durable_before_blocked_runtime_cleanup(
    monkeypatch,
) -> None:
    disconnect = asyncio.Event()
    starter_entered = asyncio.Event()
    starter_canceled = asyncio.Event()
    persisted = []
    repository = FakeRepository()

    class WebSocket(FakeWebSocket):
        async def receive_text(self):
            await disconnect.wait()
            raise ConnectionError("provider websocket closed")

    async def starter(**_values):
        starter_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            starter_canceled.set()

    async def persist_caller(**values):
        phone_turn = values["phone_turn"]
        persisted.append(
            (
                values["caller_text"],
                phone_turn.context.persistence,
                phone_turn.context.turn_key.turn_id,
            )
        )
        return 101

    monkeypatch.setattr(
        "integrations.telephony.session._OWNED_TASK_CANCEL_GRACE_SECONDS", 0.05
    )

    session = PhoneMediaSession(
        context(),
        deepgram=BlockingDeepgram(),
        repository=repository,
        current_user_loader=_user,
        hangup_call=_noop,
        notice_loader=_noop_notice_loader,
        greeting_loader=lambda _context: _immediate_playback(),
        runtime_starter=starter,
        caller_turn_persister=persist_caller,
    )
    session._utterances.put_nowait(
        FinalPhoneUtterance("accepted before disconnect", 1, 1.0, 1.2, 0.9)
    )
    connected, start, _ = media_messages()
    run_task = asyncio.create_task(
        session.run(WebSocket(), initial_messages=(connected, start))
    )
    await asyncio.wait_for(starter_entered.wait(), timeout=1.0)

    disconnect.set()
    for _ in range(50):
        outcome = await repository.get_stream_attempt_result(
            call_id="call-session-1", stream_attempt=0
        )
        if outcome is not None:
            break
        await asyncio.sleep(0)

    assert outcome == {
        "stream_attempt": 0,
        "reason": "websocket_closed",
        "reconnectable": True,
        "internal_failure": True,
    }
    assert run_task.done() is False

    result = await asyncio.wait_for(run_task, timeout=1.0)
    assert result.reconnectable is True
    assert result.attempt_result_published is True
    assert len(repository.stream_attempt_results) == 1
    assert starter_canceled.is_set()
    assert persisted == [
        ("accepted before disconnect", "ingest_only", "stt-1")
    ]
    assert session._caller_turns == 1
    _assert_no_owned_phone_tasks()


@pytest.mark.asyncio
async def test_stop_during_runtime_start_persists_active_and_queued_callers(
    monkeypatch,
) -> None:
    starter_entered = asyncio.Event()
    starter_canceled = asyncio.Event()
    persisted = []

    class WebSocket(FakeWebSocket):
        async def receive_text(self):
            await starter_entered.wait()
            return json.dumps(
                {
                    "event": "stop",
                    "sequenceNumber": "2",
                    "streamSid": STREAM_SID,
                    "stop": {"accountSid": ACCOUNT_SID, "callSid": CALL_SID},
                }
            )

    async def starter(**_values):
        starter_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            starter_canceled.set()

    async def persist_caller(**values):
        phone_turn = values["phone_turn"]
        persisted.append(
            (
                values["caller_text"],
                phone_turn.context.persistence,
                phone_turn.context.turn_key.turn_id,
            )
        )
        return 101

    monkeypatch.setattr(
        "integrations.telephony.session._STOP_FINAL_DRAIN_TOTAL_SECONDS", 0.005
    )
    monkeypatch.setattr(
        "integrations.telephony.session._OWNED_TASK_CANCEL_GRACE_SECONDS", 0.01
    )
    session = PhoneMediaSession(
        context(),
        deepgram=BlockingDeepgram(),
        repository=FakeRepository(),
        current_user_loader=_user,
        hangup_call=_noop,
        notice_loader=_noop_notice_loader,
        greeting_loader=lambda _context: _immediate_playback(),
        runtime_starter=starter,
        caller_turn_persister=persist_caller,
    )
    session._utterances.put_nowait(
        FinalPhoneUtterance("accepted before stop", 1, 1.0, 1.2, 0.9)
    )
    session._utterances.put_nowait(
        FinalPhoneUtterance("queued behind starter", 2, 2.0, 2.2, 0.9)
    )
    connected, start, _ = media_messages()

    result = await asyncio.wait_for(
        session.run(WebSocket(), initial_messages=(connected, start)),
        timeout=1.0,
    )

    assert result.reason == "twilio_stop"
    assert starter_canceled.is_set()
    assert persisted == [
        ("accepted before stop", "ingest_only", "stt-1"),
        ("queued behind starter", "ingest_only", "stt-2"),
    ]
    assert session._caller_turns == 2
    _assert_no_owned_phone_tasks()


@pytest.mark.asyncio
async def test_canceled_runtime_starter_persists_committed_caller_before_cleanup(
    monkeypatch,
) -> None:
    persisted = []

    async def starter(**_values):
        raise asyncio.CancelledError

    async def persist_caller(**values):
        phone_turn = values["phone_turn"]
        persisted.append(
            (
                values["caller_text"],
                phone_turn.context.persistence,
                phone_turn.context.turn_key.turn_id,
            )
        )
        return 101

    monkeypatch.setattr(
        "integrations.telephony.session._OWNED_TASK_CANCEL_GRACE_SECONDS", 0.01
    )
    session = PhoneMediaSession(
        context(),
        deepgram=BlockingDeepgram(),
        repository=FakeRepository(),
        current_user_loader=_user,
        hangup_call=_noop,
        notice_loader=_noop_notice_loader,
        greeting_loader=lambda _context: _immediate_playback(),
        runtime_starter=starter,
        caller_turn_persister=persist_caller,
    )
    session._utterances.put_nowait(
        FinalPhoneUtterance("accepted before starter cancellation", 1, 1.0, 1.2, 0.9)
    )
    connected, start, _ = media_messages()

    result = await asyncio.wait_for(
        session.run(BlockingWebSocket(), initial_messages=(connected, start)),
        timeout=1.0,
    )

    assert result.reason == "error"
    assert persisted == [
        ("accepted before starter cancellation", "ingest_only", "stt-1")
    ]
    assert session._caller_turns == 1
    _assert_no_owned_phone_tasks()


@pytest.mark.asyncio
async def test_stop_settlement_marks_returned_runtime_caller_only_interrupted() -> None:
    state = PhoneTurnLinkState()
    interruptions = []

    class Runtime:
        async def interrupt(self, text, *, played_ms, reason):
            assert state.interrupted is True
            interruptions.append((text, played_ms, reason))
            return (101, None)

    async def start_runtime():
        return Runtime()

    session = make_session()
    session._ensure_shutdown_deadline(1.0)
    runtime_start = asyncio.create_task(start_runtime())
    await runtime_start
    phone_turn = type("PhoneTurn", (), {"link_state": state})()

    await session._settle_runtime_start_after_shutdown(
        runtime_start,
        FinalPhoneUtterance("accepted caller", 1, 1.0, 1.2, 0.9),
        phone_turn,
    )

    assert interruptions == [
        ("", 0, "phone_session_closed_during_start")
    ]
    assert session._caller_turns == 1
    assert session._active_runtime is None
    assert session._active_phone_turn is None


@pytest.mark.asyncio
async def test_simultaneous_websocket_and_stt_failure_is_terminal_not_reconnectable(
    monkeypatch,
) -> None:
    release = asyncio.Event()
    actions = []

    class WebSocket(FakeWebSocket):
        async def receive_text(self):
            await release.wait()
            raise ConnectionError("provider websocket closed")

    class Deepgram(FakeDeepgram):
        async def connect(self):
            asyncio.get_running_loop().call_soon(release.set)
            return await super().connect()

        async def events(self):
            await release.wait()
            raise RuntimeError("stt failed")
            if False:
                yield None

    async def return_receive_before_already_done_stt(tasks, *, return_when):
        assert return_when is asyncio.FIRST_COMPLETED
        receive = next(task for task in tasks if task.get_name() == "phone-twilio-receive")
        stt = next(task for task in tasks if task.get_name() == "phone-stt-events")
        await asyncio.gather(receive, stt, return_exceptions=True)
        # Model the exact race: FIRST_COMPLETED reports the websocket task,
        # while the STT sibling has also failed before the session can fence it.
        return {receive}, set(tasks) - {receive}

    monkeypatch.setattr(session_module.asyncio, "wait", return_receive_before_already_done_stt)

    async def notice(_context, kind):
        return ImmediateCachedPlayback(lambda: actions.append(("notice", kind)))

    async def hangup(_call_sid):
        actions.append(("hangup",))
        return True

    session = PhoneMediaSession(
        context(),
        deepgram=Deepgram(),
        repository=(repository := FakeRepository()),
        current_user_loader=_user,
        hangup_call=hangup,
        notice_loader=notice,
        greeting_loader=lambda _context: _immediate_playback(),
        caller_turn_persister=_noop_caller_turn_persister,
    )
    connected, start, _ = media_messages()

    result = await session.run(WebSocket(), initial_messages=(connected, start))

    assert result.reconnectable is False
    assert result.internal_failure is False
    assert repository.stream_attempt_results == []
    # The Media Streams receiver has already disconnected, so no cached audio
    # can be confirmation-gated.  Durable hangup still runs.
    assert actions == [("hangup",)]


@pytest.mark.asyncio
async def test_late_cleared_greeting_mark_is_not_routed_to_new_turn_playback() -> None:
    acknowledgements = []

    class Greeting(ImmediateCachedPlayback):
        def owns_mark(self, name):
            return name == "cache-greeting"

    class Playback:
        def owns_mark(self, name):
            return name.startswith("turn-mark-")

        async def acknowledge_mark(self, name):
            acknowledgements.append(name)

    session = make_session()
    connected, start, _ = media_messages()
    await session.feed_twilio_message(connected, FakeWebSocket())
    await session.feed_twilio_message(start, FakeWebSocket())
    session._active_greeting = Greeting()
    await session._interrupt_active_output(reason="barge_in")
    session._active_playback = Playback()

    late_greeting_mark = {
        "event": "mark",
        "sequenceNumber": "2",
        "streamSid": STREAM_SID,
        "mark": {"name": "cache-greeting"},
    }
    own_turn_mark = {
        "event": "mark",
        "sequenceNumber": "3",
        "streamSid": STREAM_SID,
        "mark": {"name": "turn-mark-1"},
    }

    await session.feed_twilio_message(late_greeting_mark, FakeWebSocket())
    await session.feed_twilio_message(own_turn_mark, FakeWebSocket())

    assert acknowledgements == ["turn-mark-1"]


@pytest.mark.asyncio
async def test_runtime_failure_keeps_receiver_for_notice_then_hangup_and_stt_close(
    monkeypatch,
) -> None:
    actions = []
    notice_started = asyncio.Event()
    notice_confirmed = asyncio.Event()

    class Repository(FakeRepository):
        async def record_hangup_requested(self, **values):
            actions.append(("claim", values["reason"]))
            return await super().record_hangup_requested(**values)

    class Stt(FakeDeepgram):
        async def events(self):
            raise RuntimeError("synthetic STT failure")
            if False:
                yield None

        async def close(self):
            actions.append(("stt-close",))

    class Notice(ImmediateCachedPlayback):
        def owns_mark(self, name):
            return name == "notice-final"

        async def run(self, **_values):
            actions.append(("notice-start",))
            notice_started.set()
            await notice_confirmed.wait()

        async def acknowledge_mark(self, name):
            actions.append(("notice-mark", name))
            notice_confirmed.set()

    class WebSocket(FakeWebSocket):
        def __init__(self):
            super().__init__()
            self.receives = 0

        async def receive_text(self):
            await notice_started.wait()
            self.receives += 1
            if self.receives == 1:
                _, _, media = media_messages(payload=b"\x00" * 160)
                return json.dumps(media)
            if self.receives == 2:
                return json.dumps(
                    {
                        "event": "mark",
                        "sequenceNumber": "3",
                        "streamSid": STREAM_SID,
                        "mark": {"name": "notice-final"},
                    }
                )
            await asyncio.Event().wait()

    async def notice_loader(_context, _kind):
        return Notice()

    async def hangup(_call_sid):
        actions.append(("hangup",))
        return True

    monkeypatch.setattr(
        "integrations.telephony.session._SESSION_DURABLE_HANGUP_TIMEOUT_SECONDS",
        0.1,
    )
    monkeypatch.setattr(
        "integrations.telephony.session._SESSION_PROVIDER_CLOSE_TIMEOUT_SECONDS",
        0.1,
    )
    stt = Stt()
    session = PhoneMediaSession(
        context(),
        deepgram=stt,
        repository=Repository(),
        current_user_loader=_user,
        hangup_call=hangup,
        notice_loader=notice_loader,
        greeting_loader=lambda _context: _immediate_playback(),
        caller_turn_persister=_noop_caller_turn_persister,
    )
    connected, start, _ = media_messages()

    result = await asyncio.wait_for(
        session.run(WebSocket(), initial_messages=(connected, start)),
        timeout=1.0,
    )

    assert result.reconnectable is False
    assert result.internal_failure is False
    assert stt.audio == []
    assert actions == [
        ("notice-start",),
        ("notice-mark", "notice-final"),
        ("claim", "error"),
        ("hangup",),
        ("stt-close",),
    ]


async def _immediate_playback():
    return ImmediateCachedPlayback()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_source", ["stt", "llm", "tts"])
async def test_stt_llm_and_tts_failures_play_confirmed_notice_and_hang_up(
    failure_source,
    monkeypatch,
) -> None:
    actions = []

    class Repository(FakeRepository):
        async def record_hangup_requested(self, **values):
            actions.append(("claim", values["reason"]))
            return await super().record_hangup_requested(**values)

    class FailedDeepgram(FakeDeepgram):
        async def events(self):
            raise RuntimeError("stt failed")
            if False:
                yield None

    async def notice(_context, kind):
        return ImmediateCachedPlayback(lambda: actions.append(("notice", kind)))

    async def hangup(_call_sid):
        actions.append(("hangup",))
        return True

    class Runtime:
        pass

    async def runtime(**_values):
        if failure_source == "llm":
            raise RuntimeError("llm failed")
        return Runtime()

    class FailedPlayback:
        def __init__(self, **_values):
            pass

        async def run(self):
            raise RuntimeError("tts failed")

    if failure_source == "tts":
        monkeypatch.setattr(
            "integrations.telephony.session.PhoneTurnPlayback", FailedPlayback
        )

    session = PhoneMediaSession(
        context(),
        deepgram=(FailedDeepgram() if failure_source == "stt" else BlockingDeepgram()),
        repository=Repository(),
        current_user_loader=_user,
        hangup_call=hangup,
        notice_loader=notice,
        greeting_loader=lambda _context: _immediate_playback(),
        runtime_starter=runtime,
    )
    if failure_source in {"llm", "tts"}:
        session._utterances.put_nowait(
            FinalPhoneUtterance("hello", 1, 1.0, 1.5, 0.9)
        )
    connected, start, _ = media_messages()

    result = await session.run(
        BlockingWebSocket(), initial_messages=(connected, start)
    )

    assert result.reconnectable is False
    assert result.internal_failure is False
    assert actions == [
        ("notice", "technical_failure"),
        ("claim", "error"),
        ("hangup",),
    ]


@pytest.mark.asyncio
async def test_forced_close_commits_playback_frontier_before_notice_and_hangup() -> None:
    actions = []

    class Playback:
        async def barge_in(self):
            actions.append("barge_commit")

    class Runtime:
        async def abort(self, _reason):
            raise AssertionError("active playback must own durable interruption")

    async def notice(_context, kind):
        assert session._active_playback is None
        return ImmediateCachedPlayback(lambda: actions.append(f"notice:{kind}"))

    async def hangup(_call_sid):
        actions.append("hangup")
        return True

    session = make_session(notice_loader=notice, hangup_call=hangup)
    session._stream_sid = STREAM_SID
    session._active_playback = Playback()
    session._active_runtime = Runtime()
    directive = EndCallDirective.forced(
        requested_at=datetime.now(UTC),
        reason=EndCallReason.ERROR,
    )

    await session._force_close(directive, FakeWebSocket())

    assert actions == ["barge_commit", "notice:technical_failure", "hangup"]


@pytest.mark.asyncio
async def test_forced_close_marks_caller_only_runtime_interrupted() -> None:
    state = PhoneTurnLinkState()
    interruptions = []

    class Runtime:
        async def interrupt(self, text, *, played_ms, reason):
            assert state.interrupted is True
            interruptions.append((text, played_ms, reason))
            return (101, None)

    session = make_session()
    session._active_runtime = Runtime()
    session._active_phone_turn = type(
        "PhoneTurn", (), {"link_state": state}
    )()

    await session._interrupt_active_output(reason="deadline")

    assert interruptions == [("", 0, "deadline")]
    assert session._active_runtime is None
    assert session._active_phone_turn is None


@pytest.mark.asyncio
async def test_deadline_preempts_greeting_before_notice_and_hangup() -> None:
    actions = []

    class Greeting:
        async def barge_in(self):
            actions.append("greeting-barge")

    async def notice(_context, kind):
        assert session._active_greeting is None
        return ImmediateCachedPlayback(lambda: actions.append(f"notice:{kind}"))

    async def hangup(_call_sid):
        actions.append("hangup")
        return True

    session = make_session(notice_loader=notice, hangup_call=hangup)
    session._stream_sid = STREAM_SID
    session._active_greeting = Greeting()
    directive = EndCallDirective.forced(
        requested_at=datetime.now(UTC),
        reason=EndCallReason.DEADLINE,
    )

    await session._force_close(directive, FakeWebSocket())

    assert actions == ["greeting-barge", "notice:deadline", "hangup"]


@pytest.mark.asyncio
async def test_silence_check_waits_for_safe_gap_without_overlapping_tts() -> None:
    session = make_session()
    session._stream_sid = STREAM_SID
    session._active_playback = object()

    class Silence:
        calls = 0

        def at_safe_point(self):
            self.calls += 1
            return None

    silence = Silence()
    session.silence = silence

    async def stop_after_first_poll(_seconds):
        session._stopping.set()

    session._sleep = stop_after_first_poll
    await session._timer_loop(FakeWebSocket())

    assert silence.calls == 0


@pytest.mark.asyncio
async def test_live_billing_timer_runs_while_assistant_audio_is_active() -> None:
    class Meter:
        def __init__(self):
            self.coverage = []

        async def ensure_live_coverage(self, **values):
            self.coverage.append(values)

    meter = Meter()
    now = 100.0
    session = make_session(billing_meter=meter, monotonic=lambda: now)
    session._call_started_monotonic = now
    session._stream_started_monotonic = 95.0
    session._active_playback = object()

    async def stop_after_first_tick(_seconds):
        session._stopping.set()

    session._sleep = stop_after_first_tick

    await session._timer_loop(FakeWebSocket())

    assert meter.coverage == [
        {
            "call_elapsed_seconds": pytest.approx(0),
            "stream_elapsed_seconds": pytest.approx(5),
            "include_pstn": True,
        }
    ]


@pytest.mark.asyncio
async def test_live_billing_error_racing_with_twilio_stop_is_ignored() -> None:
    class Meter:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def ensure_live_coverage(self, **_values):
            self.entered.set()
            await self.release.wait()
            raise PhoneBillingError("late tranche failure")

    meter = Meter()
    session = make_session(billing_meter=meter)
    session._stream_started_monotonic = session._monotonic()

    timer = asyncio.create_task(session._timer_loop(FakeWebSocket()))
    await meter.entered.wait()
    session._twilio_stop_observed = True
    meter.release.set()

    await timer


@pytest.mark.asyncio
async def test_live_billing_exhaustion_racing_with_twilio_stop_is_ignored() -> None:
    class Meter:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def ensure_live_coverage(self, **_values):
            self.entered.set()
            await self.release.wait()
            raise PhoneBillingExhausted("late balance exhaustion")

    meter = Meter()
    session = make_session(billing_meter=meter)
    session._stream_started_monotonic = session._monotonic()

    timer = asyncio.create_task(session._timer_loop(FakeWebSocket()))
    await meter.entered.wait()
    session._twilio_stop_observed = True
    meter.release.set()

    await timer


@pytest.mark.asyncio
async def test_live_billing_error_without_twilio_stop_remains_fatal() -> None:
    class Meter:
        async def ensure_live_coverage(self, **_values):
            raise PhoneBillingError("real tranche failure")

    session = make_session(billing_meter=Meter())
    session._stream_started_monotonic = session._monotonic()

    with pytest.raises(PhoneBillingError, match="real tranche failure"):
        await session._timer_loop(FakeWebSocket())


@pytest.mark.asyncio
async def test_live_billing_exhaustion_uses_cached_notice_and_hangs_up() -> None:
    actions = []

    class ExhaustedMeter:
        async def ensure_live_coverage(self, **_values):
            raise PhoneBillingExhausted("next tranche is unaffordable")

    async def notice(_context, kind):
        return ImmediateCachedPlayback(lambda: actions.append(f"notice:{kind}"))

    async def hangup(_call_sid):
        actions.append("hangup")
        return True

    session = make_session(
        billing_meter=ExhaustedMeter(),
        notice_loader=notice,
        hangup_call=hangup,
    )
    session._stream_sid = STREAM_SID
    session._stream_started_monotonic = session._monotonic()

    await session._timer_loop(FakeWebSocket())

    assert actions == ["notice:balance_exhausted", "hangup"]
    assert session._stopping.is_set()


@pytest.mark.asyncio
async def test_turn_does_not_publish_playback_while_notice_owns_wire(
    monkeypatch,
) -> None:
    actions = []

    class Runtime:
        key = None

    async def starter(**values):
        runtime = Runtime()
        runtime.key = values["phone_turn"].context.turn_key
        return runtime

    class Playback:
        def __init__(self, **_values):
            actions.append("published")

        async def run(self):
            actions.append("played")
            return type("Result", (), {"interrupted": False})()

    monkeypatch.setattr("integrations.telephony.session.PhoneTurnPlayback", Playback)
    session = make_session(runtime_starter=starter)
    session._current_user = type("User", (), {"is_enabled": True})()
    session._stream_sid = STREAM_SID
    await session._audio_lock.acquire()
    turn = asyncio.create_task(
        session._run_turn(
            FinalPhoneUtterance("caller during notice", 1, 1.0, 1.5, 0.9),
            FakeWebSocket(),
        )
    )
    try:
        for _ in range(10):
            if session._active_runtime is not None:
                break
            await asyncio.sleep(0)
        assert session._active_runtime is not None
        assert session._active_playback is None
        assert actions == []
    finally:
        session._audio_lock.release()
    await turn

    assert actions == ["published", "played"]


@pytest.mark.asyncio
async def test_speech_during_runtime_start_persists_caller_only() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    interruptions = []

    phone_turn = None

    class Runtime:
        async def interrupt(self, text, *, played_ms, reason):
            assert phone_turn.link_state.interrupted is True
            interruptions.append((text, played_ms, reason))
            return (101, None)

    async def starter(**values):
        nonlocal phone_turn
        phone_turn = values["phone_turn"]
        entered.set()
        await release.wait()
        return Runtime()

    session = make_session(runtime_starter=starter)
    session._current_user = type("User", (), {"is_enabled": True})()
    session._stream_sid = STREAM_SID
    task = asyncio.create_task(
        session._run_turn(
            FinalPhoneUtterance("first caller turn", 1, 1.0, 1.5, 0.9),
            FakeWebSocket(),
        )
    )
    await entered.wait()
    await session.feed_deepgram_event(
        DeepgramSpeechStartedEvent(1.6), FakeWebSocket()
    )
    await session.feed_deepgram_event(
        DeepgramTranscriptEvent(
            text="wait, I need to correct that",
            is_final=False,
            speech_final=False,
            from_finalize=False,
            start_seconds=1.6,
            duration_seconds=0.4,
            confidence=0.95,
            words=(),
        ),
        FakeWebSocket(),
    )
    release.set()
    await task

    assert interruptions == [("", 0, "barge_during_runtime_start")]
    assert session._caller_turns == 1
    assert session._active_playback is None


@pytest.mark.asyncio
async def test_barge_generation_is_rechecked_at_playback_publish_boundary(
    monkeypatch,
) -> None:
    interruptions = []

    class Runtime:
        async def interrupt(self, text, *, played_ms, reason):
            interruptions.append((text, played_ms, reason))
            return (101, None)

    async def starter(**_values):
        return Runtime()

    session = make_session(runtime_starter=starter)

    class Playback:
        def __init__(self, **_values):
            # Deterministically model SpeechStarted landing after runtime
            # admission but immediately before playback becomes its owner.
            session._speech_generation += 1

        async def run(self):
            raise AssertionError("barge boundary must not start assistant audio")

    monkeypatch.setattr("integrations.telephony.session.PhoneTurnPlayback", Playback)
    session._current_user = type("User", (), {"is_enabled": True})()
    session._stream_sid = STREAM_SID

    await session._run_turn(
        FinalPhoneUtterance("second caller turn", 2, 2.0, 2.5, 0.9),
        FakeWebSocket(),
    )

    assert interruptions == [("", 0, "barge_during_runtime_start")]
    assert session._caller_turns == 1
    assert session._active_playback is None


@pytest.mark.asyncio
async def test_forced_close_invalidates_runtime_starter_before_notice(
    monkeypatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    actions = []

    class Runtime:
        async def interrupt(self, text, *, played_ms, reason):
            actions.append(("interrupt", text, played_ms, reason))
            return (101, None)

    async def starter(**_values):
        entered.set()
        await release.wait()
        return Runtime()

    class Playback:
        def __init__(self, **_values):
            raise AssertionError("forced close must fence assistant playback")

    async def notice(_context, kind):
        return ImmediateCachedPlayback(lambda: actions.append(("notice", kind)))

    async def hangup(_call_sid):
        actions.append(("hangup",))
        return True

    monkeypatch.setattr("integrations.telephony.session.PhoneTurnPlayback", Playback)
    session = make_session(
        runtime_starter=starter,
        notice_loader=notice,
        hangup_call=hangup,
    )
    session._current_user = type("User", (), {"is_enabled": True})()
    session._stream_sid = STREAM_SID
    turn = asyncio.create_task(
        session._run_turn(
            FinalPhoneUtterance("last caller turn", 1, 1.0, 1.5, 0.9),
            FakeWebSocket(),
        )
    )
    await entered.wait()
    directive = EndCallDirective.forced(
        requested_at=datetime.now(UTC),
        reason=EndCallReason.ERROR,
    )

    await session._force_close(directive, FakeWebSocket())
    assert actions == [("notice", "technical_failure"), ("hangup",)]
    release.set()
    await turn

    assert actions == [
        ("notice", "technical_failure"),
        ("hangup",),
        ("interrupt", "", 0, "barge_during_runtime_start"),
    ]
    assert session._caller_turns == 1


@pytest.mark.asyncio
async def test_ambiguous_hangup_keeps_retry_open_until_provider_accepts() -> None:
    actions = []

    class Repository(FakeRepository):
        attempts = 0

        async def record_hangup_requested(self, **_values):
            self.attempts += 1
            actions.append("requested")
            return PhoneHangupAttemptClaim(
                call_id="call-session-1",
                provider_call_sid=CALL_SID,
                state="in_flight",
                attempt_count=self.attempts,
                attempt_token=f"attempt-token-{self.attempts}",
                lease_until="2030-01-01T00:01:00Z",
                reason=str(_values["reason"]),
                target_status=_values["target_status"].value,
                origin="session",
                claimed=True,
            )

        async def mark_hangup_unresolved(self, **_values):
            actions.append("unresolved")
            return True

        async def mark_hangup_accepted(self, **_values):
            actions.append("accepted")
            return True

    outcomes = [RuntimeError("timeout"), True]

    async def provider_hangup(_call_sid):
        actions.append("provider")
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    session = make_session(
        repository=Repository(), hangup_call=provider_hangup
    )
    with pytest.raises(PhoneMediaSessionError, match="could not be confirmed"):
        await session._hangup("end_call")
    assert session._hangup_confirmed is False

    await session._hangup("end_call")

    assert actions == [
        "requested",
        "provider",
        "unresolved",
        "requested",
        "provider",
        "accepted",
    ]
    assert session._hangup_confirmed is False
    assert session._hangup_accepted is True


@pytest.mark.asyncio
async def test_stale_session_hangup_token_is_not_treated_as_confirmed() -> None:
    class Repository(FakeRepository):
        async def record_hangup_requested(self, **_values):
            return PhoneHangupAttemptClaim(
                call_id="call-session-1",
                provider_call_sid=CALL_SID,
                state="in_flight",
                attempt_count=1,
                attempt_token="stale-attempt-token",
                lease_until="2030-01-01T00:01:00Z",
                reason="end_call",
                target_status="completed",
                origin="session",
                claimed=True,
            )

        async def mark_hangup_accepted(self, **_values):
            return False

        async def get_hangup_attempt_state(self, **_values):
            return "in_flight"

        async def mark_hangup_unresolved(self, **_values):
            return False

    session = make_session(repository=Repository(), hangup_call=_noop)

    with pytest.raises(PhoneMediaSessionError, match="lost its durable fence"):
        await session._hangup("end_call")

    assert session._hangup_confirmed is False
    assert session._hangup_accepted is False
    assert session._stop_reason == "hangup_pending"


@pytest.mark.asyncio
async def test_session_reconciles_definitive_provider_absence() -> None:
    actions = []

    class Repository(FakeRepository):
        async def reconcile_hangup_provider_absent(self, **_values):
            actions.append("provider-absent")
            return True

    async def provider_absent(_call_sid):
        actions.append("provider")
        return False

    session = make_session(
        repository=Repository(),
        hangup_call=provider_absent,
    )

    await session._hangup("end_call")

    assert actions == ["provider", "provider-absent"]
    assert session._hangup_confirmed is True
    assert session._hangup_accepted is False
    assert session._stop_reason == "end_call"


@pytest.mark.asyncio
async def test_invalid_session_hangup_adapter_result_is_unresolved() -> None:
    actions = []

    class Repository(FakeRepository):
        async def mark_hangup_unresolved(self, **_values):
            actions.append("unresolved")
            return True

    async def invalid_adapter(_call_sid):
        actions.append("provider")
        return None

    session = make_session(
        repository=Repository(),
        hangup_call=invalid_adapter,
    )

    with pytest.raises(PhoneMediaSessionError, match="could not be confirmed"):
        await session._hangup("end_call")

    assert actions == ["provider", "unresolved"]
    assert session._hangup_confirmed is False
    assert session._hangup_accepted is False
    assert session._stop_reason == "hangup_unresolved"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_result", [True, False])
async def test_session_persistence_failure_after_rest_is_fenced_unresolved(
    provider_result,
) -> None:
    actions = []

    class Repository(FakeRepository):
        async def mark_hangup_accepted(self, **_values):
            actions.append("accepted-write-failed")
            raise RuntimeError("accepted write failed")

        async def reconcile_hangup_provider_absent(self, **_values):
            actions.append("absent-write-failed")
            raise RuntimeError("absent write failed")

        async def mark_hangup_unresolved(self, **_values):
            actions.append("unresolved")
            return True

    async def provider(_call_sid):
        actions.append("provider")
        return provider_result

    session = make_session(repository=Repository(), hangup_call=provider)

    with pytest.raises(PhoneMediaSessionError, match="could not be persisted"):
        await session._hangup("end_call")

    expected_write = (
        "accepted-write-failed" if provider_result else "absent-write-failed"
    )
    assert actions == ["provider", expected_write, "unresolved"]
    assert session._hangup_confirmed is False
    assert session._hangup_accepted is False
    assert session._stop_reason == "hangup_unresolved"


@pytest.mark.asyncio
async def test_session_renews_foreground_under_exact_fence() -> None:
    renewals = []

    class Repository(FakeRepository):
        async def renew_session_foreground(self, **values):
            renewals.append(values)
            return True

    session = make_session(repository=Repository())

    async def stop_after_renewal(_seconds):
        session._stopping.set()

    session._sleep = stop_after_renewal
    await session._lease_loop()

    assert len(renewals) == 1
    assert renewals[0]["call_id"] == "call-session-1"
    assert renewals[0]["fencing_token"] == 7
    assert renewals[0]["lease_owner"] == "media:call-session-1"
