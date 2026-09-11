"""Durable application guards without browser, provider, or production state."""

from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from ai_runtime.channel_turns import ChannelContext
from integrations.applications.models import ApplicationContext
from integrations.applications.runtime import admit_application_turn
from integrations.embed import runtime
from integrations.embed.models import EmbedError


def application(**changes):
    values = dict(
        app_id="first", subject="member", user_id=5, context_id="context-a",
        assistant_id="coach", prompt_id=10, conversation_id=1,
        membership_version=1, capabilities={"text": True},
    )
    values.update(changes)
    return ApplicationContext(**values)


@pytest.mark.asyncio
async def test_web_gets_server_resolved_scope_without_request():
    resolved = application(membership_version=4)
    service = SimpleNamespace(authorize_runtime_conversation=AsyncMock(return_value=resolved))
    original = ChannelContext(application=application())
    connection = object()
    admitted = await admit_application_turn(
        connection, conversation_id=1, user_id=5, channel_context=original,
        service=service,
    )
    service.authorize_runtime_conversation.assert_awaited_once_with(connection, 5, 1)
    assert admitted.application is resolved
    assert original.application.membership_version == 1
    with pytest.raises(FrozenInstanceError):
        admitted.application = None
    with pytest.raises(TypeError):
        admitted.application.capabilities["memory"] = True


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("app_id", "second"), ("subject", "other"), ("user_id", 6),
    ("context_id", "context-b"), ("assistant_id", "other"),
    ("prompt_id", 11), ("conversation_id", 2),
])
async def test_supplied_context_cannot_cross_durable_scope(field, value):
    service = SimpleNamespace(authorize_runtime_conversation=AsyncMock(return_value=application()))
    with pytest.raises(EmbedError, match="application_scope_mismatch"):
        await admit_application_turn(
            None, conversation_id=1, user_id=5,
            channel_context=ChannelContext(application=application(**{field: value})),
            service=service,
        )


@pytest.mark.asyncio
async def test_native_conversation_stays_native_and_rejects_forged_attribution():
    service = SimpleNamespace(authorize_runtime_conversation=AsyncMock(return_value=None))
    native = ChannelContext()
    assert await admit_application_turn(
        None, conversation_id=7, user_id=5, channel_context=native, service=service,
    ) is native
    with pytest.raises(EmbedError, match="application_scope_mismatch"):
        await admit_application_turn(
            None, conversation_id=7, user_id=5,
            channel_context=ChannelContext(application=application()), service=service,
        )


@pytest.mark.asyncio
async def test_revocation_is_checked_again_even_with_prior_attribution():
    service = SimpleNamespace(authorize_runtime_conversation=AsyncMock(return_value=application()))
    admitted = await admit_application_turn(
        None, conversation_id=1, user_id=5, channel_context=ChannelContext(), service=service,
    )
    service.authorize_runtime_conversation.side_effect = EmbedError("membership_inactive", 403)
    with pytest.raises(EmbedError, match="membership_inactive"):
        await admit_application_turn(
            None, conversation_id=1, user_id=5, channel_context=admitted, service=service,
        )
    assert service.authorize_runtime_conversation.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("context,attachments,text_allowed", [
    (ChannelContext(), True, True),
    (ChannelContext(channel="whatsapp"), False, True),
    (ChannelContext(channel="telegram"), False, True),
    (ChannelContext(channel="device"), False, True),
    (ChannelContext(channel="phone"), False, True),
    (ChannelContext(channel="telegram", input_origin="telegram.voice_note",
                    input_perception="transcript_only"), False, True),
    (ChannelContext(persistence="ingest_only"), False, True),
    (ChannelContext(), False, False),
])
async def test_application_admission_rejects_missing_capabilities_or_channel_proof(context, attachments, text_allowed):
    service = SimpleNamespace(authorize_runtime_conversation=AsyncMock(
        return_value=application(capabilities={"text": text_allowed, "voice": True}),
    ))
    expected = ("application_channel_required" if context.channel in {"phone", "telegram", "whatsapp"}
                else "application_capability_denied")
    with pytest.raises(EmbedError, match=expected):
        await admit_application_turn(
            None, conversation_id=1, user_id=5, channel_context=context,
            has_attachments=attachments, service=service,
        )


@pytest.mark.asyncio
async def test_web_attachments_use_the_authorized_application_context():
    resolved = application(capabilities={"text": True, "attachments": True})
    service = SimpleNamespace(authorize_runtime_conversation=AsyncMock(return_value=resolved))
    admitted = await admit_application_turn(
        None, conversation_id=1, user_id=5, channel_context=ChannelContext(),
        has_attachments=True, service=service,
    )
    assert admitted.application is resolved


