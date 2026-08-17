"""Manual historical sync from Aurvek chat history into Atagia."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import json
import logging
from typing import Any, Literal

import aiosqlite

import database
from chat.services.privacy import ensure_conversation_privacy_schema

logger = logging.getLogger(__name__)

AtagiaRole = Literal["user", "assistant"]

# Atagia stores its links in the shared generic memory-provider tables under
# this provider name (coordinated with the deletion coordinator).
ATAGIA_PROVIDER = "atagia"

RECENT_ERROR_LIMIT = 20
DEFAULT_BATCH_SIZE = 100
TRANSIENT_INGEST_RETRY_DELAYS_SECONDS = (1.0, 3.0, 7.0)

_sync_lock = asyncio.Lock()
_sync_task: asyncio.Task | None = None


@dataclass(slots=True)
class AtagiaSyncSummary:
    run_id: int
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


async def ensure_atagia_sync_schema() -> None:
    """Create Atagia sync tables idempotently."""
    async with database.get_db_connection() as conn:
        await ensure_conversation_privacy_schema(conn)
        await _ensure_schema(conn)
        await conn.commit()


async def record_atagia_message_link(
    *,
    message_id: int,
    atagia_message_id: str,
    conversation_id: int,
    user_id: int,
    role: AtagiaRole,
    source: str = "live",
) -> bool:
    """Persist the Aurvek message -> Atagia message id mapping."""
    if not message_id or not atagia_message_id:
        return False

    async with database.get_db_connection() as conn:
        await _ensure_schema(conn)
        changed = await _insert_message_link(
            conn,
            message_id=message_id,
            atagia_message_id=atagia_message_id,
            conversation_id=conversation_id,
            user_id=user_id,
            role=role,
            source=source,
        )
        await conn.commit()
        return changed


async def start_atagia_history_sync(batch_size: int = DEFAULT_BATCH_SIZE) -> dict[str, Any]:
    """Start a process-local background sync run."""
    global _sync_task
    async with _sync_lock:
        if _sync_task is not None and not _sync_task.done():
            status = await get_atagia_sync_status()
            return {
                "started": False,
                "message": "Atagia history sync is already running.",
                "status": status,
            }

        run_id = await _create_sync_run()
        _sync_task = asyncio.create_task(
            _run_sync_task(run_id=run_id, batch_size=batch_size)
        )
        return {
            "started": True,
            "run_id": run_id,
            "message": "Atagia history sync started.",
        }


async def sync_all_history(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    bridge: Any | None = None,
    run_id: int | None = None,
) -> AtagiaSyncSummary:
    """Backfill all unlinked Aurvek messages into Atagia."""
    await ensure_atagia_sync_schema()
    if run_id is None:
        run_id = await _create_sync_run()

    total = await _count_unlinked_messages()
    summary = AtagiaSyncSummary(
        run_id=run_id,
        status="running",
        total_messages=total,
        recent_errors=[],
    )
    await _update_sync_run(run_id, total_messages=total)

    if bridge is None:
        try:
            from atagia_config import get_atagia_bridge_config
            from atagia_bridge import get_atagia_bridge

            config = await get_atagia_bridge_config()
            if not config.enabled:
                raise RuntimeError("Atagia is disabled.")
            bridge = get_atagia_bridge()
        except Exception as exc:
            await _mark_run_finished(
                summary,
                "failed",
                error=f"Could not initialize Atagia bridge: {exc}",
            )
            return summary

    after_message_id = 0
    try:
        while True:
            rows = await _fetch_unlinked_message_batch(
                after_message_id=after_message_id,
                batch_size=batch_size,
            )
            if not rows:
                break

            for row in rows:
                after_message_id = max(after_message_id, int(row["id"]))
                await _sync_one_message(row, bridge, summary)
                await _update_sync_run_from_summary(summary)

        if hasattr(bridge, "flush"):
            try:
                await bridge.flush()
            except Exception as exc:
                _add_recent_error(summary, f"flush failed: {exc}")

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


async def get_atagia_sync_status() -> dict[str, Any]:
    """Return latest sync run and aggregate link counts for admin UI."""
    await ensure_atagia_sync_schema()
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
            (ATAGIA_PROVIDER,),
        )
        run = await run_cursor.fetchone()
        count_cursor = await conn.execute(
            "SELECT COUNT(*) AS linked_count FROM MEMORY_PROVIDER_MESSAGE_LINKS "
            "WHERE provider = ?",
            (ATAGIA_PROVIDER,),
        )
        linked_row = await count_cursor.fetchone()
        pending_cursor = await conn.execute(
            """
            SELECT COUNT(*) AS pending_count
            FROM MESSAGES m
            JOIN CONVERSATIONS c ON c.id = m.conversation_id
            LEFT JOIN MEMORY_PROVIDER_MESSAGE_LINKS l
                ON l.message_id = m.id AND l.provider = ?
            WHERE l.message_id IS NULL
              AND m.type IN ('user', 'bot')
              AND COALESCE(c.hidden_from_history, 0) = 0
              AND EXISTS (
                  SELECT 1 FROM USERS u
                  WHERE u.id = COALESCE(m.user_id, c.user_id)
              )
            """,
            (ATAGIA_PROVIDER,),
        )
        pending_row = await pending_cursor.fetchone()

    return {
        "latest_run": _row_to_dict(run) if run else None,
        "linked_messages": int(linked_row["linked_count"] if linked_row else 0),
        "pending_messages": int(pending_row["pending_count"] if pending_row else 0),
    }


async def _run_sync_task(*, run_id: int, batch_size: int) -> None:
    try:
        await sync_all_history(batch_size=batch_size, run_id=run_id)
    except Exception:
        logger.error("Atagia history sync task failed", exc_info=True)


async def _sync_one_message(
    row: aiosqlite.Row,
    bridge: Any,
    summary: AtagiaSyncSummary,
) -> None:
    message_id = int(row["id"])
    conversation_id = int(row["conversation_id"])
    user_id = int(row["user_id"])
    role = _role_for_message_type(str(row["type"]))
    prompt_id = row["prompt_id"]
    text = _message_text_for_atagia_sync(row["message"]).strip()

    summary.processed_messages += 1

    if not text:
        summary.skipped_messages += 1
        await _update_sync_state(conversation_id, message_id)
        return

    ingest_kwargs = {
        "user_id": user_id,
        "conversation_id": conversation_id,
        "role": role,
        "text": text,
        "occurred_at": row["date"],
        "prompt_id": prompt_id,
        "message_id": message_id,
        "source_seq": message_id,
        "ingest_origin": "backfill",
        "confirmation_strategy": "admin_review_only",
    }

    try:
        ok = await _ingest_with_transient_retries(
            bridge,
            message_id=message_id,
            ingest_kwargs=ingest_kwargs,
        )
    except Exception as exc:
        ok = False
        _add_recent_error(summary, f"message {message_id}: {exc}")

    if not ok:
        summary.failed_messages += 1
        if not summary.recent_errors or not any(
            item.startswith(f"message {message_id}:") for item in summary.recent_errors
        ):
            error_detail = _format_bridge_last_error(bridge)
            message = "Atagia ingest failed"
            if error_detail:
                message = f"{message}: {error_detail}"
            _add_recent_error(summary, f"message {message_id}: {message}")
        return

    async with database.get_db_connection() as conn:
        await _ensure_schema(conn)
        changed = await _insert_message_link(
            conn,
            message_id=message_id,
            atagia_message_id=_aurvek_atagia_message_id(message_id),
            conversation_id=conversation_id,
            user_id=user_id,
            role=role,
            source="backfill",
        )
        await conn.commit()

    summary.linked_messages += 1 if changed else 0
    summary.skipped_messages += 0 if changed else 1
    await _update_sync_state(conversation_id, message_id)


async def _ingest_with_transient_retries(
    bridge: Any,
    *,
    message_id: int,
    ingest_kwargs: dict[str, Any],
) -> bool:
    for attempt_index, delay_seconds in enumerate(
        (*TRANSIENT_INGEST_RETRY_DELAYS_SECONDS, None)
    ):
        try:
            ok = await bridge.ingest_message(**ingest_kwargs)
        except Exception as exc:
            if delay_seconds is None or not _is_transient_sqlite_lock_error(exc):
                raise
            logger.info(
                "Atagia history sync hit a transient SQLite lock for message %s; "
                "retrying in %.1fs (attempt %s).",
                message_id,
                delay_seconds,
                attempt_index + 2,
            )
            await _sleep_before_retry(delay_seconds)
            continue

        if ok:
            return True

        if delay_seconds is None or not _is_transient_sqlite_lock_error(
            getattr(bridge, "last_error", None)
        ):
            return False

        logger.info(
            "Atagia history sync hit a transient SQLite lock for message %s; "
            "retrying in %.1fs (attempt %s).",
            message_id,
            delay_seconds,
            attempt_index + 2,
        )
        await _sleep_before_retry(delay_seconds)

    return False


async def _sleep_before_retry(delay_seconds: float) -> None:
    await asyncio.sleep(delay_seconds)


def _is_transient_sqlite_lock_error(value: Any) -> bool:
    if isinstance(value, Exception) and database.is_lock_error(value):
        return True

    if isinstance(value, dict):
        error_type = str(value.get("error_type") or "")
        message = str(value.get("message") or "").lower()
    else:
        error_type = str(getattr(value, "error_type", "") or "")
        message = str(
            getattr(value, "message", None)
            or getattr(value, "details", None)
            or value
            or ""
        ).lower()

    if error_type and error_type != "OperationalError":
        return False
    return any(token in message for token in database.LOCK_ERROR_MESSAGES)


async def _ensure_schema(conn: aiosqlite.Connection) -> None:
    """Create the generic memory-provider tables Atagia depends on (idempotent)."""
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


async def _create_sync_run() -> int:
    async with database.get_db_connection() as conn:
        await _ensure_schema(conn)
        cursor = await conn.execute(
            "INSERT INTO MEMORY_PROVIDER_SYNC_RUNS (provider, status) "
            "VALUES (?, 'running') RETURNING id",
            (ATAGIA_PROVIDER,),
        )
        row = await cursor.fetchone()
        await conn.commit()
    return int(row[0])


async def _count_unlinked_messages() -> int:
    async with database.get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM MESSAGES m
            JOIN CONVERSATIONS c ON c.id = m.conversation_id
            LEFT JOIN MEMORY_PROVIDER_MESSAGE_LINKS l
                ON l.message_id = m.id AND l.provider = ?
            WHERE l.message_id IS NULL
              AND m.type IN ('user', 'bot')
              AND COALESCE(c.hidden_from_history, 0) = 0
              AND EXISTS (
                  SELECT 1 FROM USERS u
                  WHERE u.id = COALESCE(m.user_id, c.user_id)
              )
            """,
            (ATAGIA_PROVIDER,),
        )
        row = await cursor.fetchone()
    return int(row[0] if row else 0)


