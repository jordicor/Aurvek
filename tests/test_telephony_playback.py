import asyncio
from pathlib import Path

import pytest

from ai_runtime.channel_turns import ChannelDraft, TurnKey
from integrations.telephony.audio import PcmuCacheAsset
from integrations.telephony.clock import EndCallDirective, utc_now
from integrations.telephony.foreground import ForegroundCommitGuard
from integrations.telephony.phone_context import create_phone_channel_turn
from integrations.telephony.playback import (
    PhonePlaybackError,
    PhoneTurnPlayback,
)
from integrations.telephony.speech import PhoneSpeechAsset
from integrations.telephony.transport import PhoneRuntimeEvent


class FakeRuntimeTurn:
    def __init__(self, *, content_parts, draft):
        self.key = TurnKey("call-1", "turn-1")
        self.content_parts = list(content_parts)
        self._draft = ChannelDraft(draft)
        self.confirmations = []
        self.interruptions = []
        self.aborts = []
        self.release_draft = asyncio.Event()
        self.events_started = asyncio.Event()

    async def events_until_draft(self):
        self.events_started.set()
        for content in self.content_parts:
            yield PhoneRuntimeEvent(content=content)
        await self.release_draft.wait()

    async def wait_for_draft(self):
        await self.release_draft.wait()
        return self._draft

    async def confirm_audible(self, text_prefix, *, played_ms):
        self.confirmations.append((text_prefix, played_ms))
        return 11, 12

    async def interrupt(self, text_prefix, *, played_ms, reason):
        self.interruptions.append((text_prefix, played_ms, reason))
        self.release_draft.set()
        return 21, 22

    async def abort(self, reason):
        self.aborts.append(reason)
        self.release_draft.set()


class MutableMonotonic:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


def _phone_turn():
    return create_phone_channel_turn(
        ForegroundCommitGuard(
            conversation_id=7,
            epoch=3,
            expected_owner="phone",
            call_id="call-1",
            lease_owner="worker-1",
        ),
        turn_id="turn-1",
    )


async def _wait_for_message(messages, event, *, timeout=2):
    async def wait():
        while not any(item.get("event") == event for item in messages):
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=timeout)


def _renderer(audio=b"\x7f" * 800):
    async def render(text):
        return PhoneSpeechAsset(
            text=text,
            pcmu=audio,
            cache=PcmuCacheAsset(
                path=Path("unused.mulaw"),
                byte_length=len(audio),
                duration_ms=len(audio) / 8,
                sha256="0" * 64,
            ),
        )

    return render


@pytest.mark.asyncio
async def test_final_mark_confirms_exact_canonical_draft():
    runtime = FakeRuntimeTurn(
        content_parts=["Hello ", "there."],
        draft="Hello there.",
    )
    phone_turn = _phone_turn()
    sent = []

    async def send(message):
        sent.append(dict(message))

    playback = PhoneTurnPlayback(
        stream_sid="MZ" + "1" * 32,
        phone_turn=phone_turn,
        runtime_turn=runtime,
        render_speech=_renderer(),
        send_message=send,
    )
    assert playback.output_started is False
    task = asyncio.create_task(playback.run())
    runtime.release_draft.set()
    await _wait_for_message(sent, "mark")
    assert playback.output_started is True
    mark = next(item["mark"]["name"] for item in sent if item["event"] == "mark")
    confirmation = await playback.acknowledge_mark(mark)
    result = await task

    assert confirmation.text_prefix == "Hello there."
    assert runtime.confirmations == [("Hello there.", 100)]
    assert result.message_ids == (11, 12)
    assert result.confirmed_text == "Hello there."
    assert result.played_ms == 100
    assert result.interrupted is False
    assert len([item for item in sent if item["event"] == "media"]) == 5


@pytest.mark.asyncio
async def test_barge_in_clears_audio_and_cancels_voluntary_hangup():
    runtime = FakeRuntimeTurn(
        content_parts=["Hello world. "],
        draft="Hello world. ",
    )
    phone_turn = _phone_turn()
    phone_turn.end_controller.request(
        EndCallDirective.voluntary(
            requested_at=utc_now(),
            final_message="Hello world.",
        )
    )
    sent = []
    monotonic = MutableMonotonic()

    async def send(message):
        sent.append(dict(message))

    playback = PhoneTurnPlayback(
        stream_sid="MZ" + "2" * 32,
        phone_turn=phone_turn,
        runtime_turn=runtime,
        render_speech=_renderer(),
        send_message=send,
        monotonic=monotonic,
        call_started_monotonic=99.0,
    )
    task = asyncio.create_task(playback.run())
    await _wait_for_message(sent, "mark")
    monotonic.value = 100.2

    result = await playback.barge_in()
    assert await task == result
    assert result.message_ids == (21, 22)
    assert result.confirmed_text == "Hello world. "
    assert result.played_ms == 100
    assert result.interrupted is True
    assert runtime.interruptions == [("Hello world. ", 100, "barge_in")]
    assert sent[-1]["event"] == "clear"
    assert phone_turn.link_state.interrupted is True
    assert phone_turn.end_controller.pending is None

    # Twilio returns pending marks after clear; they cannot expand persistence.
    mark = next(item["mark"]["name"] for item in sent if item["event"] == "mark")
    drained = await playback.acknowledge_mark(mark)
    assert drained.drained_after_clear is True
    assert drained.text_prefix == result.confirmed_text


