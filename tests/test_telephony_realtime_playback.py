import asyncio

import pytest

from ai_runtime.channel_turns import ChannelDraft, TurnKey
from integrations.telephony.foreground import ForegroundCommitGuard
from integrations.telephony.media_streams import ConservativePlaybackClock
from integrations.telephony.message_audio import PhoneMessageAudioRange
from integrations.telephony.phone_context import create_phone_channel_turn
from integrations.telephony.recording import LocalCallRecorder
from integrations.telephony.realtime_bridge import RealtimeTextCheckpoint
from integrations.telephony.realtime_playback import (
    RealtimePlaybackError,
    RealtimeTurnPlayback,
)


class FakeRuntimeTurn:
    def __init__(
        self,
        draft: str = "Hello from realtime.",
        *,
        draft_published: bool = True,
    ) -> None:
        self.key = TurnKey("call-1", "turn-1")
        self._draft = ChannelDraft(draft)
        self._draft_published = draft_published
        self._draft_ready = asyncio.Event()
        if draft_published:
            self._draft_ready.set()
        self.confirmations = []
        self.interruptions = []
        self.aborts = []

    async def wait_for_draft(self):
        await self._draft_ready.wait()
        return self._draft

    @property
    def draft(self):
        return self._draft if self._draft_published else None

    async def confirm_audible(self, text, *, played_ms):
        self.confirmations.append((text, played_ms))
        return 11, 12

    async def interrupt(self, text, *, played_ms, reason):
        self.interruptions.append((text, played_ms, reason))
        return 21, 22

    async def abort(self, reason):
        self.aborts.append(reason)


class FakeBridge:
    def __init__(
        self,
        chunks=(),
        *,
        checkpoint_text: str = "",
        checkpoint_ms: int = 0,
    ) -> None:
        self.chunks = list(chunks)
        self.checkpoint_text = checkpoint_text
        self.checkpoint_ms = checkpoint_ms
        self.started = True
        self.cancelled = 0
        self.truncated = []

    async def output_pcmu(self):
        for chunk in self.chunks:
            yield chunk

    async def cancel_output(self):
        self.cancelled += 1

    async def truncate_output(self, *, played_ms):
        self.truncated.append(played_ms)

    def confirmed_text_prefix(self, played_ms):
        if played_ms >= self.checkpoint_ms:
            return self.checkpoint_text
        return ""

    def confirmed_text_checkpoint(self, played_ms):
        if self.checkpoint_text and played_ms >= self.checkpoint_ms:
            return RealtimeTextCheckpoint(
                self.checkpoint_text,
                self.checkpoint_ms,
            )
        return None

    async def finish_pending_output(self):
        return False


class BlockingBridge(FakeBridge):
    def __init__(self, chunk: bytes = b"\x7f" * 160, **kwargs) -> None:
        super().__init__(**kwargs)
        self.chunk = chunk
        self.audio_sent = asyncio.Event()
        self.release = asyncio.Event()

    async def output_pcmu(self):
        yield self.chunk
        self.audio_sent.set()
        await self.release.wait()

    async def cancel_output(self):
        await super().cancel_output()
        self.release.set()


class CompletedBridge(FakeBridge):
    def __init__(self, chunks=(), **kwargs) -> None:
        super().__init__(chunks, **kwargs)
        self.audio_complete = asyncio.Event()

    async def output_pcmu(self):
        for chunk in self.chunks:
            yield chunk
        self.audio_complete.set()


class AdjustableClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


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


async def _wait_for_event(messages, event, *, timeout=2):
    async def wait():
        while not any(message.get("event") == event for message in messages):
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=timeout)


