"""Channel-neutral durable foreground coordination for phone conversations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from integrations.telephony.repository import TelephonyRepository


NonPhoneChannel = Literal["web", "whatsapp", "telegram", "device"]
ForegroundOwner = Literal["non_phone", "phone"]
TurnAction = Literal["generate", "queued_for_active_phone"]


@dataclass(frozen=True, slots=True)
class ForegroundCommitGuard:
    conversation_id: int
    epoch: int
    expected_owner: ForegroundOwner
    call_id: str | None = None
    lease_owner: str | None = None


@dataclass(frozen=True, slots=True)
class TurnForegroundDecision:
    action: TurnAction
    channel: NonPhoneChannel
    commit_guard: ForegroundCommitGuard

    @property
    def phone_active(self) -> bool:
        return self.commit_guard.expected_owner == "phone"


async def assert_commit_guard_in_transaction(
    conn,
    guard: ForegroundCommitGuard,
    *,
    now_utc: str | None = None,
) -> bool:
    """Verify a guard on the caller's commit transaction connection.

    The caller must invoke this after ``BEGIN IMMEDIATE`` and immediately before
    its guarded write/commit, using the same connection for both operations.
    """
    if guard.expected_owner == "non_phone":
        cursor = await conn.execute(
            """
            SELECT 1 FROM PHONE_CONVERSATION_FOREGROUND
            WHERE conversation_id=? AND epoch=? AND current_call_id IS NULL
            """,
            (guard.conversation_id, guard.epoch),
        )
        return await cursor.fetchone() is not None
    if not guard.call_id or not guard.lease_owner:
        return False
    now = now_utc or datetime.now(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    cursor = await conn.execute(
        """
        SELECT 1 FROM PHONE_CONVERSATION_FOREGROUND
        WHERE conversation_id=? AND current_call_id=? AND epoch=?
          AND lease_owner=? AND lease_until>=?
        """,
        (
            guard.conversation_id, guard.call_id, guard.epoch,
            guard.lease_owner, now,
        ),
    )
    return await cursor.fetchone() is not None


class ForegroundCoordinator:
    """Small service used by every channel before generating or committing a turn."""

    def __init__(self, repository: TelephonyRepository | None = None) -> None:
        self.repository = repository or TelephonyRepository()

    async def capture_turn(
        self,
        *,
        conversation_id: int,
        channel: NonPhoneChannel,
    ) -> TurnForegroundDecision:
        if channel not in {"web", "whatsapp", "telegram", "device"}:
            raise ValueError("Unsupported non-phone channel")
        state = await self.repository.capture_foreground_state(conversation_id)
        if state["phone_active"]:
            guard = ForegroundCommitGuard(
                conversation_id=int(conversation_id),
                epoch=int(state["epoch"]),
                expected_owner="phone",
                call_id=str(state["call_id"]),
                lease_owner=state["lease_owner"],
            )
            action: TurnAction = "queued_for_active_phone"
        else:
            guard = ForegroundCommitGuard(
                conversation_id=int(conversation_id),
                epoch=int(state["epoch"]),
                expected_owner="non_phone",
            )
            action = "generate"
        return TurnForegroundDecision(action, channel, guard)

    async def commit_guard_is_current(
        self,
        guard: ForegroundCommitGuard,
        *,
        now_utc: str | None = None,
    ) -> bool:
        if guard.expected_owner == "non_phone":
            return await self.repository.assert_non_phone_foreground(
                conversation_id=guard.conversation_id,
                epoch=guard.epoch,
            )
        if not guard.call_id or not guard.lease_owner:
            return False
        return await self.repository.assert_conversation_foreground(
            conversation_id=guard.conversation_id,
            call_id=guard.call_id,
            epoch=guard.epoch,
            lease_owner=guard.lease_owner,
            now_utc=now_utc,
        )

    async def assert_commit_guard_in_transaction(
        self,
        conn,
        guard: ForegroundCommitGuard,
        *,
        now_utc: str | None = None,
    ) -> bool:
        return await assert_commit_guard_in_transaction(
            conn, guard, now_utc=now_utc
        )

    async def queue_persisted_message(
        self,
        decision: TurnForegroundDecision,
        *,
        message_id: int,
    ) -> tuple[dict, bool]:
        guard = decision.commit_guard
        if decision.action != "queued_for_active_phone" or not guard.call_id:
            raise ValueError("Turn was not captured behind an active phone call")
        return await self.repository.link_other_channel_message(
            conversation_id=guard.conversation_id,
            call_id=guard.call_id,
            message_id=int(message_id),
            origin_channel=decision.channel,
            expected_epoch=guard.epoch,
        )

    async def queue_persisted_message_in_transaction(
        self,
        conn,
        decision: TurnForegroundDecision,
        *,
        message_id: int,
    ) -> tuple[dict, bool]:
        """Link a just-inserted message on the caller's commit transaction."""
        guard = decision.commit_guard
        if decision.action != "queued_for_active_phone" or not guard.call_id:
            raise ValueError("Turn was not captured behind an active phone call")
        return await self.repository.link_other_channel_message_in_transaction(
            conn,
            conversation_id=guard.conversation_id,
            call_id=guard.call_id,
            message_id=int(message_id),
            origin_channel=decision.channel,
            expected_epoch=guard.epoch,
        )

    async def acquire_phone(
        self,
        *,
        conversation_id: int,
        call_id: str,
        expected_epoch: int,
        lease_owner: str,
        lease_until: str,
    ) -> ForegroundCommitGuard | None:
        epoch = await self.repository.acquire_conversation_foreground(
            conversation_id=conversation_id,
            call_id=call_id,
            expected_epoch=expected_epoch,
            lease_owner=lease_owner,
            lease_until=lease_until,
        )
        if epoch is None:
            return None
        return ForegroundCommitGuard(
            int(conversation_id), epoch, "phone", call_id, lease_owner
        )

    async def renew_phone(
        self,
        guard: ForegroundCommitGuard,
        *,
        lease_until: str,
        now_utc: str | None = None,
    ) -> bool:
        if guard.expected_owner != "phone" or not guard.call_id or not guard.lease_owner:
            return False
        return await self.repository.renew_foreground_lease(
            call_id=guard.call_id,
            lease_owner=guard.lease_owner,
            fencing_token=guard.epoch,
            lease_until=lease_until,
            now_utc=now_utc,
        )

    async def takeover_phone(
        self,
        guard: ForegroundCommitGuard,
        *,
        new_lease_owner: str,
        now_utc: str,
        lease_until: str,
    ) -> ForegroundCommitGuard | None:
        if guard.expected_owner != "phone" or not guard.call_id:
            return None
        epoch = await self.repository.take_over_expired_foreground_lease(
            call_id=guard.call_id,
            new_lease_owner=new_lease_owner,
            now_utc=now_utc,
            lease_until=lease_until,
        )
        if epoch is None:
            return None
        return ForegroundCommitGuard(
            guard.conversation_id, epoch, "phone", guard.call_id, new_lease_owner
        )

    async def list_queued(
        self, guard: ForegroundCommitGuard, *, now_utc: str | None = None
    ) -> list[dict]:
        if guard.expected_owner != "phone" or not guard.call_id or not guard.lease_owner:
            return []
        return await self.repository.list_queued_other_channel_messages(
            conversation_id=guard.conversation_id,
            call_id=guard.call_id,
            epoch=guard.epoch,
            lease_owner=guard.lease_owner,
            now_utc=now_utc,
        )

    async def consume_queued(
        self,
        guard: ForegroundCommitGuard,
        *,
        message_id: int,
        turn_id: str,
        now_utc: str | None = None,
    ) -> bool:
        if guard.expected_owner != "phone" or not guard.call_id or not guard.lease_owner:
            return False
        return await self.repository.consume_queued_other_channel_message(
            conversation_id=guard.conversation_id,
            call_id=guard.call_id,
            message_id=int(message_id),
            epoch=guard.epoch,
            lease_owner=guard.lease_owner,
            turn_id=turn_id,
            now_utc=now_utc,
        )

    async def release_phone(self, guard: ForegroundCommitGuard) -> int | None:
        if guard.expected_owner != "phone" or not guard.call_id:
            return None
        return await self.repository.release_conversation_foreground(
            conversation_id=guard.conversation_id,
            call_id=guard.call_id,
            epoch=guard.epoch,
        )

    async def reconcile_hangup(self, guard: ForegroundCommitGuard) -> dict:
        if guard.expected_owner != "phone" or not guard.call_id:
            return {"released_epoch": None, "released_message_ids": []}
        return await self.repository.reconcile_phone_hangup(
            conversation_id=guard.conversation_id,
            call_id=guard.call_id,
            expected_epoch=guard.epoch,
        )
