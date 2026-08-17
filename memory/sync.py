from __future__ import annotations

from dataclasses import asdict, dataclass
import asyncio
import json
from typing import Any, Literal

import aiosqlite

from chat.services.privacy import ensure_conversation_privacy_schema
import database
from log_config import logger
from memory.config import (
    ensure_memory_preference_schema,
    get_active_memory_provider,
    get_user_memory_preferences,
)


MemoryRole = Literal["user", "assistant"]
DEFAULT_BATCH_SIZE = 100
RECENT_ERROR_LIMIT = 20
_sync_lock = asyncio.Lock()
_sync_task: asyncio.Task | None = None


@dataclass(slots=True)
class MemorySyncSummary:
    run_id: int
    provider: str
    status: str
    total_messages: int = 0
    processed_messages: int = 0
    linked_messages: int = 0
    skipped_messages: int = 0
    failed_messages: int = 0
    recent_errors: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["recent_errors"] = self.recent_errors or []
        return data


async def ensure_memory_sync_schema() -> None:
    await ensure_memory_preference_schema()
    await ensure_conversation_privacy_schema()
    async with database.get_db_connection() as conn:
        await _ensure_schema(conn)
        await conn.commit()


async def record_memory_message_link(
    *,
    provider: str,
    message_id: int,
    provider_message_id: str,
    conversation_id: int,
    user_id: int,
    role: MemoryRole,
    source: str = "live",
    provider_event_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> bool:
    if not message_id or not provider_message_id:
        return False
    async with database.get_db_connection() as conn:
        await _ensure_schema(conn)
        changed = await _insert_message_link(
            conn,
            provider=provider,
            message_id=message_id,
            provider_message_id=provider_message_id,
            provider_event_id=provider_event_id,
            conversation_id=conversation_id,
            user_id=user_id,
            role=role,
            source=source,
            metadata=metadata or {},
        )
        await conn.commit()
        return changed


async def record_memory_conversation_link(
    *,
    provider: str,
    conversation_id: int,
    user_id: int,
    source: str = "live",
    metadata: dict[str, Any] | None = None,
) -> bool:
    if not provider or not conversation_id:
        return False
    async with database.get_db_connection() as conn:
        await _ensure_schema(conn)
        cursor = await conn.execute(
            """
            INSERT INTO MEMORY_PROVIDER_CONVERSATION_LINKS
                (provider, conversation_id, user_id, source, metadata_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(provider, conversation_id) DO UPDATE SET
                user_id = excluded.user_id,
                source = excluded.source,
                last_seen_at = CURRENT_TIMESTAMP,
                metadata_json = excluded.metadata_json
            """,
            (
                provider,
                conversation_id,
                user_id,
                source,
                json.dumps(metadata or {}),
            ),
        )
        await conn.commit()
        return bool(cursor.rowcount)


async def get_memory_sync_status(provider: str | None = None) -> dict[str, Any]:
    await ensure_memory_sync_schema()
    active_provider = provider or await get_active_memory_provider()
    async with database.get_db_connection(readonly=True) as conn:
        conn.row_factory = aiosqlite.Row
        run_cursor = await conn.execute(
            """
            SELECT *
            FROM MEMORY_PROVIDER_SYNC_RUNS
            WHERE provider = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (active_provider,),
        )
        run = await run_cursor.fetchone()
        linked_cursor = await conn.execute(
            "SELECT COUNT(*) AS linked_count FROM MEMORY_PROVIDER_MESSAGE_LINKS WHERE provider = ?",
            (active_provider,),
        )
        linked_row = await linked_cursor.fetchone()
        pending_cursor = await conn.execute(
            """
            SELECT COUNT(*) AS pending_count
            FROM MESSAGES m
            JOIN CONVERSATIONS c ON c.id = m.conversation_id
            LEFT JOIN MEMORY_PROVIDER_MESSAGE_LINKS l
              ON l.message_id = m.id AND l.provider = ?
            LEFT JOIN MEMORY_USER_PREFERENCES p
              ON p.user_id = COALESCE(m.user_id, c.user_id) AND p.provider = ?
            WHERE l.message_id IS NULL
              AND m.type IN ('user', 'bot', 'assistant')
              AND COALESCE(c.hidden_from_history, 0) = 0
              AND COALESCE(p.remember_across_chats, 1) = 1
            """,
            (active_provider, active_provider),
        )
        pending_row = await pending_cursor.fetchone()
    return {
        "provider": active_provider,
        "latest_run": _row_to_dict(run) if run else None,
        "linked_messages": int(linked_row["linked_count"] if linked_row else 0),
        "pending_messages": int(pending_row["pending_count"] if pending_row else 0),
    }


async def start_mem0_history_sync(batch_size: int = DEFAULT_BATCH_SIZE) -> dict[str, Any]:
    global _sync_task
    async with _sync_lock:
        if _sync_task is not None and not _sync_task.done():
            return {
                "started": False,
                "message": "Mem0 history sync is already running.",
                "status": await get_memory_sync_status("mem0"),
            }
        run_id = await _create_sync_run("mem0")
        _sync_task = asyncio.create_task(_run_mem0_sync_task(run_id=run_id, batch_size=batch_size))
        return {
            "started": True,
            "run_id": run_id,
            "message": "Mem0 history sync started.",
        }


async def sync_mem0_history(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    provider: Any | None = None,
    run_id: int | None = None,
) -> MemorySyncSummary:
    await ensure_memory_sync_schema()
    if run_id is None:
        run_id = await _create_sync_run("mem0")
    summary = MemorySyncSummary(
        run_id=run_id,
        provider="mem0",
        status="running",
        total_messages=await _count_unlinked_messages("mem0"),
        recent_errors=[],
    )
    await _update_sync_run(run_id, total_messages=summary.total_messages)
    if provider is None:
        from memory.providers.mem0 import get_mem0_provider

        provider = await get_mem0_provider()

    after_message_id = 0
    try:
        while True:
            rows = await _fetch_unlinked_message_batch(
                provider="mem0",
                after_message_id=after_message_id,
                batch_size=batch_size,
            )
            if not rows:
                break
            for row in rows:
                after_message_id = max(after_message_id, int(row["id"]))
                await _sync_one_mem0_message(row, provider, summary)
                await _update_sync_run_from_summary(summary)
        final_status = (
            "completed_with_errors"
            if summary.failed_messages or summary.recent_errors
            else "completed"
        )
        await _mark_run_finished(summary, final_status)
        return summary
    except Exception as exc:
        await _mark_run_finished(summary, "failed", error=str(exc))
        return summary


async def _run_mem0_sync_task(*, run_id: int, batch_size: int) -> None:
    try:
        await sync_mem0_history(batch_size=batch_size, run_id=run_id)
    except Exception:
        logger.error("Mem0 history sync task failed", exc_info=True)


async def _sync_one_mem0_message(
    row: aiosqlite.Row,
    provider: Any,
    summary: MemorySyncSummary,
) -> None:
    message_id = int(row["id"])
    conversation_id = int(row["conversation_id"])
    user_id = int(row["user_id"])
    role = _role_for_message_type(str(row["type"]))
    text = _message_text_for_memory_sync(row["message"]).strip()
    summary.processed_messages += 1
    if not text:
        summary.skipped_messages += 1
        await _update_sync_state("mem0", conversation_id, message_id)
        return
    preferences = await get_user_memory_preferences(user_id, "mem0")
    if preferences.get("remember_across_chats") is False:
        summary.skipped_messages += 1
        await _update_sync_state("mem0", conversation_id, message_id)
        return

    result = await provider.add_message(
        user_id=user_id,
        conversation_id=conversation_id,
        role=role,
        text=text,
        occurred_at=row["date"],
        prompt_id=row["prompt_id"],
        message_id=message_id,
        incognito=False,
    )
    if result is None:
        summary.failed_messages += 1
        _add_recent_error(summary, f"message {message_id}: Mem0 add failed")
        return

    async with database.get_db_connection() as conn:
        await _ensure_schema(conn)
        changed = await _insert_message_link(
            conn,
            provider="mem0",
            message_id=message_id,
            provider_message_id=_mem0_provider_message_id(message_id, result),
            provider_event_id=_extract_provider_event_id(result),
            conversation_id=conversation_id,
            user_id=user_id,
            role=role,
            source="backfill",
            metadata=result,
        )
        await conn.commit()
    summary.linked_messages += 1 if changed else 0
    summary.skipped_messages += 0 if changed else 1
    await _update_sync_state("mem0", conversation_id, message_id)


async def _ensure_schema(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS MEMORY_PROVIDER_CONVERSATION_LINKS (
            provider TEXT NOT NULL,
            conversation_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'live',
            first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (provider, conversation_id)
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS MEMORY_PROVIDER_MESSAGE_LINKS (
            message_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            provider_message_id TEXT NOT NULL,
            provider_event_id TEXT,
            conversation_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            source TEXT NOT NULL DEFAULT 'live',
            synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (message_id, provider),
            FOREIGN KEY (message_id) REFERENCES MESSAGES(id)
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS MEMORY_PROVIDER_SYNC_RUNS (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT,
            total_messages INTEGER NOT NULL DEFAULT 0,
            processed_messages INTEGER NOT NULL DEFAULT 0,
            linked_messages INTEGER NOT NULL DEFAULT 0,
            skipped_messages INTEGER NOT NULL DEFAULT 0,
            failed_messages INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            recent_errors TEXT NOT NULL DEFAULT '[]'
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS MEMORY_PROVIDER_SYNC_STATE (
            provider TEXT NOT NULL,
            conversation_id INTEGER NOT NULL,
            last_message_id INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (provider, conversation_id)
        )
        """
    )
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_provider_links_conversation
        ON MEMORY_PROVIDER_MESSAGE_LINKS(provider, conversation_id, message_id)
        """
    )


async def _insert_message_link(
    conn: aiosqlite.Connection,
    *,
    provider: str,
    message_id: int,
    provider_message_id: str,
    provider_event_id: str | None,
    conversation_id: int,
    user_id: int,
    role: MemoryRole,
    source: str,
    metadata: dict[str, Any],
) -> bool:
    cursor = await conn.execute(
        """
        INSERT OR IGNORE INTO MEMORY_PROVIDER_MESSAGE_LINKS
            (message_id, provider, provider_message_id, provider_event_id,
             conversation_id, user_id, role, source, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            provider,
            provider_message_id,
            provider_event_id,
            conversation_id,
            user_id,
            role,
            source,
            json.dumps(metadata),
        ),
    )
    return bool(cursor.rowcount)


