from __future__ import annotations

from typing import Any

import pytest

import atagia_sync
from atagia_sync import (
    get_atagia_sync_status,
    record_atagia_message_link,
    sync_all_history,
)


class FakeBridge:
    def __init__(
        self,
        fail_message_ids: set[int] | None = None,
        transient_lock_failures: dict[int, int] | None = None,
    ) -> None:
        self.fail_message_ids = fail_message_ids or set()
        self.transient_lock_failures = transient_lock_failures or {}
        self.ingest_calls: list[dict[str, Any]] = []
        self.flush_calls = 0
        self.last_error: Any | None = None

    async def ingest_message(
        self,
        *,
        user_id: int,
        conversation_id: int,
        role: str,
        text: str,
        occurred_at: str | None = None,
        prompt_id: int | None = None,
        message_id: int | None = None,
        source_seq: int | None = None,
        ingest_origin: str | None = None,
        confirmation_strategy: str | None = None,
    ) -> bool:
        self.ingest_calls.append(
            {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "role": role,
                "text": text,
                "occurred_at": occurred_at,
                "prompt_id": prompt_id,
                "message_id": message_id,
                "source_seq": source_seq,
                "ingest_origin": ingest_origin,
                "confirmation_strategy": confirmation_strategy,
            }
        )
        remaining_lock_failures = self.transient_lock_failures.get(int(message_id or 0), 0)
        if remaining_lock_failures:
            self.transient_lock_failures[int(message_id or 0)] = remaining_lock_failures - 1
            self.last_error = {
                "operation": "ingest_message",
                "error_type": "OperationalError",
                "message": "database is locked",
            }
            return False

        if int(message_id or 0) in self.fail_message_ids:
            self.last_error = {
                "operation": "ingest_message",
                "error_type": "SourceSequenceConflictError",
                "status_code": 409,
                "message": "source_seq already exists",
            }
            return False
        self.last_error = None
        return True

    async def flush(self) -> bool:
        self.flush_calls += 1
        return True


@pytest.mark.asyncio
async def test_sync_all_history_backfills_unlinked_messages(mock_db) -> None:
    async with mock_db() as conn:
        await conn.execute("INSERT INTO USERS (id, username) VALUES (7, 'u7')")
        await conn.execute(
            "INSERT INTO CONVERSATIONS (id, user_id, role_id) VALUES (1, 7, 42)"
        )
        await conn.execute(
            """
            INSERT INTO MESSAGES (id, conversation_id, user_id, message, type, date)
            VALUES
                (10, 1, 7, 'hello', 'user', '2026-05-01 10:00:00'),
                (11, 1, 7, 'hi there', 'bot', '2026-05-01 10:00:01')
            """
        )
        await conn.commit()

    bridge = FakeBridge()
    summary = await sync_all_history(batch_size=1, bridge=bridge)

    assert summary.status == "completed"
    assert summary.total_messages == 2
    assert summary.processed_messages == 2
    assert summary.linked_messages == 2
    assert bridge.flush_calls == 1
    assert bridge.ingest_calls == [
        {
            "user_id": 7,
            "conversation_id": 1,
            "role": "user",
            "text": "hello",
            "occurred_at": "2026-05-01 10:00:00",
            "prompt_id": 42,
            "message_id": 10,
            "source_seq": 10,
            "ingest_origin": "backfill",
            "confirmation_strategy": "admin_review_only",
        },
        {
            "user_id": 7,
            "conversation_id": 1,
            "role": "assistant",
            "text": "hi there",
            "occurred_at": "2026-05-01 10:00:01",
            "prompt_id": 42,
            "message_id": 11,
            "source_seq": 11,
            "ingest_origin": "backfill",
            "confirmation_strategy": "admin_review_only",
        },
    ]

    status = await get_atagia_sync_status()
    assert status["linked_messages"] == 2
    assert status["pending_messages"] == 0
    assert status["latest_run"]["status"] == "completed"

    second_bridge = FakeBridge()
    second = await sync_all_history(batch_size=10, bridge=second_bridge)

    assert second.processed_messages == 0
    assert second_bridge.ingest_calls == []


@pytest.mark.asyncio
async def test_sync_all_history_records_errors_without_stopping_run(mock_db) -> None:
    async with mock_db() as conn:
        await conn.execute("INSERT INTO USERS (id, username) VALUES (9, 'u9')")
        await conn.execute(
            "INSERT INTO CONVERSATIONS (id, user_id, role_id) VALUES (2, 9, 77)"
        )
        await conn.execute(
            """
            INSERT INTO MESSAGES (id, conversation_id, user_id, message, type, date)
            VALUES
                (20, 2, 9, 'first', 'user', '2026-05-01 11:00:00'),
                (21, 2, 9, 'second', 'bot', '2026-05-01 11:00:01'),
                (22, 2, 9, 'third', 'user', '2026-05-01 11:00:02')
            """
        )
        await conn.commit()

    bridge = FakeBridge(fail_message_ids={21})
    summary = await sync_all_history(batch_size=2, bridge=bridge)

    assert summary.status == "completed_with_errors"
    assert summary.processed_messages == 3
    assert summary.linked_messages == 2
    assert summary.failed_messages == 1
    assert any("message 21" in error for error in (summary.recent_errors or []))
    assert any("source_seq already exists" in error for error in (summary.recent_errors or []))

    status = await get_atagia_sync_status()
    assert status["linked_messages"] == 2
    assert status["pending_messages"] == 1
    assert status["latest_run"]["status"] == "completed_with_errors"


