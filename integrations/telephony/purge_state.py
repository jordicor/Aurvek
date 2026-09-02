"""Lightweight read guards shared by phone-data purge consumers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import aiosqlite

import database


def _utc_text(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


async def is_conversation_memory_blocked(
    conversation_id: int,
    *,
    connection_factory: Callable[..., Any] | None = None,
) -> bool:
    factory = connection_factory or database.get_db_connection
    try:
        async with factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT memory_blocked FROM PHONE_CONVERSATION_DATA_REVISIONS
                WHERE conversation_id_snapshot=?
                """,
                (int(conversation_id),),
            )
            row = await cursor.fetchone()
            return bool(row[0]) if row is not None else False
    except aiosqlite.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        return False


class PhoneMemoryWriteBlocked(RuntimeError):
    """A durable phone-data rebuild fence rejected a provider operation."""


@dataclass(slots=True)
class PhoneMemoryOperationLease:
    id: str
    conversation_id: int
    lease_token: str
    connection_factory: Callable[..., Any]
    provider_started: bool = False
    durable: bool = True

    async def mark_provider_started(self) -> None:
        if not self.durable:
            self.provider_started = True
            return
        now = _utc_text()
        async with self.connection_factory() as conn:
            cursor = await conn.execute(
                """
                UPDATE PHONE_MEMORY_OPERATION_LEASES
                SET provider_started=1,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND lease_token=? AND status='active' AND lease_until>=?
                """,
                (self.id, self.lease_token, now),
            )
            await conn.commit()
        if cursor.rowcount != 1:
            raise PhoneMemoryWriteBlocked("memory provider operation lease was lost")
        self.provider_started = True

    async def _finish(self, *, error: BaseException | None) -> None:
        if not self.durable:
            return
        async with self.connection_factory() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            if error is not None:
                detail = (str(error).strip() or type(error).__name__)[:1000]
                cursor = await conn.execute(
                    """
                    UPDATE PHONE_MEMORY_OPERATION_LEASES
                    SET status='needs_attention',last_error=?,lease_until=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND lease_token=? AND status='active'
                      AND provider_started=1
                    """,
                    (detail, _utc_text(), self.id, self.lease_token),
                )
                if cursor.rowcount == 1:
                    await conn.execute(
                        """
                        INSERT INTO PHONE_CONVERSATION_DATA_REVISIONS (
                            conversation_id_snapshot,owner_user_id_snapshot,
                            memory_state,memory_blocked,last_error
                        ) VALUES (?,0,'needs_attention',1,?)
                        ON CONFLICT(conversation_id_snapshot) DO UPDATE SET
                            memory_state='needs_attention',memory_blocked=1,
                            last_error=excluded.last_error,updated_at=CURRENT_TIMESTAMP
                        """,
                        (self.conversation_id, detail),
                    )
                else:
                    await conn.execute(
                        """
                        DELETE FROM PHONE_MEMORY_OPERATION_LEASES
                        WHERE id=? AND lease_token=? AND status='active'
                          AND provider_started=0
                        """,
                        (self.id, self.lease_token),
                    )
            else:
                await conn.execute(
                    """
                    DELETE FROM PHONE_MEMORY_OPERATION_LEASES
                    WHERE id=? AND lease_token=? AND status='active'
                    """,
                    (self.id, self.lease_token),
                )
            await conn.commit()