@pytest.mark.asyncio
async def test_early_barge_persists_caller_only_when_no_fragment_is_complete():
    runtime = FakeRuntimeTurn(
        content_parts=["Hello world. "],
        draft="Hello world. ",
    )
    sent = []
    monotonic = MutableMonotonic()

    async def send(message):
        sent.append(dict(message))

    playback = PhoneTurnPlayback(
        stream_sid="MZ" + "5" * 32,
        phone_turn=_phone_turn(),
        runtime_turn=runtime,
        render_speech=_renderer(),
        send_message=send,
        monotonic=monotonic,
        call_started_monotonic=99.0,
    )
    task = asyncio.create_task(playback.run())
    await _wait_for_message(sent, "mark")
    # Ten milliseconds are conservatively audible after the safety lag, but
    # not the complete 100 ms text/audio alignment unit.
    monotonic.value = 100.09

    result = await playback.barge_in()
    assert await task == result
    assert runtime.interruptions == [("", 0, "barge_in")]
    assert result.confirmed_text == ""
    assert result.played_ms == 0
    assert result.message_ids == (21, 22)


@pytest.mark.asyncio
async def test_cancel_during_generation_persists_caller_once_without_fake_speech():
    runtime = FakeRuntimeTurn(content_parts=[], draft="unused")
    phone_turn = _phone_turn()
    directive = EndCallDirective.voluntary(
        requested_at=utc_now(),
        final_message="Goodbye.",
    )
    phone_turn.end_controller.request(directive)

    async def closed_send(_message):
        raise RuntimeError("websocket already closed")

    playback = PhoneTurnPlayback(
        stream_sid="MZ" + "6" * 32,
        phone_turn=phone_turn,
        runtime_turn=runtime,
        render_speech=_renderer(),
        send_message=closed_send,
    )
    task = asyncio.create_task(playback.run())
    await runtime.events_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.interruptions == [
        ("", 0, "phone_playback_cancelled")
    ]
    assert runtime.aborts == []
    assert phone_turn.end_controller.pending == directive


@pytest.mark.asyncio
async def test_cancel_during_playback_persists_safe_prefix_despite_closed_socket():
    runtime = FakeRuntimeTurn(
        content_parts=["Hello world. "],
        draft="Hello world. ",
    )
    phone_turn = _phone_turn()
    directive = EndCallDirective.voluntary(
        requested_at=utc_now(),
        final_message="Hello world.",
    )
    phone_turn.end_controller.request(directive)
    sent = []
    socket_closed = False
    monotonic = MutableMonotonic()

    async def send(message):
        if socket_closed:
            raise RuntimeError("websocket already closed")
        sent.append(dict(message))

    playback = PhoneTurnPlayback(
        stream_sid="MZ" + "7" * 32,
        phone_turn=phone_turn,
        runtime_turn=runtime,
        render_speech=_renderer(),
        send_message=send,
        monotonic=monotonic,
        call_started_monotonic=99.0,
    )
    task = asyncio.create_task(playback.run())
    await _wait_for_message(sent, "mark")
    monotonic.value = 100.2
    socket_closed = True
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.interruptions == [
        ("Hello world. ", 100, "phone_playback_cancelled")
    ]
    assert runtime.aborts == []
    assert phone_turn.end_controller.pending == directive


@pytest.mark.asyncio
async def test_streamed_text_mismatch_persists_caller_without_confirmation():
    runtime = FakeRuntimeTurn(content_parts=["one"], draft="another")
    sent = []

    async def send(message):
        sent.append(dict(message))

    playback = PhoneTurnPlayback(
        stream_sid="MZ" + "3" * 32,
        phone_turn=_phone_turn(),
        runtime_turn=runtime,
        render_speech=_renderer(),
        send_message=send,
    )
    runtime.release_draft.set()

    with pytest.raises(PhonePlaybackError, match="does not match"):
        await playback.run()
    assert runtime.confirmations == []
    assert runtime.interruptions == [("", 0, "phone_playback_failed")]
    assert runtime.aborts == []
    assert sent[-1]["event"] == "clear"


