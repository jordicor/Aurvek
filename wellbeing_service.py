"""Usage wellbeing tracking and break reminder policy helpers."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

import aiosqlite

from database import get_db_connection
from log_config import logger


UTC_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
DEFAULT_PAUSE_MINUTES = 5
SEVERITY_ORDER = {"normal": 0, "soft": 1, "intense": 2, "strong": 3}
PASSIVE_ACTIVITY_TYPES = {"chat_presence", "presence", "heartbeat", "status"}
CLIENT_INACTIVE_ACTIVITY_TYPES = {"client_afk", "client_idle", "focus_lost", "visibility_hidden"}


DEFAULT_CONFIG: dict[str, tuple[str, str]] = {
    "wellbeing_enabled": ("1", "Enable break reminder nudges."),
    "wellbeing_idle_gap_minutes": ("25", "Idle minutes before a continuous session is closed."),
    "wellbeing_soft_minutes": ("90", "Soft reminder active-session minutes."),
    "wellbeing_soft_user_messages": ("75", "Soft reminder user message count."),
    "wellbeing_soft_user_words": ("2500", "Soft reminder approximate user word count."),
    "wellbeing_intense_minutes": ("180", "Intense reminder active-session minutes."),
    "wellbeing_intense_user_messages": ("150", "Intense reminder user message count."),
    "wellbeing_intense_user_words": ("6000", "Intense reminder approximate user word count."),
    "wellbeing_strong_minutes": ("360", "Strong reminder active-session minutes."),
    "wellbeing_strong_user_messages": ("300", "Strong reminder user message count."),
    "wellbeing_cooldown_minutes": ("45", "Minimum minutes between reminders in the same session."),
    "wellbeing_snooze_minutes": ("10", "Default reminder snooze minutes."),
    "wellbeing_allow_snooze": ("1", "Allow users to snooze break reminders."),
    "wellbeing_mode": ("informational", "Reminder mode: informational or strict."),
    "wellbeing_notice_text_soft": (
        "You have been using the chat continuously for a while. Consider taking a short break.",
        "Soft reminder copy.",
    ),
    "wellbeing_notice_text_intense": (
        "This session is getting intense. It may be a good moment to rest before continuing.",
        "Intense reminder copy.",
    ),
    "wellbeing_notice_text_strong": (
        "You have had a lot of continuous activity. We recommend stopping for a few minutes before continuing.",
        "Strong reminder copy.",
    ),
}

STRICT_MIN_PAUSE_MINUTES = 5


CONFIG_TYPES: dict[str, str] = {
    "wellbeing_enabled": "bool",
    "wellbeing_idle_gap_minutes": "int",
    "wellbeing_soft_minutes": "int",
    "wellbeing_soft_user_messages": "int",
    "wellbeing_soft_user_words": "int",
    "wellbeing_intense_minutes": "int",
    "wellbeing_intense_user_messages": "int",
    "wellbeing_intense_user_words": "int",
    "wellbeing_strong_minutes": "int",
    "wellbeing_strong_user_messages": "int",
    "wellbeing_cooldown_minutes": "int",
    "wellbeing_snooze_minutes": "int",
    "wellbeing_allow_snooze": "bool",
    "wellbeing_mode": "choice",
    "wellbeing_notice_text_soft": "text",
    "wellbeing_notice_text_intense": "text",
    "wellbeing_notice_text_strong": "text",
}

CONFIG_INT_BOUNDS: dict[str, tuple[int, int]] = {
    "wellbeing_idle_gap_minutes": (5, 240),
    "wellbeing_soft_minutes": (5, 1440),
    "wellbeing_soft_user_messages": (1, 5000),
    "wellbeing_soft_user_words": (1, 250000),
    "wellbeing_intense_minutes": (10, 2880),
    "wellbeing_intense_user_messages": (1, 10000),
    "wellbeing_intense_user_words": (1, 500000),
    "wellbeing_strong_minutes": (15, 4320),
    "wellbeing_strong_user_messages": (1, 20000),
    "wellbeing_cooldown_minutes": (1, 1440),
    "wellbeing_snooze_minutes": (1, 240),
}

PREFERENCE_DEFAULTS = {
    "reminders_enabled": True,
    "intense_reminders_enabled": True,
    "preferred_soft_minutes": None,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt_to_str(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(UTC_FORMAT)


def _parse_dt(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    raw = str(value)
    for fmt in (UTC_FORMAT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(raw.replace("Z", ""), fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        logger.debug("Could not parse wellbeing timestamp: %s", raw)
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value or {}, ensure_ascii=True, separators=(",", ":"))
    except TypeError:
        return "{}"


def _row_to_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except Exception:
        return {key: row[key] for key in row.keys()}


def _extract_text_parts(value: Any) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="ignore")
        except Exception:
            return
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            yield stripped
            return
        if isinstance(parsed, (dict, list)):
            yield from _extract_text_parts(parsed)
        elif isinstance(parsed, str):
            yield parsed
        return
    if isinstance(value, list):
        for item in value:
            yield from _extract_text_parts(item)
        return
    if isinstance(value, dict):
        if isinstance(value.get("responses"), list):
            for response in value["responses"]:
                yield from _extract_text_parts(response)
            return

        for key in ("text", "content", "message", "transcript", "answer"):
            if key in value and isinstance(value[key], str):
                yield value[key]

        text_file = value.get("text_file")
        if isinstance(text_file, dict):
            for key in ("text", "content", "extracted_text"):
                if isinstance(text_file.get(key), str):
                    yield text_file[key]
                    break
        return


def _text_from_message(value: Any) -> str:
    return "\n".join(part for part in _extract_text_parts(value) if part)


def _word_count(value: Any) -> int:
    text = _text_from_message(value)
    if not text:
        return 0
    return len(re.findall(r"\b[\w']+\b", text, flags=re.UNICODE))


async def _column_names(conn: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return {row[1] for row in rows}


async def _ensure_column(conn: aiosqlite.Connection, table: str, column: str, definition: str) -> None:
    columns = await _column_names(conn, table)
    if column not in columns:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def ensure_wellbeing_schema(conn: Optional[aiosqlite.Connection] = None) -> None:
    """Create additive wellbeing tables and default config values."""

    if conn is not None:
        await _ensure_wellbeing_schema(conn)
        return

    async with get_db_connection() as local_conn:
        await _ensure_wellbeing_schema(local_conn)
        await local_conn.commit()


async def _ensure_wellbeing_schema(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS SYSTEM_CONFIG (
            key TEXT PRIMARY KEY,
            value TEXT,
            description TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await _ensure_column(conn, "SYSTEM_CONFIG", "description", "TEXT")
    system_config_columns = await _column_names(conn, "SYSTEM_CONFIG")
    if "updated_at" not in system_config_columns:
        await conn.execute("ALTER TABLE SYSTEM_CONFIG ADD COLUMN updated_at TEXT")
    await conn.execute("UPDATE SYSTEM_CONFIG SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL")

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS USER_ACTIVITY_SESSIONS (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            last_activity_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            user_messages_count INTEGER NOT NULL DEFAULT 0,
            assistant_messages_count INTEGER NOT NULL DEFAULT 0,
            user_word_count INTEGER NOT NULL DEFAULT 0,
            assistant_word_count INTEGER NOT NULL DEFAULT 0,
            conversation_count INTEGER NOT NULL DEFAULT 0,
            voice_call_seconds INTEGER NOT NULL DEFAULT 0,
            reminders_shown INTEGER NOT NULL DEFAULT 0,
            last_reminder_at TEXT,
            snoozed_until TEXT,
            pause_until TEXT,
            pause_reason TEXT,
            current_severity TEXT NOT NULL DEFAULT 'normal',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await _ensure_column(conn, "USER_ACTIVITY_SESSIONS", "pause_reason", "TEXT")
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS USER_ACTIVITY_SESSION_CONVERSATIONS (
            session_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            conversation_id INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (session_id, conversation_id)
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS USER_WELLBEING_EVENTS (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            session_id INTEGER,
            conversation_id INTEGER,
            event_type TEXT NOT NULL,
            severity TEXT,
            threshold_key TEXT,
            threshold_value REAL,
            observed_value REAL,
            user_action TEXT,
            metadata_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS USER_WELLBEING_PREFERENCES (
            user_id INTEGER PRIMARY KEY,
            reminders_enabled INTEGER NOT NULL DEFAULT 1,
            intense_reminders_enabled INTEGER NOT NULL DEFAULT 1,
            preferred_soft_minutes INTEGER,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_activity_sessions_user_last ON USER_ACTIVITY_SESSIONS(user_id, last_activity_at)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_activity_sessions_status_last ON USER_ACTIVITY_SESSIONS(status, last_activity_at)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wellbeing_events_user_created ON USER_WELLBEING_EVENTS(user_id, created_at)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wellbeing_events_type_created ON USER_WELLBEING_EVENTS(event_type, created_at)"
    )

    for key, (value, description) in DEFAULT_CONFIG.items():
        await conn.execute(
            """
            INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value, description, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (key, value, description),
        )


