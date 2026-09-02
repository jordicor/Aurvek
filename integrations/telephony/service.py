"""Application service for durable outbound telephone jobs.

This layer creates database work only.  It never contacts Twilio; immediate
calls are represented by a job due at the current UTC instant and therefore
follow exactly the same dispatcher path as scheduled calls.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from integrations.telephony.repository import TelephonyRepository


Clock = Callable[[], datetime]
JobIdFactory = Callable[[], str]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduled_at must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _timezone_name(value: str) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("timezone_name is required")
    try:
        return ZoneInfo(name).key
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone_name must be a valid IANA timezone") from exc


def _new_job_id() -> str:
    return f"phone-job-{uuid4().hex}"


class OutboundCallService:
    """Create, cancel, and reschedule durable one-shot call jobs."""

    def __init__(
        self,
        repository: TelephonyRepository,
        *,
        clock: Clock = _utc_now,
        job_id_factory: JobIdFactory = _new_job_id,
    ) -> None:
        self.repository = repository
        self._clock = clock
        self._job_id_factory = job_id_factory

    async def call_now(
        self,
        *,
        owner_user_id: int,
        conversation_id: int,
        binding_id: int,
        timezone_name: str,
        origin: str,
        idempotency_key: str,
        config_snapshot: Mapping[str, Any],
        origin_message_id: int | None = None,
        recording_override: bool | None = None,
        amd_override: bool | None = None,
        expected_destination_e164: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create an immediately-due job; no direct provider call is made."""

        return await self._create_job(
            owner_user_id=owner_user_id,
            conversation_id=conversation_id,
            binding_id=binding_id,
            scheduled_at=self._clock(),
            timezone_name=timezone_name,
            origin=origin,
            idempotency_key=idempotency_key,
            config_snapshot=config_snapshot,
            origin_message_id=origin_message_id,
            recording_override=recording_override,
            amd_override=amd_override,
            expected_destination_e164=expected_destination_e164,
            future_schedule=False,
            future_cutoff_utc=None,
        )

    async def call_now_in_transaction(
        self,
        conn: Any,
        *,
        owner_user_id: int,
        conversation_id: int,
        binding_id: int,
        timezone_name: str,
        origin: str,
        idempotency_key: str,
        config_snapshot: Mapping[str, Any],
        origin_message_id: int | None = None,
        recording_override: bool | None = None,
        amd_override: bool | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create an immediately-due job inside a caller-owned transaction."""

        job_id = self._next_job_id()
        return await self.repository.create_call_job_in_transaction(
            conn,
            job_id=job_id,
            owner_user_id=owner_user_id,
            conversation_id=conversation_id,
            binding_id=binding_id,
            scheduled_at_utc=_utc_text(self._clock()),
            timezone_name=_timezone_name(timezone_name),
            origin=origin,
            idempotency_key=idempotency_key,
            config_snapshot=dict(config_snapshot),
            origin_message_id=origin_message_id,
            recording_override=recording_override,
            amd_override=amd_override,
        )

    async def schedule_call(
        self,
        *,
        owner_user_id: int,
        conversation_id: int,
        binding_id: int,
        scheduled_at: datetime,
        timezone_name: str,
        origin: str,
        idempotency_key: str,
        config_snapshot: Mapping[str, Any],
        origin_message_id: int | None = None,
        recording_override: bool | None = None,
        amd_override: bool | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create a one-shot future job, storing both UTC and its IANA zone."""

        scheduled_text = _utc_text(scheduled_at)
        now_text = _utc_text(self._clock())
        if scheduled_text <= now_text:
            raise ValueError("scheduled_at must be in the future")
        return await self._create_job(
            owner_user_id=owner_user_id,
            conversation_id=conversation_id,
            binding_id=binding_id,
            scheduled_at=scheduled_at,
            timezone_name=timezone_name,
            origin=origin,
            idempotency_key=idempotency_key,
            config_snapshot=config_snapshot,
            origin_message_id=origin_message_id,
            recording_override=recording_override,
            amd_override=amd_override,
            future_schedule=True,
            future_cutoff_utc=now_text,
        )

    async def cancel_call(
        self,
        *,
        owner_user_id: int,
        job_id: str,
    ) -> bool:
        """Cancel only if a dispatcher has not won the scheduling race."""

        return await self.repository.cancel_scheduled_job(
            owner_user_id=owner_user_id,
            job_id=job_id,
        )

    async def reschedule_call(
        self,
        *,
        owner_user_id: int,
        job_id: str,
        scheduled_at: datetime,
        timezone_name: str,
    ) -> bool:
        """Reschedule only while the job remains unclaimed."""

        scheduled_text = _utc_text(scheduled_at)
        now_text = _utc_text(self._clock())
        if scheduled_text <= now_text:
            raise ValueError("scheduled_at must be in the future")
        return await self.repository.reschedule_scheduled_job(
            owner_user_id=owner_user_id,
            job_id=job_id,
            scheduled_at_utc=scheduled_text,
            timezone_name=_timezone_name(timezone_name),
            future_cutoff_utc=now_text,
        )

    async def _create_job(
        self,
        *,
        owner_user_id: int,
        conversation_id: int,
        binding_id: int,
        scheduled_at: datetime,
        timezone_name: str,
        origin: str,
        idempotency_key: str,
        config_snapshot: Mapping[str, Any],
        origin_message_id: int | None,
        recording_override: bool | None,
        amd_override: bool | None,
        expected_destination_e164: str | None = None,
        future_schedule: bool = False,
        future_cutoff_utc: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        job_id = self._next_job_id()
        return await self.repository.create_call_job(
            job_id=job_id,
            owner_user_id=owner_user_id,
            conversation_id=conversation_id,
            binding_id=binding_id,
            scheduled_at_utc=_utc_text(scheduled_at),
            timezone_name=_timezone_name(timezone_name),
            origin=origin,
            idempotency_key=idempotency_key,
            config_snapshot=dict(config_snapshot),
            origin_message_id=origin_message_id,
            recording_override=recording_override,
            amd_override=amd_override,
            expected_destination_e164=expected_destination_e164,
            future_schedule=future_schedule,
            future_cutoff_utc=future_cutoff_utc,
        )

    def _next_job_id(self) -> str:
        job_id = str(self._job_id_factory() or "").strip()
        if not job_id:
            raise ValueError("job_id_factory returned an empty identifier")
        return job_id


__all__ = ["OutboundCallService"]
