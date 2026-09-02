import sqlite3
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from integrations.elevenlabs import service as service_module
from integrations.elevenlabs.service import ElevenLabsService
from ai_runtime.voice_resolution import (
    CanonicalVoiceResolutionError,
    require_elevenlabs_webrtc_compatible,
    resolve_catalog_voice,
    resolve_default_voice,
    resolve_prompt_voice,
)


SCHEMA = """
CREATE TABLE SERVICES (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE VOICES (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    voice_code TEXT NOT NULL,
    tts_service INTEGER NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    deprecated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE PROMPTS (
    id INTEGER PRIMARY KEY,
    name TEXT,
    prompt TEXT,
    description TEXT,
    voice_id INTEGER
);
CREATE TABLE USERS (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL
);
CREATE TABLE CONVERSATIONS (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    role_id INTEGER,
    chat_name TEXT,
    locked INTEGER DEFAULT 0,
    is_incognito INTEGER DEFAULT 0,
    elevenlabs_session_id TEXT,
    elevenlabs_status TEXT
);
CREATE TABLE MESSAGES (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER,
    message TEXT,
    type TEXT,
    date TEXT
);
CREATE TABLE MESSAGE_INPUT_PROVENANCE (
    message_id INTEGER PRIMARY KEY,
    origin TEXT NOT NULL,
    perception TEXT NOT NULL
);
CREATE TABLE WATCHDOG_STATE (
    conversation_id INTEGER,
    prompt_id INTEGER,
    pending_hint TEXT,
    last_evaluated_message_id INTEGER
);
CREATE TABLE ELEVENLABS_AGENTS (
    id INTEGER PRIMARY KEY,
    agent_id TEXT NOT NULL,
    agent_name TEXT,
    is_default INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE PROMPT_AGENT_MAPPING (
    id INTEGER PRIMARY KEY,
    prompt_id INTEGER NOT NULL UNIQUE,
    agent_id TEXT NOT NULL,
    voice_id TEXT
);
"""