@pytest.mark.asyncio
async def test_empty_draft_persists_caller_only_without_audio():
    runtime = FakeRuntimeTurn(content_parts=[], draft="")
    sent = []

    async def send(message):
        sent.append(dict(message))

    playback = PhoneTurnPlayback(
        stream_sid="MZ" + "4" * 32,
        phone_turn=_phone_turn(),
        runtime_turn=runtime,
        render_speech=_renderer(),
        send_message=send,
    )
    runtime.release_draft.set()

    result = await playback.run()
    assert result.message_ids == (11, 12)
    assert runtime.confirmations == [("", 0)]
    assert sent == []


@pytest.mark.asyncio
async def test_output_does_not_start_when_first_media_send_fails():
    runtime = FakeRuntimeTurn(
        content_parts=["Hello world. "],
        draft="Hello world. ",
    )

    async def fail_send(_message):
        raise RuntimeError("Twilio wire failed")

    playback = PhoneTurnPlayback(
        stream_sid="MZ" + "8" * 32,
        phone_turn=_phone_turn(),
        runtime_turn=runtime,
        render_speech=_renderer(),
        send_message=fail_send,
    )
    runtime.release_draft.set()

    with pytest.raises(PhonePlaybackError, match="phone playback failed"):
        await playback.run()

    assert playback.output_started is False


@pytest.mark.asyncio
async def test_native_pcmu_reaches_twilio_before_provider_stream_completes():
    runtime = FakeRuntimeTurn(
        content_parts=["Hello world. "],
        draft="Hello world. ",
    )
    sent = []
    first_chunk_sent = asyncio.Event()
    release_provider = asyncio.Event()
    raw_pcmu = b"\x7f" * 320

    async def send(message):
        sent.append(dict(message))

    async def stream_render(text, on_chunk, on_complete):
        await on_chunk(raw_pcmu[:160])
        first_chunk_sent.set()
        await release_provider.wait()
        await on_chunk(raw_pcmu[160:])
        await on_complete(raw_pcmu)
        return PhoneSpeechAsset(
            text=text,
            pcmu=raw_pcmu,
            cache=PcmuCacheAsset(
                path=Path("unused.mulaw"),
                byte_length=len(raw_pcmu),
                duration_ms=len(raw_pcmu) / 8,
                sha256="1" * 64,
            ),
        )

    playback = PhoneTurnPlayback(
        stream_sid="MZ" + "9" * 32,
        phone_turn=_phone_turn(),
        runtime_turn=runtime,
        render_speech=_renderer(),
        stream_render_speech=stream_render,
        send_message=send,
    )
    task = asyncio.create_task(playback.run())
    await asyncio.wait_for(first_chunk_sent.wait(), timeout=2)

    assert playback.output_started is True
    assert [item["event"] for item in sent] == ["media"]

    release_provider.set()
    runtime.release_draft.set()
    await _wait_for_message(sent, "mark")
    mark = next(item["mark"]["name"] for item in sent if item["event"] == "mark")
    await playback.acknowledge_mark(mark)
    result = await task

    assert result.confirmed_text == "Hello world. "
    assert len([item for item in sent if item["event"] == "media"]) == 2


@pytest.mark.asyncio
async def test_barge_in_during_provider_stream_claims_no_partial_fragment():
    runtime = FakeRuntimeTurn(
        content_parts=["Hello world. "],
        draft="Hello world. ",
    )
    sent = []
    first_chunk_sent = asyncio.Event()
    release_provider = asyncio.Event()
    raw_pcmu = b"\x7f" * 320

    async def send(message):
        sent.append(dict(message))

    async def stream_render(text, on_chunk, on_complete):
        await on_chunk(raw_pcmu[:160])
        first_chunk_sent.set()
        await release_provider.wait()
        await on_chunk(raw_pcmu[160:])
        await on_complete(raw_pcmu)
        raise AssertionError("interrupted stream must not complete")

    playback = PhoneTurnPlayback(
        stream_sid="MZ" + "a" * 32,
        phone_turn=_phone_turn(),
        runtime_turn=runtime,
        render_speech=_renderer(),
        stream_render_speech=stream_render,
        send_message=send,
    )
    task = asyncio.create_task(playback.run())
    await asyncio.wait_for(first_chunk_sent.wait(), timeout=2)

    result = await playback.barge_in()
    release_provider.set()
    assert await task == result
    assert result.confirmed_text == ""
    assert result.played_ms == 0
    assert runtime.interruptions == [("", 0, "barge_in")]
    assert sent[-1]["event"] == "clear"
