from __future__ import annotations

import aiosqlite
import pytest

from ai_runtime.channel_turns import ChannelCommit, StaleChannelTurnError
from integrations.telephony.foreground import ForegroundCommitGuard
from integrations.telephony.phone_context import create_phone_channel_turn


async def _schema(conn):
    await conn.executescript(
        """
        CREATE TABLE PHONE_CONVERSATION_FOREGROUND (
            conversation_id INTEGER PRIMARY KEY,
            epoch INTEGER NOT NULL,
            current_call_id TEXT,
            lease_owner TEXT,
            lease_until TEXT
        );
        CREATE TABLE PHONE_CALL_MESSAGE_LINKS (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            participant TEXT NOT NULL,
            turn_id TEXT,
            origin_channel TEXT NOT NULL,
            interrupted INTEGER NOT NULL DEFAULT 0,
            played_ms INTEGER,
            confirmed_text TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'consumed',
            UNIQUE(call_id, message_id),
            UNIQUE(call_id, turn_id, participant)
        );
        CREATE TABLE MESSAGES (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            type TEXT NOT NULL,
            date TEXT
        );
        CREATE TABLE PHONE_CALLS (
            id TEXT PRIMARY KEY,
            conversation_id INTEGER NOT NULL,
            owner_user_id INTEGER NOT NULL,
            config_snapshot_json TEXT NOT NULL
        );
        CREATE TABLE PHONE_MEMORY_OUTBOX (
            message_id INTEGER PRIMARY KEY,
            call_id TEXT NOT NULL,
            conversation_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            prompt_id INTEGER,
            message_text TEXT NOT NULL,
            occurred_at TEXT
        );
        INSERT INTO PHONE_CONVERSATION_FOREGROUND(
            conversation_id, epoch, current_call_id, lease_owner, lease_until
        ) VALUES (7, 4, 'call-1', 'media-1', '2999-01-01T00:00:00Z');
        INSERT INTO PHONE_CALLS(
            id, conversation_id, owner_user_id, config_snapshot_json
        ) VALUES ('call-1', 7, 11, '{"prompt_id": 91}');
        """
    )


def _guard() -> ForegroundCommitGuard:
    return ForegroundCommitGuard(
        conversation_id=7,
        epoch=4,
        expected_owner="phone",
        call_id="call-1",
        lease_owner="media-1",
    )


def test_phone_context_is_deferred_and_carries_exact_turn_identity():
    phone_turn = create_phone_channel_turn(_guard(), turn_id="turn-9")

    assert phone_turn.context.channel == "phone"
    assert phone_turn.context.persistence == "deferred"
    assert phone_turn.context.turn_key.call_id == "call-1"
    assert phone_turn.context.turn_key.turn_id == "turn-9"
    assert phone_turn.context.provenance["phone_call_id"] == "call-1"
    assert phone_turn.context.provenance["foreground_epoch"] == 4
    assert phone_turn.context.provenance["turn_id"] == "turn-9"
    assert (
        phone_turn.context.provenance["end_call_controller"]
        is phone_turn.end_controller
    )


def test_phone_context_rejects_non_phone_or_incomplete_guards():
    with pytest.raises(ValueError):
        create_phone_channel_turn(
            ForegroundCommitGuard(7, 4, "non_phone"),
            turn_id="turn-1",
        )
    with pytest.raises(ValueError):
        create_phone_channel_turn(
            ForegroundCommitGuard(7, 4, "phone", "call-1", None),
            turn_id="turn-1",
        )
    with pytest.raises(ValueError):
        create_phone_channel_turn(_guard(), turn_id=" ")


@pytest.mark.asyncio
async def test_commit_links_caller_and_confirmed_assistant_atomically():
    async with aiosqlite.connect(":memory:") as conn:
        await _schema(conn)
        phone_turn = create_phone_channel_turn(_guard(), turn_id="turn-1")
        phone_turn.link_state.mark_interrupted()
        commit = ChannelCommit(
            context=phone_turn.context,
            user_message_id=101,
            assistant_message_id=102,
            confirmed_text="I heard this far",
            played_ms=1280,
        )

        await phone_turn.context.on_commit_in_transaction(commit, conn)
        # The canonical persistence caller owns commit/rollback.
        rows = await (
            await conn.execute(
                """
                SELECT message_id, participant, turn_id, interrupted, played_ms,
                       confirmed_text, delivery_state
                FROM PHONE_CALL_MESSAGE_LINKS ORDER BY message_id
                """
            )
        ).fetchall()

        assert rows == [
            (101, "caller", "turn-1", 0, None, None, "consumed"),
            (
                102,
                "assistant",
                "turn-1",
                1,
                1280,
                "I heard this far",
                "consumed",
            ),
        ]