def _normalize_config_value(key: str, value: Any) -> str:
    if key not in DEFAULT_CONFIG:
        raise ValueError(f"Unknown wellbeing config key: {key}")

    value_type = CONFIG_TYPES.get(key, "text")
    if value_type == "bool":
        return "1" if _as_bool(value) else "0"
    if value_type == "int":
        default = int(DEFAULT_CONFIG[key][0])
        normalized = _int_or_default(value, default)
        low, high = CONFIG_INT_BOUNDS[key]
        normalized = max(low, min(high, normalized))
        return str(normalized)
    if value_type == "choice":
        mode = str(value or "").strip().lower()
        return mode if mode in {"informational", "strict"} else DEFAULT_CONFIG[key][0]
    text = str(value or "").strip()
    if not text:
        return DEFAULT_CONFIG[key][0]
    return text[:1000]


def _typed_config_value(key: str, value: Any) -> Any:
    value_type = CONFIG_TYPES.get(key, "text")
    if value_type == "bool":
        return _as_bool(value)
    if value_type == "int":
        return _int_or_default(value, int(DEFAULT_CONFIG[key][0]))
    return value


async def get_wellbeing_config() -> dict[str, Any]:
    async with get_db_connection(readonly=True) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT key, value, description FROM SYSTEM_CONFIG WHERE key LIKE 'wellbeing_%'"
        )
        rows = await cursor.fetchall()

    values = {key: value for key, (value, _) in DEFAULT_CONFIG.items()}
    descriptions = {key: desc for key, (_, desc) in DEFAULT_CONFIG.items()}
    for row in rows:
        values[row["key"]] = row["value"]
        descriptions[row["key"]] = row["description"] or descriptions.get(row["key"], "")

    return {
        key: {
            "value": _typed_config_value(key, values[key]),
            "raw_value": values[key],
            "type": CONFIG_TYPES.get(key, "text"),
            "description": descriptions.get(key, ""),
        }
        for key in DEFAULT_CONFIG
    }


