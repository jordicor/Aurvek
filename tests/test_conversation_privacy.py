from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import orjson
import pytest

from chat.services.privacy import (
    ensure_conversation_privacy_schema,
    get_conversation_privacy,
    mark_conversation_incognito,
    purge_conversation_local_records,
)


@pytest.mark.asyncio
async def test_mark_conversation_incognito_hides_and_purges_on_close(mock_db) -> None:
    async with mock_db() as conn:
        await ensure_conversation_privacy_schema(conn)
        await conn.execute(
            "INSERT INTO CONVERSATIONS (id, user_id, role_id) VALUES (1, 7, 42)"
        )
        await conn.commit()

    async with mock_db() as conn:
        changed = await mark_conversation_incognito(
            conn,
            conversation_id=1,
            user_id=7,
            incognito=True,
        )
        await conn.commit()

    assert changed is True
    privacy = await get_conversation_privacy(1, user_id=7)
    assert privacy is not None
    assert privacy["is_incognito"] == 1
    assert privacy["hidden_from_history"] == 1
    assert privacy["purge_on_close"] == 1


@pytest.mark.asyncio
async def test_purge_conversation_local_records_deletes_messages_and_links(mock_db) -> None:
    async with mock_db() as conn:
        await ensure_conversation_privacy_schema(conn)
        await conn.execute(
            "INSERT INTO CONVERSATIONS (id, user_id, role_id) VALUES (2, 9, 77)"
        )
        await conn.execute(
            """
            INSERT INTO MESSAGES (id, conversation_id, user_id, message, type, date)
            VALUES (20, 2, 9, 'secret', 'user', '2026-05-01 10:00:00')
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS MEMORY_PROVIDER_MESSAGE_LINKS (
                message_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                provider_message_id TEXT NOT NULL,
                provider_event_id TEXT,
                conversation_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                source TEXT NOT NULL DEFAULT 'live',
                synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (message_id, provider),
                FOREIGN KEY (message_id) REFERENCES MESSAGES(id)
            )
            """
        )
        await conn.execute(
            """
            INSERT INTO MEMORY_PROVIDER_MESSAGE_LINKS
                (message_id, provider, provider_message_id, conversation_id, user_id, role)
            VALUES (20, 'atagia', 'aurvek:msg:20', 2, 9, 'user')
            """
        )
        await mark_conversation_incognito(
            conn,
            conversation_id=2,
            user_id=9,
            incognito=True,
        )
        await conn.commit()

    assert await purge_conversation_local_records(conversation_id=2, user_id=9) is True

    async with mock_db() as conn:
        conv = await (await conn.execute("SELECT id FROM CONVERSATIONS WHERE id = 2")).fetchone()
        msg = await (await conn.execute("SELECT id FROM MESSAGES WHERE id = 20")).fetchone()
        link = await (await conn.execute("SELECT message_id FROM MEMORY_PROVIDER_MESSAGE_LINKS WHERE message_id = 20")).fetchone()

    assert conv is None
    assert msg is None
    assert link is None


