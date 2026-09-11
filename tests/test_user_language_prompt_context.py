import aiosqlite
import pytest

from ai_runtime.context.user_language import (
    load_user_language_context,
    render_user_language_context,
)


def test_render_user_language_context_sets_primary_and_secondary_policy():
    context = render_user_language_context(["es", "en"])

    assert "profile_primary_language=Spanish (es)" in context
    assert "profile_additional_languages=English (en)" in context
    assert "assistant prompt remains authoritative" in context
    assert "explicitly requested" in context
    assert "current message clearly establishes a language" in context
    assert "only as the fallback" in context
    assert "do not alternate or translate" in context


def test_render_user_language_context_never_echoes_invalid_storage():
    context = render_user_language_context('["xx-injected"]')

    assert "xx-injected" not in context
    assert "profile_primary_language=not configured" in context


@pytest.mark.asyncio
async def test_load_user_language_context_reads_profile_and_supports_legacy(tmp_path):
    db_path = tmp_path / "language-context.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "CREATE TABLE USERS (id INTEGER PRIMARY KEY, preferred_languages_json TEXT)"
        )
        await conn.execute(
            "INSERT INTO USERS (id, preferred_languages_json) VALUES (7, '[\"pt\",\"es\"]')"
        )
        await conn.commit()
        configured = await load_user_language_context(conn, 7)

    assert "profile_primary_language=Portuguese (pt)" in configured
    assert "profile_additional_languages=Spanish (es)" in configured

    legacy_path = tmp_path / "legacy-language-context.db"
    async with aiosqlite.connect(legacy_path) as conn:
        await conn.execute("CREATE TABLE USERS (id INTEGER PRIMARY KEY)")
        await conn.execute("INSERT INTO USERS (id) VALUES (7)")
        await conn.commit()
        legacy = await load_user_language_context(conn, 7)

    assert "profile_primary_language=not configured" in legacy
