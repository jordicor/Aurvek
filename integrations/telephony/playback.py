"""Interruptible, confirmation-gated playback for one canonical phone turn."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import hashlib
import time
from typing import Any

from integrations.telephony.audio import (
    PcmuFrame,
    PcmuFrameBuffer,
    iter_pcmu_frames,
)
from integrations.telephony.media_streams import (
    ConservativePlaybackClock,
    MarkConfirmation,
    MediaStreamError,
    PlaybackLedger,
    build_clear_message,
    build_mark_message,
    build_media_message,
)
from integrations.telephony.phone_context import PhoneChannelTurn
from integrations.telephony.recording import LocalCallRecorder
from integrations.telephony.speech import (
    PcmuChunkConsumer,
    PcmuCompleteConsumer,
    PhoneSpeechAsset,
    PhoneTextFragmenter,
)
from integrations.telephony.transport import (
    CanonicalPhoneTurn,
)


SendMessage = Callable[[Mapping[str, Any]], Awaitable[None]]
SpeechRenderer = Callable[[str], Awaitable[PhoneSpeechAsset]]
StreamingSpeechRenderer = Callable[
    [str, PcmuChunkConsumer, PcmuCompleteConsumer],
    Awaitable[PhoneSpeechAsset],
]
MonotonicClock = Callable[[], float]


class PhonePlaybackError(RuntimeError):
    """Phone output could not be aligned and confirmed safely."""


class _PlaybackInterrupted(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PhonePlaybackResult:
    message_ids: tuple[int | None, int | None]
    confirmed_text: str
    played_ms: int
    interrupted: bool


class PhoneTurnPlayback:
    """Stream canonical text through exact-voice TTS and Twilio marks."""

    def __init__(
        self,
        *,
        stream_sid: str,
        phone_turn: PhoneChannelTurn,
        runtime_turn: CanonicalPhoneTurn,
        render_speech: SpeechRenderer,
        stream_render_speech: StreamingSpeechRenderer | None = None,
        send_message: SendMessage,
        recorder: LocalCallRecorder | None = None,
        call_started_monotonic: float | None = None,
        monotonic: MonotonicClock = time.monotonic,
        fragmenter: PhoneTextFragmenter | None = None,
        ledger: PlaybackLedger | None = None,
        playback_clock: ConservativePlaybackClock | None = None,
    ) -> None:
        if runtime_turn.key != phone_turn.context.turn_key:
            raise ValueError("runtime_turn and phone_turn identities do not match")
        self.stream_sid = str(stream_sid)
        self.phone_turn = phone_turn
        self.runtime_turn = runtime_turn
        self._render_speech = render_speech
        self._stream_render_speech = stream_render_speech
        self._send_message = send_message
        self._recorder = recorder
        self._monotonic = monotonic
        now = float(monotonic())
        self._call_started_monotonic = (
            now if call_started_monotonic is None else float(call_started_monotonic)
        )
        if self._call_started_monotonic > now:
            raise ValueError("call_started_monotonic cannot be in the future")
        # Smaller than chat paragraphs so a barge-in never claims an entire
        # long sentence merely because part of it was heard.
        self._fragmenter = fragmenter or PhoneTextFragmenter(
            min_chars=12,
            max_chars=80,
        )
        self.ledger = ledger or PlaybackLedger()
        self.playback_clock = playback_clock or ConservativePlaybackClock()
        self._state_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._capacity_changed = asyncio.Event()
        self._done = asyncio.Event()
        self._result: PhonePlaybackResult | None = None
        self._error: BaseException | None = None
        self._interrupted = False
        self._output_started = False
        self._final_mark_name: str | None = None
        self._last_mark_name: str | None = None
        self._last_confirmation: MarkConfirmation | None = None
        self._playback_call_start_ms: int | None = None
        self._finalize_task: asyncio.Task[Any] | None = None
        key = runtime_turn.key
        identity = f"{key.call_id}\n{key.turn_id}".encode("utf-8")
        self._mark_prefix = hashlib.sha256(identity).hexdigest()[:16]
        self._mark_counter = 0

    @property
    def interrupted(self) -> bool:
        return self._interrupted

    @property
    def output_started(self) -> bool:
        """Return whether at least one media frame reached Twilio."""

        return self._output_started

    def owns_mark(self, name: str) -> bool:
        """Return whether a Twilio mark belongs to this turn's namespace."""

        prefix = f"{self._mark_prefix}-"
        if not name.startswith(prefix):
            return False
        return name[len(prefix) :].isdigit()

    async def run(self) -> PhonePlaybackResult:
        """Render provisional output, then wait for a final mark or barge-in."""

        streamed_parts: list[str] = []
        try:
            async for event in self.runtime_turn.events_until_draft():
                if event.persistence_error:
                    raise PhonePlaybackError(
                        "canonical runtime reported a persistence error"
                    )
                if event.content:
                    streamed_parts.append(event.content)
                    for fragment in self._fragmenter.feed(event.content):
                        await self._render_and_send(fragment)

            draft = await self.runtime_turn.wait_for_draft()
            for fragment in self._fragmenter.finish():
                await self._render_and_send(fragment)
            streamed_text = "".join(streamed_parts)
            if streamed_text != draft.content:
                raise PhonePlaybackError(
                    "provisional phone text does not match the canonical draft"
                )

            if not draft.content:
                ids = await self.runtime_turn.confirm_audible("", played_ms=0)
                self._set_result(
                    PhonePlaybackResult(ids, "", 0, interrupted=False)
                )
            else:
                async with self._state_lock:
                    if self._interrupted:
                        raise _PlaybackInterrupted
                    if self._last_mark_name is None:
                        raise PhonePlaybackError(
                            "canonical phone text produced no marked audio"
                        )
                    self._final_mark_name = self._last_mark_name
                    confirmation = self._last_confirmation
                    if (
                        confirmation is not None
                        and confirmation.name == self._final_mark_name
                    ):
                        self._schedule_final_confirmation(confirmation)

            await self._done.wait()
        except _PlaybackInterrupted:
            await self._done.wait()
        except asyncio.CancelledError:
            if not self._interrupted:
                cleanup_task = asyncio.create_task(
                    self._persist_interruption(
                        reason="phone_playback_cancelled",
                        participant_speech=False,
                        tolerate_clear_failure=True,
                    ),
                    name=f"phone-cancel-persist-{self._mark_prefix}",
                )
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    # A second task cancellation must not tear down the one
                    # durable fallback that preserves the caller transcript.
                    await cleanup_task
            raise
        except BaseException as exc:
            if self._interrupted:
                await self._done.wait()
            else:
                self._error = exc
                try:
                    await self._persist_interruption(
                        reason="phone_playback_failed",
                        participant_speech=False,
                        tolerate_clear_failure=True,
                    )
                except BaseException:
                    # _persist_interruption aborts only when its durable
                    # caller/prefix commit fails.
                    pass
                self._done.set()

        if self._finalize_task is not None:
            try:
                await self._finalize_task
            except BaseException as exc:
                self._error = self._error or exc
        if self._error is not None:
            if isinstance(self._error, PhonePlaybackError):
                raise self._error
            raise PhonePlaybackError("phone playback failed") from self._error
        if self._result is None:
            raise PhonePlaybackError("phone playback ended without a result")
        return self._result

    async def acknowledge_mark(self, name: str) -> MarkConfirmation:
        """Apply one Twilio mark; a mark returned by ``clear`` never expands text."""

        async with self._state_lock:
            confirmation = self.ledger.acknowledge_mark(name)
            self.playback_clock.note_mark_confirmed(confirmation.played_ms)
            self._last_confirmation = confirmation
            self._capacity_changed.set()
            if (
                not self._interrupted
                and self._final_mark_name == confirmation.name
            ):
                self._schedule_final_confirmation(confirmation)
            return confirmation

    async def barge_in(self) -> PhonePlaybackResult:
        """Clear queued audio and commit only the conservative audible prefix."""

        return await self._persist_interruption(
            reason="barge_in",
            participant_speech=True,
            tolerate_clear_failure=False,
        )

    async def disconnect(self) -> PhonePlaybackResult:
        """Persist the safe frontier when the media transport disappears.

        A disconnect is not participant speech: it must not reset the silence
        watchdog or revoke a voluntary ``end_call`` request.  A closed
        WebSocket may reject ``clear`` during cleanup, but that cannot prevent
        the canonical caller message from being written.
        """

        return await self._persist_interruption(
            reason="phone_media_disconnected",
            participant_speech=False,
            tolerate_clear_failure=True,
        )

    async def _persist_interruption(
        self,
        *,
        reason: str,
        participant_speech: bool,
        tolerate_clear_failure: bool,
    ) -> PhonePlaybackResult:
        """Freeze playback and durably resolve the canonical turn once."""

        async with self._state_lock:
            if self._result is not None:
                existing = self._result
                wait_existing = False
            elif self._finalize_task is not None:
                # Twilio already confirmed the final playback frontier.  New
                # speech belongs to the next caller turn, not to an unheard
                # prefix race while the database commit finishes.
                existing = None
                wait_existing = True
            elif self._interrupted:
                existing = None
                wait_existing = True
            else:
                if self.ledger.fragments:
                    try:
                        outcome = self.ledger.barge_in(
                            stream_sid=self.stream_sid,
                            playback_clock=self.playback_clock,
                            observed_at=float(self._monotonic()),
                        )
                    except MediaStreamError as exc:
                        raise PhonePlaybackError(
                            "could not freeze playback frontier"
                        ) from exc
                    clear_message = outcome.clear_message
                    # Twilio may have played part of the first alignment unit.
                    # That elapsed time is useful only as transport telemetry:
                    # without one complete text fragment there is no exact
                    # assistant prefix that Aurvek may persist.
                    confirmed_text = outcome.text_prefix
                    confirmed_ms = outcome.played_ms if confirmed_text else 0
                else:
                    clear_message = build_clear_message(stream_sid=self.stream_sid)
                    confirmed_text = ""
                    confirmed_ms = 0
                self._interrupted = True
                self.phone_turn.link_state.mark_interrupted()
                if participant_speech:
                    self.phone_turn.end_controller.on_real_participant_speech()
                self._capacity_changed.set()
                existing = None
                wait_existing = False
        if existing is not None:
            return existing
        if wait_existing:
            await self._done.wait()
            if self._error is not None:
                raise PhonePlaybackError("barge-in persistence failed") from self._error
            if self._result is None:
                raise PhonePlaybackError("barge-in ended without a result")
            return self._result

        try:
            await self._send(clear_message)
        except BaseException:
            if not tolerate_clear_failure:
                # Persistence remains authoritative even if Twilio rejected
                # clear; report the send failure only after resolving it.
                clear_failed = True
            else:
                clear_failed = False
        else:
            clear_failed = False
        try:
            ids = await self.runtime_turn.interrupt(
                confirmed_text,
                played_ms=confirmed_ms,
                reason=reason,
            )
        except BaseException:
            try:
                await self.runtime_turn.abort(f"{reason}_persistence_failed")
            except BaseException:
                pass
            raise
        result = PhonePlaybackResult(
            ids,
            confirmed_text,
            confirmed_ms,
            interrupted=True,
        )
        self._set_result(result)
        if clear_failed:
            raise PhonePlaybackError("could not clear interrupted phone audio")
        return result

    async def _render_and_send(self, text: str) -> None:
        if self._interrupted:
            raise _PlaybackInterrupted
        asset: PhoneSpeechAsset | None = None
        if self._stream_render_speech is not None:
            streamed, asset = await self._stream_render_and_send(text)
            if streamed:
                return
        if asset is None:
            asset = await self._render_speech(text)
        if asset.text != text:
            raise PhonePlaybackError("TTS renderer changed canonical fragment text")
        if not asset.pcmu:
            raise PhonePlaybackError("TTS renderer returned no PCMU audio")
        frames = tuple(iter_pcmu_frames(asset.pcmu))
        wire_audio = b"".join(frame.payload for frame in frames)

        while True:
            async with self._state_lock:
                if self._interrupted:
                    raise _PlaybackInterrupted
                pending = self.ledger.backpressure
                if len(wire_audio) > pending.max_bytes:
                    raise PhonePlaybackError(
                        "one TTS fragment exceeds the playback buffer"
                    )
                fits = (
                    pending.pending_bytes + len(wire_audio) <= pending.max_bytes
                    and pending.pending_fragments + 1 <= pending.max_fragments
                )
                if fits:
                    fragment = self.ledger.append_fragment(
                        text=text,
                        audio=wire_audio,
                    )
                    self._mark_counter += 1
                    mark_name = f"{self._mark_prefix}-{self._mark_counter}"
                    self.ledger.bind_mark(mark_name)
                    self._last_mark_name = mark_name
                    if self._playback_call_start_ms is None:
                        self._playback_call_start_ms = max(
                            0,
                            int(
                                (
                                    float(self._monotonic())
                                    - self._call_started_monotonic
                                )
                                * 1_000
                            ),
                        )
                    recording_start_ms = self._playback_call_start_ms + int(
                        fragment.start_ms
                    )
                    self._capacity_changed.clear()
                    break
            await self._capacity_changed.wait()

        for frame in frames:
            if self._interrupted:
                raise _PlaybackInterrupted
            sent_at = float(self._monotonic())
            await self._send(
                build_media_message(
                    stream_sid=self.stream_sid,
                    audio=frame.payload,
                )
            )
            self._output_started = True
            self.playback_clock.note_audio_sent(frame.payload, sent_at=sent_at)
            if self._recorder is not None:
                self._recorder.record_assistant(
                    frame.payload,
                    start_ms=recording_start_ms + frame.start_ms,
                )
        if self._interrupted:
            raise _PlaybackInterrupted
        await self._send(
            build_mark_message(stream_sid=self.stream_sid, name=mark_name)
        )

    async def _stream_render_and_send(
        self,
        text: str,
    ) -> tuple[bool, PhoneSpeechAsset]:
        """Forward a native PCMU cache miss while ElevenLabs is producing it.

        Cache hits intentionally invoke neither callback and fall back to the
        complete-asset path.  Until a streamed fragment is complete it is not
        an exact text/audio alignment unit, so a barge-in may retain earlier
        complete fragments but never claims part of this one.
        """

        frame_buffer = PcmuFrameBuffer()
        provider_audio = bytearray()
        wire_audio = bytearray()
        stream_completed = False
        recording_start_ms: int | None = None

        async def send_frame(frame: PcmuFrame) -> None:
            nonlocal recording_start_ms
            while True:
                async with self._state_lock:
                    if self._interrupted:
                        raise _PlaybackInterrupted
                    pending = self.ledger.backpressure
                    prospective_bytes = len(wire_audio) + len(frame.payload)
                    if prospective_bytes > pending.max_bytes:
                        raise PhonePlaybackError(
                            "one streamed TTS fragment exceeds the playback buffer"
                        )
                    fits = (
                        pending.pending_bytes + prospective_bytes
                        <= pending.max_bytes
                        and pending.pending_fragments + 1
                        <= pending.max_fragments
                    )
                    if fits:
                        if self._playback_call_start_ms is None:
                            self._playback_call_start_ms = max(
                                0,
                                int(
                                    (
                                        float(self._monotonic())
                                        - self._call_started_monotonic
                                    )
                                    * 1_000
                                ),
                            )
                        recording_start_ms = self._playback_call_start_ms + int(
                            self.ledger.duration_ms
                        )
                        self._capacity_changed.clear()
                        break
                await self._capacity_changed.wait()

            if self._interrupted:
                raise _PlaybackInterrupted
            sent_at = float(self._monotonic())
            await self._send(
                build_media_message(
                    stream_sid=self.stream_sid,
                    audio=frame.payload,
                )
            )
            # `output_started` means Twilio accepted at least one complete
            # frame, identically for buffered and provider-streamed playback.
            self._output_started = True
            self.playback_clock.note_audio_sent(frame.payload, sent_at=sent_at)
            wire_audio.extend(frame.payload)
            if self._recorder is not None:
                assert recording_start_ms is not None
                self._recorder.record_assistant(
                    frame.payload,
                    start_ms=recording_start_ms + frame.start_ms,
                )

        async def on_pcmu_chunk(chunk: bytes) -> None:
            if self._interrupted:
                raise _PlaybackInterrupted
            provider_audio.extend(chunk)
            for frame in frame_buffer.feed(chunk):
                await send_frame(frame)

        async def on_pcmu_complete(pcmu: bytes) -> None:
            nonlocal stream_completed
            if bytes(provider_audio) != pcmu:
                raise PhonePlaybackError(
                    "streamed TTS bytes changed before completion"
                )
            for frame in frame_buffer.finish():
                await send_frame(frame)
            if not wire_audio:
                raise PhonePlaybackError("TTS renderer returned no PCMU audio")

            while True:
                async with self._state_lock:
                    if self._interrupted:
                        raise _PlaybackInterrupted
                    pending = self.ledger.backpressure
                    fits = (
                        pending.pending_bytes + len(wire_audio)
                        <= pending.max_bytes
                        and pending.pending_fragments + 1
                        <= pending.max_fragments
                    )
                    if fits:
                        self.ledger.append_fragment(
                            text=text,
                            audio=bytes(wire_audio),
                        )
                        self._mark_counter += 1
                        mark_name = f"{self._mark_prefix}-{self._mark_counter}"
                        self.ledger.bind_mark(mark_name)
                        self._last_mark_name = mark_name
                        self._capacity_changed.clear()
                        break
                await self._capacity_changed.wait()
            if self._interrupted:
                raise _PlaybackInterrupted
            await self._send(
                build_mark_message(stream_sid=self.stream_sid, name=mark_name)
            )
            stream_completed = True

        asset = await self._stream_render_speech(
            text,
            on_pcmu_chunk,
            on_pcmu_complete,
        )
        if asset.text != text:
            raise PhonePlaybackError("TTS renderer changed canonical fragment text")
        if not provider_audio:
            # Native cache hit or a durable non-streaming provider snapshot.
            return False, asset
        if not stream_completed:
            raise PhonePlaybackError("streamed TTS ended without a final frontier")
        if bytes(provider_audio) != asset.pcmu:
            raise PhonePlaybackError("streamed TTS differs from cached audio")
        return True, asset

    async def _send(self, message: Mapping[str, Any]) -> None:
        async with self._send_lock:
            await self._send_message(message)

    def _schedule_final_confirmation(
        self,
        confirmation: MarkConfirmation,
    ) -> None:
        if self._finalize_task is not None:
            return
        self._finalize_task = asyncio.create_task(
            self._confirm_final(confirmation),
            name=f"phone-confirm-{self._mark_prefix}",
        )

    async def _confirm_final(self, confirmation: MarkConfirmation) -> None:
        try:
            draft = await self.runtime_turn.wait_for_draft()
            if confirmation.drained_after_clear or not confirmation.advanced:
                raise PhonePlaybackError(
                    "final playback mark was not a drained audio confirmation"
                )
            if confirmation.text_prefix != draft.content:
                raise PhonePlaybackError(
                    "final playback mark does not cover the canonical draft"
                )
            ids = await self.runtime_turn.confirm_audible(
                confirmation.text_prefix,
                played_ms=confirmation.played_ms,
            )
            self._set_result(
                PhonePlaybackResult(
                    ids,
                    confirmation.text_prefix,
                    confirmation.played_ms,
                    interrupted=False,
                )
            )
        except BaseException as exc:
            self._error = exc
            try:
                await self.runtime_turn.abort("phone_confirmation_failed")
            except BaseException:
                pass
            self._done.set()

    def _set_result(self, result: PhonePlaybackResult) -> None:
        if self._result is None:
            self._result = result
        elif self._result != result:
            self._error = PhonePlaybackError("phone playback resolved twice")
        self._done.set()


__all__ = [
    "PhonePlaybackError",
    "PhonePlaybackResult",
    "PhoneTurnPlayback",
]