@pytest.mark.asyncio
async def test_zero_ms_interruption_links_only_the_caller():
    async with aiosqlite.connect(":memory:") as conn:
        await _schema(conn)
        phone_turn = create_phone_channel_turn(_guard(), turn_id="turn-zero")
        phone_turn.link_state.mark_interrupted()
        commit = ChannelCommit(
            context=phone_turn.context,
            user_message_id=201,
            assistant_message_id=None,
            confirmed_text=None,
            played_ms=0,
        )

        await phone_turn.context.on_commit_in_transaction(commit, conn)
        rows = await (
            await conn.execute(
                "SELECT message_id, participant, interrupted "
                "FROM PHONE_CALL_MESSAGE_LINKS"
            )
        ).fetchall()

        assert rows == [(201, "caller", 1)]


@pytest.mark.asyncio
async def test_link_callback_is_idempotent_for_the_same_commit():
    async with aiosqlite.connect(":memory:") as conn:
        await _schema(conn)
        phone_turn = create_phone_channel_turn(_guard(), turn_id="turn-repeat")
        commit = ChannelCommit(
            context=phone_turn.context,
            user_message_id=301,
            assistant_message_id=302,
            confirmed_text="Complete reply",
            played_ms=800,
        )

        await phone_turn.context.on_commit_in_transaction(commit, conn)
        await phone_turn.context.on_commit_in_transaction(commit, conn)
        count = (
            await (
                await conn.execute("SELECT COUNT(*) FROM PHONE_CALL_MESSAGE_LINKS")
            ).fetchone()
        )[0]

        assert count == 2


@pytest.mark.asyncio
async def test_link_callback_fails_closed_after_foreground_changes():
    async with aiosqlite.connect(":memory:") as conn:
        await _schema(conn)
        phone_turn = create_phone_channel_turn(_guard(), turn_id="turn-stale")
        await conn.execute(
            "UPDATE PHONE_CONVERSATION_FOREGROUND SET epoch=5, current_call_id=NULL"
        )
        commit = ChannelCommit(
            context=phone_turn.context,
            user_message_id=401,
            assistant_message_id=None,
            confirmed_text=None,
            played_ms=0,
        )

        with pytest.raises(StaleChannelTurnError):
            await phone_turn.context.on_commit_in_transaction(commit, conn)
        count = (
            await (
                await conn.execute("SELECT COUNT(*) FROM PHONE_CALL_MESSAGE_LINKS")
            ).fetchone()
        )[0]
        assert count == 0


@pytest.mark.asyncio
async def test_ingest_only_commit_enqueues_memory_atomically_but_moderation_does_not():
    async with aiosqlite.connect(":memory:") as conn:
        await _schema(conn)
        await conn.executemany(
            "INSERT INTO MESSAGES(id,conversation_id,user_id,message,type,date) "
            "VALUES (?,?,?,?,?,?)",
            (
                (501, 7, 11, "durable caller final", "user", "2030-01-01"),
                (502, 7, 11, "[Blocked Message]", "user", "2030-01-01"),
            ),
        )
        phone_turn = create_phone_channel_turn(
            _guard(), turn_id="turn-ingest", persistence="ingest_only"
        )

        await phone_turn.context.on_commit_in_transaction(
            ChannelCommit(
                context=phone_turn.context,
                user_message_id=501,
                assistant_message_id=None,
                confirmed_text=None,
                played_ms=None,
            ),
            conn,
        )
        await phone_turn.context.on_commit_in_transaction(
            ChannelCommit(
                context=phone_turn.context,
                user_message_id=501,
                assistant_message_id=None,
                confirmed_text=None,
                played_ms=None,
            ),
            conn,
        )
        moderated_turn = create_phone_channel_turn(
            _guard(), turn_id="turn-moderated", persistence="ingest_only"
        )
        await moderated_turn.context.on_commit_in_transaction(
            ChannelCommit(
                context=moderated_turn.context,
                user_message_id=502,
                assistant_message_id=None,
                confirmed_text=None,
                played_ms=None,
                persistence_only=True,
            ),
            conn,
        )

        outbox = await (
            await conn.execute(
                "SELECT message_id,call_id,prompt_id,message_text "
                "FROM PHONE_MEMORY_OUTBOX"
            )
        ).fetchall()

    assert outbox == [(501, "call-1", 91, "durable caller final")]
