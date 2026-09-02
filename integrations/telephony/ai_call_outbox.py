"""Transactional handoff from one real AI turn to an immediate phone job."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import os
import socket
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

import database
from ai_runtime.channel_turns import ChannelCommit
from integrations.telephony.config import load_telephony_config
from integrations.telephony.repository import (
    TelephonyConflictError,
    TelephonyRepository,
)
from integrations.telephony.service import OutboundCallService
from integrations.telephony.tooling import CallStartController
from integrations.telephony.user_service import (
    PhoneUserServiceError,
    UserPhoneService,
)
from log_config import logger


_OUTBOX_TTL_SECONDS = 120
_VALID_NON_PHONE_CHANNELS = {"web", "whatsapp", "telegram", "device"}


def _utc_text(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True, slots=True)
class AiCallStartCapability:
    """Call identity preflighted before exposing the model tool."""

    owner_user_id: int
    conversation_id: int
    binding_id: int
    prompt_id: int
    channel: Literal["web", "whatsapp", "telegram", "device"]
    mode: Literal["on_request", "proactive"]
    controller: CallStartController


@dataclass(frozen=True, slots=True)
class AiCallStartJob:
    id: int
    owner_user_id: int
    conversation_id: int
    binding_id: int
    prompt_id: int
    origin_channel: str
    initiation_mode: str
    user_message_id: int
    assistant_message_id: int
    attempt_count: int
    expires_at: str
    lease_token: str


@dataclass(frozen=True, slots=True)
class AiCallJobPreparation:
    """Read-only preflight result consumed by the final SQLite transaction."""

    timezone_name: str
    config_snapshot: dict[str, Any]


class AiCallStartCancelled(RuntimeError):
    """The committed request is no longer safe to dispatch."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = str(code)


class AiCallStartFenceLost(RuntimeError):
    """The worker may no longer create or mutate work for this request."""


async def load_ai_call_start_capability(
    *,
    conversation_id: int,
    channel: str,
    connection_factory: Callable[..., Any] = database.get_db_connection,
) -> AiCallStartCapability | None:
    """Fail closed unless an immediate outbound call is ready at turn capture."""

    normalized_channel = str(channel or "").strip().lower()
    if normalized_channel not in _VALID_NON_PHONE_CHANNELS:
        return None
    async with connection_factory(readonly=True) as conn:
        cursor = await conn.execute(
            """
            SELECT c.user_id,
                   COALESCE(c.role_id, ud.current_prompt_id) AS prompt_id
            FROM CONVERSATIONS c
            LEFT JOIN USER_DETAILS ud ON ud.user_id=c.user_id
            WHERE c.id=?
            """,
            (int(conversation_id),),
        )
        row = await cursor.fetchone()
    if row is None or row[1] is None:
        return None
    owner_user_id = int(row[0])
    prompt_id = int(row[1])

    async def config_loader():
        async with connection_factory(readonly=True) as conn:
            return await load_telephony_config(conn=conn)

    repository = TelephonyRepository(connection_factory=connection_factory)
    service = UserPhoneService(
        repository,
        connection_factory=connection_factory,
        config_loader=config_loader,
    )
    try:
        prepared = await service.prepare_outbound_call(
            owner_user_id=owner_user_id,
            conversation_id=int(conversation_id),
        )
    except (PhoneUserServiceError, TelephonyConflictError, ValueError):
        return None
    mode = str(prepared.config_snapshot.get("ai_initiation_mode") or "").strip()
    if mode not in {"on_request", "proactive"}:
        return None
    if int(prepared.config_snapshot.get("prompt_id") or 0) != prompt_id:
        return None
    typed_channel = normalized_channel
    typed_mode = mode
    return AiCallStartCapability(
        owner_user_id=owner_user_id,
        conversation_id=int(conversation_id),
        binding_id=int(prepared.binding["id"]),
        prompt_id=prompt_id,
        channel=typed_channel,  # type: ignore[arg-type]
        mode=typed_mode,  # type: ignore[arg-type]
        controller=CallStartController(typed_mode),  # type: ignore[arg-type]
    )