async def _fetch_unlinked_message_batch(
    *,
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
            WHERE m.id > ?
              AND l.message_id IS NULL
              AND m.type IN ('user', 'bot')
              AND COALESCE(c.hidden_from_history, 0) = 0
              AND EXISTS (
                  SELECT 1 FROM USERS u
                  WHERE u.id = COALESCE(m.user_id, c.user_id)
              )
            ORDER BY m.id ASC
            LIMIT ?
            """,
            (ATAGIA_PROVIDER, after_message_id, max(1, int(batch_size))),
        )
        rows = await cursor.fetchall()
    return list(rows)


async def _insert_message_link(
    conn: aiosqlite.Connection,
    *,
    message_id: int,
    atagia_message_id: str,
    conversation_id: int,
    user_id: int,
    role: AtagiaRole,
    source: str,
) -> bool:
    cursor = await conn.execute(
        """
        INSERT OR IGNORE INTO MEMORY_PROVIDER_MESSAGE_LINKS
            (message_id, provider, provider_message_id, provider_event_id,
             conversation_id, user_id, role, source, metadata_json)
        VALUES (?, ?, ?, NULL, ?, ?, ?, ?, '{}')
        """,
        (
            message_id,
            ATAGIA_PROVIDER,
            atagia_message_id,
            conversation_id,
            user_id,
            role,
            source,
        ),
    )
    return bool(cursor.rowcount)


async def _update_sync_state(conversation_id: int, message_id: int) -> None:
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
            (ATAGIA_PROVIDER, conversation_id, message_id),
        )
        await conn.commit()


async def _update_sync_run_from_summary(summary: AtagiaSyncSummary) -> None:
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
    values = [
        json.dumps(value) if key == "recent_errors" else value
        for key, value in fields.items()
    ]
    async with database.get_db_connection() as conn:
        await _ensure_schema(conn)
        await conn.execute(
            f"UPDATE MEMORY_PROVIDER_SYNC_RUNS SET {assignments} "
            "WHERE id = ? AND provider = ?",
            (*values, run_id, ATAGIA_PROVIDER),
        )
        await conn.commit()


async def _mark_run_finished(
    summary: AtagiaSyncSummary,
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
            WHERE id = ? AND provider = ?
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
                ATAGIA_PROVIDER,
            ),
        )
        await conn.commit()


def _message_text_for_atagia_sync(value: Any) -> str:
    try:
        from ai_runtime.atagia.context import _message_text_for_atagia
        from ai_runtime.context.formatting import parse_stored_message
        from common import custom_unescape

        normalized = custom_unescape(value) if isinstance(value, str) else value
        return _message_text_for_atagia(parse_stored_message(normalized))
    except Exception:
        return "" if value is None else str(value)


def _role_for_message_type(message_type: str) -> AtagiaRole:
    return "user" if message_type == "user" else "assistant"


def _aurvek_atagia_message_id(message_id: int | str) -> str:
    text = str(message_id).strip()
    if text.startswith("aurvek:msg:"):
        return text
    try:
        from atagia.integrations import aurvek_message_id

        return aurvek_message_id(text)
    except Exception:
        return f"aurvek:msg:{text}"


def _format_bridge_last_error(bridge: Any) -> str:
    error = getattr(bridge, "last_error", None)
    if error is None:
        return ""

    if isinstance(error, dict):
        operation = error.get("operation")
        error_type = error.get("error_type")
        message = error.get("message")
        status_code = error.get("status_code")
        parts = [
            str(part)
            for part in (operation, error_type, status_code, message)
            if part not in (None, "")
        ]
        return " / ".join(parts)

    operation = getattr(error, "operation", None)
    error_type = getattr(error, "error_type", None)
    status_code = getattr(error, "status_code", None)
    message = getattr(error, "message", None)
    details = getattr(error, "details", None)
    if details and details != message:
        message = f"{message} ({details})" if message else str(details)
    parts = [
        str(part)
        for part in (operation, error_type, status_code, message)
        if part not in (None, "")
    ]
    return " / ".join(parts) or str(error)


def _add_recent_error(summary: AtagiaSyncSummary, message: str) -> None:
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