async def update_wellbeing_config(payload: dict[str, Any]) -> dict[str, Any]:
    updates = {}
    for key, value in payload.items():
        if key in DEFAULT_CONFIG:
            updates[key] = _normalize_config_value(key, value)

    if not updates:
        return await get_wellbeing_config()

    async with get_db_connection() as conn:
        for key, value in updates.items():
            description = DEFAULT_CONFIG[key][1]
            await conn.execute(
                """
                INSERT INTO SYSTEM_CONFIG (key, value, description, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    description = excluded.description,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, value, description),
            )
        await conn.commit()

    return await get_wellbeing_config()


async def _config_values(conn: aiosqlite.Connection) -> dict[str, Any]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT key, value FROM SYSTEM_CONFIG WHERE key LIKE 'wellbeing_%'"
    )
    rows = await cursor.fetchall()
    values = {key: value for key, (value, _) in DEFAULT_CONFIG.items()}
    values.update({row["key"]: row["value"] for row in rows})
    return {key: _typed_config_value(key, value) for key, value in values.items()}


async def _get_preferences(conn: aiosqlite.Connection, user_id: int) -> dict[str, Any]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        """
        SELECT reminders_enabled, intense_reminders_enabled, preferred_soft_minutes
        FROM USER_WELLBEING_PREFERENCES
        WHERE user_id = ?
        """,
        (user_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return dict(PREFERENCE_DEFAULTS)
    return {
        "reminders_enabled": bool(row["reminders_enabled"]),
        "intense_reminders_enabled": bool(row["intense_reminders_enabled"]),
        "preferred_soft_minutes": row["preferred_soft_minutes"],
    }


async def get_user_preferences(user_id: int) -> dict[str, Any]:
    async with get_db_connection(readonly=True) as conn:
        return await _get_preferences(conn, int(user_id))


async def update_user_preferences(user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    reminders_enabled = _as_bool(payload.get("reminders_enabled", PREFERENCE_DEFAULTS["reminders_enabled"]))
    intense_reminders_enabled = _as_bool(
        payload.get("intense_reminders_enabled", PREFERENCE_DEFAULTS["intense_reminders_enabled"])
    )
    preferred_soft_minutes = payload.get("preferred_soft_minutes")
    if preferred_soft_minutes in ("", None):
        preferred_soft_minutes = None
    else:
        preferred_soft_minutes = max(5, min(1440, _int_or_default(preferred_soft_minutes, 90)))

    async with get_db_connection() as conn:
        await conn.execute(
            """
            INSERT INTO USER_WELLBEING_PREFERENCES
                (user_id, reminders_enabled, intense_reminders_enabled, preferred_soft_minutes, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                reminders_enabled = excluded.reminders_enabled,
                intense_reminders_enabled = excluded.intense_reminders_enabled,
                preferred_soft_minutes = excluded.preferred_soft_minutes,
                updated_at = excluded.updated_at
            """,
            (
                int(user_id),
                1 if reminders_enabled else 0,
                1 if intense_reminders_enabled else 0,
                preferred_soft_minutes,
                _dt_to_str(_now()),
            ),
        )
        await conn.commit()

    return await get_user_preferences(user_id)


async def _insert_event(
    conn: aiosqlite.Connection,
    *,
    user_id: int,
    event_type: str,
    session_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    severity: Optional[str] = None,
    threshold_key: Optional[str] = None,
    threshold_value: Optional[float] = None,
    observed_value: Optional[float] = None,
    user_action: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    created_at: Optional[datetime] = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO USER_WELLBEING_EVENTS
            (user_id, session_id, conversation_id, event_type, severity,
             threshold_key, threshold_value, observed_value, user_action,
             metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(user_id),
            session_id,
            conversation_id,
            event_type,
            severity,
            threshold_key,
            threshold_value,
            observed_value,
            user_action,
            _safe_json(metadata),
            _dt_to_str(created_at or _now()),
        ),
    )


async def _load_active_session(conn: aiosqlite.Connection, user_id: int) -> Optional[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        """
        SELECT *
        FROM USER_ACTIVITY_SESSIONS
        WHERE user_id = ? AND status = 'active'
        ORDER BY last_activity_at DESC, id DESC
        LIMIT 1
        """,
        (int(user_id),),
    )
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


async def _load_latest_pause_session(
    conn: aiosqlite.Connection,
    user_id: int,
) -> Optional[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        """
        SELECT *
        FROM USER_ACTIVITY_SESSIONS
        WHERE user_id = ?
          AND pause_until IS NOT NULL
        ORDER BY pause_until DESC, id DESC
        LIMIT 1
        """,
        (int(user_id),),
    )
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


async def _load_active_pause_session(
    conn: aiosqlite.Connection,
    user_id: int,
    now: datetime,
) -> Optional[dict[str, Any]]:
    session = await _load_latest_pause_session(conn, user_id)
    pause_until = _parse_dt(session.get("pause_until")) if session else None
    if pause_until and pause_until > now:
        return session
    return None


async def _close_session(
    conn: aiosqlite.Connection,
    session: dict[str, Any],
    *,
    ended_at: datetime,
    reason: str,
) -> None:
    await conn.execute(
        """
        UPDATE USER_ACTIVITY_SESSIONS
        SET status = 'closed', ended_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (_dt_to_str(ended_at), _dt_to_str(_now()), session["id"]),
    )
    await _insert_event(
        conn,
        user_id=int(session["user_id"]),
        session_id=int(session["id"]),
        event_type="session_closed",
        severity=session.get("current_severity"),
        metadata={"reason": reason},
        created_at=ended_at,
    )


async def _close_active_session_for_client_inactive(
    conn: aiosqlite.Connection,
    user_id: int,
    *,
    now: datetime,
    reason: str,
) -> bool:
    session = await _load_active_session(conn, user_id)
    if not session:
        return False

    ended_at = now
    if reason in {"client_afk", "client_idle"}:
        ended_at = _parse_dt(session.get("last_activity_at")) or now
    await _close_session(conn, session, ended_at=ended_at, reason=reason)
    return True


async def _create_session(conn: aiosqlite.Connection, user_id: int, now: datetime) -> dict[str, Any]:
    timestamp = _dt_to_str(now)
    cursor = await conn.execute(
        """
        INSERT INTO USER_ACTIVITY_SESSIONS
            (user_id, started_at, last_activity_at, status, current_severity, created_at, updated_at)
        VALUES (?, ?, ?, 'active', 'normal', ?, ?)
        """,
        (int(user_id), timestamp, timestamp, timestamp, timestamp),
    )
    session_id = cursor.lastrowid
    await _insert_event(
        conn,
        user_id=int(user_id),
        session_id=session_id,
        event_type="session_started",
        severity="normal",
        created_at=now,
    )
    cursor = await conn.execute(
        "SELECT * FROM USER_ACTIVITY_SESSIONS WHERE id = ?",
        (session_id,),
    )
    return _row_to_dict(await cursor.fetchone())


async def _get_or_create_current_session(
    conn: aiosqlite.Connection,
    user_id: int,
    config: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    session = await _load_active_session(conn, user_id)
    if not session:
        return await _create_session(conn, user_id, now)

    last_activity = _parse_dt(session.get("last_activity_at")) or now
    idle_gap = max(1, int(config["wellbeing_idle_gap_minutes"]))
    if now - last_activity > timedelta(minutes=idle_gap):
        await _close_session(conn, session, ended_at=last_activity, reason="idle_gap")
        return await _create_session(conn, user_id, now)

    return session


async def _close_stale_active_sessions(
    conn: aiosqlite.Connection,
    config: dict[str, Any],
    *,
    user_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> int:
    now = now or _now()
    idle_gap = max(1, int(config["wellbeing_idle_gap_minutes"]))
    threshold = now - timedelta(minutes=idle_gap)
    params: list[Any] = [_dt_to_str(threshold)]
    user_clause = ""
    if user_id is not None:
        user_clause = "AND user_id = ?"
        params.append(int(user_id))

    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        f"""
        SELECT *
        FROM USER_ACTIVITY_SESSIONS
        WHERE status = 'active'
          AND last_activity_at < ?
          {user_clause}
        """,
        tuple(params),
    )
    rows = await cursor.fetchall()
    closed = 0
    for row in rows:
        session = _row_to_dict(row)
        last_activity = _parse_dt(session.get("last_activity_at")) or threshold
        if last_activity <= threshold:
            await _close_session(
                conn,
                session,
                ended_at=last_activity,
                reason="idle_gap_read",
            )
            closed += 1
    return closed


def _active_minutes(session: dict[str, Any], now: Optional[datetime] = None) -> int:
    started_at = _parse_dt(session.get("started_at")) or (now or _now())
    ref = now or _parse_dt(session.get("last_activity_at")) or _now()
    return max(0, int((ref - started_at).total_seconds() // 60))


def _severity_for_session(
    session: dict[str, Any],
    config: dict[str, Any],
    preferences: dict[str, Any],
    now: datetime,
) -> str:
    active_minutes = _active_minutes(session, now)
    user_messages = int(session.get("user_messages_count") or 0)
    user_words = int(session.get("user_word_count") or 0)
    soft_minutes = preferences.get("preferred_soft_minutes") or int(config["wellbeing_soft_minutes"])

    if (
        active_minutes >= int(config["wellbeing_strong_minutes"])
        or user_messages >= int(config["wellbeing_strong_user_messages"])
    ):
        return "strong"
    if (
        active_minutes >= int(config["wellbeing_intense_minutes"])
        or user_messages >= int(config["wellbeing_intense_user_messages"])
        or user_words >= int(config["wellbeing_intense_user_words"])
    ):
        return "intense"
    if (
        active_minutes >= int(soft_minutes)
        or user_messages >= int(config["wellbeing_soft_user_messages"])
        or user_words >= int(config["wellbeing_soft_user_words"])
    ):
        return "soft"
    return "normal"


def _trigger_for_severity(
    session: dict[str, Any],
    config: dict[str, Any],
    preferences: dict[str, Any],
    severity: str,
    now: datetime,
) -> dict[str, Any]:
    active_minutes = _active_minutes(session, now)
    user_messages = int(session.get("user_messages_count") or 0)
    user_words = int(session.get("user_word_count") or 0)

    if severity == "strong":
        candidates = [
            ("wellbeing_strong_minutes", int(config["wellbeing_strong_minutes"]), active_minutes),
            ("wellbeing_strong_user_messages", int(config["wellbeing_strong_user_messages"]), user_messages),
        ]
    elif severity == "intense":
        candidates = [
            ("wellbeing_intense_minutes", int(config["wellbeing_intense_minutes"]), active_minutes),
            ("wellbeing_intense_user_messages", int(config["wellbeing_intense_user_messages"]), user_messages),
            ("wellbeing_intense_user_words", int(config["wellbeing_intense_user_words"]), user_words),
        ]
    elif severity == "soft":
        soft_minutes = int(preferences.get("preferred_soft_minutes") or config["wellbeing_soft_minutes"])
        candidates = [
            ("wellbeing_soft_minutes", soft_minutes, active_minutes),
            ("wellbeing_soft_user_messages", int(config["wellbeing_soft_user_messages"]), user_messages),
            ("wellbeing_soft_user_words", int(config["wellbeing_soft_user_words"]), user_words),
        ]
    else:
        return {}

    reached = [item for item in candidates if item[2] >= item[1]]
    key, threshold, observed = max(reached or candidates, key=lambda item: (item[2] / max(item[1], 1), item[2]))
    return {"threshold_key": key, "threshold_value": threshold, "observed_value": observed}


def _reminder_copy(config: dict[str, Any], severity: str) -> dict[str, str]:
    config_key = f"wellbeing_notice_text_{severity}"
    default_text = DEFAULT_CONFIG[config_key][0]
    text = config.get(config_key) or default_text
    copy = {"text": text}
    if text == default_text:
        copy.update({
            "text_key": f"notice_{severity}",
            "text_source": "platform_default",
        })
    else:
        copy["text_source"] = "admin_configured"
    return copy


def _build_status_payload(
    session: Optional[dict[str, Any]],
    config: dict[str, Any],
    preferences: dict[str, Any],
    now: datetime,
    *,
    pause_session: Optional[dict[str, Any]] = None,
    allow_reminder: bool = True,
) -> dict[str, Any]:
    reminder = {"should_show": False}
    pause_source = pause_session or session
    pause_until = _parse_dt(pause_source.get("pause_until")) if pause_source else None
    snoozed_until = _parse_dt(session.get("snoozed_until")) if session else None
    active_pause = bool(pause_until and pause_until > now)
    strict_pause_completed = bool(
        session
        and pause_until
        and pause_until <= now
        and session.get("pause_reason") == "strict_strong"
    )

    if session:
        severity = session.get("current_severity") or "normal"
        strict_pause_required = (
            bool(config["wellbeing_enabled"])
            and config["wellbeing_mode"] == "strict"
            and severity == "strong"
            and not strict_pause_completed
            and not active_pause
        )
        if not allow_reminder and not active_pause:
            reminder["reason"] = "client_inactive"
        elif strict_pause_required:
            trigger = _trigger_for_severity(session, config, preferences, severity, now)
            reminder = {
                "should_show": True,
                "severity": severity,
                **_reminder_copy(config, "strong"),
                "allow_snooze": False,
                "snooze_minutes": int(config["wellbeing_snooze_minutes"]),
                "mode": config["wellbeing_mode"],
                "requires_pause": True,
                **trigger,
            }
        elif (
            bool(config["wellbeing_enabled"])
            and config["wellbeing_mode"] == "strict"
            and severity == "strong"
            and pause_until
            and pause_until <= now
            and (session.get("pause_reason") or "") == "strict_strong"
        ):
            reminder["reason"] = "pause_completed"
        elif not config["wellbeing_enabled"]:
            reminder["reason"] = "disabled_by_admin"
        elif not preferences.get("reminders_enabled", True):
            reminder["reason"] = "disabled_by_user"
        elif severity == "normal":
            reminder["reason"] = "normal"
        elif severity in {"intense", "strong"} and not preferences.get("intense_reminders_enabled", True):
            reminder["reason"] = "intense_disabled_by_user"
        elif active_pause:
            reminder["reason"] = "pause_active"
        elif snoozed_until and snoozed_until > now:
            reminder["reason"] = "snoozed"
            reminder["snoozed_until"] = snoozed_until.isoformat()
        else:
            last_reminder = _parse_dt(session.get("last_reminder_at"))
            cooldown_minutes = int(config["wellbeing_cooldown_minutes"])
            if last_reminder and now - last_reminder < timedelta(minutes=cooldown_minutes):
                reminder["reason"] = "cooldown"
                reminder["next_allowed_at"] = (last_reminder + timedelta(minutes=cooldown_minutes)).isoformat()
            else:
                trigger = _trigger_for_severity(session, config, preferences, severity, now)
                reminder = {
                    "should_show": True,
                    "severity": severity,
                    **_reminder_copy(config, severity),
                    "allow_snooze": bool(config["wellbeing_allow_snooze"]),
                    "snooze_minutes": int(config["wellbeing_snooze_minutes"]),
                    "mode": config["wellbeing_mode"],
                    "requires_pause": config["wellbeing_mode"] == "strict" and severity == "strong",
                    **trigger,
                }

    payload = {
        "enabled": bool(config["wellbeing_enabled"]),
        "preferences": preferences,
        "session": None,
        "reminder": reminder,
        "active_pause": active_pause,
        "pause_until": pause_until.isoformat() if pause_until and (active_pause or session) else None,
        "snoozed_until": snoozed_until.isoformat() if snoozed_until else None,
        "mode": config["wellbeing_mode"],
        "idle_gap_minutes": int(config["wellbeing_idle_gap_minutes"]),
    }

    if session:
        payload["session"] = {
            "id": session["id"],
            "started_at": session.get("started_at"),
            "last_activity_at": session.get("last_activity_at"),
            "active_minutes": _active_minutes(session, now),
            "user_messages_count": int(session.get("user_messages_count") or 0),
            "assistant_messages_count": int(session.get("assistant_messages_count") or 0),
            "user_word_count": int(session.get("user_word_count") or 0),
            "assistant_word_count": int(session.get("assistant_word_count") or 0),
            "conversation_count": int(session.get("conversation_count") or 0),
            "voice_call_seconds": int(session.get("voice_call_seconds") or 0),
            "reminders_shown": int(session.get("reminders_shown") or 0),
            "last_reminder_at": session.get("last_reminder_at"),
            "current_severity": session.get("current_severity") or "normal",
        }
    return payload


async def get_status(
    user_id: int,
    conversation_id: Optional[int] = None,
    *,
    allow_reminder: bool = True,
) -> dict[str, Any]:
    del conversation_id
    now = _now()
    async with get_db_connection() as conn:
        config = await _config_values(conn)
        await _close_stale_active_sessions(conn, config, user_id=int(user_id), now=now)
        preferences = await _get_preferences(conn, int(user_id))
        session = await _load_active_session(conn, int(user_id))
        pause_session = await _load_latest_pause_session(conn, int(user_id))
        await conn.commit()
    return _build_status_payload(
        session,
        config,
        preferences,
        now,
        pause_session=pause_session,
        allow_reminder=allow_reminder,
    )


async def record_activity(
    *,
    user_id: int,
    conversation_id: Optional[int] = None,
    activity_type: str = "chat_presence",
    user_text: Any = None,
    assistant_text: Any = None,
    user_messages_delta: int = 0,
    assistant_messages_delta: int = 0,
    voice_seconds: int = 0,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    now = _now()
    activity_type = str(activity_type or "chat_presence").strip().lower()
    user_id = int(user_id)
    if conversation_id is not None:
        conversation_id = int(conversation_id)

    user_word_delta = _word_count(user_text)
    assistant_word_delta = _word_count(assistant_text)
    if user_text is not None and user_messages_delta == 0:
        user_messages_delta = 1
    if assistant_text is not None and assistant_messages_delta == 0:
        assistant_messages_delta = 1
    voice_seconds = max(0, _int_or_default(voice_seconds, 0))
    has_activity_delta = any(
        (
            user_text is not None,
            assistant_text is not None,
            int(user_messages_delta) != 0,
            int(assistant_messages_delta) != 0,
            int(voice_seconds) != 0,
        )
    )

    async with get_db_connection() as conn:
        conn.row_factory = aiosqlite.Row
        config = await _config_values(conn)
        preferences = await _get_preferences(conn, user_id)

        if activity_type in CLIENT_INACTIVE_ACTIVITY_TYPES:
            await _close_active_session_for_client_inactive(
                conn,
                user_id,
                now=now,
                reason=activity_type,
            )
            session = await _load_active_session(conn, user_id)
            pause_session = await _load_latest_pause_session(conn, user_id)
            await conn.commit()
            return _build_status_payload(
                session,
                config,
                preferences,
                now,
                pause_session=pause_session,
                allow_reminder=False,
            )

        if activity_type in PASSIVE_ACTIVITY_TYPES and not has_activity_delta:
            await _close_stale_active_sessions(conn, config, user_id=user_id, now=now)
            session = await _load_active_session(conn, user_id)
            pause_session = await _load_latest_pause_session(conn, user_id)
            await conn.commit()
            return _build_status_payload(
                session,
                config,
                preferences,
                now,
                pause_session=pause_session,
                allow_reminder=False,
            )

        active_pause_session = await _load_active_pause_session(conn, user_id, now)
        if active_pause_session:
            session = await _load_active_session(conn, user_id)
            await conn.commit()
            return _build_status_payload(
                session,
                config,
                preferences,
                now,
                pause_session=active_pause_session,
                allow_reminder=False,
            )

        session = await _get_or_create_current_session(conn, user_id, config, now)

        if conversation_id is not None:
            timestamp = _dt_to_str(now)
            await conn.execute(
                """
                INSERT OR IGNORE INTO USER_ACTIVITY_SESSION_CONVERSATIONS
                    (session_id, user_id, conversation_id, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session["id"], user_id, conversation_id, timestamp, timestamp),
            )
            await conn.execute(
                """
                UPDATE USER_ACTIVITY_SESSION_CONVERSATIONS
                SET last_seen_at = ?
                WHERE session_id = ? AND conversation_id = ?
                """,
                (timestamp, session["id"], conversation_id),
            )

        cursor = await conn.execute(
            """
            SELECT COUNT(*)
            FROM USER_ACTIVITY_SESSION_CONVERSATIONS
            WHERE session_id = ?
            """,
            (session["id"],),
        )
        conversation_count = (await cursor.fetchone())[0]

        await conn.execute(
            """
            UPDATE USER_ACTIVITY_SESSIONS
            SET last_activity_at = ?,
                user_messages_count = user_messages_count + ?,
                assistant_messages_count = assistant_messages_count + ?,
                user_word_count = user_word_count + ?,
                assistant_word_count = assistant_word_count + ?,
                voice_call_seconds = voice_call_seconds + ?,
                conversation_count = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                _dt_to_str(now),
                int(user_messages_delta),
                int(assistant_messages_delta),
                int(user_word_delta),
                int(assistant_word_delta),
                int(voice_seconds),
                int(conversation_count),
                _dt_to_str(now),
                session["id"],
            ),
        )

        cursor = await conn.execute("SELECT * FROM USER_ACTIVITY_SESSIONS WHERE id = ?", (session["id"],))
        updated_session = _row_to_dict(await cursor.fetchone())
        severity = _severity_for_session(updated_session, config, preferences, now)
        if severity != updated_session.get("current_severity"):
            await conn.execute(
                "UPDATE USER_ACTIVITY_SESSIONS SET current_severity = ?, updated_at = ? WHERE id = ?",
                (severity, _dt_to_str(now), updated_session["id"]),
            )
            await _insert_event(
                conn,
                user_id=user_id,
                session_id=updated_session["id"],
                conversation_id=conversation_id,
                event_type="severity_changed",
                severity=severity,
                metadata={"previous": updated_session.get("current_severity"), "activity_type": activity_type},
                created_at=now,
            )
            updated_session["current_severity"] = severity

        await conn.commit()

    return await get_status(user_id, conversation_id)


async def record_chat_turn(
    *,
    user_id: int,
    conversation_id: int,
    user_message: Any = None,
    assistant_message: Any = None,
) -> dict[str, Any]:
    return await record_activity(
        user_id=int(user_id),
        conversation_id=int(conversation_id),
        activity_type="chat_turn",
        user_text=user_message,
        assistant_text=assistant_message,
    )


def _transcript_seconds(transcript: list[dict[str, Any]]) -> int:
    starts: list[float] = []
    ends: list[float] = []
    for turn in transcript:
        for key in ("start_time", "start", "start_ms"):
            value = turn.get(key)
            if isinstance(value, (int, float)):
                starts.append(float(value) / (1000 if key.endswith("_ms") else 1))
                break
        for key in ("end_time", "end", "end_ms"):
            value = turn.get(key)
            if isinstance(value, (int, float)):
                ends.append(float(value) / (1000 if key.endswith("_ms") else 1))
                break
    if starts and ends:
        return max(0, int(max(ends) - min(starts)))
    return max(0, len(transcript) * 8)


async def record_voice_transcript_activity(
    *,
    user_id: int,
    conversation_id: int,
    transcript: list[dict[str, Any]],
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    user_parts = []
    assistant_parts = []
    user_roles = {"user", "customer", "client", "speaker", "human", "caller"}
    for turn in transcript or []:
        text = _text_from_message(turn.get("message") or turn.get("text") or turn.get("content") or "").strip()
        if not text:
            continue
        role = str(turn.get("role") or "").lower()
        if role in user_roles:
            user_parts.append(text)
        else:
            assistant_parts.append(text)

    user_text = "\n".join(user_parts) if user_parts else None
    assistant_text = "\n".join(assistant_parts) if assistant_parts else None
    return await record_activity(
        user_id=int(user_id),
        conversation_id=int(conversation_id),
        activity_type="voice_transcript",
        user_text=user_text,
        assistant_text=assistant_text,
        user_messages_delta=len([part for part in user_parts if part]),
        assistant_messages_delta=len([part for part in assistant_parts if part]),
        voice_seconds=_transcript_seconds(transcript or []),
        metadata={"elevenlabs_session_id": session_id},
    )


async def record_user_action(
    *,
    user_id: int,
    action: str,
    session_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    severity: Optional[str] = None,
    threshold_key: Optional[str] = None,
    threshold_value: Optional[float] = None,
    observed_value: Optional[float] = None,
    snooze_minutes: Optional[int] = None,
    pause_minutes: Optional[int] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    valid_actions = {
        "reminder_shown",
        "reminder_dismissed",
        "reminder_snoozed",
        "pause_started",
        "pause_completed",
        "session_reset",
    }
    if action not in valid_actions:
        raise ValueError(f"Unsupported wellbeing action: {action}")

    now = _now()
    user_id = int(user_id)
    if conversation_id is not None:
        conversation_id = int(conversation_id)

    async with get_db_connection() as conn:
        conn.row_factory = aiosqlite.Row
        config = await _config_values(conn)
        session = None
        if session_id:
            cursor = await conn.execute(
                "SELECT * FROM USER_ACTIVITY_SESSIONS WHERE id = ? AND user_id = ?",
                (int(session_id), user_id),
            )
            session = _row_to_dict(await cursor.fetchone())
        if not session:
            session = await _load_active_session(conn, user_id)
        if not session and action == "pause_completed":
            session = await _load_latest_pause_session(conn, user_id)
        if not session:
            if action == "pause_completed":
                await conn.commit()
                return await get_status(user_id, conversation_id)
            session = await _create_session(conn, user_id, now)

        pause_until = _parse_dt(session.get("pause_until"))
        strict_pause_required = (
            bool(config["wellbeing_enabled"])
            and config["wellbeing_mode"] == "strict"
            and (session.get("current_severity") or "normal") == "strong"
            and not (
                pause_until
                and pause_until <= now
                and (session.get("pause_reason") or "") == "strict_strong"
            )
        )
        if strict_pause_required and action == "reminder_snoozed":
            raise ValueError("Strong strict break reminders cannot be snoozed")
        if action == "pause_completed" and pause_until and pause_until > now:
            raise ValueError("Pause cannot be completed before pause_until")

        updates = []
        params: list[Any] = []
        if action == "reminder_shown":
            updates.extend(["reminders_shown = reminders_shown + 1", "last_reminder_at = ?"])
            params.append(_dt_to_str(now))
        elif action == "reminder_snoozed":
            minutes = int(snooze_minutes or config["wellbeing_snooze_minutes"])
            updates.append("snoozed_until = ?")
            params.append(_dt_to_str(now + timedelta(minutes=max(1, minutes))))
        elif action == "pause_started":
            minutes = int(pause_minutes or DEFAULT_PAUSE_MINUTES)
            if strict_pause_required:
                minutes = max(minutes, STRICT_MIN_PAUSE_MINUTES)
            updates.extend(["pause_until = ?", "pause_reason = ?"])
            params.extend([
                _dt_to_str(now + timedelta(minutes=max(1, minutes))),
                "strict_strong" if strict_pause_required else "voluntary",
            ])
        elif action == "pause_completed":
            updates.append("snoozed_until = NULL")

        if updates:
            updates.append("updated_at = ?")
            params.append(_dt_to_str(now))
            params.append(session["id"])
            await conn.execute(
                f"UPDATE USER_ACTIVITY_SESSIONS SET {', '.join(updates)} WHERE id = ?",
                tuple(params),
            )
            if action in {"pause_started", "pause_completed"}:
                cursor = await conn.execute(
                    "SELECT * FROM USER_ACTIVITY_SESSIONS WHERE id = ?",
                    (session["id"],),
                )
                refreshed_session = _row_to_dict(await cursor.fetchone())
                if (
                    refreshed_session
                    and refreshed_session.get("status") == "active"
                    and (refreshed_session.get("pause_reason") or "") == "strict_strong"
                ):
                    await _close_session(
                        conn,
                        refreshed_session,
                        ended_at=now,
                        reason=action,
                    )

        await _insert_event(
            conn,
            user_id=user_id,
            session_id=int(session["id"]) if session else None,
            conversation_id=conversation_id,
            event_type=action,
            severity=severity or (session.get("current_severity") if session else None),
            threshold_key=threshold_key,
            threshold_value=threshold_value,
            observed_value=observed_value,
            user_action=action,
            metadata=metadata,
            created_at=now,
        )
        await conn.commit()

    return await get_status(user_id, conversation_id)


async def reset_user_session(user_id: int, conversation_id: Optional[int] = None) -> dict[str, Any]:
    now = _now()
    user_id = int(user_id)
    async with get_db_connection() as conn:
        conn.row_factory = aiosqlite.Row
        config = await _config_values(conn)
        session = await _load_active_session(conn, user_id)
        if (
            session
            and bool(config["wellbeing_enabled"])
            and config["wellbeing_mode"] == "strict"
            and (session.get("current_severity") or "normal") == "strong"
            and not (
                _parse_dt(session.get("pause_until"))
                and _parse_dt(session.get("pause_until")) <= now
                and (session.get("pause_reason") or "") == "strict_strong"
            )
        ):
            raise ValueError("Current strict break session cannot be reset before a completed pause")
        if session:
            await _close_session(conn, session, ended_at=now, reason="user_reset")
        await _insert_event(
            conn,
            user_id=user_id,
            session_id=int(session["id"]) if session else None,
            conversation_id=conversation_id,
            event_type="session_reset",
            severity=session.get("current_severity") if session else None,
            user_action="session_reset",
            created_at=now,
        )
        await conn.commit()
    return await get_status(user_id, conversation_id)


async def get_active_pause(user_id: int) -> Optional[dict[str, Any]]:
    now = _now()
    async with get_db_connection() as conn:
        config = await _config_values(conn)
        await _close_stale_active_sessions(conn, config, user_id=int(user_id), now=now)
        session = await _load_active_session(conn, int(user_id))
        pause_session = await _load_latest_pause_session(conn, int(user_id))
        await conn.commit()

    pause_until = _parse_dt(pause_session.get("pause_until")) if pause_session else None
    if pause_until and pause_until > now:
        return {
            "active": True,
            "reason": "pause_active",
            "pause_until": pause_until.isoformat(),
            "session_id": pause_session["id"],
            "severity": pause_session.get("current_severity") or "normal",
            "message": "A break pause is active for this account. You can continue when it ends.",
        }

    if not session:
        return None
    session_pause_until = _parse_dt(session.get("pause_until"))
    if (
        bool(config["wellbeing_enabled"])
        and config["wellbeing_mode"] == "strict"
        and (session.get("current_severity") or "normal") == "strong"
        and not (
            session_pause_until
            and session_pause_until <= now
            and (session.get("pause_reason") or "") == "strict_strong"
        )
    ):
        return {
            "active": True,
            "reason": "strict_pause_required",
            "pause_until": None,
            "session_id": session["id"],
            "severity": "strong",
            "message": "This session reached the strict break threshold. Start a short pause before continuing.",
        }
    return None


async def get_admin_overview() -> dict[str, Any]:
    async with get_db_connection() as conn:
        conn.row_factory = aiosqlite.Row
        config = await _config_values(conn)
        await _close_stale_active_sessions(conn, config, now=_now())
        cursor = await conn.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_sessions,
                SUM(CASE WHEN status = 'active' AND current_severity IN ('intense', 'strong') THEN 1 ELSE 0 END) AS intense_sessions,
                COALESCE(SUM(reminders_shown), 0) AS reminders_shown,
                COALESCE(SUM(voice_call_seconds), 0) AS voice_call_seconds
            FROM USER_ACTIVITY_SESSIONS
            """
        )
        row = await cursor.fetchone()
        cursor = await conn.execute(
            """
            SELECT COUNT(*)
            FROM USER_WELLBEING_EVENTS
            WHERE event_type IN ('reminder_shown', 'reminder_snoozed', 'pause_started')
              AND created_at >= datetime('now', '-7 days')
            """
        )
        events_7d = (await cursor.fetchone())[0]
        await conn.commit()
    return {
        "active_sessions": row["active_sessions"] or 0,
        "intense_sessions": row["intense_sessions"] or 0,
        "reminders_shown": row["reminders_shown"] or 0,
        "voice_call_seconds": row["voice_call_seconds"] or 0,
        "events_7d": events_7d or 0,
    }


async def get_admin_live_sessions(limit: int = 50, search: Optional[str] = None) -> list[dict[str, Any]]:
    limit = max(1, min(200, int(limit or 50)))
    params: list[Any] = []
    search_clause = ""
    if search:
        search_clause = "AND (u.username LIKE ? OR CAST(s.user_id AS TEXT) = ?)"
        params.extend([f"%{search}%", search])

    async with get_db_connection() as conn:
        conn.row_factory = aiosqlite.Row
        config = await _config_values(conn)
        await _close_stale_active_sessions(conn, config, now=_now())
        cursor = await conn.execute(
            f"""
            SELECT
                s.*,
                u.username
            FROM USER_ACTIVITY_SESSIONS s
            LEFT JOIN USERS u ON u.id = s.user_id
            WHERE s.status = 'active' {search_clause}
            ORDER BY s.last_activity_at DESC
            LIMIT ?
            """,
            (*params, limit),
        )
        rows = await cursor.fetchall()
        await conn.commit()

    now = _now()
    result = []
    for row in rows:
        item = _row_to_dict(row)
        item["active_minutes"] = _active_minutes(item, now)
        result.append(item)
    return result


async def get_admin_events(
    *,
    page: int = 1,
    per_page: int = 50,
    event_type: Optional[str] = None,
    severity: Optional[str] = None,
    search: Optional[str] = None,
) -> dict[str, Any]:
    page = max(1, int(page or 1))
    per_page = max(1, min(200, int(per_page or 50)))
    offset = (page - 1) * per_page
    clauses = []
    params: list[Any] = []
    if event_type:
        clauses.append("e.event_type = ?")
        params.append(event_type)
    if severity:
        clauses.append("e.severity = ?")
        params.append(severity)
    if search:
        clauses.append("(u.username LIKE ? OR CAST(e.user_id AS TEXT) = ?)")
        params.extend([f"%{search}%", search])
    where_clause = "WHERE " + " AND ".join(clauses) if clauses else ""

    async with get_db_connection(readonly=True) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            f"""
            SELECT COUNT(*)
            FROM USER_WELLBEING_EVENTS e
            LEFT JOIN USERS u ON u.id = e.user_id
            {where_clause}
            """,
            tuple(params),
        )
        total = (await cursor.fetchone())[0]
        cursor = await conn.execute(
            f"""
            SELECT
                e.*,
                u.username
            FROM USER_WELLBEING_EVENTS e
            LEFT JOIN USERS u ON u.id = e.user_id
            {where_clause}
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, per_page, offset),
        )
        rows = await cursor.fetchall()

    return {
        "page": page,
        "per_page": per_page,
        "total": total,
        "events": [_row_to_dict(row) for row in rows],
    }


async def get_user_wellbeing_summary(user_id: int, days: int = 30) -> dict[str, Any]:
    days = max(1, min(3650, int(days or 30)))
    async with get_db_connection() as conn:
        conn.row_factory = aiosqlite.Row
        config = await _config_values(conn)
        await _close_stale_active_sessions(conn, config, user_id=int(user_id), now=_now())
        cursor = await conn.execute(
            """
            SELECT
                COUNT(*) AS sessions,
                COALESCE(SUM(user_messages_count), 0) AS user_messages,
                COALESCE(SUM(assistant_messages_count), 0) AS assistant_messages,
                COALESCE(SUM(user_word_count), 0) AS user_words,
                COALESCE(SUM(assistant_word_count), 0) AS assistant_words,
                COALESCE(SUM(voice_call_seconds), 0) AS voice_call_seconds,
                COALESCE(SUM(reminders_shown), 0) AS reminders_shown,
                SUM(CASE
                    WHEN current_severity IN ('intense', 'strong')
                      OR (julianday(COALESCE(ended_at, last_activity_at)) - julianday(started_at)) * 24 * 60 >= 180
                    THEN 1 ELSE 0 END
                ) AS long_sessions
            FROM USER_ACTIVITY_SESSIONS
            WHERE user_id = ? AND started_at >= datetime('now', ?)
            """,
            (int(user_id), f"-{days} days"),
        )
        totals = _row_to_dict(await cursor.fetchone())
        preferences = await _get_preferences(conn, int(user_id))
        active_session = await _load_active_session(conn, int(user_id))
        await conn.commit()

    now = _now()
    return {
        "days": days,
        "sessions": totals.get("sessions") or 0,
        "user_messages": totals.get("user_messages") or 0,
        "assistant_messages": totals.get("assistant_messages") or 0,
        "user_words": totals.get("user_words") or 0,
        "assistant_words": totals.get("assistant_words") or 0,
        "voice_call_seconds": totals.get("voice_call_seconds") or 0,
        "reminders_shown": totals.get("reminders_shown") or 0,
        "long_sessions": totals.get("long_sessions") or 0,
        "active_session": _build_status_payload(
            active_session,
            config,
            preferences,
            now,
        )["session"]
        if active_session
        else None,
    }
