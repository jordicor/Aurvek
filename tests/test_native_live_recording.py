"""Native turn originals use the existing private, quota-accounted attachments."""

import base64
from dataclasses import replace
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import wave

from fastapi import HTTPException
import pytest
import pytest_asyncio

from ai_runtime.channel_turns import ChannelContext, StaleChannelTurnError
from ai_runtime.persistence import messages as persistence
import file_storage
from integrations.applications.web_audio import load_original_recordings
from integrations.live.session import LiveMediaSession
from tests.test_file_storage import _seed_user_conversation


@pytest_asyncio.fixture
async def live_recording(mock_db, monkeypatch, tmp_path):
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "blobs")
    monkeypatch.setattr(persistence, "get_db_connection", mock_db)
    monkeypatch.setattr(persistence, "_record_memory_turn_best_effort", AsyncMock())
    monkeypatch.setattr(persistence, "record_chat_turn", AsyncMock())
    monkeypatch.setattr("chat.services.privacy.is_incognito_conversation", AsyncMock(return_value=False))
    async with mock_db() as connection:
        await _seed_user_conversation(connection)
        columns = {row[1] for row in await (await connection.execute("PRAGMA table_info(MESSAGES)")).fetchall()}
        if "citations_json" not in columns:
            await connection.execute("ALTER TABLE MESSAGES ADD COLUMN citations_json TEXT")
        await connection.execute("CREATE TABLE IF NOT EXISTS MESSAGE_INPUT_PROVENANCE "
            "(message_id INTEGER PRIMARY KEY,origin TEXT,perception TEXT)")
        await file_storage.ensure_file_storage_schema(connection)
        await connection.commit()
    session = LiveMediaSession(SimpleNamespace(send_json=AsyncMock()), conversation_id=1,
        user=SimpleNamespace(id=1), request=None, check_identity=AsyncMock())
    session.model_id = 1
    session.settings = {"kind": "gemini_live", "model": {"model": "synthetic-live"},
                        "conversation": {"prompt_id": 1}}
    session.foreground = SimpleNamespace(context=ChannelContext())
    session.client = SimpleNamespace(_aurvek_internal_realtime_bridge=True, send_audio=AsyncMock())
    session.check_access = AsyncMock()
    return session


async def input_events(session, messages):
    session.websocket.receive = AsyncMock(side_effect=messages + [
        {"type": "websocket.receive", "text": '{"event":"stop"}'}])
    await session._input()


async def reply(session, audio, text):
    await session._provider_event({"type": "session.output_audio.delta",
        "delta": base64.b64encode(audio).decode("ascii")})
    await session._provider_event({"type": "session.output_transcript.delta", "delta": text})
    await session._provider_event({"type": "session.turn.completed"})


async def acknowledge(session):
    await input_events(session, [{"type": "websocket.receive", "text": json.dumps({
        "event": "played", "generation": session.generation, "id": mark,
    })} for mark in session.audio_marks])


async def originals(connection):
    return await (await connection.execute(
        "SELECT m.id,m.message,m.type,a.public_id FROM MESSAGES m "
        "LEFT JOIN FILE_ATTACHMENTS a ON a.message_id=m.id ORDER BY m.id")).fetchall()


async def read_wav(reference):
    content, _ = await file_storage.read_attachment_bytes(reference, user_id=1, conversation_id=1)
    with wave.open(io.BytesIO(content), "rb") as recording:
        assert (recording.getnchannels(), recording.getsampwidth()) == (1, 2)
        return recording.getframerate(), recording.readframes(recording.getnframes())


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gemini_live", "openai_live"])
async def test_real_samples_link_to_each_message_only_after_playback(live_recording, mock_db, kind):
    session = live_recording
    session.settings["kind"] = kind
    user_pcm, bot_pcm = b"\x00\x10" * 320, b"\x00\x20" * 480
    await input_events(session, [{"type": "websocket.receive", "bytes": user_pcm}])
    await session._provider_event({"type": "session.input_transcript.delta", "delta": "Hello"})
    await reply(session, bot_pcm, "Good morning")
    await session._flush()
    async with mock_db() as connection:
        rows = await originals(connection)
    assert [row["message"] for row in rows] == ["Hello"]
    assert await read_wav(rows[0]["public_id"]) == (16000, user_pcm)
    if kind == "openai_live":
        assert session.client.send_audio.await_args.args[0] != user_pcm  # Playback retains input before resampling.

    await acknowledge(session)
    await session._flush()
    await input_events(session, [{"type": "websocket.receive", "bytes": user_pcm[::-1]}])
    await session._provider_event({"type": "session.input_transcript.delta", "delta": "Another question"})
    await session._flush(final=True)
    async with mock_db() as connection:
        rows = await originals(connection)
        metadata = await load_original_recordings(connection, None, [row["id"] for row in rows],
            conversation_id=1, user_id=1)
        from tools.download_mp3 import _discover_original_audio_sources
        download_sources = await _discover_original_audio_sources(connection, 1)
        assert set(download_sources) == {row["id"] for row in rows}
        assert all(source.kind == "attachment" for source in download_sources.values())
        assert await load_original_recordings(connection, None, [row["id"] for row in rows],
            conversation_id=1, user_id=2) == {}
        assert await load_original_recordings(connection, None, [row["id"] for row in rows],
            conversation_id=2, user_id=1) == {}
    assert [row["message"] for row in rows] == ["Hello", "Good morning", "Another question"]
    assert await read_wav(rows[1]["public_id"]) == (24000, bot_pcm)
    assert await read_wav(rows[2]["public_id"]) == (16000, user_pcm[::-1])
    assert len(metadata) == 3
    assert all(value["original_audio_url"].startswith("/api/attachments/")
               and value["original_audio_mime"] == "audio/wav" for value in metadata.values())


