"""Conversation privacy helpers for local Aurvek chat state."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite

import database
from log_config import logger


_PRIVACY_COLUMNS: dict[str, str] = {
    "is_incognito": (
        "is_incognito INTEGER NOT NULL DEFAULT 0 CHECK(is_incognito IN (0, 1))"
    ),
    "hidden_from_history": (
        "hidden_from_history INTEGER NOT NULL DEFAULT 0 "
        "CHECK(hidden_from_history IN (0, 1))"
    ),
    "purge_on_close": (
        "purge_on_close INTEGER NOT NULL DEFAULT 0 CHECK(purge_on_close IN (0, 1))"
    ),
    "incognito_closed_at": "incognito_closed_at TEXT",
}

_PRIVACY_SCHEMA_VERSION = 1
_privacy_schema_ready: set[tuple[str, int]] = set()
_privacy_schema_lock = asyncio.Lock()
_DEFAULT_DATABASE_CONNECTION_FACTORY = database.get_db_connection


async def ensure_conversation_privacy_schema(
    conn: aiosqlite.Connection | None = None,
) -> None:
    """Add conversation privacy columns idempotently."""
    if conn is None:
        configured_key = _configured_database_key()
        if configured_key is not None and configured_key in _privacy_schema_ready:
            return
        async with database.get_db_connection() as owned_conn:
            await _ensure_privacy_schema_once(owned_conn)
        return

    await _ensure_privacy_schema_once(conn)


async def _resolved_database_identity(conn: aiosqlite.Connection) -> str:
    marked_identity = getattr(conn, "_aurvek_database_identity", None)
    if marked_identity:
        return str(marked_identity)
    cursor = await conn.execute("PRAGMA database_list")
    try:
        rows = await cursor.fetchall()
    finally:
        await cursor.close()
    for _, name, filename in rows:
        if name == "main":
            if filename:
                return str(Path(filename).resolve())
            return f":memory:{id(conn)}"
    return f":connection:{id(conn)}"


def _configured_database_key() -> tuple[str, int] | None:
    """Return the native factory's DB key, or None for patched/custom factories."""
    if database.get_db_connection is not _DEFAULT_DATABASE_CONNECTION_FACTORY:
        return None
    return (database.get_database_identity(), _PRIVACY_SCHEMA_VERSION)


async def _ensure_privacy_schema_once(conn: aiosqlite.Connection) -> None:
    key = (await _resolved_database_identity(conn), _PRIVACY_SCHEMA_VERSION)
    if key in _privacy_schema_ready:
        return

    async with _privacy_schema_lock:
        if key in _privacy_schema_ready:
            return
        had_transaction = conn.in_transaction
        await _ensure_schema_on_connection(conn)
        if had_transaction:
            # Never commit a caller's business transaction. Startup (or an
            # explicit fixture initialization) will mark this DB on a clean
            # connection after the schema transaction is durable.
            return
        await conn.commit()
        _privacy_schema_ready.add(key)


def reset_conversation_privacy_schema_guard() -> None:
    """Reset the once-guard for fixtures that switch or recreate databases."""
    _privacy_schema_ready.clear()