@asynccontextmanager
async def phone_memory_operation_lease(
    conversation_id: int,
    *,
    provider: str,
    operation: str,
    connection_factory: Callable[..., Any] | None = None,
    lease_seconds: float = 60.0,
):
    """Fence one provider write against phone deletion/replay across processes."""

    factory = connection_factory or database.get_db_connection
    lease_id = uuid4().hex
    lease_token = uuid4().hex
    lease_until = _utc_text(datetime.now(UTC) + timedelta(seconds=lease_seconds))
    try:
        async with factory() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "SELECT 1 FROM CONVERSATIONS WHERE id=?",
                (int(conversation_id),),
            )
            if await cursor.fetchone() is None:
                await conn.rollback()
                raise PhoneMemoryWriteBlocked("conversation no longer exists")
            await conn.execute(
                """
                INSERT OR IGNORE INTO PHONE_CONVERSATION_DATA_REVISIONS (
                    conversation_id_snapshot,owner_user_id_snapshot
                ) VALUES (
                    ?,COALESCE((SELECT user_id FROM CONVERSATIONS WHERE id=?),0)
                )
                """,
                (int(conversation_id), int(conversation_id)),
            )
            cursor = await conn.execute(
                """
                SELECT content_revision,memory_blocked,memory_state
                FROM PHONE_CONVERSATION_DATA_REVISIONS
                WHERE conversation_id_snapshot=?
                """,
                (int(conversation_id),),
            )
            row = await cursor.fetchone()
            if row is not None and (bool(row[1]) or str(row[2]) != "ready"):
                await conn.rollback()
                raise PhoneMemoryWriteBlocked("conversation memory is rebuilding")
            content_revision = int(row[0]) if row is not None else 0
            await conn.execute(
                """
                INSERT INTO PHONE_MEMORY_OPERATION_LEASES (
                    id,conversation_id_snapshot,provider,operation,lease_owner,
                    lease_token,content_revision,lease_until
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    lease_id,
                    int(conversation_id),
                    str(provider),
                    str(operation),
                    "aurvek-memory",
                    lease_token,
                    content_revision,
                    lease_until,
                ),
            )
            await conn.commit()
    except aiosqlite.OperationalError as exc:
        if "no such table" in str(exc).lower():
            async with factory(readonly=True) as conn:
                cursor = await conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='PHONE_DATA_PURGE_JOBS'"
                )
                purge_feature_exists = await cursor.fetchone() is not None
            if not purge_feature_exists:
                lease = PhoneMemoryOperationLease(
                    id=lease_id,
                    conversation_id=int(conversation_id),
                    lease_token=lease_token,
                    connection_factory=factory,
                    durable=False,
                )
                yield lease
                return
            raise PhoneMemoryWriteBlocked(
                "phone memory operation fencing schema is unavailable"
            ) from exc
        raise

    lease = PhoneMemoryOperationLease(
        id=lease_id,
        conversation_id=int(conversation_id),
        lease_token=lease_token,
        connection_factory=factory,
    )
    try:
        yield lease
    except BaseException as exc:
        finish = asyncio.create_task(lease._finish(error=exc))
        try:
            await asyncio.shield(finish)
        except asyncio.CancelledError:
            await finish
        raise
    else:
        finish = asyncio.create_task(lease._finish(error=None))
        try:
            await asyncio.shield(finish)
        except asyncio.CancelledError:
            await finish
            raise


async def phone_data_purge_runtime_operational(
    conn: Any,
    *,
    now_utc: str | None = None,
) -> bool:
    try:
        required_tables = {
            "PHONE_DATA_PURGE_JOBS",
            "PHONE_CALL_TOMBSTONES",
            "PHONE_RECORDING_TOMBSTONES",
            "PHONE_CONVERSATION_DATA_REVISIONS",
            "PHONE_DATA_PURGE_RUNTIME",
            "PHONE_MEMORY_OPERATION_LEASES",
            "PHONE_PURGED_MESSAGE_TOMBSTONES",
        }
        placeholders = ",".join("?" for _ in required_tables)
        cursor = await conn.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({placeholders})",
            tuple(sorted(required_tables)),
        )
        if {str(row[0]) for row in await cursor.fetchall()} != required_tables:
            return False
        required_columns = {
            "PHONE_DATA_PURGE_JOBS": {
                "id",
                "status",
                "conversation_id_snapshot",
                "source_snapshot_json",
                "progress_json",
                "next_attempt_at",
                "lease_owner",
                "lease_token",
                "lease_until",
                "runtime_lease_token",
                "content_revision_snapshot",
                "source_revision",
            },
            "PHONE_CALL_TOMBSTONES": {
                "call_id",
                "conversation_id_snapshot",
                "dispatch_token",
                "provider_call_sid",
                "purge_job_id",
            },
            "PHONE_RECORDING_TOMBSTONES": {
                "call_id_snapshot",
                "conversation_id_snapshot",
                "purge_job_id",
            },
            "PHONE_CONVERSATION_DATA_REVISIONS": {
                "revision",
                "content_revision",
                "memory_state",
                "memory_blocked",
                "active_job_id",
                "lease_token",
                "lease_until",
            },
            "PHONE_MEMORY_OPERATION_LEASES": {
                "lease_token",
                "provider_started",
                "status",
                "lease_until",
                "content_revision",
            },
            "PHONE_PURGED_MESSAGE_TOMBSTONES": {
                "message_id",
                "conversation_id_snapshot",
                "purge_job_id",
            },
            "PHONE_DATA_PURGE_RUNTIME": {
                "singleton",
                "worker_id",
                "lease_token",
                "lease_until",
                "heartbeat_at",
            },
        }
        for table, required in required_columns.items():
            cursor = await conn.execute(f"PRAGMA table_info({table})")
            columns = {str(row[1]) for row in await cursor.fetchall()}
            if not required.issubset(columns):
                return False
        for table in (
            "PHONE_CALL_TOMBSTONES",
            "PHONE_RECORDING_TOMBSTONES",
            "PHONE_PURGED_MESSAGE_TOMBSTONES",
        ):
            cursor = await conn.execute(f"PRAGMA foreign_key_list({table})")
            foreign_keys = {
                (str(row[2]), str(row[3]), str(row[4]))
                for row in await cursor.fetchall()
            }
            if (
                "PHONE_DATA_PURGE_JOBS",
                "purge_job_id",
                "id",
            ) not in foreign_keys:
                return False
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_phone_%'"
        )
        triggers = {str(row[0]) for row in await cursor.fetchall()}
        required_triggers = {
            "trg_phone_content_revision_message_insert",
            "trg_phone_content_revision_message_update",
            "trg_phone_content_revision_message_delete",
        }
        optional_trigger_sets = {
            "WATCHDOG_EVENTS": {
                "trg_phone_block_purged_watchdog_event_insert",
                "trg_phone_block_purged_watchdog_event_update",
            },
            "WATCHDOG_STATE": {
                "trg_phone_clear_purged_watchdog_state_insert",
                "trg_phone_clear_purged_watchdog_state_update",
            },
        }
        for table, names in optional_trigger_sets.items():
            cursor = await conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            if await cursor.fetchone() is not None:
                required_triggers.update(names)
        if not required_triggers.issubset(triggers):
            return False
        cursor = await conn.execute(
            """
            SELECT 1 FROM PHONE_DATA_PURGE_RUNTIME
            WHERE singleton=1 AND lease_until>=?
            """,
            (now_utc or _utc_text(),),
        )
        return await cursor.fetchone() is not None
    except aiosqlite.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        return False


__all__ = [
    "PhoneMemoryOperationLease",
    "PhoneMemoryWriteBlocked",
    "is_conversation_memory_blocked",
    "phone_memory_operation_lease",
    "phone_data_purge_runtime_operational",
]
