"""Cache-only phone audio playback and production cache resolution.

This module is the transport adapter between activated ``CachedPhoneAudio``
rows and Twilio Media Streams.  It never renders TTS and never falls back to
an LLM response: missing or mismatched cache data is a hard readiness error.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import hashlib
import time
from typing import Any, TYPE_CHECKING

from ai_runtime.voice_resolution import resolve_default_voice
from database import get_db_connection
from integrations.telephony.audio import iter_pcmu_frames
from integrations.telephony.greetings import (
    CachedPhoneAudio,
    GLOBAL_AUDIO_REVISION_CONFIG_KEY,
    PROMPT_TECHNICAL_NOTICE_KEYS,
    load_cached_technical_notice,
    select_cached_greeting,
)
from integrations.telephony.media_streams import (
    ConservativePlaybackClock,
    MediaStreamError,
    PlaybackLedger,
    build_clear_message,
    build_mark_message,
    build_media_message,
)
from integrations.telephony.provider_repository import TelephonyProviderRepository
from integrations.telephony.recording import LocalCallRecorder
from integrations.telephony.snapshot import (
    canonical_voice_from_snapshot,
    tts_profile_from_snapshot,
)
from tools.tts_config import get_tts_profile

if TYPE_CHECKING:
    from integrations.telephony.session import PhoneMediaSessionContext


SendMessage = Callable[[Mapping[str, Any]], Awaitable[None]]
PersistAudiblePrefix = Callable[[str, int, bool], Awaitable[int | None]]


class CachedAudioPlaybackError(RuntimeError):
    """Activated phone audio could not be confirmed safely."""


@dataclass(frozen=True, slots=True)
class CachedAudioPlaybackResult:
    confirmed_text: str
    played_ms: int
    interrupted: bool
    message_id: int | None = None


class CachedAudioPlayback:
    """Play one private PCMU asset and resolve only at a Twilio mark."""

    def __init__(
        self,
        asset: CachedPhoneAudio,
        *,
        persist_audible_prefix: PersistAudiblePrefix | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        confirmation_timeout_seconds: float = 30.0,
    ) -> None:
        if confirmation_timeout_seconds <= 0:
            raise ValueError("confirmation_timeout_seconds must be positive")
        self.asset = asset
        self._persist = persist_audible_prefix
        self._monotonic = monotonic
        self._timeout = float(confirmation_timeout_seconds)
        self.ledger = PlaybackLedger()
        self.playback_clock = ConservativePlaybackClock()
        self._state_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._done = asyncio.Event()
        self._send_message: SendMessage | None = None
        self._stream_sid: str | None = None
        self._result: CachedAudioPlaybackResult | None = None
        self._error: BaseException | None = None
        self._interrupted = False
        identity = f"{asset.cache_id}\n{asset.content_hash}".encode("utf-8")
        self._mark_name = f"cache-{hashlib.sha256(identity).hexdigest()[:20]}"

    def owns_mark(self, name: str) -> bool:
        """Return whether a Twilio mark was issued by this cached playback."""

        return name == self._mark_name

    @property
    def literal_text(self) -> str:
        """The configured phrase selected for this cache-backed playback."""

        return self.asset.literal_text

    async def persist_audible_prefix(
        self,
        text: str,
        played_ms: int,
        interrupted: bool,
    ) -> int | None:
        """Expose the existing greeting commit hook to alternate renderers."""

        if self._persist is None:
            return None
        return await self._persist(str(text), int(played_ms), bool(interrupted))

    async def run(
        self,
        *,
        stream_sid: str,
        send_message: SendMessage,
        recorder: LocalCallRecorder,
        call_started_monotonic: float,
    ) -> CachedAudioPlaybackResult:
        audio = self.asset.read_pcmu()
        frames = tuple(iter_pcmu_frames(audio))
        if not frames:
            raise CachedAudioPlaybackError("cached phone audio has no frames")
        now = float(self._monotonic())
        playback_start_ms = max(
            0, int((now - float(call_started_monotonic)) * 1_000)
        )
        async with self._state_lock:
            if self._send_message is not None:
                raise CachedAudioPlaybackError("cached audio playback already started")
            self._stream_sid = str(stream_sid)
            self._send_message = send_message
            self.ledger.append_fragment(text=self.asset.literal_text, audio=audio)
            self.ledger.bind_mark(self._mark_name)
        try:
            for frame in frames:
                if self._interrupted:
                    break
                await self._send(
                    build_media_message(stream_sid=str(stream_sid), audio=frame.payload)
                )
                self.playback_clock.note_audio_sent(
                    frame.payload, sent_at=float(self._monotonic())
                )
                recorder.record_assistant(
                    frame.payload,
                    start_ms=playback_start_ms + frame.start_ms,
                )
            if not self._interrupted:
                await self._send(
                    build_mark_message(
                        stream_sid=str(stream_sid), name=self._mark_name
                    )
                )
            await asyncio.wait_for(self._done.wait(), timeout=self._timeout)
        except TimeoutError as exc:
            raise CachedAudioPlaybackError(
                "Twilio did not confirm cached phone audio"
            ) from exc
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise CachedAudioPlaybackError("cached phone audio playback failed") from exc
        if self._error is not None:
            raise CachedAudioPlaybackError("cached phone audio persistence failed") from self._error
        if self._result is None:
            raise CachedAudioPlaybackError("cached phone audio ended without a result")
        return self._result

    async def acknowledge_mark(self, name: str) -> CachedAudioPlaybackResult | None:
        async with self._state_lock:
            if self._result is not None:
                return self._result
            if name != self._mark_name:
                return None
            try:
                confirmation = self.ledger.acknowledge_mark(name)
            except MediaStreamError as exc:
                raise CachedAudioPlaybackError("cached audio mark is invalid") from exc
            if confirmation.drained_after_clear or not confirmation.advanced:
                return None
            text = confirmation.text_prefix
            played_ms = int(confirmation.played_ms)
        return await self._finish(
            text=text,
            played_ms=played_ms,
            interrupted=False,
        )

    async def barge_in(self) -> CachedAudioPlaybackResult:
        return await self._interrupt(tolerate_clear_failure=False)

    async def disconnect(self) -> CachedAudioPlaybackResult:
        return await self._interrupt(tolerate_clear_failure=True)

    async def _interrupt(
        self, *, tolerate_clear_failure: bool
    ) -> CachedAudioPlaybackResult:
        async with self._state_lock:
            if self._result is not None:
                return self._result
            stream_sid = self._stream_sid
            if stream_sid is None:
                self._interrupted = True
                return await self._finish_locked("", 0, True)
            try:
                outcome = self.ledger.barge_in(
                    stream_sid=stream_sid,
                    playback_clock=self.playback_clock,
                    observed_at=float(self._monotonic()),
                )
                # Character timings are validated when the cache activates.
                # They provide a finer, still conservative prefix than the
                # transport ledger's whole-asset alignment unit.
                text = self.asset.audible_prefix(int(outcome.played_ms))
                played_ms = int(outcome.played_ms) if text else 0
                clear = outcome.clear_message
            except MediaStreamError:
                text = ""
                played_ms = 0
                clear = build_clear_message(stream_sid=stream_sid)
            self._interrupted = True
        try:
            await self._send(clear)
        except Exception:
            if not tolerate_clear_failure:
                raise
        return await self._finish(
            text=text,
            played_ms=played_ms,
            interrupted=True,
        )

    async def _finish(
        self, *, text: str, played_ms: int, interrupted: bool
    ) -> CachedAudioPlaybackResult:
        async with self._state_lock:
            return await self._finish_locked(text, played_ms, interrupted)

    async def _finish_locked(
        self, text: str, played_ms: int, interrupted: bool
    ) -> CachedAudioPlaybackResult:
        if self._result is not None:
            return self._result
        try:
            message_id = (
                await self._persist(text, int(played_ms), bool(interrupted))
                if self._persist is not None and text
                else None
            )
            self._result = CachedAudioPlaybackResult(
                confirmed_text=text,
                played_ms=int(played_ms),
                interrupted=bool(interrupted),
                message_id=message_id,
            )
        except BaseException as exc:
            self._error = exc
            self._done.set()
            raise
        self._done.set()
        return self._result

    async def _send(self, message: Mapping[str, Any]) -> None:
        sender = self._send_message
        if sender is None:
            raise CachedAudioPlaybackError("cached audio sender is unavailable")
        async with self._send_lock:
            await sender(message)


class PhoneCachedAudioBackend:
    """Resolve activated assets using the immutable call voice/profile."""

    def __init__(
        self,
        repository: TelephonyProviderRepository,
        *,
        connection_factory: Callable[..., Any] = get_db_connection,
    ) -> None:
        self.repository = repository
        self._connection_factory = connection_factory

    async def load_greeting(
        self, context: PhoneMediaSessionContext
    ) -> CachedAudioPlayback:
        call = await self.repository.get_call_by_provider_sid(
            context.provider_call_sid
        )
        if call is None or str(call["id"]) != context.call_id:
            raise CachedAudioPlaybackError("phone call is unavailable")
        previous = await self.repository.previous_greeting_id(
            call_id=context.call_id,
            contact_id=int(call["contact_id"]),
            direction=context.direction,
        )
        voice = canonical_voice_from_snapshot(context.call_snapshot)
        profile = tts_profile_from_snapshot(context.call_snapshot)
        revision = _captured_audio_revision(context.call_snapshot)
        greeting_mode = str(
            context.call_snapshot[f"{context.direction}_greeting_mode"]
        )
        async with self._connection_factory(readonly=True) as conn:
            asset = await select_cached_greeting(
                conn,
                prompt_id=int(context.call_snapshot["prompt_id"]),
                direction=context.direction,
                revision=revision,
                greeting_mode=greeting_mode,
                previous_greeting_id=previous,
                voice=voice,
                profile=profile,
            )
        await self.repository.pin_call_audio_revision(
            call_id=context.call_id,
            provider_call_sid=context.provider_call_sid,
            audio_revision=asset.audio_revision,
        )

        async def persist(text: str, played_ms: int, interrupted: bool) -> int | None:
            return await self.repository.persist_greeting_prefix(
                call_id=context.call_id,
                greeting_id=int(asset.greeting_id),
                confirmed_text=text,
                played_ms=played_ms,
                interrupted=interrupted,
                fencing_token=context.foreground_epoch,
                lease_owner=context.foreground_lease_owner,
            )

        return CachedAudioPlayback(asset, persist_audible_prefix=persist)

    async def load_notice(
        self, context: PhoneMediaSessionContext, notice_key: str
    ) -> CachedAudioPlayback:
        asset = await self.load_notice_asset(context, notice_key)
        return CachedAudioPlayback(asset)

    async def load_notice_asset(
        self, context: PhoneMediaSessionContext, notice_key: str
    ) -> CachedPhoneAudio:
        """Resolve one exact call-scoped notice without starting playback."""

        revision = _captured_audio_revision(context.call_snapshot)
        voice = canonical_voice_from_snapshot(context.call_snapshot)
        profile = tts_profile_from_snapshot(context.call_snapshot)
        async with self._connection_factory(readonly=True) as conn:
            return await load_cached_technical_notice(
                conn,
                prompt_id=int(context.call_snapshot["prompt_id"]),
                notice_key=notice_key,
                voice=voice,
                profile=profile,
                revision=revision,
            )

    async def probe_context(self, context: PhoneMediaSessionContext) -> bool:
        """Prove greeting and the complete prompt notice set without fallback."""

        try:
            voice = canonical_voice_from_snapshot(context.call_snapshot)
            profile = tts_profile_from_snapshot(context.call_snapshot)
            revision = _captured_audio_revision(context.call_snapshot)
            greeting_mode = str(
                context.call_snapshot[f"{context.direction}_greeting_mode"]
            )
            async with self._connection_factory(readonly=True) as conn:
                await select_cached_greeting(
                    conn,
                    prompt_id=int(context.call_snapshot["prompt_id"]),
                    direction=context.direction,
                    revision=revision,
                    greeting_mode=greeting_mode,
                    voice=voice,
                    profile=profile,
                )
                for key in sorted(PROMPT_TECHNICAL_NOTICE_KEYS):
                    await load_cached_technical_notice(
                        conn,
                        prompt_id=int(context.call_snapshot["prompt_id"]),
                        notice_key=key,
                        voice=voice,
                        profile=profile,
                        revision=revision,
                    )
            return True
        except Exception:
            return False

    async def load_unknown_notice(self) -> CachedPhoneAudio:
        voice = await resolve_default_voice()
        profile = await get_tts_profile("external")
        async with self._connection_factory(readonly=True) as conn:
            revision = await _global_audio_revision(conn)
            return await load_cached_technical_notice(
                conn,
                prompt_id=None,
                notice_key="unknown_caller",
                voice=voice,
                profile=profile,
                revision=revision,
            )

    async def load_inbound_unavailable_notice(self) -> CachedPhoneAudio:
        voice = await resolve_default_voice()
        profile = await get_tts_profile("external")
        async with self._connection_factory(readonly=True) as conn:
            revision = await _global_audio_revision(conn)
            return await load_cached_technical_notice(
                conn,
                prompt_id=None,
                notice_key="inbound_unavailable",
                voice=voice,
                profile=profile,
                revision=revision,
            )

    async def global_ready(self) -> bool:
        try:
            await self.load_unknown_notice()
            return True
        except Exception:
            return False


def _captured_audio_revision(snapshot: Mapping[str, Any]) -> int:
    try:
        revision = int(snapshot["audio_revision"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CachedAudioPlaybackError(
            "call snapshot has no captured audio revision"
        ) from exc
    if revision <= 0:
        raise CachedAudioPlaybackError("call audio revision is invalid")
    return revision


async def _global_audio_revision(conn: Any) -> int:
    cursor = await conn.execute(
        "SELECT value FROM SYSTEM_CONFIG WHERE key=?",
        (GLOBAL_AUDIO_REVISION_CONFIG_KEY,),
    )
    row = await cursor.fetchone()
    try:
        revision = int(row[0])
    except (TypeError, ValueError) as exc:
        raise CachedAudioPlaybackError(
            "global phone audio revision is unavailable"
        ) from exc
    if revision <= 0:
        raise CachedAudioPlaybackError("global phone audio revision is invalid")
    return revision


__all__ = [
    "CachedAudioPlayback",
    "CachedAudioPlaybackError",
    "CachedAudioPlaybackResult",
    "PhoneCachedAudioBackend",
]