async def mark_conversation_incognito(
    conn: aiosqlite.Connection,
    *,
    conversation_id: int,
    user_id: int,
    incognito: bool,
) -> bool:
    """Set privacy flags and remove phone routing in the same transaction."""
    await ensure_conversation_privacy_schema(conn)
    if not incognito:
        cursor = await conn.execute(
            """
            UPDATE CONVERSATIONS
            SET is_incognito = 0,
                hidden_from_history = 0,
                purge_on_close = 0
            WHERE id = ?
              AND user_id = ?
            """,
            (conversation_id, user_id),
        )
        return bool(cursor.rowcount)

    # Preserve a caller-owned transaction and still make a caught telephony
    # conflict roll back only this privacy transition.
    had_transaction = conn.in_transaction
    savepoint = "aurvek_incognito_phone_transition"
    if had_transaction:
        await conn.execute(f"SAVEPOINT {savepoint}")
    else:
        await conn.execute("BEGIN")
    try:
        from integrations.telephony.repository import TelephonyRepository

        repository = TelephonyRepository()
        await repository.disable_phone_for_incognito_in_transaction(
            conn,
            owner_user_id=int(user_id),
            conversation_id=int(conversation_id),
        )
        cursor = await conn.execute(
            """
            UPDATE CONVERSATIONS
            SET is_incognito = 1,
                hidden_from_history = 1,
                purge_on_close = 1
            WHERE id = ?
              AND user_id = ?
            """,
            (conversation_id, user_id),
        )
        changed = bool(cursor.rowcount)
        if not changed:
            raise ValueError("Conversation not found")
    except Exception:
        if had_transaction:
            await conn.execute(f"ROLLBACK TO {savepoint}")
            await conn.execute(f"RELEASE {savepoint}")
        else:
            await conn.rollback()
        raise
    else:
        if had_transaction:
            await conn.execute(f"RELEASE {savepoint}")
        return True


async def get_conversation_privacy(
    conversation_id: int,
    *,
    user_id: int | None = None,
) -> dict[str, Any] | None:
    """Return local privacy flags and prompt ownership for a conversation."""
    await ensure_conversation_privacy_schema()
    async with database.get_db_connection(readonly=True) as conn:
        conn.row_factory = aiosqlite.Row
        params: list[Any] = [conversation_id]
        user_filter = ""
        if user_id is not None:
            user_filter = " AND user_id = ?"
            params.append(user_id)
        cursor = await conn.execute(
            f"""
            SELECT id, user_id, role_id,
                   COALESCE(is_incognito, 0) AS is_incognito,
                   COALESCE(hidden_from_history, 0) AS hidden_from_history,
                   COALESCE(purge_on_close, 0) AS purge_on_close,
                   incognito_closed_at
            FROM CONVERSATIONS
            WHERE id = ?{user_filter}
            """,
            params,
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def is_incognito_conversation(
    conversation_id: int,
    *,
    user_id: int | None = None,
) -> bool:
    row = await get_conversation_privacy(conversation_id, user_id=user_id)
    return bool(row and row.get("is_incognito"))


async def purge_conversation_local_records(
    *,
    conversation_id: int,
    user_id: int,
    memory_link_providers_to_delete: set[str] | None = None,
) -> bool:
    """Delete local records for an incognito conversation after close."""
    await ensure_conversation_privacy_schema()
    async with database.get_db_connection() as conn:
        await ensure_conversation_privacy_schema(conn)
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """
            SELECT id, user_id, COALESCE(is_incognito, 0) AS is_incognito
            FROM CONVERSATIONS
            WHERE id = ?
              AND user_id = ?
            """,
            (conversation_id, user_id),
        )
        row = await cursor.fetchone()
        if row is None:
            await conn.commit()
            return False
        if not bool(row["is_incognito"]):
            await conn.commit()
            raise ValueError("Conversation is not incognito")

        await delete_conversation_rows(
            conn,
            conversation_id=conversation_id,
            user_id=user_id,
            memory_link_providers_to_delete=memory_link_providers_to_delete,
        )
        await conn.commit()
        return True


