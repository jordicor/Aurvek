"""Administrative control plane for Aurvek's native telephone channel.

The service keeps provider credentials out of every return value.  Expensive
global audio publication is an injected boundary: definitions are immutable
and versioned here, while provider rendering/activation must be registered by
the production audio subsystem before the mutation is accepted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
from typing import Any, Protocol

from ai_runtime.voice_resolution import provider_from_service_name
from database import get_db_connection
from integrations.telephony.config import (
    TelephonyConfigError,
    load_telephony_config,
    serialize_config_updates,
)
from integrations.telephony.billing import (
    PhoneBillingConfigurationError,
    phone_billing_readiness,
    upsert_phone_billing_rate,
)
from integrations.telephony.ffmpeg import is_ffmpeg_available
from integrations.telephony.greetings import (
    DEFAULT_GLOBAL_TECHNICAL_NOTICE_TEXT,
    GLOBAL_AUDIO_REVISION_CONFIG_KEY,
    GLOBAL_TECHNICAL_NOTICE_KEYS,
    GREETING_DIRECTIONS,
    GreetingInput,
    PhoneGreetingConfigurationError,
    TECHNICAL_NOTICE_KEYS,
    normalize_literal_text,
    stage_greeting_revision,
)
from integrations.telephony.security import (
    TwilioCanonicalURLConfigurationError,
    canonical_twilio_url,
)
from integrations.telephony.technical_notices import (
    load_prompt_technical_notice_revision,
    stage_prompt_technical_notice_revision,
    stage_technical_notice_revision,
)
from integrations.telephony.twilio_client import (
    AsyncTwilioVoiceClient,
    IncomingPhoneNumber,
)
from integrations.telephony.recording import DEFAULT_RECORDING_ROOT
from integrations.telephony.recording_storage import (
    PrivateRecordingPathError,
    resolve_private_recording_path,
)
from integrations.telephony.repository import (
    TelephonyConflictError,
    TelephonyNotFoundError,
    TelephonyRepository,
    TelephonyStateError,
)
from integrations.telephony.user_service import (
    PhoneUserServiceError,
    UserPhoneService,
    resolve_e164_country,
)
NUMBER_INVENTORY_SYNC_CONFIG_KEY = "telephony_numbers_last_sync_at"
SUPPORTED_CANONICAL_TTS_PROVIDERS = frozenset({"elevenlabs", "openai"})
PAID_TEST_CALLS_ENABLED_ENV = "TELEPHONY_PAID_TEST_CALLS_ENABLED"
PAID_TEST_DESTINATIONS_ENV = "TELEPHONY_PAID_TEST_DESTINATIONS"


class TelephonyAdminError(RuntimeError):
    """Base error for an invalid administrative operation."""


class TelephonyAdminConflict(TelephonyAdminError):
    """The requested mutation conflicts with durable telephone state."""


class TelephonyAdminNotFound(TelephonyAdminError):
    """The requested administrative resource does not exist."""


class TelephonyAdminUnavailable(TelephonyAdminError):
    """An external dependency required for a safe mutation is unavailable."""


class TelephonyAdminMaterializedError(TelephonyAdminUnavailable):
    """A failed request still materialized a durable administrative result."""

    def __init__(self, message: str, *, materialized_state: str) -> None:
        super().__init__(message)
        self.materialized_state = str(materialized_state)


@dataclass(frozen=True, slots=True)
class TelephonyCredentials:
    account_sid: str
    provider_credential: str
    elevenlabs_available: bool
    openai_available: bool

    def provider_tts_ready(self, provider: str | None) -> bool:
        if provider == "elevenlabs":
            return self.elevenlabs_available
        if provider == "openai":
            return self.openai_available and self.elevenlabs_available
        return False

    def public_dict(self, *, canonical_provider: str | None) -> dict[str, bool]:
        return {
            "twilio_account_present": bool(self.account_sid),
            "twilio_credential_present": bool(self.provider_credential),
            "elevenlabs_stt_present": self.elevenlabs_available,
            "canonical_tts_present": self.provider_tts_ready(canonical_provider),
        }


@dataclass(frozen=True, slots=True)
class PaidTestCallGate:
    enabled: bool
    allowed_destinations: frozenset[str]
    configuration_error: str | None = None

    def public_dict(self) -> dict[str, Any]:
        destinations = sorted(self.allowed_destinations)
        return {
            "enabled": self.enabled,
            "ready": bool(
                self.enabled and destinations and self.configuration_error is None
            ),
            "configured_destination_count": len(destinations),
            "destinations_redacted": [_redact_e164(value) for value in destinations],
            "configuration_error": self.configuration_error,
        }


def _redact_e164(value: str) -> str:
    visible_suffix = str(value)[-4:]
    return f"+{'*' * max(len(str(value)) - 5, 4)}{visible_suffix}"


def environment_paid_test_call_gate() -> PaidTestCallGate:
    """Read the paid-test kill switch and exact destinations fail-closed."""

    enabled_raw = os.getenv(PAID_TEST_CALLS_ENABLED_ENV, "false").strip().lower()
    if enabled_raw not in {"true", "false"}:
        return PaidTestCallGate(
            enabled=False,
            allowed_destinations=frozenset(),
            configuration_error=f"{PAID_TEST_CALLS_ENABLED_ENV} must be true or false",
        )

    raw_destinations = os.getenv(PAID_TEST_DESTINATIONS_ENV, "").strip()
    if not raw_destinations:
        return PaidTestCallGate(
            enabled=enabled_raw == "true",
            allowed_destinations=frozenset(),
        )

    parts = [item.strip() for item in raw_destinations.split(",")]
    if any(not item for item in parts):
        return PaidTestCallGate(
            enabled=False,
            allowed_destinations=frozenset(),
            configuration_error=(
                f"{PAID_TEST_DESTINATIONS_ENV} must be a comma-separated E.164 list"
            ),
        )
    try:
        canonical = [resolve_e164_country(item)[0] for item in parts]
    except (PhoneUserServiceError, ValueError):
        return PaidTestCallGate(
            enabled=False,
            allowed_destinations=frozenset(),
            configuration_error=(
                f"{PAID_TEST_DESTINATIONS_ENV} contains an invalid E.164 destination"
            ),
        )
    if canonical != parts or len(set(canonical)) != len(canonical):
        return PaidTestCallGate(
            enabled=False,
            allowed_destinations=frozenset(),
            configuration_error=(
                f"{PAID_TEST_DESTINATIONS_ENV} must contain unique canonical E.164 values"
            ),
        )
    return PaidTestCallGate(
        enabled=enabled_raw == "true",
        allowed_destinations=frozenset(canonical),
    )


def environment_telephony_credentials() -> TelephonyCredentials:
    """Read credentials at operation time so tests and reloads remain accurate."""

    from tools.tts_load_balancer import has_elevenlabs_keys

    return TelephonyCredentials(
        account_sid=os.getenv("TWILIO_SID", "").strip(),
        provider_credential=os.getenv("TWILIO_AUTH", "").strip(),
        elevenlabs_available=has_elevenlabs_keys(),
        openai_available=bool(os.getenv("OPENAI_KEY", "").strip()),
    )


@dataclass(frozen=True, slots=True)
class AdminGreetingPhrase:
    literal_text: str
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class AdminGreetingList:
    mode: str
    phrases: tuple[AdminGreetingPhrase, ...]
    fixed_index: int | None = None


@dataclass(frozen=True, slots=True)
class GlobalAudioPublication:
    revision: int
    voice_id: int
    billing_user_id: int
    greetings: Mapping[str, AdminGreetingList]
    notices: Mapping[str, str]
    prompt_active_revisions: Mapping[int, int | None] | None = None


@dataclass(frozen=True, slots=True)
class GlobalAudioPublishResult:
    revision: int
    activation_id: str
    status: str


class GlobalAudioPublisher(Protocol):
    async def __call__(
        self, publication: GlobalAudioPublication
    ) -> GlobalAudioPublishResult: ...


class TelephonyRuntimeLifecycle(Protocol):
    """Narrow boundary from the admin control plane to process lifecycle."""

    async def reconcile(self) -> Any: ...

    def status(self) -> Any: ...


class TwilioNumberClient(Protocol):
    async def list_incoming_phone_numbers(
        self,
    ) -> tuple[IncomingPhoneNumber, ...]: ...

    async def update_incoming_number_voice(
        self,
        number_sid: str,
        *,
        voice_url: str,
        voice_method: str = "POST",
        status_callback_url: str,
        status_callback_method: str = "POST",
    ) -> IncomingPhoneNumber: ...

    async def close(self) -> None: ...

    async def end_call_once(self, call_sid: str) -> bool: ...


CredentialProvider = Callable[[], TelephonyCredentials]
PaidTestCallGateProvider = Callable[[], PaidTestCallGate]
NumberClientFactory = Callable[[TelephonyCredentials], TwilioNumberClient]
FfmpegProbe = Callable[[], Awaitable[bool]]
_CONFIG_TRANSITION_LOCK = asyncio.Lock()
_registered_runtime_lifecycle: TelephonyRuntimeLifecycle | None = None


def register_runtime_lifecycle(lifecycle: TelephonyRuntimeLifecycle) -> None:
    """Register the app-owned runtime without importing it from this module."""

    if not callable(getattr(lifecycle, "reconcile", None)) or not callable(
        getattr(lifecycle, "status", None)
    ):
        raise TypeError("telephony runtime lifecycle is invalid")
    global _registered_runtime_lifecycle
    _registered_runtime_lifecycle = lifecycle


def unregister_runtime_lifecycle(
    lifecycle: TelephonyRuntimeLifecycle,
) -> None:
    """Remove only the lifecycle instance owned by the shutting-down app."""

    global _registered_runtime_lifecycle
    if _registered_runtime_lifecycle is lifecycle:
        _registered_runtime_lifecycle = None


def _default_number_client_factory(
    credentials: TelephonyCredentials,
) -> AsyncTwilioVoiceClient:
    return AsyncTwilioVoiceClient(
        credentials.account_sid,
        credentials.provider_credential,
    )


class TelephonyAdminService:
    """Read and mutate telephone administration state with SQLite authority."""

    def __init__(
        self,
        *,
        connection_factory: Callable[..., Any] = get_db_connection,
        credential_provider: CredentialProvider = environment_telephony_credentials,
        number_client_factory: NumberClientFactory = _default_number_client_factory,
        global_audio_publisher: GlobalAudioPublisher | None = None,
        runtime_lifecycle: TelephonyRuntimeLifecycle | None = None,
        repository: TelephonyRepository | None = None,
        user_phone_service: UserPhoneService | None = None,
        paid_test_call_gate_provider: PaidTestCallGateProvider = environment_paid_test_call_gate,
        recording_root: str | os.PathLike[str] = DEFAULT_RECORDING_ROOT,
        ffmpeg_probe: FfmpegProbe = is_ffmpeg_available,
    ) -> None:
        self._connection_factory = connection_factory
        self._credential_provider = credential_provider
        self._number_client_factory = number_client_factory
        self._global_audio_publisher = global_audio_publisher
        self._runtime_lifecycle = runtime_lifecycle
        self._repository = repository or TelephonyRepository(connection_factory)
        async def admin_config_loader():
            async with connection_factory(readonly=True) as conn:
                return await load_telephony_config(conn=conn)

        self._user_phone_service = user_phone_service or UserPhoneService(
            self._repository,
            connection_factory=connection_factory,
            config_loader=admin_config_loader,
        )
        self._paid_test_call_gate_provider = paid_test_call_gate_provider
        self._recording_root = Path(recording_root)
        self._ffmpeg_probe = ffmpeg_probe

    def register_global_audio_publisher(
        self, publisher: GlobalAudioPublisher
    ) -> None:
        if not callable(publisher):
            raise TypeError("global audio publisher must be callable")
        self._global_audio_publisher = publisher

    @asynccontextmanager
    async def _write(self):
        async with self._connection_factory() as conn:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                yield conn
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def dashboard(self) -> dict[str, Any]:
        credentials = self._credential_provider()
        paid_test_call = self._paid_test_call_gate_provider()
        async with self._connection_factory(readonly=True) as conn:
            config = await load_telephony_config(conn=conn)
            state = await self._readiness_state(conn, credentials=credentials)
            numbers = await self._load_numbers(conn, state["callbacks"])
            global_audio = await self._load_global_audio(conn)
            operation = await self._load_operation(conn)
            diagnostics = await self._load_diagnostics(conn)
            voices = await self._load_voices(conn)
            billing = await self._load_billing(conn)
        return {
            "config": config.public_dict(),
            "credentials": credentials.public_dict(
                canonical_provider=state["canonical_voice_provider"]
            ),
            "readiness": state["readiness"],
            "callbacks": state["callbacks"],
            "numbers": numbers,
            "voices": voices,
            "global_audio": global_audio,
            "operation": operation,
            "diagnostics": diagnostics,
            "billing": billing,
            "audio_publisher_registered": self._global_audio_publisher is not None,
            "runtime": self._runtime_public_status(),
            "paid_test_call": paid_test_call.public_dict(),
        }

    async def list_operations(
        self,
        *,
        resource: str,
        status: str | None = None,
        line_id: int | None = None,
        contact_id: int | None = None,
        owner_user_id: int | None = None,
        conversation_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return one bounded, server-filtered operations page."""

        if resource not in {"calls", "jobs"}:
            raise TelephonyAdminError("resource must be calls or jobs")
        bounded_limit = int(limit)
        bounded_offset = int(offset)
        if not 1 <= bounded_limit <= 100 or not 0 <= bounded_offset <= 100_000:
            raise TelephonyAdminError("Invalid operations page")
        normalized_status = str(status or "").strip()
        if len(normalized_status) > 40:
            raise TelephonyAdminError("Invalid status filter")
        alias = "c" if resource == "calls" else "j"
        conditions = ["c.deleted_at IS NULL"] if resource == "calls" else ["1=1"]
        params: list[Any] = []
        for column, value in (
            ("status", normalized_status or None),
            ("telephony_number_id", line_id),
            ("contact_id", contact_id),
            ("owner_user_id", owner_user_id),
            ("conversation_id", conversation_id),
        ):
            if value is not None:
                conditions.append(f"{alias}.{column}=?")
                params.append(value)
        where = " AND ".join(conditions)
        if resource == "calls":
            select = """
                SELECT c.id,c.job_id,c.owner_user_id,c.conversation_id,c.direction,
                       c.status,c.answered_by,c.duration_seconds,c.estimated_cost,
                       c.final_cost,c.currency,c.termination_reason,c.created_at,
                       c.answered_at,c.ended_at,p.display_name AS contact_name,
                       p.id AS contact_id,n.e164 AS line_e164,n.id AS line_id,
                       EXISTS(SELECT 1 FROM PHONE_HANGUP_ATTEMPTS h
                              WHERE h.call_id=c.id AND h.state='unresolved')
                              AS can_retry_hangup
                FROM PHONE_CALLS c
                LEFT JOIN PHONE_CONTACTS p ON p.id=c.contact_id
                LEFT JOIN TELEPHONY_NUMBERS n ON n.id=c.telephony_number_id
            """
            order = "c.created_at DESC,c.id DESC"
        else:
            select = """
                SELECT j.id,j.owner_user_id,j.conversation_id,j.scheduled_at_utc,
                       j.timezone_name,j.origin,j.status,j.last_error_code,
                       j.last_error_detail,j.created_at,j.updated_at,
                       p.display_name AS contact_name,p.id AS contact_id,
                       n.e164 AS line_e164,n.id AS line_id,c.id AS call_id
                FROM PHONE_CALL_JOBS j
                LEFT JOIN PHONE_CONTACTS p ON p.id=j.contact_id
                LEFT JOIN TELEPHONY_NUMBERS n ON n.id=j.telephony_number_id
                LEFT JOIN PHONE_CALLS c ON c.job_id=j.id AND c.deleted_at IS NULL
            """
            order = "j.scheduled_at_utc DESC,j.created_at DESC,j.id DESC"
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                f"SELECT COUNT(*) FROM ({select} WHERE {where})",
                tuple(params),
            )
            total = int((await cursor.fetchone())[0])
            cursor = await conn.execute(
                f"{select} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, bounded_limit, bounded_offset),
            )
            items = [dict(row) for row in await cursor.fetchall()]
            if resource == "jobs":
                for item in items:
                    if item.get("last_error_detail"):
                        item["last_error_detail"] = "Requires operational attention"
        next_offset = bounded_offset + len(items)
        return {
            "resource": resource,
            "items": items,
            "total": total,
            "limit": bounded_limit,
            "offset": bounded_offset,
            "next_offset": next_offset if next_offset < total else None,
        }

    async def call_detail(self, call_id: str) -> dict[str, Any]:
        normalized = _admin_identifier(call_id, "call_id")
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT c.id,c.job_id,c.owner_user_id,c.conversation_id,c.direction,
                       c.from_e164,c.to_e164,c.status,c.answered_by,c.initiated_at,
                       c.ringing_at,c.answered_at,c.ended_at,c.duration_seconds,
                       c.termination_reason,c.estimated_cost,c.final_cost,c.currency,
                       c.recording_enabled,c.amd_enabled,c.created_at,c.updated_at,
                       p.display_name AS contact_name,n.e164 AS line_e164,
                       EXISTS(SELECT 1 FROM PHONE_HANGUP_ATTEMPTS h
                              WHERE h.call_id=c.id AND h.state='unresolved')
                              AS can_retry_hangup
                FROM PHONE_CALLS c
                LEFT JOIN PHONE_CONTACTS p ON p.id=c.contact_id
                LEFT JOIN TELEPHONY_NUMBERS n ON n.id=c.telephony_number_id
                WHERE c.id=? AND c.deleted_at IS NULL
                """,
                (normalized,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise TelephonyAdminNotFound("Phone call not found")
            call = dict(row)
            cursor = await conn.execute(
                """
                SELECT id,event_type,signature_valid,provider_occurred_at,
                       payload_json,received_at
                FROM PHONE_CALL_EVENTS WHERE call_id=?
                ORDER BY received_at,id LIMIT 500
                """,
                (normalized,),
            )
            events = []
            for event in await cursor.fetchall():
                public = dict(event)
                public["payload"] = _redacted_event_payload(public.pop("payload_json"))
                events.append(public)
            cursor = await conn.execute(
                """
                SELECT m.id,m.type,m.message,l.created_at AS date,l.participant,l.turn_id,
                       l.interrupted,l.played_ms,l.delivery_state,l.origin_channel
                FROM PHONE_CALL_MESSAGE_LINKS l
                JOIN MESSAGES m ON m.id=l.message_id
                WHERE l.call_id=? ORDER BY l.created_at,m.id LIMIT 1000
                """,
                (normalized,),
            )
            transcript = [dict(item) for item in await cursor.fetchall()]
            cursor = await conn.execute(
                """
                SELECT id,status,duration_seconds,
                       participant_path IS NOT NULL AS has_participant,
                       assistant_path IS NOT NULL AS has_assistant,
                       mixed_path IS NOT NULL AS has_mixed,
                       created_at,updated_at,last_error IS NOT NULL AS has_error
                FROM PHONE_RECORDINGS WHERE call_id=? ORDER BY id DESC LIMIT 20
                """,
                (normalized,),
            )
            recordings = []
            for recording in await cursor.fetchall():
                public = dict(recording)
                public["tracks"] = [
                    track
                    for track in ("mixed", "participant", "assistant")
                    if bool(public.pop(f"has_{track}"))
                ]
                recordings.append(public)
            cursor = await conn.execute(
                """
                SELECT id,provider,component_type,quantity,unit,
                       provider_cost,customer_charge,currency,state,
                       platform_absorbed,occurred_at,created_at,
                       last_error IS NOT NULL AS has_error
                FROM PHONE_CALL_COST_COMPONENTS WHERE call_id=?
                ORDER BY created_at,id LIMIT 500
                """,
                (normalized,),
            )
            costs = [dict(item) for item in await cursor.fetchall()]
        return {
            "call": call,
            "events": events,
            "transcript": transcript,
            "recordings": recordings,
            "costs": costs,
        }

    async def recording_file(self, call_id: str, track: str) -> tuple[Path, str]:
        normalized = _admin_identifier(call_id, "call_id")
        column = {
            "mixed": "mixed_path",
            "participant": "participant_path",
            "assistant": "assistant_path",
        }.get(str(track))
        if column is None:
            raise TelephonyAdminError("Invalid recording track")
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                f"""
                SELECT r.{column} FROM PHONE_RECORDINGS r
                JOIN PHONE_CALLS c ON c.id=r.call_id
                WHERE r.call_id=? AND c.deleted_at IS NULL
                  AND r.status='available' AND r.{column} IS NOT NULL
                ORDER BY r.id DESC LIMIT 1
                """,
                (normalized,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise TelephonyAdminNotFound("Phone recording not found")
        try:
            path = resolve_private_recording_path(
                normalized, str(row[0]), root=self._recording_root
            )
        except PrivateRecordingPathError as exc:
            raise TelephonyAdminUnavailable(
                "Private recording metadata is invalid"
            ) from exc
        if not path.is_file() or path.is_symlink():
            raise TelephonyAdminNotFound("Phone recording not found")
        return path, "audio/mpeg" if track == "mixed" else "audio/basic"

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        job = await self._admin_job(job_id)
        if str(job["status"]) == "canceled":
            return {"changed": False, "state": "already_canceled"}
        if str(job["status"]) != "scheduled":
            raise TelephonyAdminConflict("Only an unclaimed scheduled job can be canceled")
        changed = await self._repository.cancel_scheduled_job(
            owner_user_id=int(job["owner_user_id"]), job_id=str(job["id"])
        )
        if not changed:
            raise TelephonyAdminConflict("Job was claimed concurrently")
        return {"changed": True, "state": "canceled"}

    async def reschedule_job(
        self,
        job_id: str,
        *,
        scheduled_at: str,
        timezone_name: str,
        fold: int | None,
    ) -> dict[str, Any]:
        from integrations.telephony.user_service import parse_local_schedule

        job = await self._admin_job(job_id)
        if str(job["status"]) != "scheduled":
            raise TelephonyAdminConflict("Only an unclaimed scheduled job can be rescheduled")
        instant = parse_local_schedule(scheduled_at, timezone_name, fold=fold)
        changed = await self._user_phone_service.outbound_service.reschedule_call(
            owner_user_id=int(job["owner_user_id"]),
            job_id=str(job["id"]),
            scheduled_at=instant,
            timezone_name=timezone_name,
        )
        if not changed:
            raise TelephonyAdminConflict("Job was claimed concurrently")
        return {"changed": True, "state": "scheduled"}

    async def hangup_call(self, call_id: str, *, retry_unresolved: bool) -> dict[str, Any]:
        call = await self._admin_call(call_id)
        owner_user_id = int(call["owner_user_id"])
        if str(call["status"]) == "unresolved" and not bool(
            call["can_retry_hangup"]
        ):
            raise TelephonyAdminConflict(
                "Dispatch-unresolved calls are diagnostic only"
            )
        if bool(retry_unresolved) and not bool(call["can_retry_hangup"]):
            raise TelephonyAdminConflict("Call has no unresolved hangup attempt")
        try:
            call, claim = await self._repository.claim_owned_hangup_request(
                owner_user_id=owner_user_id,
                call_id=str(call["id"]),
                retry_unresolved=bool(retry_unresolved),
            )
        except (TelephonyNotFoundError, TelephonyStateError) as exc:
            raise TelephonyAdminConflict(str(exc)) from exc
        if not claim.claimed:
            return {"changed": False, "state": str(claim.state)}
        if claim.attempt_token is None:
            raise TelephonyAdminConflict("Hangup attempt lost its durable fence")
        credentials = self._credential_provider()
        client = self._number_client_factory(credentials)
        unresolved_materialized = False

        async def settle_unresolved() -> None:
            nonlocal unresolved_materialized
            unresolved_materialized = bool(
                await self._repository.mark_owned_hangup_unresolved(
                owner_user_id=owner_user_id,
                call_id=str(call["id"]),
                attempt_token=claim.attempt_token,
            )
            )

        try:
            try:
                changed = await client.end_call_once(str(call["provider_call_sid"]))
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    await asyncio.shield(settle_unresolved())
                else:
                    await settle_unresolved()
                raise
            try:
                if type(changed) is not bool:
                    raise TelephonyAdminUnavailable(
                        "Provider returned an invalid hangup result"
                    )
                if changed:
                    persisted = await self._repository.mark_owned_hangup_accepted(
                        owner_user_id=owner_user_id,
                        call_id=str(call["id"]),
                        attempt_token=claim.attempt_token,
                    )
                    state = "provider_requested"
                else:
                    persisted = (
                        await self._repository.reconcile_owned_hangup_provider_absent(
                            owner_user_id=owner_user_id,
                            call_id=str(call["id"]),
                            attempt_token=claim.attempt_token,
                        )
                    )
                    state = "provider_absent_reconciled"
                if not persisted:
                    raise TelephonyAdminUnavailable(
                        "Hangup result lost its durable fence"
                    )
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    await asyncio.shield(settle_unresolved())
                else:
                    await settle_unresolved()
                raise
            return {"changed": True, "state": state}
        except asyncio.CancelledError:
            raise
        except TelephonyAdminMaterializedError:
            raise
        except TelephonyAdminError as exc:
            if unresolved_materialized:
                raise TelephonyAdminMaterializedError(
                    "Provider hangup request could not be confirmed",
                    materialized_state="hangup_unresolved",
                ) from exc
            raise
        except Exception as exc:
            if unresolved_materialized:
                raise TelephonyAdminMaterializedError(
                    "Provider hangup request could not be confirmed",
                    materialized_state="hangup_unresolved",
                ) from exc
            raise TelephonyAdminUnavailable(
                "Provider hangup request could not be confirmed"
            ) from exc
        finally:
            with suppress(Exception):
                await client.close()

    async def resync_diagnostics(self) -> dict[str, Any]:
        lifecycle = self._require_runtime_lifecycle()
        inventory: dict[str, Any] | None = None
        try:
            inventory = await self.sync_numbers()
            runtime = await lifecycle.reconcile()
        except TelephonyAdminMaterializedError:
            raise
        except Exception as exc:
            if inventory is not None:
                raise TelephonyAdminMaterializedError(
                    "Number inventory was synchronized but runtime reconciliation failed",
                    materialized_state="inventory_synced_runtime_failed",
                ) from exc
            if isinstance(exc, TelephonyAdminError):
                raise
            raise TelephonyAdminUnavailable("Diagnostic resynchronization failed") from exc
        dashboard = await self.dashboard()
        return {
            "runtime": _runtime_status_reason(runtime),
            "inventory": inventory,
            "readiness": dashboard["readiness"],
            "diagnostics": dashboard["diagnostics"],
        }

    async def create_paid_test_call(
        self,
        *,
        conversation_id: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        gate = self._paid_test_call_gate_provider()
        if gate.configuration_error:
            raise TelephonyAdminConflict(gate.configuration_error)
        if not gate.enabled:
            raise TelephonyAdminConflict("Paid test calls are disabled")
        if not gate.allowed_destinations:
            raise TelephonyAdminConflict(
                "Paid test calls have no exact destinations configured"
            )
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT user_id FROM CONVERSATIONS WHERE id=?",
                (int(conversation_id),),
            )
            row = await cursor.fetchone()
        if row is None:
            raise TelephonyAdminNotFound("Conversation not found")
        owner_user_id = int(row[0])
        try:
            job, created = await self._user_phone_service.create_paid_test_call_job(
                owner_user_id=owner_user_id,
                conversation_id=int(conversation_id),
                idempotency_key=str(idempotency_key),
                allowed_destinations=gate.allowed_destinations,
            )
        except (
            PhoneUserServiceError,
            TelephonyConflictError,
            TelephonyStateError,
            ValueError,
        ) as exc:
            raise TelephonyAdminConflict(str(exc)) from exc
        except Exception as exc:
            raise TelephonyAdminUnavailable(
                "Paid test call could not be prepared"
            ) from exc
        return {"created": bool(created), "job_id": str(job["id"]), "owner_user_id": owner_user_id}

    async def _admin_job(self, job_id: str) -> dict[str, Any]:
        normalized = _admin_identifier(job_id, "job_id")
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT id,owner_user_id,conversation_id,status FROM PHONE_CALL_JOBS WHERE id=?",
                (normalized,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise TelephonyAdminNotFound("Phone-call job not found")
        return dict(row)

    async def _admin_call(self, call_id: str) -> dict[str, Any]:
        normalized = _admin_identifier(call_id, "call_id")
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT c.id,c.owner_user_id,c.status,
                       EXISTS(SELECT 1 FROM PHONE_HANGUP_ATTEMPTS h
                              WHERE h.call_id=c.id AND h.state='unresolved')
                              AS can_retry_hangup
                FROM PHONE_CALLS c WHERE c.id=? AND c.deleted_at IS NULL
                """,
                (normalized,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise TelephonyAdminNotFound("Phone call not found")
        return dict(row)

    async def save_config(self, values: Mapping[str, Any]) -> dict[str, Any]:
        try:
            serialized = serialize_config_updates(values)
        except TelephonyConfigError as exc:
            raise TelephonyAdminError(str(exc)) from exc
        if not serialized:
            return (await self.dashboard())["config"]

        lifecycle = self._require_runtime_lifecycle()
        async with _CONFIG_TRANSITION_LOCK:
            previous: dict[str, tuple[str, str | None] | None] = {}
            previous_enabled = False
            async with self._write() as conn:
                current = await load_telephony_config(conn=conn)
                previous_enabled = current.enabled
                requested_enabled = (
                    serialized["telephony_enabled"] == "1"
                    if "telephony_enabled" in serialized
                    else current.enabled
                )
                proposed_maximum = int(
                    serialized.get(
                        "telephony_max_call_seconds", current.max_call_seconds
                    )
                )
                cursor = await conn.execute(
                    """
                    SELECT COUNT(*) FROM PROMPT_PHONE_SETTINGS
                    WHERE max_duration_seconds IS NOT NULL
                      AND max_duration_seconds > ?
                    """,
                    (proposed_maximum,),
                )
                if int((await cursor.fetchone())[0]) > 0:
                    raise TelephonyAdminConflict(
                        "Some prompts exceed the proposed technical duration limit"
                    )
                if requested_enabled:
                    readiness = (
                        await self._readiness_state(
                            conn,
                            credentials=self._credential_provider(),
                            billing_config_overrides=serialized,
                        )
                    )["readiness"]
                    if not readiness["ready_for_enable"]:
                        raise TelephonyAdminConflict(
                            "Telephony cannot be enabled: "
                            + ", ".join(readiness["blocking_reasons"])
                        )
                placeholders = ",".join("?" for _ in serialized)
                cursor = await conn.execute(
                    f"SELECT key,value,description FROM SYSTEM_CONFIG "
                    f"WHERE key IN ({placeholders})",
                    tuple(serialized),
                )
                for row in await cursor.fetchall():
                    previous[str(row["key"])] = (
                        str(row["value"]),
                        row["description"],
                    )
                for key in serialized:
                    previous.setdefault(key, None)
                for key, value in serialized.items():
                    await conn.execute(
                        """
                        INSERT INTO SYSTEM_CONFIG(key,value,description,updated_at)
                        VALUES(?,?,?,CURRENT_TIMESTAMP)
                        ON CONFLICT(key) DO UPDATE SET
                            value=excluded.value,updated_at=CURRENT_TIMESTAMP
                        """,
                        (key, value, "Native telephone channel administration"),
                    )

            try:
                status = await lifecycle.reconcile()
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._compensate_runtime_transition(
                        lifecycle,
                        serialized=serialized,
                        previous=previous,
                        previous_enabled=previous_enabled,
                    )
                )
                raise
            except Exception as exc:
                restored, rollback_ready = await self._compensate_runtime_transition(
                    lifecycle,
                    serialized=serialized,
                    previous=previous,
                    previous_enabled=previous_enabled,
                )
                detail = _rollback_detail(restored, rollback_ready)
                raise TelephonyAdminUnavailable(
                    f"Telephone runtime transition failed; {detail}"
                ) from exc

            if not _runtime_matches_config(status, enabled=requested_enabled):
                reason = _runtime_status_reason(status)
                restored, rollback_ready = await self._compensate_runtime_transition(
                    lifecycle,
                    serialized=serialized,
                    previous=previous,
                    previous_enabled=previous_enabled,
                )
                detail = _rollback_detail(restored, rollback_ready)
                raise TelephonyAdminUnavailable(
                    f"Telephone runtime rejected configuration ({reason}); {detail}"
                )
            return (await self.dashboard())["config"]

    def _require_runtime_lifecycle(self) -> TelephonyRuntimeLifecycle:
        lifecycle = self._runtime_lifecycle or _registered_runtime_lifecycle
        if lifecycle is None:
            raise TelephonyAdminUnavailable(
                "Telephone runtime lifecycle is not registered"
            )
        return lifecycle

    def _runtime_public_status(self) -> dict[str, Any]:
        lifecycle = self._runtime_lifecycle or _registered_runtime_lifecycle
        if lifecycle is None:
            return {
                "registered": False,
                "enabled": False,
                "ready": False,
                "reason": "not_registered",
                "dispatcher_running": False,
                "memory_outbox_running": False,
            }
        try:
            status = lifecycle.status()
        except Exception:
            return {
                "registered": True,
                "enabled": False,
                "ready": False,
                "reason": "status_unavailable",
                "dispatcher_running": False,
                "memory_outbox_running": False,
            }
        return {
            "registered": True,
            "enabled": bool(getattr(status, "enabled", False)),
            "ready": bool(getattr(status, "ready", False)),
            "reason": _runtime_status_reason(status),
            "dispatcher_running": bool(
                getattr(status, "dispatcher_running", False)
            ),
            "memory_outbox_running": bool(
                getattr(status, "memory_outbox_running", False)
            ),
        }

    async def _compensate_runtime_transition(
        self,
        lifecycle: TelephonyRuntimeLifecycle,
        *,
        serialized: Mapping[str, str],
        previous: Mapping[str, tuple[str, str | None] | None],
        previous_enabled: bool,
    ) -> tuple[bool, bool]:
        restored = await self._restore_config_if_unchanged(
            serialized=serialized,
            previous=previous,
        )
        try:
            status = await lifecycle.reconcile()
        except Exception:
            return restored, False
        return restored, bool(
            restored
            and _runtime_matches_config(
                status,
                enabled=previous_enabled,
            )
        )

    async def _restore_config_if_unchanged(
        self,
        *,
        serialized: Mapping[str, str],
        previous: Mapping[str, tuple[str, str | None] | None],
    ) -> bool:
        """Compensate only while our just-written values remain authoritative."""

        async with self._write() as conn:
            placeholders = ",".join("?" for _ in serialized)
            cursor = await conn.execute(
                f"SELECT key,value FROM SYSTEM_CONFIG WHERE key IN ({placeholders})",
                tuple(serialized),
            )
            durable = {
                str(row["key"]): str(row["value"])
                for row in await cursor.fetchall()
            }
            if any(durable.get(key) != value for key, value in serialized.items()):
                return False
            for key, prior in previous.items():
                if prior is None:
                    await conn.execute("DELETE FROM SYSTEM_CONFIG WHERE key=?", (key,))
                    continue
                value, description = prior
                await conn.execute(
                    """
                    UPDATE SYSTEM_CONFIG SET value=?,description=?,
                        updated_at=CURRENT_TIMESTAMP WHERE key=?
                    """,
                    (value, description, key),
                )
        return True

    async def sync_numbers(self) -> dict[str, Any]:
        credentials = self._require_twilio_credentials()
        client = self._number_client_factory(credentials)
        try:
            remote_numbers = tuple(await client.list_incoming_phone_numbers())
        except Exception as exc:
            raise TelephonyAdminUnavailable(
                "Twilio number inventory could not be synchronized"
            ) from exc
        finally:
            await _close_client(client)
        _validate_remote_number_set(remote_numbers)
        synced_at = _utc_now()
        async with self._write() as conn:
            remote_sids = tuple(item.sid for item in remote_numbers)
            if remote_sids:
                placeholders = ",".join("?" for _ in remote_sids)
                await conn.execute(
                    f"""
                    UPDATE TELEPHONY_NUMBERS
                    SET enabled=0,inbound_enabled=0,is_outbound_default=0,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE provider_number_sid NOT IN ({placeholders})
                    """,
                    remote_sids,
                )
            else:
                await conn.execute(
                    """
                    UPDATE TELEPHONY_NUMBERS
                    SET enabled=0,inbound_enabled=0,is_outbound_default=0,
                        updated_at=CURRENT_TIMESTAMP
                    """
                )
            for item in remote_numbers:
                try:
                    canonical_e164, derived_country = resolve_e164_country(item.e164)
                except (PhoneUserServiceError, ValueError) as exc:
                    raise TelephonyAdminUnavailable(
                        "Twilio number inventory contains an invalid E.164 number"
                    ) from exc
                provider_country = str(item.iso_country or "").strip().upper()
                if canonical_e164 != item.e164 or (
                    provider_country and provider_country != derived_country
                ):
                    raise TelephonyAdminUnavailable(
                        "Twilio number inventory contains inconsistent country data"
                    )
                await conn.execute(
                    """
                    INSERT INTO TELEPHONY_NUMBERS(
                        provider_number_sid,e164,friendly_name,iso_country,region,
                        capabilities_json,voice_url,voice_method,
                        status_callback_url,status_callback_method,
                        voice_application_sid,trunk_sid,synced_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                    ON CONFLICT(provider_number_sid) DO UPDATE SET
                        e164=excluded.e164,friendly_name=excluded.friendly_name,
                        iso_country=excluded.iso_country,region=excluded.region,
                        capabilities_json=excluded.capabilities_json,
                        voice_url=excluded.voice_url,voice_method=excluded.voice_method,
                        status_callback_url=excluded.status_callback_url,
                        status_callback_method=excluded.status_callback_method,
                        voice_application_sid=excluded.voice_application_sid,
                        trunk_sid=excluded.trunk_sid,
                        synced_at=excluded.synced_at,updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        item.sid,
                        item.e164,
                        item.friendly_name,
                        derived_country,
                        item.region,
                        json.dumps(
                            item.capabilities,
                            ensure_ascii=True,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        item.voice_url,
                        item.voice_method,
                        item.status_callback_url,
                        item.status_callback_method,
                        item.voice_application_sid,
                        item.trunk_sid,
                        synced_at,
                    ),
                )
                if item.capabilities.get("voice") is not True:
                    await conn.execute(
                        """
                        UPDATE TELEPHONY_NUMBERS
                        SET enabled=0,inbound_enabled=0,is_outbound_default=0,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE provider_number_sid=?
                        """,
                        (item.sid,),
                    )
            await conn.execute(
                """
                INSERT INTO SYSTEM_CONFIG(key,value,description,updated_at)
                VALUES(?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,updated_at=CURRENT_TIMESTAMP
                """,
                (
                    NUMBER_INVENTORY_SYNC_CONFIG_KEY,
                    synced_at,
                    "Last authoritative Twilio number inventory sync",
                ),
            )
        return {"synced": len(remote_numbers), "synced_at": synced_at}

    async def configure_number(
        self,
        number_id: int,
        *,
        enabled: bool,
        inbound_enabled: bool,
        is_outbound_default: bool,
        confirm_affected_bindings: bool = False,
        repair_webhook: bool = False,
    ) -> dict[str, Any]:
        normalized_id = _positive_integer(number_id, "number id")
        if inbound_enabled and not enabled:
            raise TelephonyAdminError("Inbound routing requires an enabled number")
        if is_outbound_default and not enabled:
            raise TelephonyAdminError("The outbound default must remain enabled")

        async with self._connection_factory(readonly=True) as conn:
            row = await _number_by_id(conn, normalized_id)
            inventory_sync_at = await _inventory_sync_at(conn)
            _require_number_available_for_configuration(
                row,
                inventory_sync_at=inventory_sync_at,
                enabled=enabled,
                inbound_enabled=inbound_enabled,
                is_outbound_default=is_outbound_default,
            )
            if inbound_enabled or is_outbound_default:
                await _require_current_canonical_voice(conn)
            affected = await _explicit_binding_count(conn, normalized_id)
            if not enabled and affected and not confirm_affected_bindings:
                raise TelephonyAdminConflict(
                    f"Disabling this number affects {affected} explicit bindings"
                )
            if bool(row["is_outbound_default"]) and not is_outbound_default:
                cursor = await conn.execute(
                    """
                    SELECT COUNT(*) FROM TELEPHONY_NUMBERS
                    WHERE id<>? AND enabled=1 AND is_outbound_default=1
                    """,
                    (normalized_id,),
                )
                if int((await cursor.fetchone())[0]) != 1:
                    raise TelephonyAdminConflict(
                        "Choose another outbound default before clearing this one"
                    )
            should_configure_voice = inbound_enabled and (
                not bool(row["inbound_enabled"]) or repair_webhook
            )
            if should_configure_voice:
                readiness = (
                    await self._readiness_state(
                        conn,
                        credentials=self._credential_provider(),
                    )
                )["readiness"]
                blockers = list(readiness["blocking_reasons"])
                if is_outbound_default:
                    blockers = [
                        item
                        for item in blockers
                        if item != "outbound_default_number_missing"
                    ]
                if repair_webhook:
                    blockers = [
                        item
                        for item in blockers
                        if item != "inbound_voice_webhook_drift"
                    ]
                if blockers:
                    raise TelephonyAdminConflict(
                        "Inbound cannot be enabled: "
                        + ", ".join(blockers)
                    )

        expected_voice_url: str | None = None
        expected_status_callback_url: str | None = None
        if should_configure_voice:
            expected_voice_url = canonical_twilio_url(
                "/webhooks/twilio/voice/inbound"
            )
            expected_status_callback_url = canonical_twilio_url(
                "/webhooks/twilio/voice/inbound-status"
            )
            credentials = self._require_twilio_credentials()
            client = self._number_client_factory(credentials)
            try:
                provider_number = await client.update_incoming_number_voice(
                    str(row["provider_number_sid"]),
                    voice_url=expected_voice_url,
                    voice_method="POST",
                    status_callback_url=expected_status_callback_url,
                    status_callback_method="POST",
                )
                if (
                    not isinstance(provider_number, IncomingPhoneNumber)
                    or provider_number.sid != str(row["provider_number_sid"])
                    or provider_number.voice_url != expected_voice_url
                    or str(provider_number.voice_method or "").upper() != "POST"
                    or provider_number.status_callback_url
                    != expected_status_callback_url
                    or str(
                        provider_number.status_callback_method or ""
                    ).upper()
                    != "POST"
                    or provider_number.voice_application_sid is not None
                    or provider_number.trunk_sid is not None
                ):
                    raise TelephonyAdminUnavailable(
                        "Twilio did not confirm the exact Voice webhook configuration"
                    )
            except TelephonyAdminUnavailable:
                raise
            except Exception as exc:
                raise TelephonyAdminUnavailable(
                    "Twilio Voice webhook could not be configured"
                ) from exc
            finally:
                await _close_client(client)

        async with self._write() as conn:
            current = await _number_by_id(conn, normalized_id)
            inventory_sync_at = await _inventory_sync_at(conn)
            _require_number_available_for_configuration(
                current,
                inventory_sync_at=inventory_sync_at,
                enabled=enabled,
                inbound_enabled=inbound_enabled,
                is_outbound_default=is_outbound_default,
            )
            if inbound_enabled or is_outbound_default:
                await _require_current_canonical_voice(conn)
            affected_now = await _explicit_binding_count(conn, normalized_id)
            if not enabled and affected_now and not confirm_affected_bindings:
                raise TelephonyAdminConflict(
                    f"Disabling this number affects {affected_now} explicit bindings"
                )
            if is_outbound_default:
                await conn.execute(
                    "UPDATE TELEPHONY_NUMBERS SET is_outbound_default=0 "
                    "WHERE id<>? AND is_outbound_default=1",
                    (normalized_id,),
                )
            elif bool(current["is_outbound_default"]):
                raise TelephonyAdminConflict(
                    "Choose another outbound default before clearing this one"
                )
            await conn.execute(
                """
                UPDATE TELEPHONY_NUMBERS
                SET enabled=?,inbound_enabled=?,is_outbound_default=?,
                    voice_url=COALESCE(?,voice_url),
                    voice_method=CASE WHEN ? IS NULL THEN voice_method ELSE 'POST' END,
                    status_callback_url=COALESCE(?,status_callback_url),
                    status_callback_method=CASE
                        WHEN ? IS NULL THEN status_callback_method ELSE 'POST' END,
                    voice_application_sid=CASE
                        WHEN ? IS NULL THEN voice_application_sid ELSE NULL END,
                    trunk_sid=CASE
                        WHEN ? IS NULL THEN trunk_sid ELSE NULL END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    int(enabled),
                    int(inbound_enabled),
                    int(is_outbound_default),
                    expected_voice_url,
                    expected_voice_url,
                    expected_status_callback_url,
                    expected_status_callback_url,
                    expected_voice_url,
                    expected_voice_url,
                    normalized_id,
                ),
            )
            updated = await _number_by_id(conn, normalized_id)
            config = await load_telephony_config(conn=conn)
            if config.enabled and (inbound_enabled or is_outbound_default):
                billing = await phone_billing_readiness(conn)
                if not billing["ready"]:
                    raise TelephonyAdminConflict(
                        "Number cannot be enabled without telephone billing "
                        "rates: " + ", ".join(billing["missing_rates"])
                    )
        return _public_number(
            updated,
            callbacks=_callback_urls_or_error(),
            inventory_sync_at=inventory_sync_at,
        )

    async def save_billing_rate(
        self,
        values: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Store one explicit rate while preserving enabled-runtime readiness."""

        try:
            async with self._write() as conn:
                rate_id = await upsert_phone_billing_rate(
                    conn,
                    provider=str(values.get("provider") or ""),
                    component_type=str(values.get("component_type") or ""),
                    direction=str(values.get("direction") or ""),
                    from_country=str(values.get("from_country") or ""),
                    to_country=str(values.get("to_country") or ""),
                    unit=str(values.get("unit") or ""),
                    provider_rate_per_unit=float(
                        values.get("provider_rate_per_unit", -1)
                    ),
                    customer_rate_per_unit=float(
                        values.get("customer_rate_per_unit", -1)
                    ),
                    currency=str(values.get("currency") or ""),
                    service_id=values.get("service_id"),
                    active=bool(values.get("active", True)),
                )
                config = await load_telephony_config(conn=conn)
                readiness = await phone_billing_readiness(conn)
                if config.enabled and not readiness["ready"]:
                    raise TelephonyAdminConflict(
                        "An enabled telephone channel must retain all billing "
                        "rates: " + ", ".join(readiness["missing_rates"])
                    )
                cursor = await conn.execute(
                    """
                    SELECT r.*,s.name AS service_name
                    FROM PHONE_BILLING_RATES r
                    LEFT JOIN SERVICES s ON s.id=r.service_id WHERE r.id=?
                    """,
                    (rate_id,),
                )
                row = await cursor.fetchone()
        except (PhoneBillingConfigurationError, TypeError, ValueError) as exc:
            raise TelephonyAdminError(str(exc)) from exc
        if row is None:
            raise TelephonyAdminUnavailable("Telephone billing rate was not stored")
        return _public_billing_rate(dict(row))

    async def publish_global_audio(
        self,
        *,
        billing_user_id: int,
        voice_id: int,
        greetings: Mapping[str, AdminGreetingList],
        notices: Mapping[str, str],
    ) -> dict[str, Any]:
        if self._global_audio_publisher is None:
            raise TelephonyAdminUnavailable(
                "Global phone audio rendering is not registered"
            )
        admin_id = _positive_integer(billing_user_id, "billing user id")
        normalized_voice_id = _positive_integer(voice_id, "voice id")
        normalized_greetings = _normalize_global_greetings(greetings)
        normalized_notices = _normalize_notice_set(notices)

        async with self._write() as conn:
            await _require_publishable_voice(conn, normalized_voice_id)
            revision = await _next_global_revision(conn)
            for direction in sorted(GREETING_DIRECTIONS):
                item = normalized_greetings[direction]
                await stage_greeting_revision(
                    conn,
                    scope="global",
                    prompt_id=None,
                    revision=revision,
                    direction=direction,
                    mode=item.mode,
                    greetings=tuple(
                        GreetingInput(phrase.literal_text, phrase.enabled)
                        for phrase in item.phrases
                    ),
                    fixed_index=item.fixed_index,
                )
            await stage_technical_notice_revision(
                conn,
                revision=revision,
                notices=normalized_notices,
                updated_by=admin_id,
            )
            await _stage_custom_prompt_greeting_copies(
                conn,
                revision=revision,
            )
            await _stage_custom_prompt_notice_copies(
                conn,
                revision=revision,
                updated_by=admin_id,
            )
            prompt_active_revisions = await _snapshot_prompt_audio_revisions(conn)

        publication = GlobalAudioPublication(
            revision=revision,
            voice_id=normalized_voice_id,
            billing_user_id=admin_id,
            greetings=normalized_greetings,
            notices=normalized_notices,
            prompt_active_revisions=prompt_active_revisions,
        )
        try:
            result = await self._global_audio_publisher(publication)
        except Exception as exc:
            raise TelephonyAdminUnavailable(
                "Global phone audio generation failed; the prior revision remains active"
            ) from exc
        if (
            not isinstance(result, GlobalAudioPublishResult)
            or result.revision != revision
            or result.status != "activated"
            or not result.activation_id
        ):
            raise TelephonyAdminUnavailable(
                "Global phone audio publisher did not confirm activation"
            )
        return {
            "revision": result.revision,
            "activation_id": result.activation_id,
            "status": result.status,
        }

    def _require_twilio_credentials(self) -> TelephonyCredentials:
        credentials = self._credential_provider()
        if not credentials.account_sid or not credentials.provider_credential:
            raise TelephonyAdminUnavailable("Twilio Voice is not configured")
        return credentials

    async def _readiness_state(
        self,
        conn: Any,
        *,
        credentials: TelephonyCredentials,
        billing_config_overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        callbacks = _callback_urls_or_error()
        cursor = await conn.execute(
            """
            SELECT v.id,v.name,v.voice_code,v.tts_service,
                   COALESCE(v.deprecated,0) AS deprecated,
                   s.name AS service_name
            FROM VOICES v LEFT JOIN SERVICES s ON s.id=v.tts_service
            WHERE COALESCE(v.is_default,0)=1
            ORDER BY v.id
            """
        )
        default_rows = [dict(row) for row in await cursor.fetchall()]
        canonical_provider = None
        canonical_voice_valid = False
        if len(default_rows) == 1:
            default_row = default_rows[0]
            service_name = str(default_row["service_name"] or "").strip()
            canonical_provider = (
                provider_from_service_name(service_name) if service_name else None
            )
            canonical_voice_valid = bool(
                not default_row["deprecated"]
                and str(default_row["voice_code"] or "").strip()
                and default_row["tts_service"] is not None
                and service_name
                and canonical_provider in SUPPORTED_CANONICAL_TTS_PROVIDERS
            )
        inventory_sync_at = await _inventory_sync_at(conn)
        cursor = await conn.execute(
            """
            SELECT synced_at,capabilities_json FROM TELEPHONY_NUMBERS
            WHERE enabled=1 AND is_outbound_default=1
            """
        )
        default_number_rows = [dict(row) for row in await cursor.fetchall()]
        valid_default_numbers = [
            row
            for row in default_number_rows
            if inventory_sync_at
            and row["synced_at"] == inventory_sync_at
            and _safe_json_object(row["capabilities_json"]).get("voice") is True
        ]
        default_number_count = len(valid_default_numbers)
        cursor = await conn.execute(
            """
            SELECT synced_at,capabilities_json,voice_url,voice_method,
                   status_callback_url,status_callback_method,
                   voice_application_sid,trunk_sid
            FROM TELEPHONY_NUMBERS WHERE enabled=1 AND inbound_enabled=1
            """
        )
        expected_voice_url = callbacks.get("voice_inbound")
        expected_status_callback_url = callbacks.get("voice_inbound_status")
        inbound_webhook_drift_count = sum(
            1
            for row in await cursor.fetchall()
            if not inventory_sync_at
            or row["synced_at"] != inventory_sync_at
            or _safe_json_object(row["capabilities_json"]).get("voice") is not True
            or not expected_voice_url
            or row["voice_url"] != expected_voice_url
            or str(row["voice_method"] or "").upper() != "POST"
            or not expected_status_callback_url
            or row["status_callback_url"] != expected_status_callback_url
            or str(row["status_callback_method"] or "").upper() != "POST"
            or row["voice_application_sid"] is not None
            or row["trunk_sid"] is not None
        )
        global_audio = await self._global_bundle_readiness(
            conn,
            default_voice_id=(
                int(default_rows[0]["id"])
                if canonical_voice_valid
                else None
            ),
        )
        billing = await phone_billing_readiness(
            conn,
            config_overrides=billing_config_overrides,
        )
        try:
            ffmpeg_ready = bool(await self._ffmpeg_probe())
        except Exception:
            ffmpeg_ready = False
        reasons: list[str] = []
        if not credentials.account_sid or not credentials.provider_credential:
            reasons.append("twilio_credentials_missing")
        if not credentials.elevenlabs_available:
            reasons.append("elevenlabs_stt_credential_missing")
        if callbacks.get("configured") is not True:
            reasons.append("public_domain_missing_or_invalid")
        if inventory_sync_at is None:
            reasons.append("number_inventory_not_synchronized")
        if default_number_count != 1:
            reasons.append("outbound_default_number_missing")
        if inbound_webhook_drift_count:
            reasons.append("inbound_voice_webhook_drift")
        if len(default_rows) != 1:
            reasons.append("canonical_default_voice_missing")
        elif not canonical_voice_valid:
            reasons.append("canonical_default_voice_invalid")
        elif not credentials.provider_tts_ready(canonical_provider):
            reasons.append("canonical_tts_credential_missing")
        if not global_audio["notices_complete"]:
            reasons.append("technical_notices_incomplete")
        if not global_audio["ready"]:
            reasons.append("global_audio_bundle_not_ready")
        if not billing["ready"]:
            reasons.append("phone_billing_rates_missing")
        if not ffmpeg_ready:
            reasons.append("ffmpeg_unavailable")
        return {
            "canonical_voice_provider": canonical_provider,
            "callbacks": callbacks,
            "readiness": {
                "ready_for_enable": not reasons,
                "inbound_configuration_ready": not any(
                    reason != "outbound_default_number_missing"
                    for reason in reasons
                ),
                "blocking_reasons": reasons,
                "default_number_count": default_number_count,
                "inbound_webhook_drift_count": inbound_webhook_drift_count,
                "canonical_default_voice_count": len(default_rows),
                "canonical_voice_valid": canonical_voice_valid,
                "global_audio_ready": global_audio["ready"],
                "technical_notices_complete": global_audio["notices_complete"],
                "billing_ready": billing["ready"],
                "ffmpeg_ready": ffmpeg_ready,
                "missing_billing_rates": billing["missing_rates"],
            },
        }

    async def _global_bundle_readiness(
        self,
        conn: Any,
        *,
        default_voice_id: int | None,
    ) -> dict[str, Any]:
        cursor = await conn.execute(
            "SELECT value FROM SYSTEM_CONFIG WHERE key=?",
            (GLOBAL_AUDIO_REVISION_CONFIG_KEY,),
        )
        row = await cursor.fetchone()
        try:
            active_revision = int(row[0]) if row and row[0] is not None else 0
        except (TypeError, ValueError):
            active_revision = 0
        cursor = await conn.execute(
            "SELECT MAX(revision) FROM PHONE_TECHNICAL_NOTICE_DEFINITIONS"
        )
        notice_row = await cursor.fetchone()
        latest_notice_revision = int(notice_row[0]) if notice_row and notice_row[0] else 0
        notice_revision = active_revision or latest_notice_revision
        notice_count = 0
        if notice_revision:
            cursor = await conn.execute(
                """
                SELECT COUNT(DISTINCT notice_key)
                FROM PHONE_TECHNICAL_NOTICE_DEFINITIONS WHERE revision=?
                """,
                (notice_revision,),
            )
            notice_count = int((await cursor.fetchone())[0])
        notices_complete = notice_count == len(TECHNICAL_NOTICE_KEYS)
        if active_revision <= 0 or default_voice_id is None:
            return {
                "ready": False,
                "notices_complete": notices_complete,
                "active_revision": active_revision or None,
            }
        cursor = await conn.execute(
            """
            SELECT COUNT(*) FROM VOICE_CANONICAL_ACTIVATIONS
            WHERE scope='global' AND prompt_id IS NULL AND audio_revision=?
              AND target_voice_id=? AND status='activated'
            """,
            (active_revision, default_voice_id),
        )
        activation_count = int((await cursor.fetchone())[0])
        cursor = await conn.execute(
            """
            SELECT id,direction FROM PROMPT_PHONE_GREETINGS
            WHERE scope='global' AND prompt_id IS NULL AND revision=?
              AND enabled=1 ORDER BY id
            """,
            (active_revision,),
        )
        definitions = [dict(row) for row in await cursor.fetchall()]
        cursor = await conn.execute(
            """
            SELECT greeting_id,direction,source_mp3_path,pcmu_path
            FROM PHONE_PROMPT_AUDIO_CACHE
            WHERE prompt_id IS NULL AND revision=? AND voice_id=?
              AND asset_kind='greeting' AND status='ready'
              AND source_mp3_path IS NOT NULL AND pcmu_path IS NOT NULL
            ORDER BY greeting_id
            """,
            (active_revision, default_voice_id),
        )
        greeting_cache = [dict(row) for row in await cursor.fetchall()]
        notice_predicates = " OR ".join(
            "cache_key LIKE ?" for _ in GLOBAL_TECHNICAL_NOTICE_KEYS
        )
        cursor = await conn.execute(
            f"""
            SELECT source_mp3_path,pcmu_path
            FROM PHONE_PROMPT_AUDIO_CACHE
            WHERE prompt_id IS NULL AND revision=? AND voice_id=?
              AND asset_kind='technical_notice' AND status='ready'
              AND ({notice_predicates}) AND source_mp3_path IS NOT NULL
              AND pcmu_path IS NOT NULL
            """,
            (
                active_revision,
                default_voice_id,
                *(
                    f"phone:global:r{active_revision}:notice:{key}:%"
                    for key in sorted(GLOBAL_TECHNICAL_NOTICE_KEYS)
                ),
            ),
        )
        notice_cache = [dict(row) for row in await cursor.fetchall()]
        definition_ids = {int(row["id"]) for row in definitions}
        cached_ids = {int(row["greeting_id"]) for row in greeting_cache}
        directions = {str(row["direction"]) for row in definitions}
        paths_ready = all(
            _private_audio_paths_exist(row)
            for row in (*greeting_cache, *notice_cache)
        )
        ready = (
            activation_count == 1
            and directions == set(GREETING_DIRECTIONS)
            and bool(definition_ids)
            and cached_ids == definition_ids
            and len(greeting_cache) == len(definition_ids)
            and len(notice_cache) == len(GLOBAL_TECHNICAL_NOTICE_KEYS)
            and paths_ready
            and notices_complete
        )
        return {
            "ready": ready,
            "notices_complete": notices_complete,
            "active_revision": active_revision,
        }

    async def _load_numbers(
        self,
        conn: Any,
        callbacks: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        inventory_sync_at = await _inventory_sync_at(conn)
        cursor = await conn.execute(
            """
            SELECT n.*,
                   (SELECT COUNT(*) FROM PHONE_CONVERSATION_BINDINGS b
                    WHERE b.preferred_number_id=n.id AND b.active=1)
                   AS explicit_binding_count
            FROM TELEPHONY_NUMBERS n ORDER BY n.e164,n.id
            """
        )
        return [
            _public_number(
                dict(row),
                callbacks=callbacks,
                inventory_sync_at=inventory_sync_at,
            )
            for row in await cursor.fetchall()
        ]

    async def _load_voices(self, conn: Any) -> list[dict[str, Any]]:
        cursor = await conn.execute(
            """
            SELECT v.id,v.name,v.voice_code,v.is_default,
                   COALESCE(v.deprecated,0) AS deprecated,s.name AS service_name
            FROM VOICES v LEFT JOIN SERVICES s ON s.id=v.tts_service
            ORDER BY COALESCE(v.deprecated,0),v.name,v.id
            """
        )
        return [
            {
                "id": int(row["id"]),
                "name": str(row["name"]),
                "provider": provider_from_service_name(row["service_name"] or ""),
                "is_default": bool(row["is_default"]),
                "deprecated": bool(row["deprecated"]),
                "available": bool(
                    not row["deprecated"]
                    and str(row["voice_code"] or "").strip()
                    and str(row["service_name"] or "").strip()
                    and provider_from_service_name(row["service_name"] or "")
                    in SUPPORTED_CANONICAL_TTS_PROVIDERS
                ),
            }
            for row in await cursor.fetchall()
        ]

    async def _load_global_audio(self, conn: Any) -> dict[str, Any]:
        cursor = await conn.execute(
            "SELECT value FROM SYSTEM_CONFIG WHERE key=?",
            (GLOBAL_AUDIO_REVISION_CONFIG_KEY,),
        )
        row = await cursor.fetchone()
        try:
            active_revision = int(row[0]) if row and row[0] else None
        except (TypeError, ValueError):
            active_revision = None
        cursor = await conn.execute(
            """
            SELECT MAX(revision) FROM (
                SELECT revision FROM PROMPT_PHONE_GREETINGS
                WHERE scope='global' AND prompt_id IS NULL
                UNION ALL
                SELECT revision FROM PHONE_TECHNICAL_NOTICE_DEFINITIONS
            )
            """
        )
        latest_row = await cursor.fetchone()
        latest_revision = int(latest_row[0]) if latest_row and latest_row[0] else None
        greetings: dict[str, list[dict[str, Any]]] = {
            "inbound": [],
            "outbound": [],
        }
        notices = {
            key: DEFAULT_GLOBAL_TECHNICAL_NOTICE_TEXT.get(key, "")
            for key in sorted(TECHNICAL_NOTICE_KEYS)
        }
        if latest_revision:
            cursor = await conn.execute(
                """
                SELECT direction,literal_text,enabled,is_fixed_selection,display_order
                FROM PROMPT_PHONE_GREETINGS
                WHERE scope='global' AND prompt_id IS NULL AND revision=?
                ORDER BY direction,display_order,id
                """,
                (latest_revision,),
            )
            for greeting in await cursor.fetchall():
                greetings[str(greeting["direction"])].append(
                    {
                        "literal_text": str(greeting["literal_text"]),
                        "enabled": bool(greeting["enabled"]),
                        "fixed": bool(greeting["is_fixed_selection"]),
                    }
                )
            cursor = await conn.execute(
                """
                SELECT notice_key,literal_text
                FROM PHONE_TECHNICAL_NOTICE_DEFINITIONS WHERE revision=?
                """,
                (latest_revision,),
            )
            for notice in await cursor.fetchall():
                notices[str(notice["notice_key"])] = str(notice["literal_text"])
        cursor = await conn.execute(
            """
            SELECT id,audio_revision,status,
                   last_error IS NOT NULL AS has_error,created_at,activated_at
            FROM VOICE_CANONICAL_ACTIVATIONS
            WHERE scope='global' AND prompt_id IS NULL
            ORDER BY audio_revision DESC,created_at DESC LIMIT 1
            """
        )
        activation = await cursor.fetchone()
        return {
            "active_revision": active_revision,
            "latest_definition_revision": latest_revision,
            "greetings": greetings,
            "notices": notices,
            "last_activation": dict(activation) if activation is not None else None,
        }

    async def _load_operation(self, conn: Any) -> dict[str, Any]:
        cursor = await conn.execute(
            """
            SELECT c.id,c.owner_user_id,c.conversation_id,c.direction,c.status,
                   c.from_e164,c.to_e164,c.answered_by,
                   c.duration_seconds,c.estimated_cost,c.final_cost,c.currency,
                   c.termination_reason,c.created_at,c.answered_at,c.ended_at,
                   p.display_name AS contact_name,n.e164 AS line_e164,
                   EXISTS(SELECT 1 FROM PHONE_HANGUP_ATTEMPTS h
                          WHERE h.call_id=c.id AND h.state='unresolved')
                          AS can_retry_hangup
            FROM PHONE_CALLS c
            LEFT JOIN PHONE_CONTACTS p ON p.id=c.contact_id
            LEFT JOIN TELEPHONY_NUMBERS n ON n.id=c.telephony_number_id
            WHERE c.deleted_at IS NULL
            ORDER BY c.created_at DESC LIMIT 100
            """
        )
        calls = [dict(row) for row in await cursor.fetchall()]
        cursor = await conn.execute(
            """
            SELECT j.id,j.owner_user_id,j.conversation_id,j.scheduled_at_utc,
                   j.timezone_name,j.origin,j.status,j.last_error_code,
                   j.last_error_detail,j.created_at,p.display_name AS contact_name,
                   n.e164 AS line_e164
            FROM PHONE_CALL_JOBS j
            LEFT JOIN PHONE_CONTACTS p ON p.id=j.contact_id
            LEFT JOIN TELEPHONY_NUMBERS n ON n.id=j.telephony_number_id
            WHERE j.status IN ('scheduled','dispatching','needs_attention',
                               'missed','conflict')
            ORDER BY j.scheduled_at_utc DESC LIMIT 100
            """
        )
        jobs = [dict(row) for row in await cursor.fetchall()]
        for job in jobs:
            if job.get("last_error_detail"):
                job["last_error_detail"] = "Requires operational attention"
        cursor = await conn.execute(
            """
            SELECT component_type,currency,SUM(provider_cost) AS provider_cost,
                   SUM(customer_charge) AS customer_charge,COUNT(*) AS items
            FROM PHONE_CALL_COST_COMPONENTS
            WHERE created_at >= datetime('now','-30 days')
            GROUP BY component_type,currency ORDER BY component_type,currency
            """
        )
        costs = [dict(row) for row in await cursor.fetchall()]
        return {"calls": calls, "jobs": jobs, "costs_30_days": costs}

    async def _load_billing(self, conn: Any) -> dict[str, Any]:
        cursor = await conn.execute(
            """
            SELECT r.*,s.name AS service_name
            FROM PHONE_BILLING_RATES r
            LEFT JOIN SERVICES s ON s.id=r.service_id
            ORDER BY r.provider,r.component_type,r.direction,
                     r.from_country,r.to_country,r.id
            """
        )
        rates = [_public_billing_rate(dict(row)) for row in await cursor.fetchall()]
        cursor = await conn.execute(
            """
            SELECT state,currency,COUNT(*) AS items,
                   COALESCE(SUM(estimated_provider_cost),0) AS estimated_provider_cost,
                   COALESCE(SUM(estimated_customer_charge),0) AS estimated_customer_charge,
                   COALESCE(SUM(final_provider_cost),0) AS final_provider_cost,
                   COALESCE(SUM(final_customer_charge),0) AS final_customer_charge,
                   COALESCE(SUM(platform_absorbed),0) AS platform_absorbed_items
            FROM PHONE_CALL_COST_COMPONENTS
            WHERE created_at>=datetime('now','-30 days')
            GROUP BY state,currency ORDER BY state,currency
            """
        )
        stats = [dict(row) for row in await cursor.fetchall()]
        return {"rates": rates, "stats_30_days": stats}

    async def _load_diagnostics(self, conn: Any) -> dict[str, Any]:
        attention: dict[str, int] = {}
        checks = {
            "calls": (
                "PHONE_CALLS",
                "SELECT COUNT(*) FROM PHONE_CALLS WHERE deleted_at IS NULL "
                "AND status IN ('dispatch_unknown','unresolved')",
            ),
            "jobs": (
                "PHONE_CALL_JOBS",
                "SELECT COUNT(*) FROM PHONE_CALL_JOBS WHERE status='needs_attention' "
                "OR (status='dispatching' AND datetime(lease_until)<CURRENT_TIMESTAMP)",
            ),
            "memory": (
                "PHONE_MEMORY_OUTBOX",
                "SELECT COUNT(*) FROM PHONE_MEMORY_OUTBOX WHERE status='needs_attention' "
                "OR (status='processing' AND datetime(lease_until)<CURRENT_TIMESTAMP)",
            ),
            "renders": (
                "PHONE_AUDIO_RENDER_ATTEMPTS",
                "SELECT COUNT(*) FROM PHONE_AUDIO_RENDER_ATTEMPTS "
                "WHERE needs_attention=1",
            ),
            "purges": (
                "PHONE_DATA_PURGE_JOBS",
                "SELECT COUNT(*) FROM PHONE_DATA_PURGE_JOBS "
                "WHERE status='needs_attention'",
            ),
            "billing": (
                "PHONE_CALL_COST_COMPONENTS",
                "SELECT COUNT(*) FROM PHONE_CALL_COST_COMPONENTS "
                "WHERE state='needs_attention'",
            ),
        }
        for key, (table, query) in checks.items():
            if await _table_exists(conn, table):
                cursor = await conn.execute(query)
                attention[key] = int((await cursor.fetchone())[0])
            else:
                attention[key] = 0
        inventory_sync_at = await _inventory_sync_at(conn)
        cursor = await conn.execute(
            "SELECT synced_at,capabilities_json FROM TELEPHONY_NUMBERS"
        )
        attention["numbers"] = sum(
            1
            for row in await cursor.fetchall()
            if not inventory_sync_at
            or row["synced_at"] != inventory_sync_at
            or _safe_json_object(row["capabilities_json"]).get("voice") is not True
        )
        webhooks: dict[str, Any] = {"valid": None, "invalid": None}
        if await _table_exists(conn, "PHONE_CALL_EVENTS"):
            for label, signature_valid in (("valid", 1), ("invalid", 0)):
                cursor = await conn.execute(
                    """
                    SELECT call_id,event_type,received_at
                    FROM PHONE_CALL_EVENTS WHERE signature_valid=?
                    ORDER BY received_at DESC,id DESC LIMIT 1
                    """,
                    (signature_valid,),
                )
                row = await cursor.fetchone()
                webhooks[label] = dict(row) if row is not None else None
        purge_jobs: list[dict[str, Any]] = []
        if await _table_exists(conn, "PHONE_DATA_PURGE_JOBS"):
            cursor = await conn.execute(
                """
                SELECT id,purge_scope,call_id_snapshot,conversation_id_snapshot,
                       attempt_count,updated_at
                FROM PHONE_DATA_PURGE_JOBS WHERE status='needs_attention'
                ORDER BY updated_at DESC,id DESC LIMIT 50
                """
            )
            purge_jobs = [dict(row) for row in await cursor.fetchall()]
        return {
            "attention": attention,
            "last_webhooks": webhooks,
            "purge_jobs": purge_jobs,
        }


_ADMIN_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_SENSITIVE_EVENT_KEYS = frozenset(
    {
        "sid",
        "callsid",
        "recordingsid",
        "streamsid",
        "token",
        "authorization",
        "auth",
        "signature",
        "path",
        "url",
        "voiceurl",
        "recordingurl",
    }
)
_SENSITIVE_EVENT_VALUE_PATTERNS = (
    re.compile(r"(?i)\b(?:https?|wss?)://\S+"),
    re.compile(r"(?i)(?:^|[\s\"'=])(?:[a-z]:[\\/])[^\s\"']+"),
    re.compile(r"(?<!\w)/(?:[^/\s]+/)+[^/\s]+"),
    re.compile(
        r"\b(?:AC|AP|CA|CH|CR|MM|NO|PN|RE|RM|SK|SM|VE|ZS)"
        r"[0-9A-Fa-f]{32}\b"
    ),
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)\b(?:sk|pk|rk)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+"),
)


def _admin_identifier(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if _ADMIN_IDENTIFIER_RE.fullmatch(normalized) is None:
        raise TelephonyAdminError(f"Invalid {label}")
    return normalized


def _redacted_event_payload(value: Any) -> dict[str, Any]:
    parsed = _safe_json_object(value)

    def redact(item: Any, *, depth: int = 0) -> Any:
        if depth >= 4:
            return "[limited]"
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for raw_key, raw_value in list(item.items())[:50]:
                key = str(raw_key)[:80]
                compact = re.sub(r"[^a-z0-9]", "", key.lower())
                if (
                    compact in _SENSITIVE_EVENT_KEYS
                    or compact.endswith(("sid", "url"))
                    or compact.startswith(("auth", "path"))
                    or compact.endswith(
                        (
                            "credential",
                            "credentials",
                            "apikey",
                            "accesskey",
                            "privatekey",
                        )
                    )
                    or any(
                        marker in compact
                        for marker in (
                            "token",
                            "secret",
                            "password",
                            "signature",
                            "path",
                        )
                    )
                ):
                    result[key] = "[redacted]"
                else:
                    result[key] = redact(raw_value, depth=depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            return [redact(child, depth=depth + 1) for child in list(item)[:50]]
        if isinstance(item, (int, float, bool)) or item is None:
            return item
        text = str(item)[:500]
        if any(pattern.search(text) for pattern in _SENSITIVE_EVENT_VALUE_PATTERNS):
            return "[redacted]"
        return text

    redacted = redact(parsed)
    encoded = json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > 16_000:
        return {"_limited": True}
    return redacted


def _public_billing_rate(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "provider": str(row["provider"]),
        "component_type": str(row["component_type"]),
        "direction": str(row["direction"]),
        "from_country": str(row["from_country"]),
        "to_country": str(row["to_country"]),
        "unit": str(row["unit"]),
        "provider_rate_per_unit": float(row["provider_rate_per_unit"]),
        "customer_rate_per_unit": float(row["customer_rate_per_unit"]),
        "currency": str(row["currency"]),
        "service_id": int(row["service_id"]) if row["service_id"] else None,
        "service_name": row.get("service_name"),
        "active": bool(row["active"]),
        "updated_at": row.get("updated_at"),
    }


async def _number_by_id(conn: Any, number_id: int) -> dict[str, Any]:
    cursor = await conn.execute(
        "SELECT * FROM TELEPHONY_NUMBERS WHERE id=?", (int(number_id),)
    )
    row = await cursor.fetchone()
    if row is None:
        raise TelephonyAdminNotFound("Telephony number not found")
    return dict(row)


async def _explicit_binding_count(conn: Any, number_id: int) -> int:
    cursor = await conn.execute(
        """
        SELECT COUNT(*) FROM PHONE_CONVERSATION_BINDINGS
        WHERE preferred_number_id=? AND active=1
        """,
        (int(number_id),),
    )
    return int((await cursor.fetchone())[0])


async def _inventory_sync_at(conn: Any) -> str | None:
    cursor = await conn.execute(
        "SELECT value FROM SYSTEM_CONFIG WHERE key=?",
        (NUMBER_INVENTORY_SYNC_CONFIG_KEY,),
    )
    row = await cursor.fetchone()
    value = str(row[0] or "").strip() if row is not None else ""
    return value or None


def _require_number_available_for_configuration(
    row: Mapping[str, Any],
    *,
    inventory_sync_at: str | None,
    enabled: bool,
    inbound_enabled: bool,
    is_outbound_default: bool,
) -> None:
    if not (enabled or inbound_enabled or is_outbound_default):
        return
    if not inventory_sync_at or row.get("synced_at") != inventory_sync_at:
        raise TelephonyAdminConflict(
            "The number is absent from the current Twilio inventory"
        )
    capabilities = _safe_json_object(row.get("capabilities_json"))
    if (inbound_enabled or is_outbound_default) and capabilities.get("voice") is not True:
        raise TelephonyAdminConflict("The number has no Twilio Voice capability")


def _public_number(
    row: Mapping[str, Any],
    *,
    callbacks: Mapping[str, Any],
    inventory_sync_at: str | None,
) -> dict[str, Any]:
    expected = callbacks.get("voice_inbound")
    expected_status = callbacks.get("voice_inbound_status")
    voice_url = row.get("voice_url")
    capabilities = _safe_json_object(row.get("capabilities_json"))
    provider_present = bool(
        inventory_sync_at and row.get("synced_at") == inventory_sync_at
    )
    voice_capable = capabilities.get("voice") is True
    return {
        "id": int(row["id"]),
        "e164": str(row["e164"]),
        "friendly_name": row.get("friendly_name"),
        "iso_country": row.get("iso_country"),
        "region": row.get("region"),
        "capabilities": capabilities,
        "provider_present": provider_present,
        "voice_capable": voice_capable,
        "needs_attention": not provider_present or not voice_capable,
        "drift_reason": (
            "missing_from_provider"
            if not provider_present
            else ("voice_capability_missing" if not voice_capable else None)
        ),
        "enabled": bool(row.get("enabled")),
        "inbound_enabled": bool(row.get("inbound_enabled")),
        "is_outbound_default": bool(row.get("is_outbound_default")),
        "voice_method": row.get("voice_method"),
        "voice_webhook_matches": bool(
            expected
            and voice_url == expected
            and str(row.get("voice_method") or "").upper() == "POST"
        ),
        "status_callback_matches": bool(
            expected_status
            and row.get("status_callback_url") == expected_status
            and str(row.get("status_callback_method") or "").upper() == "POST"
        ),
        "webhooks_match": bool(
            expected
            and voice_url == expected
            and str(row.get("voice_method") or "").upper() == "POST"
            and expected_status
            and row.get("status_callback_url") == expected_status
            and str(row.get("status_callback_method") or "").upper() == "POST"
            and row.get("voice_application_sid") is None
            and row.get("trunk_sid") is None
        ),
        "alternate_voice_routing_present": bool(
            row.get("voice_application_sid") is not None
            or row.get("trunk_sid") is not None
        ),
        "synced_at": row.get("synced_at"),
        "explicit_binding_count": int(row.get("explicit_binding_count") or 0),
    }


def _callback_urls_or_error() -> dict[str, Any]:
    try:
        return {
            "configured": True,
            "voice_inbound": canonical_twilio_url(
                "/webhooks/twilio/voice/inbound"
            ),
            "voice_inbound_status": canonical_twilio_url(
                "/webhooks/twilio/voice/inbound-status"
            ),
            "media_stream": canonical_twilio_url(
                "/ws/twilio/media-stream", websocket=True
            ),
        }
    except TwilioCanonicalURLConfigurationError as exc:
        return {"configured": False, "error": str(exc)}


async def _require_publishable_voice(conn: Any, voice_id: int) -> None:
    cursor = await conn.execute(
        """
        SELECT v.voice_code,v.tts_service,COALESCE(v.deprecated,0),s.name
        FROM VOICES v LEFT JOIN SERVICES s ON s.id=v.tts_service WHERE v.id=?
        """,
        (voice_id,),
    )
    row = await cursor.fetchone()
    if (
        row is None
        or bool(row[2])
        or not str(row[0] or "").strip()
        or row[1] is None
        or not str(row[3] or "").strip()
        or provider_from_service_name(str(row[3]))
        not in SUPPORTED_CANONICAL_TTS_PROVIDERS
    ):
        raise TelephonyAdminConflict("Canonical voice is unavailable")


async def _require_current_canonical_voice(conn: Any) -> None:
    cursor = await conn.execute(
        """
        SELECT v.voice_code,v.tts_service,COALESCE(v.deprecated,0),s.name
        FROM VOICES v LEFT JOIN SERVICES s ON s.id=v.tts_service
        WHERE COALESCE(v.is_default,0)=1 ORDER BY v.id
        """
    )
    rows = await cursor.fetchall()
    if len(rows) != 1:
        raise TelephonyAdminConflict("Canonical voice is unavailable")
    row = rows[0]
    if (
        bool(row[2])
        or not str(row[0] or "").strip()
        or row[1] is None
        or not str(row[3] or "").strip()
        or provider_from_service_name(str(row[3]))
        not in SUPPORTED_CANONICAL_TTS_PROVIDERS
    ):
        raise TelephonyAdminConflict("Canonical voice is unavailable")


async def _next_global_revision(conn: Any) -> int:
    cursor = await conn.execute(
        """
        SELECT COALESCE(MAX(revision),0) FROM (
            SELECT revision FROM PROMPT_PHONE_GREETINGS
            WHERE revision IS NOT NULL
            UNION ALL SELECT revision FROM PHONE_TECHNICAL_NOTICE_DEFINITIONS
            UNION ALL SELECT revision
            FROM PROMPT_PHONE_TECHNICAL_NOTICE_DEFINITIONS
            UNION ALL SELECT revision FROM PHONE_PROMPT_AUDIO_CACHE
            WHERE revision IS NOT NULL
            UNION ALL SELECT audio_revision AS revision
            FROM VOICE_CANONICAL_ACTIVATIONS
            WHERE audio_revision IS NOT NULL
        )
        """
    )
    return int((await cursor.fetchone())[0]) + 1


async def _stage_custom_prompt_greeting_copies(
    conn: Any,
    *,
    revision: int,
) -> None:
    """Copy active custom lists into a new immutable global bundle revision."""

    cursor = await conn.execute(
        """
        SELECT p.id AS prompt_id,s.active_audio_revision,
               s.inbound_greeting_mode,s.outbound_greeting_mode
        FROM PROMPTS p
        JOIN PROMPT_PHONE_SETTINGS s ON s.prompt_id=p.id
        WHERE s.inbound_greeting_mode<>'inherit'
           OR s.outbound_greeting_mode<>'inherit'
        ORDER BY p.id
        """
    )
    settings_rows = [dict(row) for row in await cursor.fetchall()]
    for settings in settings_rows:
        prompt_id = int(settings["prompt_id"])
        active_revision = settings["active_audio_revision"]
        if active_revision is None:
            raise TelephonyAdminConflict(
                "A custom prompt greeting list has no active audio revision"
            )
        for direction in sorted(GREETING_DIRECTIONS):
            mode = str(settings[f"{direction}_greeting_mode"])
            if mode == "inherit":
                continue
            cursor = await conn.execute(
                """
                SELECT literal_text,enabled,is_fixed_selection,display_order
                FROM PROMPT_PHONE_GREETINGS
                WHERE scope='prompt' AND prompt_id=? AND revision=?
                  AND direction=?
                ORDER BY display_order,id
                """,
                (prompt_id, int(active_revision), direction),
            )
            definitions = [dict(row) for row in await cursor.fetchall()]
            enabled = [row for row in definitions if bool(row["enabled"])]
            fixed = [row for row in enabled if bool(row["is_fixed_selection"])]
            if (
                not enabled
                or (mode == "fixed" and len(fixed) != 1)
                or (mode == "random" and fixed)
            ):
                raise TelephonyAdminConflict(
                    "A custom prompt greeting list is incomplete"
                )
            for definition in definitions:
                await conn.execute(
                    """
                    INSERT INTO PROMPT_PHONE_GREETINGS(
                        scope,prompt_id,direction,literal_text,enabled,
                        is_fixed_selection,display_order,revision
                    ) VALUES('prompt',?,?,?,?,?,?,?)
                    """,
                    (
                        prompt_id,
                        direction,
                        definition["literal_text"],
                        int(bool(definition["enabled"])),
                        int(bool(definition["is_fixed_selection"])),
                        int(definition["display_order"]),
                        int(revision),
                    ),
                )


async def _stage_custom_prompt_notice_copies(
    conn: Any,
    *,
    revision: int,
    updated_by: int,
) -> None:
    """Carry active exact-seven prompt notice overrides into a global bundle."""

    cursor = await conn.execute(
        """
        SELECT p.id AS prompt_id,s.active_audio_revision
        FROM PROMPTS p
        JOIN PROMPT_PHONE_SETTINGS s ON s.prompt_id=p.id
        WHERE s.active_audio_revision IS NOT NULL
        ORDER BY p.id
        """
    )
    for row in await cursor.fetchall():
        prompt_id = int(row["prompt_id"])
        active_revision = int(row["active_audio_revision"])
        try:
            active = await load_prompt_technical_notice_revision(
                conn,
                prompt_id=prompt_id,
                revision=active_revision,
            )
        except PhoneGreetingConfigurationError as exc:
            raise TelephonyAdminConflict(
                "A custom prompt technical notice set is incomplete"
            ) from exc
        if active is None:
            continue
        await stage_prompt_technical_notice_revision(
            conn,
            prompt_id=prompt_id,
            revision=revision,
            notices=active.notices,
            updated_by=updated_by,
        )


async def _snapshot_prompt_audio_revisions(
    conn: Any,
) -> dict[int, int | None]:
    """Capture every prompt's active revision and reject in-flight mutations."""

    cursor = await conn.execute(
        """
        SELECT p.id AS prompt_id,s.active_audio_revision,s.pending_audio_revision
        FROM PROMPTS p
        LEFT JOIN PROMPT_PHONE_SETTINGS s ON s.prompt_id=p.id
        ORDER BY p.id
        """
    )
    snapshot: dict[int, int | None] = {}
    for row in await cursor.fetchall():
        if row["pending_audio_revision"] is not None:
            raise TelephonyAdminConflict(
                "A prompt phone audio update is already pending"
            )
        snapshot[int(row["prompt_id"])] = (
            None
            if row["active_audio_revision"] is None
            else int(row["active_audio_revision"])
        )
    return snapshot


def _normalize_global_greetings(
    greetings: Mapping[str, AdminGreetingList],
) -> dict[str, AdminGreetingList]:
    if set(greetings) != set(GREETING_DIRECTIONS):
        raise TelephonyAdminError("Inbound and outbound greetings are required")
    normalized: dict[str, AdminGreetingList] = {}
    for direction in GREETING_DIRECTIONS:
        item = greetings[direction]
        mode = str(item.mode or "").strip().lower()
        if mode not in {"fixed", "random"}:
            raise TelephonyAdminError("Global greeting mode must be fixed or random")
        if not item.phrases or len(item.phrases) > 50:
            raise TelephonyAdminError("Each global greeting list needs 1 to 50 phrases")
        phrases = tuple(
            AdminGreetingPhrase(
                normalize_literal_text(phrase.literal_text), bool(phrase.enabled)
            )
            for phrase in item.phrases
        )
        if not any(phrase.enabled for phrase in phrases):
            raise TelephonyAdminError("Each global greeting list needs an enabled phrase")
        fixed_index = item.fixed_index
        if mode == "fixed":
            if (
                isinstance(fixed_index, bool)
                or not isinstance(fixed_index, int)
                or not 0 <= fixed_index < len(phrases)
                or not phrases[fixed_index].enabled
            ):
                raise TelephonyAdminError("The fixed global greeting is invalid")
        elif fixed_index is not None:
            raise TelephonyAdminError("Random greeting lists do not use fixed_index")
        normalized[direction] = AdminGreetingList(mode, phrases, fixed_index)
    return normalized


def _normalize_notice_set(notices: Mapping[str, str]) -> dict[str, str]:
    if set(notices) != set(TECHNICAL_NOTICE_KEYS):
        raise TelephonyAdminError("All technical notices are required")
    try:
        return {
            str(key): normalize_literal_text(value)
            for key, value in notices.items()
        }
    except PhoneGreetingConfigurationError as exc:
        raise TelephonyAdminError(str(exc)) from exc


def _validate_remote_number_set(numbers: Sequence[IncomingPhoneNumber]) -> None:
    if any(not isinstance(item, IncomingPhoneNumber) for item in numbers):
        raise TelephonyAdminUnavailable("Twilio number inventory is invalid")
    sids = [item.sid for item in numbers]
    e164s = [item.e164 for item in numbers]
    if len(sids) != len(set(sids)) or len(e164s) != len(set(e164s)):
        raise TelephonyAdminUnavailable("Twilio number inventory contains duplicates")


async def _close_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        await close()


async def _table_exists(conn: Any, name: str) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return await cursor.fetchone() is not None


def _safe_json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _private_audio_paths_exist(row: Mapping[str, Any]) -> bool:
    try:
        mp3 = Path(str(row["source_mp3_path"])).resolve()
        pcmu = Path(str(row["pcmu_path"])).resolve()
        return (
            mp3.is_file()
            and mp3.stat().st_size > 0
            and pcmu.is_file()
            and pcmu.stat().st_size > 0
        )
    except (KeyError, OSError, RuntimeError, ValueError):
        return False


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise TelephonyAdminError(f"{label} must be positive")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise TelephonyAdminError(f"{label} must be positive") from exc
    if result <= 0 or result != value:
        raise TelephonyAdminError(f"{label} must be positive")
    return result


def _runtime_matches_config(status: Any, *, enabled: bool) -> bool:
    status_enabled = bool(getattr(status, "enabled", False))
    status_ready = bool(getattr(status, "ready", False))
    if enabled:
        return status_enabled and status_ready
    return not status_enabled and not status_ready


def _runtime_status_reason(status: Any) -> str:
    reason = str(getattr(status, "reason", "status_unavailable") or "").strip()
    return reason or "status_unavailable"


def _rollback_detail(restored: bool, runtime_ready: bool) -> str:
    if restored and runtime_ready:
        return "the prior configuration and runtime were restored"
    if restored:
        return (
            "the prior durable configuration was restored; the runtime remains "
            "fail-closed"
        )
    return (
        "a concurrent durable change superseded rollback; the latest runtime "
        "state remains fail-closed"
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "AdminGreetingList",
    "AdminGreetingPhrase",
    "GlobalAudioPublication",
    "GlobalAudioPublishResult",
    "TelephonyAdminConflict",
    "TelephonyAdminError",
    "TelephonyAdminMaterializedError",
    "TelephonyAdminNotFound",
    "TelephonyAdminService",
    "TelephonyAdminUnavailable",
    "TelephonyCredentials",
    "environment_telephony_credentials",
    "register_runtime_lifecycle",
    "unregister_runtime_lifecycle",
]