@pytest.mark.asyncio
async def test_save_incognito_preserves_chat_and_checks_access(mock_db, monkeypatch) -> None:
    from fastapi import HTTPException
    from chat.routes import conversations
    from integrations.applications import web
    from ai_runtime.context.warmup import _build_warmup_cache_key_from_state
    from chat.services.warmup import get_snapshot, put_snapshot

    monkeypatch.setattr(conversations, "get_db_connection", mock_db)
    async with mock_db() as conn:
        await ensure_conversation_privacy_schema(conn)
        columns = {row["name"] for row in await (await conn.execute("PRAGMA table_info(CONVERSATIONS)")).fetchall()}
        for column in ("start_date TEXT", "folder_id INTEGER", "llm_id INTEGER"):
            if column.split()[0] not in columns:
                await conn.execute(f"ALTER TABLE CONVERSATIONS ADD COLUMN {column}")
        columns = {row["name"] for row in await (await conn.execute("PRAGMA table_info(PROMPTS)")).fetchall()}
        for column in ("forced_llm_id INTEGER", "hide_llm_name INTEGER", "allowed_llms TEXT",
                       "disable_web_search INTEGER", "force_web_search INTEGER"):
            if column.split()[0] not in columns:
                await conn.execute(f"ALTER TABLE PROMPTS ADD COLUMN {column}")
        await conn.execute("INSERT INTO LLM(id,machine,model) VALUES(3,'GPT','test-model')")
        await conn.execute("INSERT INTO PROMPTS(id,name,allowed_llms) VALUES(42,'Test prompt','[3]')")
        await conn.execute("""INSERT INTO CONVERSATIONS
            (id,user_id,role_id,llm_id,chat_name,start_date,is_incognito,hidden_from_history,
             purge_on_close,incognito_closed_at)
            VALUES(11,7,42,3,'A useful chat','2026-09-07',1,1,1,'old marker')""")
        await conn.execute("""INSERT INTO MESSAGES(id,conversation_id,user_id,message,type,date)
            VALUES(110,11,7,'Keep this message','user','2026-09-07')""")
        await conn.execute("""INSERT INTO GENERATED_MEDIA_FILES
            (id,user_id,conversation_id,kind,rel_path,size_bytes)
            VALUES(12,7,11,'image','preserved/image.png',15)""")
        await conn.commit()

    user = SimpleNamespace(id=7)
    response = await conversations.save_incognito_conversation(11, None)
    assert response.status_code == 401
    with pytest.raises(HTTPException) as denied_owner:
        await conversations.save_incognito_conversation(11, SimpleNamespace(id=8))
    assert denied_owner.value.status_code == 404

    with monkeypatch.context() as blocked:
        blocked.setattr(web, "authorize_application_read", AsyncMock(
            return_value=SimpleNamespace(capabilities={"conversation_controls": False})))
        with pytest.raises(HTTPException) as denied_app:
            await conversations.save_incognito_conversation(11, user)
        assert denied_app.value.status_code == 403
    assert (await get_conversation_privacy(11))["is_incognito"] == 1

    old_key = _build_warmup_cache_key_from_state({"is_incognito": True}, 7, 11)
    put_snapshot(old_key, {"state": {"is_incognito": True}})
    try:
        result = orjson.loads((await conversations.save_incognito_conversation(11, user)).body)
        repeated = orjson.loads((await conversations.save_incognito_conversation(11, user)).body)
        assert result["success"] and not result["already_saved"]
        assert repeated["already_saved"]
        assert repeated["conversation"] == result["conversation"]
        chat = result["conversation"]
        assert (chat["id"], chat["chat_name"], chat["llm_id"], chat["folder_id"]) == (11, "A useful chat", 3, None)
        assert chat["allowed_llms"] == [3] and chat["prompt_name"] == "Test prompt"
        privacy = await get_conversation_privacy(11)
        assert not any(privacy[key] for key in ("is_incognito", "hidden_from_history", "purge_on_close"))
        assert privacy["incognito_closed_at"] is None
        assert get_snapshot(_build_warmup_cache_key_from_state(chat, 7, 11)) is None
        async with mock_db() as conn:
            message = await (await conn.execute("SELECT message,date FROM MESSAGES WHERE id=110")).fetchone()
            media = await (await conn.execute("SELECT rel_path FROM GENERATED_MEDIA_FILES WHERE id=12")).fetchone()
        assert tuple(message) == ("Keep this message", "2026-09-07")
        assert media[0] == "preserved/image.png"
    finally:
        from chat.services.warmup import clear_warmup_cache
        clear_warmup_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_cleanup", [False, True])
async def test_queued_incognito_close_cannot_purge_saved_chat(mock_db, monkeypatch, stale_cleanup) -> None:
    from chat.services import deletion
    from chat.services.locks import conversation_write_lock

    async with mock_db() as conn:
        await ensure_conversation_privacy_schema(conn)
        await conn.execute("""INSERT INTO CONVERSATIONS
            (id,user_id,role_id,is_incognito,hidden_from_history,purge_on_close)
            VALUES(11,7,42,1,1,1)""")
        await conn.commit()
    purge_memory = AsyncMock(return_value=set())
    delete_files = AsyncMock()
    monkeypatch.setattr(deletion, "purge_linked_memory_providers_best_effort", purge_memory)
    monkeypatch.setattr(deletion, "delete_conversation_files_for_user", delete_files)
    close_waiting = asyncio.Event()

    @asynccontextmanager
    async def observe_close_lock(conversation_id):
        close_waiting.set()
        async with conversation_write_lock(conversation_id):
            yield

    monkeypatch.setattr(deletion, "conversation_write_lock", observe_close_lock)
    user = SimpleNamespace(id=7, username="test-user")
    async with conversation_write_lock(11):
        closing = asyncio.create_task(
            deletion.purge_stale_incognito_conversations_for_user(user) if stale_cleanup
            else deletion.close_incognito_conversation_for_user(user, 11)
        )
        await asyncio.wait_for(close_waiting.wait(), timeout=5)
        async with mock_db() as conn:
            await mark_conversation_incognito(conn, conversation_id=11, user_id=7, incognito=False)
            await conn.commit()
    await asyncio.wait_for(closing, timeout=5)
    purge_memory.assert_not_awaited()
    delete_files.assert_not_awaited()
    assert (await get_conversation_privacy(11))["is_incognito"] == 0
