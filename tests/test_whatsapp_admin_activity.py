"""Regression coverage for the WhatsApp administration activity list."""

import aiosqlite
import pytest

from integrations.whatsapp.admin_routes import _load_active_whatsapp_users


@pytest.mark.asyncio
async def test_last_message_uses_user_id_independently_of_audio_storage(
    tmp_path,
) -> None:
    db_path = tmp_path / "Aurvek.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE USERS(
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                phone_number TEXT
            );
            CREATE TABLE USER_DETAILS(
                user_id INTEGER PRIMARY KEY,
                external_platforms TEXT
            );
            CREATE TABLE WHATSAPP_LOG(
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                user_id INTEGER,
                phone_number TEXT
            );

            INSERT INTO USERS(id, username, phone_number) VALUES
                (1, 'admin', '+34600111333'),
                (2, 'quiet', '+34600999888');
            INSERT INTO USER_DETAILS(user_id, external_platforms) VALUES
                (1, '{"whatsapp":{"conversation_id":42,"answer":"voice"}}'),
                (2, '{"whatsapp":{"conversation_id":84,"answer":"text"}}');
            -- Twilio retains the transport prefix and this is an older profile number.
            INSERT INTO WHATSAPP_LOG(
                id, timestamp, user_id, phone_number
            ) VALUES
                (1, '2026-09-02 20:35:00', 1, 'whatsapp:+34600111222'),
                (2, '2026-09-02 20:36:36', 1, 'whatsapp:+34600111222');
            """
        )
        users = await _load_active_whatsapp_users(conn)

    assert users == [
        {
            "username": "admin",
            "phone_display": "+346***1333",
            "conversation_id": 42,
            "answer_mode": "voice",
            "last_message": "2026-09-02 20:36:36",
        },
        {
            "username": "quiet",
            "phone_display": "+346***9888",
            "conversation_id": 84,
            "answer_mode": "text",
            "last_message": None,
        },
    ]
