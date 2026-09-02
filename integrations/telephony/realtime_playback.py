"""Confirmation-gated playback for provider-native realtime phone audio.

The realtime provider and Twilio both exchange headerless 8 kHz PCMU.  This
module keeps that audio untouched, frames it for Twilio, and deliberately
commits assistant text only after Twilio confirms the final playback mark.

Realtime audio does not provide trustworthy word-to-audio alignment.  A
barge-in therefore retains no assistant prefix: it clears Twilio, removes the
provider response from its conversation, and commits the canonical caller
turn with an empty assistant prefix.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
import hashlib
import math
import time
from typing import Any, Protocol

from integrations.telephony.audio import (
    PcmuFrame,
    PcmuFrameBuffer,
    pcmu_duration_ceiling_ms,
)
from integrations.telephony.media_streams import (
    build_clear_message,
    build_mark_message,
    build_media_message,
)
from integrations.telephony.phone_context import PhoneChannelTurn
from integrations.telephony.playback import PhonePlaybackResult
from integrations.telephony.recording import LocalCallRecorder
from integrations.telephony.transport import CanonicalPhoneTurn


SendMessage = Callable[[Mapping[str, Any]], Awaitable[None]]
MonotonicClock = Callable[[], float]
DEFAULT_MARK_CONFIRMATION_GRACE_SECONDS = 10.0


class RealtimePcmuBridge(Protocol):
    """Provider-neutral output seam used by :class:`RealtimeTurnPlayback`.

    ``output_pcmu`` yields one completed native model response.

    ``truncate_output(played_ms=0)`` must remove the interrupted assistant
    item from provider-side conversation state.  Zero is intentional: neither
    OpenAI Realtime nor this seam promises exact audio/text alignment.
    """

    def output_pcmu(self) -> AsyncIterator[bytes]: ...

    async def cancel_output(self) -> None: ...

    async def truncate_output(self, *, played_ms: int) -> None: ...

class RealtimePlaybackError(RuntimeError):
    """Realtime phone output could not be resolved durably."""


class _PlaybackInterrupted(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RealtimeMarkConfirmation:
    name: str
    advanced: bool
    played_ms: int


class RealtimeTurnPlayback:
    """Stream native PCMU and gate the canonical commit on one final mark."""

    def __init__(
        self,
        *,
        stream_sid: str,
        phone_turn: PhoneChannelTurn,
        runtime_turn: CanonicalPhoneTurn,
        bridge: RealtimePcmuBridge,
        send_message: SendMessage,
        recorder: LocalCallRecorder | None = None,
        call_started_monotonic: float | None = None,
        monotonic: MonotonicClock = time.monotonic,
        mark_confirmation_grace_seconds: float = (
            DEFAULT_MARK_CONFIRMATION_GRACE_SECONDS
        ),
    ) -> None:
        if runtime_turn.key != phone_turn.context.turn_key:
            raise ValueError("runtime_turn and phone_turn identities do not match")
        self.stream_sid = str(stream_sid)
        self.phone_turn = phone_turn
        self.runtime_turn = runtime_turn
        self.bridge = bridge
        self._send_message = send_message
        self._recorder = recorder
        self._monotonic = monotonic
        now = float(monotonic())
        self._call_started_monotonic = (
            now if call_started_monotonic is None else float(call_started_monotonic)
        )
        if self._call_started_monotonic > now:
            raise ValueError("call_started_monotonic cannot be in the future")
        try:
            mark_confirmation_grace_seconds = float(
                mark_confirmation_grace_seconds
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "mark_confirmation_grace_seconds must be positive"
            ) from exc
        if (
            not math.isfinite(mark_confirmation_grace_seconds)
            or mark_confirmation_grace_seconds <= 0
        ):
            raise ValueError(
                "mark_confirmation_grace_seconds must be positive"
            )
        self._mark_confirmation_grace_seconds = (
            mark_confirmation_grace_seconds
        )

        identity = (
            f"{runtime_turn.key.call_id}\n{runtime_turn.key.turn_id}\nrealtime"
        ).encode("utf-8")
        self._mark_prefix = hashlib.sha256(identity).hexdigest()[:16]
        self._mark_name = f"{self._mark_prefix}-1"

        self._state_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._done = asyncio.Event()
        self._result: PhonePlaybackResult | None = None
        self._error: BaseException | None = None
        self._interrupted = False
        self._output_started = False
        self._mark_sent = False
        self._source_audio_bytes = 0
        self._finalize_task: asyncio.Task[Any] | None = None

    @property
    def output_started(self) -> bool:
        return self._output_started

    def owns_mark(self, name: str) -> bool:
        return name == self._mark_name

    async def run(self) -> PhonePlaybackResult:
        """Forward native output and wait for Twilio's final mark."""

        draft_task: asyncio.Task[Any] | None = None
        audio_task: asyncio.Task[Any] | None = None
        try:
            draft_task = asyncio.create_task(
                self.runtime_turn.wait_for_draft(),
                name=f"realtime-draft-{self._mark_prefix}",
            )
            audio_task = asyncio.create_task(
                self._send_audio_stream(self.bridge.output_pcmu()),
                name=f"realtime-audio-{self._mark_prefix}",
            )
            done, _ = await asyncio.wait(
                (draft_task, audio_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if draft_task in done:
                try:
                    draft_task.result()
                except BaseException:
                    if not audio_task.done():
                        audio_task.cancel()
                        await asyncio.gather(audio_task, return_exceptions=True)
                    raise
                if not audio_task.done() and not bool(
                    getattr(self.bridge, "started", False)
                ):
                    audio_task.cancel()
                    await asyncio.gather(audio_task, return_exceptions=True)
                    raise RealtimePlaybackError(
                        "canonical runtime completed before Realtime started"
                    )
            # A Realtime tool call is continued on the same provider socket.
            # Its runtime draft can therefore finish only after native audio
            # has also reached its response sentinel (or the bridge's bounded
            # response watchdog fails the turn).
            await audio_task
            draft = await draft_task

            if self._interrupted:
                raise _PlaybackInterrupted
            if self._source_audio_bytes == 0 and draft.content:
                raise RealtimePlaybackError(
                    "canonical draft has no matching Realtime audio"
                )

            if not draft.content and self._source_audio_bytes:
                raise RealtimePlaybackError(
                    "realtime audio has no canonical assistant draft"
                )
            if not draft.content:
                ids = await self.runtime_turn.confirm_audible("", played_ms=0)
                self._set_result(PhonePlaybackResult(ids, "", 0, False))
            elif self._source_audio_bytes == 0:
                raise RealtimePlaybackError(
                    "realtime provider produced no phone audio"
                )
            else:
                async with self._state_lock:
                    if self._interrupted:
                        raise _PlaybackInterrupted
                    self._mark_sent = True
                await self._send(
                    build_mark_message(
                        stream_sid=self.stream_sid,
                        name=self._mark_name,
                    )
                )

            mark_timeout_seconds = (
                pcmu_duration_ceiling_ms(self._source_audio_bytes) / 1_000
                + self._mark_confirmation_grace_seconds
            )
            try:
                await asyncio.wait_for(
                    self._done.wait(),
                    timeout=mark_timeout_seconds,
                )
            except TimeoutError:
                # Timeout and mark acknowledgement race under the same state
                # lock inside ``_interrupt``/``acknowledge_mark``.  If the
                # acknowledgement won, return its confirmed result.  Otherwise
                # clear Twilio, cancel/truncate provider output and persist the
                # caller-only turn exactly once before surfacing a terminal
                # transport error.
                timeout_result = await self._interrupt(
                    reason="phone_realtime_mark_timeout",
                    participant_speech=False,
                    tolerate_cleanup_failure=True,
                )
                if timeout_result.interrupted:
                    raise RealtimePlaybackError(
                        "realtime playback mark confirmation timed out"
                    ) from None
        except _PlaybackInterrupted:
            await self._done.wait()
        except asyncio.CancelledError:
            if not self._interrupted:
                cleanup = asyncio.create_task(
                    self._interrupt(
                        reason="phone_realtime_playback_cancelled",
                        participant_speech=False,
                        tolerate_cleanup_failure=True,
                    ),
                    name=f"realtime-cancel-{self._mark_prefix}",
                )
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
            raise
        except BaseException as exc:
            self._error = exc
            if not self._interrupted:
                try:
                    await self._interrupt(
                        reason="phone_realtime_playback_failed",
                        participant_speech=False,
                        tolerate_cleanup_failure=True,
                    )
                except BaseException:
                    pass
            self._done.set()
        finally:
            for task in (audio_task, draft_task):
                if task is not None and not task.done():
                    task.cancel()
            pending = [
                task
                for task in (audio_task, draft_task)
                if task is not None
            ]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        if self._finalize_task is not None:
            try:
                await self._finalize_task
            except BaseException as exc:
                self._error = self._error or exc
        if self._error is not None:
            if isinstance(self._error, RealtimePlaybackError):
                raise self._error
            raise RealtimePlaybackError("realtime phone playback failed") from self._error
        if self._result is None:
            raise RealtimePlaybackError("realtime phone playback ended without a result")
        return self._result

    async def acknowledge_mark(self, name: str) -> RealtimeMarkConfirmation:
        """Confirm the only durable frontier exposed by native audio."""

        if not self.owns_mark(name):
            raise RealtimePlaybackError("realtime playback mark is not owned")
        async with self._state_lock:
            played_ms = pcmu_duration_ceiling_ms(self._source_audio_bytes)
            advanced = bool(
                self._mark_sent
                and not self._interrupted
                and self._result is None
            )
            if advanced and self._finalize_task is None:
                self._finalize_task = asyncio.create_task(
                    self._confirm_final(played_ms),
                    name=f"realtime-confirm-{self._mark_prefix}",
                )
        return RealtimeMarkConfirmation(name, advanced, played_ms if advanced else 0)

    async def barge_in(self) -> PhonePlaybackResult:
        return await self._interrupt(
            reason="barge_in",
            participant_speech=True,
            tolerate_cleanup_failure=False,
        )

    async def disconnect(self) -> PhonePlaybackResult:
        return await self._interrupt(
            reason="phone_media_disconnected",
            participant_speech=False,
            tolerate_cleanup_failure=True,
        )

    async def _send_audio_stream(self, chunks: AsyncIterator[bytes]) -> None:
        frame_buffer = PcmuFrameBuffer()
        recording_start_ms: int | None = None

        async for chunk in chunks:
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise RealtimePlaybackError("realtime PCMU chunk must be bytes")
            raw = bytes(chunk)
            if not raw:
                continue
            self._source_audio_bytes += len(raw)
            for frame in frame_buffer.feed(raw):
                recording_start_ms = await self._send_frame(
                    frame,
                    recording_start_ms=recording_start_ms,
                )
        for frame in frame_buffer.finish():
            recording_start_ms = await self._send_frame(
                frame,
                recording_start_ms=recording_start_ms,
            )

    async def _send_frame(
        self,
        frame: PcmuFrame,
        *,
        recording_start_ms: int | None,
    ) -> int:
        if recording_start_ms is None:
            recording_start_ms = max(
                0,
                int(
                    (float(self._monotonic()) - self._call_started_monotonic)
                    * 1_000
                ),
            )
        async with self._send_lock:
            async with self._state_lock:
                if self._interrupted:
                    raise _PlaybackInterrupted
            await self._send_message(
                build_media_message(stream_sid=self.stream_sid, audio=frame.payload)
            )
            self._output_started = True
        if self._recorder is not None:
            self._recorder.record_assistant(
                frame.payload,
                start_ms=recording_start_ms + frame.start_ms,
            )
        return recording_start_ms

    async def _interrupt(
        self,
        *,
        reason: str,
        participant_speech: bool,
        tolerate_cleanup_failure: bool,
    ) -> PhonePlaybackResult:
        async with self._state_lock:
            if self._result is not None:
                return self._result
            if self._finalize_task is not None or self._interrupted:
                wait_existing = True
            else:
                self._interrupted = True
                self.phone_turn.link_state.mark_interrupted()
                if participant_speech:
                    self.phone_turn.end_controller.on_real_participant_speech()
                wait_existing = False
        if wait_existing:
            await self._done.wait()
            if self._result is None:
                raise RealtimePlaybackError("realtime interruption did not resolve")
            return self._result

        cleanup_errors: list[BaseException] = []
        for cleanup in (
            self._clear_twilio,
            self.bridge.cancel_output,
            lambda: self.bridge.truncate_output(played_ms=0),
        ):
            try:
                await cleanup()
            except BaseException as exc:
                cleanup_errors.append(exc)

        try:
            ids = await self.runtime_turn.interrupt(
                "",
                played_ms=0,
                reason=reason,
            )
        except BaseException:
            try:
                await self.runtime_turn.abort(f"{reason}_persistence_failed")
            except BaseException:
                pass
            self._done.set()
            raise
        result = PhonePlaybackResult(ids, "", 0, True)
        self._set_result(result)
        if cleanup_errors and not tolerate_cleanup_failure:
            raise RealtimePlaybackError(
                "could not clear interrupted realtime phone audio"
            ) from cleanup_errors[0]
        return result

    async def _clear_twilio(self) -> None:
        await self._send(
            build_clear_message(stream_sid=self.stream_sid)
        )

    async def _send(self, message: Mapping[str, Any]) -> None:
        async with self._send_lock:
            await self._send_message(message)

    async def _confirm_final(self, played_ms: int) -> None:
        try:
            draft = await self.runtime_turn.wait_for_draft()
            ids = await self.runtime_turn.confirm_audible(
                draft.content,
                played_ms=played_ms,
            )
            self._set_result(
                PhonePlaybackResult(ids, draft.content, played_ms, False)
            )
        except BaseException as exc:
            self._error = exc
            try:
                await self.runtime_turn.abort("phone_realtime_confirmation_failed")
            except BaseException:
                pass
            self._done.set()

    def _set_result(self, result: PhonePlaybackResult) -> None:
        if self._result is None:
            self._result = result
        elif self._result != result:
            self._error = RealtimePlaybackError(
                "realtime phone playback resolved twice"
            )
        self._done.set()


__all__ = [
    "RealtimeMarkConfirmation",
    "RealtimePcmuBridge",
    "RealtimePlaybackError",
    "RealtimeTurnPlayback",
]
