import aiosqlite
import pytest

from user_languages import (
    language_display_name,
    load_user_preferred_languages,
    normalize_preferred_languages,
    selectable_user_languages,
    serialize_preferred_languages,
)


def test_normalize_preferred_languages_canonicalizes_legacy_values():
    assert normalize_preferred_languages('["es-ES", "EN_us", "es"]') == (
        "es",
        "en",
    )
    assert normalize_preferred_languages(
        {"primary_language": "Spanish", "other_languages": "English; pt-BR"}
    ) == ("es", "en", "pt")
    assert normalize_preferred_languages(
        {"preferred_languages": ["pt-BR", "Spanish"]}
    ) == ("pt", "es")
    assert normalize_preferred_languages("null") == ()


def test_serialize_preferred_languages_is_compact_and_ordered():
    assert serialize_preferred_languages(["Spanish", "en-GB"]) == '["es","en"]'
    assert language_display_name("es-MX") == "Spanish"
    assert {option["code"] for option in selectable_user_languages()} >= {
        "en",
        "es",
        "pt",
        "ru",
    }


def test_normalize_preferred_languages_rejects_unknown_and_excess_values():
    with pytest.raises(ValueError, match="Unsupported language"):
        normalize_preferred_languages(["xx"])
    with pytest.raises(ValueError, match="Unsupported language"):
        normalize_preferred_languages(["ar"])
    with pytest.raises(ValueError, match="at most 6"):
        normalize_preferred_languages(["en", "es", "pt", "fr", "de", "it", "pl"])


def test_localized_language_choices_preserve_supported_codes_and_internal_names():
    english_codes = {option["code"] for option in selectable_user_languages()}
    for locale in ("es", "ja", "fr", "pt", "it", "de"):
        options = selectable_user_languages(locale)
        assert {option["code"] for option in options} == english_codes
        assert next(option["label"] for option in options if option["code"] == "es") == language_display_name("es", locale)
    assert language_display_name("es", "ja") == "スペイン語"
    assert language_display_name("es") == "Spanish"


@pytest.mark.asyncio
async def test_load_user_preferred_languages_reads_canonical_order(tmp_path):
    db_path = tmp_path / "languages.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "CREATE TABLE USERS (id INTEGER PRIMARY KEY, preferred_languages_json TEXT)"
        )
        await conn.execute(
            "INSERT INTO USERS (id, preferred_languages_json) VALUES (?, ?)",
            (7, '["es","en"]'),
        )
        await conn.commit()

        languages = await load_user_preferred_languages(conn, 7)

    assert languages == ("es", "en")


@pytest.mark.asyncio
@pytest.mark.parametrize("create_users", [True, False])
async def test_load_user_preferred_languages_tolerates_legacy_schema(
    tmp_path,
    create_users,
):
    db_path = tmp_path / "legacy-languages.db"
    async with aiosqlite.connect(db_path) as conn:
        if create_users:
            await conn.execute("CREATE TABLE USERS (id INTEGER PRIMARY KEY)")
            await conn.execute("INSERT INTO USERS (id) VALUES (7)")
            await conn.commit()

        languages = await load_user_preferred_languages(conn, 7)

    assert languages == ()


@pytest.mark.asyncio
async def test_load_user_preferred_languages_ignores_invalid_storage(tmp_path):
    db_path = tmp_path / "invalid-languages.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "CREATE TABLE USERS (id INTEGER PRIMARY KEY, preferred_languages_json TEXT)"
        )
        await conn.execute(
            "INSERT INTO USERS (id, preferred_languages_json) VALUES (?, ?)",
            (7, '["xx"]'),
        )
        await conn.commit()

        languages = await load_user_preferred_languages(conn, 7)

    assert languages == ()