def test_application_cannot_enter_gransabio_without_effective_capability():
    assert ChannelContext(application=application()).bypass_gransabio
    assert not ChannelContext().bypass_gransabio
    gransabio = application(capabilities={"text": True, "gransabio": True, "multi_ai": False})
    assert not ChannelContext(application=gransabio).bypass_gransabio
    assert ChannelContext(channel="phone", application=gransabio).bypass_gransabio


@pytest.fixture
def application_db(tmp_path, monkeypatch):
    path = tmp_path / "runtime.sqlite"
    with sqlite3.connect(path) as connection:
        # Lowercase proves table detection follows SQLite's identifier semantics.
        connection.executescript("""
        CREATE TABLE application_conversations (
            conversation_id INTEGER PRIMARY KEY, app_id TEXT, subject TEXT,
            user_id INTEGER, context_id TEXT, assistant_id TEXT, prompt_id INTEGER);
        INSERT INTO application_conversations VALUES(1,'first','member',5,'a','coach',10);
        INSERT INTO application_conversations VALUES(2,'second','member',5,'a','coach',10);
        CREATE TABLE APPLICATION_CONTEXT_SOURCES (
            conversation_id INTEGER PRIMARY KEY, source_conversation_id INTEGER);
        """)

    @asynccontextmanager
    async def connect(readonly=False):
        async with aiosqlite.connect(path) as connection:
            connection.row_factory = aiosqlite.Row
            yield connection

    monkeypatch.setattr(runtime.database, "get_db_connection", connect)
    return path


@pytest.mark.asyncio
async def test_incomplete_app_scope_never_falls_back_to_native_memory(application_db, monkeypatch):
    from ai_runtime.memory import context, recording

    forbidden = AsyncMock(side_effect=AssertionError("Unscoped memory provider accessed"))
    monkeypatch.setattr(context, "get_active_memory_provider", forbidden)
    monkeypatch.setattr(recording, "get_active_memory_provider", forbidden)
    for conversation_id in (1, 2):
        assert await runtime.is_embed_conversation(conversation_id)
        assert await runtime.append_interview_brief("Prompt", conversation_id, 5) == "Prompt"
        with pytest.raises((EmbedError, sqlite3.OperationalError)):
            await context._resolve_memory_context(
                "Prompt", user_id=5, conversation_id=conversation_id, message="Test",
            )
        with pytest.raises((EmbedError, sqlite3.OperationalError)):
            await context._warmup_memory_provider(5, conversation_id)
        assert not await recording._record_memory_turn_best_effort(
            user_id=5, conversation_id=conversation_id, assistant_content="Test",
        )
    forbidden.assert_not_awaited()
    assert not await runtime.is_embed_conversation(99)
    with pytest.raises(PermissionError):
        await runtime.append_interview_brief("Prompt", 1, 99)


@pytest.mark.asyncio
async def test_broken_application_schema_never_enables_unscoped_memory(application_db):
    with sqlite3.connect(application_db) as connection:
        connection.execute("ALTER TABLE application_conversations RENAME COLUMN context_id TO broken")
    with pytest.raises(sqlite3.OperationalError):
        await runtime.is_embed_conversation(1)


@pytest.mark.asyncio
async def test_disagreeing_legacy_binding_fails_closed(application_db):
    with sqlite3.connect(application_db) as connection:
        connection.executescript("""
        CREATE TABLE EMBED_INTERVIEWS (
            conversation_id INTEGER, app_id TEXT, subject TEXT, user_id INTEGER,
            external_project_id TEXT, brief_json TEXT);
        INSERT INTO EMBED_INTERVIEWS VALUES(1,'second','member',5,'p','{}');
        """)
    with pytest.raises(PermissionError, match="bindings disagree"):
        await runtime.is_embed_conversation(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["web", "whatsapp", "telegram", "device", "phone"])
async def test_message_entrypoint_denies_before_spend_or_provider(monkeypatch, channel):
    from ai_runtime import messages
    from integrations.applications import runtime as application_runtime

    @asynccontextmanager
    async def connect(**_):
        # No downstream SQL, reservation, or provider is reachable after denial.
        yield object()

    denied = AsyncMock(side_effect=EmbedError("membership_inactive", 403))
    monkeypatch.setattr(application_runtime, "admit_application_turn", denied)
    monkeypatch.setattr(messages, "get_db_connection", connect)
    monkeypatch.setattr(messages, "ensure_conversation_privacy_schema", AsyncMock())
    response = await messages.process_save_message(
        request=None, conversation_id=1,
        current_user=SimpleNamespace(id=5, can_send_files=True),
        text_plain="Test", prevalidated=True, full_response=True,
        channel_context=ChannelContext(channel=channel),
    )
    assert response.status_code == 403
    assert json.loads(response.body)["error_code"] == "membership_inactive"
    denied.assert_awaited_once()
