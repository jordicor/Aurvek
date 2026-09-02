"""Owner-scoped application service for native phone-channel user APIs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from database import get_db_connection
from integrations.telephony.config import TelephonyConfig, load_telephony_config
from integrations.telephony.greetings import (
    PROMPT_TECHNICAL_NOTICE_KEYS,
    load_cached_technical_notice,
    select_cached_greeting,
)
from integrations.telephony.repository import TelephonyRepository
from integrations.telephony.service import OutboundCallService
from integrations.telephony.snapshot import (
    ConversationPhoneSnapshot,
    PhoneAudioRevisionUnavailable,
    build_conversation_phone_snapshot,
)


CountryResolver = Callable[[str], tuple[str, str]]
SnapshotBuilder = Callable[..., Awaitable[ConversationPhoneSnapshot]]
ConfigLoader = Callable[..., Awaitable[TelephonyConfig]]


class PhoneUserServiceError(RuntimeError):
    """Base error for an owner-facing phone operation."""


class PhoneUserUnavailableError(PhoneUserServiceError):
    """The configured phone stack cannot safely begin a paid call."""


class PhoneCountryBlockedError(PhoneUserServiceError):
    """The E.164 destination is outside the administrator allowlist."""


class PhoneNumberValidationUnavailable(PhoneUserUnavailableError):
    """The complete numbering-plan dependency is unavailable."""


@dataclass(frozen=True, slots=True)
class PreparedPhoneCall:
    binding: dict[str, Any]
    config_snapshot: dict[str, Any]
    destination_country: str


def resolve_e164_country(value: str) -> tuple[str, str]:
    """Return canonical E.164 and ISO region using libphonenumber data."""

    try:
        import phonenumbers
    except ImportError as exc:
        raise PhoneNumberValidationUnavailable(
            "Phone number validation is unavailable"
        ) from exc
    raw = str(value or "").strip()
    try:
        parsed = phonenumbers.parse(raw, None)
    except phonenumbers.NumberParseException as exc:
        raise ValueError("Phone number must use valid E.164 format") from exc
    canonical = phonenumbers.format_number(
        parsed, phonenumbers.PhoneNumberFormat.E164
    )
    if raw != canonical or not phonenumbers.is_possible_number(
        parsed
    ) or not phonenumbers.is_valid_number(parsed):
        raise ValueError("Phone number must use valid E.164 format")
    country = phonenumbers.region_code_for_number(parsed)
    if not country or len(country) != 2:
        raise ValueError("Phone number has no supported numbering region")
    return canonical, str(country).upper()


def parse_local_schedule(
    scheduled_at: str,
    timezone_name: str,
    *,
    fold: int | None = None,
) -> datetime:
    """Resolve a local wall time while rejecting DST gaps and ambiguity."""

    zone_name = str(timezone_name or "").strip()
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone_name must be a valid IANA timezone") from exc
    try:
        parsed = datetime.fromisoformat(str(scheduled_at or "").strip())
    except ValueError as exc:
        raise ValueError("scheduled_at must be an ISO local date and time") from exc
    if parsed.tzinfo is not None or parsed.utcoffset() is not None:
        raise ValueError("scheduled_at must be local time without an offset")
    if fold not in {None, 0, 1}:
        raise ValueError("fold must be 0 or 1")

    candidates: dict[datetime, datetime] = {}
    for candidate_fold in (0, 1):
        aware = parsed.replace(tzinfo=zone, fold=candidate_fold)
        instant = aware.astimezone(UTC)
        round_trip = instant.astimezone(zone)
        if round_trip.replace(tzinfo=None) == parsed:
            candidates[instant] = aware
    if not candidates:
        raise ValueError("scheduled_at does not exist in this timezone because of DST")
    if len(candidates) > 1:
        if fold is None:
            raise ValueError("scheduled_at is ambiguous because of DST; fold is required")
        selected = parsed.replace(tzinfo=zone, fold=fold)
        if selected.astimezone(UTC) not in candidates:
            raise ValueError("fold does not identify a valid scheduled instant")
        return selected
    return next(iter(candidates.values()))


class UserPhoneService:
    """Coordinate validation, immutable snapshots and durable one-shot jobs."""

    def __init__(
        self,
        repository: TelephonyRepository,
        *,
        outbound_service: OutboundCallService | None = None,
        connection_factory: Callable[..., Any] = get_db_connection,
        config_loader: ConfigLoader = load_telephony_config,
        snapshot_builder: SnapshotBuilder = build_conversation_phone_snapshot,
        country_resolver: CountryResolver = resolve_e164_country,
        verify_audio_cache: bool = True,
    ) -> None:
        self.repository = repository
        self.outbound_service = outbound_service or OutboundCallService(repository)
        self._connection_factory = connection_factory
        self._config_loader = config_loader
        self._snapshot_builder = snapshot_builder
        self._country_resolver = country_resolver
        self._verify_audio_cache = bool(verify_audio_cache)

    def normalize_contact_number(self, e164: str) -> str:
        return self._country_resolver(e164)[0]

    async def prepare_outbound_call(
        self, *, owner_user_id: int, conversation_id: int
    ) -> PreparedPhoneCall:
        config = await self._config_loader()
        if not config.enabled:
            raise PhoneUserUnavailableError("Telephony is disabled")
        binding = await self.repository.get_active_binding(
            owner_user_id=owner_user_id,
            conversation_id=conversation_id,
        )
        if binding is None:
            raise PhoneUserServiceError("Conversation has no active phone binding")
        await self.repository.require_profile_phone(
            owner_user_id=owner_user_id,
            expected_e164=str(binding["e164"]),
        )
        if not bool(binding["allow_outbound"]):
            raise PhoneUserServiceError(
                "Calls from Aurvek to your phone are disabled for this conversation"
            )
        canonical, country = self._country_resolver(str(binding["e164"]))
        if canonical != str(binding["e164"]):
            raise PhoneUserUnavailableError("Stored phone contact is not canonical E.164")
        if country not in config.allowed_countries:
            raise PhoneCountryBlockedError(
                "Destination country is not enabled for outbound calls"
            )

        async with self._connection_factory(readonly=True) as conn:
            try:
                snapshot = await self._snapshot_builder(
                    int(conversation_id),
                    expected_owner_user_id=int(owner_user_id),
                    conn=conn,
                )
            except PhoneAudioRevisionUnavailable as exc:
                raise PhoneUserUnavailableError(
                    "Required phone audio cache is not ready"
                ) from exc
            values = snapshot.as_dict()
            if self._verify_audio_cache:
                try:
                    await select_cached_greeting(
                        conn,
                        prompt_id=snapshot.prompt_id,
                        direction="outbound",
                        revision=snapshot.audio_revision,
                        greeting_mode=str(values["outbound_greeting_mode"]),
                        voice=snapshot.canonical_voice,
                        profile=snapshot.tts_profile,
                    )
                    for notice_key in sorted(PROMPT_TECHNICAL_NOTICE_KEYS):
                        await load_cached_technical_notice(
                            conn,
                            prompt_id=snapshot.prompt_id,
                            notice_key=notice_key,
                            voice=snapshot.canonical_voice,
                            profile=snapshot.tts_profile,
                            revision=snapshot.audio_revision,
                        )
                except Exception as exc:
                    raise PhoneUserUnavailableError(
                        "Required phone audio cache is not ready"
                    ) from exc
        values["destination_country"] = country
        values["allowed_countries_at_schedule"] = list(config.allowed_countries)
        return PreparedPhoneCall(
            binding=binding,
            config_snapshot=values,
            destination_country=country,
        )

    async def create_call_job(
        self,
        *,
        owner_user_id: int,
        conversation_id: int,
        idempotency_key: str,
        scheduled_at: str | None,
        timezone_name: str | None,
        fold: int | None,
        recording_override: bool | None,
        amd_override: bool | None,
    ) -> tuple[dict[str, Any], bool]:
        prepared = await self.prepare_outbound_call(
            owner_user_id=owner_user_id,
            conversation_id=conversation_id,
        )
        zone = str(timezone_name or prepared.binding["timezone_name"]).strip()
        common = {
            "owner_user_id": int(owner_user_id),
            "conversation_id": int(conversation_id),
            "binding_id": int(prepared.binding["id"]),
            "timezone_name": zone,
            "origin": "ui",
            "idempotency_key": str(idempotency_key),
            "config_snapshot": prepared.config_snapshot,
            "recording_override": recording_override,
            "amd_override": amd_override,
        }
        if scheduled_at is None:
            return await self.outbound_service.call_now(**common)
        instant = parse_local_schedule(scheduled_at, zone, fold=fold)
        return await self.outbound_service.schedule_call(
            scheduled_at=instant,
            **common,
        )

    async def create_paid_test_call_job(
        self,
        *,
        owner_user_id: int,
        conversation_id: int,
        idempotency_key: str,
        allowed_destinations: frozenset[str],
    ) -> tuple[dict[str, Any], bool]:
        """Create an admin-paid test only for one explicitly allowed destination."""

        if not allowed_destinations:
            raise PhoneUserServiceError("Paid test destination allowlist is empty")
        prepared = await self.prepare_outbound_call(
            owner_user_id=owner_user_id,
            conversation_id=conversation_id,
        )
        destination_e164 = str(prepared.binding["e164"])
        if destination_e164 not in allowed_destinations:
            raise PhoneUserServiceError(
                "Destination is not enabled for paid admin tests"
            )
        return await self.outbound_service.call_now(
            owner_user_id=int(owner_user_id),
            conversation_id=int(conversation_id),
            binding_id=int(prepared.binding["id"]),
            timezone_name=str(prepared.binding["timezone_name"]),
            origin="ui",
            idempotency_key=str(idempotency_key),
            config_snapshot=prepared.config_snapshot,
            recording_override=None,
            amd_override=None,
            expected_destination_e164=destination_e164,
        )

    async def create_ai_initiated_call_job(
        self,
        *,
        owner_user_id: int,
        conversation_id: int,
        expected_binding_id: int,
        origin_message_id: int,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Create only an immediate assistant-origin job after fresh preflight."""

        if int(origin_message_id) <= 0:
            raise ValueError("origin_message_id must be positive")
        prepared = await self.prepare_ai_initiated_call(
            owner_user_id=owner_user_id,
            conversation_id=conversation_id,
            expected_binding_id=expected_binding_id,
        )
        return await self.outbound_service.call_now(
            owner_user_id=int(owner_user_id),
            conversation_id=int(conversation_id),
            binding_id=int(expected_binding_id),
            timezone_name=str(prepared.binding["timezone_name"]),
            origin="assistant",
            idempotency_key=str(idempotency_key),
            config_snapshot=prepared.config_snapshot,
            origin_message_id=int(origin_message_id),
            recording_override=None,
            amd_override=None,
        )

    async def prepare_ai_initiated_call(
        self,
        *,
        owner_user_id: int,
        conversation_id: int,
        expected_binding_id: int,
    ) -> PreparedPhoneCall:
        """Preflight an assistant request without creating durable work."""

        prepared = await self.prepare_outbound_call(
            owner_user_id=owner_user_id,
            conversation_id=conversation_id,
        )
        if int(prepared.binding["id"]) != int(expected_binding_id):
            raise PhoneUserServiceError("Phone binding changed before the call")
        return prepared


__all__ = [
    "PhoneCountryBlockedError",
    "PhoneNumberValidationUnavailable",
    "PhoneUserServiceError",
    "PhoneUserUnavailableError",
    "PreparedPhoneCall",
    "UserPhoneService",
    "parse_local_schedule",
    "resolve_e164_country",
]
