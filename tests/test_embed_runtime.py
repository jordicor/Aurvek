"""Embed isolation at the durable context and actual streaming lifecycle."""

from contextlib import asynccontextmanager
import json
import sqlite3

import aiosqlite
import pytest

from integrations.embed import activity, runtime
from integrations.embed.models import EmbedPrincipal, EmbedError


@pytest.fixture
def interview_db(tmp_path, monkeypatch):
    path = tmp_path / "embed-runtime.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE EMBED_INTERVIEWS (
            app_id TEXT, subject TEXT, user_id INTEGER, external_project_id TEXT,
            conversation_id INTEGER UNIQUE, brief_json TEXT)""")
        for conversation_id, app_id, language in [(1, "first", "es"), (2, "second", "ja")]:
            conn.execute("INSERT INTO EMBED_INTERVIEWS VALUES (?,?,?,?,?,?)", (
                app_id, "subject", 5, "project", conversation_id,
                json.dumps({"schema_version": 1, "revision": 1, "language": language}),
            ))

    @asynccontextmanager
    async def connect(readonly=False):
        async with aiosqlite.connect(path) as conn:
            conn.row_factory = aiosqlite.Row
            yield conn

    monkeypatch.setattr(runtime.database, "get_db_connection", connect)
    return path


@pytest.mark.asyncio
async def test_brief_is_bound_to_conversation_and_owner(interview_db):
    first = await runtime.append_interview_brief("Shared prompt", 1, 5)
    second = await runtime.append_interview_brief("Shared prompt", 2, 5)
    assert '"language":"es"' in first and '"language":"ja"' not in first
    assert '"language":"ja"' in second and '"language":"es"' not in second
    assert await runtime.append_interview_brief("Native prompt", 3, 5) == "Native prompt"
    with pytest.raises(PermissionError):
        await runtime.append_interview_brief("Shared prompt", 1, 99)


@pytest.mark.asyncio
async def test_binding_survives_without_browser_session(interview_db):
    assert await runtime.is_embed_conversation(1)
    assert await runtime.is_embed_conversation(2)
    assert not await runtime.is_embed_conversation(3)


@pytest.mark.asyncio
async def test_unmigrated_embed_scope_cannot_access_unscoped_provider(interview_db, monkeypatch):
    from ai_runtime.memory import context, recording

    async def forbidden_provider():
        pytest.fail("An embed interview attempted to enter unscoped provider memory")

    monkeypatch.setattr(context, "get_active_memory_provider", forbidden_provider)
    monkeypatch.setattr(recording, "get_active_memory_provider", forbidden_provider)
    with pytest.raises(EmbedError, match="service_unavailable"):
        await context._resolve_memory_context(
            "Interview prompt", user_id=5, conversation_id=1, message="Non-sensitive test",
        )
    with pytest.raises(EmbedError, match="service_unavailable"):
        await context._warmup_memory_provider(5, 2)
    assert not await recording._record_memory_turn_best_effort(
        user_id=5, conversation_id=1, assistant_content="Not sent to memory",
    )


@pytest.mark.asyncio
async def test_database_failure_cannot_enable_global_memory(interview_db):
    with sqlite3.connect(interview_db) as conn:
        conn.execute("ALTER TABLE EMBED_INTERVIEWS RENAME COLUMN brief_json TO broken")
    with pytest.raises(sqlite3.OperationalError):
        await runtime.is_embed_conversation(1)


def principal(conversation_id=1, session="session-a"):
    return EmbedPrincipal(
        app_id="first", issuer="https://identity.example", subject="subject",
        user_id=5, prompt_id=10, delegated_session_id=session,
        session_version=1, membership_version=1, expires_at=9999999999,
        conversation_id=conversation_id,
    )


@pytest.fixture(autouse=True)
def allow_test_principal(monkeypatch):
    activity._ended_sessions.clear()
    async def revalidate(principal):
        return None

    monkeypatch.setattr(activity, "_revalidate", revalidate)


@pytest.mark.asyncio
async def test_logout_between_revalidation_and_admission_never_starts_generation(monkeypatch):
    async def concurrent_logout(p):
        # The DB read succeeded, but logout completed while that connection closed.
        await activity.end_session_activity(p.delegated_session_id, p.user_id)

    monkeypatch.setattr(activity, "_revalidate", concurrent_logout)
    with pytest.raises(Exception) as error:
        async with activity.track_embed_generation(principal()):
            pytest.fail("A logged-out session began a paid generation")
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_stop_acknowledges_only_finished_stream(monkeypatch):
    async def signal(item):
        item.ending = True

    monkeypatch.setattr(activity, "_signal_stop", signal)
    p = principal()
    async with activity.track_embed_generation(p):
        assert (await activity.get_activity(p))["activity"] == "generating"
        assert await activity.request_stop(p) == {"activity": "ending", "closed": False}
        with pytest.raises(Exception) as error:
            async with activity.track_embed_generation(p):
                pytest.fail("A duplicate generation was admitted")
        assert error.value.status_code == 409
    assert await activity.get_activity(p) == {"activity": "idle", "closed": True}


@pytest.mark.asyncio
async def test_logout_stops_only_its_delegated_session(monkeypatch):
    stopped = []

    async def signal(item):
        stopped.append(item.conversation_id)
        item.ending = True

    monkeypatch.setattr(activity, "_signal_stop", signal)
    async with activity.track_embed_generation(principal(1)):
        async with activity.track_embed_generation(principal(2, "session-b")):
            result = await activity.end_session_activity("session-a", 5)
            assert result == {"activity": "ending", "closed": False}
            assert stopped == [1]
            assert await activity.activity_state(2) == "generating"
    assert await activity.end_session_activity("session-a", 5) == {
        "activity": "idle", "closed": True,
    }
