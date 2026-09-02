"""Durable, retryable memory delivery for finalized caller-only phone turns."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import socket
import os
from typing import Any
from uuid import uuid4

import aiosqlite

import database
from integrations.telephony.purge_state import (
    phone_memory_operation_lease,
)
from log_config import logger


_VALID_PROVIDERS = {"atagia", "mem0", "none"}


def _utc_text(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True, slots=True)
class PhoneMemoryJob:
    message_id: int
    call_id: str
    conversation_id: int
    user_id: int
    prompt_id: int | None
    message_text: str
    occurred_at: str | None
    provider: str | None
    attempt_count: int
    lease_token: str


async def enqueue_phone_memory_in_transaction(
    conn: Any,
    *,
    call_id: str,
    message_id: int,
) -> None:
    """Atomically hand one canonical caller message to the memory worker."""

    await conn.execute(
        """
        INSERT INTO PHONE_MEMORY_OUTBOX (
            message_id, call_id, conversation_id, user_id, prompt_id,
            message_text, occurred_at
        )
        SELECT m.id, c.id, m.conversation_id, m.user_id,
               CASE
                   WHEN CAST(json_extract(c.config_snapshot_json, '$.prompt_id') AS INTEGER) > 0
                   THEN CAST(json_extract(c.config_snapshot_json, '$.prompt_id') AS INTEGER)
                   ELSE NULL
               END,
               m.message, m.date
        FROM MESSAGES AS m
        JOIN PHONE_CALLS AS c ON c.id = ?
        WHERE m.id = ?
          AND m.conversation_id = c.conversation_id
          AND m.user_id = c.owner_user_id
          AND m.type = 'user'
        ON CONFLICT(message_id) DO NOTHING
        """,
        (str(call_id), int(message_id)),
    )
    cursor = await conn.execute(
        """
        SELECT call_id, conversation_id, user_id
        FROM PHONE_MEMORY_OUTBOX
        WHERE message_id=?
        """,
        (int(message_id),),
    )
    row = await cursor.fetchone()
    # An absent row means the message/call identity did not match.  A row for
    # another call means an incompatible prior enqueue.  Either condition must
    # roll back the surrounding canonical message transaction.
    if row is None or str(row[0]) != str(call_id):
        raise RuntimeError("Phone caller memory handoff is incompatible")


class PhoneMemoryOutboxRepository:
    """SQLite claims and acknowledgements for the phone memory outbox."""

    def __init__(self, connection_factory: Callable[..., Any] | None = None) -> None:
        self._connection_factory = connection_factory or database.get_db_connection

    @property
    def connection_factory(self) -> Callable[..., Any]:
        return self._connection_factory

    async def claim_one(
        self,
        *,
        worker_id: str,
        max_attempts: int,
        lease_seconds: float,
    ) -> PhoneMemoryJob | None:
        now = datetime.now(UTC)
        now_text = _utc_text(now)
        lease_until = _utc_text(now + timedelta(seconds=lease_seconds))
        lease_token = uuid4().hex
        async with self._connection_factory() as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await conn.execute(
                    """
                    UPDATE PHONE_MEMORY_OUTBOX
                    SET status=CASE
                            WHEN provider='mem0' AND provider_started_at IS NOT NULL
                            THEN 'needs_attention'
                            WHEN attempt_count >= ? THEN 'needs_attention'
                            ELSE 'retry'
                        END,
                        next_attempt_at=CASE
                            WHEN provider='mem0' AND provider_started_at IS NOT NULL
                            THEN NULL
                            WHEN attempt_count >= ? THEN NULL
                            ELSE ?
                        END,
                        lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                        last_error=CASE
                            WHEN provider='mem0' AND provider_started_at IS NOT NULL
                            THEN COALESCE(last_error, 'ambiguous Mem0 delivery')
                            WHEN attempt_count >= ?
                            THEN COALESCE(last_error, 'worker lease expired')
                            ELSE last_error
                        END,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE status='processing' AND lease_until <= ?
                    """,
                    (
                        int(max_attempts),
                        int(max_attempts),
                        now_text,
                        int(max_attempts),
                        now_text,
                    ),
                )
                cursor = await conn.execute(
                    """
                    SELECT * FROM PHONE_MEMORY_OUTBOX
                    WHERE status IN ('pending', 'retry')
                      AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    ORDER BY created_at, message_id
                    LIMIT 1
                    """,
                    (now_text,),
                )
                row = await cursor.fetchone()
                if row is None:
                    await conn.commit()
                    return None
                cursor = await conn.execute(
                    """
                    UPDATE PHONE_MEMORY_OUTBOX
                    SET status='processing', attempt_count=attempt_count+1,
                        lease_owner=?, lease_token=?, lease_until=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE message_id=? AND status IN ('pending', 'retry')
                    """,
                    (
                        str(worker_id),
                        lease_token,
                        lease_until,
                        int(row["message_id"]),
                    ),
                )
                if cursor.rowcount != 1:
                    await conn.rollback()
                    return None
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return PhoneMemoryJob(
            message_id=int(row["message_id"]),
            call_id=str(row["call_id"]),
            conversation_id=int(row["conversation_id"]),
            user_id=int(row["user_id"]),
            prompt_id=None if row["prompt_id"] is None else int(row["prompt_id"]),
            message_text=str(row["message_text"]),
            occurred_at=None if row["occurred_at"] is None else str(row["occurred_at"]),
            provider=None if row["provider"] is None else str(row["provider"]),
            attempt_count=int(row["attempt_count"]) + 1,
            lease_token=lease_token,
        )

    async def bind_provider(self, job: PhoneMemoryJob, provider: str) -> bool:
        normalized = str(provider).strip().lower()
        if normalized not in _VALID_PROVIDERS:
            raise ValueError("Unsupported memory provider")
        async with self._connection_factory() as conn:
            cursor = await conn.execute(
                """
                UPDATE PHONE_MEMORY_OUTBOX
                SET provider=COALESCE(provider, ?), updated_at=CURRENT_TIMESTAMP
                WHERE message_id=? AND status='processing' AND lease_token=?
                  AND (provider IS NULL OR provider=?)
                """,
                (
                    normalized,
                    job.message_id,
                    job.lease_token,
                    normalized,
                ),
            )
            await conn.commit()
            return cursor.rowcount == 1

    async def mark_completed(self, job: PhoneMemoryJob) -> bool:
        async with self._connection_factory() as conn:
            cursor = await conn.execute(
                """
                UPDATE PHONE_MEMORY_OUTBOX
                SET status='completed', next_attempt_at=NULL,
                    lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                    last_error=NULL, completed_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE message_id=? AND status='processing' AND lease_token=?
                """,
                (job.message_id, job.lease_token),
            )
            await conn.commit()
            return cursor.rowcount == 1

    async def mark_provider_started(self, job: PhoneMemoryJob) -> bool:
        """Persist the ambiguity boundary immediately before provider I/O."""

        async with self._connection_factory() as conn:
            cursor = await conn.execute(
                """
                UPDATE PHONE_MEMORY_OUTBOX
                SET provider_started_at=COALESCE(provider_started_at, ?),
                    updated_at=CURRENT_TIMESTAMP
                WHERE message_id=? AND status='processing' AND lease_token=?
                """,
                (_utc_text(), job.message_id, job.lease_token),
            )
            await conn.commit()
            return cursor.rowcount == 1

    async def mark_failed(
        self,
        job: PhoneMemoryJob,
        *,
        error: BaseException | str,
        max_attempts: int,
        retry_delay_seconds: float,
        force_needs_attention: bool = False,
    ) -> bool:
        detail = str(error).strip() or type(error).__name__
        async with self._connection_factory() as conn:
            state_cursor = await conn.execute(
                """
                SELECT provider,provider_started_at
                FROM PHONE_MEMORY_OUTBOX
                WHERE message_id=? AND status='processing' AND lease_token=?
                """,
                (job.message_id, job.lease_token),
            )
            state = await state_cursor.fetchone()
            if state is None:
                return False
            durable_mem0_ambiguity = str(state[0]) == "mem0" and state[1] is not None
            needs_attention = (
                bool(force_needs_attention)
                or durable_mem0_ambiguity
                or job.attempt_count >= int(max_attempts)
            )
            next_attempt = None
            if not needs_attention:
                next_attempt = _utc_text(
                    datetime.now(UTC) + timedelta(seconds=retry_delay_seconds)
                )
            cursor = await conn.execute(
                """
                UPDATE PHONE_MEMORY_OUTBOX
                SET status=?, next_attempt_at=?, lease_owner=NULL,
                    lease_token=NULL, lease_until=NULL, last_error=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE message_id=? AND status='processing' AND lease_token=?
                """,
                (
                    "needs_attention" if needs_attention else "retry",
                    next_attempt,
                    detail[:1000],
                    job.message_id,
                    job.lease_token,
                ),
            )
            await conn.commit()
            return cursor.rowcount == 1


ProviderStart = Callable[[], Awaitable[None]]
MemoryRecorder = Callable[[PhoneMemoryJob, str, ProviderStart], Awaitable[None]]
ProviderLoader = Callable[[], Awaitable[str]]


class PhoneMemoryOutboxWorker:
    """Lifecycle-owned bounded delivery worker with durable retry state."""

    def __init__(
        self,
        repository: PhoneMemoryOutboxRepository | None = None,
        *,
        recorder: MemoryRecorder | None = None,
        provider_loader: ProviderLoader | None = None,
        worker_id: str | None = None,
        attempt_timeout_seconds: float = 10.0,
        lease_seconds: float = 30.0,
        poll_seconds: float = 1.0,
        max_attempts: int = 5,
    ) -> None:
        if attempt_timeout_seconds <= 0 or lease_seconds <= attempt_timeout_seconds:
            raise ValueError("Memory outbox lease must exceed its attempt timeout")
        if poll_seconds <= 0 or max_attempts <= 0:
            raise ValueError("Memory outbox polling and attempts must be positive")
        self.repository = repository or PhoneMemoryOutboxRepository()
        self.recorder = recorder or record_phone_caller_memory
        self.provider_loader = provider_loader or _active_provider
        self.worker_id = worker_id or (
            f"phone-memory-{socket.gethostname()}-{os.getpid()}"
        )
        self.attempt_timeout_seconds = float(attempt_timeout_seconds)
        self.lease_seconds = float(lease_seconds)
        self.poll_seconds = float(poll_seconds)
        self.max_attempts = int(max_attempts)

    async def run_once(self) -> bool:
        job = await self.repository.claim_one(
            worker_id=self.worker_id,
            max_attempts=self.max_attempts,
            lease_seconds=self.lease_seconds,
        )
        if job is None:
            return False
        provider: str | None = None
        provider_started = False
        operation_lease = None

        async def mark_provider_started() -> None:
            nonlocal provider_started
            if operation_lease is None:
                raise RuntimeError("Phone memory operation lease is unavailable")
            await operation_lease.mark_provider_started()
            if not await self.repository.mark_provider_started(job):
                raise RuntimeError("Phone memory outbox lease was lost")
            provider_started = True

        try:
            provider = job.provider or await self.provider_loader()
            provider = str(provider).strip().lower()
            if not await self.repository.bind_provider(job, provider):
                raise RuntimeError("Phone memory outbox provider claim was lost")
            if provider != "none":
                # This local SQLite guard is deliberately outside the provider
                # timeout.  Cold provider imports are part of delivery; the
                # purge revision check is not provider I/O.
                if self.recorder is record_phone_caller_memory:
                    _prepare_default_recorder(provider)
                async with phone_memory_operation_lease(
                    job.conversation_id,
                    provider=provider,
                    operation="phone_memory_outbox",
                    connection_factory=self.repository.connection_factory,
                    lease_seconds=self.lease_seconds,
                ) as operation_lease:
                    await asyncio.wait_for(
                        self.recorder(job, provider, mark_provider_started),
                        timeout=self.attempt_timeout_seconds,
                    )
        except asyncio.CancelledError:
            # Leave the durable lease intact.  Recovery reclaims Atagia after
            # expiry (stable message_id/source_seq), while an ambiguous Mem0
            # provider start is fenced to needs_attention without replay.
            raise
        except BaseException as exc:
            delay = min(300.0, 2.0 ** max(0, job.attempt_count - 1))
            await self.repository.mark_failed(
                job,
                error=(
                    "memory delivery timed out"
                    if isinstance(exc, TimeoutError)
                    else exc
                ),
                max_attempts=self.max_attempts,
                retry_delay_seconds=delay,
                force_needs_attention=provider_started,
            )
            return True
        if not await self.repository.mark_completed(job):
            raise RuntimeError("Phone memory outbox lease was lost before completion")
        return True

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Phone memory outbox worker iteration failed")
                processed = False
            if processed:
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass


async def _active_provider() -> str:
    from memory.config import get_active_memory_provider

    return str(await get_active_memory_provider())


def _prepare_default_recorder(provider: str) -> None:
    """Load local adapters before starting the bounded provider-I/O window."""

    import memory.config  # noqa: F401
    import memory.sync  # noqa: F401

    if provider == "atagia":
        import ai_runtime.atagia.recording  # noqa: F401
        import ai_runtime.atagia.state  # noqa: F401
        import atagia_bridge  # noqa: F401
        import atagia_sync  # noqa: F401
    elif provider == "mem0":
        import memory.providers.mem0  # noqa: F401


async def record_phone_caller_memory(
    job: PhoneMemoryJob,
    provider: str,
    mark_provider_started: ProviderStart,
) -> None:
    """Strict idempotent provider handoff for one canonical caller message."""

    from memory.config import get_user_memory_preferences
    from memory.sync import (
        record_memory_conversation_link,
        record_memory_message_link,
    )

    preferences = await get_user_memory_preferences(job.user_id, provider)
    if preferences.get("remember_across_chats") is False:
        return
    if await _memory_message_is_linked(job.message_id, provider):
        return
    prompt_id = None if preferences.get("memory_scope") == "global" else job.prompt_id
    await record_memory_conversation_link(
        provider=provider,
        conversation_id=job.conversation_id,
        user_id=job.user_id,
        source="live",
        metadata={"prompt_id": str(prompt_id) if prompt_id is not None else None},
    )

    if provider == "atagia":
        from ai_runtime.atagia.recording import _aurvek_atagia_message_id
        from ai_runtime.atagia.state import (
            ATAGIA_LIVE_CONFIRMATION_STRATEGY,
            ATAGIA_LIVE_INGEST_ORIGIN,
        )
        from atagia_bridge import get_atagia_bridge
        from atagia_sync import record_atagia_message_link

        bridge = get_atagia_bridge()
        await mark_provider_started()
        recorded = await bridge.ingest_message(
            user_id=job.user_id,
            conversation_id=job.conversation_id,
            role="user",
            text=job.message_text,
            occurred_at=job.occurred_at,
            prompt_id=prompt_id,
            message_id=job.message_id,
            source_seq=job.message_id,
            ingest_origin=ATAGIA_LIVE_INGEST_ORIGIN,
            confirmation_strategy=ATAGIA_LIVE_CONFIRMATION_STRATEGY,
        )
        if not recorded:
            raise RuntimeError("Atagia did not accept the caller message")
        provider_message_id = _aurvek_atagia_message_id(job.message_id)
        assert provider_message_id is not None
        await record_atagia_message_link(
            message_id=job.message_id,
            atagia_message_id=provider_message_id,
            conversation_id=job.conversation_id,
            user_id=job.user_id,
            role="user",
            source="live",
        )
        await record_memory_message_link(
            provider="atagia",
            message_id=job.message_id,
            provider_message_id=provider_message_id,
            conversation_id=job.conversation_id,
            user_id=job.user_id,
            role="user",
            source="live",
        )
        return

    if provider == "mem0":
        from ai_runtime.memory.recording import (
            _extract_provider_event_id,
            _mem0_provider_message_id,
        )
        from memory.providers.mem0 import get_mem0_provider

        mem0 = await get_mem0_provider()
        # Everything above is local/configuration work and remains retryable.
        # Persist ambiguity only at the last boundary before external Mem0 I/O.
        await mark_provider_started()
        result = await mem0.add_turn(
            user_id=job.user_id,
            conversation_id=job.conversation_id,
            user_text=job.message_text,
            assistant_text=None,
            prompt_id=prompt_id,
            message_id=None,
            user_message_id=job.message_id,
            occurred_at=job.occurred_at,
            incognito=False,
        )
        if not result:
            raise RuntimeError("Mem0 did not accept the caller message")
        await record_memory_message_link(
            provider="mem0",
            message_id=job.message_id,
            provider_message_id=_mem0_provider_message_id(job.message_id, result),
            provider_event_id=_extract_provider_event_id(result),
            conversation_id=job.conversation_id,
            user_id=job.user_id,
            role="user",
            source="live",
            metadata=result,
        )
        return

    raise ValueError("Unsupported memory provider")


async def _memory_message_is_linked(message_id: int, provider: str) -> bool:
    """Use the local provider link as the idempotent completion ledger."""

    try:
        async with database.get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT 1 FROM MEMORY_PROVIDER_MESSAGE_LINKS
                WHERE message_id=? AND provider=?
                """,
                (int(message_id), str(provider)),
            )
            return await cursor.fetchone() is not None
    except aiosqlite.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        return False


__all__ = [
    "PhoneMemoryJob",
    "PhoneMemoryOutboxRepository",
    "PhoneMemoryOutboxWorker",
    "enqueue_phone_memory_in_transaction",
    "record_phone_caller_memory",
]
