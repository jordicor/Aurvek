import json
import sqlite3
from contextlib import asynccontextmanager

import aiosqlite
import pytest

from chat.services.conversation_channels import get_conversation_channel_summaries
from integrations import conversations as platform_conversations


def _create_channel_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE USER_ROLES (
            id INTEGER PRIMARY KEY,
            role_name TEXT NOT NULL
        );
        CREATE TABLE USERS (
            id INTEGER PRIMARY KEY,
            role_id INTEGER NOT NULL,
            phone_number TEXT,
            phone_verified INTEGER NOT NULL DEFAULT 0,
            telegram_chat_id TEXT,
            is_enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE USER_DETAILS (
            user_id INTEGER PRIMARY KEY,
            external_platforms TEXT
        );
        CREATE TABLE SYSTEM_CONFIG (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE CONVERSATIONS (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            chat_name TEXT,
            locked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE PHONE_CONTACTS (
            id INTEGER PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            display_name TEXT NOT NULL,
            e164 TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE PHONE_CONVERSATION_BINDINGS (
            id INTEGER PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            conversation_id INTEGER NOT NULL,
            contact_id INTEGER NOT NULL,
            allow_inbound INTEGER NOT NULL DEFAULT 1,
            allow_outbound INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO USER_ROLES VALUES (1, 'admin'), (2, 'user');
        """
    )
    conn.commit()
    conn.close()


@asynccontextmanager
async def _connection(path, readonly=False):
    mode = "ro" if readonly else "rwc"
    conn = await aiosqlite.connect(f"file:{path}?mode={mode}", uri=True)
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_channel_summary_combines_channels_without_exposing_e164(tmp_path):
    db_path = tmp_path / "channels.db"
    _create_channel_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO USERS VALUES (?, ?, ?, ?, ?, ?)",
        (1, 2, "+13055550123", 1, "tg-1", 1),
    )
    conn.execute(
        "INSERT INTO USER_DETAILS VALUES (?, ?)",
        (
            1,
            json.dumps(
                {
                    "whatsapp": {"conversation_id": 10},
                    "telegram": {"conversation_id": 10},
                }
            ),
        ),
    )
    conn.execute("INSERT INTO CONVERSATIONS VALUES (10, 1, 'Combined', 0)")
    conn.execute(
        "INSERT INTO PHONE_CONTACTS VALUES (7, 1, 'Jordi', '+13055550123', 1)"
    )
    conn.execute(
        "INSERT INTO PHONE_CONVERSATION_BINDINGS VALUES (8, 1, 10, 7, 1, 0, 1)"
    )
    conn.commit()
    conn.close()

    async with _connection(db_path, readonly=True) as db:
        summaries = await get_conversation_channel_summaries(1, conn=db)

    assert summaries[10]["external_channels"] == [
        "whatsapp",
        "telegram",
        "phone",
    ]
    assert summaries[10]["phone_binding"] == {
        "id": 8,
        "contact_id": 7,
        "display_name": "Jordi",
        "allow_inbound": True,
        "allow_outbound": False,
    }
    assert "+13055550123" not in json.dumps(summaries)


@pytest.mark.asyncio
async def test_phone_projection_requires_live_enabled_matching_user_number(tmp_path):
    db_path = tmp_path / "phone-eligibility.db"
    _create_channel_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO USERS VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, 1, "+13055550101", 0, None, 1),
            (2, 2, "+13055550102", 1, None, 0),
            (3, 1, "+13055550103", 0, None, 1),
        ],
    )
    conn.executemany(
        "INSERT INTO USER_DETAILS VALUES (?, '{}')",
        [(1,), (2,), (3,)],
    )
    conn.executemany(
        "INSERT INTO PHONE_CONTACTS VALUES (?, ?, ?, ?, 1)",
        [
            (11, 1, "Admin matching", "+13055550101"),
            (12, 2, "Disabled user", "+13055550102"),
            (13, 3, "Admin mismatch", "+13055550999"),
        ],
    )
    conn.executemany(
        "INSERT INTO PHONE_CONVERSATION_BINDINGS VALUES (?, ?, ?, ?, 1, 1, 1)",
        [(21, 1, 101, 11), (22, 2, 102, 12), (23, 3, 103, 13)],
    )
    conn.commit()
    conn.close()

    async with _connection(db_path, readonly=True) as db:
        admin_matching = await get_conversation_channel_summaries(1, conn=db)
    async with _connection(db_path, readonly=True) as db:
        disabled_user = await get_conversation_channel_summaries(2, conn=db)
    async with _connection(db_path, readonly=True) as db:
        admin_mismatch = await get_conversation_channel_summaries(3, conn=db)

    assert admin_matching[101]["external_channels"] == ["phone"]
    assert disabled_user == {}
    assert admin_mismatch == {}


@pytest.mark.asyncio
async def test_assigning_messaging_channel_preserves_the_other_channel(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "messaging-coexistence.db"
    _create_channel_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO USERS VALUES (?, ?, ?, ?, ?, ?)",
        (1, 2, "+13055550123", 1, "tg-1", 1),
    )
    conn.execute(
        "INSERT INTO USER_DETAILS VALUES (?, ?)",
        (
            1,
            json.dumps(
                {
                    "whatsapp": {"conversation_id": "1", "answer": "voice"},
                    "telegram": {"conversation_id": 2, "answer": "text"},
                }
            ),
        ),
    )
    conn.executemany(
        "INSERT INTO CONVERSATIONS VALUES (?, 1, ?, 0)",
        [(1, "Old WhatsApp"), (2, "Telegram"), (3, "Target")],
    )
    conn.commit()
    conn.close()

    @asynccontextmanager
    async def _test_connection(readonly=False):
        async with _connection(db_path, readonly=readonly) as db:
            yield db

    monkeypatch.setattr(platform_conversations, "get_db_connection", _test_connection)

    result = await platform_conversations.set_external_conversation(
        1, 3, "whatsapp", "whatsapp"
    )
    assert result["affected_conversation_ids"] == [3, 1]

    conn = sqlite3.connect(db_path)
    platforms = json.loads(
        conn.execute(
            "SELECT external_platforms FROM USER_DETAILS WHERE user_id=1"
        ).fetchone()[0]
    )
    conn.close()
    assert platforms["whatsapp"]["conversation_id"] == 3
    assert platforms["whatsapp"]["answer"] == "voice"
    assert platforms["telegram"]["conversation_id"] == 2