async def delete_message_rows(
    conn: aiosqlite.Connection,
    *,
    conversation_id: int,
    message_ids: list[int],
) -> None:
    """Delete an explicit set of messages and their coordinated local state.

    Shared by conversation rollback and empty-message auto-repair so that no
    caller deletes MESSAGES directly and leaves memory-provider links, watchdog
    rows, or sync watermarks pointing at rows that no longer exist. Preserves the
    caller's transaction.
    """
    message_ids = [int(mid) for mid in message_ids]
    if not message_ids:
        return
    id_placeholders = ",".join("?" for _ in message_ids)

    # Provider message links reference MESSAGES(id) with no cascade, so they must
    # go before the messages themselves. The messages are being removed entirely,
    # so links for every provider are deleted.
    await _delete_provider_message_links(conn, message_ids, providers=None)

    # WATCHDOG_EVENTS keeps plain user_message_id/bot_message_id columns (no FK).
    # Drop the events that referenced any of the removed messages.
    if await _table_exists(conn, "WATCHDOG_EVENTS"):
        await conn.execute(
            f"""
            DELETE FROM WATCHDOG_EVENTS
            WHERE user_message_id IN ({id_placeholders})
               OR bot_message_id IN ({id_placeholders})
            """,
            [*message_ids, *message_ids],
        )

    # FILE_ATTACHMENTS cascade from MESSAGES(id); blob pruning is the caller's job.
    if await _table_exists(conn, "WATCHDOG_STATE"):
        await conn.execute(
            f"""
            DELETE FROM WATCHDOG_STATE
            WHERE conversation_id = ?
              AND last_evaluated_message_id IN ({id_placeholders})
            """,
            [conversation_id, *message_ids],
        )
    await conn.execute(
        f"DELETE FROM messages WHERE id IN ({id_placeholders})",
        message_ids,
    )

    # Recompute the highest surviving message id for this conversation so any
    # watermark pointing above it can be clamped or regenerated.
    cursor = await conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM MESSAGES WHERE conversation_id = ?",
        (conversation_id,),
    )
    max_surviving_id = int((await cursor.fetchone())[0])

    # MEMORY_PROVIDER_SYNC_STATE.last_message_id is a per-conversation watermark.
    # Clamp it down when it points above the surviving tail so the next sync run
    # does not skip re-added messages.
    if await _table_exists(conn, "MEMORY_PROVIDER_SYNC_STATE"):
        await conn.execute(
            """
            UPDATE MEMORY_PROVIDER_SYNC_STATE
            SET last_message_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE conversation_id = ? AND last_message_id > ?
            """,
            (max_surviving_id, conversation_id, max_surviving_id),
        )

    # WATCHDOG_STATE.last_evaluated_message_id can reference a now-deleted message.
    # Delete the stale row so the watchdog regenerates it cleanly on the next turn
    # (its upsert recreates the row); rows still pointing at surviving messages are
    # left untouched.
    if await _table_exists(conn, "WATCHDOG_STATE"):
        await conn.execute(
            """
            DELETE FROM WATCHDOG_STATE
            WHERE conversation_id = ? AND last_evaluated_message_id > ?
            """,
            (conversation_id, max_surviving_id),
        )