async def enqueue_ai_call_start_in_transaction(
    conn: Any,
    *,
    capability: AiCallStartCapability,
    commit: ChannelCommit,
) -> None:
    """Insert the outbox row beside the exact user/assistant message pair."""

    directive = capability.controller.directive
    if directive is None:
        return
    if commit.persistence_only:
        raise RuntimeError("AI phone calls require a generated assistant response")
    if commit.user_message_id is None or commit.assistant_message_id is None:
        raise RuntimeError("AI phone calls require a persisted turn pair")
    confirmed = str(commit.confirmed_text or "")
    if directive.reply_message not in confirmed:
        raise RuntimeError("The saved response does not contain the call reply")
    expires_at = _utc_text(
        datetime.now(UTC) + timedelta(seconds=_OUTBOX_TTL_SECONDS)
    )
    cursor = await conn.execute(
        """
        INSERT INTO PHONE_AI_CALL_OUTBOX (
            owner_user_id, conversation_id, binding_id, prompt_id,
            origin_channel, initiation_mode, user_message_id,
            assistant_message_id, expires_at
        )
        SELECT c.user_id, c.id, b.id,
               COALESCE(c.role_id, ud.current_prompt_id), ?, ?, u.id, a.id, ?
        FROM CONVERSATIONS c
        JOIN USERS owner ON owner.id=c.user_id AND COALESCE(owner.is_enabled,0)=1
        LEFT JOIN USER_DETAILS ud ON ud.user_id=c.user_id
        JOIN PHONE_CONVERSATION_BINDINGS b
          ON b.conversation_id=c.id AND b.owner_user_id=c.user_id
        JOIN PHONE_CONTACTS pc ON pc.id=b.contact_id
        JOIN MESSAGES u ON u.id=? AND u.conversation_id=c.id
        JOIN MESSAGES a ON a.id=? AND a.conversation_id=c.id
        LEFT JOIN PROMPT_PHONE_SETTINGS ps
          ON ps.prompt_id=COALESCE(c.role_id, ud.current_prompt_id)
        WHERE c.id=? AND c.user_id=?
          AND COALESCE(c.locked,0)=0 AND COALESCE(c.is_incognito,0)=0
          AND b.id=? AND b.active=1 AND b.allow_outbound=1 AND pc.active=1
          AND COALESCE(c.role_id, ud.current_prompt_id)=?
          AND COALESCE(ps.ai_initiation_mode,'on_request')=?
          AND u.user_id=c.user_id AND u.type='user'
          AND a.user_id=c.user_id AND a.type='bot'
          AND instr(a.message,?)>0
        ON CONFLICT DO NOTHING
        """,
        (
            capability.channel,
            capability.mode,
            expires_at,
            int(commit.user_message_id),
            int(commit.assistant_message_id),
            capability.conversation_id,
            capability.owner_user_id,
            capability.binding_id,
            capability.prompt_id,
            capability.mode,
            directive.reply_message,
        ),
    )
    if cursor.rowcount == 1:
        return
    existing = await (
        await conn.execute(
            """
            SELECT owner_user_id,conversation_id,binding_id,prompt_id,
                   origin_channel,initiation_mode,user_message_id
            FROM PHONE_AI_CALL_OUTBOX WHERE assistant_message_id=?
            """,
            (int(commit.assistant_message_id),),
        )
    ).fetchone()
    expected = (
        capability.owner_user_id,
        capability.conversation_id,
        capability.binding_id,
        capability.prompt_id,
        capability.channel,
        capability.mode,
        int(commit.user_message_id),
    )
    if existing is None or tuple(existing) != expected:
        raise RuntimeError("AI phone call capability changed before message commit")


