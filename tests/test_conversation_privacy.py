from __future__ import annotations

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