@pytest.fixture()
def voice_db(tmp_path):
    path = tmp_path / "voices.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO SERVICES (id, name) VALUES (?, ?)",
            [(1, "TTS-ELEVENLABS"), (5, "TTS-OPENAI")],
        )
        conn.executemany(
            """
            INSERT INTO VOICES
                (id, name, voice_code, tts_service, is_default, deprecated)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (10, "Eleven", "el-canonical", 1, 1, 0),
                (20, "OpenAI", "alloy", 5, 0, 0),
                (30, "Other", "el-other", 1, 0, 0),
            ],
        )
        conn.executemany(
            "INSERT INTO PROMPTS (id, name, prompt, voice_id) VALUES (?, ?, ?, ?)",
            [
                (100, "Explicit Eleven", "Help", 30),
                (200, "Inherited", "Help", None),
                (300, "Explicit OpenAI", "Help", 20),
            ],
        )
        conn.execute("INSERT INTO USERS (id, username) VALUES (1, 'owner')")
        conn.execute(
            "INSERT INTO CONVERSATIONS (id, user_id, role_id, chat_name) "
            "VALUES (1, 1, 100, 'Voice')"
        )
        conn.execute(
            "INSERT INTO MESSAGES (id, conversation_id, message, type, date) "
            "VALUES (50, 1, 'Earlier browser voice', 'user', CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO MESSAGE_INPUT_PROVENANCE(message_id, origin, perception) "
            "VALUES (50, 'web.live_voice', 'transcript_only')"
        )
        conn.execute(
            "INSERT INTO ELEVENLABS_AGENTS "
            "(id, agent_id, agent_name, is_default) "
            "VALUES (1, 'agent-main', 'Main', 1)"
        )
        conn.execute(
            "INSERT INTO PROMPT_AGENT_MAPPING "
            "(id, prompt_id, agent_id, voice_id) "
            "VALUES (1, 100, 'agent-main', 'legacy-override')"
        )
        conn.commit()
    return path


@asynccontextmanager
async def _connect(path):
    conn = await aiosqlite.connect(str(path))
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_prompt_voice_is_canonical_and_default_is_inherited(voice_db):
    async with _connect(voice_db) as conn:
        explicit = await resolve_prompt_voice(100, conn=conn)
        inherited = await resolve_prompt_voice(200, conn=conn)

    assert explicit.voice_code == "el-other"
    assert explicit.provider == "elevenlabs"
    assert explicit.inherited_default is False
    assert inherited.voice_code == "el-canonical"
    assert inherited.provider == "elevenlabs"
    assert inherited.inherited_default is True


@pytest.mark.asyncio
async def test_default_voice_requires_exactly_one_row(voice_db):
    with sqlite3.connect(voice_db) as conn:
        conn.execute("UPDATE VOICES SET is_default = 0")
        conn.commit()

    async with _connect(voice_db) as conn:
        with pytest.raises(CanonicalVoiceResolutionError) as missing:
            await resolve_default_voice(conn=conn)
    assert missing.value.code == "canonical_voice_default_count"

    with sqlite3.connect(voice_db) as conn:
        conn.execute("UPDATE VOICES SET is_default = 1 WHERE id IN (10, 30)")
        conn.commit()

    async with _connect(voice_db) as conn:
        with pytest.raises(CanonicalVoiceResolutionError) as multiple:
            await resolve_default_voice(conn=conn)
    assert multiple.value.code == "canonical_voice_default_count"


@pytest.mark.asyncio
async def test_webrtc_rejects_non_elevenlabs_canonical_voice(voice_db):
    async with _connect(voice_db) as conn:
        voice = await resolve_prompt_voice(300, conn=conn)

    with pytest.raises(CanonicalVoiceResolutionError) as incompatible:
        require_elevenlabs_webrtc_compatible(voice)
    assert incompatible.value.code == "elevenlabs_webrtc_voice_incompatible"


@pytest.mark.asyncio
async def test_catalog_voice_resolves_provider_for_user_and_preview_paths(voice_db):
    async with _connect(voice_db) as conn:
        openai_voice = await resolve_catalog_voice("alloy", conn=conn)
        missing_voice = await resolve_catalog_voice("not-catalogued", conn=conn)

    assert openai_voice is not None
    assert openai_voice.provider == "openai"
    assert missing_voice is None


@pytest.mark.asyncio
async def test_elevenlabs_configuration_ignores_mapping_voice_override(
    voice_db, monkeypatch
):
    @asynccontextmanager
    async def get_connection(readonly=False):
        async with _connect(voice_db) as conn:
            yield conn

    monkeypatch.setattr(service_module, "get_db_connection", get_connection)
    service = ElevenLabsService()
    monkeypatch.setattr(service, "_fetch_signed_url", AsyncMock(return_value=None))

    config = await service.get_configuration(1, 1, False)

    assert config["agent_id"] == "agent-main"
    assert config["voice_id"] == "el-other"
    assert config["voice_provider"] == "elevenlabs"
    assert config["voice_id"] != "legacy-override"
    assert "current origin=web.live_voice" in config["prompt_text"]
    assert "perception=transcript_only" in config["prompt_text"]
    assert "[TRUSTED_INPUT_HISTORY]" in config["prompt_text"]
    assert "[AVCTX:" in config["context"]
    assert all("id" not in message for message in config["recent_messages"])


@pytest.mark.asyncio
async def test_elevenlabs_configuration_fails_closed_for_openai_voice(
    voice_db, monkeypatch
):
    with sqlite3.connect(voice_db) as conn:
        conn.execute("UPDATE CONVERSATIONS SET role_id = 300 WHERE id = 1")
        conn.commit()

    @asynccontextmanager
    async def get_connection(readonly=False):
        async with _connect(voice_db) as conn:
            yield conn

    monkeypatch.setattr(service_module, "get_db_connection", get_connection)
    service = ElevenLabsService()

    with pytest.raises(CanonicalVoiceResolutionError) as incompatible:
        await service.get_configuration(1, 1, False)
    assert incompatible.value.code == "elevenlabs_webrtc_voice_incompatible"
