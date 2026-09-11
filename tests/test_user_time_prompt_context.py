from datetime import datetime, timezone

import aiosqlite
import pytest

from ai_runtime.context.user_time import (
    load_user_time_context,
    render_user_time_context,
)


def test_render_user_time_context_includes_profile_zone_and_local_clock():
    context = render_user_time_context(
        "America/New_York",
        now_utc=datetime(2026, 9, 3, 23, 15, 0, tzinfo=timezone.utc),
    )

    assert "profile_time_zone=America/New_York (IANA)" in context
    assert "current_utc_datetime=2026-09-03T23:15Z" in context
    assert (
        "current_user_local_datetime=Thursday, 2026-09-03 19:15 EDT "
        "(UTC-04:00)"
    ) in context
    assert "explicitly supplied" in context
    assert "overrides profile_time_zone for that call only" in context
    assert "answer to a scheduling clarification completes the pending" in context
    assert "status=scheduled" in context


def test_render_user_time_context_requires_location_when_profile_zone_is_missing():
    context = render_user_time_context(None)

    assert "profile_time_zone=not configured" in context
    assert "current_user_local_datetime=unavailable" in context
    assert "ask for the user's city and country" in context
    assert "Account Information" in context
    assert "Do not call schedule_phone_call" in context
    assert "Never update or claim to update the user's profile" in context


def test_render_user_time_context_does_not_echo_an_invalid_stored_value():
    invalid_value = "Mars/Olympus"

    context = render_user_time_context(invalid_value)

    assert invalid_value not in context
    assert "profile_time_zone=not configured" in context


@pytest.mark.asyncio
async def test_load_user_time_context_reads_the_profile_zone(tmp_path):
    db_path = tmp_path / "user-time.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "CREATE TABLE USERS (id INTEGER PRIMARY KEY, timezone_name TEXT)"
        )
        await conn.execute(
            "INSERT INTO USERS (id, timezone_name) VALUES (?, ?)",
            (7, "Europe/Madrid"),
        )
        await conn.commit()

        context = await load_user_time_context(
            conn,
            7,
            now_utc=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        )

    assert "profile_time_zone=Europe/Madrid (IANA)" in context
    assert "Thursday, 2026-01-15 13:00 CET (UTC+01:00)" in context


@pytest.mark.asyncio
async def test_load_user_time_context_supports_a_rolling_legacy_schema(tmp_path):
    db_path = tmp_path / "legacy-user-time.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("CREATE TABLE USERS (id INTEGER PRIMARY KEY)")
        await conn.execute("INSERT INTO USERS (id) VALUES (7)")
        await conn.commit()

        context = await load_user_time_context(conn, 7)

    assert "profile_time_zone=not configured" in context
    assert "Account Information" in context