@pytest.mark.asyncio
async def test_interruption_and_final_drain_never_attach_unplayed_audio(live_recording, mock_db):
    session = live_recording
    heard, unheard, resumed = (value * 480 for value in (b"\x00\x10", b"\x00\x20", b"\x00\x30"))
    await reply(session, heard, "Heard")
    await acknowledge(session)
    await reply(session, unheard, "Canceled")
    await session._provider_event({"type": "session.output_audio.interrupted"})
    await reply(session, resumed, "Resumed")
    await acknowledge(session)
    await session._provider_event({"type": "session.output_audio.delta",
        "delta": base64.b64encode(unheard).decode("ascii")}, draining=True)
    await session._flush(final=True)
    async with mock_db() as connection:
        rows = await originals(connection)
    assert [row["message"] for row in rows] == ["Heard", "Resumed"]
    assert await read_wav(rows[0]["public_id"]) == (24000, heard)
    assert await read_wav(rows[1]["public_id"]) == (24000, resumed)


@pytest.mark.asyncio
async def test_storage_quota_failure_preserves_text_and_notifies_once(live_recording, mock_db):
    session = live_recording
    async with mock_db() as connection:
        await connection.execute("INSERT INTO USER_DETAILS(user_id,storage_quota_bytes) VALUES(1,1)")
        await connection.commit()
    for text in ("First", "Second"):
        await session._persist(text, "user", audio=b"\x00\x10" * 320)
    async with mock_db() as connection:
        rows = await originals(connection)
        assert (await (await connection.execute("SELECT COUNT(*) FROM FILE_ATTACHMENTS")).fetchone())[0] == 0
    assert [row["message"] for row in rows] == ["First", "Second"]
    session.websocket.send_json.assert_awaited_once_with({"event": "notice", "generation": 0,
                                                        "message": "voice_recording_unavailable"})


@pytest.mark.asyncio
async def test_revoked_commit_discards_pending_recording(live_recording, mock_db):
    session = live_recording
    session.foreground.context = replace(session.foreground.context, commit_guard=AsyncMock(return_value=False))
    with pytest.raises(StaleChannelTurnError):
        await session._persist("Stale", "user", audio=b"\x00\x10" * 320)
    async with mock_db() as connection:
        assert await originals(connection) == []
        assert (await (await connection.execute("SELECT COUNT(*) FROM FILE_ATTACHMENTS")).fetchone())[0] == 0
        assert (await (await connection.execute("SELECT COUNT(*) FROM FILE_BLOBS")).fetchone())[0] == 0


@pytest.mark.asyncio
async def test_original_playback_is_private_and_quiet_edges_keep_context(live_recording, mock_db, monkeypatch):
    from chat.routes import attachments
    session = live_recording
    speech = b"\x00\x10" * 16000
    await session._persist("Recorded", "user", audio=b"\x00\x00" * 32000 + speech + b"\x00\x00" * 32000)
    async with mock_db() as connection:
        reference = (await originals(connection))[0]["public_id"]
    assert await read_wav(reference) == (16000, b"\x00\x00" * 8000 + speech + b"\x00\x00" * 8000)

    class Reader:
        id = 1

        @property
        async def is_admin(self):
            return False

    monkeypatch.setattr(attachments, "get_db_connection", mock_db)
    response = await attachments.serve_attachment_file(reference, Reader())
    assert response.media_type == "audio/wav"
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    Reader.id = 2
    with pytest.raises(HTTPException) as exc:
        await attachments.serve_attachment_file(reference, Reader())
    assert exc.value.status_code == 404
