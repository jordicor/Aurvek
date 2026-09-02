"""Durable, fenced deletion of telephone calls and private recordings."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
import socket
from typing import Any
from uuid import uuid4

import aiosqlite

import database
from chat.services.locks import conversation_write_lock
from integrations.telephony.recording import DEFAULT_RECORDING_ROOT
from integrations.telephony.recording_storage import delete_private_call_audio
from integrations.telephony.purge_state import (
    is_conversation_memory_blocked,
    phone_data_purge_runtime_operational,
)
from integrations.telephony.repository import (
    TelephonyConflictError,
    TelephonyNotFoundError,
    TelephonyStateError,
)
from integrations.telephony.twilio_client import AsyncTwilioVoiceClient


_DELETABLE_CALL_STATUSES = {
    "completed",
    "busy",
    "no_answer",
    "machine",
    "failed",
    "canceled",
}
_PURGE_SCHEMA_TABLES = {
    "PHONE_DATA_PURGE_JOBS",
    "PHONE_CALL_TOMBSTONES",
    "PHONE_RECORDING_TOMBSTONES",
    "PHONE_CONVERSATION_DATA_REVISIONS",
    "PHONE_DATA_PURGE_RUNTIME",
    "PHONE_MEMORY_OPERATION_LEASES",
    "PHONE_PURGED_MESSAGE_TOMBSTONES",
}
_PURGE_REQUIRED_COLUMNS = {
    "PHONE_DATA_PURGE_JOBS": {
        "progress_json",
        "next_attempt_at",
        "runtime_lease_token",
        "content_revision_snapshot",
        "source_revision",
    },
    "PHONE_CONVERSATION_DATA_REVISIONS": {"revision", "content_revision"},
    "PHONE_MEMORY_OPERATION_LEASES": {
        "lease_token",
        "provider_started",
        "status",
        "lease_until",
    },
    "PHONE_PURGED_MESSAGE_TOMBSTONES": {"message_id", "purge_job_id"},
}
_PURGE_REQUIRED_TRIGGERS = {
    "trg_phone_content_revision_message_insert",
    "trg_phone_content_revision_message_update",
    "trg_phone_content_revision_message_delete",
}


def _utc_text(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _loads(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _row(value: Any) -> dict[str, Any] | None:
    return dict(value) if value is not None else None


class PhoneDataPurgeFailure(RuntimeError):
    """A deletion phase failed and must remain durably visible."""


class PhoneDataPurgeAmbiguous(PhoneDataPurgeFailure):
    """Provider I/O may have succeeded and must not be hidden as completion."""


class PhoneDataPurgeSnapshotChanged(PhoneDataPurgeFailure):
    """Conversation content or late provider assets changed under a fenced job."""


@dataclass(frozen=True, slots=True)
class PurgeRequest:
    job: dict[str, Any] | None
    created: bool
    already_deleted: bool = False


class PhoneDataPurgeRepository:
    """SQLite state machine for deletion requests, leases and tombstones."""

    def __init__(
        self,
        connection_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._connection_factory = connection_factory or database.get_db_connection

    @asynccontextmanager
    async def _write(self):
        async with self._connection_factory() as conn:
            conn.row_factory = aiosqlite.Row
            try:
                await conn.execute("BEGIN IMMEDIATE")
                yield conn
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def schema_ready(self, conn: Any | None = None) -> bool:
        async def check(active: Any) -> bool:
            placeholders = ",".join("?" for _ in _PURGE_SCHEMA_TABLES)
            cursor = await active.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' "
                f"AND name IN ({placeholders})",
                tuple(sorted(_PURGE_SCHEMA_TABLES)),
            )
            tables = {str(row[0]) for row in await cursor.fetchall()}
            if tables != _PURGE_SCHEMA_TABLES:
                return False
            for table, required in _PURGE_REQUIRED_COLUMNS.items():
                cursor = await active.execute(f"PRAGMA table_info({table})")
                columns = {str(row[1]) for row in await cursor.fetchall()}
                if not required.issubset(columns):
                    return False
            cursor = await active.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
            triggers = {str(row[0]) for row in await cursor.fetchall()}
            required_triggers = set(_PURGE_REQUIRED_TRIGGERS)
            for table, names in {
                "WATCHDOG_EVENTS": {
                    "trg_phone_block_purged_watchdog_event_insert",
                    "trg_phone_block_purged_watchdog_event_update",
                },
                "WATCHDOG_STATE": {
                    "trg_phone_clear_purged_watchdog_state_insert",
                    "trg_phone_clear_purged_watchdog_state_update",
                },
            }.items():
                cursor = await active.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                )
                if await cursor.fetchone() is not None:
                    required_triggers.update(names)
            return required_triggers.issubset(triggers)

        if conn is not None:
            return await check(conn)
        async with self._connection_factory(readonly=True) as active:
            return await check(active)

    async def request_owned_call_purge(
        self,
        *,
        owner_user_id: int,
        call_id: str,
    ) -> PurgeRequest:
        async with self._write() as conn:
            await self._require_schema(conn)
            tombstone = await self._call_tombstone(conn, call_id, owner_user_id)
            if tombstone is not None:
                return PurgeRequest(
                    await self._job(conn, str(tombstone["purge_job_id"])),
                    False,
                    True,
                )
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CALLS WHERE id=? AND owner_user_id=?",
                (str(call_id), int(owner_user_id)),
            )
            call = _row(await cursor.fetchone())
            if call is None:
                raise TelephonyNotFoundError("Phone call not found")
            if str(call["status"]) not in _DELETABLE_CALL_STATUSES:
                raise TelephonyStateError("Only a terminal phone call can be deleted")
            job = await self._stage_call_job(conn, call, conversation_deleted=False)
            return PurgeRequest(job, True)

    async def owned_call_conversation_id(
        self,
        *,
        owner_user_id: int,
        call_id: str,
    ) -> int:
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT conversation_id FROM PHONE_CALLS
                WHERE id=? AND owner_user_id=?
                UNION ALL
                SELECT conversation_id_snapshot FROM PHONE_CALL_TOMBSTONES
                WHERE call_id=? AND owner_user_id_snapshot=?
                LIMIT 1
                """,
                (str(call_id), int(owner_user_id), str(call_id), int(owner_user_id)),
            )
            row = await cursor.fetchone()
            if row is None:
                raise TelephonyNotFoundError("Phone call not found")
            return int(row[0])

    async def request_owned_recording_purge(
        self,
        *,
        owner_user_id: int,
        call_id: str,
    ) -> PurgeRequest:
        async with self._write() as conn:
            await self._require_schema(conn)
            call_tombstone = await self._call_tombstone(conn, call_id, owner_user_id)
            if call_tombstone is not None:
                return PurgeRequest(
                    await self._job(conn, str(call_tombstone["purge_job_id"])),
                    False,
                    True,
                )
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CALLS WHERE id=? AND owner_user_id=? "
                "AND deleted_at IS NULL",
                (str(call_id), int(owner_user_id)),
            )
            call = _row(await cursor.fetchone())
            if call is None:
                raise TelephonyNotFoundError("Phone call not found")
            if str(call["status"]) not in _DELETABLE_CALL_STATUSES:
                raise TelephonyStateError(
                    "Only a terminal phone call recording can be deleted"
                )
            cursor = await conn.execute(
                "SELECT * FROM PHONE_RECORDING_TOMBSTONES "
                "WHERE call_id_snapshot=? AND owner_user_id_snapshot=?",
                (str(call_id), int(owner_user_id)),
            )
            tombstone = _row(await cursor.fetchone())
            if tombstone is not None:
                return PurgeRequest(
                    await self._job(conn, str(tombstone["purge_job_id"])),
                    False,
                    True,
                )
            recordings = await self._recordings(conn, str(call_id))
            if not recordings:
                return PurgeRequest(None, False, True)
            source = await self._source_snapshot(
                conn,
                call,
                recordings=recordings,
                conversation_deleted=False,
            )
            job_id = uuid4().hex
            selected_id = int(recordings[0]["recording_id"])
            cursor = await conn.execute(
                """
                INSERT INTO PHONE_DATA_PURGE_JOBS (
                    id,owner_user_id,conversation_id,call_id,recording_id,
                    owner_user_id_snapshot,conversation_id_snapshot,
                    call_id_snapshot,recording_id_snapshot,purge_scope,
                    conversation_revision,provider_call_sid_snapshot,
                    provider_recording_sid_snapshot,source_snapshot_json,
                    progress_json,next_attempt_at
                ) VALUES (?,?,?,?,?,?,?,?,?,'recording',1,?,?,?,?,NULL)
                RETURNING *
                """,
                (
                    job_id,
                    int(call["owner_user_id"]),
                    int(call["conversation_id"]),
                    str(call["id"]),
                    selected_id,
                    int(call["owner_user_id"]),
                    int(call["conversation_id"]),
                    str(call["id"]),
                    selected_id,
                    call.get("provider_call_sid"),
                    recordings[0].get("provider_recording_sid"),
                    _json(source),
                    "{}",
                ),
            )
            job = dict(await cursor.fetchone())
            await conn.execute(
                """
                INSERT INTO PHONE_RECORDING_TOMBSTONES (
                    call_id_snapshot,owner_user_id_snapshot,
                    conversation_id_snapshot,purge_job_id
                ) VALUES (?,?,?,?)
                """,
                (
                    str(call["id"]),
                    int(call["owner_user_id"]),
                    int(call["conversation_id"]),
                    job_id,
                ),
            )
            await conn.execute(
                "UPDATE PHONE_RECORDINGS SET status='deleting', "
                "updated_at=CURRENT_TIMESTAMP WHERE call_id=?",
                (str(call["id"]),),
            )
            return PurgeRequest(job, True)

    async def stage_conversation_purges_in_transaction(
        self,
        conn: Any,
        *,
        conversation_id: int,
        owner_user_id: int | None = None,
    ) -> list[str]:
        """Capture every external/local phone asset before conversation cascades."""

        if not await self.schema_ready(conn):
            cursor = await conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='PHONE_CALLS'"
            )
            if await cursor.fetchone() is None:
                return []
            cursor = await conn.execute(
                "SELECT 1 FROM PHONE_CALLS WHERE conversation_id=? LIMIT 1",
                (int(conversation_id),),
            )
            if await cursor.fetchone() is not None:
                raise PhoneDataPurgeFailure(
                    "phone data purge schema is incomplete for existing call assets"
                )
            return []
        params: list[Any] = [int(conversation_id)]
        owner_filter = ""
        if owner_user_id is not None:
            owner_filter = " AND owner_user_id=?"
            params.append(int(owner_user_id))
        cursor = await conn.execute(
            "SELECT * FROM PHONE_CALLS WHERE conversation_id=?" + owner_filter,
            tuple(params),
        )
        calls = [dict(raw) for raw in await cursor.fetchall()]
        if any(str(call["status"]) not in _DELETABLE_CALL_STATUSES for call in calls):
            raise TelephonyConflictError(
                "An active or unresolved phone call prevents conversation deletion"
            )
        jobs: list[str] = []
        seen_jobs: set[str] = set()
        tombstone_params: list[Any] = [int(conversation_id)]
        tombstone_owner_filter = ""
        if owner_user_id is not None:
            tombstone_owner_filter = " AND owner_user_id_snapshot=?"
            tombstone_params.append(int(owner_user_id))
        cursor = await conn.execute(
            "SELECT purge_job_id FROM PHONE_CALL_TOMBSTONES "
            "WHERE conversation_id_snapshot=?" + tombstone_owner_filter,
            tuple(tombstone_params),
        )
        for row in await cursor.fetchall():
            job_id = str(row[0])
            await self._mark_job_conversation_deleted(conn, job_id)
            jobs.append(job_id)
            seen_jobs.add(job_id)
        for call in calls:
            tombstone = await self._call_tombstone(
                conn, str(call["id"]), int(call["owner_user_id"])
            )
            if tombstone is not None:
                job_id = str(tombstone["purge_job_id"])
                if job_id not in seen_jobs:
                    await self._mark_job_conversation_deleted(conn, job_id)
                    jobs.append(job_id)
                    seen_jobs.add(job_id)
                continue
            job = await self._stage_call_job(conn, call, conversation_deleted=True)
            jobs.append(str(job["id"]))
        return jobs

    async def get_owned_recording(
        self,
        *,
        owner_user_id: int,
        call_id: str,
        track: str,
    ) -> dict[str, Any]:
        column = {
            "mixed": "mixed_path",
            "participant": "participant_path",
            "assistant": "assistant_path",
        }.get(str(track))
        if column is None:
            raise ValueError("recording track is invalid")
        async with self._connection_factory(readonly=True) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                f"""
                SELECT r.id,r.call_id,r.status,r.duration_seconds,r.{column} AS path,
                       c.conversation_id
                FROM PHONE_RECORDINGS r
                JOIN PHONE_CALLS c ON c.id=r.call_id
                WHERE r.call_id=? AND c.owner_user_id=? AND c.deleted_at IS NULL
                  AND r.status='available' AND r.{column} IS NOT NULL
                ORDER BY r.id DESC LIMIT 1
                """,
                (str(call_id), int(owner_user_id)),
            )
            row = _row(await cursor.fetchone())
            if row is None:
                raise TelephonyNotFoundError("Phone recording not found")
            return row

    async def claim_next(
        self,
        *,
        lease_owner: str,
        lease_seconds: float,
        runtime_lease_token: str,
    ) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        now_text = _utc_text(now)
        lease_until = _utc_text(now + timedelta(seconds=float(lease_seconds)))
        token = uuid4().hex
        async with self._write() as conn:
            await self._require_schema(conn)
            if not await self._runtime_lease_valid(
                conn, lease_owner, runtime_lease_token, now_text
            ):
                raise PhoneDataPurgeFailure("phone data purge runtime lease was lost")
            cursor = await conn.execute(
                """
                SELECT * FROM PHONE_DATA_PURGE_JOBS j
                WHERE (
                    (j.status='scheduled' AND
                        (j.next_attempt_at IS NULL OR j.next_attempt_at<=?))
                    OR (j.status='running' AND
                        (j.lease_until IS NULL OR j.lease_until<?))
                )
                AND NOT EXISTS (
                    SELECT 1 FROM PHONE_CONVERSATION_DATA_REVISIONS r
                    WHERE r.conversation_id_snapshot=j.conversation_id_snapshot
                      AND r.memory_blocked=1 AND r.active_job_id<>j.id
                )
                ORDER BY j.created_at,j.id LIMIT 1
                """,
                (now_text, now_text),
            )
            job = _row(await cursor.fetchone())
            if job is None:
                return None
            if not await self._memory_leases_quiescent(conn, job, now_text):
                return None
            cursor = await conn.execute(
                """
                UPDATE PHONE_DATA_PURGE_JOBS
                SET status='running',lease_owner=?,lease_token=?,lease_until=?,
                    runtime_lease_token=?,
                    attempt_count=attempt_count+1,last_error=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND (
                    status='scheduled' OR
                    (status='running' AND (lease_until IS NULL OR lease_until<?))
                )
                RETURNING *
                """,
                (
                    lease_owner,
                    token,
                    lease_until,
                    runtime_lease_token,
                    job["id"],
                    now_text,
                ),
            )
            claimed = _row(await cursor.fetchone())
            if claimed is None:
                return None
            source = _loads(claimed.get("source_snapshot_json"))
            if claimed["purge_scope"] == "call" and not source.get(
                "conversation_deleted", False
            ):
                revision, content_revision = await self._acquire_conversation_revision(
                    conn,
                    job_id=str(claimed["id"]),
                    owner_user_id=int(claimed["owner_user_id_snapshot"]),
                    conversation_id=int(claimed["conversation_id_snapshot"]),
                    lease_owner=lease_owner,
                    lease_token=token,
                    lease_until=lease_until,
                    now_text=now_text,
                )
                await conn.execute(
                    "UPDATE PHONE_DATA_PURGE_JOBS SET conversation_revision=?,"
                    "content_revision_snapshot=? WHERE id=?",
                    (revision, content_revision, str(claimed["id"])),
                )
                claimed["conversation_revision"] = revision
                claimed["content_revision_snapshot"] = content_revision
            return claimed

    async def renew_job(
        self,
        *,
        job_id: str,
        lease_owner: str,
        lease_token: str,
        runtime_lease_token: str,
        lease_seconds: float,
    ) -> bool:
        lease_until = _utc_text(
            datetime.now(UTC) + timedelta(seconds=float(lease_seconds))
        )
        async with self._write() as conn:
            now_text = _utc_text()
            if not await self._runtime_lease_valid(
                conn, lease_owner, runtime_lease_token, now_text
            ):
                return False
            cursor = await conn.execute(
                "SELECT purge_scope,source_snapshot_json FROM PHONE_DATA_PURGE_JOBS "
                "WHERE id=?",
                (str(job_id),),
            )
            job_row = await cursor.fetchone()
            if job_row is None:
                return False
            requires_revision = str(job_row[0]) == "call" and not _loads(
                job_row[1]
            ).get("conversation_deleted", False)
            cursor = await conn.execute(
                """
                UPDATE PHONE_DATA_PURGE_JOBS SET lease_until=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='running' AND lease_owner=? AND lease_token=?
                  AND runtime_lease_token=? AND lease_until>=?
                """,
                (
                    lease_until,
                    str(job_id),
                    lease_owner,
                    lease_token,
                    runtime_lease_token,
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                return False
            revision_cursor = await conn.execute(
                """
                UPDATE PHONE_CONVERSATION_DATA_REVISIONS
                SET lease_until=?,updated_at=CURRENT_TIMESTAMP
                WHERE active_job_id=? AND memory_state='rebuilding'
                  AND lease_owner=? AND lease_token=? AND lease_until>=?
                """,
                (lease_until, str(job_id), lease_owner, lease_token, now_text),
            )
            if requires_revision and revision_cursor.rowcount != 1:
                raise PhoneDataPurgeFailure("conversation revision lease was lost")
            return True

    async def update_progress(
        self,
        job: Mapping[str, Any],
        progress: Mapping[str, Any],
    ) -> None:
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                UPDATE PHONE_DATA_PURGE_JOBS SET progress_json=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='running' AND lease_owner=? AND lease_token=?
                """,
                (
                    _json(progress),
                    str(job["id"]),
                    str(job["lease_owner"]),
                    str(job["lease_token"]),
                ),
            )
            if cursor.rowcount != 1:
                raise PhoneDataPurgeFailure("phone data purge lease was lost")

    async def refresh_changed_snapshot(self, job: Mapping[str, Any]) -> dict[str, Any]:
        """Refresh only mutable snapshot generations under the existing fence."""

        async with self._write() as conn:
            await self._require_job_lease(conn, job)
            cursor = await conn.execute(
                "SELECT source_revision,source_snapshot_json,progress_json "
                "FROM PHONE_DATA_PURGE_JOBS WHERE id=?",
                (str(job["id"]),),
            )
            row = await cursor.fetchone()
            progress = _loads(row[2])
            source = _loads(row[1])
            content_revision = int(job.get("content_revision_snapshot") or 0)
            if job["purge_scope"] == "call" and not source.get(
                "conversation_deleted", False
            ):
                cursor = await conn.execute(
                    "SELECT content_revision FROM PHONE_CONVERSATION_DATA_REVISIONS "
                    "WHERE conversation_id_snapshot=? AND active_job_id=?",
                    (int(job["conversation_id_snapshot"]), str(job["id"])),
                )
                revision_row = await cursor.fetchone()
                if revision_row is None:
                    raise PhoneDataPurgeFailure("conversation revision fence was lost")
                content_revision = int(revision_row[0])
                progress.pop("atagia_replayed", None)
            cursor = await conn.execute(
                """
                UPDATE PHONE_DATA_PURGE_JOBS
                SET content_revision_snapshot=?,progress_json=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='running' AND lease_owner=? AND lease_token=?
                RETURNING *
                """,
                (
                    content_revision,
                    _json(progress),
                    str(job["id"]),
                    str(job["lease_owner"]),
                    str(job["lease_token"]),
                ),
            )
            refreshed = await cursor.fetchone()
            if refreshed is None:
                raise PhoneDataPurgeFailure("phone data purge lease was lost")
            return dict(refreshed)

    async def reset_memory_links(self, job: Mapping[str, Any], provider: str) -> None:
        async with self._write() as conn:
            await self._require_job_lease(conn, job)
            await conn.execute(
                "DELETE FROM MEMORY_PROVIDER_MESSAGE_LINKS "
                "WHERE provider=? AND conversation_id=?",
                (str(provider), int(job["conversation_id_snapshot"])),
            )
            await conn.execute(
                "DELETE FROM MEMORY_PROVIDER_CONVERSATION_LINKS "
                "WHERE provider=? AND conversation_id=?",
                (str(provider), int(job["conversation_id_snapshot"])),
            )
            await conn.execute(
                "DELETE FROM MEMORY_PROVIDER_SYNC_STATE "
                "WHERE provider=? AND conversation_id=?",
                (str(provider), int(job["conversation_id_snapshot"])),
            )

    async def surviving_messages(
        self,
        job: Mapping[str, Any],
        excluded_message_ids: Sequence[int],
    ) -> list[dict[str, Any]]:
        async with self._connection_factory(readonly=True) as conn:
            conn.row_factory = aiosqlite.Row
            params: list[Any] = [int(job["conversation_id_snapshot"])]
            exclusion = ""
            if excluded_message_ids:
                placeholders = ",".join("?" for _ in excluded_message_ids)
                exclusion = f" AND m.id NOT IN ({placeholders})"
                params.extend(int(value) for value in excluded_message_ids)
            cursor = await conn.execute(
                """
                SELECT m.id,m.conversation_id,COALESCE(m.user_id,c.user_id) AS user_id,
                       m.message,m.type,m.date,c.role_id AS prompt_id,
                       COALESCE(c.is_incognito,0) AS is_incognito
                FROM MESSAGES m JOIN CONVERSATIONS c ON c.id=m.conversation_id
                WHERE m.conversation_id=? AND m.type IN ('user','bot','assistant')
                """
                + exclusion
                + " ORDER BY m.id",
                tuple(params),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def record_replay_link(
        self,
        *,
        job: Mapping[str, Any],
        provider: str,
        message_id: int,
        provider_message_id: str,
        conversation_id: int,
        user_id: int,
        role: str,
        prompt_id: int | None,
    ) -> None:
        async with self._write() as conn:
            await self._require_job_lease(conn, job)
            await conn.execute(
                """
                INSERT INTO MEMORY_PROVIDER_CONVERSATION_LINKS (
                    provider,conversation_id,user_id,source,metadata_json
                ) VALUES (?,?,?,'phone_delete_replay',?)
                ON CONFLICT(provider,conversation_id) DO UPDATE SET
                    user_id=excluded.user_id,source=excluded.source,
                    last_seen_at=CURRENT_TIMESTAMP,metadata_json=excluded.metadata_json
                """,
                (
                    provider,
                    int(conversation_id),
                    int(user_id),
                    _json(
                        {"prompt_id": str(prompt_id) if prompt_id is not None else None}
                    ),
                ),
            )
            await conn.execute(
                """
                INSERT INTO MEMORY_PROVIDER_MESSAGE_LINKS (
                    message_id,provider,provider_message_id,conversation_id,
                    user_id,role,source,metadata_json
                ) VALUES (?,?,?,?,?,?,'phone_delete_replay','{}')
                ON CONFLICT(message_id,provider) DO UPDATE SET
                    provider_message_id=excluded.provider_message_id,
                    conversation_id=excluded.conversation_id,user_id=excluded.user_id,
                    role=excluded.role,source=excluded.source,
                    synced_at=CURRENT_TIMESTAMP
                """,
                (
                    int(message_id),
                    provider,
                    provider_message_id,
                    int(conversation_id),
                    int(user_id),
                    role,
                ),
            )

    async def complete(self, job: Mapping[str, Any]) -> bool:
        source = _loads(job.get("source_snapshot_json"))
        async with self._write() as conn:
            if not await self._job_lease_valid(conn, job):
                return False
            cursor = await conn.execute(
                "SELECT source_revision FROM PHONE_DATA_PURGE_JOBS WHERE id=?",
                (str(job["id"]),),
            )
            current_source_revision = int((await cursor.fetchone())[0])
            if current_source_revision != int(job.get("source_revision") or 0):
                raise PhoneDataPurgeSnapshotChanged("late recording snapshot changed")
            live_call_purge = job["purge_scope"] == "call" and not source.get(
                "conversation_deleted", False
            )
            if live_call_purge:
                cursor = await conn.execute(
                    """
                    SELECT content_revision FROM PHONE_CONVERSATION_DATA_REVISIONS
                    WHERE conversation_id_snapshot=? AND active_job_id=?
                      AND revision=? AND memory_state='rebuilding' AND memory_blocked=1
                      AND lease_owner=? AND lease_token=? AND lease_until>=?
                    """,
                    (
                        int(job["conversation_id_snapshot"]),
                        str(job["id"]),
                        int(job["conversation_revision"]),
                        str(job["lease_owner"]),
                        str(job["lease_token"]),
                        _utc_text(),
                    ),
                )
                revision_row = await cursor.fetchone()
                if revision_row is None:
                    return False
                if int(revision_row[0]) != int(job["content_revision_snapshot"]):
                    raise PhoneDataPurgeSnapshotChanged("conversation content changed")
            if job["purge_scope"] == "recording":
                ids = [
                    int(item["recording_id"])
                    for item in source.get("recordings", [])
                    if item.get("recording_id") is not None
                ]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    await conn.execute(
                        f"DELETE FROM PHONE_RECORDINGS WHERE id IN ({placeholders})",
                        tuple(ids),
                    )
            else:
                message_ids = [int(value) for value in source.get("message_ids", [])]
                if message_ids:
                    from chat.services.privacy import delete_message_rows

                    await delete_message_rows(
                        conn,
                        conversation_id=int(job["conversation_id_snapshot"]),
                        message_ids=message_ids,
                    )
                await conn.execute(
                    "DELETE FROM PHONE_CALLS WHERE id=?",
                    (str(job["call_id_snapshot"]),),
                )
            if live_call_purge:
                cursor = await conn.execute(
                    """
                    UPDATE PHONE_CONVERSATION_DATA_REVISIONS
                    SET memory_state='ready',memory_blocked=0,active_job_id=NULL,
                        lease_owner=NULL,lease_token=NULL,lease_until=NULL,last_error=NULL,
                        completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                    WHERE active_job_id=? AND revision=? AND lease_owner=?
                      AND lease_token=? AND lease_until>=?
                    """,
                    (
                        str(job["id"]),
                        int(job["conversation_revision"]),
                        str(job["lease_owner"]),
                        str(job["lease_token"]),
                        _utc_text(),
                    ),
                )
                if cursor.rowcount != 1:
                    raise PhoneDataPurgeFailure("conversation revision fence was lost")
            cursor = await conn.execute(
                """
                UPDATE PHONE_DATA_PURGE_JOBS
                SET status='completed',completed_at=CURRENT_TIMESTAMP,
                    lease_owner=NULL,lease_token=NULL,lease_until=NULL,runtime_lease_token=NULL,
                    last_error=NULL,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='running' AND lease_owner=? AND lease_token=?
                  AND runtime_lease_token=? AND source_revision=? AND lease_until>=?
                """,
                (
                    str(job["id"]),
                    str(job["lease_owner"]),
                    str(job["lease_token"]),
                    str(job["runtime_lease_token"]),
                    int(job.get("source_revision") or 0),
                    _utc_text(),
                ),
            )
            if cursor.rowcount != 1:
                raise PhoneDataPurgeFailure(
                    "phone data purge completion fence was lost"
                )
            return True

    async def fail(self, job: Mapping[str, Any], error: BaseException | str) -> bool:
        detail = str(error).strip() or type(error).__name__
        async with self._write() as conn:
            if not await self._job_lease_valid(conn, job):
                return False
            await conn.execute(
                """
                UPDATE PHONE_DATA_PURGE_JOBS
                SET status='needs_attention',last_error=?,next_attempt_at=NULL,
                    lease_owner=NULL,lease_token=NULL,lease_until=NULL,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?
                """,
                (detail[:1000], str(job["id"])),
            )
            await conn.execute(
                """
                UPDATE PHONE_CONVERSATION_DATA_REVISIONS
                SET memory_state='needs_attention',memory_blocked=1,
                    lease_owner=NULL,lease_token=NULL,lease_until=NULL,last_error=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE active_job_id=?
                """,
                (detail[:1000], str(job["id"])),
            )
            return True

    async def retry(
        self,
        *,
        job_id: str,
        expected_attempt_count: int,
        resolution: str,
    ) -> bool:
        if resolution != "reconcile_by_purge":
            raise ValueError("phone data purge retry resolution is invalid")
        async with self._write() as conn:
            job = await self._job(conn, job_id)
            if (
                job is None
                or str(job["status"]) != "needs_attention"
                or int(job["attempt_count"]) != int(expected_attempt_count)
            ):
                return False
            conversation_id = int(job["conversation_id_snapshot"])
            source = _loads(job.get("source_snapshot_json"))
            cursor = await conn.execute(
                """
                SELECT DISTINCT provider FROM PHONE_MEMORY_OPERATION_LEASES
                WHERE conversation_id_snapshot=? AND status='needs_attention'
                """,
                (conversation_id,),
            )
            ambiguous_providers = {
                str(row[0]) for row in await cursor.fetchall() if row[0]
            }
            reconcilable_providers = (
                {"atagia", "mem0"}
                if source.get("conversation_deleted", False)
                else {"atagia"}
            )
            if not ambiguous_providers.issubset(reconcilable_providers):
                # Generic Mem0 has no safe call-scoped purge/replay contract.
                # Keep this ambiguity visible instead of acknowledging away
                # memory that may still contain the deleted phone turns.
                return False
            await conn.execute(
                """
                DELETE FROM PHONE_MEMORY_OPERATION_LEASES
                WHERE conversation_id_snapshot=? AND status='needs_attention'
                """,
                (conversation_id,),
            )
            await conn.execute(
                """
                UPDATE PHONE_CONVERSATION_DATA_REVISIONS
                SET memory_state='rebuilding',memory_blocked=1,last_error=NULL,
                    lease_owner=NULL,lease_token=NULL,lease_until=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE conversation_id_snapshot=? AND active_job_id=?
                """,
                (conversation_id, str(job_id)),
            )
            cursor = await conn.execute(
                """
                UPDATE PHONE_DATA_PURGE_JOBS
                SET status='scheduled',last_error=NULL,next_attempt_at=NULL,
                    lease_owner=NULL,lease_token=NULL,lease_until=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='needs_attention' AND attempt_count=?
                """,
                (str(job_id), int(expected_attempt_count)),
            )
            return cursor.rowcount == 1

    async def is_deleted_callback(self, token: str, provider_call_sid: str) -> bool:
        try:
            async with self._connection_factory(readonly=True) as conn:
                cursor = await conn.execute(
                    """
                    SELECT 1 FROM PHONE_CALL_TOMBSTONES
                    WHERE dispatch_token=? AND
                        (provider_call_sid IS NULL OR provider_call_sid=?)
                    """,
                    (str(token), str(provider_call_sid)),
                )
                return await cursor.fetchone() is not None
        except aiosqlite.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            return False

    async def is_deleted_provider_call(self, provider_call_sid: str) -> bool:
        try:
            async with self._connection_factory(readonly=True) as conn:
                cursor = await conn.execute(
                    "SELECT 1 FROM PHONE_CALL_TOMBSTONES WHERE provider_call_sid=?",
                    (str(provider_call_sid),),
                )
                return await cursor.fetchone() is not None
        except aiosqlite.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            return False

    async def capture_late_recording(
        self,
        *,
        token: str,
        provider_call_sid: str,
        provider_recording_sid: str,
    ) -> bool:
        """Attach a late remote asset to its durable purge without recreating rows."""

        try:
            async with self._write() as conn:
                cursor = await conn.execute(
                    """
                SELECT t.purge_job_id FROM PHONE_CALL_TOMBSTONES t
                WHERE t.dispatch_token=? AND
                    (t.provider_call_sid IS NULL OR t.provider_call_sid=?)
                UNION ALL
                SELECT r.purge_job_id FROM PHONE_RECORDING_TOMBSTONES r
                JOIN PHONE_CALLS c ON c.id=r.call_id_snapshot
                WHERE c.dispatch_token=? AND c.provider_call_sid=?
                LIMIT 1
                """,
                    (
                        str(token),
                        str(provider_call_sid),
                        str(token),
                        str(provider_call_sid),
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    return False
                job_id = str(row[0])
                job = await self._job(conn, job_id)
                if job is None:
                    raise PhoneDataPurgeFailure("late recording purge job is missing")
                source = _loads(job.get("source_snapshot_json"))
                recordings = source.setdefault("recordings", [])
                if any(
                    str(item.get("provider_recording_sid"))
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
                return cursor.rowcount == 1
        except aiosqlite.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            return False

    async def acquire_runtime(
        self, *, worker_id: str, lease_seconds: float
    ) -> str | None:
        now = datetime.now(UTC)
        token = uuid4().hex
        lease_until = _utc_text(now + timedelta(seconds=float(lease_seconds)))
        async with self._write() as conn:
            cursor = await conn.execute(
                "SELECT lease_until FROM PHONE_DATA_PURGE_RUNTIME WHERE singleton=1"
            )
            row = await cursor.fetchone()
            if row is not None and str(row[0]) >= _utc_text(now):
                return None
            await conn.execute(
                """
                INSERT INTO PHONE_DATA_PURGE_RUNTIME (
                    singleton,worker_id,lease_token,lease_until,heartbeat_at
                ) VALUES (1,?,?,?,?)
                ON CONFLICT(singleton) DO UPDATE SET
                    worker_id=excluded.worker_id,lease_token=excluded.lease_token,
                    lease_until=excluded.lease_until,heartbeat_at=excluded.heartbeat_at,
                    started_at=CURRENT_TIMESTAMP
                """,
                (worker_id, token, lease_until, _utc_text(now)),
            )
            return token

    async def renew_runtime(
        self, *, worker_id: str, lease_token: str, lease_seconds: float
    ) -> bool:
        now = datetime.now(UTC)
        async with self._write() as conn:
            cursor = await conn.execute(
                """
                UPDATE PHONE_DATA_PURGE_RUNTIME
                SET lease_until=?,heartbeat_at=?
                WHERE singleton=1 AND worker_id=? AND lease_token=? AND lease_until>=?
                """,
                (
                    _utc_text(now + timedelta(seconds=float(lease_seconds))),
                    _utc_text(now),
                    worker_id,
                    lease_token,
                    _utc_text(now),
                ),
            )
            return cursor.rowcount == 1

    async def release_runtime(self, *, worker_id: str, lease_token: str) -> None:
        async with self._write() as conn:
            await conn.execute(
                "DELETE FROM PHONE_DATA_PURGE_RUNTIME "
                "WHERE singleton=1 AND worker_id=? AND lease_token=?",
                (worker_id, lease_token),
            )

    async def _stage_call_job(
        self,
        conn: Any,
        call: Mapping[str, Any],
        *,
        conversation_deleted: bool,
    ) -> dict[str, Any]:
        recordings = await self._recordings(conn, str(call["id"]))
        source = await self._source_snapshot(
            conn,
            call,
            recordings=recordings,
            conversation_deleted=conversation_deleted,
        )
        job_id = uuid4().hex
        cursor = await conn.execute(
            """
            INSERT INTO PHONE_DATA_PURGE_JOBS (
                id,owner_user_id,conversation_id,call_id,recording_id,
                owner_user_id_snapshot,conversation_id_snapshot,
                call_id_snapshot,recording_id_snapshot,purge_scope,
                conversation_revision,provider_call_sid_snapshot,
                provider_recording_sid_snapshot,source_snapshot_json,
                progress_json,next_attempt_at
            ) VALUES (?,?,?,?,NULL,?,?,?,NULL,'call',1,?,NULL,?,'{}',NULL)
            RETURNING *
            """,
            (
                job_id,
                int(call["owner_user_id"]),
                int(call["conversation_id"]),
                str(call["id"]),
                int(call["owner_user_id"]),
                int(call["conversation_id"]),
                str(call["id"]),
                call.get("provider_call_sid"),
                _json(source),
            ),
        )
        job = dict(await cursor.fetchone())
        if not conversation_deleted:
            revision = await self._reserve_conversation_revision(
                conn,
                job_id=job_id,
                owner_user_id=int(call["owner_user_id"]),
                conversation_id=int(call["conversation_id"]),
            )
            await conn.execute(
                "UPDATE PHONE_DATA_PURGE_JOBS SET conversation_revision=? WHERE id=?",
                (revision, job_id),
            )
            job["conversation_revision"] = revision
        await conn.execute(
            """
            INSERT INTO PHONE_CALL_TOMBSTONES (
                call_id,owner_user_id_snapshot,conversation_id_snapshot,
                dispatch_token,provider_call_sid,purge_job_id
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                str(call["id"]),
                int(call["owner_user_id"]),
                int(call["conversation_id"]),
                str(call["dispatch_token"]),
                call.get("provider_call_sid"),
                job_id,
            ),
        )
        for message_id in source.get("message_ids", []):
            await conn.execute(
                """
                INSERT OR IGNORE INTO PHONE_PURGED_MESSAGE_TOMBSTONES (
                    message_id,conversation_id_snapshot,purge_job_id
                ) VALUES (?,?,?)
                """,
                (int(message_id), int(call["conversation_id"]), job_id),
            )
        await conn.execute(
            "UPDATE PHONE_CALLS SET deleted_at=COALESCE(deleted_at,CURRENT_TIMESTAMP),"
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (str(call["id"]),),
        )
        await conn.execute(
            "UPDATE PHONE_RECORDINGS SET status='deleting',updated_at=CURRENT_TIMESTAMP "
            "WHERE call_id=?",
            (str(call["id"]),),
        )
        return job

    async def _reserve_conversation_revision(
        self,
        conn: Any,
        *,
        job_id: str,
        owner_user_id: int,
        conversation_id: int,
    ) -> int:
        await conn.execute(
            """
            INSERT OR IGNORE INTO PHONE_CONVERSATION_DATA_REVISIONS (
                conversation_id_snapshot,owner_user_id_snapshot
            ) VALUES (?,?)
            """,
            (int(conversation_id), int(owner_user_id)),
        )
        cursor = await conn.execute(
            "SELECT revision,memory_blocked,active_job_id "
            "FROM PHONE_CONVERSATION_DATA_REVISIONS "
            "WHERE conversation_id_snapshot=?",
            (int(conversation_id),),
        )
        state = await cursor.fetchone()
        if bool(state[1]) and state[2] != job_id:
            raise TelephonyConflictError(
                "Conversation already has a pending phone-data deletion"
            )
        revision = int(state[0]) + (0 if state[2] == job_id else 1)
        await conn.execute(
            """
            UPDATE PHONE_CONVERSATION_DATA_REVISIONS
            SET owner_user_id_snapshot=?,revision=?,memory_state='rebuilding',
                memory_blocked=1,active_job_id=?,lease_owner=NULL,
                lease_token=NULL,lease_until=NULL,last_error=NULL,
                completed_at=NULL,updated_at=CURRENT_TIMESTAMP
            WHERE conversation_id_snapshot=?
            """,
            (
                int(owner_user_id),
                revision,
                job_id,
                int(conversation_id),
            ),
        )
        return revision

    async def _source_snapshot(
        self,
        conn: Any,
        call: Mapping[str, Any],
        *,
        recordings: list[dict[str, Any]],
        conversation_deleted: bool,
    ) -> dict[str, Any]:
        cursor = await conn.execute(
            """
            SELECT l.message_id FROM PHONE_CALL_MESSAGE_LINKS l
            WHERE l.call_id=? AND l.origin_channel='phone'
            ORDER BY l.message_id
            """,
            (str(call["id"]),),
        )
        message_ids = [int(row[0]) for row in await cursor.fetchall()]
        providers: set[str] = set()
        for table in (
            "MEMORY_PROVIDER_MESSAGE_LINKS",
            "MEMORY_PROVIDER_CONVERSATION_LINKS",
        ):
            cursor = await conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
            if await cursor.fetchone() is None:
                continue
            cursor = await conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
            if await cursor.fetchone() is None:
                continue
            cursor = await conn.execute(
                f"SELECT DISTINCT provider FROM {table} WHERE conversation_id=?",
                (int(call["conversation_id"]),),
            )
            providers.update(str(row[0]) for row in await cursor.fetchall() if row[0])
        cursor = await conn.execute(
            """
            SELECT DISTINCT provider FROM PHONE_MEMORY_OPERATION_LEASES
            WHERE conversation_id_snapshot=? AND provider<>'none'
            """,
            (int(call["conversation_id"]),),
        )
        providers.update(str(row[0]) for row in await cursor.fetchall() if row[0])
        cursor = await conn.execute(
            "SELECT role_id,COALESCE(is_incognito,0) FROM CONVERSATIONS WHERE id=?",
            (int(call["conversation_id"]),),
        )
        conversation = await cursor.fetchone()
        return {
            "call_id": str(call["id"]),
            "provider_call_sid": call.get("provider_call_sid"),
            "dispatch_token": str(call["dispatch_token"]),
            "owner_user_id": int(call["owner_user_id"]),
            "conversation_id": int(call["conversation_id"]),
            "prompt_id": int(conversation[0])
            if conversation and conversation[0]
            else None,
            "incognito": bool(conversation[1]) if conversation else False,
            "conversation_deleted": bool(conversation_deleted),
            "message_ids": message_ids,
            "memory_providers": sorted(providers),
            "recordings": recordings,
        }

    async def _recordings(self, conn: Any, call_id: str) -> list[dict[str, Any]]:
        cursor = await conn.execute(
            """
            SELECT id AS recording_id,provider_recording_sid,
                   participant_path,assistant_path,mixed_path
            FROM PHONE_RECORDINGS WHERE call_id=? ORDER BY id
            """,
            (str(call_id),),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def _mark_job_conversation_deleted(self, conn: Any, job_id: str) -> None:
        job = await self._job(conn, job_id)
        if job is None:
            return
        source = _loads(job.get("source_snapshot_json"))
        if source.get("conversation_deleted"):
            return
        source["conversation_deleted"] = True
        providers = {
            str(value) for value in source.get("memory_providers") or [] if value
        }
        conversation_id = int(job["conversation_id_snapshot"])
        for table in (
            "MEMORY_PROVIDER_MESSAGE_LINKS",
            "MEMORY_PROVIDER_CONVERSATION_LINKS",
        ):
            cursor = await conn.execute(
                f"SELECT DISTINCT provider FROM {table} WHERE conversation_id=?",
                (conversation_id,),
            )
            providers.update(str(row[0]) for row in await cursor.fetchall() if row[0])
        cursor = await conn.execute(
            "SELECT DISTINCT provider FROM PHONE_MEMORY_OPERATION_LEASES "
            "WHERE conversation_id_snapshot=? AND provider<>'none'",
            (conversation_id,),
        )
        providers.update(str(row[0]) for row in await cursor.fetchall() if row[0])
        source["memory_providers"] = sorted(providers)
        progress = _loads(job.get("progress_json"))
        progress.pop("atagia_replayed", None)
        cursor = await conn.execute(
            """
            UPDATE PHONE_DATA_PURGE_JOBS
            SET source_snapshot_json=?,progress_json=?,source_revision=source_revision+1,
                status=CASE WHEN status='completed' THEN 'scheduled' ELSE status END,
                completed_at=CASE WHEN status='completed' THEN NULL ELSE completed_at END,
                next_attempt_at=CASE WHEN status='completed' THEN NULL ELSE next_attempt_at END,
                lease_owner=CASE WHEN status='completed' THEN NULL ELSE lease_owner END,
                lease_token=CASE WHEN status='completed' THEN NULL ELSE lease_token END,
                lease_until=CASE WHEN status='completed' THEN NULL ELSE lease_until END,
                runtime_lease_token=CASE WHEN status='completed' THEN NULL ELSE runtime_lease_token END,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND source_revision=?
            """,
            (
                _json(source),
                _json(progress),
                str(job_id),
                int(job.get("source_revision") or 0),
            ),
        )
        if cursor.rowcount != 1:
            raise PhoneDataPurgeSnapshotChanged("conversation deletion snapshot changed")
        await conn.execute(
            """
            UPDATE PHONE_CONVERSATION_DATA_REVISIONS
            SET memory_state='ready',memory_blocked=0,active_job_id=NULL,
                lease_owner=NULL,lease_token=NULL,lease_until=NULL,last_error=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE active_job_id=?
            """,
            (str(job_id),),
        )

    async def _acquire_conversation_revision(
        self,
        conn: Any,
        *,
        job_id: str,
        owner_user_id: int,
        conversation_id: int,
        lease_owner: str,
        lease_token: str,
        lease_until: str,
        now_text: str,
    ) -> tuple[int, int]:
        await conn.execute(
            """
            INSERT OR IGNORE INTO PHONE_CONVERSATION_DATA_REVISIONS (
                conversation_id_snapshot,owner_user_id_snapshot
            ) VALUES (?,?)
            """,
            (int(conversation_id), int(owner_user_id)),
        )
        cursor = await conn.execute(
            "SELECT * FROM PHONE_CONVERSATION_DATA_REVISIONS "
            "WHERE conversation_id_snapshot=?",
            (int(conversation_id),),
        )
        state = dict(await cursor.fetchone())
        active_job = state.get("active_job_id")
        if (
            active_job not in {None, job_id}
            and state.get("lease_until") is not None
            and str(state["lease_until"]) >= now_text
        ):
            raise TelephonyConflictError("Conversation memory rebuild is already owned")
        revision = int(state["revision"])
        if active_job != job_id:
            revision += 1
        await conn.execute(
            """
            UPDATE PHONE_CONVERSATION_DATA_REVISIONS
            SET owner_user_id_snapshot=?,revision=?,memory_state='rebuilding',
                memory_blocked=1,active_job_id=?,lease_owner=?,lease_token=?,
                lease_until=?,last_error=NULL,completed_at=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE conversation_id_snapshot=?
            """,
            (
                int(owner_user_id),
                revision,
                job_id,
                lease_owner,
                lease_token,
                lease_until,
                int(conversation_id),
            ),
        )
        return revision, int(state["content_revision"])

    async def _call_tombstone(
        self, conn: Any, call_id: str, owner_user_id: int
    ) -> dict[str, Any] | None:
        cursor = await conn.execute(
            "SELECT * FROM PHONE_CALL_TOMBSTONES "
            "WHERE call_id=? AND owner_user_id_snapshot=?",
            (str(call_id), int(owner_user_id)),
        )
        return _row(await cursor.fetchone())

    async def _job(self, conn: Any, job_id: str) -> dict[str, Any] | None:
        cursor = await conn.execute(
            "SELECT * FROM PHONE_DATA_PURGE_JOBS WHERE id=?", (str(job_id),)
        )
        return _row(await cursor.fetchone())

    async def _require_schema(self, conn: Any) -> None:
        if not await self.schema_ready(conn):
            raise PhoneDataPurgeFailure("phone data purge schema is unavailable")

    async def _runtime_lease_valid(
        self, conn: Any, worker_id: str, lease_token: str, now_text: str
    ) -> bool:
        cursor = await conn.execute(
            """
            SELECT 1 FROM PHONE_DATA_PURGE_RUNTIME
            WHERE singleton=1 AND worker_id=? AND lease_token=? AND lease_until>=?
            """,
            (str(worker_id), str(lease_token), now_text),
        )
        return await cursor.fetchone() is not None

    async def _memory_leases_quiescent(
        self, conn: Any, job: Mapping[str, Any], now_text: str
    ) -> bool:
        if job["purge_scope"] != "call":
            return True
        conversation_id = int(job["conversation_id_snapshot"])
        await conn.execute(
            """
            DELETE FROM PHONE_MEMORY_OPERATION_LEASES
            WHERE conversation_id_snapshot=? AND status='active'
              AND provider_started=0 AND lease_until<?
            """,
            (conversation_id, now_text),
        )
        cursor = await conn.execute(
            """
            SELECT id,status,provider_started,lease_until,last_error
            FROM PHONE_MEMORY_OPERATION_LEASES
            WHERE conversation_id_snapshot=?
              AND (status='needs_attention' OR status='active')
            ORDER BY created_at LIMIT 1
            """,
            (conversation_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return True
        if str(row[1]) == "active" and str(row[3]) >= now_text:
            return False
        detail = str(row[4] or "memory provider outcome is ambiguous")[:1000]
        if str(row[1]) == "active" and bool(row[2]):
            await conn.execute(
                """
                UPDATE PHONE_MEMORY_OPERATION_LEASES
                SET status='needs_attention',last_error=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='active'
                """,
                (detail, str(row[0])),
            )
        await conn.execute(
            """
            UPDATE PHONE_DATA_PURGE_JOBS
            SET status='needs_attention',last_error=?,next_attempt_at=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status IN ('scheduled','running')
            """,
            (detail, str(job["id"])),
        )
        await conn.execute(
            """
            UPDATE PHONE_CONVERSATION_DATA_REVISIONS
            SET memory_state='needs_attention',memory_blocked=1,last_error=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE conversation_id_snapshot=? AND active_job_id=?
            """,
            (detail, conversation_id, str(job["id"])),
        )
        return False

    async def _job_lease_valid(self, conn: Any, job: Mapping[str, Any]) -> bool:
        cursor = await conn.execute(
            """
            SELECT 1 FROM PHONE_DATA_PURGE_JOBS j
            JOIN PHONE_DATA_PURGE_RUNTIME r ON r.singleton=1
            WHERE j.id=? AND j.status='running' AND j.lease_owner=?
              AND j.lease_token=? AND j.runtime_lease_token=?
              AND j.lease_until>=? AND r.worker_id=j.lease_owner
              AND r.lease_token=j.runtime_lease_token AND r.lease_until>=?
            """,
            (
                str(job["id"]),
                str(job["lease_owner"]),
                str(job["lease_token"]),
                str(job["runtime_lease_token"]),
                _utc_text(),
                _utc_text(),
            ),
        )
        return await cursor.fetchone() is not None

    async def _require_job_lease(self, conn: Any, job: Mapping[str, Any]) -> None:
        if not await self._job_lease_valid(conn, job):
            raise PhoneDataPurgeFailure("phone data purge lease was lost")


async def _default_atagia_bridge() -> Any:
    from atagia_bridge import AtagiaBridge
    from atagia_config import bridge_config_from_mapping, get_atagia_config

    config = bridge_config_from_mapping(
        await get_atagia_config(), enabled_override=True
    )
    return AtagiaBridge(config)


def _default_voice_client() -> AsyncTwilioVoiceClient:
    sid = os.getenv("TWILIO_SID", "").strip()
    token = os.getenv("TWILIO_AUTH", "").strip()
    if not sid or not token:
        raise PhoneDataPurgeFailure("Twilio deletion credentials are unavailable")
    return AsyncTwilioVoiceClient(sid, token)


class PhoneDataPurgeService:
    """Execute idempotent external and local phases for one fenced job."""

    def __init__(
        self,
        repository: PhoneDataPurgeRepository | None = None,
        *,
        voice_client_factory: Callable[
            [], AsyncTwilioVoiceClient
        ] = _default_voice_client,
        atagia_bridge_factory: Callable[[], Awaitable[Any]] = _default_atagia_bridge,
        memory_preferences_loader: Callable[[int, str], Awaitable[dict[str, Any]]]
        | None = None,
        recording_root: str | os.PathLike[str] = DEFAULT_RECORDING_ROOT,
    ) -> None:
        self.repository = repository or PhoneDataPurgeRepository()
        self.voice_client_factory = voice_client_factory
        self.atagia_bridge_factory = atagia_bridge_factory
        self.memory_preferences_loader = memory_preferences_loader
        self.recording_root = recording_root

    async def process(self, job: Mapping[str, Any]) -> None:
        source = _loads(job.get("source_snapshot_json"))
        progress = _loads(job.get("progress_json"))
        call_id = str(job["call_id_snapshot"])
        recordings = list(source.get("recordings") or [])

        if not progress.get("local_audio_deleted"):
            paths = [
                recording.get(key)
                for recording in recordings
                for key in ("participant_path", "assistant_path", "mixed_path")
            ]
            delete_private_call_audio(call_id, paths, root=self.recording_root)
            progress["local_audio_deleted"] = True
            await self.repository.update_progress(job, progress)

        remote_recordings = sorted(
            {
                str(item["provider_recording_sid"])
                for item in recordings
                if item.get("provider_recording_sid")
            }
        )
        deleted_remote = set(progress.get("remote_recordings_deleted") or [])
        client: AsyncTwilioVoiceClient | None = None
        try:
            if any(sid not in deleted_remote for sid in remote_recordings) or (
                job["purge_scope"] == "call"
                and source.get("provider_call_sid")
                and not progress.get("remote_call_deleted")
            ):
                client = self.voice_client_factory()
            for recording_sid in remote_recordings:
                if recording_sid in deleted_remote:
                    continue
                try:
                    await client.delete_recording_once(recording_sid)  # type: ignore[union-attr]
                except RuntimeError as exc:
                    raise PhoneDataPurgeAmbiguous(str(exc)) from exc
                deleted_remote.add(recording_sid)
                progress["remote_recordings_deleted"] = sorted(deleted_remote)
                await self.repository.update_progress(job, progress)
            if (
                job["purge_scope"] == "call"
                and source.get("provider_call_sid")
                and not progress.get("remote_call_deleted")
            ):
                try:
                    await client.delete_call_record_once(  # type: ignore[union-attr]
                        str(source["provider_call_sid"])
                    )
                except RuntimeError as exc:
                    raise PhoneDataPurgeAmbiguous(str(exc)) from exc
                progress["remote_call_deleted"] = True
                await self.repository.update_progress(job, progress)
        finally:
            if client is not None:
                await client.close()

        if job["purge_scope"] != "call":
            return
        providers = {str(value) for value in source.get("memory_providers") or []}
        if "mem0" in providers and not source.get("conversation_deleted"):
            raise PhoneDataPurgeFailure(
                "Mem0 call-scoped replay has no safe idempotent generic contract"
            )
        if "atagia" in providers and not progress.get("atagia_replayed"):
            await self._purge_and_replay_atagia(job, source)
            progress["atagia_replayed"] = True
            await self.repository.update_progress(job, progress)
        if "mem0" in providers and source.get("conversation_deleted"):
            from ai_runtime.memory.recording import (
                _purge_memory_conversation_best_effort,
            )

            purged = await _purge_memory_conversation_best_effort(
                user_id=int(source["owner_user_id"]),
                conversation_id=int(source["conversation_id"]),
                prompt_id=source.get("prompt_id"),
                incognito=bool(source.get("incognito")),
                provider="mem0",
            )
            if not purged:
                raise PhoneDataPurgeFailure("Mem0 conversation purge was not confirmed")

    async def _purge_and_replay_atagia(
        self,
        job: Mapping[str, Any],
        source: Mapping[str, Any],
    ) -> None:
        bridge = await self.atagia_bridge_factory()
        user_id = int(source["owner_user_id"])
        conversation_id = int(source["conversation_id"])
        prompt_id = source.get("prompt_id")
        incognito = bool(source.get("incognito"))
        try:
            scopes = [prompt_id, None] if prompt_id is not None else [None]
            for scope in scopes:
                confirmed = await bridge.purge_conversation(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    prompt_id=scope,
                    incognito=incognito,
                )
                if not confirmed:
                    raise PhoneDataPurgeFailure(
                        "Atagia conversation purge was not confirmed"
                    )
            await self.repository.reset_memory_links(job, "atagia")
            if source.get("conversation_deleted"):
                return
            excluded = [int(value) for value in source.get("message_ids") or []]
            messages = await self.repository.surviving_messages(job, excluded)
            if self.memory_preferences_loader is None:
                from memory.config import get_user_memory_preferences

                preferences = await get_user_memory_preferences(user_id, "atagia")
            else:
                preferences = await self.memory_preferences_loader(user_id, "atagia")
            if preferences.get("remember_across_chats") is False:
                return
            replay_prompt = (
                None if preferences.get("memory_scope") == "global" else prompt_id
            )
            from atagia_sync import _message_text_for_atagia_sync

            for message in messages:
                text = _message_text_for_atagia_sync(message.get("message")).strip()
                if not text:
                    continue
                role = "user" if str(message.get("type")) == "user" else "assistant"
                message_id = int(message["id"])
                accepted = await bridge.ingest_message(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    role=role,
                    text=text,
                    occurred_at=message.get("date"),
                    prompt_id=replay_prompt,
                    message_id=message_id,
                    source_seq=message_id,
                    ingest_origin="backfill",
                    confirmation_strategy="admin_review_only",
                )
                if not accepted:
                    raise PhoneDataPurgeFailure(
                        f"Atagia replay failed for message {message_id}"
                    )
                # This is the same stable host identity used by the normal
                # Atagia bridge, kept local here to avoid importing the whole
                # interactive runtime into a background privacy worker.
                provider_message_id = f"aurvek:msg:{message_id}"
                await self.repository.record_replay_link(
                    job=job,
                    provider="atagia",
                    message_id=message_id,
                    provider_message_id=provider_message_id,
                    conversation_id=conversation_id,
                    user_id=int(message["user_id"]),
                    role=role,
                    prompt_id=replay_prompt,
                )
            flush = getattr(bridge, "flush", None)
            if callable(flush):
                await flush()
        finally:
            close = getattr(bridge, "close", None)
            if callable(close):
                await close()


class PhoneDataPurgeWorker:
    """Lifecycle-injectable worker with runtime and per-job lease heartbeats."""

    def __init__(
        self,
        repository: PhoneDataPurgeRepository | None = None,
        *,
        service: PhoneDataPurgeService | None = None,
        worker_id: str | None = None,
        lease_seconds: float = 180.0,
        runtime_lease_seconds: float = 30.0,
        poll_seconds: float = 1.0,
        attempt_timeout_seconds: float = 120.0,
    ) -> None:
        if lease_seconds <= attempt_timeout_seconds:
            raise ValueError("purge job lease must exceed its attempt timeout")
        if runtime_lease_seconds <= poll_seconds or poll_seconds <= 0:
            raise ValueError("purge runtime lease and polling are invalid")
        self.repository = repository or PhoneDataPurgeRepository()
        self.service = service or PhoneDataPurgeService(self.repository)
        self.worker_id = (
            worker_id or f"phone-purge-{socket.gethostname()}-{os.getpid()}"
        )
        self.lease_seconds = float(lease_seconds)
        self.runtime_lease_seconds = float(runtime_lease_seconds)
        self.poll_seconds = float(poll_seconds)
        self.attempt_timeout_seconds = float(attempt_timeout_seconds)
        self._runtime_token: str | None = None

    async def run_once(self) -> bool:
        owns_runtime = False
        if self._runtime_token is None:
            self._runtime_token = await self.repository.acquire_runtime(
                worker_id=self.worker_id,
                lease_seconds=max(
                    self.runtime_lease_seconds, self.attempt_timeout_seconds + 5.0
                ),
            )
            if self._runtime_token is None:
                return False
            owns_runtime = True
        try:
            return await self._run_once_fenced(self._runtime_token)
        finally:
            if owns_runtime:
                await self.repository.release_runtime(
                    worker_id=self.worker_id,
                    lease_token=self._runtime_token,
                )
                self._runtime_token = None

    async def _run_once_fenced(self, runtime_token: str) -> bool:
        job = await self.repository.claim_next(
            lease_owner=self.worker_id,
            lease_seconds=self.lease_seconds,
            runtime_lease_token=runtime_token,
        )
        if job is None:
            return False
        lost = asyncio.Event()
        heartbeat = asyncio.create_task(self._job_heartbeat(job, lost))
        source = _loads(job.get("source_snapshot_json"))

        async def process_to_stable_snapshot() -> None:
            nonlocal job
            while True:
                await self.service.process(job)
                try:
                    if lost.is_set() or not await self.repository.complete(job):
                        raise PhoneDataPurgeFailure("phone data purge lease was lost")
                except PhoneDataPurgeSnapshotChanged:
                    job = await self.repository.refresh_changed_snapshot(job)
                    continue
                return

        try:
            if job["purge_scope"] == "call" and not source.get(
                "conversation_deleted", False
            ):
                async with conversation_write_lock(
                    int(job["conversation_id_snapshot"])
                ):
                    await asyncio.wait_for(
                        process_to_stable_snapshot(),
                        timeout=self.attempt_timeout_seconds,
                    )
            else:
                await asyncio.wait_for(
                    process_to_stable_snapshot(),
                    timeout=self.attempt_timeout_seconds,
                )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self.repository.fail(job, exc)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        return True

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        runtime_token = await self.repository.acquire_runtime(
            worker_id=self.worker_id,
            lease_seconds=self.runtime_lease_seconds,
        )
        if runtime_token is None:
            raise RuntimeError("another phone data purge worker owns the runtime lease")
        self._runtime_token = runtime_token
        owner_task = asyncio.current_task()
        if owner_task is None:
            raise RuntimeError("phone data purge worker has no owning task")
        heartbeat = asyncio.create_task(
            self._runtime_heartbeat(runtime_token, stop_event, owner_task),
            name=f"{self.worker_id}-heartbeat",
        )
        try:
            while not stop_event.is_set():
                processed = await self.run_once()
                if processed:
                    continue
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass
        finally:
            heartbeat.cancel()
            heartbeat_error: BaseException | None = None
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                heartbeat_error = exc
            await self.repository.release_runtime(
                worker_id=self.worker_id,
                lease_token=runtime_token,
            )
            self._runtime_token = None
            if heartbeat_error is not None:
                raise heartbeat_error

    async def _job_heartbeat(self, job: Mapping[str, Any], lost: asyncio.Event) -> None:
        interval = max(1.0, self.lease_seconds / 3.0)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self.repository.renew_job(
                    job_id=str(job["id"]),
                    lease_owner=str(job["lease_owner"]),
                    lease_token=str(job["lease_token"]),
                    runtime_lease_token=str(job["runtime_lease_token"]),
                    lease_seconds=self.lease_seconds,
                )
            except PhoneDataPurgeFailure:
                renewed = False
            if not renewed:
                lost.set()
                return

    async def _runtime_heartbeat(
        self,
        runtime_token: str,
        stop_event: asyncio.Event,
        owner_task: asyncio.Task[Any],
    ) -> None:
        try:
            interval = max(1.0, self.runtime_lease_seconds / 3.0)
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                    return
                except TimeoutError:
                    pass
                if not await self.repository.renew_runtime(
                    worker_id=self.worker_id,
                    lease_token=runtime_token,
                    lease_seconds=self.runtime_lease_seconds,
                ):
                    raise RuntimeError("phone data purge runtime lease was lost")
        except asyncio.CancelledError:
            raise
        except BaseException:
            # The runtime owner treats a non-None token as an admission lease.
            # Clear it before waking/canceling the main loop so every concurrent
            # readiness and dispatch fence fails closed immediately.
            self._runtime_token = None
            if not owner_task.done():
                owner_task.cancel()
            raise


def create_phone_data_purge_worker(
    **kwargs: Any,
) -> PhoneDataPurgeWorker:
    """Stable injection hook for the telephony lifecycle owner."""

    return PhoneDataPurgeWorker(**kwargs)


__all__ = [
    "PhoneDataPurgeAmbiguous",
    "PhoneDataPurgeFailure",
    "PhoneDataPurgeRepository",
    "PhoneDataPurgeService",
    "PhoneDataPurgeWorker",
    "PurgeRequest",
    "create_phone_data_purge_worker",
    "is_conversation_memory_blocked",
    "phone_data_purge_runtime_operational",
]