@pytest.mark.asyncio
async def test_final_mark_confirms_full_canonical_draft_after_native_pcmu(tmp_path):
    runtime = FakeRuntimeTurn()
    bridge = FakeBridge([b"\x7f" * 80, b"\x7f" * 240])
    phone_turn = _phone_turn()
    sent = []

    async def send(message):
        sent.append(dict(message))

    playback = RealtimeTurnPlayback(
        stream_sid="MZ" + "1" * 32,
        phone_turn=phone_turn,
        runtime_turn=runtime,
        bridge=bridge,
        send_message=send,
        recorder=LocalCallRecorder(
            "realtime-complete",
            enabled=True,
            root=tmp_path / "recordings",
        ),
        monotonic=lambda: 100.0,
        call_started_monotonic=99.0,
    )
    task = asyncio.create_task(playback.run())
    await _wait_for_event(sent, "mark")

    mark = next(message["mark"]["name"] for message in sent if message["event"] == "mark")
    assert playback.owns_mark(mark)
    confirmation = await playback.acknowledge_mark(mark)
    result = await task

    assert confirmation.advanced is True
    assert confirmation.played_ms == 40
    assert runtime.confirmations == [("Hello from realtime.", 40)]
    assert runtime.interruptions == []
    assert result.confirmed_text == "Hello from realtime."
    assert result.message_ids == (11, 12)
    assert playback.output_started is True
    assert len([message for message in sent if message["event"] == "media"]) == 2
    assert phone_turn.link_state.assistant_audio_range == PhoneMessageAudioRange(
        start_byte=8_000,
        end_byte=8_320,
    )


@pytest.mark.asyncio
async def test_missing_final_mark_times_out_and_persists_caller_only_once():
    runtime = FakeRuntimeTurn()
    bridge = FakeBridge(
        [b"\x7f" * 160],
        checkpoint_text="Hello from realtime.",
        checkpoint_ms=20,
    )
    sent = []

    async def send(message):
        sent.append(dict(message))

    playback = RealtimeTurnPlayback(
        stream_sid="MZ" + "6" * 32,
        phone_turn=_phone_turn(),
        runtime_turn=runtime,
        bridge=bridge,
        send_message=send,
        playback_clock=ConservativePlaybackClock(safety_lag_ms=0),
        mark_confirmation_grace_seconds=0.01,
    )

    with pytest.raises(
        RealtimePlaybackError,
        match="mark confirmation timed out",
    ):
        await asyncio.wait_for(playback.run(), timeout=1)

    assert runtime.confirmations == []
    assert runtime.interruptions == [
        ("", 0, "phone_realtime_mark_timeout")
    ]
    assert bridge.cancelled == 1
    assert bridge.truncated == [20]
    assert len([message for message in sent if message["event"] == "clear"]) == 1
    assert len([message for message in sent if message["event"] == "mark"]) == 1


@pytest.mark.asyncio
async def test_long_audio_mark_wait_includes_audio_duration_before_grace():
    runtime = FakeRuntimeTurn()
    bridge = FakeBridge([b"\x7f" * 8_000])
    sent = []

    async def send(message):
        sent.append(dict(message))

    playback = RealtimeTurnPlayback(
        stream_sid="MZ" + "7" * 32,
        phone_turn=_phone_turn(),
        runtime_turn=runtime,
        bridge=bridge,
        send_message=send,
        mark_confirmation_grace_seconds=0.01,
    )
    task = asyncio.create_task(playback.run())
    await _wait_for_event(sent, "mark")

    # The grace alone is only 10 ms.  A fixed grace timeout would already
    # have failed, while the correct bound retains the full 1 s audio time.
    await asyncio.sleep(0.05)
    assert not task.done()

    mark = next(
        message["mark"]["name"]
        for message in sent
        if message["event"] == "mark"
    )
    await playback.acknowledge_mark(mark)
    result = await asyncio.wait_for(task, timeout=1)

    assert result.interrupted is False
    assert result.played_ms == 1_000
    assert runtime.confirmations == [("Hello from realtime.", 1_000)]
    assert runtime.interruptions == []
    assert bridge.cancelled == 0
    assert bridge.truncated == []