async def delete_conversation_rows(
    conn: aiosqlite.Connection,
    *,
    conversation_id: int,
    user_id: int | None = None,
    memory_link_providers_to_delete: set[str] | None = None,
) -> None:
    """Delete local rows owned by one conversation, preserving caller transaction."""
    # Capture provider identities and private recording paths before FK cascades
    # remove the mutable phone rows.  The purge jobs and tombstones intentionally
    # survive conversation/user deletion through their snapshot columns.
    from integrations.telephony.purge import PhoneDataPurgeRepository

    await PhoneDataPurgeRepository().stage_conversation_purges_in_transaction(
        conn,
        conversation_id=conversation_id,
        owner_user_id=user_id,
    )
    message_ids: list[int] = []
    cursor = await conn.execute(
        "SELECT id FROM MESSAGES WHERE conversation_id = ?",
        (conversation_id,),
    )
    rows = await cursor.fetchall()
    message_ids = [int(row[0]) for row in rows]

    await _delete_provider_message_links(
        conn, message_ids, providers=memory_link_providers_to_delete
    )

    if await _table_exists(conn, "MEMORY_PROVIDER_CONVERSATION_LINKS"):
        providers = memory_link_providers_to_delete
        if providers is None:
            await conn.execute(
                "DELETE FROM MEMORY_PROVIDER_CONVERSATION_LINKS WHERE conversation_id = ?",
                (conversation_id,),
            )
        else:
            provider_names = sorted(providers)
            if provider_names:
                provider_placeholders = ",".join("?" for _ in provider_names)
                await conn.execute(
                    f"""
                    DELETE FROM MEMORY_PROVIDER_CONVERSATION_LINKS
                    WHERE conversation_id = ?
                      AND provider IN ({provider_placeholders})
                    """,
                    [conversation_id, *provider_names],
                )

    try:
        from file_storage import delete_attachments_for_conversation

        await delete_attachments_for_conversation(
            conn,
            conversation_id=conversation_id,
        )
    except Exception:
        logger.exception(
            "Failed to delete file attachments for conversation_id=%s",
            conversation_id,
        )
        raise

    await conn.execute(
        "DELETE FROM WATCHDOG_STATE WHERE conversation_id = ?",
        (conversation_id,),
    )
    await conn.execute(
        "DELETE FROM WATCHDOG_EVENTS WHERE conversation_id = ?",
        (conversation_id,),
    )
    # The conversation is being removed, so its per-conversation memory sync
    # watermark is orphaned regardless of which providers were purged remotely.
    if await _table_exists(conn, "MEMORY_PROVIDER_SYNC_STATE"):
        await conn.execute(
            "DELETE FROM MEMORY_PROVIDER_SYNC_STATE WHERE conversation_id = ?",
            (conversation_id,),
        )
    await conn.execute(
        "DELETE FROM messages WHERE conversation_id = ?",
        (conversation_id,),
    )

    if user_id is None:
        await conn.execute(
            "DELETE FROM conversations WHERE id = ?",
            (conversation_id,),
        )
    else:
        await conn.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        )


async def _ensure_schema_on_connection(conn: aiosqlite.Connection) -> None:
    try:
        cursor = await conn.execute("PRAGMA table_info(CONVERSATIONS)")
        columns = {str(row[1]) for row in await cursor.fetchall()}
        for name, definition in _PRIVACY_COLUMNS.items():
            if name not in columns:
                await conn.execute(f"ALTER TABLE CONVERSATIONS ADD COLUMN {definition}")
        if "folder_id" in columns and "last_activity" in columns:
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversations_history_visible
                ON CONVERSATIONS(user_id, folder_id, hidden_from_history, last_activity, id)
                """
            )
        else:
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversations_history_visible
                ON CONVERSATIONS(user_id, hidden_from_history, id)
                """
            )
    except Exception:
        logger.exception("Failed to ensure conversation privacy schema")
        raise


async def _delete_provider_message_links(
    conn: aiosqlite.Connection,
    message_ids: list[int],
    *,
    providers: set[str] | None,
) -> None:
    """Delete MEMORY_PROVIDER_MESSAGE_LINKS rows for the given message ids.

    ``providers=None`` removes links for every provider; otherwise only the named
    providers are removed. Shared by full-conversation and per-message deletion.
    """
    if not message_ids:
        return
    if not await _table_exists(conn, "MEMORY_PROVIDER_MESSAGE_LINKS"):
        return
    message_placeholders = ",".join("?" for _ in message_ids)
    if providers is None:
        await conn.execute(
            f"DELETE FROM MEMORY_PROVIDER_MESSAGE_LINKS WHERE message_id IN ({message_placeholders})",
            list(message_ids),
        )
        return
    provider_names = sorted(providers)
    if not provider_names:
        return
    provider_placeholders = ",".join("?" for _ in provider_names)
    await conn.execute(
        f"""
        DELETE FROM MEMORY_PROVIDER_MESSAGE_LINKS
        WHERE message_id IN ({message_placeholders})
          AND provider IN ({provider_placeholders})
        """,
        [*message_ids, *provider_names],
    )


async def _table_exists(conn: aiosqlite.Connection, table_name: str) -> bool:
    cursor = await conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,),
    )
    return await cursor.fetchone() is not None