async def _create_sync_run(provider: str) -> int:
    async with database.get_db_connection() as conn:
        await _ensure_schema(conn)
        cursor = await conn.execute(
            "INSERT INTO MEMORY_PROVIDER_SYNC_RUNS (provider, status) VALUES (?, 'running') RETURNING id",
            (provider,),
        )
        row = await cursor.fetchone()
        await conn.commit()
    return int(row[0])


async def _count_unlinked_messages(provider: str) -> int:
    async with database.get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM MESSAGES m
            JOIN CONVERSATIONS c ON c.id = m.conversation_id
            LEFT JOIN MEMORY_PROVIDER_MESSAGE_LINKS l
              ON l.message_id = m.id AND l.provider = ?
            LEFT JOIN MEMORY_USER_PREFERENCES p
              ON p.user_id = COALESCE(m.user_id, c.user_id) AND p.provider = ?
            WHERE l.message_id IS NULL
              AND m.type IN ('user', 'bot', 'assistant')
              AND COALESCE(c.hidden_from_history, 0) = 0
              AND COALESCE(p.remember_across_chats, 1) = 1
            """,
            (provider, provider),
        )
        row = await cursor.fetchone()
    return int(row[0] if row else 0)


async def _fetch_unlinked_message_batch(
    *,
    provider: str,
    after_message_id: int,
    batch_size: int,
) -> list[aiosqlite.Row]:
    async with database.get_db_connection(readonly=True) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """
            SELECT
                m.id,
                m.conversation_id,
                COALESCE(m.user_id, c.user_id) AS user_id,
                m.message,
                m.type,
                m.date,
                c.role_id AS prompt_id
            FROM MESSAGES m
            JOIN CONVERSATIONS c ON c.id = m.conversation_id
            LEFT JOIN MEMORY_PROVIDER_MESSAGE_LINKS l
              ON l.message_id = m.id AND l.provider = ?
            LEFT JOIN MEMORY_USER_PREFERENCES p
              ON p.user_id = COALESCE(m.user_id, c.user_id) AND p.provider = ?
            WHERE m.id > ?
              AND l.message_id IS NULL
              AND m.type IN ('user', 'bot', 'assistant')
              AND COALESCE(c.hidden_from_history, 0) = 0
              AND COALESCE(p.remember_across_chats, 1) = 1
            ORDER BY m.id ASC
            LIMIT ?
            """,
            (provider, provider, after_message_id, max(1, int(batch_size))),
        )
        rows = await cursor.fetchall()
    return list(rows)


async def _update_sync_state(provider: str, conversation_id: int, message_id: int) -> None:
    async with database.get_db_connection() as conn:
        await _ensure_schema(conn)
        await conn.execute(
            """
            INSERT INTO MEMORY_PROVIDER_SYNC_STATE
                (provider, conversation_id, last_message_id, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(provider, conversation_id) DO UPDATE SET
                last_message_id = MAX(last_message_id, excluded.last_message_id),
                updated_at = CURRENT_TIMESTAMP
            """,
            (provider, conversation_id, message_id),
        )
        await conn.commit()


async def _update_sync_run_from_summary(summary: MemorySyncSummary) -> None:
    await _update_sync_run(
        summary.run_id,
        total_messages=summary.total_messages,
        processed_messages=summary.processed_messages,
        linked_messages=summary.linked_messages,
        skipped_messages=summary.skipped_messages,
        failed_messages=summary.failed_messages,
        recent_errors=summary.recent_errors or [],
        last_error=(summary.recent_errors or [None])[-1],
    )


async def _update_sync_run(run_id: int, **fields: Any) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = [json.dumps(value) if key == "recent_errors" else value for key, value in fields.items()]
    async with database.get_db_connection() as conn:
        await _ensure_schema(conn)
        await conn.execute(
            f"UPDATE MEMORY_PROVIDER_SYNC_RUNS SET {assignments} WHERE id = ?",
            (*values, run_id),
        )
        await conn.commit()


async def _mark_run_finished(
    summary: MemorySyncSummary,
    status: str,
    *,
    error: str | None = None,
) -> None:
    if error:
        summary.failed_messages += 1
        _add_recent_error(summary, error)
    summary.status = status
    async with database.get_db_connection() as conn:
        await _ensure_schema(conn)
        await conn.execute(
            """
            UPDATE MEMORY_PROVIDER_SYNC_RUNS
            SET
                status = ?,
                finished_at = CURRENT_TIMESTAMP,
                total_messages = ?,
                processed_messages = ?,
                linked_messages = ?,
                skipped_messages = ?,
                failed_messages = ?,
                last_error = ?,
                recent_errors = ?
            WHERE id = ?
            """,
            (
                status,
                summary.total_messages,
                summary.processed_messages,
                summary.linked_messages,
                summary.skipped_messages,
                summary.failed_messages,
                error or ((summary.recent_errors or [None])[-1]),
                json.dumps(summary.recent_errors or []),
                summary.run_id,
            ),
        )
        await conn.commit()


def _message_text_for_memory_sync(value: Any) -> str:
    try:
        from ai_runtime.memory.context import _message_text_for_memory
        from ai_runtime.context.formatting import parse_stored_message
        from common import custom_unescape

        normalized = custom_unescape(value) if isinstance(value, str) else value
        return _message_text_for_memory(parse_stored_message(normalized))
    except Exception:
        return "" if value is None else str(value)


def _role_for_message_type(message_type: str) -> MemoryRole:
    return "user" if message_type == "user" else "assistant"


def _extract_provider_event_id(result: dict[str, Any]) -> str | None:
    for key in ("event_id", "id", "request_id"):
        value = result.get(key)
        if value:
            return str(value)
    results = result.get("results") or result.get("memories")
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            value = first.get("id") or first.get("memory_id")
            if value:
                return str(value)
    return None


def _mem0_provider_message_id(message_id: int | str, result: dict[str, Any]) -> str:
    event_id = _extract_provider_event_id(result)
    if event_id:
        return f"mem0:{event_id}"
    return f"mem0:aurvek:msg:{message_id}"


def _add_recent_error(summary: MemorySyncSummary, message: str) -> None:
    errors = summary.recent_errors if summary.recent_errors is not None else []
    errors.append(message)
    del errors[:-RECENT_ERROR_LIMIT]
    summary.recent_errors = errors


def _row_to_dict(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    raw_errors = data.get("recent_errors")
    try:
        data["recent_errors"] = json.loads(raw_errors) if raw_errors else []
    except Exception:
        data["recent_errors"] = []
    return data
