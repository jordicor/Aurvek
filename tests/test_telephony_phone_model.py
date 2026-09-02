import json

import aiosqlite
import pytest

from integrations.telephony.snapshot import (
    PhoneSnapshotError,
    _resolve_phone_model_selection,
    realtime_voice_from_snapshot,
    reasoning_selection_from_snapshot,
    runtime_kind_from_snapshot,
    runtime_llm_id_from_snapshot,
)


async def _database():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(
        """
        CREATE TABLE PROMPTS(
            id INTEGER PRIMARY KEY,
            forced_llm_id INTEGER,
            forced_reasoning_json TEXT,
            phone_llm_id INTEGER,
            phone_reasoning_json TEXT,
            phone_realtime_voice TEXT
        );
        CREATE TABLE LLM(
            id INTEGER PRIMARY KEY,
            machine TEXT,
            model TEXT,
            enabled INTEGER,
            provider_key TEXT,
            provider_model_id TEXT,
            raw_metadata_json TEXT,
            capabilities_json TEXT,
            manual_overrides_json TEXT
        );
        """
    )
    return conn


async def _insert_model(
    conn,
    llm_id,
    *,
    machine="Claude",
    model="claude-sonnet-4-6",
    enabled=1,
    provider="anthropic",
):
    await conn.execute(
        """
        INSERT INTO LLM VALUES(?,?,?,?,?,?,?, ?,?)
        """,
        (
            llm_id,
            machine,
            model,
            enabled,
            provider,
            model,
            "{}",
            "{}",
            "{}",
        ),
    )


@pytest.mark.asyncio
async def test_phone_model_override_and_reasoning_are_resolved_together():
    conn = await _database()
    try:
        await _insert_model(conn, 1, model="claude-opus-4-6")
        await _insert_model(conn, 2)
        await conn.execute(
            "INSERT INTO PROMPTS VALUES(1,1,?,2,?,NULL)",
            (json.dumps({"mode": "high"}), json.dumps({"mode": "off"})),
        )
        result = await _resolve_phone_model_selection(
            conn, prompt_id=1, conversation_llm_id=1
        )
    finally:
        await conn.close()

    assert result == (
        2,
        "standard",
        "claude-sonnet-4-6",
        {"mode": "off"},
        None,
    )


@pytest.mark.asyncio
async def test_inherited_phone_model_inherits_forced_reasoning_only_without_phone_value():
    conn = await _database()
    try:
        await _insert_model(conn, 1)
        await conn.execute(
            "INSERT INTO PROMPTS VALUES(1,1,?,NULL,NULL,NULL)",
            (json.dumps({"mode": "low"}),),
        )
        inherited = await _resolve_phone_model_selection(
            conn, prompt_id=1, conversation_llm_id=1
        )
        await conn.execute(
            "UPDATE PROMPTS SET phone_llm_id=1 WHERE id=1"
        )
        explicit = await _resolve_phone_model_selection(
            conn, prompt_id=1, conversation_llm_id=1
        )
    finally:
        await conn.close()

    assert inherited[3] == {"mode": "low"}
    assert explicit[3] == {"mode": "default"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("machine", "enabled"),
    [("Claude", 0), ("GPTSub", 1), ("GranSabio", 1)],
)
async def test_phone_snapshot_rejects_unavailable_standard_models(machine, enabled):
    conn = await _database()
    try:
        await _insert_model(conn, 1, machine=machine, enabled=enabled)
        await conn.execute("INSERT INTO PROMPTS(id) VALUES(1)")
        with pytest.raises(PhoneSnapshotError):
            await _resolve_phone_model_selection(
                conn, prompt_id=1, conversation_llm_id=1
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_realtime_model_captures_default_voice_and_validates_override():
    conn = await _database()
    try:
        await _insert_model(
            conn,
            2,
            machine="GPT",
            model="gpt-realtime-2.1-mini",
            provider="openai",
        )
        await conn.execute(
            "INSERT INTO PROMPTS(id,phone_llm_id) VALUES(1,2)"
        )
        resolved = await _resolve_phone_model_selection(
            conn, prompt_id=1, conversation_llm_id=2
        )
        await conn.execute(
            "UPDATE PROMPTS SET phone_realtime_voice='invalid' WHERE id=1"
        )
        with pytest.raises(PhoneSnapshotError, match="Realtime voice"):
            await _resolve_phone_model_selection(
                conn, prompt_id=1, conversation_llm_id=2
            )
    finally:
        await conn.close()

    assert resolved == (
        2,
        "openai_realtime",
        "gpt-realtime-2.1-mini",
        {"mode": "default"},
        "marin",
    )


def test_legacy_snapshot_defaults_to_standard_runtime_and_default_reasoning():
    values = {"llm_id": 7}
    assert runtime_llm_id_from_snapshot(values) == 7
    assert runtime_kind_from_snapshot(values) == "standard"
    assert reasoning_selection_from_snapshot(values) == {"mode": "default"}
    assert realtime_voice_from_snapshot(values) is None
