"""Transactional persistence for the durable telephone-channel domain."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from database import get_db_connection
from integrations.telephony.schemas import (
    CALL_INCOMPATIBLE_STATUSES,
    CALL_TERMINAL_STATUSES,
    BindingSnapshot,
    PhoneCallStatus,
    PhoneJobStatus,
    call_transition_result,
    can_transition_job,
    normalize_call_status,
    normalize_job_status,
)


_E164_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
_DISPATCHER_HEARTBEAT_CONFIG_KEY = "phone_dispatcher_heartbeats"
_MAX_DURABLE_DISPATCHER_HEARTBEATS = 256
_HANGUP_ATTEMPT_LEASE_SECONDS = 60
_NUMBER_INVENTORY_SYNC_CONFIG_KEY = "telephony_numbers_last_sync_at"
_CALLBACK_CONFIRMED_DISPATCH_STATUSES = frozenset(
    {
        PhoneCallStatus.QUEUED.value,
        PhoneCallStatus.INITIATED.value,
        PhoneCallStatus.RINGING.value,
        PhoneCallStatus.IN_PROGRESS.value,
        *(
            status.value
            for status in CALL_TERMINAL_STATUSES
            if status is not PhoneCallStatus.UNRESOLVED
        ),
    }
)


class TelephonyRepositoryError(RuntimeError):
    """Base error for a rejected durable-domain operation."""


class TelephonyConflictError(TelephonyRepositoryError):
    """A domain uniqueness or active-session invariant would be violated."""


class TelephonyNotFoundError(TelephonyRepositoryError):
    """The requested owned resource does not exist."""


class TelephonyInboundUnavailableError(TelephonyRepositoryError):
    """A known inbound route exists but is not accepting calls."""


class TelephonyStateError(TelephonyRepositoryError):
    """A requested state transition is invalid."""


@dataclass(frozen=True, slots=True)
class PhoneHangupAttemptClaim:
    """Result of the cross-origin durable hangup attempt latch."""

    call_id: str
    provider_call_sid: str
    state: str
    attempt_count: int
    attempt_token: str | None
    lease_until: str | None
    reason: str
    target_status: str
    origin: str
    claimed: bool


def _json(value: dict[str, Any] | list[Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _row_dict(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _has_voice_capability(value: Any) -> bool:
    try:
        capabilities = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return False
    return isinstance(capabilities, dict) and capabilities.get("voice") is True


async def _number_inventory_marker(conn: Any) -> str | None:
    cursor = await conn.execute(
        "SELECT value FROM SYSTEM_CONFIG WHERE key=?",
        (_NUMBER_INVENTORY_SYNC_CONFIG_KEY,),
    )
    row = await cursor.fetchone()
    marker = str(row[0] or "").strip() if row is not None else ""
    return marker or None


def _validate_e164(value: str) -> str:
    normalized = str(value or "").strip()
    if not _E164_RE.fullmatch(normalized):
        raise ValueError("Phone number must use E.164 format")
    return normalized


def _utc_now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc_text(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_utc_text(value: str) -> str:
    return _parse_utc_text(value).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _validate_timezone_name(value: str) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("IANA timezone is required")
    try:
        return ZoneInfo(name).key
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Invalid IANA timezone") from exc


def _hangup_claim_from_row(
    row: dict[str, Any],
    *,
    claimed: bool,
) -> PhoneHangupAttemptClaim:
    return PhoneHangupAttemptClaim(
        call_id=str(row["call_id"]),
        provider_call_sid=str(row["provider_call_sid"]),
        state=str(row["state"]),
        attempt_count=int(row["attempt_count"]),
        attempt_token=str(row["attempt_token"]) if claimed else None,
        lease_until=str(row["lease_until"]) if claimed else None,
        reason=str(row["reason"]),
        target_status=str(row["target_status"]),
        origin=str(row["origin"]),
        claimed=claimed,
    )


def _hangup_attempt_event_payload(
    *,
    attempt_count: int,
    attempt_token: str,
    origin: str,
    reason: str,
    target_status: str,
) -> dict[str, Any]:
    """Build an audit payload without persisting the raw fencing secret."""

    token = str(attempt_token or "")
    if not token:
        raise ValueError("hangup attempt token is required")
    return {
        "attempt": int(attempt_count),
        "attempt_token_id": hashlib.sha256(token.encode("utf-8")).hexdigest()[:16],
        "origin": str(origin),
        "reason": str(reason),
        "target_status": str(target_status),
    }


async def claim_phone_hangup_attempt_in_transaction(
    conn: Any,
    *,
    call_id: str,
    provider_call_sid: str,
    origin: str,
    reason: str,
    target_status: str | PhoneCallStatus,
    retry_unresolved: bool = False,
    now_utc: str | None = None,
    lease_seconds: int = _HANGUP_ATTEMPT_LEASE_SECONDS,
) -> PhoneHangupAttemptClaim:
    """Claim the one provider hangup POST allowed across every origin.

    The caller must already hold a serialized write transaction.  Ambiguous or
    expired attempts are never retried by background magic: a caller must
    explicitly pass ``retry_unresolved=True`` for a new fenced attempt.
    """

    normalized_call_id = str(call_id or "").strip()
    normalized_sid = str(provider_call_sid or "").strip()
    lease_owner = str(origin or "").strip()[:100]
    normalized_reason = str(reason or "").strip()[:100]
    target = normalize_call_status(target_status)
    if not normalized_call_id or not normalized_sid:
        raise ValueError("call_id and provider_call_sid are required")
    if not lease_owner or not normalized_reason:
        raise ValueError("origin and reason are required")
    if target not in CALL_TERMINAL_STATUSES or target is PhoneCallStatus.UNRESOLVED:
        raise ValueError("hangup target_status must be a confirmed terminal state")
    bounded_lease = int(lease_seconds)
    if bounded_lease < 5 or bounded_lease > 300:
        raise ValueError("hangup lease_seconds must be between 5 and 300")
    now = _parse_utc_text(now_utc) if now_utc is not None else datetime.now(UTC)
    now_text = now.isoformat(timespec="seconds").replace("+00:00", "Z")

    cursor = await conn.execute(
        """
        SELECT status,termination_reason FROM PHONE_CALLS
        WHERE id=? AND provider_call_sid=? AND deleted_at IS NULL
        """,
        (normalized_call_id, normalized_sid),
    )
    call = await cursor.fetchone()
    if call is None:
        raise TelephonyNotFoundError("Phone call not found")

    cursor = await conn.execute(
        "SELECT * FROM PHONE_HANGUP_ATTEMPTS WHERE call_id=?",
        (normalized_call_id,),
    )
    existing = _row_dict(await cursor.fetchone())
    status = str(call[0])
    confirmed_terminal = (
        status in {item.value for item in CALL_TERMINAL_STATUSES}
        and status != PhoneCallStatus.UNRESOLVED.value
    )
    if confirmed_terminal:
        if existing is not None and existing["state"] != "confirmed":
            await conn.execute(
                """
                UPDATE PHONE_HANGUP_ATTEMPTS
                SET state='confirmed',attempt_token=NULL,lease_owner=NULL,
                    lease_until=NULL,last_error_code=NULL,last_error_detail=NULL,
                    confirmed_at=COALESCE(confirmed_at,?),updated_at=?
                WHERE call_id=?
                """,
                (now_text, now_text, normalized_call_id),
            )
            existing.update(
                {
                    "state": "confirmed",
                    "attempt_token": None,
                    "lease_until": None,
                }
            )
        if existing is not None:
            return _hangup_claim_from_row(existing, claimed=False)
        return PhoneHangupAttemptClaim(
            call_id=normalized_call_id,
            provider_call_sid=normalized_sid,
            state="confirmed",
            attempt_count=0,
            attempt_token=None,
            lease_until=None,
            reason=normalized_reason,
            target_status=target.value,
            origin=lease_owner,
            claimed=False,
        )
    if status not in {item.value for item in CALL_INCOMPATIBLE_STATUSES}:
        raise TelephonyStateError("Phone call cannot be hung up")

    if existing is None:
        attempt = 1
        token = secrets.token_urlsafe(32)
        lease_until = (now + timedelta(seconds=bounded_lease)).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        await conn.execute(
            """
            INSERT INTO PHONE_HANGUP_ATTEMPTS(
                call_id,provider_call_sid,state,attempt_count,attempt_token,
                lease_owner,lease_until,origin,reason,target_status,
                created_at,updated_at
            ) VALUES (?,?,'in_flight',?,?,?,?,?,?,?,?,?)
            """,
            (
                normalized_call_id,
                normalized_sid,
                attempt,
                token,
                lease_owner,
                lease_until,
                lease_owner,
                normalized_reason,
                target.value,
                now_text,
                now_text,
            ),
        )
        cursor = await conn.execute(
            "SELECT * FROM PHONE_HANGUP_ATTEMPTS WHERE call_id=?",
            (normalized_call_id,),
        )
        return _hangup_claim_from_row(dict(await cursor.fetchone()), claimed=True)

    state = str(existing["state"])
    if state in {"accepted", "confirmed"}:
        return _hangup_claim_from_row(existing, claimed=False)
    expired = False
    if state == "in_flight" and existing.get("lease_until"):
        expired = _parse_utc_text(str(existing["lease_until"])) <= now
    if state == "in_flight" and not (retry_unresolved and expired):
        return _hangup_claim_from_row(existing, claimed=False)
    if state == "unresolved" and not retry_unresolved:
        return _hangup_claim_from_row(existing, claimed=False)
    if state not in {"in_flight", "unresolved"}:
        raise TelephonyStateError("Phone hangup attempt state is invalid")
    if state == "in_flight" and not expired:
        return _hangup_claim_from_row(existing, claimed=False)

    attempt = int(existing["attempt_count"]) + 1
    token = secrets.token_urlsafe(32)
    lease_until = (now + timedelta(seconds=bounded_lease)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    await conn.execute(
        """
        UPDATE PHONE_HANGUP_ATTEMPTS
        SET state='in_flight',attempt_count=?,attempt_token=?,lease_owner=?,
            lease_until=?,origin=?,last_error_code=NULL,
            last_error_detail=NULL,updated_at=?
        WHERE call_id=? AND state IN ('in_flight','unresolved')
        """,
        (
            attempt,
            token,
            lease_owner,
            lease_until,
            lease_owner,
            now_text,
            normalized_call_id,
        ),
    )
    cursor = await conn.execute(
        "SELECT * FROM PHONE_HANGUP_ATTEMPTS WHERE call_id=?",
        (normalized_call_id,),
    )
    return _hangup_claim_from_row(dict(await cursor.fetchone()), claimed=True)


async def mark_phone_hangup_unresolved_in_transaction(
    conn: Any,
    *,
    call_id: str,
    attempt_token: str,
    error_code: str,
    error_detail: str | None = None,
) -> bool:
    """Fence an ambiguous attempt; stale results cannot change a newer claim."""

    cursor = await conn.execute(
        """
        UPDATE PHONE_HANGUP_ATTEMPTS
        SET state='unresolved',attempt_token=NULL,lease_owner=NULL,lease_until=NULL,
            last_error_code=?,last_error_detail=?,updated_at=?
        WHERE call_id=? AND state='in_flight' AND attempt_token=?
        """,
        (
            str(error_code or "provider_result_unknown")[:100],
            str(error_detail)[:500] if error_detail else None,
            _utc_now_text(),
            str(call_id),
            str(attempt_token),
        ),
    )
    return cursor.rowcount == 1


async def mark_phone_hangup_accepted_in_transaction(
    conn: Any,
    *,
    call_id: str,
    attempt_token: str,
) -> bool:
    """Record a definitive REST 2xx without terminalizing provider state."""

    cursor = await conn.execute(
        """
        UPDATE PHONE_HANGUP_ATTEMPTS
        SET state='accepted',attempt_token=NULL,lease_owner=NULL,lease_until=NULL,
            last_error_code=NULL,last_error_detail=NULL,updated_at=?
        WHERE call_id=? AND state='in_flight' AND attempt_token=?
        """,
        (_utc_now_text(), str(call_id), str(attempt_token)),
    )
    return cursor.rowcount == 1


async def confirm_phone_hangup_in_transaction(
    conn: Any,
    *,
    call_id: str,
    attempt_token: str | None = None,
) -> bool:
    """Close the common latch after an authoritative signed terminal callback."""

    params: list[Any] = [_utc_now_text(), _utc_now_text(), str(call_id)]
    token_filter = ""
    if attempt_token is not None:
        token_filter = " AND state='in_flight' AND attempt_token=?"
        params.append(str(attempt_token))
    cursor = await conn.execute(
        """
        UPDATE PHONE_HANGUP_ATTEMPTS
        SET state='confirmed',attempt_token=NULL,lease_owner=NULL,lease_until=NULL,
            last_error_code=NULL,last_error_detail=NULL,
            confirmed_at=COALESCE(confirmed_at,?),updated_at=?
        WHERE call_id=? AND state!='confirmed'
        """
        + token_filter,
        tuple(params),
    )
    return cursor.rowcount == 1


async def reconcile_phone_hangup_provider_absent_in_transaction(
    conn: Any,
    *,
    call_id: str,
    provider_call_sid: str,
    attempt_token: str,
) -> bool:
    """Finalize a fenced hangup after Twilio proves the call is absent.

    A Calls API 404/410 is stronger than an accepted mutation: there is no
    mutable provider call left from which a later terminal callback can be
    required.  The exact attempt fence therefore owns one atomic local
    reconciliation using the target and reason captured by the shared latch.
    """

    normalized_call_id = str(call_id or "").strip()
    normalized_sid = str(provider_call_sid or "").strip()
    token = str(attempt_token or "").strip()
    if not normalized_call_id or not normalized_sid or not token:
        raise ValueError("call_id, provider_call_sid and attempt_token are required")

    cursor = await conn.execute(
        """
        SELECT c.status AS call_status,c.job_id,c.conversation_id,
               h.attempt_count,h.attempt_token,h.origin,h.reason,h.target_status
        FROM PHONE_CALLS c
        JOIN PHONE_HANGUP_ATTEMPTS h ON h.call_id=c.id
        WHERE c.id=? AND c.provider_call_sid=? AND c.deleted_at IS NULL
          AND h.provider_call_sid=? AND h.state='in_flight'
          AND h.attempt_token=?
        """,
        (normalized_call_id, normalized_sid, normalized_sid, token),
    )
    row = _row_dict(await cursor.fetchone())
    if row is None:
        return False

    target = normalize_call_status(str(row["target_status"]))
    if target not in CALL_TERMINAL_STATUSES or target is PhoneCallStatus.UNRESOLVED:
        raise TelephonyStateError("Phone hangup attempt target is invalid")
    current = normalize_call_status(str(row["call_status"]))
    if current not in CALL_INCOMPATIBLE_STATUSES:
        return False

    now_text = _utc_now_text()
    cursor = await conn.execute(
        """
        UPDATE PHONE_HANGUP_ATTEMPTS
        SET state='confirmed',attempt_token=NULL,lease_owner=NULL,lease_until=NULL,
            last_error_code=NULL,last_error_detail=NULL,
            confirmed_at=COALESCE(confirmed_at,?),updated_at=?
        WHERE call_id=? AND provider_call_sid=? AND state='in_flight'
          AND attempt_token=?
        """,
        (now_text, now_text, normalized_call_id, normalized_sid, token),
    )
    if cursor.rowcount != 1:
        return False

    await conn.execute(
        """
        UPDATE PHONE_CALLS
        SET status=?,termination_reason=?,ended_at=COALESCE(ended_at,CURRENT_TIMESTAMP),
            foreground_lease_owner=NULL,foreground_lease_until=NULL,
            updated_at=CURRENT_TIMESTAMP
        WHERE id=? AND provider_call_sid=? AND deleted_at IS NULL
        """,
        (target.value, str(row["reason"]), normalized_call_id, normalized_sid),
    )
    await conn.execute(
        """
        UPDATE PHONE_CONVERSATION_FOREGROUND
        SET epoch=epoch+1,current_call_id=NULL,lease_owner=NULL,lease_until=NULL,
            updated_at=CURRENT_TIMESTAMP
        WHERE conversation_id=? AND current_call_id=?
        """,
        (int(row["conversation_id"]), normalized_call_id),
    )
    await conn.execute(
        """
        UPDATE PHONE_CALL_MESSAGE_LINKS
        SET delivery_state='released'
        WHERE call_id=? AND participant='other_channel'
          AND delivery_state='queued'
        """,
        (normalized_call_id,),
    )
    if row["job_id"] is not None:
        await conn.execute(
            """
            UPDATE PHONE_CALL_JOBS
            SET status='completed',completed_at=COALESCE(completed_at,CURRENT_TIMESTAMP),
                last_error_code=NULL,last_error_detail=NULL,
                lease_owner=NULL,lease_token=NULL,lease_until=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status IN ('dispatching','needs_attention')
            """,
            (str(row["job_id"]),),
        )

    payload = _hangup_attempt_event_payload(
        attempt_count=int(row["attempt_count"]),
        attempt_token=token,
        origin=str(row["origin"]),
        reason=str(row["reason"]),
        target_status=target.value,
    )
    payload["provider_result"] = "call_absent"
    await conn.execute(
        """
        INSERT OR IGNORE INTO PHONE_CALL_EVENTS(
            call_id,provider_call_sid,dedupe_key,event_type,
            signature_valid,payload_json
        ) VALUES (?,?,?,'hangup_rest_reconciled',1,?)
        """,
        (
            normalized_call_id,
            normalized_sid,
            f"hangup-rest-reconciled:{normalized_call_id}:{int(row['attempt_count'])}",
            _json(payload),
        ),
    )
    return True


class TelephonyRepository:
    """Small repository whose writes use SQLite as the concurrency authority."""

    def __init__(self, connection_factory: Callable[..., Any] = get_db_connection):
        self._connection_factory = connection_factory

    @property
    def connection_factory(self) -> Callable[..., Any]:
        """Expose the injected database boundary to sibling domain services."""

        return self._connection_factory

    async def list_enabled_numbers(self) -> list[dict[str, Any]]:
        """Return public routing choices without exposing provider identifiers."""

        async with self._connection_factory(readonly=True) as conn:
            inventory_marker = await _number_inventory_marker(conn)
            if inventory_marker is None:
                return []
            cursor = await conn.execute(
                """
                SELECT id,e164,friendly_name,inbound_enabled,is_outbound_default,
                       capabilities_json
                FROM TELEPHONY_NUMBERS
                WHERE enabled=1 AND synced_at=?
                ORDER BY is_outbound_default DESC,lower(COALESCE(friendly_name,e164)),id
                """,
                (inventory_marker,),
            )
            result: list[dict[str, Any]] = []
            for row in await cursor.fetchall():
                if not _has_voice_capability(row["capabilities_json"]):
                    continue
                result.append(
                    {
                        key: row[key]
                        for key in (
                            "id",
                            "e164",
                            "friendly_name",
                            "inbound_enabled",
                            "is_outbound_default",
                        )
                    }
                )
            return result

    @asynccontextmanager
    async def _write(self) -> AsyncIterator[Any]:
        async with self._connection_factory() as conn:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                yield conn
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def get_profile_phone_state(
        self, *, owner_user_id: int
    ) -> dict[str, Any]:
        """Return the live profile-phone authority for one account."""

        async with self._connection_factory(readonly=True) as conn:
            return await self._profile_phone_state(conn, int(owner_user_id))

    async def require_profile_phone(
        self,
        *,
        owner_user_id: int,
        expected_e164: str | None = None,
    ) -> dict[str, Any]:
        """Require an eligible live profile number, optionally matching a route."""

        async with self._connection_factory(readonly=True) as conn:
            return await self._require_profile_phone(
                conn,
                int(owner_user_id),
                expected_e164=expected_e164,
            )

    async def get_active_profile_binding(
        self, *, owner_user_id: int
    ) -> dict[str, Any] | None:
        """Return only the binding currently routed from the live profile number."""

        async with self._connection_factory(readonly=True) as conn:
            state = await self._profile_phone_state(conn, int(owner_user_id))
            phone = state.get("e164")
            if not phone:
                return None
            cursor = await conn.execute(
                """
                SELECT b.id,b.conversation_id
                FROM PHONE_ACTIVE_ROUTES r
                JOIN PHONE_CONVERSATION_BINDINGS b
                  ON b.id=r.binding_id AND b.active=1
                WHERE r.owner_user_id=? AND r.e164=?
                LIMIT 1
                """,
                (int(owner_user_id), str(phone)),
            )
            return _row_dict(await cursor.fetchone())

    async def create_contact(
        self,
        *,
        owner_user_id: int,
        display_name: str,
        e164: str,
        timezone_name: str,
        enforce_profile_phone: bool = False,
    ) -> dict[str, Any]:
        phone = _validate_e164(e164)
        name = str(display_name or "").strip()
        timezone = _validate_timezone_name(timezone_name)
        if not name:
            raise ValueError("Contact name and IANA timezone are required")
        if len(name) > 200 or any(ord(character) < 32 for character in name):
            raise ValueError("Contact name is invalid")
        async with self._write() as conn:
            if enforce_profile_phone:
                await self._require_profile_phone(
                    conn, int(owner_user_id), expected_e164=phone
                )
            cursor = await conn.execute(
                """
                INSERT INTO PHONE_CONTACTS
                    (owner_user_id, display_name, e164, timezone_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(owner_user_id, e164) DO UPDATE SET
                    display_name = excluded.display_name,
                    timezone_name = excluded.timezone_name,
                    active = 1,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING *
                """,
                (int(owner_user_id), name, phone, timezone),
            )
            return dict(await cursor.fetchone())

    async def list_contacts(self, *, owner_user_id: int) -> list[dict[str, Any]]:
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT c.id,c.display_name,c.e164,c.timezone_name,c.created_at,
                       c.updated_at,b.id AS binding_id,b.conversation_id
                FROM PHONE_CONTACTS c
                LEFT JOIN PHONE_CONVERSATION_BINDINGS b
                  ON b.contact_id=c.id AND b.active=1
                WHERE c.owner_user_id=? AND c.active=1
                ORDER BY lower(c.display_name),c.id
                """,
                (int(owner_user_id),),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_contact(
        self, *, owner_user_id: int, contact_id: int
    ) -> dict[str, Any]:
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT c.id,c.display_name,c.e164,c.timezone_name,c.created_at,
                       c.updated_at,b.id AS binding_id,b.conversation_id
                FROM PHONE_CONTACTS c
                LEFT JOIN PHONE_CONVERSATION_BINDINGS b
                  ON b.contact_id=c.id AND b.active=1
                WHERE c.id=? AND c.owner_user_id=? AND c.active=1
                """,
                (int(contact_id), int(owner_user_id)),
            )
            row = _row_dict(await cursor.fetchone())
            if row is None:
                raise TelephonyNotFoundError("Contact not found")
            return row

    async def update_contact(
        self,
        *,
        owner_user_id: int,
        contact_id: int,
        display_name: str,
        e164: str,
        timezone_name: str,
        enforce_profile_phone: bool = False,
    ) -> dict[str, Any]:
        phone = _validate_e164(e164)
        timezone = _validate_timezone_name(timezone_name)
        name = str(display_name or "").strip()
        if not name or len(name) > 200 or any(ord(character) < 32 for character in name):
            raise ValueError("Contact name is invalid")
        async with self._write() as conn:
            if enforce_profile_phone:
                await self._require_profile_phone(
                    conn, int(owner_user_id), expected_e164=phone
                )
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CONTACTS WHERE id=? AND owner_user_id=? AND active=1",
                (int(contact_id), int(owner_user_id)),
            )
            contact = _row_dict(await cursor.fetchone())
            if contact is None:
                raise TelephonyNotFoundError("Contact not found")
            cursor = await conn.execute(
                """
                SELECT * FROM PHONE_CONVERSATION_BINDINGS
                WHERE contact_id=? AND owner_user_id=? AND active=1
                """,
                (int(contact_id), int(owner_user_id)),
            )
            binding = _row_dict(await cursor.fetchone())
            number_changed = str(contact["e164"]) != phone
            if binding is not None:
                await self._require_bound_contact_mutable(
                    conn,
                    owner_user_id=int(owner_user_id),
                    conversation_id=int(binding["conversation_id"]),
                )
            if number_changed and binding is not None:
                if await self._binding_has_incompatible_call(conn, int(binding["id"])):
                    raise TelephonyConflictError("An active call prevents contact changes")
                route_cursor = await conn.execute(
                    "SELECT owner_user_id FROM PHONE_ACTIVE_ROUTES WHERE e164=?",
                    (phone,),
                )
                route = await route_cursor.fetchone()
                if route is not None:
                    raise TelephonyConflictError("Phone participant is already assigned")
                await conn.execute(
                    "UPDATE PHONE_ACTIVE_ROUTES SET e164=?,updated_at=CURRENT_TIMESTAMP "
                    "WHERE binding_id=?",
                    (phone, int(binding["id"])),
                )
                await self._cancel_unstarted_binding_jobs(
                    conn, int(binding["id"]), error_code="contact_changed"
                )
            try:
                cursor = await conn.execute(
                    """
                    UPDATE PHONE_CONTACTS
                    SET display_name=?,e164=?,timezone_name=?,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND owner_user_id=? AND active=1
                    RETURNING *
                    """,
                    (name, phone, timezone, int(contact_id), int(owner_user_id)),
                )
            except sqlite3.IntegrityError as exc:
                raise TelephonyConflictError("Contact phone number already exists") from exc
            return dict(await cursor.fetchone())

    async def delete_contact(self, *, owner_user_id: int, contact_id: int) -> bool:
        async with self._write() as conn:
            cursor = await conn.execute(
                "SELECT id FROM PHONE_CONTACTS WHERE id=? AND owner_user_id=? AND active=1",
                (int(contact_id), int(owner_user_id)),
            )
            if await cursor.fetchone() is None:
                raise TelephonyNotFoundError("Contact not found")
            cursor = await conn.execute(
                """
                SELECT id FROM PHONE_CONVERSATION_BINDINGS
                WHERE contact_id=? AND owner_user_id=? AND active=1
                """,
                (int(contact_id), int(owner_user_id)),
            )
            binding = await cursor.fetchone()
            if binding is not None:
                binding_id = int(binding[0])
                binding_cursor = await conn.execute(
                    "SELECT conversation_id FROM PHONE_CONVERSATION_BINDINGS "
                    "WHERE id=? AND owner_user_id=? AND active=1",
                    (binding_id, int(owner_user_id)),
                )
                binding_row = await binding_cursor.fetchone()
                if binding_row is None:
                    raise TelephonyStateError("Active phone binding is inconsistent")
                if await self._binding_has_incompatible_call(conn, binding_id):
                    raise TelephonyConflictError("An active call prevents contact deletion")
                await self._deactivate_binding(conn, binding_id)
            cursor = await conn.execute(
                """
                UPDATE PHONE_CONTACTS SET active=0,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND owner_user_id=? AND active=1
                """,
                (int(contact_id), int(owner_user_id)),
            )
            return cursor.rowcount == 1

    async def get_owned_conversation_phone_state(
        self, *, owner_user_id: int, conversation_id: int
    ) -> dict[str, Any]:
        async with self._connection_factory(readonly=True) as conn:
            conversation = await self._owned_conversation(
                conn, int(owner_user_id), int(conversation_id)
            )
            return {
                "id": int(conversation["id"]),
                "locked": bool(conversation.get("locked")),
                "is_incognito": await self._conversation_is_incognito(
                    conn, int(conversation_id)
                ),
            }

    async def assign_binding(
        self,
        *,
        owner_user_id: int,
        conversation_id: int,
        contact_id: int,
        preferred_number_id: int | None = None,
        allow_inbound: bool = True,
        allow_outbound: bool = True,
        enforce_profile_phone: bool = False,
        preserve_existing_direction_flags: bool = False,
    ) -> dict[str, Any]:
        """Assign one active E.164 route, moving same-owner bindings atomically."""
        owner_id = int(owner_user_id)
        conv_id = int(conversation_id)
        contact_id = int(contact_id)
        async with self._write() as conn:
            conversation = await self._owned_conversation(conn, owner_id, conv_id)
            if bool(conversation.get("locked")):
                raise TelephonyConflictError("Conversation is locked")
            if await self._conversation_is_incognito(conn, conv_id):
                raise TelephonyConflictError("Incognito conversations cannot be bound")

            cursor = await conn.execute(
                "SELECT * FROM PHONE_CONTACTS WHERE id = ? AND owner_user_id = ? AND active = 1",
                (contact_id, owner_id),
            )
            contact = _row_dict(await cursor.fetchone())
            if contact is None:
                raise TelephonyNotFoundError("Contact not found")
            if enforce_profile_phone:
                await self._require_profile_phone(
                    conn, owner_id, expected_e164=str(contact["e164"])
                )
            if preferred_number_id is not None:
                await self._require_enabled_number(conn, int(preferred_number_id))

            cursor = await conn.execute(
                "SELECT * FROM PHONE_ACTIVE_ROUTES WHERE e164 = ?",
                (contact["e164"],),
            )
            route = _row_dict(await cursor.fetchone())
            if route is not None and int(route["owner_user_id"]) != owner_id:
                raise TelephonyConflictError("Phone participant is already assigned")

            cursor = await conn.execute(
                "SELECT * FROM PHONE_CONVERSATION_BINDINGS WHERE conversation_id = ? AND active = 1",
                (conv_id,),
            )
            conversation_binding = _row_dict(await cursor.fetchone())
            if (
                conversation_binding is not None
                and int(conversation_binding["contact_id"]) == contact_id
            ):
                if preserve_existing_direction_flags:
                    allow_inbound = bool(conversation_binding["allow_inbound"])
                    allow_outbound = bool(conversation_binding["allow_outbound"])
                changed = (
                    conversation_binding["preferred_number_id"] != preferred_number_id
                    or bool(conversation_binding["allow_inbound"])
                    != bool(allow_inbound)
                    or bool(conversation_binding["allow_outbound"])
                    != bool(allow_outbound)
                )
                if changed and await self._binding_has_incompatible_call(
                    conn, int(conversation_binding["id"])
                ):
                    raise TelephonyConflictError(
                        "An active call prevents binding changes"
                    )
                outbound_changed = (
                    conversation_binding["preferred_number_id"]
                    != preferred_number_id
                    or bool(conversation_binding["allow_outbound"])
                    != bool(allow_outbound)
                )
                await conn.execute(
                    """
                    UPDATE PHONE_CONVERSATION_BINDINGS
                    SET preferred_number_id = ?, allow_inbound = ?, allow_outbound = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        preferred_number_id,
                        int(bool(allow_inbound)),
                        int(bool(allow_outbound)),
                        conversation_binding["id"],
                    ),
                )
                await conn.execute(
                    "UPDATE PHONE_ACTIVE_ROUTES SET updated_at = CURRENT_TIMESTAMP WHERE binding_id = ?",
                    (conversation_binding["id"],),
                )
                if outbound_changed:
                    await self._cancel_unstarted_binding_jobs(
                        conn,
                        int(conversation_binding["id"]),
                        error_code="binding_updated",
                    )
                return await self._binding_by_id(conn, int(conversation_binding["id"]))

            binding_ids_to_deactivate: set[int] = set()
            if conversation_binding is not None:
                binding_ids_to_deactivate.add(int(conversation_binding["id"]))
            if route is not None:
                binding_ids_to_deactivate.add(int(route["binding_id"]))
            for old_binding_id in sorted(binding_ids_to_deactivate):
                if await self._binding_has_incompatible_call(conn, old_binding_id):
                    raise TelephonyConflictError("An active call prevents reassignment")
                await self._deactivate_binding(conn, old_binding_id)

            try:
                cursor = await conn.execute(
                    """
                    INSERT INTO PHONE_CONVERSATION_BINDINGS (
                        owner_user_id, conversation_id, contact_id,
                        preferred_number_id, allow_inbound, allow_outbound
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    RETURNING id
                    """,
                    (
                        owner_id,
                        conv_id,
                        contact_id,
                        preferred_number_id,
                        int(bool(allow_inbound)),
                        int(bool(allow_outbound)),
                    ),
                )
                binding_id = int((await cursor.fetchone())[0])
                await conn.execute(
                    """
                    INSERT INTO PHONE_ACTIVE_ROUTES
                        (e164, owner_user_id, binding_id, contact_id, conversation_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (contact["e164"], owner_id, binding_id, contact_id, conv_id),
                )
            except sqlite3.IntegrityError as exc:
                raise TelephonyConflictError("Phone participant is already assigned") from exc
            return await self._binding_by_id(conn, binding_id)

    async def get_active_binding(
        self, *, owner_user_id: int, conversation_id: int
    ) -> dict[str, Any] | None:
        async with self._connection_factory(readonly=True) as conn:
            await self._owned_conversation(
                conn, int(owner_user_id), int(conversation_id)
            )
            cursor = await conn.execute(
                """
                SELECT b.id,b.conversation_id,b.contact_id,b.preferred_number_id,
                       b.allow_inbound,b.allow_outbound,b.created_at,b.updated_at,
                       c.display_name,c.e164,c.timezone_name,
                       n.e164 AS preferred_number_e164
                FROM PHONE_CONVERSATION_BINDINGS b
                JOIN PHONE_CONTACTS c ON c.id=b.contact_id AND c.active=1
                LEFT JOIN TELEPHONY_NUMBERS n ON n.id=b.preferred_number_id
                WHERE b.owner_user_id=? AND b.conversation_id=? AND b.active=1
                """,
                (int(owner_user_id), int(conversation_id)),
            )
            return _row_dict(await cursor.fetchone())

    async def update_binding(
        self,
        *,
        owner_user_id: int,
        conversation_id: int,
        binding_id: int,
        preferred_number_id: int | None,
        allow_inbound: bool,
        allow_outbound: bool,
        enforce_profile_phone: bool = False,
    ) -> dict[str, Any]:
        async with self._write() as conn:
            conversation = await self._owned_conversation(
                conn, int(owner_user_id), int(conversation_id)
            )
            if bool(conversation.get("locked")):
                raise TelephonyConflictError("Conversation is locked")
            if await self._conversation_is_incognito(conn, int(conversation_id)):
                raise TelephonyConflictError("Incognito conversations cannot use phone bindings")
            cursor = await conn.execute(
                """
                SELECT * FROM PHONE_CONVERSATION_BINDINGS
                WHERE id=? AND owner_user_id=? AND conversation_id=? AND active=1
                """,
                (int(binding_id), int(owner_user_id), int(conversation_id)),
            )
            binding = _row_dict(await cursor.fetchone())
            if binding is None:
                raise TelephonyNotFoundError("Active phone binding not found")
            if enforce_profile_phone:
                contact_cursor = await conn.execute(
                    "SELECT e164 FROM PHONE_CONTACTS WHERE id=? AND active=1",
                    (int(binding["contact_id"]),),
                )
                contact = await contact_cursor.fetchone()
                if contact is None:
                    raise TelephonyNotFoundError("Active phone contact not found")
                await self._require_profile_phone(
                    conn, int(owner_user_id), expected_e164=str(contact[0])
                )
            if await self._binding_has_incompatible_call(conn, int(binding_id)):
                raise TelephonyConflictError("An active call prevents binding changes")
            if preferred_number_id is not None:
                await self._require_enabled_number(conn, int(preferred_number_id))
            outbound_changed = (
                binding["preferred_number_id"] != preferred_number_id
                or bool(binding["allow_outbound"]) != bool(allow_outbound)
            )
            await conn.execute(
                """
                UPDATE PHONE_CONVERSATION_BINDINGS
                SET preferred_number_id=?,allow_inbound=?,allow_outbound=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    preferred_number_id,
                    int(bool(allow_inbound)),
                    int(bool(allow_outbound)),
                    int(binding_id),
                ),
            )
            if outbound_changed:
                await self._cancel_unstarted_binding_jobs(
                    conn, int(binding_id), error_code="binding_updated"
                )
            return await self._binding_details(conn, int(binding_id))

    async def remove_binding(
        self,
        *,
        owner_user_id: int,
        conversation_id: int,
        binding_id: int,
    ) -> bool:
        async with self._write() as conn:
            await self._owned_conversation(
                conn, int(owner_user_id), int(conversation_id)
            )
            cursor = await conn.execute(
                """
                SELECT id FROM PHONE_CONVERSATION_BINDINGS
                WHERE id=? AND owner_user_id=? AND conversation_id=? AND active=1
                """,
                (int(binding_id), int(owner_user_id), int(conversation_id)),
            )
            if await cursor.fetchone() is None:
                raise TelephonyNotFoundError("Active phone binding not found")
            if await self._binding_has_incompatible_call(conn, int(binding_id)):
                raise TelephonyConflictError("An active call prevents unassignment")
            await self._deactivate_binding(conn, int(binding_id))
            return True

    async def resolve_inbound_binding(
        self,
        *,
        caller_e164: str,
        called_e164: str,
    ) -> dict[str, Any] | None:
        caller = _validate_e164(caller_e164)
        called = _validate_e164(called_e164)
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT b.*, c.e164 AS contact_e164, c.display_name,
                       n.id AS inbound_number_id, n.e164 AS inbound_number_e164
                FROM PHONE_ACTIVE_ROUTES r
                JOIN PHONE_CONVERSATION_BINDINGS b ON b.id = r.binding_id
                JOIN PHONE_CONTACTS c ON c.id = r.contact_id
                JOIN CONVERSATIONS conv ON conv.id = b.conversation_id
                JOIN USERS owner ON owner.id=b.owner_user_id
                LEFT JOIN USER_ROLES owner_role ON owner_role.id=owner.role_id
                JOIN TELEPHONY_NUMBERS n ON n.e164 = ?
                WHERE r.e164 = ? AND b.active = 1 AND b.allow_inbound = 1
                  AND c.active = 1 AND n.enabled = 1 AND n.inbound_enabled = 1
                  AND COALESCE(owner.is_enabled,0)=1
                  AND owner.phone_number=c.e164
                  AND r.e164=c.e164
                  AND (
                    COALESCE(owner.phone_verified,0)=1
                    OR lower(COALESCE(owner_role.role_name,''))='admin'
                  )
                  AND COALESCE(conv.is_incognito, 0) = 0
                  AND COALESCE(conv.locked, 0) = 0
                """,
                (called, caller),
            )
            return _row_dict(await cursor.fetchone())

    async def create_call_job(
        self,
        *,
        job_id: str,
        owner_user_id: int,
        conversation_id: int,
        binding_id: int,
        scheduled_at_utc: str,
        timezone_name: str,
        origin: str,
        idempotency_key: str,
        config_snapshot: dict[str, Any],
        origin_message_id: int | None = None,
        recording_override: bool | None = None,
        amd_override: bool | None = None,
        expected_destination_e164: str | None = None,
        future_schedule: bool = False,
        future_cutoff_utc: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        async with self._write() as conn:
            return await self.create_call_job_in_transaction(
                conn,
                job_id=job_id,
                owner_user_id=owner_user_id,
                conversation_id=conversation_id,
                binding_id=binding_id,
                scheduled_at_utc=scheduled_at_utc,
                timezone_name=timezone_name,
                origin=origin,
                idempotency_key=idempotency_key,
                config_snapshot=config_snapshot,
                origin_message_id=origin_message_id,
                recording_override=recording_override,
                amd_override=amd_override,
                expected_destination_e164=expected_destination_e164,
                future_schedule=future_schedule,
                future_cutoff_utc=future_cutoff_utc,
            )

    async def create_call_job_in_transaction(
        self,
        conn: Any,
        *,
        job_id: str,
        owner_user_id: int,
        conversation_id: int,
        binding_id: int,
        scheduled_at_utc: str,
        timezone_name: str,
        origin: str,
        idempotency_key: str,
        config_snapshot: dict[str, Any],
        origin_message_id: int | None = None,
        recording_override: bool | None = None,
        amd_override: bool | None = None,
        expected_destination_e164: str | None = None,
        future_schedule: bool = False,
        future_cutoff_utc: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create one job on a caller-owned write transaction."""

        if origin not in {"ui", "assistant", "api"}:
            raise ValueError("Invalid phone-call origin")
        if not str(idempotency_key or "").strip():
            raise ValueError("Idempotency key is required")
        scheduled_text = _normalize_utc_text(scheduled_at_utc)
        cutoff_text = (
            _normalize_utc_text(future_cutoff_utc)
            if future_cutoff_utc is not None
            else None
        )
        if future_schedule and cutoff_text is None:
            raise ValueError("future_cutoff_utc is required for a future schedule")
        if future_schedule and scheduled_text <= str(cutoff_text):
            raise ValueError("scheduled_at must be in the future")
        timezone = _validate_timezone_name(timezone_name)
        expected_destination = (
            _validate_e164(expected_destination_e164)
            if expected_destination_e164 is not None
            else None
        )
        conversation = await self._owned_conversation(
            conn, int(owner_user_id), int(conversation_id)
        )
        if not await self._owner_is_enabled(conn, int(owner_user_id)):
            raise TelephonyConflictError("Conversation owner is disabled")
        if bool(conversation.get("locked")):
            raise TelephonyConflictError("Conversation is locked")
        if await self._conversation_is_incognito(conn, int(conversation_id)):
            raise TelephonyConflictError("Incognito conversations cannot schedule calls")
        cursor = await conn.execute(
            """
            SELECT * FROM PHONE_CALL_JOBS
            WHERE owner_user_id = ? AND origin = ? AND idempotency_key = ?
            """,
            (int(owner_user_id), origin, idempotency_key),
        )
        existing = _row_dict(await cursor.fetchone())
        if existing is not None and expected_destination is None:
            return existing, False

        binding = await self._binding_snapshot(
            conn,
            owner_user_id=int(owner_user_id),
            conversation_id=int(conversation_id),
            binding_id=int(binding_id),
        )
        await self._require_profile_phone(
            conn,
            int(owner_user_id),
            expected_e164=binding.contact_e164,
        )
        if not binding.allow_outbound:
            raise TelephonyConflictError(
                "Calls from Aurvek to your phone are disabled for this conversation"
            )
        if (
            expected_destination is not None
            and binding.contact_e164 != expected_destination
        ):
            raise TelephonyConflictError("Phone binding changed before job creation")
        if existing is not None:
            if (
                int(existing["conversation_id"]) != int(conversation_id)
                or int(existing["binding_id"]) != int(binding_id)
            ):
                raise TelephonyConflictError(
                    "Idempotency key belongs to another phone-call request"
                )
            try:
                existing_binding_snapshot = json.loads(
                    str(existing["binding_snapshot_json"] or "")
                )
            except (TypeError, ValueError):
                existing_binding_snapshot = None
            if (
                not isinstance(existing_binding_snapshot, dict)
                or existing_binding_snapshot.get("to_e164")
                != expected_destination
            ):
                raise TelephonyConflictError(
                    "Idempotent phone-call request has a different destination"
                )
            return existing, False
        if future_schedule:
            cursor = await conn.execute(
                """
                SELECT id FROM PHONE_CALL_JOBS
                WHERE owner_user_id=?
                  AND status='scheduled' AND scheduled_at_utc>?
                LIMIT 1
                """,
                (int(owner_user_id), str(cutoff_text)),
            )
            if await cursor.fetchone() is not None:
                raise TelephonyConflictError(
                    "You already have a future scheduled call"
                )
        number_id = await self._resolve_outbound_number(
            conn, binding.preferred_number_id
        )
        number = await self._require_enabled_number(conn, number_id)
        binding_snapshot = binding.as_dict()
        binding_snapshot.update(
            {
                "telephony_number_id": number_id,
                "from_e164": str(number["e164"]),
                "to_e164": binding.contact_e164,
            }
        )
        cursor = await conn.execute(
            """
            INSERT INTO PHONE_CALL_JOBS (
                id, owner_user_id, conversation_id, binding_id, contact_id,
                telephony_number_id, scheduled_at_utc, timezone_name, origin,
                origin_message_id, idempotency_key, recording_override,
                amd_override, binding_snapshot_json, config_snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING *
            """,
            (
                str(job_id),
                int(owner_user_id),
                int(conversation_id),
                int(binding_id),
                binding.contact_id,
                number_id,
                scheduled_text,
                timezone,
                origin,
                origin_message_id,
                str(idempotency_key),
                None if recording_override is None else int(recording_override),
                None if amd_override is None else int(amd_override),
                _json(binding_snapshot),
                _json(config_snapshot),
            ),
        )
        return dict(await cursor.fetchone()), True

    async def heartbeat_dispatcher(
        self,
        *,
        dispatcher_id: str,
        started_at_utc: str,
        heartbeat_at_utc: str,
        lease_until_utc: str,
    ) -> dict[str, Any]:
        """Publish one bounded durable dispatcher-presence record."""

        instance_id = str(dispatcher_id or "").strip()
        if not instance_id or len(instance_id) > 128:
            raise ValueError("dispatcher_id must contain between 1 and 128 characters")
        started = _normalize_utc_text(started_at_utc)
        heartbeat = _normalize_utc_text(heartbeat_at_utc)
        lease_until = _normalize_utc_text(lease_until_utc)
        if _parse_utc_text(started) > _parse_utc_text(heartbeat):
            raise ValueError("dispatcher started_at cannot be after its heartbeat")
        if _parse_utc_text(lease_until) <= _parse_utc_text(heartbeat):
            raise ValueError("dispatcher heartbeat lease must expire after heartbeat")

        async with self._write() as conn:
            cursor = await conn.execute(
                "SELECT value FROM SYSTEM_CONFIG WHERE key=?",
                (_DISPATCHER_HEARTBEAT_CONFIG_KEY,),
            )
            row = await cursor.fetchone()
            registry = self._decode_dispatcher_heartbeats(row[0] if row else None)
            live: dict[str, dict[str, Any]] = {}
            heartbeat_dt = _parse_utc_text(heartbeat)
            for key, record in registry.items():
                try:
                    if _parse_utc_text(record["lease_until_utc"]) >= heartbeat_dt:
                        live[key] = record
                except (KeyError, TypeError, ValueError):
                    continue

            previous = live.get(instance_id)
            previous_any = registry.get(instance_id)
            previous_epoch = (
                int(previous_any.get("epoch", 0))
                if isinstance(previous_any, dict)
                else 0
            )
            continuous = bool(
                previous and previous.get("started_at_utc") == started
            )
            epoch = (
                previous_epoch
                if continuous
                else previous_epoch + 1
            )
            live_since = (
                _normalize_utc_text(previous["live_since_utc"])
                if continuous and previous.get("live_since_utc")
                else heartbeat
            )
            durable = {
                "epoch": max(epoch, 1),
                "started_at_utc": started,
                "live_since_utc": live_since,
                "heartbeat_at_utc": heartbeat,
                "lease_until_utc": lease_until,
            }
            live[instance_id] = durable
            if len(live) > _MAX_DURABLE_DISPATCHER_HEARTBEATS:
                newest = sorted(
                    (
                        item
                        for item in live.items()
                        if item[0] != instance_id
                    ),
                    key=lambda item: str(item[1].get("lease_until_utc", "")),
                    reverse=True,
                )[: _MAX_DURABLE_DISPATCHER_HEARTBEATS - 1]
                live = dict(newest)
                live[instance_id] = durable

            await conn.execute(
                """
                INSERT INTO SYSTEM_CONFIG(key,value,description,updated_at)
                VALUES (?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, description=excluded.description,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    _DISPATCHER_HEARTBEAT_CONFIG_KEY,
                    _json(live),
                    "Bounded durable liveness registry for phone dispatchers",
                ),
            )
            return {"dispatcher_id": instance_id, **durable}

    async def list_dispatchable_job_ids(
        self,
        *,
        now_utc: str,
        limit: int = 100,
    ) -> tuple[str, ...]:
        """Return due or recoverable job IDs without claiming them.

        Claiming remains the concurrency authority.  This read is deliberately
        allowed to race so any number of dispatcher processes can scan the same
        due rows while :meth:`claim_job` selects at most one winner.
        """

        if isinstance(limit, bool) or not 1 <= int(limit) <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        now = _parse_utc_text(now_utc)
        now_text = now.isoformat(timespec="seconds").replace("+00:00", "Z")
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT id
                FROM PHONE_CALL_JOBS
                WHERE (status='scheduled' AND scheduled_at_utc <= ?)
                   OR (status='dispatching' AND
                       (lease_until IS NULL OR lease_until < ?))
                ORDER BY scheduled_at_utc, created_at, id
                LIMIT ?
                """,
                (now_text, now_text, int(limit)),
            )
            return tuple(str(row[0]) for row in await cursor.fetchall())

    async def cancel_scheduled_job(
        self,
        *,
        owner_user_id: int,
        job_id: str,
    ) -> bool:
        """Cancel only while the dispatcher has not claimed the job."""

        async with self._write() as conn:
            cursor = await conn.execute(
                """
                UPDATE PHONE_CALL_JOBS
                SET status='canceled', completed_at=CURRENT_TIMESTAMP,
                    last_error_code='canceled_by_user',
                    lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND owner_user_id=? AND status='scheduled'
                """,
                (job_id, int(owner_user_id)),
            )
            return cursor.rowcount == 1

    async def reschedule_scheduled_job(
        self,
        *,
        owner_user_id: int,
        job_id: str,
        scheduled_at_utc: str,
        timezone_name: str,
        future_cutoff_utc: str,
    ) -> bool:
        """Move a still-unclaimed job; a concurrent claim wins atomically."""

        scheduled_text = _normalize_utc_text(scheduled_at_utc)
        cutoff_text = _normalize_utc_text(future_cutoff_utc)
        if scheduled_text <= cutoff_text:
            raise ValueError("scheduled_at must be in the future")
        timezone = _validate_timezone_name(timezone_name)
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                SELECT conversation_id FROM PHONE_CALL_JOBS
                WHERE id=? AND owner_user_id=? AND status='scheduled'
                """,
                (job_id, int(owner_user_id)),
            )
            job = await cursor.fetchone()
            if job is None:
                return False
            cursor = await conn.execute(
                """
                SELECT id FROM PHONE_CALL_JOBS
                WHERE owner_user_id=? AND id<>?
                  AND status='scheduled' AND scheduled_at_utc>?
                LIMIT 1
                """,
                (int(owner_user_id), job_id, cutoff_text),
            )
            if await cursor.fetchone() is not None:
                raise TelephonyConflictError(
                    "You already have a future scheduled call"
                )
            cursor = await conn.execute(
                """
                UPDATE PHONE_CALL_JOBS
                SET scheduled_at_utc=?, timezone_name=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND owner_user_id=? AND status='scheduled'
                """,
                (
                    scheduled_text,
                    timezone,
                    job_id,
                    int(owner_user_id),
                ),
            )
            return cursor.rowcount == 1

    async def claim_job(
        self,
        *,
        job_id: str,
        lease_owner: str,
        lease_until: str,
        lease_token: str | None = None,
        now_utc: str | None = None,
        dispatcher_started_at_utc: str | None = None,
        dispatcher_id: str | None = None,
        jitter_seconds: int = 10,
        reconcile_deadline: str | None = None,
    ) -> dict[str, Any] | None:
        token = lease_token or secrets.token_urlsafe(24)
        now = _normalize_utc_text(now_utc or _utc_now_text())
        lease_until_text = _normalize_utc_text(lease_until)
        if _parse_utc_text(lease_until_text) <= _parse_utc_text(now):
            raise ValueError("lease_until must be after now_utc")
        started_at = (
            _normalize_utc_text(dispatcher_started_at_utc)
            if dispatcher_started_at_utc is not None
            else None
        )
        if isinstance(jitter_seconds, bool) or int(jitter_seconds) < 0:
            raise ValueError("jitter_seconds cannot be negative")
        unknown_deadline = _normalize_utc_text(
            reconcile_deadline
            or (
                _parse_utc_text(now) + timedelta(minutes=15)
            ).isoformat()
        )
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                SELECT j.*, c.id AS linked_call_id, c.status AS linked_call_status,
                       c.provider_request_started_at
                FROM PHONE_CALL_JOBS j
                LEFT JOIN PHONE_CALLS c ON c.job_id = j.id
                WHERE j.id = ?
                """,
                (job_id,),
            )
            job = _row_dict(await cursor.fetchone())
            if job is None:
                raise TelephonyNotFoundError("Phone-call job not found")
            if job["status"] not in {"scheduled", "dispatching"}:
                return None
            if job["status"] == "scheduled":
                scheduled = _parse_utc_text(job["scheduled_at_utc"])
                now_dt = _parse_utc_text(now)
                if scheduled > now_dt:
                    return None
                restarted_after_due = (
                    started_at is not None
                    and scheduled < _parse_utc_text(started_at)
                )
                outside_live_jitter = (now_dt - scheduled).total_seconds() > int(
                    jitter_seconds
                )
                protected_by_live_dispatcher = False
                if restarted_after_due and not outside_live_jitter:
                    protected_by_live_dispatcher = (
                        await self._has_live_pre_due_dispatcher(
                            conn,
                            scheduled_at_utc=job["scheduled_at_utc"],
                            now_utc=now,
                            excluding_dispatcher_id=dispatcher_id,
                        )
                    )
                if restarted_after_due and protected_by_live_dispatcher:
                    return None
                if restarted_after_due or outside_live_jitter:
                    await conn.execute(
                        """
                        UPDATE PHONE_CALL_JOBS
                        SET status='missed', completed_at=CURRENT_TIMESTAMP,
                            last_error_code='job_missed',
                            last_error_detail='Scheduled dispatch window elapsed',
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status='scheduled'
                        """,
                        (job_id,),
                    )
                    return None
            expired = (
                job["status"] == "dispatching"
                and (
                    job["lease_until"] is None
                    or _parse_utc_text(job["lease_until"]) < _parse_utc_text(now)
                )
            )
            if job["status"] == "dispatching" and not expired:
                return None

            if job["provider_request_started_at"] is not None:
                if (
                    job["linked_call_status"]
                    in _CALLBACK_CONFIRMED_DISPATCH_STATUSES
                ):
                    await conn.execute(
                        """
                        UPDATE PHONE_CALL_JOBS
                        SET status='completed',
                            completed_at=COALESCE(completed_at, CURRENT_TIMESTAMP),
                            lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                            last_error_code=NULL, last_error_detail=NULL,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status='dispatching'
                        """,
                        (job_id,),
                    )
                    return None
                await conn.execute(
                    """
                    UPDATE PHONE_CALL_JOBS
                    SET status='needs_attention', lease_owner=NULL, lease_token=NULL,
                        lease_until=NULL, last_error_code='dispatch_unknown',
                        last_error_detail='Lease expired after provider dispatch began',
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (job_id,),
                )
                await conn.execute(
                    """
                    UPDATE PHONE_CALLS
                    SET status='dispatch_unknown', reconcile_deadline=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status IN ('created','dispatching')
                    """,
                    (unknown_deadline, job["linked_call_id"]),
                )
                return None

            if not await self._owner_is_enabled(conn, int(job["owner_user_id"])):
                await self._cancel_unstarted_job_for_disabled_owner(conn, job)
                return None

            if await self._conversation_is_incognito(conn, int(job["conversation_id"])):
                await self._cancel_unstarted_job_for_incognito(conn, job)
                return None
            if await self._conversation_is_locked(conn, int(job["conversation_id"])):
                await self._reject_unstarted_job_for_lock(conn, job)
                return None

            cursor = await conn.execute(
                """
                UPDATE PHONE_CALL_JOBS
                SET status = 'dispatching', lease_owner = ?, lease_token = ?,
                    lease_until = ?, dispatch_started_at = COALESCE(dispatch_started_at, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND (status = 'scheduled' OR
                    (status = 'dispatching' AND (lease_until IS NULL OR lease_until < ?)))
                RETURNING *
                """,
                (lease_owner, token, lease_until_text, job_id, now),
            )
            return _row_dict(await cursor.fetchone())

    async def claim_next_due_job(
        self,
        *,
        lease_owner: str,
        lease_until: str,
        now_utc: str,
        dispatcher_started_at_utc: str,
        dispatcher_id: str | None = None,
        jitter_seconds: int = 10,
        reconcile_deadline: str | None = None,
        scan_limit: int = 100,
    ) -> dict[str, Any] | None:
        """Discover and claim one due job using :meth:`claim_job` as the fence."""

        for job_id in await self.list_dispatchable_job_ids(
            now_utc=now_utc,
            limit=scan_limit,
        ):
            claimed = await self.claim_job(
                job_id=job_id,
                lease_owner=lease_owner,
                lease_until=lease_until,
                now_utc=now_utc,
                dispatcher_started_at_utc=dispatcher_started_at_utc,
                dispatcher_id=dispatcher_id,
                jitter_seconds=jitter_seconds,
                reconcile_deadline=reconcile_deadline,
            )
            if claimed is not None:
                return claimed
        return None

    async def renew_job_lease(
        self,
        *,
        job_id: str,
        lease_owner: str,
        lease_token: str,
        lease_until: str,
        now_utc: str | None = None,
    ) -> bool:
        now = _normalize_utc_text(now_utc or _utc_now_text())
        lease_until_text = _normalize_utc_text(lease_until)
        if _parse_utc_text(lease_until_text) <= _parse_utc_text(now):
            raise ValueError("lease_until must be after now_utc")
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                UPDATE PHONE_CALL_JOBS SET lease_until=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='dispatching' AND lease_owner=? AND lease_token=?
                  AND lease_until IS NOT NULL AND lease_until >= ?
                """,
                (
                    lease_until_text,
                    job_id,
                    lease_owner,
                    lease_token,
                    now,
                ),
            )
            return cursor.rowcount == 1

    async def miss_unstarted_dispatch_if_late(
        self,
        *,
        job_id: str,
        lease_owner: str,
        lease_token: str,
        now_utc: str,
        jitter_seconds: int,
    ) -> bool:
        """Atomically close an owned dispatch that can no longer start on time."""

        now = _normalize_utc_text(now_utc)
        if isinstance(jitter_seconds, bool) or int(jitter_seconds) < 0:
            raise ValueError("jitter_seconds cannot be negative")
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                SELECT j.*, c.id AS linked_call_id, c.provider_request_started_at
                FROM PHONE_CALL_JOBS j
                LEFT JOIN PHONE_CALLS c ON c.job_id=j.id
                WHERE j.id=? AND j.status='dispatching'
                  AND j.lease_owner=? AND j.lease_token=?
                  AND j.lease_until IS NOT NULL AND j.lease_until>=?
                """,
                (job_id, lease_owner, lease_token, now),
            )
            job = _row_dict(await cursor.fetchone())
            if job is None:
                raise TelephonyStateError("Phone-call job lease is no longer owned")
            if job["provider_request_started_at"] is not None:
                return False
            deadline = _parse_utc_text(job["scheduled_at_utc"]) + timedelta(
                seconds=int(jitter_seconds)
            )
            if _parse_utc_text(now) <= deadline:
                return False
            await self._miss_unstarted_dispatch(conn, job)
            return True

    async def transition_job(
        self,
        job_id: str,
        target: str | PhoneJobStatus,
        *,
        error_code: str | None = None,
        error_detail: str | None = None,
        lease_owner: str | None = None,
        lease_token: str | None = None,
        now_utc: str | None = None,
    ) -> bool:
        target_status = normalize_job_status(target)
        now = _normalize_utc_text(now_utc or _utc_now_text())
        async with self._write() as conn:
            cursor = await conn.execute(
                "SELECT status, lease_owner, lease_token, lease_until "
                "FROM PHONE_CALL_JOBS WHERE id = ?",
                (job_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise TelephonyNotFoundError("Phone-call job not found")
            current = normalize_job_status(row[0])
            if current == PhoneJobStatus.DISPATCHING and (
                not lease_owner
                or not lease_token
                or row[1] != lease_owner
                or row[2] != lease_token
                or row[3] is None
                or _parse_utc_text(row[3]) < _parse_utc_text(now)
            ):
                raise TelephonyStateError("Phone-call job lease is no longer owned")
            if current == target_status:
                return False
            if not can_transition_job(current, target_status):
                raise TelephonyStateError(
                    f"Invalid phone job transition: {current} -> {target_status}"
                )
            completed_at = (
                "CURRENT_TIMESTAMP"
                if target_status
                in {
                    PhoneJobStatus.COMPLETED,
                    PhoneJobStatus.CANCELED,
                    PhoneJobStatus.MISSED,
                    PhoneJobStatus.CONFLICT,
                }
                else "completed_at"
            )
            await conn.execute(
                f"""
                UPDATE PHONE_CALL_JOBS
                SET status = ?, last_error_code = ?, last_error_detail = ?,
                    completed_at = {completed_at},
                    lease_owner = CASE WHEN ? = 'dispatching' THEN lease_owner ELSE NULL END,
                    lease_token = CASE WHEN ? = 'dispatching' THEN lease_token ELSE NULL END,
                    lease_until = CASE WHEN ? = 'dispatching' THEN lease_until ELSE NULL END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND (? IS NULL OR (lease_owner = ? AND lease_token = ?))
                """,
                (
                    target_status.value, error_code, error_detail,
                    target_status.value, target_status.value, target_status.value,
                    job_id,
                    lease_owner, lease_owner, lease_token,
                ),
            )
            return True

    async def capture_foreground_epoch(self, conversation_id: int) -> int:
        """Capture the durable epoch a turn must still match before it commits."""
        async with self._write() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO PHONE_CONVERSATION_FOREGROUND(conversation_id)
                VALUES (?)
                """,
                (int(conversation_id),),
            )
            cursor = await conn.execute(
                "SELECT epoch FROM PHONE_CONVERSATION_FOREGROUND WHERE conversation_id=?",
                (int(conversation_id),),
            )
            row = await cursor.fetchone()
            if row is None:
                raise TelephonyNotFoundError("Conversation not found")
            return int(row[0])

    async def capture_foreground_state(self, conversation_id: int) -> dict[str, Any]:
        """Capture both the fence and whether phone currently owns foreground."""
        async with self._write() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO PHONE_CONVERSATION_FOREGROUND(conversation_id) VALUES (?)",
                (int(conversation_id),),
            )
            cursor = await conn.execute(
                """
                SELECT epoch, current_call_id, lease_owner, lease_until
                FROM PHONE_CONVERSATION_FOREGROUND WHERE conversation_id=?
                """,
                (int(conversation_id),),
            )
            row = await cursor.fetchone()
            if row is None:
                raise TelephonyNotFoundError("Conversation not found")
            return {
                "epoch": int(row[0]),
                "phone_active": row[1] is not None,
                "call_id": row[1],
                "lease_owner": row[2],
                "lease_until": row[3],
            }

    async def assert_non_phone_foreground(
        self, *, conversation_id: int, epoch: int
    ) -> bool:
        """CAS guard for a non-phone turn before it writes its final result."""
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT 1 FROM PHONE_CONVERSATION_FOREGROUND
                WHERE conversation_id=? AND epoch=? AND current_call_id IS NULL
                """,
                (int(conversation_id), int(epoch)),
            )
            return await cursor.fetchone() is not None

    async def is_foreground_epoch_current(
        self, *, conversation_id: int, epoch: int
    ) -> bool:
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT 1 FROM PHONE_CONVERSATION_FOREGROUND
                WHERE conversation_id=? AND epoch=?
                """,
                (int(conversation_id), int(epoch)),
            )
            return await cursor.fetchone() is not None

    async def acquire_conversation_foreground(
        self,
        *,
        conversation_id: int,
        call_id: str,
        expected_epoch: int,
        lease_owner: str,
        lease_until: str,
    ) -> int | None:
        async with self._write() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO PHONE_CONVERSATION_FOREGROUND(conversation_id) VALUES (?)",
                (int(conversation_id),),
            )
            cursor = await conn.execute(
                """
                UPDATE PHONE_CONVERSATION_FOREGROUND
                SET epoch=epoch+1, current_call_id=?, lease_owner=?, lease_until=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE conversation_id=? AND epoch=? AND current_call_id IS NULL
                  AND EXISTS (
                      SELECT 1 FROM PHONE_CALLS c
                      WHERE c.id=? AND c.conversation_id=?
                  )
                RETURNING epoch
                """,
                (
                    call_id, lease_owner, lease_until,
                    int(conversation_id), int(expected_epoch), call_id,
                    int(conversation_id),
                ),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            epoch = int(row[0])
            await conn.execute(
                """
                UPDATE PHONE_CALLS SET foreground_fencing_token=?,
                    foreground_lease_owner=?, foreground_lease_until=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND conversation_id=?
                """,
                (epoch, lease_owner, lease_until, call_id, int(conversation_id)),
            )
            return epoch

    async def assert_conversation_foreground(
        self,
        *,
        conversation_id: int,
        call_id: str,
        epoch: int,
        lease_owner: str,
        now_utc: str | None = None,
    ) -> bool:
        now = now_utc or _utc_now_text()
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT 1 FROM PHONE_CONVERSATION_FOREGROUND
                WHERE conversation_id=? AND current_call_id=? AND epoch=?
                  AND lease_owner=? AND lease_until>=?
                """,
                (int(conversation_id), call_id, int(epoch), lease_owner, now),
            )
            return await cursor.fetchone() is not None

    async def release_conversation_foreground(
        self,
        *,
        conversation_id: int,
        call_id: str,
        epoch: int,
    ) -> int | None:
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                UPDATE PHONE_CONVERSATION_FOREGROUND
                SET epoch=epoch+1, current_call_id=NULL, lease_owner=NULL,
                    lease_until=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE conversation_id=? AND current_call_id=? AND epoch=?
                RETURNING epoch
                """,
                (int(conversation_id), call_id, int(epoch)),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            await conn.execute(
                """
                UPDATE PHONE_CALLS SET foreground_lease_owner=NULL,
                    foreground_lease_until=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (call_id,),
            )
            return int(row[0])

    async def link_other_channel_message(
        self,
        *,
        conversation_id: int,
        call_id: str,
        message_id: int,
        origin_channel: str,
        expected_epoch: int,
    ) -> tuple[dict[str, Any], bool]:
        """Idempotently attach a persisted non-phone message to the active call."""
        async with self._write() as conn:
            return await self.link_other_channel_message_in_transaction(
                conn,
                conversation_id=conversation_id,
                call_id=call_id,
                message_id=message_id,
                origin_channel=origin_channel,
                expected_epoch=expected_epoch,
            )

    async def link_other_channel_message_in_transaction(
        self,
        conn: Any,
        *,
        conversation_id: int,
        call_id: str,
        message_id: int,
        origin_channel: str,
        expected_epoch: int,
    ) -> tuple[dict[str, Any], bool]:
        """Link using the caller's BEGIN IMMEDIATE transaction and connection.

        This method never opens, commits, or rolls back a connection, allowing
        the MESSAGES insert and its foreground link to share one atomic commit.
        """
        if origin_channel not in {"web", "whatsapp", "telegram", "device"}:
            raise ValueError("Invalid non-phone origin channel")
        cursor = await conn.execute(
            """
            SELECT * FROM PHONE_CALL_MESSAGE_LINKS
            WHERE call_id=? AND message_id=? AND participant='other_channel'
              AND origin_channel=?
            """,
            (call_id, int(message_id), origin_channel),
        )
        existing = _row_dict(await cursor.fetchone())
        if existing is not None:
            return existing, False
        cursor = await conn.execute(
            """
            SELECT 1
            FROM PHONE_CONVERSATION_FOREGROUND f
            JOIN PHONE_CALLS c ON c.id=f.current_call_id
            JOIN MESSAGES m ON m.id=? AND m.conversation_id=f.conversation_id
            WHERE f.conversation_id=? AND f.current_call_id=? AND f.epoch=?
              AND c.conversation_id=f.conversation_id
            """,
            (int(message_id), int(conversation_id), call_id, int(expected_epoch)),
        )
        if await cursor.fetchone() is None:
            raise TelephonyStateError("Phone foreground changed before message queueing")
        cursor = await conn.execute(
            """
            INSERT INTO PHONE_CALL_MESSAGE_LINKS (
                call_id, message_id, participant, origin_channel, delivery_state
            ) VALUES (?, ?, 'other_channel', ?, 'queued')
            ON CONFLICT(call_id, message_id) DO NOTHING
            RETURNING *
            """,
            (call_id, int(message_id), origin_channel),
        )
        row = await cursor.fetchone()
        if row is not None:
            return dict(row), True
        cursor = await conn.execute(
            "SELECT * FROM PHONE_CALL_MESSAGE_LINKS WHERE call_id=? AND message_id=?",
            (call_id, int(message_id)),
        )
        existing = _row_dict(await cursor.fetchone())
        if existing is None:
            raise TelephonyStateError("Phone foreground changed before message queueing")
        raise TelephonyConflictError("Message is already linked incompatibly")

    async def list_queued_other_channel_messages(
        self,
        *,
        conversation_id: int,
        call_id: str,
        epoch: int,
        lease_owner: str,
        now_utc: str | None = None,
    ) -> list[dict[str, Any]]:
        """List queued messages in conversation order for the fenced phone owner."""
        now = now_utc or _utc_now_text()
        async with self._connection_factory(readonly=True) as conn:
            guard = await conn.execute(
                """
                SELECT 1 FROM PHONE_CONVERSATION_FOREGROUND
                WHERE conversation_id=? AND current_call_id=? AND epoch=?
                  AND lease_owner=? AND lease_until>=?
                """,
                (int(conversation_id), call_id, int(epoch), lease_owner, now),
            )
            if await guard.fetchone() is None:
                raise TelephonyStateError("Phone foreground lease is no longer owned")
            cursor = await conn.execute(
                """
                SELECT l.*, m.message, m.type
                FROM PHONE_CALL_MESSAGE_LINKS l
                JOIN MESSAGES m ON m.id=l.message_id
                WHERE l.call_id=? AND l.participant='other_channel'
                  AND l.delivery_state='queued'
                ORDER BY l.message_id ASC, l.id ASC
                """,
                (call_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def consume_queued_other_channel_message(
        self,
        *,
        conversation_id: int,
        call_id: str,
        message_id: int,
        epoch: int,
        lease_owner: str,
        turn_id: str,
        now_utc: str | None = None,
    ) -> bool:
        """Mark one queued message consumed, fenced by the current phone lease."""
        claim_turn_id = str(turn_id or "").strip()
        if not claim_turn_id:
            raise ValueError("turn_id is required to claim a queued message")
        now = now_utc or _utc_now_text()
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                SELECT delivery_state, turn_id FROM PHONE_CALL_MESSAGE_LINKS
                WHERE call_id=? AND message_id=? AND participant='other_channel'
                """,
                (call_id, int(message_id)),
            )
            durable = await cursor.fetchone()
            if durable is not None and durable[0] == "consumed":
                return durable[1] == claim_turn_id
            cursor = await conn.execute(
                """
                UPDATE PHONE_CALL_MESSAGE_LINKS
                SET delivery_state='consumed', turn_id=?
                WHERE call_id=? AND message_id=? AND participant='other_channel'
                  AND delivery_state='queued'
                  AND EXISTS (
                      SELECT 1 FROM PHONE_CONVERSATION_FOREGROUND f
                      WHERE f.conversation_id=? AND f.current_call_id=? AND f.epoch=?
                        AND f.lease_owner=? AND f.lease_until>=?
                  )
                """,
                (
                    claim_turn_id, call_id, int(message_id), int(conversation_id), call_id,
                    int(epoch), lease_owner, now,
                ),
            )
            if cursor.rowcount == 1:
                return True
            guard = await conn.execute(
                """
                SELECT 1 FROM PHONE_CONVERSATION_FOREGROUND
                WHERE conversation_id=? AND current_call_id=? AND epoch=?
                  AND lease_owner=? AND lease_until>=?
                """,
                (int(conversation_id), call_id, int(epoch), lease_owner, now),
            )
            if await guard.fetchone() is None:
                return False
            cursor = await conn.execute(
                """
                SELECT delivery_state, turn_id FROM PHONE_CALL_MESSAGE_LINKS
                WHERE call_id=? AND message_id=? AND participant='other_channel'
                """,
                (call_id, int(message_id)),
            )
            row = await cursor.fetchone()
            return bool(row and row[0] == "consumed" and row[1] == claim_turn_id)

    async def reconcile_phone_hangup(
        self,
        *,
        conversation_id: int,
        call_id: str,
        expected_epoch: int,
    ) -> dict[str, Any]:
        """Release phone foreground and any unconsumed queued messages atomically."""
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                UPDATE PHONE_CONVERSATION_FOREGROUND
                SET epoch=epoch+1, current_call_id=NULL, lease_owner=NULL,
                    lease_until=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE conversation_id=? AND current_call_id=? AND epoch=?
                RETURNING epoch
                """,
                (int(conversation_id), call_id, int(expected_epoch)),
            )
            row = await cursor.fetchone()
            released_epoch = int(row[0]) if row is not None else None
            if released_epoch is None:
                cursor = await conn.execute(
                    """
                    SELECT f.current_call_id, f.epoch, c.status
                    FROM PHONE_CONVERSATION_FOREGROUND f
                    JOIN PHONE_CALLS c ON c.id=? AND c.conversation_id=f.conversation_id
                    WHERE f.conversation_id=?
                    """,
                    (call_id, int(conversation_id)),
                )
                state = await cursor.fetchone()
                if state is None:
                    raise TelephonyNotFoundError("Phone call foreground not found")
                terminal = state[2] in {
                    status.value for status in CALL_TERMINAL_STATUSES
                }
                if state[0] == call_id or not terminal:
                    return {"released_epoch": None, "released_message_ids": []}
            cursor = await conn.execute(
                """
                SELECT id, message_id FROM PHONE_CALL_MESSAGE_LINKS
                WHERE call_id=? AND participant='other_channel'
                  AND delivery_state='queued'
                ORDER BY message_id ASC, id ASC
                """,
                (call_id,),
            )
            queued = [dict(row) for row in await cursor.fetchall()]
            if queued:
                await conn.executemany(
                    "UPDATE PHONE_CALL_MESSAGE_LINKS SET delivery_state='released' "
                    "WHERE id=? AND delivery_state='queued'",
                    [(row["id"],) for row in queued],
                )
            await conn.execute(
                """
                UPDATE PHONE_CALLS SET foreground_lease_owner=NULL,
                    foreground_lease_until=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND conversation_id=?
                """,
                (call_id, int(conversation_id)),
            )
            return {
                "released_epoch": released_epoch,
                "released_message_ids": [int(row["message_id"]) for row in queued],
            }

    async def create_call_from_job(
        self,
        *,
        job_id: str,
        call_id: str,
        dispatch_token: str,
        foreground_lease_owner: str,
        foreground_lease_until: str,
        lease_owner: str,
        lease_token: str,
        now_utc: str | None = None,
        recover_existing_foreground: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        now = _normalize_utc_text(now_utc or _utc_now_text())
        foreground_until = _normalize_utc_text(foreground_lease_until)
        if _parse_utc_text(foreground_until) <= _parse_utc_text(now):
            raise ValueError("foreground_lease_until must be after now_utc")
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                SELECT j.*, n.e164 AS number_e164, contact.e164 AS contact_e164,
                       n.enabled AS number_enabled,
                       contact.active AS contact_active,
                       binding.active AS binding_active,
                       binding.allow_outbound AS binding_allow_outbound,
                       existing.id AS linked_call_id,
                       existing.provider_request_started_at
                FROM PHONE_CALL_JOBS j
                JOIN TELEPHONY_NUMBERS n ON n.id = j.telephony_number_id
                JOIN PHONE_CONTACTS contact ON contact.id = j.contact_id
                JOIN PHONE_CONVERSATION_BINDINGS binding ON binding.id=j.binding_id
                LEFT JOIN PHONE_CALLS existing ON existing.job_id = j.id
                WHERE j.id = ? AND j.status = 'dispatching'
                  AND j.lease_owner = ? AND j.lease_token = ?
                  AND j.lease_until IS NOT NULL AND j.lease_until >= ?
                """,
                (job_id, lease_owner, lease_token, now),
            )
            job = _row_dict(await cursor.fetchone())
            if job is None:
                raise TelephonyStateError("Phone-call job lease is no longer owned")
            if not await self._owner_is_enabled(conn, int(job["owner_user_id"])):
                if job["provider_request_started_at"] is not None:
                    await conn.execute(
                        """
                        UPDATE PHONE_CALL_JOBS SET status='needs_attention',
                            last_error_code='dispatch_unknown',
                            last_error_detail='Owner disabled after provider dispatch began',
                            lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                            updated_at=CURRENT_TIMESTAMP WHERE id=?
                        """,
                        (job_id,),
                    )
                    await conn.execute(
                        """
                        UPDATE PHONE_CALLS SET status='dispatch_unknown',
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status IN ('created','dispatching')
                        """,
                        (job["linked_call_id"],),
                    )
                else:
                    await self._cancel_unstarted_job_for_disabled_owner(conn, job)
                await conn.commit()
                raise TelephonyConflictError("Conversation owner is disabled")
            try:
                await self._require_profile_phone(
                    conn,
                    int(job["owner_user_id"]),
                    expected_e164=str(job["contact_e164"]),
                )
            except TelephonyConflictError:
                if job["provider_request_started_at"] is not None:
                    await conn.execute(
                        """
                        UPDATE PHONE_CALL_JOBS SET status='needs_attention',
                            last_error_code='dispatch_unknown',
                            last_error_detail='Profile phone became ineligible after provider dispatch began',
                            lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                            updated_at=CURRENT_TIMESTAMP WHERE id=?
                        """,
                        (job_id,),
                    )
                    await conn.execute(
                        """
                        UPDATE PHONE_CALLS SET status='dispatch_unknown',
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status IN ('created','dispatching')
                        """,
                        (job["linked_call_id"],),
                    )
                else:
                    await self._reject_unstarted_job_for_profile(conn, job)
                await conn.commit()
                raise
            try:
                binding_snapshot = json.loads(job["binding_snapshot_json"])
                snapshot_number_id = int(binding_snapshot["telephony_number_id"])
                snapshot_from_e164 = _validate_e164(binding_snapshot["from_e164"])
                snapshot_to_e164 = _validate_e164(binding_snapshot["to_e164"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                snapshot_number_id = None
                snapshot_from_e164 = None
                snapshot_to_e164 = None
            binding_invalid = (
                not bool(job["binding_active"])
                or not bool(job["binding_allow_outbound"])
                or not bool(job["contact_active"])
                or not bool(job["number_enabled"])
                or snapshot_number_id != int(job["telephony_number_id"])
                or snapshot_from_e164 != str(job["number_e164"])
                or snapshot_to_e164 != str(job["contact_e164"])
            )
            if binding_invalid:
                if job["provider_request_started_at"] is not None:
                    await conn.execute(
                        """
                        UPDATE PHONE_CALL_JOBS SET status='needs_attention',
                            last_error_code='dispatch_unknown',
                            last_error_detail='Binding changed after provider dispatch began',
                            lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                            updated_at=CURRENT_TIMESTAMP WHERE id=?
                        """,
                        (job_id,),
                    )
                    await conn.execute(
                        """
                        UPDATE PHONE_CALLS SET status='dispatch_unknown',
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status IN ('created','dispatching')
                        """,
                        (job["linked_call_id"],),
                    )
                else:
                    await self._reject_unstarted_job_for_binding(conn, job)
                await conn.commit()
                raise TelephonyConflictError(
                    "Phone binding or outbound number is no longer active"
                )
            if await self._conversation_is_incognito(conn, int(job["conversation_id"])):
                if job["provider_request_started_at"] is not None:
                    await conn.execute(
                        """
                        UPDATE PHONE_CALL_JOBS SET status='needs_attention',
                            last_error_code='dispatch_unknown',
                            last_error_detail='Conversation became incognito after provider dispatch began',
                            lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                            updated_at=CURRENT_TIMESTAMP WHERE id=?
                        """,
                        (job_id,),
                    )
                    await conn.execute(
                        """
                        UPDATE PHONE_CALLS SET status='dispatch_unknown',
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status IN ('created','dispatching')
                        """,
                        (job["linked_call_id"],),
                    )
                else:
                    await self._cancel_unstarted_job_for_incognito(conn, job)
                # This rejection is itself durable state; commit it before the
                # domain exception leaves the transaction context.
                await conn.commit()
                raise TelephonyConflictError("Incognito conversations cannot create calls")
            if await self._conversation_is_locked(conn, int(job["conversation_id"])):
                if job["provider_request_started_at"] is not None:
                    await conn.execute(
                        """
                        UPDATE PHONE_CALL_JOBS SET status='needs_attention',
                            last_error_code='dispatch_unknown',
                            last_error_detail='Conversation locked after provider dispatch began',
                            lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                            updated_at=CURRENT_TIMESTAMP WHERE id=?
                        """,
                        (job_id,),
                    )
                    await conn.execute(
                        """
                        UPDATE PHONE_CALLS SET status='dispatch_unknown',
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status IN ('created','dispatching')
                        """,
                        (job["linked_call_id"],),
                    )
                else:
                    await self._reject_unstarted_job_for_lock(conn, job)
                await conn.commit()
                raise TelephonyConflictError("Conversation is locked")
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CALLS WHERE job_id = ?", (job_id,)
            )
            existing = _row_dict(await cursor.fetchone())
            if existing is not None:
                if recover_existing_foreground:
                    epoch = await self._transfer_foreground_for_call(
                        conn,
                        conversation_id=int(job["conversation_id"]),
                        call_id=str(existing["id"]),
                        lease_owner=foreground_lease_owner,
                        lease_until=foreground_until,
                    )
                    existing["foreground_fencing_token"] = epoch
                    existing["foreground_lease_owner"] = foreground_lease_owner
                    existing["foreground_lease_until"] = foreground_until
                return existing, False
            config_snapshot = json.loads(job["config_snapshot_json"] or "{}")
            recording_enabled = (
                bool(job["recording_override"])
                if job["recording_override"] is not None
                else bool(config_snapshot.get("recording_default", False))
            )
            if recording_enabled:
                from integrations.telephony.purge_state import (
                    phone_data_purge_runtime_operational,
                )

                # Retention is fail-closed.  A configured recording preference
                # cannot create audio unless a live purge worker currently owns
                # the durable runtime lease.
                recording_enabled = await phone_data_purge_runtime_operational(conn)
            amd_enabled = (
                bool(job["amd_override"])
                if job["amd_override"] is not None
                else bool(config_snapshot.get("amd_default", False))
            )
            try:
                cursor = await conn.execute(
                    """
                    INSERT INTO PHONE_CALLS (
                        id, job_id, owner_user_id, conversation_id, binding_id,
                        contact_id, telephony_number_id, direction, from_e164,
                        to_e164, dispatch_token, binding_snapshot_json,
                        config_snapshot_json, foreground_lease_owner,
                        foreground_lease_until, recording_enabled, amd_enabled
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'outbound', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    RETURNING *
                    """,
                    (
                        call_id,
                        job_id,
                        job["owner_user_id"],
                        job["conversation_id"],
                        job["binding_id"],
                        job["contact_id"],
                        job["telephony_number_id"],
                        snapshot_from_e164,
                        snapshot_to_e164,
                        dispatch_token,
                        job["binding_snapshot_json"],
                        job["config_snapshot_json"],
                        foreground_lease_owner,
                        foreground_until,
                        int(recording_enabled),
                        int(amd_enabled),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TelephonyConflictError(
                    "Conversation already has an incompatible phone call"
                ) from exc
            call = dict(await cursor.fetchone())
            epoch = await self._acquire_foreground(
                conn,
                conversation_id=int(job["conversation_id"]),
                call_id=str(call_id),
                lease_owner=foreground_lease_owner,
                lease_until=foreground_until,
            )
            await conn.execute(
                """
                UPDATE PHONE_CALLS
                SET foreground_fencing_token=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (epoch, call_id),
            )
            call["foreground_fencing_token"] = epoch
            return call, True

    async def fail_unstarted_dispatch(
        self,
        *,
        job_id: str,
        call_id: str,
        lease_owner: str,
        lease_token: str,
        error_code: str,
        error_detail: str,
        now_utc: str | None = None,
    ) -> bool:
        """Atomically close a local failure proven to precede the provider POST."""

        now = _normalize_utc_text(now_utc or _utc_now_text())
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                SELECT c.provider_request_started_at
                FROM PHONE_CALLS c
                JOIN PHONE_CALL_JOBS j ON j.id=c.job_id
                WHERE c.id=? AND j.id=? AND c.status='created'
                  AND j.status='dispatching' AND j.lease_owner=? AND j.lease_token=?
                  AND j.lease_until IS NOT NULL AND j.lease_until >= ?
                """,
                (call_id, job_id, lease_owner, lease_token, now),
            )
            row = await cursor.fetchone()
            if row is None or row[0] is not None:
                raise TelephonyStateError("Phone-call dispatch lease is no longer owned")
            await conn.execute(
                """
                UPDATE PHONE_CALLS
                SET status='failed', termination_reason='dispatch_configuration',
                    ended_at=COALESCE(ended_at, CURRENT_TIMESTAMP),
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (call_id,),
            )
            await self._release_foreground_for_call(conn, call_id)
            await conn.execute(
                """
                UPDATE PHONE_CALL_JOBS
                SET status='completed', completed_at=COALESCE(completed_at, CURRENT_TIMESTAMP),
                    last_error_code=?, last_error_detail=?,
                    lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (str(error_code), str(error_detail), job_id),
            )
            return True

    async def mark_provider_request_started(
        self,
        *,
        call_id: str,
        dispatch_token: str,
        lease_owner: str,
        lease_token: str,
        foreground_lease_owner: str,
        foreground_fencing_token: int,
        now_utc: str | None = None,
        jitter_seconds: int | None = None,
        reconcile_deadline: str | None = None,
    ) -> bool:
        """Cross the POST boundary already fenced as durable ambiguous state."""
        now = _normalize_utc_text(now_utc or _utc_now_text())
        unknown_deadline = _normalize_utc_text(
            reconcile_deadline
            or (_parse_utc_text(now) + timedelta(minutes=15)).isoformat()
        )
        if jitter_seconds is not None and (
            isinstance(jitter_seconds, bool) or int(jitter_seconds) < 0
        ):
            raise ValueError("jitter_seconds cannot be negative")
        async with self._write() as conn:
            if jitter_seconds is not None:
                cursor = await conn.execute(
                    """
                    SELECT j.*, c.id AS linked_call_id,
                           c.provider_request_started_at
                    FROM PHONE_CALLS c
                    JOIN PHONE_CALL_JOBS j ON j.id=c.job_id
                    JOIN PHONE_CONVERSATION_FOREGROUND f
                      ON f.conversation_id=c.conversation_id
                    WHERE c.id=? AND c.dispatch_token=? AND c.status='created'
                      AND c.provider_request_started_at IS NULL
                      AND c.foreground_lease_owner=?
                      AND c.foreground_fencing_token=?
                      AND c.foreground_lease_until IS NOT NULL
                      AND c.foreground_lease_until>=?
                      AND j.status='dispatching' AND j.lease_owner=?
                      AND j.lease_token=? AND j.lease_until IS NOT NULL
                      AND j.lease_until>=?
                      AND f.current_call_id=c.id AND f.lease_owner=?
                      AND f.epoch=? AND f.lease_until IS NOT NULL
                      AND f.lease_until>=?
                    """,
                    (
                        call_id,
                        dispatch_token,
                        foreground_lease_owner,
                        int(foreground_fencing_token),
                        now,
                        lease_owner,
                        lease_token,
                        now,
                        foreground_lease_owner,
                        int(foreground_fencing_token),
                        now,
                    ),
                )
                state = _row_dict(await cursor.fetchone())
                if state is not None:
                    deadline = _parse_utc_text(
                        state["scheduled_at_utc"]
                    ) + timedelta(seconds=int(jitter_seconds))
                    if _parse_utc_text(now) > deadline:
                        await self._miss_unstarted_dispatch(conn, state)
                        return False
            cursor = await conn.execute(
                """
                UPDATE PHONE_CALLS
                SET status = 'dispatch_unknown',
                    provider_request_started_at = CURRENT_TIMESTAMP,
                    reconcile_deadline = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND dispatch_token = ? AND status = 'created'
                  AND provider_request_started_at IS NULL
                  AND foreground_lease_owner=?
                  AND foreground_fencing_token=?
                  AND foreground_lease_until IS NOT NULL
                  AND foreground_lease_until>=?
                  AND EXISTS (
                      SELECT 1 FROM PHONE_CALL_JOBS j
                      WHERE j.id = PHONE_CALLS.job_id AND j.status='dispatching'
                        AND j.lease_owner=? AND j.lease_token=?
                        AND j.lease_until IS NOT NULL AND j.lease_until >= ?
                  )
                  AND EXISTS (
                      SELECT 1 FROM PHONE_CONVERSATION_FOREGROUND f
                      WHERE f.conversation_id=PHONE_CALLS.conversation_id
                        AND f.current_call_id=PHONE_CALLS.id
                        AND f.lease_owner=? AND f.epoch=?
                        AND f.lease_until IS NOT NULL AND f.lease_until>=?
                  )
                  AND EXISTS (
                      SELECT 1 FROM USERS owner
                      WHERE owner.id=PHONE_CALLS.owner_user_id
                        AND COALESCE(owner.is_enabled,0)=1
                        AND owner.phone_number=PHONE_CALLS.to_e164
                        AND (
                          COALESCE(owner.phone_verified,0)=1
                          OR EXISTS (
                            SELECT 1 FROM USER_ROLES owner_role
                            WHERE owner_role.id=owner.role_id
                              AND lower(owner_role.role_name)='admin'
                          )
                        )
                  )
                """,
                (
                    unknown_deadline,
                    call_id,
                    dispatch_token,
                    foreground_lease_owner,
                    int(foreground_fencing_token),
                    now,
                    lease_owner,
                    lease_token,
                    now,
                    foreground_lease_owner,
                    int(foreground_fencing_token),
                    now,
                ),
            )
            if cursor.rowcount != 1:
                return False
            job_cursor = await conn.execute(
                """
                UPDATE PHONE_CALL_JOBS
                SET status='needs_attention',
                    last_error_code='dispatch_unknown',
                    last_error_detail='Provider request boundary crossed; outcome pending',
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=(SELECT job_id FROM PHONE_CALLS WHERE id=?)
                  AND status='dispatching' AND lease_owner=? AND lease_token=?
                """,
                (call_id, lease_owner, lease_token),
            )
            if job_cursor.rowcount != 1:
                raise TelephonyStateError(
                    "Provider boundary could not fence its dispatch job"
                )
            await conn.execute(
                """
                INSERT OR IGNORE INTO PHONE_CALL_EVENTS(
                    call_id,dedupe_key,event_type,signature_valid,payload_json
                ) VALUES (?,?,'provider_request_started',1,?)
                """,
                (
                    call_id,
                    f"provider-request-started:{call_id}",
                    _json(
                        {
                            "state": "dispatch_unknown",
                            "reconcile_deadline": unknown_deadline,
                        }
                    ),
                ),
            )
            return True

    async def complete_provider_dispatch(
        self,
        *,
        job_id: str,
        call_id: str,
        provider_call_sid: str,
        lease_owner: str,
        lease_token: str,
        now_utc: str | None = None,
    ) -> bool:
        """Atomically persist an accepted Create Call response and close its job."""

        sid = str(provider_call_sid or "").strip()
        if not sid:
            raise ValueError("provider_call_sid is required")
        async with self._write() as conn:
            try:
                state = await self._owned_dispatch_state(
                    conn,
                    job_id=job_id,
                    call_id=call_id,
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                    now_utc=now_utc,
                )
            except TelephonyStateError:
                cursor = await conn.execute(
                    """
                    SELECT j.status,c.provider_call_sid,c.status
                    FROM PHONE_CALL_JOBS j JOIN PHONE_CALLS c ON c.job_id=j.id
                    WHERE j.id=? AND c.id=?
                    """,
                    (job_id, call_id),
                )
                reconciled = await cursor.fetchone()
                if (
                    reconciled is not None
                    and str(reconciled[0]) == PhoneJobStatus.COMPLETED.value
                    and str(reconciled[1] or "") == sid
                    and str(reconciled[2]) in _CALLBACK_CONFIRMED_DISPATCH_STATUSES
                ):
                    return False
                raise
            existing_sid = state.get("provider_call_sid")
            if existing_sid is not None and str(existing_sid) != sid:
                raise TelephonyConflictError("Phone call already has another provider SID")
            await conn.execute(
                """
                UPDATE PHONE_CALLS
                SET status = CASE
                        WHEN status IN ('created','dispatching','dispatch_unknown')
                        THEN 'queued' ELSE status END,
                    provider_call_sid=COALESCE(provider_call_sid, ?),
                    reconcile_deadline=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (sid, call_id),
            )
            await conn.execute(
                """
                UPDATE PHONE_CALL_JOBS
                SET status='completed', completed_at=COALESCE(completed_at, CURRENT_TIMESTAMP),
                    last_error_code=NULL, last_error_detail=NULL,
                    lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (job_id,),
            )
            return True

    async def fail_provider_dispatch(
        self,
        *,
        job_id: str,
        call_id: str,
        lease_owner: str,
        lease_token: str,
        error_code: str,
        error_detail: str,
        now_utc: str | None = None,
    ) -> bool:
        """Atomically close a definitively rejected provider request."""

        async with self._write() as conn:
            try:
                await self._owned_dispatch_state(
                    conn,
                    job_id=job_id,
                    call_id=call_id,
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                    now_utc=now_utc,
                )
            except TelephonyStateError:
                cursor = await conn.execute(
                    """
                    SELECT j.status,c.status,c.provider_call_sid
                    FROM PHONE_CALL_JOBS j JOIN PHONE_CALLS c ON c.job_id=j.id
                    WHERE j.id=? AND c.id=?
                    """,
                    (job_id, call_id),
                )
                reconciled = await cursor.fetchone()
                if (
                    reconciled is not None
                    and str(reconciled[0]) == PhoneJobStatus.COMPLETED.value
                    and str(reconciled[1]) in _CALLBACK_CONFIRMED_DISPATCH_STATUSES
                    and reconciled[2] is not None
                ):
                    return False
                raise
            await conn.execute(
                """
                UPDATE PHONE_CALLS
                SET status='failed', termination_reason='provider_rejected',
                    reconcile_deadline=NULL,
                    ended_at=COALESCE(ended_at, CURRENT_TIMESTAMP),
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status IN ('created','dispatching','dispatch_unknown')
                """,
                (call_id,),
            )
            await self._release_foreground_for_call(conn, call_id)
            await conn.execute(
                """
                UPDATE PHONE_CALL_JOBS
                SET status='completed', completed_at=COALESCE(completed_at, CURRENT_TIMESTAMP),
                    last_error_code=?, last_error_detail=?,
                    lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (str(error_code), str(error_detail), job_id),
            )
            return True

    async def mark_provider_dispatch_unknown(
        self,
        *,
        job_id: str,
        call_id: str,
        lease_owner: str,
        lease_token: str,
        error_code: str,
        error_detail: str,
        reconcile_deadline: str,
        now_utc: str | None = None,
    ) -> bool:
        """Persist an ambiguous POST without releasing foreground or retrying."""

        deadline = _normalize_utc_text(reconcile_deadline)
        async with self._write() as conn:
            try:
                state = await self._owned_dispatch_state(
                    conn,
                    job_id=job_id,
                    call_id=call_id,
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                    now_utc=now_utc,
                )
            except TelephonyStateError:
                cursor = await conn.execute(
                    """
                    SELECT j.status,c.status,c.provider_call_sid
                    FROM PHONE_CALL_JOBS j JOIN PHONE_CALLS c ON c.job_id=j.id
                    WHERE j.id=? AND c.id=?
                    """,
                    (job_id, call_id),
                )
                reconciled = await cursor.fetchone()
                if (
                    reconciled is not None
                    and str(reconciled[0]) == PhoneJobStatus.COMPLETED.value
                    and str(reconciled[1]) in _CALLBACK_CONFIRMED_DISPATCH_STATUSES
                    and reconciled[2] is not None
                ):
                    return False
                raise
            call_status = str(state["call_status"])
            callback_already_proved_dispatch = call_status not in {
                PhoneCallStatus.CREATED.value,
                PhoneCallStatus.DISPATCHING.value,
                PhoneCallStatus.DISPATCH_UNKNOWN.value,
            }
            if callback_already_proved_dispatch:
                job_status = PhoneJobStatus.COMPLETED.value
                job_error_code = None
                job_error_detail = None
            else:
                await conn.execute(
                    """
                    UPDATE PHONE_CALLS
                    SET status='dispatch_unknown', reconcile_deadline=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status IN ('created','dispatching','dispatch_unknown')
                    """,
                    (deadline, call_id),
                )
                job_status = PhoneJobStatus.NEEDS_ATTENTION.value
                job_error_code = str(error_code)
                job_error_detail = str(error_detail)
            await conn.execute(
                """
                UPDATE PHONE_CALL_JOBS
                SET status=?, completed_at=CASE WHEN ?='completed'
                        THEN COALESCE(completed_at, CURRENT_TIMESTAMP) ELSE completed_at END,
                    last_error_code=?, last_error_detail=?,
                    lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    job_status,
                    job_status,
                    job_error_code,
                    job_error_detail,
                    job_id,
                ),
            )
            return not callback_already_proved_dispatch

    async def transition_call(
        self,
        call_id: str,
        target: str | PhoneCallStatus,
        *,
        provider_call_sid: str | None = None,
        provider_session_id: str | None = None,
        provider_stream_sid: str | None = None,
        answered_by: str | None = None,
        termination_reason: str | None = None,
    ) -> bool:
        target_status = normalize_call_status(target)
        async with self._write() as conn:
            cursor = await conn.execute(
                "SELECT status, provider_call_sid, provider_session_id, "
                "provider_stream_sid, job_id FROM PHONE_CALLS WHERE id = ?",
                (call_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise TelephonyNotFoundError("Phone call not found")
            incoming_provider_ids = (
                provider_call_sid,
                provider_session_id,
                provider_stream_sid,
            )
            for existing, incoming in zip(row[1:4], incoming_provider_ids):
                if incoming is not None and not str(incoming).strip():
                    raise ValueError("Provider identifiers cannot be empty")
                if existing is not None and incoming is not None and existing != incoming:
                    raise TelephonyConflictError(
                        "Phone call provider identifiers are write-once"
                    )
            decision = call_transition_result(row[0], target_status)
            metadata_changed = any(
                existing is None and incoming is not None
                for existing, incoming in zip(row[1:4], incoming_provider_ids)
            )
            if decision == "noop" and not metadata_changed:
                return False
            if decision == "invalid":
                raise TelephonyStateError(
                    f"Invalid phone call transition: {row[0]} -> {target_status.value}"
                )
            ended = target_status in CALL_TERMINAL_STATUSES
            applied_status = row[0] if decision == "noop" else target_status.value
            try:
                await conn.execute(
                    """
                    UPDATE PHONE_CALLS
                    SET status = ?, provider_call_sid = COALESCE(provider_call_sid, ?),
                        provider_session_id = COALESCE(provider_session_id, ?),
                        provider_stream_sid = COALESCE(provider_stream_sid, ?),
                        answered_by = COALESCE(answered_by, ?),
                        termination_reason = COALESCE(termination_reason, ?),
                        initiated_at = CASE WHEN ?='initiated'
                            THEN COALESCE(initiated_at, CURRENT_TIMESTAMP) ELSE initiated_at END,
                        ringing_at = CASE WHEN ?='ringing'
                            THEN COALESCE(ringing_at, CURRENT_TIMESTAMP) ELSE ringing_at END,
                        answered_at = CASE WHEN ?='in_progress'
                            THEN COALESCE(answered_at, CURRENT_TIMESTAMP) ELSE answered_at END,
                        ended_at = CASE WHEN ?
                            THEN COALESCE(ended_at, CURRENT_TIMESTAMP) ELSE ended_at END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        applied_status,
                        provider_call_sid,
                        provider_session_id,
                        provider_stream_sid,
                        answered_by,
                        termination_reason,
                        target_status.value,
                        target_status.value,
                        target_status.value,
                        int(ended),
                        call_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TelephonyConflictError(
                    "Provider identifier is already assigned to another phone call"
                ) from exc
            if ended:
                await self._release_foreground_for_call(conn, call_id)
            if (
                str(row[0]) == PhoneCallStatus.DISPATCH_UNKNOWN.value
                and provider_call_sid is not None
                and row[4] is not None
            ):
                await conn.execute(
                    """
                    UPDATE PHONE_CALL_JOBS
                    SET status='completed',
                        completed_at=COALESCE(completed_at,CURRENT_TIMESTAMP),
                        last_error_code=NULL,last_error_detail=NULL,
                        lease_owner=NULL,lease_token=NULL,lease_until=NULL,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status='needs_attention'
                    """,
                    (row[4],),
                )
            return True

    async def list_owned_jobs(
        self,
        *,
        owner_user_id: int,
        conversation_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        bounded_limit = min(max(int(limit), 1), 200)
        params: list[Any] = [int(owner_user_id)]
        condition = "j.owner_user_id=?"
        if conversation_id is not None:
            condition += " AND j.conversation_id=?"
            params.append(int(conversation_id))
        params.append(bounded_limit)
        async with self._connection_factory(readonly=True) as conn:
            if conversation_id is not None:
                await self._owned_conversation(
                    conn, int(owner_user_id), int(conversation_id)
                )
            cursor = await conn.execute(
                f"""
                SELECT j.id,j.conversation_id,j.binding_id,j.scheduled_at_utc,
                       j.timezone_name,j.origin,j.status,j.recording_override,
                       j.amd_override,j.last_error_code,j.last_error_detail,
                       j.created_at,j.updated_at,j.completed_at,
                       c.id AS call_id,
                       COALESCE(NULLIF(trim(conv.chat_name),''),p.name,'Conversation')
                         AS conversation_title,
                       p.name AS prompt_name
                FROM PHONE_CALL_JOBS j
                LEFT JOIN PHONE_CALLS c ON c.job_id=j.id AND c.deleted_at IS NULL
                JOIN CONVERSATIONS conv ON conv.id=j.conversation_id
                LEFT JOIN PROMPTS p ON p.id=conv.role_id
                WHERE {condition}
                ORDER BY j.scheduled_at_utc DESC,j.created_at DESC,j.id DESC
                LIMIT ?
                """,
                tuple(params),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_owned_job(
        self, *, owner_user_id: int, job_id: str
    ) -> dict[str, Any]:
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT j.id,j.conversation_id,j.binding_id,j.scheduled_at_utc,
                       j.timezone_name,j.origin,j.status,j.recording_override,
                       j.amd_override,j.last_error_code,j.last_error_detail,
                       j.created_at,j.updated_at,j.completed_at,
                       c.id AS call_id
                FROM PHONE_CALL_JOBS j
                LEFT JOIN PHONE_CALLS c ON c.job_id=j.id AND c.deleted_at IS NULL
                WHERE j.id=? AND j.owner_user_id=?
                """,
                (str(job_id), int(owner_user_id)),
            )
            row = _row_dict(await cursor.fetchone())
            if row is None:
                raise TelephonyNotFoundError("Phone-call job not found")
            return row

    async def list_owned_calls(
        self,
        *,
        owner_user_id: int,
        conversation_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        bounded_limit = min(max(int(limit), 1), 200)
        params: list[Any] = [int(owner_user_id)]
        condition = "c.owner_user_id=? AND c.deleted_at IS NULL"
        if conversation_id is not None:
            condition += " AND c.conversation_id=?"
            params.append(int(conversation_id))
        params.append(bounded_limit)
        async with self._connection_factory(readonly=True) as conn:
            if conversation_id is not None:
                await self._owned_conversation(
                    conn, int(owner_user_id), int(conversation_id)
                )
            cursor = await conn.execute(
                f"""
                SELECT c.id,c.job_id,c.conversation_id,c.direction,c.from_e164,
                       c.to_e164,c.status,c.answered_by,c.initiated_at,c.ringing_at,
                       c.answered_at,c.ended_at,c.duration_seconds,c.termination_reason,
                       c.estimated_cost,c.final_cost,c.currency,c.recording_enabled,
                       c.amd_enabled,c.created_at,c.updated_at,
                       COALESCE(NULLIF(trim(conv.chat_name),''),p.name,'Conversation')
                         AS conversation_title,
                       p.name AS prompt_name
                FROM PHONE_CALLS c
                JOIN CONVERSATIONS conv ON conv.id=c.conversation_id
                LEFT JOIN PROMPTS p ON p.id=conv.role_id
                WHERE {condition}
                ORDER BY c.created_at DESC,c.id DESC
                LIMIT ?
                """,
                tuple(params),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_owned_call(
        self, *, owner_user_id: int, call_id: str
    ) -> dict[str, Any]:
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT c.id,c.job_id,c.conversation_id,c.direction,c.from_e164,
                       c.to_e164,c.status,c.provider_call_sid,c.answered_by,
                       c.initiated_at,c.ringing_at,c.answered_at,c.ended_at,
                       c.duration_seconds,c.termination_reason,c.estimated_cost,
                       c.final_cost,c.currency,c.recording_enabled,c.amd_enabled,
                       c.created_at,c.updated_at
                FROM PHONE_CALLS c
                WHERE c.id=? AND c.owner_user_id=? AND c.deleted_at IS NULL
                """,
                (str(call_id), int(owner_user_id)),
            )
            row = _row_dict(await cursor.fetchone())
            if row is None:
                raise TelephonyNotFoundError("Phone call not found")
            return row

    async def claim_owned_hangup_request(
        self,
        *,
        owner_user_id: int,
        call_id: str,
        retry_unresolved: bool = False,
    ) -> tuple[dict[str, Any], PhoneHangupAttemptClaim]:
        """Owner-scoped adapter for the cross-origin hangup latch."""

        async with self._write() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM PHONE_CALLS
                WHERE id=? AND owner_user_id=? AND deleted_at IS NULL
                """,
                (str(call_id), int(owner_user_id)),
            )
            call = _row_dict(await cursor.fetchone())
            if call is None:
                raise TelephonyNotFoundError("Phone call not found")
            if not call.get("provider_call_sid"):
                raise TelephonyStateError("Phone call has no confirmed provider identity")
            claim = await claim_phone_hangup_attempt_in_transaction(
                conn,
                call_id=str(call_id),
                provider_call_sid=str(call["provider_call_sid"]),
                origin=f"user:{int(owner_user_id)}",
                reason="user_request",
                target_status=PhoneCallStatus.CANCELED,
                retry_unresolved=retry_unresolved,
            )
            if claim.claimed:
                assert claim.attempt_token is not None
                payload = _hangup_attempt_event_payload(
                    attempt_count=claim.attempt_count,
                    attempt_token=claim.attempt_token,
                    origin=claim.origin,
                    reason=claim.reason,
                    target_status=claim.target_status,
                )
                await conn.execute(
                    """
                    INSERT OR IGNORE INTO PHONE_CALL_EVENTS(
                        call_id,provider_call_sid,dedupe_key,event_type,
                        signature_valid,payload_json
                    ) VALUES (?,?,?,'user_hangup_requested',1,?)
                    """,
                    (
                        str(call_id),
                        call["provider_call_sid"],
                        f"user-hangup:{call_id}:{claim.attempt_count}",
                        _json(payload),
                    ),
                )
            return call, claim

    async def mark_owned_hangup_unresolved(
        self,
        *,
        owner_user_id: int,
        call_id: str,
        attempt_token: str,
    ) -> bool:
        """Persist an ambiguous owner attempt without opening a background retry."""

        async with self._write() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM PHONE_CALLS
                WHERE id=? AND owner_user_id=? AND deleted_at IS NULL
                """,
                (str(call_id), int(owner_user_id)),
            )
            call = _row_dict(await cursor.fetchone())
            if call is None:
                raise TelephonyNotFoundError("Phone call not found")
            status = str(call["status"])
            confirmed_terminal = (
                status in {item.value for item in CALL_TERMINAL_STATUSES}
                and status != PhoneCallStatus.UNRESOLVED.value
            )
            if confirmed_terminal:
                await confirm_phone_hangup_in_transaction(
                    conn,
                    call_id=str(call_id),
                )
                return False
            cursor = await conn.execute(
                """
                SELECT attempt_count,attempt_token,origin,reason,target_status
                FROM PHONE_HANGUP_ATTEMPTS
                WHERE call_id=? AND provider_call_sid=? AND state='in_flight'
                  AND attempt_token=?
                """,
                (
                    str(call_id),
                    str(call.get("provider_call_sid") or ""),
                    str(attempt_token),
                ),
            )
            attempt = _row_dict(await cursor.fetchone())
            if attempt is None:
                return False
            unresolved = await mark_phone_hangup_unresolved_in_transaction(
                conn,
                call_id=str(call_id),
                attempt_token=str(attempt_token),
                error_code="provider_result_unknown",
                error_detail="Provider hangup could not be confirmed",
            )
            if not unresolved:
                return False

            await conn.execute(
                """
                UPDATE PHONE_CALLS
                SET status='unresolved',
                    termination_reason=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND owner_user_id=?
                """,
                (
                    f"hangup_unresolved:{str(attempt['reason'])[:100]}",
                    str(call_id),
                    int(owner_user_id),
                ),
            )
            if call.get("job_id") is not None:
                await conn.execute(
                    """
                    UPDATE PHONE_CALL_JOBS
                    SET status='needs_attention',
                        last_error_code='hangup_unresolved',
                        last_error_detail='Provider hangup could not be confirmed',
                        lease_owner=NULL,lease_token=NULL,lease_until=NULL,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (str(call["job_id"]),),
                )
            payload = _hangup_attempt_event_payload(
                attempt_count=int(attempt["attempt_count"]),
                attempt_token=str(attempt["attempt_token"]),
                origin=str(attempt["origin"]),
                reason=str(attempt["reason"]),
                target_status=str(attempt["target_status"]),
            )
            await conn.execute(
                """
                INSERT OR IGNORE INTO PHONE_CALL_EVENTS(
                    call_id,provider_call_sid,dedupe_key,event_type,
                    signature_valid,payload_json
                ) VALUES (?,?,?,'user_hangup_unresolved',1,?)
                """,
                (
                    str(call_id),
                    call["provider_call_sid"],
                    f"user-hangup-unresolved:{call_id}:{int(attempt['attempt_count'])}",
                    _json(payload),
                ),
            )
            return True

    async def mark_owned_hangup_accepted(
        self,
        *,
        owner_user_id: int,
        call_id: str,
        attempt_token: str,
    ) -> bool:
        """Record Twilio REST acceptance without changing call/foreground state."""

        async with self._write() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM PHONE_CALLS
                WHERE id=? AND owner_user_id=? AND deleted_at IS NULL
                """,
                (str(call_id), int(owner_user_id)),
            )
            call = _row_dict(await cursor.fetchone())
            if call is None:
                raise TelephonyNotFoundError("Phone call not found")
            cursor = await conn.execute(
                """
                SELECT attempt_count,attempt_token,origin,reason,target_status
                FROM PHONE_HANGUP_ATTEMPTS
                WHERE call_id=? AND provider_call_sid=? AND state='in_flight'
                  AND attempt_token=?
                """,
                (
                    str(call_id),
                    str(call.get("provider_call_sid") or ""),
                    str(attempt_token),
                ),
            )
            attempt = _row_dict(await cursor.fetchone())
            if attempt is None:
                return False
            accepted = await mark_phone_hangup_accepted_in_transaction(
                conn,
                call_id=str(call_id),
                attempt_token=str(attempt_token),
            )
            if not accepted:
                return False
            payload = _hangup_attempt_event_payload(
                attempt_count=int(attempt["attempt_count"]),
                attempt_token=str(attempt["attempt_token"]),
                origin=str(attempt["origin"]),
                reason=str(attempt["reason"]),
                target_status=str(attempt["target_status"]),
            )
            await conn.execute(
                """
                INSERT OR IGNORE INTO PHONE_CALL_EVENTS(
                    call_id,provider_call_sid,dedupe_key,event_type,
                    signature_valid,payload_json
                ) VALUES (?,?,?,'user_hangup_provider_accepted',1,?)
                """,
                (
                    str(call_id),
                    call["provider_call_sid"],
                    f"user-hangup-provider-accepted:{call_id}:{int(attempt['attempt_count'])}",
                    _json(payload),
                ),
            )
            return True

    async def reconcile_owned_hangup_provider_absent(
        self,
        *,
        owner_user_id: int,
        call_id: str,
        attempt_token: str,
    ) -> bool:
        """Finalize an owner attempt after a definitive Twilio 404/410."""

        async with self._write() as conn:
            cursor = await conn.execute(
                """
                SELECT provider_call_sid FROM PHONE_CALLS
                WHERE id=? AND owner_user_id=? AND deleted_at IS NULL
                """,
                (str(call_id), int(owner_user_id)),
            )
            row = await cursor.fetchone()
            if row is None:
                raise TelephonyNotFoundError("Phone call not found")
            if not row[0]:
                raise TelephonyStateError(
                    "Phone call has no confirmed provider identity"
                )
            return await reconcile_phone_hangup_provider_absent_in_transaction(
                conn,
                call_id=str(call_id),
                provider_call_sid=str(row[0]),
                attempt_token=str(attempt_token),
            )

    async def get_owned_hangup_attempt_state(
        self,
        *,
        owner_user_id: int,
        call_id: str,
    ) -> str | None:
        """Read the shared latch state without exposing its fencing token."""

        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT h.state
                FROM PHONE_CALLS c
                LEFT JOIN PHONE_HANGUP_ATTEMPTS h ON h.call_id=c.id
                WHERE c.id=? AND c.owner_user_id=? AND c.deleted_at IS NULL
                """,
                (str(call_id), int(owner_user_id)),
            )
            row = await cursor.fetchone()
            if row is None:
                raise TelephonyNotFoundError("Phone call not found")
            return str(row[0]) if row[0] is not None else None

    async def get_call_by_dispatch_token(
        self,
        dispatch_token: str,
    ) -> dict[str, Any] | None:
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CALLS WHERE dispatch_token=? AND deleted_at IS NULL",
                (str(dispatch_token),),
            )
            return _row_dict(await cursor.fetchone())

    async def get_call_by_provider_sid(
        self,
        provider_call_sid: str,
    ) -> dict[str, Any] | None:
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CALLS WHERE provider_call_sid=? AND deleted_at IS NULL",
                (str(provider_call_sid),),
            )
            return _row_dict(await cursor.fetchone())

    async def reconcile_dispatch_unknown(
        self,
        *,
        call_id: str,
        provider_call_sid: str,
        target: str | PhoneCallStatus,
    ) -> bool:
        """Atomically accept callback evidence for an ambiguous Create Call."""

        target_status = normalize_call_status(target)
        sid = str(provider_call_sid or "").strip()
        if not sid:
            raise ValueError("provider_call_sid is required")
        async with self._write() as conn:
            cursor = await conn.execute(
                "SELECT status, provider_call_sid, job_id FROM PHONE_CALLS WHERE id=?",
                (call_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise TelephonyNotFoundError("Phone call not found")
            if row[1] is not None and str(row[1]) != sid:
                raise TelephonyConflictError("Phone call already has another provider SID")
            decision = call_transition_result(row[0], target_status)
            if decision == "noop":
                return False
            if decision == "invalid":
                raise TelephonyStateError(
                    f"Invalid ambiguous dispatch transition: {row[0]} -> {target_status.value}"
                )
            ended = target_status in CALL_TERMINAL_STATUSES
            try:
                await conn.execute(
                    """
                    UPDATE PHONE_CALLS
                    SET status=?, provider_call_sid=COALESCE(provider_call_sid, ?),
                        reconcile_deadline=NULL,
                        initiated_at=CASE WHEN ?='initiated'
                            THEN COALESCE(initiated_at, CURRENT_TIMESTAMP) ELSE initiated_at END,
                        ringing_at=CASE WHEN ?='ringing'
                            THEN COALESCE(ringing_at, CURRENT_TIMESTAMP) ELSE ringing_at END,
                        answered_at=CASE WHEN ?='in_progress'
                            THEN COALESCE(answered_at, CURRENT_TIMESTAMP) ELSE answered_at END,
                        ended_at=CASE WHEN ?
                            THEN COALESCE(ended_at, CURRENT_TIMESTAMP) ELSE ended_at END,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (
                        target_status.value,
                        sid,
                        target_status.value,
                        target_status.value,
                        target_status.value,
                        int(ended),
                        call_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TelephonyConflictError(
                    "Provider SID is already assigned to another phone call"
                ) from exc
            if row[2] is not None:
                await conn.execute(
                    """
                    UPDATE PHONE_CALL_JOBS
                    SET status='completed', completed_at=COALESCE(completed_at, CURRENT_TIMESTAMP),
                        last_error_code=NULL, last_error_detail=NULL,
                        lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status='needs_attention'
                    """,
                    (row[2],),
                )
            if ended:
                await self._release_foreground_for_call(conn, call_id)
            return True

    async def expire_dispatch_unknown(
        self,
        *,
        now_utc: str,
        limit: int = 100,
    ) -> int:
        """Close ambiguous calls whose reconciliation window elapsed."""

        now = _normalize_utc_text(now_utc)
        if isinstance(limit, bool) or not 1 <= int(limit) <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                SELECT id, job_id FROM PHONE_CALLS
                WHERE status='dispatch_unknown' AND reconcile_deadline IS NOT NULL
                  AND reconcile_deadline <= ?
                ORDER BY reconcile_deadline, id LIMIT ?
                """,
                (now, int(limit)),
            )
            rows = await cursor.fetchall()
            for row in rows:
                await conn.execute(
                    """
                    UPDATE PHONE_CALLS
                    SET status='unresolved', termination_reason='dispatch_unresolved',
                        ended_at=COALESCE(ended_at, CURRENT_TIMESTAMP),
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status='dispatch_unknown'
                    """,
                    (row[0],),
                )
                await self._release_foreground_for_call(conn, str(row[0]))
                if row[1] is not None:
                    await conn.execute(
                        """
                        UPDATE PHONE_CALL_JOBS
                        SET last_error_code='dispatch_unresolved',
                            last_error_detail='Provider dispatch could not be reconciled',
                            lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status='needs_attention'
                        """,
                        (row[1],),
                    )
            return len(rows)

    async def renew_foreground_lease(
        self,
        *,
        call_id: str,
        lease_owner: str,
        fencing_token: int,
        lease_until: str,
        now_utc: str | None = None,
    ) -> bool:
        now = _normalize_utc_text(now_utc or _utc_now_text())
        lease_until_text = _normalize_utc_text(lease_until)
        if _parse_utc_text(lease_until_text) <= _parse_utc_text(now):
            raise ValueError("foreground lease must expire after now_utc")
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                UPDATE PHONE_CONVERSATION_FOREGROUND
                SET lease_until=?, updated_at=CURRENT_TIMESTAMP
                WHERE current_call_id=? AND lease_owner=? AND epoch=?
                  AND lease_until>=? AND lease_until<=?
                """,
                (
                    lease_until_text, call_id, lease_owner, int(fencing_token),
                    now, lease_until_text,
                ),
            )
            if cursor.rowcount != 1:
                return False
            await conn.execute(
                """
                UPDATE PHONE_CALLS SET foreground_lease_until=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND foreground_fencing_token=?
                """,
                (lease_until_text, call_id, int(fencing_token)),
            )
            return True

    async def take_over_expired_foreground_lease(
        self,
        *,
        call_id: str,
        new_lease_owner: str,
        now_utc: str,
        lease_until: str,
    ) -> int | None:
        """Take an expired lease and return its new fencing token."""
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                UPDATE PHONE_CONVERSATION_FOREGROUND
                SET lease_owner=?, lease_until=?, epoch=epoch+1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE current_call_id=? AND (lease_until IS NULL OR lease_until < ?)
                RETURNING epoch
                """,
                (new_lease_owner, lease_until, call_id, now_utc),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            epoch = int(row[0])
            await conn.execute(
                """
                UPDATE PHONE_CALLS SET foreground_lease_owner=?, foreground_lease_until=?,
                    foreground_fencing_token=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (new_lease_owner, lease_until, epoch, call_id),
            )
            return epoch

    async def disable_phone_for_incognito(
        self, *, owner_user_id: int, conversation_id: int
    ) -> dict[str, int]:
        """Atomically remove routing and cancel work when a chat becomes incognito."""
        async with self._write() as conn:
            return await self.disable_phone_for_incognito_in_transaction(
                conn,
                owner_user_id=owner_user_id,
                conversation_id=conversation_id,
                require_incognito=True,
            )

    async def disable_phone_for_incognito_in_transaction(
        self,
        conn: Any,
        *,
        owner_user_id: int,
        conversation_id: int,
        require_incognito: bool = False,
    ) -> dict[str, int]:
        """Remove phone routing inside the caller's privacy transaction.

        A live/ambiguous call blocks the privacy transition.  Ending it is an
        external provider action and must not be attempted from this database
        transaction.
        """

        owner_id = int(owner_user_id)
        conv_id = int(conversation_id)
        await self._owned_conversation(conn, owner_id, conv_id)
        if not await self._telephony_schema_present(conn):
            return {"bindings_disabled": 0, "jobs_canceled": 0}
        if require_incognito and not await self._conversation_is_incognito(conn, conv_id):
            raise TelephonyConflictError("Conversation is not incognito")
        if await self._conversation_has_incompatible_call(conn, conv_id):
            raise TelephonyConflictError(
                "An active phone call prevents enabling incognito mode"
            )

        cursor = await conn.execute(
            """
            SELECT id FROM PHONE_CONVERSATION_BINDINGS
            WHERE conversation_id=? AND owner_user_id=? AND active=1
            """,
            (conv_id, owner_id),
        )
        binding_ids = [int(row[0]) for row in await cursor.fetchall()]
        for binding_id in binding_ids:
            await conn.execute(
                "DELETE FROM PHONE_ACTIVE_ROUTES WHERE binding_id=?", (binding_id,)
            )
            await conn.execute(
                """
                UPDATE PHONE_CONVERSATION_BINDINGS
                SET active=0, deactivated_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND active=1
                """,
                (binding_id,),
            )

        cursor = await conn.execute(
            """
            UPDATE PHONE_CALL_JOBS
            SET status='canceled', completed_at=CURRENT_TIMESTAMP,
                last_error_code='conversation_incognito',
                lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE conversation_id=? AND owner_user_id=?
              AND (
                  status='scheduled'
                  OR (
                      status='dispatching'
                      AND NOT EXISTS (
                          SELECT 1 FROM PHONE_CALLS c
                          WHERE c.job_id=PHONE_CALL_JOBS.id
                      )
                  )
              )
            """,
            (conv_id, owner_id),
        )
        return {
            "bindings_disabled": len(binding_ids),
            "jobs_canceled": int(cursor.rowcount),
        }

    async def create_purge_job(
        self,
        *,
        job_id: str,
        owner_user_id: int,
        conversation_id: int,
        purge_scope: str,
        conversation_revision: int,
        call_id: str | None = None,
        recording_id: int | None = None,
    ) -> dict[str, Any]:
        if purge_scope not in {"call", "recording"}:
            raise ValueError("Invalid purge scope")
        async with self._write() as conn:
            await self._owned_conversation(conn, int(owner_user_id), int(conversation_id))
            cursor = await conn.execute(
                """
                SELECT id AS call_id, provider_call_sid FROM PHONE_CALLS
                WHERE id=? AND owner_user_id=? AND conversation_id=?
                """,
                (call_id, int(owner_user_id), int(conversation_id)),
            )
            source = _row_dict(await cursor.fetchone())
            if source is None:
                raise TelephonyNotFoundError("Purge source not found")
            recording_cursor = await conn.execute(
                """
                SELECT id AS recording_id, provider_recording_sid,
                       participant_path, assistant_path, mixed_path
                FROM PHONE_RECORDINGS
                WHERE call_id=? AND (? IS NULL OR id=?) ORDER BY id
                """,
                (
                    source["call_id"],
                    recording_id if purge_scope == "recording" else None,
                    recording_id if purge_scope == "recording" else None,
                ),
            )
            recordings = [dict(row) for row in await recording_cursor.fetchall()]
            if purge_scope == "recording" and not recordings:
                raise TelephonyNotFoundError("Purge source not found")
            selected_recording = recordings[0] if purge_scope == "recording" else None
            snapshot = {
                "provider_call_sid": source["provider_call_sid"],
                "recordings": recordings,
            }
            cursor = await conn.execute(
                """
                INSERT INTO PHONE_DATA_PURGE_JOBS (
                    id, owner_user_id, conversation_id, call_id, recording_id,
                    owner_user_id_snapshot, conversation_id_snapshot,
                    call_id_snapshot, recording_id_snapshot, purge_scope,
                    conversation_revision, provider_call_sid_snapshot,
                    provider_recording_sid_snapshot, source_snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING *
                """,
                (
                    job_id, int(owner_user_id), int(conversation_id), source["call_id"],
                    selected_recording["recording_id"] if selected_recording else None,
                    int(owner_user_id), int(conversation_id), source["call_id"],
                    selected_recording["recording_id"] if selected_recording else None,
                    purge_scope, int(conversation_revision), source["provider_call_sid"],
                    selected_recording["provider_recording_sid"] if selected_recording else None,
                    _json(snapshot),
                ),
            )
            return dict(await cursor.fetchone())

    async def claim_purge_job(
        self,
        *,
        job_id: str,
        lease_owner: str,
        lease_until: str,
        lease_token: str | None = None,
        now_utc: str | None = None,
    ) -> dict[str, Any] | None:
        token = lease_token or secrets.token_urlsafe(24)
        now = now_utc or _utc_now_text()
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                UPDATE PHONE_DATA_PURGE_JOBS
                SET status='running', lease_owner=?, lease_token=?, lease_until=?,
                    attempt_count=attempt_count+1, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND (status='scheduled' OR
                    (status='running' AND (lease_until IS NULL OR lease_until<?)))
                RETURNING *
                """,
                (lease_owner, token, lease_until, job_id, now),
            )
            return _row_dict(await cursor.fetchone())

    async def retry_purge_job(
        self,
        *,
        job_id: str,
        expected_attempt_count: int,
    ) -> bool:
        """Explicitly requeue an attended purge while fencing stale workers."""
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                UPDATE PHONE_DATA_PURGE_JOBS
                SET status='scheduled', last_error=NULL, lease_owner=NULL,
                    lease_token=NULL, lease_until=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='needs_attention' AND attempt_count=?
                """,
                (job_id, int(expected_attempt_count)),
            )
            return cursor.rowcount == 1

    async def finish_purge_job(
        self,
        *,
        job_id: str,
        lease_owner: str,
        lease_token: str,
        success: bool,
        error: str | None = None,
    ) -> bool:
        """Complete and delete the source atomically, or retain it for attention."""
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM PHONE_DATA_PURGE_JOBS
                WHERE id=? AND status='running' AND lease_owner=? AND lease_token=?
                """,
                (job_id, lease_owner, lease_token),
            )
            job = _row_dict(await cursor.fetchone())
            if job is None:
                return False
            if not success:
                await conn.execute(
                    """
                    UPDATE PHONE_DATA_PURGE_JOBS
                    SET status='needs_attention', last_error=?, lease_owner=NULL,
                        lease_token=NULL, lease_until=NULL, updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (error or "purge_failed", job_id),
                )
                return True
            await conn.execute(
                """
                UPDATE PHONE_DATA_PURGE_JOBS
                SET status='completed', completed_at=CURRENT_TIMESTAMP,
                    lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (job_id,),
            )
            if job["purge_scope"] == "recording" and job["recording_id"] is not None:
                await conn.execute("DELETE FROM PHONE_RECORDINGS WHERE id=?", (job["recording_id"],))
            elif job["purge_scope"] == "call" and job["call_id"] is not None:
                await conn.execute("DELETE FROM PHONE_CALLS WHERE id=?", (job["call_id"],))
            return True

    async def append_call_event(
        self,
        *,
        dedupe_key: str,
        event_type: str,
        signature_valid: bool,
        payload: dict[str, Any],
        call_id: str | None = None,
        provider_call_sid: str | None = None,
        provider_event_id: str | None = None,
        provider_occurred_at: str | None = None,
    ) -> tuple[int, bool]:
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                INSERT OR IGNORE INTO PHONE_CALL_EVENTS (
                    call_id, provider_call_sid, provider_event_id, dedupe_key,
                    event_type, signature_valid, provider_occurred_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id,
                    provider_call_sid,
                    provider_event_id,
                    dedupe_key,
                    event_type,
                    int(signature_valid),
                    provider_occurred_at,
                    _json(payload),
                ),
            )
            inserted = cursor.rowcount == 1
            lookup = await conn.execute(
                "SELECT id FROM PHONE_CALL_EVENTS WHERE dedupe_key = ?", (dedupe_key,)
            )
            return int((await lookup.fetchone())[0]), inserted

    async def _owned_conversation(self, conn: Any, owner_id: int, conv_id: int) -> dict[str, Any]:
        cursor = await conn.execute(
            "SELECT * FROM CONVERSATIONS WHERE id = ? AND user_id = ?",
            (conv_id, owner_id),
        )
        row = _row_dict(await cursor.fetchone())
        if row is None:
            raise TelephonyNotFoundError("Conversation not found")
        return row

    async def _owner_is_enabled(self, conn: Any, owner_id: int) -> bool:
        cursor = await conn.execute(
            "SELECT COALESCE(is_enabled,0) FROM USERS WHERE id=?",
            (int(owner_id),),
        )
        row = await cursor.fetchone()
        return bool(row and row[0])

    async def _profile_phone_state(
        self, conn: Any, owner_id: int
    ) -> dict[str, Any]:
        cursor = await conn.execute(
            """
            SELECT u.phone_number,COALESCE(u.phone_verified,0) AS phone_verified,
                   COALESCE(u.is_enabled,0) AS is_enabled,
                   lower(COALESCE(r.role_name,'')) AS role_name
            FROM USERS u
            LEFT JOIN USER_ROLES r ON r.id=u.role_id
            WHERE u.id=?
            """,
            (int(owner_id),),
        )
        row = _row_dict(await cursor.fetchone())
        if row is None:
            raise TelephonyNotFoundError("User profile not found")
        raw_phone = str(row.get("phone_number") or "").strip()
        configured = bool(raw_phone)
        canonical = configured and _E164_RE.fullmatch(raw_phone) is not None
        verified = row.get("phone_verified") == 1
        is_admin = str(row.get("role_name") or "") == "admin"
        enabled = bool(row.get("is_enabled"))
        eligible = bool(enabled and canonical and (verified or is_admin))
        return {
            "e164": raw_phone or None,
            "configured": configured,
            "canonical": bool(canonical),
            "verified": verified,
            "eligible": eligible,
            "verification_bypassed": bool(
                enabled and canonical and is_admin and not verified
            ),
            "is_admin": is_admin,
            "is_enabled": enabled,
        }

    async def _require_profile_phone(
        self,
        conn: Any,
        owner_id: int,
        *,
        expected_e164: str | None = None,
    ) -> dict[str, Any]:
        state = await self._profile_phone_state(conn, int(owner_id))
        if not state["is_enabled"]:
            raise TelephonyConflictError("Conversation owner is disabled")
        if not state["configured"]:
            raise TelephonyConflictError(
                "Add a phone number to your profile before using telephone calls"
            )
        if not state["canonical"]:
            raise TelephonyConflictError(
                "Your profile phone number must use valid E.164 format"
            )
        if expected_e164 is not None and str(expected_e164) != state["e164"]:
            raise TelephonyConflictError(
                "Telephone calls can only use your profile phone number"
            )
        if not state["eligible"]:
            raise TelephonyConflictError(
                "Verify your profile phone number before using telephone calls"
            )
        return state

    async def _conversation_is_incognito(self, conn: Any, conversation_id: int) -> bool:
        cursor = await conn.execute("PRAGMA table_info(CONVERSATIONS)")
        columns = {str(row[1]) for row in await cursor.fetchall()}
        if "is_incognito" not in columns:
            return False
        cursor = await conn.execute(
            "SELECT COALESCE(is_incognito, 0) FROM CONVERSATIONS WHERE id = ?",
            (conversation_id,),
        )
        row = await cursor.fetchone()
        return bool(row and row[0])

    async def _conversation_is_locked(self, conn: Any, conversation_id: int) -> bool:
        cursor = await conn.execute(
            "SELECT COALESCE(locked, 0) FROM CONVERSATIONS WHERE id = ?",
            (conversation_id,),
        )
        row = await cursor.fetchone()
        return bool(row and row[0])

    async def _require_bound_contact_mutable(
        self,
        conn: Any,
        *,
        owner_user_id: int,
        conversation_id: int,
    ) -> None:
        conversation = await self._owned_conversation(
            conn,
            int(owner_user_id),
            int(conversation_id),
        )
        if bool(conversation.get("locked")):
            raise TelephonyConflictError("Conversation is locked")
        if await self._conversation_is_incognito(conn, int(conversation_id)):
            raise TelephonyConflictError(
                "Incognito conversations cannot retain phone contacts"
            )

    async def _telephony_schema_present(self, conn: Any) -> bool:
        required = {
            "PHONE_CONVERSATION_BINDINGS",
            "PHONE_ACTIVE_ROUTES",
            "PHONE_CALL_JOBS",
            "PHONE_CALLS",
        }
        placeholders = ",".join("?" for _ in required)
        cursor = await conn.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' "
            f"AND name IN ({placeholders})",
            tuple(sorted(required)),
        )
        present = {str(row[0]) for row in await cursor.fetchall()}
        if not present:
            return False
        if present != required:
            raise TelephonyStateError("Telephony schema is incomplete")
        return True

    async def _conversation_has_incompatible_call(
        self, conn: Any, conversation_id: int
    ) -> bool:
        placeholders = ",".join("?" for _ in CALL_INCOMPATIBLE_STATUSES)
        statuses = sorted(status.value for status in CALL_INCOMPATIBLE_STATUSES)
        cursor = await conn.execute(
            f"SELECT 1 FROM PHONE_CALLS WHERE conversation_id=? "
            f"AND deleted_at IS NULL AND status IN ({placeholders}) LIMIT 1",
            (int(conversation_id), *statuses),
        )
        return await cursor.fetchone() is not None

    async def _owned_dispatch_state(
        self,
        conn: Any,
        *,
        job_id: str,
        call_id: str,
        lease_owner: str,
        lease_token: str,
        now_utc: str | None = None,
    ) -> dict[str, Any]:
        cursor = await conn.execute(
            """
            SELECT j.status AS job_status, j.lease_owner, j.lease_token,
                   j.lease_until,
                   c.status AS call_status, c.provider_call_sid,
                   c.provider_request_started_at
            FROM PHONE_CALL_JOBS j
            JOIN PHONE_CALLS c ON c.job_id=j.id
            WHERE j.id=? AND c.id=?
            """,
            (job_id, call_id),
        )
        state = _row_dict(await cursor.fetchone())
        now = _parse_utc_text(now_utc or _utc_now_text())
        if (
            state is None
            or state["job_status"]
            not in {
                PhoneJobStatus.DISPATCHING.value,
                PhoneJobStatus.NEEDS_ATTENTION.value,
            }
            or state["lease_owner"] != lease_owner
            or state["lease_token"] != lease_token
            or state["lease_until"] is None
            or _parse_utc_text(state["lease_until"]) < now
            or state["provider_request_started_at"] is None
        ):
            raise TelephonyStateError("Phone-call dispatch lease is no longer owned")
        return state

    @staticmethod
    def _decode_dispatcher_heartbeats(value: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(value, str) or not value.strip():
            return {}
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        if not isinstance(decoded, dict):
            return {}
        return {
            str(key): record
            for key, record in decoded.items()
            if isinstance(key, str) and isinstance(record, dict)
        }

    async def _has_live_pre_due_dispatcher(
        self,
        conn: Any,
        *,
        scheduled_at_utc: str,
        now_utc: str,
        excluding_dispatcher_id: str | None,
    ) -> bool:
        cursor = await conn.execute(
            "SELECT value FROM SYSTEM_CONFIG WHERE key=?",
            (_DISPATCHER_HEARTBEAT_CONFIG_KEY,),
        )
        row = await cursor.fetchone()
        registry = self._decode_dispatcher_heartbeats(row[0] if row else None)
        scheduled = _parse_utc_text(scheduled_at_utc)
        now = _parse_utc_text(now_utc)
        for instance_id, record in registry.items():
            if excluding_dispatcher_id and instance_id == excluding_dispatcher_id:
                continue
            try:
                live_since = _parse_utc_text(record["live_since_utc"])
                lease_until = _parse_utc_text(record["lease_until_utc"])
            except (KeyError, TypeError, ValueError):
                continue
            if live_since <= scheduled and lease_until >= now:
                return True
        return False

    async def _acquire_foreground(
        self,
        conn: Any,
        *,
        conversation_id: int,
        call_id: str,
        lease_owner: str,
        lease_until: str,
    ) -> int:
        await conn.execute(
            "INSERT OR IGNORE INTO PHONE_CONVERSATION_FOREGROUND(conversation_id) VALUES (?)",
            (conversation_id,),
        )
        cursor = await conn.execute(
            """
            UPDATE PHONE_CONVERSATION_FOREGROUND
            SET epoch=epoch+1, current_call_id=?, lease_owner=?, lease_until=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE conversation_id=? AND current_call_id IS NULL
            RETURNING epoch
            """,
            (call_id, lease_owner, lease_until, conversation_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise TelephonyConflictError("Conversation foreground is already owned")
        return int(row[0])

    async def _transfer_foreground_for_call(
        self,
        conn: Any,
        *,
        conversation_id: int,
        call_id: str,
        lease_owner: str,
        lease_until: str,
    ) -> int:
        """Fence a pre-POST recovery onto its already-created call."""

        cursor = await conn.execute(
            """
            UPDATE PHONE_CONVERSATION_FOREGROUND
            SET epoch=epoch+1, lease_owner=?, lease_until=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE conversation_id=? AND current_call_id=?
            RETURNING epoch
            """,
            (lease_owner, lease_until, conversation_id, call_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise TelephonyStateError(
                "Existing phone call no longer owns conversation foreground"
            )
        epoch = int(row[0])
        await conn.execute(
            """
            UPDATE PHONE_CALLS
            SET foreground_fencing_token=?, foreground_lease_owner=?,
                foreground_lease_until=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND conversation_id=?
            """,
            (epoch, lease_owner, lease_until, call_id, conversation_id),
        )
        return epoch

    async def _release_foreground_for_call(self, conn: Any, call_id: str) -> int | None:
        cursor = await conn.execute(
            """
            UPDATE PHONE_CONVERSATION_FOREGROUND
            SET epoch=epoch+1, current_call_id=NULL, lease_owner=NULL,
                lease_until=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE current_call_id=?
            RETURNING epoch
            """,
            (call_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        await conn.execute(
            """
            UPDATE PHONE_CALLS SET foreground_lease_owner=NULL,
                foreground_lease_until=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (call_id,),
        )
        return int(row[0])

    async def _miss_unstarted_dispatch(
        self,
        conn: Any,
        job: dict[str, Any],
    ) -> None:
        call_id = job.get("linked_call_id") or job.get("call_id")
        if call_id:
            await conn.execute(
                """
                UPDATE PHONE_CALLS SET status='canceled',
                    termination_reason='dispatch_window_elapsed',
                    ended_at=COALESCE(ended_at, CURRENT_TIMESTAMP),
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND provider_request_started_at IS NULL
                  AND status IN ('created','dispatching')
                """,
                (call_id,),
            )
            await self._release_foreground_for_call(conn, str(call_id))
        await conn.execute(
            """
            UPDATE PHONE_CALL_JOBS SET status='missed',
                completed_at=COALESCE(completed_at, CURRENT_TIMESTAMP),
                last_error_code='job_missed',
                last_error_detail='Scheduled dispatch window elapsed',
                lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='dispatching'
            """,
            (job["id"],),
        )

    async def _cancel_unstarted_job_for_incognito(
        self, conn: Any, job: dict[str, Any]
    ) -> None:
        call_id = job.get("linked_call_id") or job.get("call_id")
        if call_id:
            await conn.execute(
                """
                UPDATE PHONE_CALLS SET status='canceled',
                    termination_reason='conversation_incognito',
                    ended_at=COALESCE(ended_at, CURRENT_TIMESTAMP),
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND provider_request_started_at IS NULL
                  AND status IN ('created','dispatching')
                """,
                (call_id,),
            )
            await self._release_foreground_for_call(conn, str(call_id))
        await conn.execute(
            """
            UPDATE PHONE_CALL_JOBS SET status='canceled',
                completed_at=CURRENT_TIMESTAMP,
                last_error_code='conversation_incognito',
                lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status IN ('scheduled','dispatching')
            """,
            (job["id"],),
        )

    async def _cancel_unstarted_job_for_disabled_owner(
        self, conn: Any, job: dict[str, Any]
    ) -> None:
        call_id = job.get("linked_call_id") or job.get("call_id")
        if call_id:
            await conn.execute(
                """
                UPDATE PHONE_CALLS SET status='canceled',
                    termination_reason='owner_disabled',
                    ended_at=COALESCE(ended_at,CURRENT_TIMESTAMP),
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND provider_request_started_at IS NULL
                  AND status IN ('created','dispatching')
                """,
                (call_id,),
            )
            await self._release_foreground_for_call(conn, str(call_id))
        await conn.execute(
            """
            UPDATE PHONE_CALL_JOBS SET status='canceled',
                completed_at=CURRENT_TIMESTAMP,
                last_error_code='owner_disabled',
                last_error_detail='Conversation owner is disabled',
                lease_owner=NULL,lease_token=NULL,lease_until=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status IN ('scheduled','dispatching')
            """,
            (job["id"],),
        )

    async def _reject_unstarted_job_for_lock(
        self, conn: Any, job: dict[str, Any]
    ) -> None:
        call_id = job.get("linked_call_id") or job.get("call_id")
        if call_id:
            await conn.execute(
                """
                UPDATE PHONE_CALLS SET status='canceled',
                    termination_reason='conversation_locked',
                    ended_at=COALESCE(ended_at, CURRENT_TIMESTAMP),
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND provider_request_started_at IS NULL
                  AND status IN ('created','dispatching')
                """,
                (call_id,),
            )
            await self._release_foreground_for_call(conn, str(call_id))
        await conn.execute(
            """
            UPDATE PHONE_CALL_JOBS SET status='conflict',
                completed_at=CURRENT_TIMESTAMP,
                last_error_code='conversation_locked',
                lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status IN ('scheduled','dispatching')
            """,
            (job["id"],),
        )

    async def _reject_unstarted_job_for_binding(
        self, conn: Any, job: dict[str, Any]
    ) -> None:
        call_id = job.get("linked_call_id") or job.get("call_id")
        if call_id:
            await conn.execute(
                """
                UPDATE PHONE_CALLS SET status='canceled',
                    termination_reason='binding_inactive',
                    ended_at=COALESCE(ended_at, CURRENT_TIMESTAMP),
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND provider_request_started_at IS NULL
                  AND status IN ('created','dispatching')
                """,
                (call_id,),
            )
            await self._release_foreground_for_call(conn, str(call_id))
        await conn.execute(
            """
            UPDATE PHONE_CALL_JOBS SET status='conflict',
                completed_at=CURRENT_TIMESTAMP,
                last_error_code='binding_inactive',
                lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status IN ('scheduled','dispatching')
            """,
            (job["id"],),
        )

    async def _reject_unstarted_job_for_profile(
        self, conn: Any, job: dict[str, Any]
    ) -> None:
        call_id = job.get("linked_call_id") or job.get("call_id")
        if call_id:
            await conn.execute(
                """
                UPDATE PHONE_CALLS SET status='canceled',
                    termination_reason='profile_phone_ineligible',
                    ended_at=COALESCE(ended_at,CURRENT_TIMESTAMP),
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND provider_request_started_at IS NULL
                  AND status IN ('created','dispatching')
                """,
                (call_id,),
            )
            await self._release_foreground_for_call(conn, str(call_id))
        await conn.execute(
            """
            UPDATE PHONE_CALL_JOBS SET status='conflict',
                completed_at=CURRENT_TIMESTAMP,
                last_error_code='profile_phone_ineligible',
                last_error_detail='Profile phone is missing, changed, or unverified',
                lease_owner=NULL,lease_token=NULL,lease_until=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status IN ('scheduled','dispatching')
            """,
            (job["id"],),
        )

    async def _require_enabled_number(self, conn: Any, number_id: int) -> dict[str, Any]:
        inventory_marker = await _number_inventory_marker(conn)
        cursor = await conn.execute(
            "SELECT * FROM TELEPHONY_NUMBERS WHERE id = ? AND enabled = 1",
            (number_id,),
        )
        row = _row_dict(await cursor.fetchone())
        if (
            row is None
            or inventory_marker is None
            or str(row.get("synced_at") or "") != inventory_marker
            or not _has_voice_capability(row.get("capabilities_json"))
        ):
            raise TelephonyConflictError("Preferred telephony number is not enabled")
        return row

    async def _resolve_outbound_number(self, conn: Any, preferred: int | None) -> int:
        if preferred is not None:
            row = await self._require_enabled_number(conn, int(preferred))
            return int(row["id"])
        inventory_marker = await _number_inventory_marker(conn)
        if inventory_marker is None:
            raise TelephonyConflictError("Exactly one outbound default number is required")
        cursor = await conn.execute(
            """
            SELECT * FROM TELEPHONY_NUMBERS
            WHERE enabled=1 AND is_outbound_default=1 AND synced_at=?
            """,
            (inventory_marker,),
        )
        rows = [
            dict(row)
            for row in await cursor.fetchall()
            if _has_voice_capability(row["capabilities_json"])
        ]
        if len(rows) != 1:
            raise TelephonyConflictError("Exactly one outbound default number is required")
        row = await self._require_enabled_number(conn, int(rows[0]["id"]))
        return int(row["id"])

    async def _binding_by_id(self, conn: Any, binding_id: int) -> dict[str, Any]:
        cursor = await conn.execute(
            "SELECT * FROM PHONE_CONVERSATION_BINDINGS WHERE id = ?", (binding_id,)
        )
        row = _row_dict(await cursor.fetchone())
        if row is None:
            raise TelephonyNotFoundError("Phone binding not found")
        return row

    async def _binding_details(self, conn: Any, binding_id: int) -> dict[str, Any]:
        cursor = await conn.execute(
            """
            SELECT b.id,b.conversation_id,b.contact_id,b.preferred_number_id,
                   b.allow_inbound,b.allow_outbound,b.created_at,b.updated_at,
                   c.display_name,c.e164,c.timezone_name,
                   n.e164 AS preferred_number_e164
            FROM PHONE_CONVERSATION_BINDINGS b
            JOIN PHONE_CONTACTS c ON c.id=b.contact_id AND c.active=1
            LEFT JOIN TELEPHONY_NUMBERS n ON n.id=b.preferred_number_id
            WHERE b.id=? AND b.active=1
            """,
            (int(binding_id),),
        )
        row = _row_dict(await cursor.fetchone())
        if row is None:
            raise TelephonyNotFoundError("Active phone binding not found")
        return row

    async def _binding_snapshot(
        self,
        conn: Any,
        *,
        owner_user_id: int,
        conversation_id: int,
        binding_id: int,
    ) -> BindingSnapshot:
        cursor = await conn.execute(
            """
            SELECT b.*, c.e164 AS contact_e164
            FROM PHONE_CONVERSATION_BINDINGS b
            JOIN PHONE_CONTACTS c ON c.id = b.contact_id
            WHERE b.id = ? AND b.owner_user_id = ? AND b.conversation_id = ?
              AND b.active = 1 AND c.active = 1
            """,
            (binding_id, owner_user_id, conversation_id),
        )
        row = _row_dict(await cursor.fetchone())
        if row is None:
            raise TelephonyNotFoundError("Active phone binding not found")
        return BindingSnapshot(
            binding_id=int(row["id"]),
            owner_user_id=int(row["owner_user_id"]),
            conversation_id=int(row["conversation_id"]),
            contact_id=int(row["contact_id"]),
            contact_e164=str(row["contact_e164"]),
            preferred_number_id=(
                int(row["preferred_number_id"])
                if row["preferred_number_id"] is not None
                else None
            ),
            allow_inbound=bool(row["allow_inbound"]),
            allow_outbound=bool(row["allow_outbound"]),
        )

    async def _binding_has_incompatible_call(self, conn: Any, binding_id: int) -> bool:
        placeholders = ",".join("?" for _ in CALL_INCOMPATIBLE_STATUSES)
        statuses = sorted(status.value for status in CALL_INCOMPATIBLE_STATUSES)
        cursor = await conn.execute(
            f"SELECT 1 FROM PHONE_CALLS WHERE binding_id = ? AND deleted_at IS NULL "
            f"AND status IN ({placeholders}) LIMIT 1",
            (binding_id, *statuses),
        )
        return await cursor.fetchone() is not None

    async def _deactivate_binding(self, conn: Any, binding_id: int) -> None:
        await conn.execute(
            "DELETE FROM PHONE_ACTIVE_ROUTES WHERE binding_id = ?", (binding_id,)
        )
        await conn.execute(
            """
            UPDATE PHONE_CONVERSATION_BINDINGS
            SET active = 0, deactivated_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND active = 1
            """,
            (binding_id,),
        )
        await self._cancel_unstarted_binding_jobs(
            conn, int(binding_id), error_code="binding_reassigned"
        )

    async def _cancel_unstarted_binding_jobs(
        self, conn: Any, binding_id: int, *, error_code: str
    ) -> None:
        await conn.execute(
            """
            UPDATE PHONE_CALL_JOBS
            SET status='canceled',completed_at=CURRENT_TIMESTAMP,
                last_error_code=?,lease_owner=NULL,lease_token=NULL,
                lease_until=NULL,updated_at=CURRENT_TIMESTAMP
            WHERE binding_id=? AND status='scheduled'
            """,
            (str(error_code), int(binding_id)),
        )
        await conn.execute(
            """
            UPDATE PHONE_CALL_JOBS
            SET status='conflict',completed_at=CURRENT_TIMESTAMP,
                last_error_code=?,lease_owner=NULL,lease_token=NULL,
                lease_until=NULL,updated_at=CURRENT_TIMESTAMP
            WHERE binding_id=? AND status='dispatching'
              AND NOT EXISTS (
                  SELECT 1 FROM PHONE_CALLS c
                  WHERE c.job_id=PHONE_CALL_JOBS.id
              )
            """,
            (str(error_code), int(binding_id)),
        )
