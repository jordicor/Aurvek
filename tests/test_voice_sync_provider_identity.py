import sqlite3
from contextlib import asynccontextmanager

import aiosqlite
import pytest

from ai_runtime.voice_resolution import CanonicalVoice
from tools import voice_sync


@pytest.mark.asyncio
async def test_deprecating_elevenlabs_voice_never_crosses_provider(tmp_path, monkeypatch):
    path = tmp_path / "voice-sync.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE SERVICES (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE VOICES (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                voice_code TEXT NOT NULL,
                tts_service INTEGER NOT NULL,
                deprecated INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO SERVICES VALUES (1, 'TTS-ELEVENLABS'), (2, 'TTS-OPENAI');
            INSERT INTO VOICES VALUES
                (10, 'Eleven duplicate', 'duplicate', 1, 0),
                (20, 'OpenAI duplicate', 'duplicate', 2, 0);
            """
        )
        conn.commit()

    @asynccontextmanager
    async def get_connection(readonly=False):
        conn = await aiosqlite.connect(str(path))
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    monkeypatch.setattr(voice_sync, "get_db_connection", get_connection)
    canonical = CanonicalVoice(
        id=10,
        voice_code="duplicate",
        name="Eleven duplicate",
        tts_service=1,
        service_name="TTS-ELEVENLABS",
        provider="elevenlabs",
        inherited_default=False,
    )

    await voice_sync.mark_voice_deprecated(canonical)

    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT id, deprecated FROM VOICES ORDER BY id"
        ).fetchall()
    assert rows == [(10, 1), (20, 0)]