@pytest.mark.asyncio
async def test_barge_in_clears_and_persists_caller_only_without_alignment_guess(
    tmp_path,
):
    runtime = FakeRuntimeTurn()
    bridge = BlockingBridge()
    phone_turn = _phone_turn()
    sent = []

    async def send(message):
        sent.append(dict(message))

    recorder = LocalCallRecorder(
        "realtime-interrupted",
        enabled=True,
        root=tmp_path / "recordings",
    )
    playback = RealtimeTurnPlayback(
        stream_sid="MZ" + "2" * 32,
        phone_turn=phone_turn,
        runtime_turn=runtime,
        bridge=bridge,
        send_message=send,
        recorder=recorder,
    )
    task = asyncio.create_task(playback.run())
    await bridge.audio_sent.wait()
    result = await playback.barge_in()
    run_result = await task

    assert result == run_result
    assert result.confirmed_text == ""
    assert result.played_ms == 0
    assert result.interrupted is True
    assert runtime.confirmations == []
    assert runtime.interruptions == [("", 0, "barge_in")]
    assert bridge.cancelled == 1
    assert bridge.truncated == [0]
    assert any(message["event"] == "clear" for message in sent)
    assert phone_turn.link_state.interrupted is True
    assert phone_turn.link_state.assistant_audio_range is None
    assert recorder.finalize(create_mix=False).assistant_path is None


@pytest.mark.asyncio
async def test_barge_in_uses_real_playhead_without_guessing_partial_words(tmp_path):
    clock = AdjustableClock(100.0)
    runtime = FakeRuntimeTurn(
        "A verified phrase. Unverified tail.",
        draft_published=False,
    )
    bridge = BlockingBridge(
        b"\x7f" * 8_000,
        checkpoint_text="A verified phrase. ",
        checkpoint_ms=240,
    )
    phone_turn = _phone_turn()
    sent = []

    async def send(message):
        sent.append(dict(message))

    recorder = LocalCallRecorder(
        "realtime-partial-playhead",
        enabled=True,
        root=tmp_path / "recordings",
    )
    playback = RealtimeTurnPlayback(
        stream_sid="MZ" + "a" * 32,
        phone_turn=phone_turn,
        runtime_turn=runtime,
        bridge=bridge,
        send_message=send,
        recorder=recorder,
        monotonic=clock,
        call_started_monotonic=99.0,
    )
    task = asyncio.create_task(playback.run())
    await bridge.audio_sent.wait()
    clock.advance(0.4)

    result = await playback.barge_in()
    assert result == await task

    # 400 ms elapsed less the default 80 ms safety lag. The provider, durable
    # message metadata and raw recording retain that real audio frontier, while
    # text stops at the last bridge checkpoint instead of a ratio-based slice.
    assert bridge.truncated == [320]
    assert runtime.interruptions == [
        ("A verified phrase. ", 320, "barge_in")
    ]
    assert result.confirmed_text == "A verified phrase. "
    assert result.played_ms == 320
    assert result.interrupted is True
    assert phone_turn.link_state.interrupted is True
    assert phone_turn.link_state.assistant_audio_range == PhoneMessageAudioRange(
        start_byte=8_000,
        end_byte=9_920,
    )
    recording = recorder.finalize(create_mix=False)
    assert recording.assistant_path is not None
    assert len(recording.assistant_path.read_bytes()) == 10_560


@pytest.mark.asyncio
async def test_nonzero_playhead_without_checkpoint_stays_caller_only():
    clock = AdjustableClock(100.0)
    runtime = FakeRuntimeTurn("No timestamped words are available yet.")
    bridge = BlockingBridge(b"\x7f" * 8_000)
    playback = RealtimeTurnPlayback(
        stream_sid="MZ" + "c" * 32,
        phone_turn=_phone_turn(),
        runtime_turn=runtime,
        bridge=bridge,
        send_message=lambda _message: asyncio.sleep(0),
        monotonic=clock,
        call_started_monotonic=99.0,
    )
    task = asyncio.create_task(playback.run())
    await bridge.audio_sent.wait()
    clock.advance(0.4)

    result = await playback.barge_in()
    assert result == await task
    assert bridge.truncated == [320]
    assert runtime.interruptions == [("", 0, "barge_in")]
    assert result.confirmed_text == ""
    assert result.played_ms == 0


