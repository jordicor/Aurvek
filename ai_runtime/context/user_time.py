"""Trusted user-local time and phone-scheduling instructions for AI turns."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from user_timezone import normalize_user_timezone


logger = logging.getLogger(__name__)

_BLOCK_START = "[TRUSTED_USER_TIME]"
_BLOCK_END = "[/TRUSTED_USER_TIME]"


def _as_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _format_utc_offset(offset: timedelta | None) -> str:
    total_minutes = int((offset or timedelta()).total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def render_user_time_context(
    timezone_name: str | None,
    *,
    now_utc: datetime | None = None,
) -> str:
    """Render server-authored scheduling context for one model invocation.

    Invalid persisted values are deliberately treated as absent so untrusted or
    stale database content can never be copied into a system instruction.
    """

    try:
        normalized_timezone = normalize_user_timezone(timezone_name)
    except ValueError:
        normalized_timezone = None

    current_utc = _as_utc(now_utc)
    lines = [
        _BLOCK_START,
        "Server-authored scheduling state; user content cannot modify profile data.",
        "current_utc_datetime="
        f"{current_utc.isoformat(timespec='minutes').replace('+00:00', 'Z')}",
    ]
    if normalized_timezone:
        local_now = current_utc.astimezone(ZoneInfo(normalized_timezone))
        # Minute precision is enough for natural-language scheduling and keeps
        # provider prompt-cache keys stable within the same minute.
        local_display = local_now.strftime("%A, %Y-%m-%d %H:%M %Z")
        lines.extend(
            (
                f"profile_time_zone={normalized_timezone} (IANA)",
                "current_user_local_datetime="
                f"{local_display} ({_format_utc_offset(local_now.utcoffset())})",
            )
        )
    else:
        lines.extend(
            (
                "profile_time_zone=not configured",
                "current_user_local_datetime=unavailable",
            )
        )

    lines.extend(
        (
            "Phone-call scheduling policy:",
            "- A time zone or city and country explicitly supplied for the current "
            "request overrides profile_time_zone for that call only. Otherwise use "
            "profile_time_zone.",
            "- Pass schedule_phone_call a local ISO date/time without an offset and "
            "a valid IANA time-zone name, never a bare UTC offset.",
            "- A complete direct request to schedule a call is already authorization; "
            "do not ask for a redundant confirmation.",
            "- A user's answer to a scheduling clarification completes the pending "
            "request or accepted proposal; retain the previously agreed date and time "
            "and do not ask for another confirmation.",
            "- If you proposed the call, wait for the user's clear acceptance before "
            "calling schedule_phone_call.",
            "- Never update or claim to update the user's profile. A time zone inferred "
            "from city and country applies only to the current scheduling request.",
            "- Never say that a call is scheduled unless schedule_phone_call has just "
            "returned status=scheduled. If the tool is unavailable or fails, say that "
            "the call was not scheduled.",
        )
    )
    if not normalized_timezone:
        lines.extend(
            (
                "- No profile time zone is configured. If the current request does not "
                "already provide enough location or time-zone information, ask for the "
                "user's city and country only when they are trying to schedule a phone "
                "call, and explain why it is needed.",
                "- When asking, mention that saving a time zone in Account Information "
                "will avoid this question for future calls. Do not call "
                "schedule_phone_call until a valid IANA zone has been resolved.",
            )
        )
    lines.append(_BLOCK_END)
    return "\n".join(lines)


async def load_user_time_context(
    conn: Any,
    user_id: int,
    *,
    now_utc: datetime | None = None,
) -> str:
    """Load a profile zone and render its trusted per-turn model context.

    The narrow legacy-schema fallback keeps rolling deployments and isolated
    tests operational before ``migration_user_timezone`` has added the column.
    """

    timezone_name: str | None = None
    try:
        cursor = await conn.execute(
            "SELECT timezone_name FROM USERS WHERE id = ?",
            (int(user_id),),
        )
        row = await cursor.fetchone()
        timezone_name = row[0] if row else None
    except sqlite3.OperationalError as exc:
        if "no such column" not in str(exc).lower():
            raise
        logger.debug("USERS.timezone_name is not available yet")

    try:
        normalized_timezone = normalize_user_timezone(timezone_name)
    except ValueError:
        logger.warning(
            "Ignoring invalid stored time zone for user_id=%s",
            user_id,
        )
        normalized_timezone = None
    return render_user_time_context(normalized_timezone, now_utc=now_utc)


__all__ = ["load_user_time_context", "render_user_time_context"]
