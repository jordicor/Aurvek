"""Bridge durable phone foreground decisions into the neutral AI runtime."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import inspect
import logging
from typing import Any

from ai_runtime.channel_turns import ChannelCommit, ChannelContext
from integrations.telephony.ai_call_outbox import (
    AiCallStartCapability,
    enqueue_ai_call_start_in_transaction,
    load_ai_call_start_capability,
)
from integrations.telephony.foreground import (
    ForegroundCommitGuard,
    ForegroundCoordinator,
    NonPhoneChannel,
    TurnForegroundDecision,
)
from integrations.telephony.repository import TelephonyRepository


CallStartCapabilityLoader = Callable[..., Awaitable[AiCallStartCapability | None]]


@dataclass(frozen=True, slots=True)
class CapturedChannelTurn:
    """One non-phone turn and the runtime context fenced to its capture."""

    decision: TurnForegroundDecision
    context: ChannelContext
    coordinator: ForegroundCoordinator

    async def is_current(self) -> bool:
        """Revalidate this capture immediately before an external side effect."""
        return await self.coordinator.commit_guard_is_current(
            self.decision.commit_guard
        )


def channel_context_from_decision(
    decision: TurnForegroundDecision,
    *,
    coordinator: ForegroundCoordinator,
) -> ChannelContext:
    """Create the canonical runtime callbacks for a captured foreground turn."""

    async def commit_guard(_context: ChannelContext, conn) -> bool:
        return await coordinator.assert_commit_guard_in_transaction(
            conn,
            decision.commit_guard,
        )

    async def queue_message(commit: ChannelCommit, conn) -> None:
        if commit.user_message_id is None:
            raise RuntimeError("Foreground ingestion did not persist a user message")
        await coordinator.queue_persisted_message_in_transaction(
            conn,
            decision,
            message_id=commit.user_message_id,
        )

    async def recover_stale_context(_context: ChannelContext) -> ChannelContext | None:
        recaptured = await coordinator.capture_turn(
            conversation_id=decision.commit_guard.conversation_id,
            channel=decision.channel,
        )
        if recaptured.action != "queued_for_active_phone":
            return None
        return channel_context_from_decision(
            recaptured,
            coordinator=coordinator,
        )

    guard = decision.commit_guard
    return ChannelContext(
        channel=decision.channel,
        persistence=(
            "ingest_only" if decision.action == "queued_for_active_phone" else "immediate"
        ),
        commit_guard=commit_guard,
        on_commit_in_transaction=(
            queue_message if decision.action == "queued_for_active_phone" else None
        ),
        recover_stale_context=recover_stale_context,
        provenance={
            "foreground_epoch": guard.epoch,
            **({"phone_call_id": guard.call_id} if guard.call_id else {}),
        },
    )


async def capture_non_phone_channel_turn(
    *,
    conversation_id: int,
    channel: NonPhoneChannel,
    coordinator: ForegroundCoordinator | None = None,
    connection_factory=None,
    call_start_capability_loader: CallStartCapabilityLoader = (
        load_ai_call_start_capability
    ),
) -> CapturedChannelTurn:
    """Capture foreground once, before any channel starts AI generation."""

    active_coordinator = coordinator or ForegroundCoordinator(
        TelephonyRepository(connection_factory=connection_factory)
        if connection_factory is not None
        else None
    )
    decision = await active_coordinator.capture_turn(
        conversation_id=conversation_id,
        channel=channel,
    )
    context = channel_context_from_decision(
        decision,
        coordinator=active_coordinator,
    )
    if decision.action == "generate" and connection_factory is not None:
        try:
            capability = await call_start_capability_loader(
                conversation_id=int(conversation_id),
                channel=channel,
                connection_factory=connection_factory,
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "Could not resolve AI phone-call capability for conversation %s",
                conversation_id,
                exc_info=True,
            )
            capability = None
        if capability is not None:
            context = _context_with_call_start_capability(context, capability)
    return CapturedChannelTurn(
        decision=decision,
        context=context,
        coordinator=active_coordinator,
    )


def _context_with_call_start_capability(
    context: ChannelContext,
    capability: AiCallStartCapability,
) -> ChannelContext:
    existing_hook = context.on_commit_in_transaction

    async def commit_hook(commit: ChannelCommit, conn: Any) -> None:
        if existing_hook is not None:
            result = existing_hook(commit, conn)
            if inspect.isawaitable(result):
                await result
        await enqueue_ai_call_start_in_transaction(
            conn,
            capability=capability,
            commit=commit,
        )

    return ChannelContext(
        channel=context.channel,
        persistence=context.persistence,
        input_origin=context.input_origin,
        input_perception=context.input_perception,
        turn_key=context.turn_key,
        commit_guard=context.commit_guard,
        on_commit_in_transaction=commit_hook,
        on_commit=context.on_commit,
        recover_stale_context=context.recover_stale_context,
        provenance={
            **dict(context.provenance),
            "call_start_controller": capability.controller,
            "call_start_mode": capability.mode,
        },
    )


def restore_non_phone_generation_context(
    *,
    conversation_id: int,
    channel: NonPhoneChannel,
    foreground_epoch: int,
    coordinator: ForegroundCoordinator | None = None,
) -> ChannelContext:
    """Restore the serializable non-phone fence inside a background worker."""

    active_coordinator = coordinator or ForegroundCoordinator()
    decision = TurnForegroundDecision(
        action="generate",
        channel=channel,
        commit_guard=ForegroundCommitGuard(
            conversation_id=int(conversation_id),
            epoch=int(foreground_epoch),
            expected_owner="non_phone",
        ),
    )
    return channel_context_from_decision(
        decision,
        coordinator=active_coordinator,
    )