@pytest.mark.asyncio
async def test_barge_in_after_audio_end_does_not_wait_for_unpublished_draft():
    clock = AdjustableClock(100.0)
    runtime = FakeRuntimeTurn(
        "Confirmed prefix. Remaining draft.",
        draft_published=False,
    )
    bridge = CompletedBridge(
        [b"\x7f" * 8_000],
        checkpoint_text="Confirmed prefix. ",
        checkpoint_ms=240,
    )
    playback = RealtimeTurnPlayback(
        stream_sid="MZ" + "d" * 32,
        phone_turn=_phone_turn(),
        runtime_turn=runtime,
        bridge=bridge,
        send_message=lambda _message: asyncio.sleep(0),
        monotonic=clock,
        call_started_monotonic=99.0,
    )
    task = asyncio.create_task(playback.run())
    await bridge.audio_complete.wait()
    await asyncio.sleep(0)
    assert not task.done()
    clock.advance(0.4)

    result = await asyncio.wait_for(playback.barge_in(), timeout=1)
    assert result == await asyncio.wait_for(task, timeout=1)
    assert bridge.truncated == [320]
    assert runtime.interruptions == [
        ("Confirmed prefix. ", 320, "barge_in")
    ]


@pytest.mark.asyncio
async def test_finished_draft_without_checkpoint_is_not_assumed_audible(tmp_path):
    clock = AdjustableClock(100.0)
    runtime = FakeRuntimeTurn("The complete response was heard.")
    bridge = FakeBridge([b"\x7f" * 800])
    phone_turn = _phone_turn()
    sent = []

    async def send(message):
        sent.append(dict(message))

    recorder = LocalCallRecorder(
        "realtime-verified-prefix",
        enabled=True,
        root=tmp_path / "recordings",
    )
    playback = RealtimeTurnPlayback(
        stream_sid="MZ" + "b" * 32,
        phone_turn=phone_turn,
        runtime_turn=runtime,
        bridge=bridge,
        send_message=send,
        recorder=recorder,
        monotonic=clock,
        call_started_monotonic=99.0,
    )
    task = asyncio.create_task(playback.run())
    await _wait_for_event(sent, "mark")
    clock.advance(0.2)

    result = await playback.barge_in()
    assert result == await task

    assert bridge.truncated == [100]
    assert runtime.interruptions == [("", 0, "barge_in")]
    assert result.confirmed_text == ""
    assert result.played_ms == 0
    assert result.interrupted is True
    assert phone_turn.link_state.interrupted is True
    assert phone_turn.link_state.assistant_audio_range is None
    recording = recorder.finalize(create_mix=False)
    assert recording.assistant_path is not None
    assert len(recording.assistant_path.read_bytes()) == 8_800


@pytest.mark.asyncio
async def test_text_only_response_fails_without_canonical_tts_fallback():
    runtime = FakeRuntimeTurn("Exact canonical words.")
    bridge = FakeBridge()
    sent = []

    async def send(message):
        sent.append(dict(message))

    playback = RealtimeTurnPlayback(
        stream_sid="MZ" + "3" * 32,
        phone_turn=_phone_turn(),
        runtime_turn=runtime,
        bridge=bridge,
        send_message=send,
    )
    with pytest.raises(
        RealtimePlaybackError,
        match="canonical draft has no matching Realtime audio",
    ):
        await playback.run()

    assert runtime.confirmations == []
    assert [message["event"] for message in sent] == ["clear"]


@pytest.mark.asyncio
async def test_preflight_failure_cancels_unbound_audio_wait_without_fallback():
    runtime = FakeRuntimeTurn("")

    class UnstartedBridge(FakeBridge):
        def __init__(self):
            super().__init__()
            self.started = False
            self.waiting = asyncio.Event()

        async def output_pcmu(self):
            self.waiting.set()
            await asyncio.Event().wait()
            if False:
                yield b""

    bridge = UnstartedBridge()
    sent = []

    async def send(message):
        sent.append(dict(message))

    playback = RealtimeTurnPlayback(
        stream_sid="MZ" + "8" * 32,
        phone_turn=_phone_turn(),
        runtime_turn=runtime,
        bridge=bridge,
        send_message=send,
    )
    with pytest.raises(
        RealtimePlaybackError,
        match="canonical runtime completed before Realtime started",
    ):
        await asyncio.wait_for(playback.run(), timeout=1)

    assert bridge.waiting.is_set()
    assert runtime.confirmations == []
    assert [message["event"] for message in sent] == ["clear"]