@pytest.mark.asyncio
async def test_sync_all_history_retries_transient_atagia_database_locks(
    mock_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(delay_seconds: float) -> None:
        return None

    monkeypatch.setattr(atagia_sync, "_sleep_before_retry", no_sleep)
    monkeypatch.setattr(atagia_sync, "TRANSIENT_INGEST_RETRY_DELAYS_SECONDS", (0.0, 0.0))

    async with mock_db() as conn:
        await conn.execute("INSERT INTO USERS (id, username) VALUES (18, 'u18')")
        await conn.execute(
            "INSERT INTO CONVERSATIONS (id, user_id, role_id) VALUES (6, 18, 81)"
        )
        await conn.execute(
            """
            INSERT INTO MESSAGES (id, conversation_id, user_id, message, type, date)
            VALUES (60, 6, 18, 'locked once', 'user', '2026-05-01 13:00:00')
            """
        )
        await conn.commit()

    bridge = FakeBridge(transient_lock_failures={60: 2})
    summary = await sync_all_history(batch_size=10, bridge=bridge)

    assert summary.status == "completed"
    assert summary.processed_messages == 1
    assert summary.linked_messages == 1
    assert summary.failed_messages == 0
    assert [call["message_id"] for call in bridge.ingest_calls] == [60, 60, 60]

    status = await get_atagia_sync_status()
    assert status["linked_messages"] == 1
    assert status["pending_messages"] == 0


@pytest.mark.asyncio
async def test_sync_all_history_skips_hidden_incognito_conversations(mock_db) -> None:
    async with mock_db() as conn:
        await conn.execute("INSERT INTO USERS (id, username) VALUES (13, 'u13')")
        await conn.execute(
            "INSERT INTO CONVERSATIONS (id, user_id, role_id) VALUES (4, 13, 90)"
        )
        await conn.execute(
            "INSERT INTO CONVERSATIONS (id, user_id, role_id) VALUES (5, 13, 90)"
        )
        from chat.services.privacy import ensure_conversation_privacy_schema, mark_conversation_incognito

        await ensure_conversation_privacy_schema(conn)
        await mark_conversation_incognito(
            conn,
            conversation_id=5,
            user_id=13,
            incognito=True,
        )
        await conn.execute(
            """
            INSERT INTO MESSAGES (id, conversation_id, user_id, message, type, date)
            VALUES
                (40, 4, 13, 'visible', 'user', '2026-05-01 12:00:00'),
                (50, 5, 13, 'hidden', 'user', '2026-05-01 12:00:01')
            """
        )
        await conn.commit()

    bridge = FakeBridge()
    summary = await sync_all_history(batch_size=10, bridge=bridge)

    assert summary.status == "completed"
    assert summary.total_messages == 1
    assert [call["message_id"] for call in bridge.ingest_calls] == [40]

    status = await get_atagia_sync_status()
    assert status["pending_messages"] == 0


@pytest.mark.asyncio
async def test_record_atagia_message_link_is_idempotent(mock_db) -> None:
    async with mock_db() as conn:
        await conn.execute("INSERT INTO USERS (id, username) VALUES (12, 'u12')")
        await conn.execute(
            "INSERT INTO CONVERSATIONS (id, user_id, role_id) VALUES (3, 12, 88)"
        )
        await conn.execute(
            """
            INSERT INTO MESSAGES (id, conversation_id, user_id, message, type, date)
            VALUES (30, 3, 12, 'hello', 'user', '2026-05-01 12:00:00')
            """
        )
        await conn.commit()

    first = await record_atagia_message_link(
        message_id=30,
        atagia_message_id="aurvek:msg:30",
        conversation_id=3,
        user_id=12,
        role="user",
    )
    second = await record_atagia_message_link(
        message_id=30,
        atagia_message_id="aurvek:msg:30",
        conversation_id=3,
        user_id=12,
        role="user",
    )

    assert first is True
    assert second is False
    status = await get_atagia_sync_status()
    assert status["linked_messages"] == 1


@pytest.mark.asyncio
async def test_sync_all_history_skips_messages_of_deleted_users(mock_db) -> None:
    """Messages whose owning user no longer exists must never be backfilled.

    Guards against re-propagating orphaned historical messages into Atagia
    after a user has been deleted (the confirmed orphaned-message bug).
    """
    async with mock_db() as conn:
        # No USERS row for id 99 -> the owning user does not exist.
        await conn.execute(
            "INSERT INTO CONVERSATIONS (id, user_id, role_id) VALUES (8, 99, 5)"
        )
        await conn.execute(
            """
            INSERT INTO MESSAGES (id, conversation_id, user_id, message, type, date)
            VALUES (80, 8, 99, 'orphan', 'user', '2026-05-01 14:00:00')
            """
        )
        await conn.commit()

    bridge = FakeBridge()
    summary = await sync_all_history(batch_size=10, bridge=bridge)

    assert summary.status == "completed"
    assert summary.total_messages == 0
    assert bridge.ingest_calls == []

    status = await get_atagia_sync_status()
    assert status["pending_messages"] == 0
    assert status["linked_messages"] == 0