class AiCallStartOutboxRepository:
    """SQLite claim and fencing operations for immediate AI call requests."""

    def __init__(
        self,
        connection_factory: Callable[..., Any] | None = None,
        *,
        outbound_service: OutboundCallService | None = None,
    ) -> None:
        self.connection_factory = connection_factory or database.get_db_connection
        self.outbound_service = outbound_service or OutboundCallService(
            TelephonyRepository(connection_factory=self.connection_factory)
        )

    async def claim_one(
        self,
        *,
        worker_id: str,
        max_attempts: int,
        lease_seconds: float,
    ) -> AiCallStartJob | None:
        now = datetime.now(UTC)
        now_text = _utc_text(now)
        lease_until = _utc_text(now + timedelta(seconds=lease_seconds))
        lease_token = uuid4().hex
        async with self.connection_factory() as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await conn.execute(
                    """
                    UPDATE PHONE_AI_CALL_OUTBOX
                    SET status='canceled', error_code='request_expired',
                        error_detail='Immediate call request expired',
                        next_attempt_at=NULL, lease_owner=NULL,
                        lease_token=NULL, lease_until=NULL,
                        completed_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE status IN ('pending','retry') AND expires_at<=?
                    """,
                    (now_text,),
                )
                await conn.execute(
                    """
                    UPDATE PHONE_AI_CALL_OUTBOX
                    SET status=CASE WHEN attempt_count>=?
                                    THEN 'needs_attention' ELSE 'retry' END,
                        next_attempt_at=CASE WHEN attempt_count>=?
                                             THEN NULL ELSE ? END,
                        error_code=CASE WHEN attempt_count>=?
                                        THEN 'worker_lease_expired' ELSE error_code END,
                        error_detail=CASE WHEN attempt_count>=?
                                          THEN 'AI call worker lease expired'
                                          ELSE error_detail END,
                        lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE status='processing' AND lease_until<=?
                    """,
                    (
                        int(max_attempts),
                        int(max_attempts),
                        now_text,
                        int(max_attempts),
                        int(max_attempts),
                        now_text,
                    ),
                )
                cursor = await conn.execute(
                    """
                    SELECT * FROM PHONE_AI_CALL_OUTBOX
                    WHERE status IN ('pending','retry')
                      AND expires_at>?
                      AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                    ORDER BY created_at,id LIMIT 1
                    """,
                    (now_text, now_text),
                )
                row = await cursor.fetchone()
                if row is None:
                    await conn.commit()
                    return None
                cursor = await conn.execute(
                    """
                    UPDATE PHONE_AI_CALL_OUTBOX
                    SET status='processing', attempt_count=attempt_count+1,
                        lease_owner=?, lease_token=?, lease_until=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status IN ('pending','retry') AND expires_at>?
                    """,
                    (worker_id, lease_token, lease_until, int(row["id"]), now_text),
                )
                if cursor.rowcount != 1:
                    await conn.rollback()
                    return None
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return AiCallStartJob(
            id=int(row["id"]),
            owner_user_id=int(row["owner_user_id"]),
            conversation_id=int(row["conversation_id"]),
            binding_id=int(row["binding_id"]),
            prompt_id=int(row["prompt_id"]),
            origin_channel=str(row["origin_channel"]),
            initiation_mode=str(row["initiation_mode"]),
            user_message_id=int(row["user_message_id"]),
            assistant_message_id=int(row["assistant_message_id"]),
            attempt_count=int(row["attempt_count"]) + 1,
            expires_at=str(row["expires_at"]),
            lease_token=lease_token,
        )

    async def create_call_job_and_complete(
        self,
        job: AiCallStartJob,
        *,
        preparation: AiCallJobPreparation,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically create/reuse the normal call job and close the outbox."""

        async with self.connection_factory() as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")
            try:
                # Acquire the SQLite writer lock before sampling the clock.  A
                # request must not survive expiry merely because BEGIN waited
                # behind another writer.
                now_text = _utc_text()
                cursor = await conn.execute(
                    """
                    SELECT status,lease_token,lease_until,expires_at
                    FROM PHONE_AI_CALL_OUTBOX WHERE id=?
                    """,
                    (job.id,),
                )
                state = await cursor.fetchone()
                if (
                    state is None
                    or str(state["status"]) != "processing"
                    or str(state["lease_token"] or "") != job.lease_token
                    or str(state["lease_until"] or "") <= now_text
                ):
                    raise AiCallStartFenceLost("AI call outbox lease was lost")
                if str(state["expires_at"]) <= now_text:
                    raise AiCallStartCancelled(
                        "request_expired", "Immediate call request expired"
                    )
                await _revalidate_committed_request_in_transaction(conn, job)
                call_job, created = (
                    await self.outbound_service.call_now_in_transaction(
                        conn,
                        owner_user_id=job.owner_user_id,
                        conversation_id=job.conversation_id,
                        binding_id=job.binding_id,
                        timezone_name=preparation.timezone_name,
                        origin="assistant",
                        idempotency_key=(
                            f"assistant-message-{job.assistant_message_id}"
                        ),
                        config_snapshot=preparation.config_snapshot,
                        origin_message_id=job.assistant_message_id,
                        recording_override=None,
                        amd_override=None,
                    )
                )
                completion_now = _utc_text()
                if str(state["lease_until"] or "") <= completion_now:
                    raise AiCallStartFenceLost(
                        "AI call outbox lease expired before completion"
                    )
                if str(state["expires_at"]) <= completion_now:
                    raise AiCallStartCancelled(
                        "request_expired", "Immediate call request expired"
                    )
                cursor = await conn.execute(
                    """
                    UPDATE PHONE_AI_CALL_OUTBOX
                    SET status='completed', call_job_id=?, next_attempt_at=NULL,
                        lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                        error_code=NULL, error_detail=NULL,
                        completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status='processing' AND lease_token=?
                      AND lease_until>=? AND expires_at>?
                    """,
                    (
                        str(call_job["id"]),
                        job.id,
                        job.lease_token,
                        completion_now,
                        completion_now,
                    ),
                )
                if cursor.rowcount != 1:
                    raise AiCallStartFenceLost(
                        "AI call outbox fence changed before completion"
                    )
                await conn.commit()
                return call_job, created
            except BaseException:
                await conn.rollback()
                raise

    async def mark_canceled(
        self, job: AiCallStartJob, *, code: str, detail: str
    ) -> bool:
        async with self.connection_factory() as conn:
            cursor = await conn.execute(
                """
                UPDATE PHONE_AI_CALL_OUTBOX
                SET status='canceled', next_attempt_at=NULL,
                    lease_owner=NULL, lease_token=NULL, lease_until=NULL,
                    error_code=?, error_detail=?, completed_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='processing' AND lease_token=?
                """,
                (str(code)[:100], str(detail)[:1000], job.id, job.lease_token),
            )
            await conn.commit()
            return cursor.rowcount == 1

    async def mark_failed(
        self,
        job: AiCallStartJob,
        *,
        error: BaseException,
        max_attempts: int,
        retry_delay_seconds: float,
    ) -> bool:
        needs_attention = job.attempt_count >= int(max_attempts)
        next_attempt = None
        if not needs_attention:
            next_attempt = _utc_text(
                datetime.now(UTC) + timedelta(seconds=retry_delay_seconds)
            )
        async with self.connection_factory() as conn:
            cursor = await conn.execute(
                """
                UPDATE PHONE_AI_CALL_OUTBOX
                SET status=?, next_attempt_at=?, lease_owner=NULL,
                    lease_token=NULL, lease_until=NULL,
                    error_code=?, error_detail=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='processing' AND lease_token=?
                """,
                (
                    "needs_attention" if needs_attention else "retry",
                    next_attempt,
                    "worker_failed",
                    (str(error).strip() or type(error).__name__)[:1000],
                    job.id,
                    job.lease_token,
                ),
            )
            await conn.commit()
            return cursor.rowcount == 1


CallPreparer = Callable[[AiCallStartJob], Awaitable[AiCallJobPreparation]]


class AiCallStartOutboxWorker:
    """Convert committed AI requests into normal idempotent immediate jobs."""

    def __init__(
        self,
        repository: AiCallStartOutboxRepository | None = None,
        *,
        call_preparer: CallPreparer | None = None,
        worker_id: str | None = None,
        attempt_timeout_seconds: float = 20.0,
        lease_seconds: float = 30.0,
        poll_seconds: float = 0.25,
        max_attempts: int = 5,
    ) -> None:
        if attempt_timeout_seconds <= 0 or lease_seconds <= attempt_timeout_seconds:
            raise ValueError("AI call outbox lease must exceed its attempt timeout")
        if poll_seconds <= 0 or max_attempts <= 0:
            raise ValueError("AI call outbox polling and attempts must be positive")
        self.repository = repository or AiCallStartOutboxRepository()
        self.call_preparer = call_preparer or self._prepare_call_job
        self.worker_id = worker_id or (
            f"phone-ai-call-{socket.gethostname()}-{os.getpid()}"
        )
        self.attempt_timeout_seconds = float(attempt_timeout_seconds)
        self.lease_seconds = float(lease_seconds)
        self.poll_seconds = float(poll_seconds)
        self.max_attempts = int(max_attempts)

    async def _prepare_call_job(
        self, job: AiCallStartJob
    ) -> AiCallJobPreparation:
        """Perform only external/read-only preflight before the final fence."""

        await _revalidate_committed_request(
            job,
            connection_factory=self.repository.connection_factory,
        )
        telephony_repository = TelephonyRepository(
            connection_factory=self.repository.connection_factory
        )

        async def config_loader():
            async with self.repository.connection_factory(readonly=True) as conn:
                return await load_telephony_config(conn=conn)

        service = UserPhoneService(
            telephony_repository,
            connection_factory=self.repository.connection_factory,
            config_loader=config_loader,
        )
        prepared = await service.prepare_ai_initiated_call(
            owner_user_id=job.owner_user_id,
            conversation_id=job.conversation_id,
            expected_binding_id=job.binding_id,
        )
        return AiCallJobPreparation(
            timezone_name=str(prepared.binding["timezone_name"]),
            config_snapshot=prepared.config_snapshot,
        )

    async def run_once(self) -> bool:
        job = await self.repository.claim_one(
            worker_id=self.worker_id,
            max_attempts=self.max_attempts,
            lease_seconds=self.lease_seconds,
        )
        if job is None:
            return False
        try:
            preparation = await asyncio.wait_for(
                self.call_preparer(job), timeout=self.attempt_timeout_seconds
            )
            await self.repository.create_call_job_and_complete(
                job, preparation=preparation
            )
        except asyncio.CancelledError:
            raise
        except AiCallStartFenceLost:
            # Another worker owns the request.  Never mutate its lease or retry
            # state with this stale token.
            raise
        except AiCallStartCancelled as exc:
            if not await self.repository.mark_canceled(
                job, code=exc.code, detail=str(exc)
            ):
                raise RuntimeError("AI call outbox lease was lost during cancellation")
            return True
        except (PhoneUserServiceError, TelephonyConflictError, ValueError) as exc:
            if not await self.repository.mark_canceled(
                job,
                code="call_no_longer_available",
                detail=str(exc),
            ):
                raise RuntimeError("AI call outbox lease was lost during cancellation")
            return True
        except BaseException as exc:
            delay = min(30.0, 2.0 ** max(0, job.attempt_count - 1))
            if not await self.repository.mark_failed(
                job,
                error=exc,
                max_attempts=self.max_attempts,
                retry_delay_seconds=delay,
            ):
                raise RuntimeError(
                    "AI call outbox lease was lost while recording failure"
                ) from exc
            return True
        return True

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("AI phone call outbox worker iteration failed")
                processed = False
            if processed:
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass


async def _revalidate_committed_request(
    job: AiCallStartJob,
    *,
    connection_factory: Callable[..., Any],
) -> None:
    """Fence binding, prompt policy and the exact committed turn before a job."""

    async with connection_factory(readonly=True) as conn:
        await _revalidate_committed_request_in_transaction(conn, job)


async def _revalidate_committed_request_in_transaction(
    conn: Any,
    job: AiCallStartJob,
) -> None:
    """Repeat the durable policy fence inside the job-creation transaction."""

    cursor = await conn.execute(
        """
        SELECT b.id,
               COALESCE(c.role_id, ud.current_prompt_id) AS prompt_id,
               COALESCE(ps.ai_initiation_mode,'on_request') AS mode,
               u.id AS user_message_id, a.id AS assistant_message_id,
               COALESCE(owner.is_enabled,0) AS owner_enabled
        FROM CONVERSATIONS c
        JOIN USERS owner ON owner.id=c.user_id
        LEFT JOIN USER_DETAILS ud ON ud.user_id=c.user_id
        JOIN PHONE_CONVERSATION_BINDINGS b
          ON b.conversation_id=c.id AND b.owner_user_id=c.user_id
        JOIN PHONE_CONTACTS pc ON pc.id=b.contact_id
        LEFT JOIN PROMPT_PHONE_SETTINGS ps
          ON ps.prompt_id=COALESCE(c.role_id, ud.current_prompt_id)
        JOIN MESSAGES u ON u.id=? AND u.conversation_id=c.id
        JOIN MESSAGES a ON a.id=? AND a.conversation_id=c.id
        WHERE c.id=? AND c.user_id=?
          AND COALESCE(c.locked,0)=0 AND COALESCE(c.is_incognito,0)=0
          AND b.id=? AND b.active=1 AND b.allow_outbound=1 AND pc.active=1
          AND u.type='user' AND u.user_id=c.user_id
          AND a.type='bot' AND a.user_id=c.user_id
        """,
        (
            job.user_message_id,
            job.assistant_message_id,
            job.conversation_id,
            job.owner_user_id,
            job.binding_id,
        ),
    )
    row = await cursor.fetchone()
    if row is None:
        raise AiCallStartCancelled(
            "stale_conversation_binding",
            "Conversation, binding, or originating messages changed",
        )
    if not bool(row[5]):
        raise AiCallStartCancelled(
            "owner_disabled", "Conversation owner is disabled"
        )
    if int(row[1]) != job.prompt_id:
        raise AiCallStartCancelled("prompt_changed", "Conversation prompt changed")
    current_mode = str(row[2])
    mode_allowed = current_mode == "proactive" or (
        current_mode == "on_request" and job.initiation_mode == "on_request"
    )
    if not mode_allowed:
        raise AiCallStartCancelled(
            "initiation_disabled",
            "Prompt no longer permits this AI-initiated call",
        )


__all__ = [
    "AiCallJobPreparation",
    "AiCallStartCapability",
    "AiCallStartCancelled",
    "AiCallStartFenceLost",
    "AiCallStartJob",
    "AiCallStartOutboxRepository",
    "AiCallStartOutboxWorker",
    "enqueue_ai_call_start_in_transaction",
    "load_ai_call_start_capability",
]