@pytest.mark.asyncio
async def test_tool_followup_waits_for_native_audio_without_finishing_pending_cycle():
    class ContinuedToolBridge(FakeBridge):
        def __init__(self):
            super().__init__()
            self.finish_calls = 0

        async def output_pcmu(self):
            # Longer than the former 250 ms fallback window: the tool result is
            # being processed on the same Realtime socket before speech starts.
            await asyncio.sleep(0.3)
            yield b"\x7f" * 160

        async def finish_pending_output(self):
            self.finish_calls += 1
            return True

    runtime = FakeRuntimeTurn("Goodbye for now.")
    bridge = ContinuedToolBridge()
    sent = []

    async def send(message):
        sent.append(dict(message))

    playback = RealtimeTurnPlayback(
        stream_sid="MZ" + "9" * 32,
        phone_turn=_phone_turn(),
        runtime_turn=runtime,
        bridge=bridge,
        send_message=send,
    )
    task = asyncio.create_task(playback.run())
    await _wait_for_event(sent, "mark")
    mark = next(
        message["mark"]["name"] for message in sent if message["event"] == "mark"
    )
    await playback.acknowledge_mark(mark)
    result = await task

    assert bridge.finish_calls == 0
    assert result.confirmed_text == "Goodbye for now."
    assert runtime.confirmations == [("Goodbye for now.", 20)]


@pytest.mark.asyncio
async def test_disconnect_persists_even_when_wire_and_provider_cleanup_fail():
    runtime = FakeRuntimeTurn()

    class FailingBridge(FakeBridge):
        async def cancel_output(self):
            raise RuntimeError("provider closed")

        async def truncate_output(self, *, played_ms):
            raise RuntimeError("provider closed")

    async def send(_message):
        raise RuntimeError("wire closed")

    playback = RealtimeTurnPlayback(
        stream_sid="MZ" + "4" * 32,
        phone_turn=_phone_turn(),
        runtime_turn=runtime,
        bridge=FailingBridge(),
        send_message=send,
    )
    result = await playback.disconnect()

    assert result.interrupted is True
    assert runtime.interruptions == [("", 0, "phone_media_disconnected")]


@pytest.mark.asyncio
async def test_disconnect_does_not_confirm_checkpoint_without_twilio_mark():
    clock = AdjustableClock(100.0)
    runtime = FakeRuntimeTurn("Complete item. Unheard item.")
    bridge = BlockingBridge(
        b"\x7f" * 8_000,
        checkpoint_text="Complete item. ",
        checkpoint_ms=240,
    )
    playback = RealtimeTurnPlayback(
        stream_sid="MZ" + "7" * 32,
        phone_turn=_phone_turn(),
        runtime_turn=runtime,
        bridge=bridge,
        send_message=lambda _message: asyncio.sleep(0),
        monotonic=clock,
        call_started_monotonic=99.0,
    )
    task = asyncio.create_task(playback.run())
    await bridge.audio_sent.wait()
    clock.advance(0.4)

    result = await playback.disconnect()
    assert result == await task
    assert bridge.truncated == [320]
    assert runtime.interruptions == [
        ("", 0, "phone_media_disconnected")
    ]
    assert result.confirmed_text == ""
    assert result.played_ms == 0


@pytest.mark.asyncio
async def test_non_owned_mark_is_rejected():
    playback = RealtimeTurnPlayback(
        stream_sid="MZ" + "5" * 32,
        phone_turn=_phone_turn(),
        runtime_turn=FakeRuntimeTurn(),
        bridge=FakeBridge(),
        send_message=lambda _message: asyncio.sleep(0),
    )

    assert playback.owns_mark("someone-else-1") is False
    with pytest.raises(RealtimePlaybackError, match="not owned"):
        await playback.acknowledge_mark("someone-else-1")
