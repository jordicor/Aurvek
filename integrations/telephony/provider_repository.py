"""Durable Twilio-facing operations for the native phone channel.

The main :mod:`repository` owns user workflows and the outbound dispatcher.
This narrower repository owns only signed provider callbacks and Media Streams
attachment.  Keeping those transactions here avoids teaching HTTP routes about
the database schema while preserving SQLite as the concurrency authority.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
import re
import secrets
import sqlite3
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping
from uuid import uuid4

from database import get_db_connection
from integrations.telephony.callbacks import (
    NormalizedCallStatus,
    NormalizedRecordingStatus,
    NormalizedStreamStatus,
    answered_by_is_machine,
)
from integrations.telephony.repository import (
    PhoneHangupAttemptClaim,
    TelephonyConflictError,
    TelephonyInboundUnavailableError,
    TelephonyNotFoundError,
    TelephonyStateError,
    claim_phone_hangup_attempt_in_transaction,
    confirm_phone_hangup_in_transaction,
    mark_phone_hangup_accepted_in_transaction,
    mark_phone_hangup_unresolved_in_transaction,
    reconcile_phone_hangup_provider_absent_in_transaction,
)
from integrations.telephony.schemas import (
    CALL_TERMINAL_STATUSES,
    PhoneCallStatus,
    call_transition_result,
)
from integrations.telephony.snapshot import build_conversation_phone_snapshot
from log_config import logger


_CALL_SID = re.compile(r"^CA[0-9A-Fa-f]{32}$")
_STREAM_SID = re.compile(r"^MZ[0-9A-Fa-f]{32}$")
_OPAQUE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_SESSION_ELIGIBLE = frozenset(
    {
        "queued",
        "initiated",
        "ringing",
        "in_progress",
        "dispatch_unknown",
    }
)
def _utc_text(value: datetime | None = None) -> str:
    observed = (value or datetime.now(UTC)).astimezone(UTC)
    return observed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _require_sid(value: str, pattern: re.Pattern[str], field: str) -> str:
    normalized = str(value or "")
    if pattern.fullmatch(normalized) is None:
        raise ValueError(f"invalid {field}")
    return normalized


def _row(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


MemoryTurnRecorder = Callable[..., Awaitable[bool]]


async def _default_memory_turn_recorder(**values: Any) -> bool:
    from ai_runtime.memory.recording import _record_memory_turn_best_effort

    return await _record_memory_turn_best_effort(**values)


def _snapshot_prompt_id(value: Any) -> int | None:
    try:
        prompt_id = int(json.loads(str(value))["prompt_id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return prompt_id if prompt_id > 0 else None


class TelephonyProviderRepository:
    """Atomic operations used only after Twilio authentication succeeds."""

    def __init__(
        self,
        connection_factory: Callable[..., Any] = get_db_connection,
        *,
        memory_turn_recorder: MemoryTurnRecorder | None = None,
    ):
        self._connection_factory = connection_factory
        self._memory_turn_recorder = (
            memory_turn_recorder or _default_memory_turn_recorder
        )

    @property
    def connection_factory(self) -> Callable[..., Any]:
        return self._connection_factory

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

    async def create_inbound_call(
        self,
        *,
        provider_call_sid: str,
        caller_e164: str,
        called_e164: str,
    ) -> tuple[dict[str, Any], bool]:
        """Resolve a known caller, acquire foreground and create one call.

        Unknown callers return ``(None, False)`` through ``resolve`` at the
        route layer; this method never creates a conversation or contact.
        A duplicate signed inbound webhook returns the existing call.
        """

        sid = _require_sid(provider_call_sid, _CALL_SID, "CallSid")
        async with self._write() as conn:
            if await self._callback_tombstoned(conn, provider_call_sid=sid):
                raise TelephonyConflictError("CallSid was permanently deleted")
            existing_cursor = await conn.execute(
                "SELECT * FROM PHONE_CALLS WHERE provider_call_sid=? AND deleted_at IS NULL",
                (sid,),
            )
            existing = _row(await existing_cursor.fetchone())
            if existing is not None:
                if (
                    existing["direction"] != "inbound"
                    or existing["from_e164"] != caller_e164
                    or existing["to_e164"] != called_e164
                ):
                    raise TelephonyConflictError("CallSid is already bound incompatibly")
                return existing, False

            number_cursor = await conn.execute(
                "SELECT id,enabled,inbound_enabled FROM TELEPHONY_NUMBERS WHERE e164=?",
                (called_e164,),
            )
            inbound_number = _row(await number_cursor.fetchone())
            if inbound_number is not None and (
                not bool(inbound_number["enabled"])
                or not bool(inbound_number["inbound_enabled"])
            ):
                raise TelephonyInboundUnavailableError(
                    "Incoming calls are disabled for this telephony number"
                )

            binding_cursor = await conn.execute(
                """
                SELECT b.*, c.e164 AS contact_e164, c.display_name,
                       n.id AS inbound_number_id, n.e164 AS inbound_number_e164
                FROM PHONE_ACTIVE_ROUTES r
                JOIN PHONE_CONVERSATION_BINDINGS b ON b.id=r.binding_id
                JOIN PHONE_CONTACTS c ON c.id=r.contact_id
                JOIN CONVERSATIONS conv ON conv.id=b.conversation_id
                JOIN USERS owner ON owner.id=b.owner_user_id
                LEFT JOIN USER_ROLES owner_role ON owner_role.id=owner.role_id
                JOIN TELEPHONY_NUMBERS n ON n.e164=?
                WHERE r.e164=? AND b.active=1 AND b.allow_inbound=1
                  AND c.active=1 AND n.enabled=1 AND n.inbound_enabled=1
                  AND COALESCE(owner.is_enabled,0)=1
                  AND owner.phone_number=c.e164
                  AND r.e164=c.e164
                  AND (
                    COALESCE(owner.phone_verified,0)=1
                    OR lower(COALESCE(owner_role.role_name,''))='admin'
                  )
                  AND COALESCE(conv.is_incognito, 0)=0
                  AND COALESCE(conv.locked, 0)=0
                """,
                (called_e164, caller_e164),
            )
            binding = _row(await binding_cursor.fetchone())
            if binding is None:
                if inbound_number is not None:
                    route_cursor = await conn.execute(
                        """
                        SELECT 1
                        FROM PHONE_ACTIVE_ROUTES r
                        JOIN PHONE_CONVERSATION_BINDINGS b ON b.id=r.binding_id
                        WHERE r.e164=? AND b.active=1
                        LIMIT 1
                        """,
                        (caller_e164,),
                    )
                    if await route_cursor.fetchone() is not None:
                        raise TelephonyInboundUnavailableError(
                            "The linked conversation is not accepting incoming calls"
                        )
                raise TelephonyNotFoundError("No active inbound phone binding")

            snapshot = await build_conversation_phone_snapshot(
                int(binding["conversation_id"]),
                expected_owner_user_id=int(binding["owner_user_id"]),
                conn=conn,
            )
            snapshot_values = snapshot.as_dict()
            recording_enabled = bool(snapshot_values["recording_default"])
            if recording_enabled:
                from integrations.telephony.purge_state import (
                    phone_data_purge_runtime_operational,
                )

                recording_enabled = await phone_data_purge_runtime_operational(conn)
            call_id = uuid4().hex
            dispatch_token = secrets.token_urlsafe(32)
            maximum = int(snapshot_values["max_duration_seconds"])
            now = datetime.now(UTC)
            deadline = now + timedelta(seconds=maximum)
            lease_until = deadline + timedelta(seconds=120)
            lease_owner = f"media:{call_id}"
            binding_snapshot = {
                "binding_id": int(binding["id"]),
                "owner_user_id": int(binding["owner_user_id"]),
                "conversation_id": int(binding["conversation_id"]),
                "contact_id": int(binding["contact_id"]),
                "contact_e164": str(binding["contact_e164"]),
                "telephony_number_id": int(binding["inbound_number_id"]),
                "from_e164": str(caller_e164),
                "to_e164": str(called_e164),
                "allow_inbound": True,
            }
            try:
                cursor = await conn.execute(
                    """
                    INSERT INTO PHONE_CALLS (
                        id, owner_user_id, conversation_id, binding_id, contact_id,
                        telephony_number_id, direction, from_e164, to_e164,
                        transport, status, provider_call_sid, dispatch_token,
                        binding_snapshot_json, config_snapshot_json,
                        foreground_lease_owner, foreground_lease_until,
                        deadline_at, warning_milestones_json,
                        recording_enabled, amd_enabled, answered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'inbound', ?, ?, 'media_streams',
                              'in_progress', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    RETURNING *
                    """,
                    (
                        call_id,
                        int(binding["owner_user_id"]),
                        int(binding["conversation_id"]),
                        int(binding["id"]),
                        int(binding["contact_id"]),
                        int(binding["inbound_number_id"]),
                        caller_e164,
                        called_e164,
                        sid,
                        dispatch_token,
                        _json(binding_snapshot),
                        _json(snapshot_values),
                        lease_owner,
                        _utc_text(lease_until),
                        _utc_text(deadline),
                        json.dumps(snapshot_values["warning_milestones_seconds"]),
                        int(recording_enabled),
                        int(bool(snapshot_values["amd_default"])),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TelephonyConflictError(
                    "Conversation already has an active phone call"
                ) from exc
            call = dict(await cursor.fetchone())

            conversation_id = int(binding["conversation_id"])
            await conn.execute(
                "INSERT OR IGNORE INTO PHONE_CONVERSATION_FOREGROUND(conversation_id) VALUES (?)",
                (conversation_id,),
            )
            foreground = await conn.execute(
                """
                UPDATE PHONE_CONVERSATION_FOREGROUND
                SET epoch=epoch+1, current_call_id=?, lease_owner=?, lease_until=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE conversation_id=? AND current_call_id IS NULL
                RETURNING epoch
                """,
                (call_id, lease_owner, _utc_text(lease_until), conversation_id),
            )
            epoch_row = await foreground.fetchone()
            if epoch_row is None:
                raise TelephonyConflictError("Conversation foreground is already owned")
            epoch = int(epoch_row[0])
            await conn.execute(
                "UPDATE PHONE_CALLS SET foreground_fencing_token=? WHERE id=?",
                (epoch, call_id),
            )
            call["foreground_fencing_token"] = epoch
            return call, True

    async def get_call_by_dispatch_token(self, token: str) -> dict[str, Any] | None:
        normalized = str(token or "")
        if _OPAQUE_TOKEN.fullmatch(normalized) is None:
            return None
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CALLS WHERE dispatch_token=? AND deleted_at IS NULL",
                (normalized,),
            )
            return _row(await cursor.fetchone())

    async def get_call_by_provider_sid(self, call_sid: str) -> dict[str, Any] | None:
        sid = _require_sid(call_sid, _CALL_SID, "CallSid")
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CALLS WHERE provider_call_sid=? AND deleted_at IS NULL",
                (sid,),
            )
            return _row(await cursor.fetchone())

    async def reconcile_outbound_twiml(
        self,
        *,
        dispatch_token: str,
        provider_call_sid: str,
    ) -> dict[str, Any]:
        """Bind CallSid even when Twilio requested TwiML before REST returned."""

        sid = _require_sid(provider_call_sid, _CALL_SID, "CallSid")
        async with self._write() as conn:
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CALLS WHERE dispatch_token=? AND deleted_at IS NULL",
                (dispatch_token,),
            )
            call = _row(await cursor.fetchone())
            if call is None:
                raise TelephonyNotFoundError("Phone call not found")
            existing = call["provider_call_sid"]
            if existing is not None and existing != sid:
                raise TelephonyConflictError("Dispatch token belongs to another CallSid")
            if existing is None:
                try:
                    await conn.execute(
                        """
                        UPDATE PHONE_CALLS
                        SET provider_call_sid=?, status=CASE
                                WHEN status IN ('created','dispatching','dispatch_unknown')
                                THEN 'queued' ELSE status END,
                            reconcile_deadline=NULL, updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                        """,
                        (sid, call["id"]),
                    )
                except sqlite3.IntegrityError as exc:
                    raise TelephonyConflictError("CallSid belongs to another call") from exc
                call["provider_call_sid"] = sid
                if call["status"] in {"created", "dispatching", "dispatch_unknown"}:
                    call["status"] = "queued"
            # A signed TwiML request is provider evidence even when the REST
            # response arrived first.  Always reconcile a durable
            # ``needs_attention`` job, not only the callback-before-REST path.
            if call["job_id"] is not None:
                await self._complete_reconciled_job(conn, str(call["job_id"]))
            return call

    async def attach_stream(
        self,
        *,
        call_id: str,
        provider_call_sid: str,
        provider_stream_sid: str,
        stream_attempt: int,
    ) -> dict[str, Any]:
        sid = _require_sid(provider_call_sid, _CALL_SID, "CallSid")
        stream_sid = _require_sid(provider_stream_sid, _STREAM_SID, "StreamSid")
        if stream_attempt not in {0, 1, 2}:
            raise ValueError("stream_attempt must be between 0 and 2")
        async with self._write() as conn:
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CALLS WHERE id=? AND deleted_at IS NULL",
                (call_id,),
            )
            call = _row(await cursor.fetchone())
            if call is None:
                raise TelephonyNotFoundError("Phone call not found")
            if call["provider_call_sid"] != sid:
                raise TelephonyConflictError("Media stream CallSid does not match")
            if call["status"] not in _SESSION_ELIGIBLE:
                raise TelephonyStateError("Phone call cannot attach media")
            if int(call["reconnect_count"]) != stream_attempt:
                raise TelephonyConflictError("Media stream reconnect attempt is stale")
            existing = call["provider_stream_sid"]
            if existing is not None and existing != stream_sid:
                raise TelephonyConflictError("Another stream already owns this attempt")
            if existing is None:
                answered_at = str(call.get("answered_at") or _utc_text())
                try:
                    await conn.execute(
                        """
                        UPDATE PHONE_CALLS
                        SET provider_stream_sid=?, provider_session_id=?,
                            transport='media_streams', status='in_progress',
                            answered_at=COALESCE(answered_at,?),
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                        """,
                        (
                            stream_sid,
                            f"{stream_attempt}:{stream_sid}",
                            answered_at,
                            call_id,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise TelephonyConflictError("StreamSid belongs to another call") from exc
                call["provider_stream_sid"] = stream_sid
                call["provider_session_id"] = f"{stream_attempt}:{stream_sid}"
                call["status"] = "in_progress"
                call["answered_at"] = answered_at
            return call

    async def renew_session_foreground(
        self,
        *,
        call_id: str,
        fencing_token: int,
        lease_owner: str,
        lease_until: str,
        now_utc: str | None = None,
    ) -> bool:
        """Renew both durable lease copies under the exact call fence."""

        async with self._write() as conn:
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CALLS WHERE id=? AND deleted_at IS NULL",
                (str(call_id),),
            )
            call = _row(await cursor.fetchone())
            if call is None:
                return False
            if (
                int(call["foreground_fencing_token"]) != int(fencing_token)
                or str(call.get("foreground_lease_owner") or "") != str(lease_owner)
            ):
                return False
            return await self._renew_session_foreground_in_transaction(
                conn,
                call=call,
                lease_until=str(lease_until),
                now_utc=now_utc or _utc_text(),
            )

    async def _renew_session_foreground_in_transaction(
        self,
        conn: Any,
        *,
        call: Mapping[str, Any],
        lease_until: str,
        now_utc: str,
    ) -> bool:
        if str(call["status"]) not in _SESSION_ELIGIBLE:
            return False
        cursor = await conn.execute(
            """
            UPDATE PHONE_CONVERSATION_FOREGROUND
            SET lease_until=?, updated_at=CURRENT_TIMESTAMP
            WHERE conversation_id=? AND current_call_id=? AND epoch=?
              AND lease_owner=? AND lease_until IS NOT NULL AND lease_until>=?
            """,
            (
                str(lease_until),
                int(call["conversation_id"]),
                str(call["id"]),
                int(call["foreground_fencing_token"]),
                str(call["foreground_lease_owner"]),
                str(now_utc),
            ),
        )
        if cursor.rowcount != 1:
            return False
        cursor = await conn.execute(
            """
            UPDATE PHONE_CALLS
            SET foreground_lease_until=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND foreground_fencing_token=?
              AND foreground_lease_owner=? AND deleted_at IS NULL
            """,
            (
                str(lease_until),
                str(call["id"]),
                int(call["foreground_fencing_token"]),
                str(call["foreground_lease_owner"]),
            ),
        )
        return cursor.rowcount == 1

    async def prepare_reconnect(
        self,
        *,
        provider_call_sid: str,
        stream_attempt: int,
        dedupe_key: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Advance one reconnect slot, never exceeding the approved maximum."""

        sid = _require_sid(provider_call_sid, _CALL_SID, "CallSid")
        if stream_attempt not in {0, 1}:
            raise ValueError("stream_attempt must be 0 or 1 for reconnect")
        async with self._write() as conn:
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CALLS WHERE provider_call_sid=? AND deleted_at IS NULL",
                (sid,),
            )
            call = _row(await cursor.fetchone())
            if call is None:
                return None
            if call["status"] not in _SESSION_ELIGIBLE:
                return None
            if dedupe_key is not None:
                cursor = await conn.execute(
                    "SELECT 1 FROM PHONE_CALL_EVENTS WHERE dedupe_key=?",
                    (str(dedupe_key),),
                )
                if await cursor.fetchone() is not None:
                    return call
            count = int(call["reconnect_count"])
            if count != stream_attempt:
                return None
            outcome = await self._stream_attempt_result_in_transaction(
                conn,
                call_id=str(call["id"]),
                stream_attempt=stream_attempt,
            )
            if (
                outcome is None
                or not bool(outcome.get("reconnectable"))
                or not bool(outcome.get("internal_failure"))
            ):
                return None
            if dedupe_key is not None:
                inserted = await self._insert_event(
                    conn,
                    call_id=str(call["id"]),
                    provider_call_sid=sid,
                    provider_event_id=None,
                    dedupe_key=dedupe_key,
                    event_type="connect_action",
                    provider_occurred_at=None,
                    payload=payload or {},
                )
                if not inserted:
                    return call
            if count >= 2:
                return None
            count += 1
            await conn.execute(
                """
                UPDATE PHONE_CALLS
                SET reconnect_count=?, provider_stream_sid=NULL,
                    provider_session_id=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (count, call["id"]),
            )
            call["reconnect_count"] = count
            call["provider_stream_sid"] = None
            call["provider_session_id"] = None
            return call

    async def record_stream_attempt_result(
        self,
        *,
        call_id: str,
        provider_call_sid: str,
        stream_attempt: int,
        reason: str,
        reconnectable: bool,
        internal_failure: bool,
    ) -> bool:
        """Persist the session decision consumed by ``connect-action``."""

        sid = _require_sid(provider_call_sid, _CALL_SID, "CallSid")
        if stream_attempt not in {0, 1, 2}:
            raise ValueError("stream_attempt must be between 0 and 2")
        payload = {
            "stream_attempt": int(stream_attempt),
            "reason": str(reason)[:100],
            "reconnectable": bool(reconnectable),
            "internal_failure": bool(internal_failure),
        }
        if payload["reconnectable"] and not payload["internal_failure"]:
            raise ValueError("only an internal failure can be reconnectable")
        dedupe_key = f"stream-attempt:{call_id}:{stream_attempt}"
        async with self._write() as conn:
            cursor = await conn.execute(
                "SELECT provider_call_sid FROM PHONE_CALLS WHERE id=? AND deleted_at IS NULL",
                (str(call_id),),
            )
            row = await cursor.fetchone()
            if row is None or str(row[0]) != sid:
                raise TelephonyNotFoundError("Phone call not found")
            inserted = await self._insert_event(
                conn,
                call_id=str(call_id),
                provider_call_sid=sid,
                provider_event_id=None,
                dedupe_key=dedupe_key,
                event_type="stream_attempt_result",
                provider_occurred_at=None,
                payload=payload,
            )
            if inserted:
                return True
            existing = await self._stream_attempt_result_in_transaction(
                conn, call_id=str(call_id), stream_attempt=int(stream_attempt)
            )
            if existing != payload:
                raise TelephonyConflictError("Stream attempt result changed")
            return False

    async def get_stream_attempt_result(
        self, *, call_id: str, stream_attempt: int
    ) -> dict[str, Any] | None:
        async with self._connection_factory(readonly=True) as conn:
            return await self._stream_attempt_result_in_transaction(
                conn, call_id=str(call_id), stream_attempt=int(stream_attempt)
            )

    async def delivered_call_milestones(
        self,
        *,
        call_id: str,
        fencing_token: int,
        lease_owner: str,
    ) -> tuple[int, ...]:
        """Load warning thresholds already delivered to this call's LLM."""

        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT e.payload_json
                FROM PHONE_CALL_EVENTS e
                JOIN PHONE_CALLS c ON c.id=e.call_id
                JOIN PHONE_CONVERSATION_FOREGROUND f
                  ON f.conversation_id=c.conversation_id
                WHERE e.call_id=? AND e.event_type='clock_milestone_delivered'
                  AND f.current_call_id=c.id AND f.epoch=? AND f.lease_owner=?
                  AND f.lease_until>=?
                ORDER BY e.id
                """,
                (
                    str(call_id),
                    int(fencing_token),
                    str(lease_owner),
                    _utc_text(),
                ),
            )
            values: set[int] = set()
            for row in await cursor.fetchall():
                try:
                    payload = json.loads(str(row[0]))
                    values.add(int(payload["remaining_seconds_threshold"]))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise TelephonyStateError(
                        "Delivered phone clock milestone is invalid"
                    ) from exc
            return tuple(sorted(values, reverse=True))

    async def record_delivered_call_milestones(
        self,
        *,
        call_id: str,
        provider_call_sid: str,
        fencing_token: int,
        lease_owner: str,
        milestones_seconds: tuple[int, ...],
    ) -> None:
        """Durably dedupe milestones after their context reached the LLM."""

        sid = _require_sid(provider_call_sid, _CALL_SID, "CallSid")
        normalized = tuple(dict.fromkeys(int(value) for value in milestones_seconds))
        if any(value <= 0 for value in normalized):
            raise ValueError("call milestones must be positive")
        if not normalized:
            return
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                SELECT c.id
                FROM PHONE_CALLS c
                JOIN PHONE_CONVERSATION_FOREGROUND f
                  ON f.conversation_id=c.conversation_id
                WHERE c.id=? AND c.provider_call_sid=? AND c.deleted_at IS NULL
                  AND f.current_call_id=c.id AND f.epoch=? AND f.lease_owner=?
                  AND f.lease_until>=?
                """,
                (
                    str(call_id),
                    sid,
                    int(fencing_token),
                    str(lease_owner),
                    _utc_text(),
                ),
            )
            if await cursor.fetchone() is None:
                raise TelephonyStateError("Phone milestone foreground fence was lost")
            for milestone in normalized:
                await self._insert_event(
                    conn,
                    call_id=str(call_id),
                    provider_call_sid=sid,
                    provider_event_id=None,
                    dedupe_key=f"clock-milestone:{call_id}:{milestone}",
                    event_type="clock_milestone_delivered",
                    provider_occurred_at=None,
                    payload={"remaining_seconds_threshold": milestone},
                )

    async def _stream_attempt_result_in_transaction(
        self, conn: Any, *, call_id: str, stream_attempt: int
    ) -> dict[str, Any] | None:
        cursor = await conn.execute(
            """
            SELECT payload_json FROM PHONE_CALL_EVENTS
            WHERE call_id=? AND dedupe_key=? AND event_type='stream_attempt_result'
            """,
            (str(call_id), f"stream-attempt:{call_id}:{stream_attempt}"),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row[0]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TelephonyStateError("Stream attempt result is invalid") from exc
        return dict(payload) if isinstance(payload, Mapping) else None

    async def record_call_status(
        self,
        event: NormalizedCallStatus,
        *,
        dispatch_token: str | None = None,
        expected_direction: str | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Deduplicate and apply an ordered call status in one transaction."""

        if expected_direction not in {None, "inbound", "outbound"}:
            raise ValueError("expected_direction is invalid")

        async with self._write() as conn:
            if await self._callback_tombstoned(
                conn,
                provider_call_sid=event.call_sid,
                dispatch_token=dispatch_token,
            ):
                return None, False
            if dispatch_token:
                cursor = await conn.execute(
                    "SELECT * FROM PHONE_CALLS WHERE dispatch_token=? AND deleted_at IS NULL",
                    (dispatch_token,),
                )
            elif expected_direction is not None:
                cursor = await conn.execute(
                    """
                    SELECT * FROM PHONE_CALLS
                    WHERE provider_call_sid=? AND direction=? AND deleted_at IS NULL
                    """,
                    (event.call_sid, expected_direction),
                )
            else:
                cursor = await conn.execute(
                    "SELECT * FROM PHONE_CALLS WHERE provider_call_sid=? AND deleted_at IS NULL",
                    (event.call_sid,),
                )
            call = _row(await cursor.fetchone())
            inserted = await self._insert_event(
                conn,
                call_id=str(call["id"]) if call else None,
                provider_call_sid=event.call_sid,
                provider_event_id=event.provider_event_id,
                dedupe_key=event.dedupe_key,
                event_type="voice_status",
                provider_occurred_at=event.provider_occurred_at,
                payload=event.sanitized_payload,
            )
            if call is None or not inserted:
                return call, inserted
            existing_sid = call["provider_call_sid"]
            if existing_sid is not None and existing_sid != event.call_sid:
                raise TelephonyConflictError("Status callback belongs to another CallSid")
            target = event.status
            if answered_by_is_machine(event.answered_by):
                target = PhoneCallStatus.MACHINE
            hangup_reason: str | None = None
            if target in {
                PhoneCallStatus.COMPLETED,
                PhoneCallStatus.CANCELED,
            }:
                cursor = await conn.execute(
                    """
                    SELECT state,reason,target_status
                    FROM PHONE_HANGUP_ATTEMPTS WHERE call_id=?
                    """,
                    (str(call["id"]),),
                )
                hangup = await cursor.fetchone()
                if hangup is not None and str(hangup[0]) in {
                    "in_flight",
                    "unresolved",
                    "accepted",
                }:
                    target = PhoneCallStatus(str(hangup[2]))
                    hangup_reason = str(hangup[1])
            # An ambiguous hangup deliberately keeps foreground ownership.
            # A later signed terminal callback is the provider confirmation
            # that resolves it and may finally release the lease.
            hangup_unresolved = (
                str(call["status"]) == PhoneCallStatus.UNRESOLVED.value
                and str(call.get("termination_reason") or "").startswith(
                    "hangup_unresolved:"
                )
            )
            if hangup_unresolved and target in CALL_TERMINAL_STATUSES:
                decision = "apply"
            elif hangup_unresolved:
                decision = "noop"
            else:
                decision = call_transition_result(call["status"], target)
            if decision == "invalid":
                return call, inserted
            applied = call["status"] if decision == "noop" else target.value
            ended = target in CALL_TERMINAL_STATUSES and decision != "noop"
            terminal_reason = (
                hangup_reason
                or event.termination_reason
                or ("machine" if target == PhoneCallStatus.MACHINE else None)
            )
            try:
                await conn.execute(
                    """
                    UPDATE PHONE_CALLS
                    SET provider_call_sid=COALESCE(provider_call_sid,?), status=?,
                        answered_by=COALESCE(answered_by,?),
                        duration_seconds=COALESCE(?,duration_seconds),
                        termination_reason=CASE
                            WHEN ? AND status='unresolved'
                              AND termination_reason LIKE 'hangup_unresolved:%'
                            THEN COALESCE(?, 'provider_terminal_callback')
                            ELSE COALESCE(termination_reason,?) END,
                        initiated_at=CASE WHEN ?='initiated' THEN COALESCE(initiated_at,CURRENT_TIMESTAMP) ELSE initiated_at END,
                        ringing_at=CASE WHEN ?='ringing' THEN COALESCE(ringing_at,CURRENT_TIMESTAMP) ELSE ringing_at END,
                        answered_at=CASE WHEN ?='in_progress' THEN COALESCE(answered_at,CURRENT_TIMESTAMP) ELSE answered_at END,
                        ended_at=CASE WHEN ? THEN COALESCE(ended_at,CURRENT_TIMESTAMP) ELSE ended_at END,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (
                        event.call_sid,
                        applied,
                        event.answered_by,
                        event.duration_seconds,
                        int(ended),
                        terminal_reason,
                        terminal_reason,
                        applied,
                        applied,
                        applied,
                        int(ended),
                        call["id"],
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TelephonyConflictError("CallSid belongs to another call") from exc
            if ended:
                await self._release_foreground(conn, call)
            if call.get("job_id") is not None and (
                not hangup_unresolved or ended
            ):
                # Any compatible signed status callback reconciles a job that
                # was left for operator attention by an ambiguous REST result
                # or a provider-confirmed terminal hangup.  A nonterminal
                # callback cannot resolve an ambiguous hangup POST.
                await self._complete_reconciled_job(conn, str(call["job_id"]))
            if ended:
                await confirm_phone_hangup_in_transaction(
                    conn,
                    call_id=str(call["id"]),
                    attempt_token=None,
                )
            call["provider_call_sid"] = event.call_sid
            call["status"] = applied
            return call, inserted

    async def record_hangup_requested(
        self,
        *,
        call_id: str,
        provider_call_sid: str,
        reason: str,
        target_status: PhoneCallStatus,
        origin: str,
        retry_unresolved: bool = False,
    ) -> PhoneHangupAttemptClaim:
        """Claim one cross-origin, token-fenced provider hangup attempt."""

        sid = _require_sid(provider_call_sid, _CALL_SID, "CallSid")
        async with self._write() as conn:
            claim = await claim_phone_hangup_attempt_in_transaction(
                conn,
                call_id=str(call_id),
                provider_call_sid=sid,
                origin=str(origin),
                reason=str(reason),
                target_status=target_status,
                retry_unresolved=bool(retry_unresolved),
            )
            if claim.claimed:
                await self._insert_event(
                    conn,
                    call_id=str(call_id),
                    provider_call_sid=sid,
                    provider_event_id=None,
                    dedupe_key=(
                        f"hangup-requested:{call_id}:{claim.attempt_count}"
                    ),
                    event_type="hangup_requested",
                    provider_occurred_at=None,
                    payload={
                        "attempt": claim.attempt_count,
                        "origin": claim.origin,
                        "reason": claim.reason,
                        "target_status": claim.target_status,
                    },
                )
            return claim

    async def mark_hangup_unresolved(
        self,
        *,
        call_id: str,
        provider_call_sid: str,
        reason: str,
        attempt_token: str,
    ) -> bool:
        """Keep the foreground fenced while surfacing an ambiguous POST."""

        sid = _require_sid(provider_call_sid, _CALL_SID, "CallSid")
        normalized_reason = str(reason or "unknown")[:100]
        async with self._write() as conn:
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CALLS WHERE id=? AND deleted_at IS NULL",
                (str(call_id),),
            )
            call = _row(await cursor.fetchone())
            if call is None or str(call.get("provider_call_sid") or "") != sid:
                raise TelephonyNotFoundError("Phone call not found")
            unresolved = await mark_phone_hangup_unresolved_in_transaction(
                conn,
                call_id=str(call_id),
                attempt_token=str(attempt_token),
                error_code="provider_result_unknown",
                error_detail="Provider hangup could not be confirmed",
            )
            if not unresolved:
                return False
            if str(call["status"]) not in {
                status.value for status in CALL_TERMINAL_STATUSES
            } or str(call["status"]) == PhoneCallStatus.UNRESOLVED.value:
                await conn.execute(
                    """
                    UPDATE PHONE_CALLS
                    SET status='unresolved', termination_reason=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (f"hangup_unresolved:{normalized_reason}", str(call_id)),
                )
                if call.get("job_id") is not None:
                    await conn.execute(
                        """
                        UPDATE PHONE_CALL_JOBS
                        SET status='needs_attention',
                            last_error_code='hangup_unresolved',
                            last_error_detail='Provider hangup could not be confirmed',
                            lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                        """,
                        (str(call["job_id"]),),
                    )
            cursor = await conn.execute(
                "SELECT attempt_count FROM PHONE_HANGUP_ATTEMPTS WHERE call_id=?",
                (str(call_id),),
            )
            attempt_count = int((await cursor.fetchone())[0])
            await self._insert_event(
                conn,
                call_id=str(call_id),
                provider_call_sid=sid,
                provider_event_id=None,
                dedupe_key=f"hangup-unresolved:{call_id}:{attempt_count}",
                event_type="hangup_unresolved",
                provider_occurred_at=None,
                payload={"attempt": attempt_count, "reason": normalized_reason},
            )
            return True

    async def mark_hangup_accepted(
        self,
        *,
        call_id: str,
        provider_call_sid: str,
        attempt_token: str,
    ) -> bool:
        """Record REST acceptance without terminalizing call or foreground."""

        sid = _require_sid(provider_call_sid, _CALL_SID, "CallSid")
        async with self._write() as conn:
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CALLS WHERE id=? AND deleted_at IS NULL",
                (str(call_id),),
            )
            call = _row(await cursor.fetchone())
            if call is None or str(call.get("provider_call_sid") or "") != sid:
                raise TelephonyNotFoundError("Phone call not found")
            accepted = await mark_phone_hangup_accepted_in_transaction(
                conn,
                call_id=str(call_id),
                attempt_token=str(attempt_token),
            )
            if not accepted:
                return False
            cursor = await conn.execute(
                """
                SELECT attempt_count,origin,reason,target_status
                FROM PHONE_HANGUP_ATTEMPTS WHERE call_id=?
                """,
                (str(call_id),),
            )
            attempt = await cursor.fetchone()
            await self._insert_event(
                conn,
                call_id=str(call_id),
                provider_call_sid=sid,
                provider_event_id=None,
                dedupe_key=f"hangup-provider-accepted:{call_id}:{int(attempt[0])}",
                event_type="hangup_provider_accepted",
                provider_occurred_at=None,
                payload={
                    "attempt": int(attempt[0]),
                    "origin": str(attempt[1]),
                    "reason": str(attempt[2]),
                    "target_status": str(attempt[3]),
                },
            )
            return True

    async def get_hangup_attempt_state(
        self,
        *,
        call_id: str,
        provider_call_sid: str,
    ) -> str | None:
        """Read the shared latch state after a fenced result lost its token."""

        sid = _require_sid(provider_call_sid, _CALL_SID, "CallSid")
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT state FROM PHONE_HANGUP_ATTEMPTS
                WHERE call_id=? AND provider_call_sid=?
                """,
                (str(call_id), sid),
            )
            row = await cursor.fetchone()
            return str(row[0]) if row is not None else None

    async def get_hangup_attempt(
        self,
        *,
        call_id: str,
        provider_call_sid: str,
    ) -> dict[str, Any] | None:
        """Return the non-secret durable hangup scope for private playback."""

        sid = _require_sid(provider_call_sid, _CALL_SID, "CallSid")
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT state,reason,target_status,origin
                FROM PHONE_HANGUP_ATTEMPTS
                WHERE call_id=? AND provider_call_sid=?
                """,
                (str(call_id), sid),
            )
            return _row(await cursor.fetchone())

    async def reconcile_hangup_provider_absent(
        self,
        *,
        call_id: str,
        provider_call_sid: str,
        attempt_token: str,
    ) -> bool:
        """Finalize a fenced hangup after Twilio proves the call is absent."""

        sid = _require_sid(provider_call_sid, _CALL_SID, "CallSid")
        async with self._write() as conn:
            return await reconcile_phone_hangup_provider_absent_in_transaction(
                conn,
                call_id=str(call_id),
                provider_call_sid=sid,
                attempt_token=str(attempt_token),
            )

    async def record_stream_status(
        self, event: NormalizedStreamStatus
    ) -> tuple[dict[str, Any] | None, bool]:
        async with self._write() as conn:
            if await self._callback_tombstoned(
                conn, provider_call_sid=event.call_sid
            ):
                return None, False
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CALLS WHERE provider_call_sid=? AND deleted_at IS NULL",
                (event.call_sid,),
            )
            call = _row(await cursor.fetchone())
            inserted = await self._insert_event(
                conn,
                call_id=str(call["id"]) if call else None,
                provider_call_sid=event.call_sid,
                provider_event_id=event.provider_event_id,
                dedupe_key=event.dedupe_key,
                event_type="stream_status",
                provider_occurred_at=event.provider_occurred_at,
                payload=event.sanitized_payload,
            )
            return call, inserted

    async def record_recording_status(
        self,
        event: NormalizedRecordingStatus,
        *,
        dispatch_token: str | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        async with self._write() as conn:
            if await self._capture_late_recording_in_transaction(
                conn,
                provider_call_sid=event.call_sid,
                provider_recording_sid=event.recording_sid,
                dispatch_token=dispatch_token,
            ):
                return None, False
            if await self._callback_tombstoned(
                conn,
                provider_call_sid=event.call_sid,
                dispatch_token=dispatch_token,
            ):
                return None, False
            if dispatch_token is None:
                cursor = await conn.execute(
                    "SELECT * FROM PHONE_CALLS "
                    "WHERE provider_call_sid=? AND deleted_at IS NULL",
                    (event.call_sid,),
                )
            else:
                cursor = await conn.execute(
                    "SELECT * FROM PHONE_CALLS WHERE provider_call_sid=? "
                    "AND dispatch_token=? AND deleted_at IS NULL",
                    (event.call_sid, str(dispatch_token)),
                )
            call = _row(await cursor.fetchone())
            inserted = await self._insert_event(
                conn,
                call_id=str(call["id"]) if call else None,
                provider_call_sid=event.call_sid,
                provider_event_id=event.provider_event_id,
                dedupe_key=event.dedupe_key,
                event_type="recording_status",
                provider_occurred_at=event.provider_occurred_at,
                payload=event.sanitized_payload,
            )
            if call is not None and inserted:
                mapped = {
                    "in-progress": "pending",
                    "completed": "available",
                    "absent": "failed",
                    "failed": "failed",
                }[event.status]
                await conn.execute(
                    """
                    INSERT INTO PHONE_RECORDINGS (
                        call_id, provider_recording_sid, status, duration_seconds,
                        last_error
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(provider_recording_sid) DO UPDATE SET
                        status=excluded.status,
                        duration_seconds=COALESCE(excluded.duration_seconds,duration_seconds),
                        last_error=excluded.last_error,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        call["id"],
                        event.recording_sid,
                        mapped,
                        event.duration_seconds,
                        "provider_recording_failed" if mapped == "failed" else None,
                    ),
                )
            return call, inserted

    async def _capture_late_recording_in_transaction(
        self,
        conn: Any,
        *,
        provider_call_sid: str,
        provider_recording_sid: str,
        dispatch_token: str | None,
    ) -> bool:
        """Fence recording-only and whole-call tombstones with callback writes."""

        required_tables = {
            "PHONE_CALL_TOMBSTONES",
            "PHONE_RECORDING_TOMBSTONES",
            "PHONE_DATA_PURGE_JOBS",
        }
        placeholders = ",".join("?" for _ in required_tables)
        cursor = await conn.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' "
            f"AND name IN ({placeholders})",
            tuple(sorted(required_tables)),
        )
        if {str(row[0]) for row in await cursor.fetchall()} != required_tables:
            return False
        cursor = await conn.execute(
            """
            SELECT t.purge_job_id
            FROM PHONE_CALL_TOMBSTONES t
            WHERE t.provider_call_sid=?
              AND (? IS NULL OR t.dispatch_token=?)
            UNION ALL
            SELECT r.purge_job_id
            FROM PHONE_RECORDING_TOMBSTONES r
            JOIN PHONE_CALLS c ON c.id=r.call_id_snapshot
            WHERE c.provider_call_sid=? AND c.deleted_at IS NULL
              AND (? IS NULL OR c.dispatch_token=?)
            LIMIT 1
            """,
            (
                str(provider_call_sid),
                dispatch_token,
                dispatch_token,
                str(provider_call_sid),
                dispatch_token,
                dispatch_token,
            ),
        )
        tombstone = await cursor.fetchone()
        if tombstone is None:
            return False
        job_id = str(tombstone[0])
        cursor = await conn.execute(
            "SELECT source_snapshot_json FROM PHONE_DATA_PURGE_JOBS WHERE id=?",
            (job_id,),
        )
        job = await cursor.fetchone()
        if job is None:
            raise TelephonyStateError("Late recording purge job is missing")
        try:
            source = json.loads(str(job[0] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TelephonyStateError("Late recording purge snapshot is invalid") from exc
        if not isinstance(source, dict):
            raise TelephonyStateError("Late recording purge snapshot is invalid")
        recordings = source.setdefault("recordings", [])
        if not isinstance(recordings, list):
            raise TelephonyStateError("Late recording purge snapshot is invalid")
        if any(
            isinstance(item, Mapping)
            and str(item.get("provider_recording_sid"))
            == str(provider_recording_sid)
            for item in recordings
        ):
            return True
        recordings.append(
            {
                "recording_id": None,
                "provider_recording_sid": str(provider_recording_sid),
                "participant_path": None,
                "assistant_path": None,
                "mixed_path": None,
            }
        )
        cursor = await conn.execute(
            """
            UPDATE PHONE_DATA_PURGE_JOBS
            SET source_snapshot_json=?,source_revision=source_revision+1,
                status=CASE WHEN status='completed' THEN 'scheduled' ELSE status END,
                completed_at=CASE WHEN status='completed' THEN NULL ELSE completed_at END,
                next_attempt_at=CASE WHEN status='completed' THEN NULL ELSE next_attempt_at END,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status IN ('completed','needs_attention','scheduled','running')
            """,
            (_json(source), job_id),
        )
        if cursor.rowcount != 1:
            raise TelephonyStateError("Late recording purge state changed")
        return True

    async def persist_local_recording(
        self,
        *,
        call_id: str,
        participant_path: str | None,
        assistant_path: str | None,
        mixed_path: str | None,
        duration_seconds: int,
        mix_error: str | None,
    ) -> int:
        async with self._write() as conn:
            cursor = await conn.execute(
                "SELECT recording_enabled FROM PHONE_CALLS WHERE id=? AND deleted_at IS NULL",
                (call_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise TelephonyNotFoundError("Phone call not found")
            if not bool(row[0]):
                raise TelephonyStateError("Phone recording is not enabled")
            cursor = await conn.execute(
                """
                SELECT id,mixed_path,last_error FROM PHONE_RECORDINGS
                WHERE call_id=? AND provider_recording_sid IS NULL
                ORDER BY id LIMIT 1
                """,
                (call_id,),
            )
            existing = await cursor.fetchone()
            if existing is None:
                cursor = await conn.execute(
                    """
                    INSERT INTO PHONE_RECORDINGS (
                        call_id, status, participant_path, assistant_path, mixed_path,
                        duration_seconds, last_error
                    ) VALUES (?, 'available', ?, ?, ?, ?, ?)
                    """,
                    (
                        call_id,
                        participant_path,
                        assistant_path,
                        mixed_path,
                        int(duration_seconds),
                        mix_error,
                    ),
                )
                return int(cursor.lastrowid)

            recording_id = int(existing[0])
            existing_mixed = str(existing[1]) if existing[1] is not None else None
            resolved_mixed = mixed_path or existing_mixed
            if mixed_path is not None:
                resolved_error = mix_error
            elif existing_mixed is not None:
                resolved_error = existing[2]
            else:
                resolved_error = mix_error
            await conn.execute(
                """
                UPDATE PHONE_RECORDINGS
                SET status='available',
                    participant_path=COALESCE(?,participant_path),
                    assistant_path=COALESCE(?,assistant_path),
                    mixed_path=?,
                    duration_seconds=MAX(COALESCE(duration_seconds,0),?),
                    last_error=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    participant_path,
                    assistant_path,
                    resolved_mixed,
                    int(duration_seconds),
                    resolved_error,
                    recording_id,
                ),
            )
            return recording_id

    async def count_caller_turns(
        self,
        *,
        call_id: str,
        fencing_token: int,
        lease_owner: str,
    ) -> int:
        """Return the durable call-wide sequence used across reconnects."""

        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT COUNT(*)
                FROM PHONE_CALL_MESSAGE_LINKS l
                JOIN PHONE_CALLS c ON c.id=l.call_id
                JOIN PHONE_CONVERSATION_FOREGROUND f
                  ON f.conversation_id=c.conversation_id
                WHERE l.call_id=? AND l.participant='caller'
                  AND f.current_call_id=c.id AND f.epoch=? AND f.lease_owner=?
                  AND f.lease_until>=?
                """,
                (
                    str(call_id),
                    int(fencing_token),
                    str(lease_owner),
                    _utc_text(),
                ),
            )
            return int((await cursor.fetchone())[0])

    async def previous_greeting_id(
        self,
        *,
        call_id: str,
        contact_id: int,
        direction: str,
    ) -> int | None:
        """Load the last actually persisted greeting for no-repeat selection."""

        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT e.payload_json
                FROM PHONE_CALL_EVENTS e
                JOIN PHONE_CALLS c ON c.id=e.call_id
                WHERE c.contact_id=? AND c.direction=? AND c.id<>?
                  AND e.event_type='greeting_persisted'
                ORDER BY e.id DESC LIMIT 1
                """,
                (int(contact_id), str(direction), str(call_id)),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            try:
                value = json.loads(str(row[0])).get("greeting_id")
                return int(value) if value is not None else None
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                return None

    async def pin_call_audio_revision(
        self,
        *,
        call_id: str,
        provider_call_sid: str,
        audio_revision: int,
    ) -> int:
        """Pin one prompt cache revision durably for every stream attempt."""

        sid = _require_sid(provider_call_sid, _CALL_SID, "CallSid")
        revision = int(audio_revision)
        if revision <= 0:
            raise ValueError("audio_revision must be positive")
        key = f"audio-revision:{call_id}"
        async with self._write() as conn:
            cursor = await conn.execute(
                "SELECT provider_call_sid FROM PHONE_CALLS WHERE id=? AND deleted_at IS NULL",
                (str(call_id),),
            )
            row = await cursor.fetchone()
            if row is None or str(row[0]) != sid:
                raise TelephonyNotFoundError("Phone call not found")
            await self._insert_event(
                conn,
                call_id=str(call_id),
                provider_call_sid=sid,
                provider_event_id=None,
                dedupe_key=key,
                event_type="audio_revision_pinned",
                provider_occurred_at=None,
                payload={"audio_revision": revision},
            )
            cursor = await conn.execute(
                "SELECT payload_json FROM PHONE_CALL_EVENTS WHERE dedupe_key=?",
                (key,),
            )
            recorded = json.loads(str((await cursor.fetchone())[0]))
            if int(recorded.get("audio_revision") or 0) != revision:
                raise TelephonyConflictError("Call audio revision changed")
            return revision

    async def get_call_audio_revision(self, *, call_id: str) -> int | None:
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT payload_json FROM PHONE_CALL_EVENTS
                WHERE call_id=? AND event_type='audio_revision_pinned'
                ORDER BY id LIMIT 1
                """,
                (str(call_id),),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            try:
                revision = int(json.loads(str(row[0]))["audio_revision"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise TelephonyStateError("Call audio revision is invalid") from exc
            return revision if revision > 0 else None

    async def persist_greeting_prefix(
        self,
        *,
        call_id: str,
        greeting_id: int,
        confirmed_text: str,
        played_ms: int,
        interrupted: bool,
        fencing_token: int,
        lease_owner: str,
    ) -> int | None:
        """Persist the audible greeting prefix as the first assistant turn."""

        text = str(confirmed_text)
        duration = int(played_ms)
        if not text.strip() or duration <= 0:
            return None
        memory_values: dict[str, Any] | None = None
        message_id: int
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                SELECT c.* FROM PHONE_CALLS c
                JOIN PHONE_CONVERSATION_FOREGROUND f
                  ON f.conversation_id=c.conversation_id
                WHERE c.id=? AND c.foreground_fencing_token=?
                  AND c.foreground_lease_owner=?
                  AND f.current_call_id=c.id AND f.epoch=? AND f.lease_owner=?
                  AND f.lease_until>=? AND c.deleted_at IS NULL
                """,
                (
                    str(call_id),
                    int(fencing_token),
                    str(lease_owner),
                    int(fencing_token),
                    str(lease_owner),
                    _utc_text(),
                ),
            )
            call = _row(await cursor.fetchone())
            if call is None:
                raise TelephonyStateError("Phone greeting foreground fence is stale")
            cursor = await conn.execute(
                """
                SELECT l.message_id,m.message,l.played_ms,l.confirmed_text,l.interrupted
                FROM PHONE_CALL_MESSAGE_LINKS l
                JOIN MESSAGES m ON m.id=l.message_id
                WHERE l.call_id=? AND l.turn_id='greeting'
                  AND l.participant='assistant'
                """,
                (str(call_id),),
            )
            existing = await cursor.fetchone()
            expected = (text, duration, text, int(bool(interrupted)))
            if existing is not None:
                actual = (
                    str(existing[1]),
                    int(existing[2]),
                    str(existing[3]),
                    int(existing[4]),
                )
                if actual != expected:
                    raise TelephonyConflictError("Greeting was persisted incompatibly")
                message_id = int(existing[0])
            else:
                # The opening task gates canonical caller turns, so any linked
                # assistant here is necessarily the first assistant message
                # for this call. The UNIQUE turn/participant index is the CAS.
                cursor = await conn.execute(
                    """
                    INSERT INTO MESSAGES(conversation_id,user_id,message,type)
                    VALUES(?,?,?,'bot')
                    """,
                    (
                        int(call["conversation_id"]),
                        int(call["owner_user_id"]),
                        text,
                    ),
                )
                message_id = int(cursor.lastrowid)
                await conn.execute(
                    """
                    INSERT INTO PHONE_CALL_MESSAGE_LINKS(
                        call_id,message_id,participant,turn_id,origin_channel,
                        interrupted,played_ms,confirmed_text,delivery_state
                    ) VALUES(?,?,'assistant','greeting','phone',?,?,?,'consumed')
                    """,
                    (
                        str(call_id),
                        message_id,
                        int(bool(interrupted)),
                        duration,
                        text,
                    ),
                )
                await self._insert_event(
                    conn,
                    call_id=str(call_id),
                    provider_call_sid=str(call["provider_call_sid"]),
                    provider_event_id=None,
                    dedupe_key=f"greeting-persisted:{call_id}",
                    event_type="greeting_persisted",
                    provider_occurred_at=None,
                    payload={"greeting_id": int(greeting_id)},
                )
                memory_values = {
                    "user_id": int(call["owner_user_id"]),
                    "conversation_id": int(call["conversation_id"]),
                    "assistant_content": text,
                    "prompt_id": _snapshot_prompt_id(
                        call.get("config_snapshot_json")
                    ),
                    "assistant_message_id": message_id,
                }

        # Memory providers may perform network I/O or open their own SQLite
        # connections. Never keep the phone write transaction open here.
        if memory_values is not None:
            try:
                await self._memory_turn_recorder(**memory_values)
            except Exception:
                logger.warning(
                    "Phone greeting memory hook failed for call_id=%s",
                    call_id,
                    exc_info=True,
                )
        return message_id

    async def append_provider_event(
        self,
        *,
        call_id: str,
        provider_call_sid: str,
        dedupe_key: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> bool:
        """Append one already-authenticated, sanitized provider event."""

        async with self._write() as conn:
            cursor = await conn.execute(
                "SELECT provider_call_sid FROM PHONE_CALLS WHERE id=? AND deleted_at IS NULL",
                (call_id,),
            )
            row = await cursor.fetchone()
            if row is None or row[0] != provider_call_sid:
                raise TelephonyNotFoundError("Phone call not found")
            return await self._insert_event(
                conn,
                call_id=call_id,
                provider_call_sid=provider_call_sid,
                provider_event_id=None,
                dedupe_key=dedupe_key,
                event_type=str(event_type)[:100],
                provider_occurred_at=None,
                payload=payload,
            )

    async def mark_terminal(
        self,
        *,
        call_id: str,
        status: PhoneCallStatus,
        reason: str,
    ) -> bool:
        if status not in CALL_TERMINAL_STATUSES:
            raise ValueError("terminal status is required")
        async with self._write() as conn:
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CALLS WHERE id=? AND deleted_at IS NULL",
                (call_id,),
            )
            call = _row(await cursor.fetchone())
            if call is None:
                return False
            decision = call_transition_result(call["status"], status)
            if decision != "apply":
                return False
            await conn.execute(
                """
                UPDATE PHONE_CALLS SET status=?, termination_reason=COALESCE(termination_reason,?),
                    ended_at=COALESCE(ended_at,CURRENT_TIMESTAMP), updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (status.value, str(reason)[:500], call_id),
            )
            await self._release_foreground(conn, call)
            return True

    async def _insert_event(
        self,
        conn: Any,
        *,
        call_id: str | None,
        provider_call_sid: str,
        provider_event_id: str | None,
        dedupe_key: str,
        event_type: str,
        provider_occurred_at: str | None,
        payload: Mapping[str, Any],
    ) -> bool:
        # Provider callbacks without a live call are deliberately ignored.  In
        # particular, a late callback after purge must not recreate durable
        # metadata with a NULL call_id.
        if call_id is None:
            return False
        cursor = await conn.execute(
            """
            INSERT OR IGNORE INTO PHONE_CALL_EVENTS (
                call_id, provider_call_sid, provider_event_id, dedupe_key,
                event_type, signature_valid, provider_occurred_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                call_id,
                provider_call_sid,
                provider_event_id,
                dedupe_key,
                event_type,
                provider_occurred_at,
                _json(payload),
            ),
        )
        return cursor.rowcount == 1

    async def _callback_tombstoned(
        self,
        conn: Any,
        *,
        provider_call_sid: str,
        dispatch_token: str | None = None,
    ) -> bool:
        """Check callback tombstones under the caller's mutation transaction."""

        cursor = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='PHONE_CALL_TOMBSTONES'"
        )
        if await cursor.fetchone() is None:
            return False
        if dispatch_token:
            cursor = await conn.execute(
                """
                SELECT 1 FROM PHONE_CALL_TOMBSTONES
                WHERE provider_call_sid=? OR dispatch_token=? LIMIT 1
                """,
                (str(provider_call_sid), str(dispatch_token)),
            )
        else:
            cursor = await conn.execute(
                "SELECT 1 FROM PHONE_CALL_TOMBSTONES "
                "WHERE provider_call_sid=? LIMIT 1",
                (str(provider_call_sid),),
            )
        return await cursor.fetchone() is not None

    async def _release_foreground(self, conn: Any, call: Mapping[str, Any]) -> None:
        conversation_id = int(call["conversation_id"])
        call_id = str(call["id"])
        await conn.execute(
            """
            UPDATE PHONE_CONVERSATION_FOREGROUND
            SET epoch=epoch+1, current_call_id=NULL, lease_owner=NULL,
                lease_until=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE conversation_id=? AND current_call_id=?
            """,
            (conversation_id, call_id),
        )
        await conn.execute(
            """
            UPDATE PHONE_CALLS SET foreground_lease_owner=NULL,
                foreground_lease_until=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (call_id,),
        )

    async def _complete_reconciled_job(self, conn: Any, job_id: str) -> None:
        await conn.execute(
            """
            UPDATE PHONE_CALL_JOBS
            SET status='completed', completed_at=COALESCE(completed_at,CURRENT_TIMESTAMP),
                last_error_code=NULL, last_error_detail=NULL,
                lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='needs_attention'
            """,
            (str(job_id),),
        )

__all__ = ["TelephonyProviderRepository"]
