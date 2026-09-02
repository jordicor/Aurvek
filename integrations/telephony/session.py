"""One authenticated Twilio Media Streams session.

This is deliberately an orchestration layer.  It joins the already-tested
wire parser, live transcription adapter, canonical Aurvek runtime and
confirmation-gated playback without implementing a second conversational
engine.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
import math
import re
import time
import unicodedata
from typing import Any, Protocol

from database import DB_MAX_RETRIES, DB_RETRY_DELAY_BASE, DEFAULT_DB_TIMEOUT
from log_config import logger

from integrations.telephony.async_shutdown import cancel_and_join_tasks
from integrations.telephony.billing import (
    PhoneBillingError,
    PhoneBillingExhausted,
    PhoneBillingService,
    PhoneLiveBillingMeter,
)
from integrations.telephony.clock import (
    CallEndController,
    DeterministicCallClock,
    DeterministicSilenceWatchdog,
    EndCallDirective,
    EndCallReason,
    SilenceDirectiveKind,
    build_phone_pre_watchdog_context,
)
from integrations.telephony.elevenlabs_realtime import (
    ApiKeyProvider,
    ElevenLabsMetadataEvent,
    ElevenLabsRealtimeClient,
    ElevenLabsRealtimeOptions,
    ElevenLabsSpeechStartedEvent,
    ElevenLabsUtteranceEndEvent,
    ElevenLabsWarningEvent,
)
from integrations.telephony.foreground import ForegroundCommitGuard
from integrations.telephony.greetings import technical_notice_key_for_end_reason
from integrations.telephony.media_streams import (
    ConnectedEvent,
    MarkEvent,
    MediaEvent,
    MediaStreamParser,
    StartEvent,
    StopEvent,
)
from integrations.telephony.phone_context import (
    PhoneChannelTurn,
    create_phone_channel_turn,
)
from integrations.telephony.playback import PhonePlaybackError, PhoneTurnPlayback
from integrations.telephony.realtime_call import (
    OpenAIRealtimeCallBridge,
    RealtimeCallSpeechStartedEvent,
)
from integrations.telephony.realtime_playback import (
    RealtimePlaybackError,
    RealtimeTurnPlayback,
)
from integrations.telephony.provider_repository import TelephonyProviderRepository
from integrations.telephony.recording import LocalCallRecorder
from integrations.telephony.snapshot import (
    phone_settings_from_snapshot,
    realtime_voice_from_snapshot,
    reasoning_selection_from_snapshot,
    runtime_kind_from_snapshot,
    runtime_llm_id_from_snapshot,
    runtime_model_from_snapshot,
)
from integrations.telephony.schemas import PhoneCallStatus
from integrations.telephony.speech import (
    PhoneSpeechBillingExhausted,
    render_phone_speech,
)
from integrations.telephony.transcription import (
    FinalPhoneUtterance,
    PhoneUtteranceAssembler,
)
from integrations.telephony.transport import (
    CanonicalPhoneTurn,
    persist_canonical_phone_caller_turn,
    start_canonical_phone_turn,
)


class MediaWebSocket(Protocol):
    async def receive_text(self) -> str: ...

    async def send_json(self, data: Any) -> None: ...


class RealtimeSttClient(Protocol):
    """Narrow transport contract owned by one phone media session."""

    options: Any

    async def connect(self) -> Any: ...

    async def send_audio(self, audio: bytes) -> None: ...

    async def finalize(self) -> None: ...

    def events(self) -> AsyncIterator[object]: ...

    async def close(self) -> None: ...


HangupCall = Callable[[str], Awaitable[bool]]
CurrentUserLoader = Callable[[int], Awaitable[Any]]
RuntimeStarter = Callable[..., Awaitable[CanonicalPhoneTurn]]
CallerTurnPersister = Callable[..., Awaitable[int]]
Sleep = Callable[[float], Awaitable[None]]

_FOREGROUND_LEASE_SECONDS = 120
_FOREGROUND_LEASE_GRACE_SECONDS = 120
_FOREGROUND_RENEW_INTERVAL_SECONDS = 30.0
_STOP_FINAL_DRAIN_TOTAL_SECONDS = 3.0
_STOP_PERSIST_TIMEOUT_SECONDS = max(
    5.0,
    DB_MAX_RETRIES * DEFAULT_DB_TIMEOUT
    + DB_RETRY_DELAY_BASE * sum(range(1, DB_MAX_RETRIES))
    + 2.0,
)
# The pre-persistence phase may have to cancel one in-flight canonical starter
# and durably settle that already-admitted caller before the STT final drain.
_STOP_SETTLEMENT_TIMEOUT_SECONDS = (
    _STOP_PERSIST_TIMEOUT_SECONDS + _STOP_FINAL_DRAIN_TOTAL_SECONDS
    + 5.0
)
_STOP_POST_SETTLEMENT_CLEANUP_SECONDS = 5.0
_SESSION_FINAL_CLEANUP_TIMEOUT_SECONDS = 10.0
_SESSION_DURABLE_HANGUP_TIMEOUT_SECONDS = 5.0
_SESSION_PROVIDER_CLOSE_TIMEOUT_SECONDS = 5.0
_OWNED_TASK_CANCEL_GRACE_SECONDS = 0.5
_RECORDING_RAW_PERSIST_GRACE_SECONDS = 2.0
_BACKCHANNEL_MAX_MS = 1_200
_PCMU_BYTES_PER_SECOND = 8_000
_PCMU_ACTIVITY_MAGNITUDE = 512
_BRIEF_BACKCHANNELS = frozenset(
    {
        "ah",
        "aha",
        "aja",
        "claro",
        "entiendo",
        "hm",
        "hmm",
        "mhm",
        "ok",
        "okay",
        "si",
        "vale",
        "ya",
        "yeah",
        "yep",
    }
)


def _stop_persistence_budget_seconds(batch_size: int) -> float:
    """Bound an admitted Stop batch without assuming an arbitrary max size."""

    if batch_size <= 0:
        return 0.0
    return float(batch_size) * (
        _STOP_PERSIST_TIMEOUT_SECONDS + _OWNED_TASK_CANCEL_GRACE_SECONDS
    )


class GreetingPlayback(Protocol):
    """One cache-backed, confirmation-gated initial greeting.

    ``greeting_cache_backend`` is the intended implementation point.  It owns
    literal selection/no-repeat, PCMU/alignment, assistant-only persistence and
    exact heard-prefix commit.  The media session owns wire events and calls
    these three narrow methods; no LLM or TTS fallback is permitted here.
    """

    async def run(
        self,
        *,
        stream_sid: str,
        send_message: Callable[[Mapping[str, Any]], Awaitable[None]],
        recorder: LocalCallRecorder,
        call_started_monotonic: float,
    ) -> Any: ...

    async def acknowledge_mark(self, name: str) -> Any: ...

    def owns_mark(self, name: str) -> bool: ...

    async def barge_in(self) -> Any: ...

    async def disconnect(self) -> Any: ...


GreetingLoader = Callable[["PhoneMediaSessionContext"], Awaitable[GreetingPlayback]]
NoticeLoader = Callable[
    ["PhoneMediaSessionContext", str], Awaitable[GreetingPlayback]
]


class PhoneMediaSessionError(RuntimeError):
    """The authenticated media session could not continue safely."""


class _TwilioWebSocketDisconnected(PhoneMediaSessionError):
    """The provider media socket disappeared and may use connect-action."""


def _normalize_spoken_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[a-z0-9]+", without_marks))


def _is_speech_started_event(event: object) -> bool:
    if isinstance(
        event,
        (ElevenLabsSpeechStartedEvent, RealtimeCallSpeechStartedEvent),
    ):
        return True
    # Narrow compatibility for old unit-test fakes.  No live runtime imports or
    # constructs the retired provider's event classes.
    return type(event).__name__ == "DeepgramSpeechStartedEvent"


def _is_transcript_event(event: object) -> bool:
    return bool(
        isinstance(getattr(event, "text", None), str)
        and isinstance(getattr(event, "is_final", None), bool)
        and isinstance(getattr(event, "speech_final", None), bool)
    )


def _is_utterance_end_event(event: object) -> bool:
    return isinstance(event, ElevenLabsUtteranceEndEvent) or (
        hasattr(event, "last_word_end_seconds")
        and not _is_transcript_event(event)
    )


def _transcript_duration_ms(event: object) -> int | None:
    duration = getattr(event, "duration_seconds", None)
    words = getattr(event, "words", ())
    if duration is None and words:
        starts = [
            word.start_seconds
            for word in words
            if word.start_seconds is not None
        ]
        ends = [
            word.end_seconds
            for word in words
            if word.end_seconds is not None
        ]
        if starts and ends:
            duration = max(ends) - min(starts)
    if duration is None or not math.isfinite(float(duration)) or duration < 0:
        return None
    return round(float(duration) * 1_000)


def _pcmu_has_speech_activity(payload: bytes) -> bool:
    """Conservatively classify one raw PCMU frame without transcoding it."""

    required_active_samples = max(1, math.ceil(len(payload) * 0.1))
    active_samples = 0
    for encoded in payload:
        inverted = (~encoded) & 0xFF
        magnitude = (((inverted & 0x0F) << 3) + 0x84) << (
            (inverted & 0x70) >> 4
        )
        magnitude = abs(magnitude - 0x84)
        if magnitude >= _PCMU_ACTIVITY_MAGNITUDE:
            active_samples += 1
        if active_samples >= required_active_samples:
            return True
    return False


@dataclass(slots=True)
class _ParticipantSpeechActivity:
    """Observed voiced audio for the current provider utterance window."""

    endpointing_ms: int
    voiced_duration_ms: int = 0
    last_voice_end_ms: int | None = None

    def observe(self, payload: bytes, *, timestamp_ms: int) -> None:
        duration_ms = max(
            1,
            round(len(payload) * 1_000 / _PCMU_BYTES_PER_SECOND),
        )
        if not _pcmu_has_speech_activity(payload):
            return
        if (
            self.last_voice_end_ms is None
            or timestamp_ms - self.last_voice_end_ms > self.endpointing_ms
        ):
            self.voiced_duration_ms = 0
        self.voiced_duration_ms += duration_ms
        self.last_voice_end_ms = max(
            timestamp_ms + duration_ms,
            self.last_voice_end_ms or 0,
        )

    def reset(self) -> None:
        self.voiced_duration_ms = 0
        self.last_voice_end_ms = None


@dataclass(frozen=True, slots=True)
class PhoneMediaSessionContext:
    call_id: str
    provider_call_sid: str
    account_sid: str
    dispatch_token: str
    stream_attempt: int
    owner_user_id: int
    conversation_id: int
    foreground_epoch: int
    foreground_lease_owner: str
    call_snapshot: Mapping[str, Any]
    recording_enabled: bool
    direction: str
    started_at: datetime

    @classmethod
    def from_call(
        cls,
        call: Mapping[str, Any],
        *,
        account_sid: str,
        stream_attempt: int,
    ) -> "PhoneMediaSessionContext":
        try:
            snapshot = json.loads(str(call["config_snapshot_json"]))
            # A callback can race the Media Stream start.  Use the instant at
            # which this session is admitted as a provisional anchor, never
            # call creation/ringing time; attach_stream returns the durable
            # answered_at and the session reanchors before any model turn.
            started_raw = call.get("answered_at")
            started_at = (
                datetime.fromisoformat(str(started_raw).replace("Z", "+00:00"))
                if started_raw
                else datetime.now(UTC)
            )
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
            context = cls(
                call_id=str(call["id"]),
                provider_call_sid=str(call["provider_call_sid"]),
                account_sid=str(account_sid),
                dispatch_token=str(call["dispatch_token"]),
                stream_attempt=int(stream_attempt),
                owner_user_id=int(call["owner_user_id"]),
                conversation_id=int(call["conversation_id"]),
                foreground_epoch=int(call["foreground_fencing_token"]),
                foreground_lease_owner=str(call["foreground_lease_owner"]),
                call_snapshot=snapshot,
                recording_enabled=bool(call["recording_enabled"]),
                direction=str(call["direction"]),
                started_at=started_at.astimezone(UTC),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PhoneMediaSessionError("Phone call session context is invalid") from exc
        if not context.foreground_lease_owner:
            raise PhoneMediaSessionError("Phone call no longer owns foreground")
        if context.direction not in {"inbound", "outbound"}:
            raise PhoneMediaSessionError("Phone call direction is invalid")
        phone_settings_from_snapshot(context.call_snapshot)
        return context


@dataclass(frozen=True, slots=True)
class PhoneMediaSessionResult:
    reason: str
    stream_sid: str | None
    caller_turns: int
    reconnectable: bool
    internal_failure: bool = False
    attempt_result_published: bool = False


@dataclass(slots=True)
class _PendingBargeIn:
    armed: bool = False
    deferred_final_events: list[object] = field(
        default_factory=list
    )

    def arm(self, armed: bool) -> None:
        self.reset()
        self.armed = armed

    def defer(self, event: object) -> None:
        self.deferred_final_events.append(event)

    def take_deferred(self) -> tuple[object, ...]:
        events = tuple(self.deferred_final_events)
        self.deferred_final_events.clear()
        return events

    def reset(self) -> None:
        self.armed = False
        self.deferred_final_events.clear()


class PhoneMediaSession:
    """Drive one stream attempt while preserving canonical turn boundaries."""

    def __init__(
        self,
        context: PhoneMediaSessionContext,
        *,
        stt: RealtimeSttClient | None = None,
        deepgram: RealtimeSttClient | None = None,
        repository: TelephonyProviderRepository,
        current_user_loader: CurrentUserLoader,
        hangup_call: HangupCall,
        notice_loader: NoticeLoader | None = None,
        greeting_loader: GreetingLoader | None = None,
        runtime_starter: RuntimeStarter = start_canonical_phone_turn,
        caller_turn_persister: CallerTurnPersister = persist_canonical_phone_caller_turn,
        recorder: LocalCallRecorder | None = None,
        sleep: Sleep = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        billing_meter: PhoneLiveBillingMeter | None = None,
    ) -> None:
        if stt is not None and deepgram is not None:
            raise ValueError("provide one live STT client")
        resolved_stt = stt if stt is not None else deepgram
        if resolved_stt is None:
            raise ValueError("a live STT client is required")
        self.context = context
        self.stt = resolved_stt
        self.repository = repository
        self._current_user_loader = current_user_loader
        self._hangup_call = hangup_call
        self._notice_loader = notice_loader
        self._greeting_loader = greeting_loader
        self._runtime_starter = runtime_starter
        self._caller_turn_persister = caller_turn_persister
        self._sleep = sleep
        self._monotonic = monotonic
        self._billing_meter = billing_meter
        self.settings = phone_settings_from_snapshot(context.call_snapshot)
        self._realtime_enabled = (
            runtime_kind_from_snapshot(context.call_snapshot)
            == "openai_realtime"
        )
        self._participant_speech_activity = _ParticipantSpeechActivity(
            endpointing_ms=self.settings.endpointing_ms
        )
        self.parser = MediaStreamParser(
            expected_account_sid=context.account_sid,
            expected_call_sid=context.provider_call_sid,
            expected_correlation_token=context.dispatch_token,
            expected_stream_attempt=context.stream_attempt,
        )
        self.clock = DeterministicCallClock(
            self.settings,
            started_at=context.started_at,
        )
        self.silence = DeterministicSilenceWatchdog(self.settings)
        self.end_controller = CallEndController()
        self.recorder = recorder or LocalCallRecorder(
            context.call_id,
            enabled=context.recording_enabled,
        )
        call_elapsed_seconds = max(
            0.0,
            (datetime.now(UTC) - context.started_at).total_seconds(),
        )
        self._recording_attempt_offset_ms = int(call_elapsed_seconds * 1_000)
        self._call_started_monotonic = float(monotonic()) - call_elapsed_seconds
        self._assembler = PhoneUtteranceAssembler()
        self._utterances: asyncio.Queue[FinalPhoneUtterance | None] = asyncio.Queue(
            maxsize=32
        )
        self._stop_final_utterances: asyncio.Queue[FinalPhoneUtterance] = (
            asyncio.Queue(maxsize=32)
        )
        self._stop_drain_active = False
        self._stop_final_admission_closed = False
        self._twilio_stop_observed = False
        self._attempt_result_published = False
        self._turn_admission_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._active_playback: (
            PhoneTurnPlayback | RealtimeTurnPlayback | None
        ) = None
        self._active_realtime_bridge: Any | None = None
        self._active_greeting: GreetingPlayback | None = None
        self._active_notice: GreetingPlayback | None = None
        self._greeting_done = asyncio.Event()
        self._active_runtime: CanonicalPhoneTurn | None = None
        self._active_phone_turn: PhoneChannelTurn | None = None
        self._runtime_start_task: asyncio.Task[CanonicalPhoneTurn] | None = None
        self._runtime_start_utterance: FinalPhoneUtterance | None = None
        self._runtime_start_phone_turn: PhoneChannelTurn | None = None
        self._runtime_start_settlement_task: asyncio.Task[None] | None = None
        self._runtime_start_settlement_handled = False
        self._runtime_start_stop_takeover: (
            asyncio.Task[CanonicalPhoneTurn] | None
        ) = None
        self._starting_runtime = False
        self._speech_generation = 0
        self._pending_barge_in = _PendingBargeIn()
        self._output_state_lock = asyncio.Lock()
        self._audio_lock = asyncio.Lock()
        self._hangup_lock = asyncio.Lock()
        self._stream_sid: str | None = None
        self._stream_started_monotonic: float | None = None
        self._caller_turns = 0
        self._stopping = asyncio.Event()
        self._stop_reason = "websocket_closed"
        self._hangup_confirmed = False
        self._hangup_accepted = False
        self._current_user: Any | None = None
        self._shutdown_deadline_loop_time: float | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._terminal_media_mode = False
        self._fatal_cleanup_started = False
        self._tts_fragment_counter = 0

    @classmethod
    def with_elevenlabs_key_provider(
        cls,
        context: PhoneMediaSessionContext,
        *,
        elevenlabs_api_key_provider: ApiKeyProvider,
        repository: TelephonyProviderRepository,
        current_user_loader: CurrentUserLoader,
        hangup_call: HangupCall,
        notice_loader: NoticeLoader | None = None,
        greeting_loader: GreetingLoader | None = None,
        billing_service: PhoneBillingService | None = None,
    ) -> "PhoneMediaSession":
        settings = phone_settings_from_snapshot(context.call_snapshot)
        options = ElevenLabsRealtimeOptions(
            language=settings.stt_locale,
            endpointing_ms=settings.endpointing_ms,
        )
        return cls(
            context,
            stt=ElevenLabsRealtimeClient(
                api_key_provider=elevenlabs_api_key_provider,
                options=options,
            ),
            repository=repository,
            current_user_loader=current_user_loader,
            hangup_call=hangup_call,
            notice_loader=notice_loader,
            greeting_loader=greeting_loader,
            billing_meter=PhoneLiveBillingMeter(
                context.call_id,
                service=billing_service,
                stream_attempt=context.stream_attempt,
            ),
        )

    @classmethod
    def with_openai_realtime_key_provider(
        cls,
        context: PhoneMediaSessionContext,
        *,
        openai_api_key_provider: ApiKeyProvider,
        repository: TelephonyProviderRepository,
        current_user_loader: CurrentUserLoader,
        hangup_call: HangupCall,
        notice_loader: NoticeLoader | None = None,
        greeting_loader: GreetingLoader | None = None,
        billing_service: PhoneBillingService | None = None,
    ) -> "PhoneMediaSession":
        if runtime_kind_from_snapshot(context.call_snapshot) != "openai_realtime":
            raise PhoneMediaSessionError(
                "OpenAI Realtime is not active for this call"
            )
        voice = realtime_voice_from_snapshot(context.call_snapshot)
        if voice is None:
            raise PhoneMediaSessionError("OpenAI Realtime voice is unavailable")
        return cls(
            context,
            stt=OpenAIRealtimeCallBridge(
                api_key_provider=openai_api_key_provider,
                model=runtime_model_from_snapshot(context.call_snapshot),
                voice=voice,
            ),
            repository=repository,
            current_user_loader=current_user_loader,
            hangup_call=hangup_call,
            notice_loader=notice_loader,
            greeting_loader=greeting_loader,
            billing_meter=PhoneLiveBillingMeter(
                context.call_id,
                service=billing_service,
                stream_attempt=context.stream_attempt,
                stt_provider="openai",
            ),
        )
    @property
    def deepgram(self) -> RealtimeSttClient:
        """Deprecated test compatibility alias for the provider-neutral client."""

        return self.stt

    async def run(
        self,
        websocket: MediaWebSocket,
        *,
        initial_messages: Iterable[str | bytes | Mapping[str, Any]] = (),
    ) -> PhoneMediaSessionResult:
        """Run until Twilio stops, a provider fails, or a forced close occurs."""

        self._current_user = await self._current_user_loader(self.context.owner_user_id)
        if self._current_user is None or not bool(getattr(self._current_user, "is_enabled", False)):
            raise PhoneMediaSessionError("Phone call owner is unavailable")
        if self._notice_loader is None:
            raise PhoneMediaSessionError("Phone notice cache backend is not configured")
        if self.context.stream_attempt == 0:
            if self._greeting_loader is None:
                raise PhoneMediaSessionError(
                    "Initial greeting cache backend is not configured"
                )
            self._active_greeting = await self._greeting_loader(self.context)
            if self._active_greeting is None:
                raise PhoneMediaSessionError("Initial greeting cache is not ready")
        tasks: set[asyncio.Task[Any]] = set()
        websocket_disconnected = False
        reconnectable = False
        try:
            try:
                for raw in initial_messages:
                    await self.feed_twilio_message(raw, websocket)
                delivered_milestones = (
                    await self.repository.delivered_call_milestones(
                        call_id=self.context.call_id,
                        fencing_token=self.context.foreground_epoch,
                        lease_owner=self.context.foreground_lease_owner,
                    )
                )
                self.clock.restore_fired_milestones(delivered_milestones)
                await self._ensure_live_billing_coverage()
                await self.stt.connect()
            except PhoneBillingExhausted:
                await self._terminate_balance_exhausted(websocket)
                return PhoneMediaSessionResult(
                    reason=self._stop_reason,
                    stream_sid=self._stream_sid,
                    caller_turns=self._caller_turns,
                    reconnectable=False,
                    internal_failure=False,
                )
            except BaseException as exc:
                logger.error(
                    "Telephone session bootstrap failed (exception_type=%s)",
                    type(exc).__name__,
                )
                await self._terminate_fatal_failure(
                    websocket,
                    notice_confirmable=False,
                )
                return PhoneMediaSessionResult(
                    reason=self._stop_reason,
                    stream_sid=self._stream_sid,
                    caller_turns=self._caller_turns,
                    reconnectable=False,
                    internal_failure=False,
                )
            self._receive_task = asyncio.create_task(
                self._receive_loop(websocket), name="phone-twilio-receive"
            )
            tasks = {
                self._receive_task,
                asyncio.create_task(
                    self._stt_loop(websocket), name="phone-stt-events"
                ),
                asyncio.create_task(
                    self._turn_loop(websocket), name="phone-canonical-turns"
                ),
                asyncio.create_task(
                    self._timer_loop(websocket), name="phone-deadline-silence"
                ),
                asyncio.create_task(
                    self._lease_loop(), name="phone-foreground-renewal"
                ),
            }
            if self._active_greeting is not None:
                tasks.add(
                    asyncio.create_task(
                        self._run_greeting(websocket),
                        name="phone-initial-greeting",
                    )
                )
            else:
                tasks.add(
                    asyncio.create_task(
                        self._run_reconnect_notice(websocket),
                        name="phone-reconnect-notice",
                    )
                )
            while tasks and not self._stopping.is_set():
                done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                batch_failures: list[bool] = []
                published_stop_sibling_failed = False
                published_stop_receive_failed = False
                for task in done:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except _TwilioWebSocketDisconnected:
                        if self._attempt_result_published:
                            logger.error(
                                "Telephone Stop settlement lost its transport "
                                "after durable outcome (task=%s)",
                                task.get_name(),
                            )
                            if task is self._receive_task:
                                published_stop_receive_failed = True
                            else:
                                published_stop_sibling_failed = True
                        else:
                            batch_failures.append(True)
                    except BaseException as exc:
                        if self._attempt_result_published:
                            # Stop is already Twilio's terminal wire outcome.
                            # Settlement failures remain observable, but must
                            # not conflict with that immutable result or start
                            # a notice/hangup flow on a closed media stream.
                            logger.error(
                                "Telephone Stop settlement failed after "
                                "durable outcome (task=%s, exception_type=%s)",
                                task.get_name(),
                                type(exc).__name__,
                            )
                            if task is self._receive_task:
                                published_stop_receive_failed = True
                            else:
                                published_stop_sibling_failed = True
                        else:
                            logger.error(
                                "Telephone session task failed "
                                "(task=%s, exception_type=%s)",
                                task.get_name(),
                                type(exc).__name__,
                            )
                            batch_failures.append(False)
                if (
                    published_stop_sibling_failed
                    and not published_stop_receive_failed
                ):
                    # The receiver owns the already-published Stop drain. Keep
                    # every still-running sibling intact: in particular the
                    # STT event loop must consume finals emitted by finalize()
                    # into _stop_final_utterances before the receiver can
                    # persist the admitted caller-only batch. ``tasks`` has
                    # already dropped the sibling which actually failed.
                    receive_task = self._receive_task
                    if (
                        receive_task is not None
                        and receive_task in tasks
                        and not receive_task.done()
                    ):
                        continue
                    self._stopping.set()
                elif published_stop_receive_failed:
                    self._stopping.set()
                if batch_failures:
                    # No await is allowed between observing and canceling the
                    # remaining siblings.  A task which has already completed
                    # contributes its real outcome; a still-pending task is
                    # synchronously fenced by cancel() before it can publish a
                    # competing provider-independent failure.
                    pending_tasks = tuple(tasks)
                    self._ensure_shutdown_deadline(
                        _SESSION_FINAL_CLEANUP_TIMEOUT_SECONDS
                    )
                    # A live Twilio receiver is the only path that can confirm
                    # the cached technical notice.  Keep that explicit task
                    # alive while fencing all conversational producers.  In
                    # terminal mode it discards further participant media but
                    # continues to route mark/stop events.
                    receive_task = self._receive_task
                    keep_receiver = bool(
                        not all(batch_failures)
                        and receive_task is not None
                        and receive_task in pending_tasks
                        and not receive_task.done()
                    )
                    if keep_receiver:
                        self._terminal_media_mode = True
                    canceled_tasks: list[asyncio.Task[Any]] = []
                    for pending in pending_tasks:
                        if pending.done():
                            try:
                                pending.result()
                            except asyncio.CancelledError:
                                pass
                            except _TwilioWebSocketDisconnected:
                                batch_failures.append(True)
                            except BaseException as exc:
                                logger.error(
                                    "Telephone session task failed "
                                    "(task=%s, exception_type=%s)",
                                    pending.get_name(),
                                    type(exc).__name__,
                                )
                                batch_failures.append(False)
                            if pending is receive_task:
                                keep_receiver = False
                        elif keep_receiver and pending is receive_task:
                            continue
                        else:
                            pending.cancel()
                            canceled_tasks.append(pending)
                    websocket_disconnected = all(batch_failures)
                    if websocket_disconnected:
                        # Publish the transport outcome before awaiting sibling
                        # cleanup. A runtime starter can take arbitrarily long
                        # to finish its caller-only cancellation, while Twilio
                        # is already free to invoke connect-action.
                        reconnectable = (
                            not self._hangup_confirmed
                            and not self._hangup_accepted
                            and self.context.stream_attempt < 2
                        )
                        await self.repository.record_stream_attempt_result(
                            call_id=self.context.call_id,
                            provider_call_sid=self.context.provider_call_sid,
                            stream_attempt=self.context.stream_attempt,
                            reason="websocket_closed",
                            reconnectable=reconnectable,
                            internal_failure=True,
                        )
                        self._attempt_result_published = True
                    if canceled_tasks:
                        await self._cancel_and_join_shutdown_tasks(canceled_tasks)
                    # A websocket retry is safe only when every failure in the
                    # completed batch is the Twilio transport disappearing.
                    # A simultaneous STT/LLM/TTS/persistence failure remains
                    # terminal even if the receive task also disconnected.
                    if websocket_disconnected:
                        # Once Twilio's media socket is gone it cannot confirm
                        # another cached notice.  All attempts, including the
                        # final one, return to the signed connect-action.  That
                        # action either reconnects (0/1) or plays the immutable
                        # reconnect_failed asset in TwiML and hangs up (2).
                        self._stop_reason = "websocket_closed"
                        self._stopping.set()
                    else:
                        await self._terminate_fatal_failure(
                            websocket,
                            notice_confirmable=keep_receiver,
                        )
                    if keep_receiver and receive_task is not None:
                        if not receive_task.done():
                            receive_task.cancel()
                        await self._cancel_and_join_shutdown_tasks((receive_task,))
                    tasks = set()
                    break
                if not self._stopping.is_set() and not tasks:
                    self._stopping.set()
            return PhoneMediaSessionResult(
                reason=self._stop_reason,
                stream_sid=self._stream_sid,
                caller_turns=self._caller_turns,
                reconnectable=reconnectable,
                # The connect-action gate uses this bit to distinguish an
                # intentional/terminal stop from the one approved retryable
                # transport outcome.  STT/LLM/TTS/persistence are terminated
                # above and can never reach this reconnect path.
                internal_failure=websocket_disconnected,
                attempt_result_published=self._attempt_result_published,
            )
        finally:
            try:
                await self._finalize_session(tasks)
            except Exception as exc:
                if not self._attempt_result_published:
                    raise
                logger.error(
                    "Telephone session final cleanup failed after durable "
                    "outcome (exception_type=%s)",
                    type(exc).__name__,
                )
            self._receive_task = None

    def _ensure_shutdown_deadline(self, timeout_seconds: float) -> None:
        if self._shutdown_deadline_loop_time is None:
            self._shutdown_deadline_loop_time = (
                asyncio.get_running_loop().time() + max(0.0, timeout_seconds)
            )

    def _extend_shutdown_deadline(self, timeout_seconds: float) -> None:
        """Reserve a newly-known bounded phase without shortening cleanup."""

        target = asyncio.get_running_loop().time() + max(0.0, timeout_seconds)
        current = self._shutdown_deadline_loop_time
        if current is None or target > current:
            self._shutdown_deadline_loop_time = target

    def _remaining_shutdown_seconds(self) -> float:
        deadline = self._shutdown_deadline_loop_time
        if deadline is None:
            return 0.0
        return max(0.0, deadline - asyncio.get_running_loop().time())

    async def _await_owned_shutdown(
        self,
        awaitable: Awaitable[Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        """Await one owned operation without letting it escape the deadline."""

        task = asyncio.ensure_future(awaitable)
        remaining = self._remaining_shutdown_seconds()
        timeout = (
            remaining
            if timeout_seconds is None
            else min(remaining, max(0.0, timeout_seconds))
        )
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            await cancel_and_join_tasks(
                (task,),
                deadline=min(
                    self._shutdown_deadline_loop_time
                    or asyncio.get_running_loop().time(),
                    asyncio.get_running_loop().time()
                    + _OWNED_TASK_CANCEL_GRACE_SECONDS,
                ),
            )
            raise
        except asyncio.CancelledError:
            await cancel_and_join_tasks(
                (task,),
                deadline=min(
                    self._shutdown_deadline_loop_time
                    or asyncio.get_running_loop().time(),
                    asyncio.get_running_loop().time()
                    + _OWNED_TASK_CANCEL_GRACE_SECONDS,
                ),
            )
            raise

    async def _cancel_and_join_shutdown_tasks(
        self, tasks: Iterable[asyncio.Task[Any]]
    ) -> tuple[asyncio.Task[Any], ...]:
        loop = asyncio.get_running_loop()
        survivors = await cancel_and_join_tasks(
            tasks,
            deadline=min(
                self._shutdown_deadline_loop_time or loop.time(),
                loop.time() + _OWNED_TASK_CANCEL_GRACE_SECONDS,
            ),
        )
        if survivors:
            logger.error(
                "Telephone session cleanup exceeded cancellation grace: %s",
                ",".join(task.get_name() for task in survivors),
            )
        return survivors

    async def _best_effort_shutdown_step(
        self, operation: Callable[[], Awaitable[Any]]
    ) -> None:
        if self._remaining_shutdown_seconds() <= 0:
            return
        try:
            await self._await_owned_shutdown(operation())
        except TimeoutError:
            return
        except Exception:
            return

    async def _finalize_session(
        self, sibling_tasks: Iterable[asyncio.Task[Any]]
    ) -> None:
        """Own all session cleanup under the Stop/failure monotonic budget."""

        self._ensure_shutdown_deadline(_SESSION_FINAL_CLEANUP_TIMEOUT_SECONDS)
        if self._fatal_cleanup_started:
            self._extend_shutdown_deadline(_SESSION_FINAL_CLEANUP_TIMEOUT_SECONDS)
        self._stopping.set()
        try:
            self._utterances.put_nowait(None)
        except asyncio.QueueFull:
            pass

        owned = list(sibling_tasks)
        survivors = await self._cancel_and_join_shutdown_tasks(owned)

        # The canonical starter is shielded by _run_turn because a final STT
        # has already crossed the session admission boundary.  Do not cancel
        # that child independently from its turn owner: doing so can discard
        # the caller before either a deferred handle or ingest-only fallback
        # commits it.  Own the one shared settlement task under this session's
        # existing monotonic deadline, then join the canceled turn owner.
        runtime_start = self._runtime_start_task
        settlement = self._runtime_start_settlement_task
        utterance = self._runtime_start_utterance
        runtime_start_phone_turn = self._runtime_start_phone_turn
        if (
            settlement is None
            and runtime_start is not None
            and utterance is not None
            and runtime_start_phone_turn is not None
        ):
            settlement = self._ensure_runtime_start_settlement(
                runtime_start,
                utterance,
                runtime_start_phone_turn,
            )
        settlement_error: PhoneMediaSessionError | None = None
        if settlement is not None and not self._runtime_start_settlement_handled:
            try:
                await self._await_owned_shutdown(settlement)
            except TimeoutError as exc:
                settlement_error = PhoneMediaSessionError(
                    "Canonical caller settlement exceeded its shutdown budget"
                )
                settlement_error.__cause__ = exc
        if survivors:
            await self._cancel_and_join_shutdown_tasks(survivors)
        self._runtime_start_task = None
        self._runtime_start_utterance = None
        self._runtime_start_phone_turn = None
        self._runtime_start_settlement_task = None
        self._runtime_start_settlement_handled = False
        self._runtime_start_stop_takeover = None
        self._starting_runtime = False
        self._speech_generation += 1

        # Detach provider objects before awaiting them.  A timed-out cleanup
        # can therefore never be repeated outside the one shared budget.
        playback = self._active_playback
        runtime = self._active_runtime
        phone_turn = self._active_phone_turn
        realtime_bridge = self._active_realtime_bridge
        greeting = self._active_greeting
        notice = self._active_notice
        self._active_playback = None
        self._active_runtime = None
        self._active_phone_turn = None
        self._active_realtime_bridge = None
        self._active_greeting = None
        self._active_notice = None

        # Fatal notice/hangup phases may legitimately consume their own
        # budgets.  Reserve a final, separately bounded provider-close phase
        # so the live provider session cannot be abandoned.
        if self._fatal_cleanup_started:
            self._extend_shutdown_deadline(_SESSION_PROVIDER_CLOSE_TIMEOUT_SECONDS)

        if playback is not None:
            await self._best_effort_shutdown_step(playback.disconnect)
        elif runtime is not None:
            if phone_turn is not None:
                phone_turn.link_state.mark_interrupted()
            await self._best_effort_shutdown_step(
                lambda: runtime.interrupt(
                    "", played_ms=0, reason="phone_session_closed"
                )
            )
        if greeting is not None:
            await self._best_effort_shutdown_step(greeting.disconnect)
        if notice is not None:
            await self._best_effort_shutdown_step(notice.disconnect)
        if realtime_bridge is not None:
            await self._best_effort_shutdown_step(realtime_bridge.close)
        await self._best_effort_shutdown_step(self.stt.close)
        if self._billing_meter is not None:
            await self._best_effort_shutdown_step(
                self._billing_meter.finalize_stt_usage
            )
            if self._stream_started_monotonic is not None:
                stream_duration = max(
                    0.0,
                    float(self._monotonic()) - self._stream_started_monotonic,
                )
                await self._best_effort_shutdown_step(
                    lambda: self._billing_meter.finalize_transport_usage(
                        duration_seconds=stream_duration,
                        external_usage_id=self._stream_sid,
                    )
                )
        raw_recording_persisted = await self._persist_raw_recording_bounded()
        if raw_recording_persisted and self._remaining_shutdown_seconds() > 0:
            await self._best_effort_shutdown_step(self._mix_and_update_recording)
        if settlement_error is not None:
            raise settlement_error

    async def feed_twilio_message(
        self,
        raw: str | bytes | Mapping[str, Any],
        websocket: MediaWebSocket,
    ) -> None:
        event = self.parser.parse(raw)
        if isinstance(event, ConnectedEvent):
            return
        if isinstance(event, StartEvent):
            call = await self.repository.attach_stream(
                call_id=self.context.call_id,
                provider_call_sid=event.call_sid,
                provider_stream_sid=event.stream_sid,
                stream_attempt=event.stream_attempt,
            )
            if int(call["foreground_fencing_token"]) != self.context.foreground_epoch:
                raise PhoneMediaSessionError("Phone foreground fence changed")
            self._anchor_answered_at(call.get("answered_at"))
            self._stream_sid = event.stream_sid
            self._stream_started_monotonic = float(self._monotonic())
            self._caller_turns = await self.repository.count_caller_turns(
                call_id=self.context.call_id,
                fencing_token=self.context.foreground_epoch,
                lease_owner=self.context.foreground_lease_owner,
            )
            return
        if isinstance(event, MediaEvent):
            self._require_started()
            if self._terminal_media_mode:
                return
            try:
                await self._ensure_live_billing_coverage()
            except PhoneBillingExhausted:
                await self._terminate_balance_exhausted(websocket)
                return
            # MediaStreamParser has already enforced mono PCMU at 8 kHz.  A
            # local energy observation informs barge-in timing, while the exact
            # original Twilio bytes go to the active STT transport without
            # transcoding.
            self._participant_speech_activity.observe(
                event.payload,
                timestamp_ms=event.timestamp_ms,
            )
            await self.stt.send_audio(event.payload)
            if self._billing_meter is not None:
                self._billing_meter.note_stt_audio_sent(len(event.payload))
            if (
                self._pending_barge_in.armed
                and self._participant_speech_activity.voiced_duration_ms
                > self._observed_barge_in_threshold_ms()
            ):
                # Once observed voiced audio is no longer plausibly a brief
                # backchannel, do not wait for another provider hypothesis to
                # clear Twilio's buffered assistant audio.
                await self._confirm_barge_in()
            self.recorder.record_participant(
                event.payload,
                start_ms=self._recording_attempt_offset_ms + event.timestamp_ms,
            )
            return
        if isinstance(event, MarkEvent):
            # ``clear`` can return an old greeting mark after a canonical
            # playback has already taken the wire.  Route by mark ownership,
            # never by whichever playback happens to be active now.  Unknown
            # marks are legitimate late confirmations for an owner that has
            # already completed and are ignored.
            for owner in (
                self._active_playback,
                self._active_greeting,
                self._active_notice,
            ):
                if owner is None:
                    continue
                owns_mark = getattr(owner, "owns_mark", None)
                if callable(owns_mark) and owns_mark(event.name):
                    await owner.acknowledge_mark(event.name)
                    break
            return
        if isinstance(event, StopEvent):
            if self._terminal_media_mode:
                # Fatal cleanup already owns notice/hangup and caller
                # settlement.  A provider Stop only closes the confirmation
                # path; it must not start the normal STT drain concurrently.
                self._stopping.set()
                return
            await self._drain_twilio_stop(websocket)

    def _anchor_answered_at(self, answered_at: Any) -> None:
        if not answered_at:
            raise PhoneMediaSessionError("Answered call has no durable answer time")
        try:
            started_at = datetime.fromisoformat(
                str(answered_at).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise PhoneMediaSessionError("Answered call time is invalid") from exc
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        started_at = started_at.astimezone(UTC)
        self.clock.reanchor(started_at)
        elapsed = max(0.0, (datetime.now(UTC) - started_at).total_seconds())
        self._recording_attempt_offset_ms = int(elapsed * 1_000)
        self._call_started_monotonic = float(self._monotonic()) - elapsed

    async def feed_stt_event(
        self,
        event: object,
        websocket: MediaWebSocket,
    ) -> None:
        deferred_events: tuple[object, ...] = ()
        if isinstance(event, ElevenLabsMetadataEvent):
            if self._billing_meter is not None:
                self._billing_meter.note_stt_metadata(
                    duration_seconds=None,
                    session_id=event.session_id,
                )
            return
        if isinstance(event, ElevenLabsWarningEvent):
            logger.warning(
                "ElevenLabs realtime transcription warning (%s)", event.code
            )
            return
        if _is_speech_started_event(event):
            self.silence.on_real_participant_speech()
            self.end_controller.on_real_participant_speech()
            async with self._output_state_lock:
                self._pending_barge_in.arm(
                    bool(
                        self.settings.interruptible
                        and (
                            self._active_playback is not None
                            or self._active_greeting is not None
                            or self._active_notice is not None
                            or self._active_runtime is not None
                            or self._starting_runtime
                        )
                    )
                )
        elif _is_transcript_event(event):
            decision_text, decision_duration_ms = self._barge_in_candidate(event)
            if self._is_brief_backchannel(
                decision_text,
                decision_duration_ms,
            ):
                if event.is_final:
                    self._pending_barge_in.defer(event)
                if event.speech_final:
                    async with self._turn_admission_lock:
                        self._assembler.discard()
                    self._pending_barge_in.reset()
                    self._participant_speech_activity.reset()
                return
            if self._should_confirm_barge_in(
                event,
                text=decision_text,
                duration_ms=decision_duration_ms,
            ):
                deferred_events = self._pending_barge_in.take_deferred()
                await self._confirm_barge_in()
        elif (
            _is_utterance_end_event(event)
            and self.settings.ignore_backchannels
            and self._pending_barge_in.armed
            and self._pending_barge_in.deferred_final_events
        ):
            async with self._turn_admission_lock:
                self._assembler.discard()
            self._pending_barge_in.reset()
            self._participant_speech_activity.reset()
            return
        async with self._turn_admission_lock:
            if self._stopping.is_set() or self._stop_final_admission_closed:
                return
            for deferred_event in deferred_events:
                deferred_utterance = self._assembler.feed(deferred_event)
                if deferred_utterance is not None:
                    raise PhoneMediaSessionError(
                        "Deferred backchannel segment closed unexpectedly"
                    )
            utterance = self._assembler.feed(event)
            if utterance is not None:
                destination = (
                    self._stop_final_utterances
                    if self._stop_drain_active
                    else self._utterances
                )
                try:
                    destination.put_nowait(utterance)
                except asyncio.QueueFull as exc:
                    raise PhoneMediaSessionError(
                        "Final transcript queue overflow"
                    ) from exc
        if _is_utterance_end_event(event) or (
            _is_transcript_event(event) and event.speech_final
        ):
            self._pending_barge_in.reset()
            self._participant_speech_activity.reset()

    async def feed_deepgram_event(
        self,
        event: object,
        websocket: MediaWebSocket,
    ) -> None:
        """Deprecated test adapter; active runtime calls :meth:`feed_stt_event`."""

        await self.feed_stt_event(event, websocket)

    def _barge_in_candidate(
        self,
        event: object,
    ) -> tuple[str, int | None]:
        events = (*self._pending_barge_in.deferred_final_events, event)
        text = " ".join(item.text.strip() for item in events if item.text.strip())
        durations = [_transcript_duration_ms(item) for item in events]
        known_durations = [
            duration for duration in durations if duration is not None
        ]
        if len(known_durations) == len(events):
            duration_ms = sum(known_durations)
        elif self._participant_speech_activity.last_voice_end_ms is not None:
            # Some live STT events omit word/duration metadata. Use the
            # voiced PCMU that this session actually observed, once for the
            # whole utterance rather than once per changing hypothesis.
            duration_ms = self._participant_speech_activity.voiced_duration_ms
        else:
            duration_ms = None
        return text, duration_ms

    def _is_brief_backchannel(
        self,
        text: str,
        duration_ms: int | None,
    ) -> bool:
        if (
            not self._pending_barge_in.armed
            or not self.settings.ignore_backchannels
        ):
            return False
        normalized = _normalize_spoken_text(text)
        tokens = normalized.split()
        return bool(
            normalized in _BRIEF_BACKCHANNELS
            and len(tokens) <= 2
            and duration_ms is not None
            and duration_ms <= self._brief_backchannel_limit_ms()
        )

    def _brief_backchannel_limit_ms(self) -> int:
        return min(
            _BACKCHANNEL_MAX_MS,
            max(700, self.settings.barge_in_confirmation_ms * 2),
        )

    def _observed_barge_in_threshold_ms(self) -> int:
        if self.settings.ignore_backchannels:
            return self._brief_backchannel_limit_ms()
        return self.settings.barge_in_confirmation_ms

    def _should_confirm_barge_in(
        self,
        event: object,
        *,
        text: str | None = None,
        duration_ms: int | None = None,
    ) -> bool:
        if not self._pending_barge_in.armed or not event.text.strip():
            return False
        if text is None:
            text = event.text
        if duration_ms is None:
            duration_ms = _transcript_duration_ms(event)
        normalized = _normalize_spoken_text(text)
        tokens = normalized.split()
        confirmation_ms = self.settings.barge_in_confirmation_ms
        if not self.settings.ignore_backchannels:
            return bool(
                event.is_final
                or (duration_ms is not None and duration_ms >= confirmation_ms)
                or (duration_ms is None and len(tokens) >= 2)
            )
        return bool(
            event.is_final
            or (duration_ms is not None and duration_ms >= confirmation_ms)
            or (duration_ms is None and len(tokens) >= 3)
        )

    async def _confirm_barge_in(self) -> None:
        async with self._output_state_lock:
            if not self._pending_barge_in.armed:
                return
            playback = self._active_playback
            # A published playback owns the canonical runtime even before its
            # first audio frame reaches Twilio.  Confirmed human speech must
            # therefore interrupt it immediately; waiting for output creates
            # a dead zone when the provider never produces that first frame.
            self._pending_barge_in.reset()
            self._speech_generation += 1
            greeting = self._active_greeting
            notice = self._active_notice
        if playback is not None:
            await playback.barge_in()
        elif greeting is not None:
            await greeting.barge_in()
        elif notice is not None:
            await notice.barge_in()

    async def _receive_loop(self, websocket: MediaWebSocket) -> None:
        while not self._stopping.is_set():
            try:
                raw = await websocket.receive_text()
            except Exception as exc:
                raise _TwilioWebSocketDisconnected(
                    "Twilio Media Streams websocket disconnected"
                ) from exc
            await self.feed_twilio_message(raw, websocket)

    async def _drain_twilio_stop(self, websocket: MediaWebSocket) -> None:
        """Persist every admitted final phrase without closed-wire TTS."""

        # Twilio invokes connect-action as soon as it sends Stop, while STT
        # finalization and caller-only persistence can still take seconds. Set
        # the in-memory fence before the first await, then publish the terminal
        # wire outcome before doing any of that slower settlement work.
        self._twilio_stop_observed = True
        self._stop_reason = "twilio_stop"
        # Provider/output settlement gets its own bounded slice.  The durable
        # persistence slice is reserved only after STT admission has closed
        # and the exact batch size is known, so no arbitrary utterance count
        # can truncate an otherwise valid Stop drain.
        self._ensure_shutdown_deadline(
            _STOP_SETTLEMENT_TIMEOUT_SECONDS
            + _STOP_POST_SETTLEMENT_CLEANUP_SECONDS
        )
        prequeued: list[FinalPhoneUtterance] = []
        async with self._turn_admission_lock:
            self._stop_drain_active = True
            while True:
                try:
                    queued = self._utterances.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if queued is not None:
                    prequeued.append(queued)
        del websocket
        try:
            await self.repository.record_stream_attempt_result(
                call_id=self.context.call_id,
                provider_call_sid=self.context.provider_call_sid,
                stream_attempt=self.context.stream_attempt,
                reason="twilio_stop",
                reconnectable=False,
                internal_failure=False,
            )
            self._attempt_result_published = True
            preparation_error = await self._await_owned_shutdown(
                self._settle_twilio_stop(prequeued),
                timeout_seconds=_STOP_SETTLEMENT_TIMEOUT_SECONDS,
            )
            persistence_error: Exception | None = None
            persistence_budget = _stop_persistence_budget_seconds(
                len(prequeued)
            )
            if persistence_budget > 0:
                # Preserve a cleanup tail after the actual admitted batch.
                # _await_owned_shutdown still applies the same monotonic
                # ownership deadline to the whole batch task.
                self._extend_shutdown_deadline(
                    persistence_budget
                    + _STOP_POST_SETTLEMENT_CLEANUP_SECONDS
                )
                persistence_error = await self._await_owned_shutdown(
                    self._persist_stop_batch(prequeued),
                    timeout_seconds=persistence_budget,
                )
            first_error = preparation_error or persistence_error
            if first_error is not None:
                raise first_error
        except TimeoutError as exc:
            raise PhoneMediaSessionError(
                "Twilio Stop settlement exceeded its total shutdown budget"
            ) from exc
        finally:
            # Close admission synchronously even when settlement was canceled
            # inside provider finalize/disconnect.  run() observes _stopping,
            # cancels the foreground-renewal sibling and performs terminal
            # cleanup without admitting another caller turn.
            self._stop_final_admission_closed = True
            self._stop_drain_active = False
            self._stop_reason = "twilio_stop"
            self._stopping.set()

    async def _settle_twilio_stop(
        self, prequeued: list[FinalPhoneUtterance]
    ) -> Exception | None:
        """Close final admission and return any starter settlement error."""

        await self._settle_active_output_for_stop()
        # A turn owner holds _turn_lock while its canonical starter is in
        # flight.  Stop takes over and cancels that starter before persisting
        # the queued batch, allowing the turn owner to release the lock while
        # preserving the already-admitted caller through the one settlement
        # task.
        first_error = await self._settle_runtime_start_for_stop()
        await self.stt.finalize()
        prequeued.extend(await self._drain_finalized_stop_utterances())
        async with self._turn_admission_lock:
            # Atomic final-admission boundary: every final utterance accepted
            # before this point is now captured exactly once.
            self._stop_final_admission_closed = True
            while True:
                try:
                    prequeued.append(self._stop_final_utterances.get_nowait())
                except asyncio.QueueEmpty:
                    break
        return first_error

    async def _persist_stop_batch(
        self, utterances: Iterable[FinalPhoneUtterance]
    ) -> Exception | None:
        """Attempt every captured final with one full persistence window."""

        first_error: Exception | None = None
        for utterance in utterances:
            try:
                await self._persist_stop_utterance_owned(
                    utterance,
                    timeout_seconds=_STOP_PERSIST_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        return first_error

    async def _drain_finalized_stop_utterances(
        self,
    ) -> list[FinalPhoneUtterance]:
        """Collect every final commit until live STT becomes briefly quiescent.

        A VAD commit can already be in flight when Twilio Stop asks STT for
        an explicit commit. The transport supplies no commit correlation ID or
        definitive finalize acknowledgement, so neither one queue read nor a
        short quiet interval proves that collection is complete.  Drain through
        one hard absolute window.  The caller then closes admission under the
        shared lock, catching the final queue-vs-close race atomically.
        """

        loop = asyncio.get_running_loop()
        started = loop.time()
        total_deadline = started + max(0.0, _STOP_FINAL_DRAIN_TOTAL_SECONDS)
        finalized: list[FinalPhoneUtterance] = []
        while True:
            now = loop.time()
            timeout = max(0.0, total_deadline - now)
            if timeout <= 0:
                break
            try:
                utterance = await asyncio.wait_for(
                    self._stop_final_utterances.get(),
                    timeout=timeout,
                )
            except TimeoutError:
                break
            finalized.append(utterance)
            while True:
                try:
                    finalized.append(self._stop_final_utterances.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if loop.time() >= total_deadline:
                break
        return finalized

    async def _persist_stop_utterance_owned(
        self,
        utterance: FinalPhoneUtterance,
        *,
        timeout_seconds: float,
    ) -> None:
        """Bound and own canonical persistence for one admitted Stop final."""

        task = asyncio.create_task(
            self._persist_stop_utterance(utterance),
            name=f"phone-stop-persist-{self._turn_id(utterance)}",
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=max(0.0, timeout_seconds)
            )
        except TimeoutError as exc:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise PhoneMediaSessionError(
                "Final caller persistence exceeded its shutdown budget"
            ) from exc
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        task.result()

    async def _settle_active_output_for_stop(self) -> None:
        async with self._output_state_lock:
            playback = self._active_playback
            greeting = self._active_greeting
            notice = self._active_notice
            # Detach before awaiting provider objects.  A timeout cancels this
            # one owned disconnect; run() must not call the same hung adapter a
            # second time outside the Stop budget.
            self._active_playback = None
            self._active_greeting = None
            self._active_notice = None
            # Fence a runtime starter or admitted runtime before it may
            # publish assistant audio.  _run_turn owns the single caller-only
            # interruption/turn count when it observes this generation change.
            self._speech_generation += 1
        if playback is not None:
            await playback.disconnect()
        elif greeting is not None:
            await greeting.disconnect()
        elif notice is not None:
            await notice.disconnect()

    async def _persist_stop_utterance(
        self, utterance: FinalPhoneUtterance
    ) -> None:
        """Persist a finalized caller phrase without attempting closed-wire TTS."""

        async with self._turn_lock:
            tick = self.clock.peek_safe_point()
            internal_context = (
                build_phone_pre_watchdog_context(tick).assistant_internal_context
            )
            delivered_milestones = (
                tick.safe_point_directive.crossed_milestones_seconds
                if tick.safe_point_directive is not None
                and tick.safe_point_directive.source == "milestone"
                else ()
            )
            guard = ForegroundCommitGuard(
                conversation_id=self.context.conversation_id,
                epoch=self.context.foreground_epoch,
                expected_owner="phone",
                call_id=self.context.call_id,
                lease_owner=self.context.foreground_lease_owner,
            )
            phone_turn = create_phone_channel_turn(
                guard,
                turn_id=self._turn_id(utterance),
                end_controller=self.end_controller,
                internal_turn_context=internal_context,
                persistence="ingest_only",
            )
            await self._caller_turn_persister(
                conversation_id=self.context.conversation_id,
                current_user=self._current_user,
                caller_text=utterance.text,
                phone_turn=phone_turn,
                expected_llm_id=int(self.context.call_snapshot["llm_id"]),
                runtime_llm_id=runtime_llm_id_from_snapshot(
                    self.context.call_snapshot
                ),
                reasoning_selection=reasoning_selection_from_snapshot(
                    self.context.call_snapshot
                ),
            )
            self._caller_turns += 1
            if delivered_milestones:
                await self.repository.record_delivered_call_milestones(
                    call_id=self.context.call_id,
                    provider_call_sid=self.context.provider_call_sid,
                    fencing_token=self.context.foreground_epoch,
                    lease_owner=self.context.foreground_lease_owner,
                    milestones_seconds=delivered_milestones,
                )
                self.clock.acknowledge_milestones(delivered_milestones)

    async def _stt_loop(self, websocket: MediaWebSocket) -> None:
        async for event in self.stt.events():
            await self.feed_stt_event(event, websocket)
            if self._stopping.is_set():
                return
        if not self._stopping.is_set():
            raise PhoneMediaSessionError(
                "Live transcription stream closed unexpectedly"
            )

    async def _turn_loop(self, websocket: MediaWebSocket) -> None:
        await self._greeting_done.wait()
        while not self._stopping.is_set():
            utterance = await self._utterances.get()
            if utterance is None:
                return
            async with self._turn_admission_lock:
                if self._stop_drain_active:
                    try:
                        self._stop_final_utterances.put_nowait(utterance)
                    except asyncio.QueueFull as exc:
                        raise PhoneMediaSessionError(
                            "Final transcript queue overflow"
                        ) from exc
                    continue
                await self._turn_lock.acquire()
            try:
                await self._run_turn(utterance, websocket)
            finally:
                self._turn_lock.release()

    async def _run_greeting(self, websocket: MediaWebSocket) -> None:
        greeting = self._active_greeting
        assert greeting is not None
        try:
            async with self._audio_lock:
                await greeting.run(
                    stream_sid=str(self._stream_sid),
                    send_message=websocket.send_json,
                    recorder=self.recorder,
                    call_started_monotonic=self._call_started_monotonic,
                )
        finally:
            if self._active_greeting is greeting:
                self._active_greeting = None
            self._greeting_done.set()

    async def _run_reconnect_notice(self, websocket: MediaWebSocket) -> None:
        try:
            await self._play_notice("reconnect_notice", websocket)
        finally:
            self._greeting_done.set()

    async def _lease_loop(self) -> None:
        deadline = self.context.started_at + timedelta(
            seconds=self.settings.max_duration_seconds
        )
        hard_limit = deadline + timedelta(
            seconds=_FOREGROUND_LEASE_GRACE_SECONDS
        )
        while not self._stopping.is_set():
            now = datetime.now(UTC)
            target = min(
                hard_limit,
                now + timedelta(seconds=_FOREGROUND_LEASE_SECONDS),
            )
            if target <= now:
                raise PhoneMediaSessionError("Phone foreground deadline expired")
            renewed = await self.repository.renew_session_foreground(
                call_id=self.context.call_id,
                fencing_token=self.context.foreground_epoch,
                lease_owner=self.context.foreground_lease_owner,
                lease_until=target.isoformat(timespec="seconds").replace("+00:00", "Z"),
                now_utc=now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            )
            if not renewed:
                raise PhoneMediaSessionError("Phone foreground lease was lost")
            await self._sleep(_FOREGROUND_RENEW_INTERVAL_SECONDS)

    def _ensure_runtime_start_settlement(
        self,
        runtime_start: asyncio.Task[CanonicalPhoneTurn],
        utterance: FinalPhoneUtterance,
        phone_turn: PhoneChannelTurn,
    ) -> asyncio.Task[None]:
        """Return the single owner of a canceled in-flight runtime admission."""

        existing = self._runtime_start_settlement_task
        if existing is not None:
            return existing
        settlement = asyncio.create_task(
            self._settle_runtime_start_after_shutdown(
                runtime_start, utterance, phone_turn
            ),
            name=f"phone-runtime-start-settle-{self._turn_id(utterance)}",
        )
        self._runtime_start_settlement_task = settlement
        self._runtime_start_settlement_handled = False
        return settlement

    async def _settle_runtime_start_for_stop(self) -> Exception | None:
        """Take ownership of a starter before Stop persists queued callers.

        The turn loop holds ``_turn_lock`` while awaiting the shielded starter.
        Stop must cancel and settle that admitted turn first; otherwise a
        queued final waits behind the same lock until the total Stop timeout.
        """

        runtime_start = self._runtime_start_task
        utterance = self._runtime_start_utterance
        phone_turn = self._runtime_start_phone_turn
        if runtime_start is None or utterance is None or phone_turn is None:
            return None
        self._runtime_start_stop_takeover = runtime_start
        settlement = self._ensure_runtime_start_settlement(
            runtime_start,
            utterance,
            phone_turn,
        )
        if not runtime_start.done():
            runtime_start.cancel()
        try:
            await asyncio.shield(settlement)
        except asyncio.CancelledError:
            # The session finalizer retains ownership of the shielded child.
            raise
        except Exception as exc:
            self._runtime_start_settlement_handled = True
            return exc
        self._runtime_start_settlement_handled = True
        return None

    async def _settle_runtime_start_after_shutdown(
        self,
        runtime_start: asyncio.Task[CanonicalPhoneTurn],
        utterance: FinalPhoneUtterance,
        phone_turn: PhoneChannelTurn,
    ) -> None:
        """Persist one admitted caller when its deferred runtime is interrupted.

        The starter gets the existing short cancellation grace to return a
        canonical handle. If it is canceled or fails before returning that
        handle, the same stable turn identity is persisted through the
        ingest-only path. No child may outlive the shared shutdown deadline,
        and only this task owns the choice between those two paths.
        """

        loop = asyncio.get_running_loop()
        try:
            if not runtime_start.done():
                admission_grace = min(
                    _OWNED_TASK_CANCEL_GRACE_SECONDS,
                    self._remaining_shutdown_seconds(),
                )
                if admission_grace > 0:
                    await asyncio.wait(
                        (runtime_start,),
                        timeout=admission_grace,
                        return_when=asyncio.ALL_COMPLETED,
                    )
            if not runtime_start.done():
                survivors = await cancel_and_join_tasks(
                    (runtime_start,),
                    deadline=min(
                        self._shutdown_deadline_loop_time or loop.time(),
                        loop.time() + _OWNED_TASK_CANCEL_GRACE_SECONDS,
                    ),
                )
                if survivors:
                    raise PhoneMediaSessionError(
                        "Canonical runtime starter ignored shutdown cancellation"
                    )

            runtime: CanonicalPhoneTurn | None = None
            if not runtime_start.cancelled():
                try:
                    runtime = runtime_start.result()
                except asyncio.CancelledError:
                    runtime = None
                except Exception:
                    logger.warning(
                        "Canonical runtime admission failed during caller settlement",
                        exc_info=True,
                    )

            if runtime is None:
                remaining = self._remaining_shutdown_seconds()
                if remaining <= 0:
                    raise PhoneMediaSessionError(
                        "Canonical caller persistence exhausted its shutdown budget"
                    )
                await self._persist_stop_utterance_owned(
                    utterance,
                    timeout_seconds=min(_STOP_PERSIST_TIMEOUT_SECONDS, remaining),
                )
                return

            async with self._output_state_lock:
                self._active_runtime = runtime
                self._active_phone_turn = phone_turn
            try:
                phone_turn.link_state.mark_interrupted()
                await self._await_owned_shutdown(
                    runtime.interrupt(
                        "",
                        played_ms=0,
                        reason="phone_session_closed_during_start",
                    )
                )
                self._caller_turns += 1
            finally:
                async with self._output_state_lock:
                    if self._active_runtime is runtime:
                        self._active_runtime = None
                        self._active_phone_turn = None
        except asyncio.CancelledError:
            if not runtime_start.done():
                survivors = await cancel_and_join_tasks(
                    (runtime_start,),
                    deadline=min(
                        self._shutdown_deadline_loop_time or loop.time(),
                        loop.time() + _OWNED_TASK_CANCEL_GRACE_SECONDS,
                    ),
                )
                if survivors:
                    logger.error(
                        "Canonical runtime starter survived canceled settlement"
                    )
            raise

    async def _run_turn(
        self,
        utterance: FinalPhoneUtterance,
        websocket: MediaWebSocket,
    ) -> None:
        self._require_started()
        tick = self.clock.peek_safe_point()
        if tick.end_call is not None:
            await self._force_close(tick.end_call, websocket)
            return
        delivered_milestones = (
            tick.safe_point_directive.crossed_milestones_seconds
            if tick.safe_point_directive is not None
            and tick.safe_point_directive.source == "milestone"
            else ()
        )
        internal_context = build_phone_pre_watchdog_context(tick).assistant_internal_context
        turn_id = self._turn_id(utterance)
        guard = ForegroundCommitGuard(
            conversation_id=self.context.conversation_id,
            epoch=self.context.foreground_epoch,
            expected_owner="phone",
            call_id=self.context.call_id,
            lease_owner=self.context.foreground_lease_owner,
        )
        realtime_bridge: Any | None = None
        if self._realtime_enabled:
            realtime_bridge = utterance.turn_handle
            if not getattr(
                realtime_bridge,
                "_aurvek_internal_realtime_bridge",
                False,
            ):
                raise PhoneMediaSessionError(
                    "OpenAI Realtime caller turn handle is unavailable"
                )
        phone_turn = create_phone_channel_turn(
            guard,
            turn_id=turn_id,
            end_controller=self.end_controller,
            internal_turn_context=internal_context,
            openai_realtime_bridge=realtime_bridge,
        )
        async with self._output_state_lock:
            speech_generation = self._speech_generation
            self._starting_runtime = True
            self._active_realtime_bridge = realtime_bridge
        runtime_start = asyncio.create_task(
            self._runtime_starter(
                conversation_id=self.context.conversation_id,
                current_user=self._current_user,
                caller_text=utterance.text,
                phone_turn=phone_turn,
                expected_llm_id=int(self.context.call_snapshot["llm_id"]),
                runtime_llm_id=runtime_llm_id_from_snapshot(
                    self.context.call_snapshot
                ),
                reasoning_selection=reasoning_selection_from_snapshot(
                    self.context.call_snapshot
                ),
            ),
            name=f"phone-runtime-start-{turn_id}",
        )
        self._runtime_start_task = runtime_start
        self._runtime_start_utterance = utterance
        self._runtime_start_phone_turn = phone_turn
        stop_takeover = False
        try:
            runtime = await asyncio.shield(runtime_start)
            stop_takeover = bool(
                self._stop_drain_active
                and self._runtime_start_stop_takeover is runtime_start
            )
        except asyncio.CancelledError:
            # The final STT was already accepted. A dedicated task owns either
            # the returned deferred runtime or the ingest-only fallback. Let
            # this turn owner unwind immediately so it releases _turn_lock;
            # _finalize_session awaits the stored settlement under the shared
            # shutdown deadline.
            current = asyncio.current_task()
            outer_cancellation = bool(
                current is not None and current.cancelling()
            )
            stop_takeover = bool(
                self._stop_drain_active
                and self._runtime_start_stop_takeover is runtime_start
            )
            self._ensure_shutdown_deadline(_SESSION_FINAL_CLEANUP_TIMEOUT_SECONDS)
            self._ensure_runtime_start_settlement(
                runtime_start,
                utterance,
                phone_turn,
            )
            if outer_cancellation:
                raise
            if stop_takeover:
                return
            raise PhoneMediaSessionError(
                "Canonical runtime starter was canceled"
            ) from None
        except Exception:
            stop_takeover = bool(
                self._stop_drain_active
                and self._runtime_start_stop_takeover is runtime_start
            )
            if stop_takeover:
                return
            if realtime_bridge is not None:
                async with self._output_state_lock:
                    if self._active_realtime_bridge is realtime_bridge:
                        self._active_realtime_bridge = None
                await realtime_bridge.close()
            raise
        finally:
            if self._runtime_start_task is runtime_start:
                self._runtime_start_task = None
                self._runtime_start_utterance = None
                self._runtime_start_phone_turn = None
            if self._runtime_start_stop_takeover is runtime_start:
                self._runtime_start_stop_takeover = None
            self._starting_runtime = False
        if stop_takeover:
            return
        async with self._output_state_lock:
            self._active_runtime = runtime
            self._active_phone_turn = phone_turn
        if delivered_milestones:
            await self.repository.record_delivered_call_milestones(
                call_id=self.context.call_id,
                provider_call_sid=self.context.provider_call_sid,
                fencing_token=self.context.foreground_epoch,
                lease_owner=self.context.foreground_lease_owner,
                milestones_seconds=delivered_milestones,
            )
            self.clock.acknowledge_milestones(delivered_milestones)

        async def render(
            text: str,
            on_pcmu_chunk=None,
            on_pcmu_complete=None,
        ):
            self._tts_fragment_counter += 1
            return await render_phone_speech(
                text=text,
                conversation_id=self.context.conversation_id,
                current_user=self._current_user,
                call_snapshot=self.context.call_snapshot,
                call_id=self.context.call_id,
                billing_dedupe_key=(
                    f"tts:{turn_id}:fragment:{self._tts_fragment_counter}"
                ),
                billing_service=(
                    self._billing_meter.service
                    if self._billing_meter is not None
                    else None
                ),
                on_pcmu_chunk=on_pcmu_chunk,
                on_pcmu_complete=on_pcmu_complete,
            )

        playback: PhoneTurnPlayback | RealtimeTurnPlayback | None = None
        # Cached notices and canonical assistant audio share one wire owner.
        # Do not publish the assistant playback until any current notice has
        # returned its final mark (or completed a barge-in clear).
        async with self._audio_lock:
            async with self._output_state_lock:
                # Check, construct and publish without yielding the ownership
                # lock.  SpeechStarted that landed while runtime admission was
                # in flight is observed here; later speech waits and then sees
                # the published playback as its explicit barge-in owner.
                interrupted_during_start = (
                    self._speech_generation != speech_generation
                    or self._stop_drain_active
                )
                if not interrupted_during_start:
                    if realtime_bridge is not None:
                        playback = RealtimeTurnPlayback(
                            stream_sid=str(self._stream_sid),
                            phone_turn=phone_turn,
                            runtime_turn=runtime,
                            bridge=realtime_bridge,
                            send_message=websocket.send_json,
                            recorder=self.recorder,
                            call_started_monotonic=(
                                self._call_started_monotonic
                            ),
                            monotonic=self._monotonic,
                        )
                    else:
                        playback = PhoneTurnPlayback(
                            stream_sid=str(self._stream_sid),
                            phone_turn=phone_turn,
                            runtime_turn=runtime,
                            render_speech=render,
                            stream_render_speech=render,
                            send_message=websocket.send_json,
                            recorder=self.recorder,
                            call_started_monotonic=(
                                self._call_started_monotonic
                            ),
                            monotonic=self._monotonic,
                        )
                    interrupted_during_start = (
                        self._speech_generation != speech_generation
                        or self._stop_drain_active
                    )
                    if not interrupted_during_start:
                        self._active_playback = playback
            if interrupted_during_start:
                phone_turn.link_state.mark_interrupted()
                await runtime.interrupt(
                    "", played_ms=0, reason="barge_during_runtime_start"
                )
                self._caller_turns += 1
                if self._active_runtime is runtime:
                    self._active_runtime = None
                    self._active_phone_turn = None
                if realtime_bridge is not None:
                    if self._active_realtime_bridge is realtime_bridge:
                        self._active_realtime_bridge = None
                    await realtime_bridge.close()
                return
            assert playback is not None
            try:
                try:
                    result = await playback.run()
                except (PhonePlaybackError, RealtimePlaybackError) as exc:
                    if _has_cause(exc, PhoneSpeechBillingExhausted):
                        await self._terminate_balance_exhausted(websocket)
                        return
                    raise
                self._caller_turns += 1
                if not result.interrupted:
                    directive = self.end_controller.audio_confirmed()
                    if directive is not None:
                        await self._hangup(directive.reason.value)
            finally:
                async with self._output_state_lock:
                    if self._active_playback is playback:
                        self._active_playback = None
                    if self._active_runtime is runtime:
                        self._active_runtime = None
                        self._active_phone_turn = None
                    if self._active_realtime_bridge is realtime_bridge:
                        self._active_realtime_bridge = None
                if realtime_bridge is not None:
                    await realtime_bridge.close()

    async def _timer_loop(self, websocket: MediaWebSocket) -> None:
        while not self._stopping.is_set():
            if self._twilio_stop_observed:
                return
            try:
                await self._ensure_live_billing_coverage()
            except PhoneBillingExhausted:
                if self._twilio_stop_observed:
                    return
                await self._terminate_balance_exhausted(websocket)
                return
            except PhoneBillingError:
                # Stop may arrive while the billing await is in flight. Its
                # durable terminal result already owns the wire outcome, so a
                # late billing failure must not turn that normal Stop into a
                # session error. Billing errors outside that exact race remain
                # fail-closed and propagate to the session supervisor.
                if self._twilio_stop_observed:
                    return
                raise
            if self._twilio_stop_observed:
                return
            tick = self.clock.poll_deadline()
            if tick.end_call is not None:
                await self._force_close(tick.end_call, websocket)
                return
            # Presence audio is a safe-gap action.  Sampling the first silence
            # stage while greeting/assistant audio is active would arm a check
            # that cannot yet be played and could overlap canonical TTS.
            if (
                self._active_playback is not None
                or self._active_greeting is not None
                or self._active_notice is not None
                or self._starting_runtime
            ):
                await self._sleep(1.0)
                continue
            silence = self.silence.at_safe_point()
            if silence is not None:
                if silence.kind == SilenceDirectiveKind.CHECK_PRESENCE:
                    await self._play_notice("silence_check", websocket)
                    self.silence.confirm_presence_check_audible()
                elif silence.end_call is not None:
                    await self._force_close(silence.end_call, websocket)
                    return
            await self._sleep(1.0)

    async def _force_close(
        self,
        directive: EndCallDirective,
        websocket: MediaWebSocket,
    ) -> None:
        self.end_controller.request(directive)
        await self._interrupt_active_output(
            reason=f"forced_{directive.reason.value}"
        )
        notice_key = technical_notice_key_for_end_reason(directive.reason)
        if notice_key is None:
            raise PhoneMediaSessionError("Forced close has no technical notice key")
        await self._play_notice(notice_key, websocket)
        await self._hangup(directive.reason.value)

    async def _terminate_fatal_failure(
        self,
        websocket: MediaWebSocket,
        *,
        notice_confirmable: bool,
    ) -> None:
        """Give a confirmed cached failure notice, then request one hangup.

        Provider-independent failures are terminal for the call.  Each cleanup
        step is best effort so a failed persistence frontier or failed notice
        cannot prevent the shared durable hangup coordinator from fencing the
        call for an explicit retry/reconciliation.
        """

        self._ensure_shutdown_deadline(_SESSION_FINAL_CLEANUP_TIMEOUT_SECONDS)
        self._fatal_cleanup_started = True
        self._terminal_media_mode = True
        self._stop_reason = "session_error"
        await self._best_effort_shutdown_step(
            lambda: self._interrupt_active_output(reason="forced_error")
        )
        receiver = self._receive_task
        if (
            notice_confirmable
            and self._stream_sid is not None
            and receiver is not None
            and not receiver.done()
        ):
            await self._best_effort_shutdown_step(
                lambda: self._play_notice("technical_failure", websocket)
            )
        # Audible confirmation and durable provider hangup are separate
        # bounded phases.  A slow or missing Twilio mark must never consume
        # the coordinator's chance to record and attempt the hangup.
        self._extend_shutdown_deadline(_SESSION_DURABLE_HANGUP_TIMEOUT_SECONDS)
        await self._best_effort_shutdown_step(lambda: self._hangup("error"))
        # The provider latch may be unresolved when the budget expires, but no
        # fatal-cleanup or hangup task is allowed to outlive this session.
        self._stopping.set()

    async def _ensure_live_billing_coverage(self) -> None:
        if self._billing_meter is None:
            return
        if self._stream_started_monotonic is None:
            raise PhoneMediaSessionError(
                "Live billing cannot start before the Media Stream"
            )
        call_elapsed = max(
            0.0,
            float(self._monotonic()) - self._call_started_monotonic,
        )
        stream_elapsed = max(
            0.0,
            float(self._monotonic()) - self._stream_started_monotonic,
        )
        await self._billing_meter.ensure_live_coverage(
            call_elapsed_seconds=call_elapsed,
            stream_elapsed_seconds=stream_elapsed,
            include_pstn=True,
        )

    async def _terminate_balance_exhausted(
        self, websocket: MediaWebSocket
    ) -> None:
        if self._stopping.is_set():
            return
        directive = EndCallDirective.forced(
            requested_at=datetime.now(UTC),
            reason=EndCallReason.BALANCE,
        )
        await self._force_close(directive, websocket)
        self._stopping.set()

    async def _interrupt_active_output(self, *, reason: str) -> None:
        """Clear one current audio owner before a required cached notice."""

        async with self._output_state_lock:
            playback = self._active_playback
            runtime = self._active_runtime
            phone_turn = self._active_phone_turn
            greeting = self._active_greeting
            notice = self._active_notice
            if playback is None and (
                runtime is not None or self._starting_runtime
            ):
                # Invalidate both an admitted runtime awaiting playback and a
                # starter still in flight.  The latter may finish after the
                # cached close notice begins, but the generation fence makes
                # it persist caller-only and forbids overlapping assistant
                # audio.
                self._speech_generation += 1
        if playback is not None:
            # ``barge_in`` freezes the conservative audible frontier, clears
            # Twilio and commits caller + heard assistant text durably.
            await playback.barge_in()
            async with self._output_state_lock:
                if self._active_playback is playback:
                    self._active_playback = None
        elif runtime is not None:
            # Generation exists but no audio ledger was exposed yet.  Commit
            # the caller-only turn rather than discarding an already-final STT.
            if phone_turn is not None:
                phone_turn.link_state.mark_interrupted()
            await runtime.interrupt(
                "",
                played_ms=0,
                reason=str(reason),
            )
            async with self._output_state_lock:
                if self._active_runtime is runtime:
                    self._active_runtime = None
                    self._active_phone_turn = None
        elif greeting is not None:
            # Deadline and other forced closes preempt the opening greeting;
            # its cache ledger persists only the conservative audible prefix.
            await greeting.barge_in()
            async with self._output_state_lock:
                if self._active_greeting is greeting:
                    self._active_greeting = None
        elif notice is not None:
            await notice.barge_in()
            async with self._output_state_lock:
                if self._active_notice is notice:
                    self._active_notice = None

    async def _play_notice(self, kind: str, websocket: MediaWebSocket) -> None:
        self._require_started()
        if self._notice_loader is None:
            raise PhoneMediaSessionError(
                f"Required cached phone notice is unavailable: {kind}"
            )
        async with self._audio_lock:
            playback = await self._notice_loader(self.context, kind)
            if playback is None:
                raise PhoneMediaSessionError(
                    f"Required cached phone notice is unavailable: {kind}"
                )
            self._active_notice = playback
            try:
                # ``run`` resolves only when Twilio returns the final mark, so
                # callers cannot continue or hang up ahead of audible audio.
                await playback.run(
                    stream_sid=str(self._stream_sid),
                    send_message=websocket.send_json,
                    recorder=self.recorder,
                    call_started_monotonic=self._call_started_monotonic,
                )
            finally:
                if self._active_notice is playback:
                    self._active_notice = None

    async def _hangup(self, reason: str) -> None:
        async with self._hangup_lock:
            if self._hangup_confirmed or self._hangup_accepted:
                return
            claim = await self.repository.record_hangup_requested(
                call_id=self.context.call_id,
                provider_call_sid=self.context.provider_call_sid,
                reason=reason,
                target_status=PhoneCallStatus.COMPLETED,
                origin="session",
                retry_unresolved=True,
            )
            if not claim.claimed:
                self._hangup_confirmed = claim.state == "confirmed"
                self._hangup_accepted = claim.state == "accepted"
                self._stop_reason = (
                    reason
                    if self._hangup_confirmed or self._hangup_accepted
                    else "hangup_pending"
                )
                self._stopping.set()
                return
            assert claim.attempt_token is not None

            async def record_failure_state() -> str | None:
                try:
                    unresolved = await self.repository.mark_hangup_unresolved(
                        call_id=self.context.call_id,
                        provider_call_sid=self.context.provider_call_sid,
                        reason=claim.reason,
                        attempt_token=claim.attempt_token,
                    )
                except Exception:
                    unresolved = False
                if unresolved:
                    return "unresolved"
                try:
                    return await self.repository.get_hangup_attempt_state(
                        call_id=self.context.call_id,
                        provider_call_sid=self.context.provider_call_sid,
                    )
                except Exception:
                    return None

            def apply_terminal_state(state: str | None) -> bool:
                self._hangup_confirmed = state == "confirmed"
                self._hangup_accepted = state == "accepted"
                return self._hangup_confirmed or self._hangup_accepted

            try:
                provider_call_existed = await self._hangup_call(
                    self.context.provider_call_sid
                )
                if (
                    provider_call_existed is not True
                    and provider_call_existed is not False
                ):
                    raise RuntimeError(
                        "Twilio hangup adapter returned an invalid result"
                    )
            except Exception:
                state = await record_failure_state()
                if apply_terminal_state(state):
                    self._stop_reason = reason
                    self._stopping.set()
                    return
                self._stop_reason = (
                    "hangup_unresolved" if state == "unresolved" else "hangup_pending"
                )
                self._stopping.set()
                raise PhoneMediaSessionError(
                    "Provider hangup could not be confirmed"
                ) from None

            try:
                if provider_call_existed is False:
                    reconciled = (
                        await self.repository.reconcile_hangup_provider_absent(
                            call_id=self.context.call_id,
                            provider_call_sid=self.context.provider_call_sid,
                            attempt_token=claim.attempt_token,
                        )
                    )
                    if reconciled:
                        self._hangup_confirmed = True
                    else:
                        state = await self.repository.get_hangup_attempt_state(
                            call_id=self.context.call_id,
                            provider_call_sid=self.context.provider_call_sid,
                        )
                        if not apply_terminal_state(state):
                            raise PhoneMediaSessionError(
                                "Provider-absent hangup lost its durable fence"
                            )
                    self._stop_reason = reason
                    self._stopping.set()
                    return
                accepted = await self.repository.mark_hangup_accepted(
                    call_id=self.context.call_id,
                    provider_call_sid=self.context.provider_call_sid,
                    attempt_token=claim.attempt_token,
                )
                if accepted:
                    self._hangup_accepted = True
                else:
                    state = await self.repository.get_hangup_attempt_state(
                        call_id=self.context.call_id,
                        provider_call_sid=self.context.provider_call_sid,
                    )
                    if not apply_terminal_state(state):
                        raise PhoneMediaSessionError(
                            "Provider hangup acceptance lost its durable fence"
                        )
            except Exception as exc:
                state = await record_failure_state()
                if apply_terminal_state(state):
                    self._stop_reason = reason
                    self._stopping.set()
                    return
                self._stop_reason = (
                    "hangup_unresolved" if state == "unresolved" else "hangup_pending"
                )
                self._stopping.set()
                if isinstance(exc, PhoneMediaSessionError):
                    raise
                raise PhoneMediaSessionError(
                    "Provider hangup result could not be persisted"
                ) from None
            self._stop_reason = reason
            self._stopping.set()

    async def _persist_raw_recording_bounded(self) -> bool:
        if not self.context.recording_enabled:
            return False
        try:
            asset = self.recorder.finalize_raw()
        except Exception:
            logger.exception("Telephone raw recording finalization failed")
            return False
        has_raw_audio = bool(asset.participant_path or asset.assistant_path)
        persistence = asyncio.create_task(
            self.repository.persist_local_recording(
                call_id=self.context.call_id,
                participant_path=(
                    str(asset.participant_path) if asset.participant_path else None
                ),
                assistant_path=(
                    str(asset.assistant_path) if asset.assistant_path else None
                ),
                mixed_path=None,
                duration_seconds=max(0, int(math.ceil(asset.duration_ms / 1_000))),
                mix_error="mixed_audio_pending" if has_raw_audio else None,
            ),
            name=f"phone-recording-raw-persist-{self.context.call_id}",
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(persistence),
                timeout=_RECORDING_RAW_PERSIST_GRACE_SECONDS,
            )
        except TimeoutError:
            survivors = await cancel_and_join_tasks(
                (persistence,),
                deadline=(
                    asyncio.get_running_loop().time()
                    + _OWNED_TASK_CANCEL_GRACE_SECONDS
                ),
            )
            logger.error(
                "Telephone raw recording persistence timed out%s",
                " with a surviving task" if survivors else "",
            )
            return False
        except asyncio.CancelledError:
            await cancel_and_join_tasks(
                (persistence,),
                deadline=(
                    asyncio.get_running_loop().time()
                    + _OWNED_TASK_CANCEL_GRACE_SECONDS
                ),
            )
            raise
        except Exception:
            logger.exception("Telephone raw recording persistence failed")
            return False
        return has_raw_audio

    async def _mix_and_update_recording(self) -> None:
        asset = await self.recorder.finalize_async()
        await self.repository.persist_local_recording(
            call_id=self.context.call_id,
            participant_path=str(asset.participant_path) if asset.participant_path else None,
            assistant_path=str(asset.assistant_path) if asset.assistant_path else None,
            mixed_path=str(asset.mixed_path) if asset.mixed_path else None,
            duration_seconds=max(0, int(math.ceil(asset.duration_ms / 1_000))),
            mix_error=asset.mix_error,
        )

    def _require_started(self) -> None:
        if self._stream_sid is None:
            raise PhoneMediaSessionError("Media stream has not started")

    def _turn_id(self, utterance: FinalPhoneUtterance) -> str:
        del utterance
        # The sequence comes from durable caller links loaded on every attach.
        # It therefore continues across stream attempts without either
        # colliding between genuine turns or encoding a transient attempt ID.
        return f"stt-{self._caller_turns + 1}"


def _has_cause(error: BaseException, expected: type[BaseException]) -> bool:
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        if isinstance(current, expected):
            return True
        visited.add(id(current))
        current = current.__cause__ or current.__context__
    return False


__all__ = [
    "PhoneMediaSession",
    "PhoneMediaSessionContext",
    "PhoneMediaSessionError",
    "PhoneMediaSessionResult",
    "GreetingLoader",
    "GreetingPlayback",
    "NoticeLoader",
]
