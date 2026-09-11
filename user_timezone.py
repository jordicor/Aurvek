"""User-profile time-zone validation and presentation helpers."""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones


MAX_TIMEZONE_NAME_LENGTH = 100


def normalize_user_timezone(value: object) -> str | None:
    """Return a validated IANA time-zone name, or ``None`` for an empty value."""

    timezone_name = str(value or "").strip()
    if not timezone_name:
        return None
    if len(timezone_name) > MAX_TIMEZONE_NAME_LENGTH:
        raise ValueError("Time zone must be a valid IANA time zone.")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Time zone must be a valid IANA time zone.") from exc
    return zone.key


@lru_cache(maxsize=1)
def selectable_user_timezones() -> tuple[str, ...]:
    """Return stable IANA names for the profile time-zone selector."""

    names = {
        name
        for name in available_timezones()
        if name != "localtime"
        and not name.startswith("posix/")
        and not name.startswith("right/")
    }
    names.add("UTC")
    return tuple(sorted(names))
