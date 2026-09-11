"""A tool requests a transfer; the canonical turn commits it after settlement.

No background workflow or new conversation transcript. Pending state belongs to
the admitted native operation; a cancelled/unpersisted source never transfers.
The existing handoff operation is the durable deduplication record.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from integrations.embed.models import EmbedError
from .activity import get_active_operation, seal_application_turn
from .billing import current_application_operation
from .models import HandoffRequest
from .service import _one


@dataclass
class PendingToolHandoff:
    actor: object
    request: HandoffRequest
    frame_instance_id: str | None
    reservation_id: str
    bot_message_id: int | None = None
    handled: bool = False
    channel_admission: object | None = None
    channel_handle: object | None = None
    preparation: object | None = None


async def request_tool_handoff(context, arguments, reservation_id, *, service=None):
    from integrations.embed.activity import _current_generation
    from .tools import _services
    service = await _services(service)
    operation = current_application_operation(context.user_id)
    active = get_active_operation(context.conversation_id)
    if (operation is None or active is None or active.operation_id != operation.operation_id
            or active.pending_handoff is not None or active.ending or active.sealed
            or not reservation_id):
        raise EmbedError("handoff_unavailable", 409)
    from ai_runtime.channel_turns import current_channel_turn
    turn = current_channel_turn()
    channel_prepare = None
    if turn is not None and turn.context.persistence != "immediate":
        if turn.context.persistence != "deferred":
            raise EmbedError("handoff_channel_unavailable", 409)
        if turn.context.channel == "phone":
            channel_prepare = turn.context.provenance.get("prepare_phone_handoff")
        elif turn.context.input_origin == "web.live_voice":
            channel_prepare = turn.context.provenance.get("prepare_voice_handoff")
        if not callable(channel_prepare):
            raise EmbedError("handoff_channel_unavailable", 409)
    request = HandoffRequest(
        operation_id=hashlib.sha256(("handoff:" + operation.operation_id).encode()).hexdigest(),
        source_conversation_id=context.conversation_id,
        assistant_id=arguments["assistant_id"], mode=arguments.get("mode", "resume"),
        conversation_id=arguments.get("conversation_id"), note=arguments.get("note"))
    async with service.store.connection(readonly=True) as connection:
        await service._validated_actor(connection, context)
        _, _, _, caps = await service._scope(connection, context.app_id, context.subject,
            context.user_id, context.context_id, request.assistant_id)
        if not caps.get("text"):
            raise EmbedError("assistant_unavailable", 403)
        if request.conversation_id is not None:
            target = await service._authorize_actor_destination(connection, context, request.conversation_id)
            if target.assistant_id != request.assistant_id or target.conversation_id == context.conversation_id:
                raise EmbedError("invalid_handoff_destination", 409)
    generation = _current_generation.get()
    actor = context
    frame = None
    if generation is not None:
        actor = generation.principal
        if (actor.user_id, actor.app_id, actor.subject, actor.conversation_id) != (
                context.user_id, context.app_id, context.subject, context.conversation_id):
            raise EmbedError("application_scope_mismatch", 403)
        frame = actor.frame_instance_id
    pending = PendingToolHandoff(actor, request, frame, reservation_id)
    if turn is not None:
        pending.channel_admission = turn.context.application_channel
        if turn.context.persistence == "deferred":
            pending.channel_handle = turn.handle
    if channel_prepare is not None:
        pending.preparation = await channel_prepare(context, request, service=service)
        pending.request = pending.preparation.request
    active.pending_handoff = pending
    return pending


def record_tool_handoff_message(pending, bot_message_id):
    if pending is not None and bot_message_id:
        pending.bot_message_id = int(bot_message_id)


async def complete_tool_handoff(user_id, conversation_id, *, service=None):
    """Called once, after canonical provider consumption and billing finally."""
    active = get_active_operation(conversation_id)
    pending = active.pending_handoff if active else None
    if pending is None or pending.handled or not pending.bot_message_id:
        return None
    pending.handled = True
    try:
        return await _complete_pending_handoff(pending, active, user_id, conversation_id, service=service)
    except BaseException:
        if pending.preparation is not None:
            await pending.preparation.close()
        raise


async def _complete_pending_handoff(pending, active, user_id, conversation_id, *, service):
    from .handoff import ApplicationHandoffService
    from .tools import _services
    if pending.channel_handle is not None and (
            not pending.channel_handle.committed or not pending.channel_handle.fully_confirmed):
        raise EmbedError("handoff_source_not_committed", 409)
    operation = current_application_operation(user_id)
    if operation is None or active.operation_id != operation.operation_id:
        raise EmbedError("application_scope_mismatch", 403)
    service = await _services(service)
    async with service.store.connection(readonly=True) as connection:
        saved = await _one(connection, "SELECT 1 FROM MESSAGES WHERE id=? AND conversation_id=? AND type='bot'",
                           (pending.bot_message_id, conversation_id))
        source = await _one(connection, """SELECT r.status FROM BILLING_USAGE_RESERVATIONS r
            JOIN APPLICATION_BILLING_RESERVATIONS a ON a.reservation_id=r.id
            WHERE r.id=? AND a.operation_id=?""", (pending.reservation_id, operation.operation_id))
        unsettled = await _one(connection, """SELECT 1 FROM BILLING_USAGE_RESERVATIONS r
            JOIN APPLICATION_BILLING_RESERVATIONS a ON a.reservation_id=r.id
            WHERE a.operation_id=? AND r.status='active' LIMIT 1""", (operation.operation_id,))
        if not saved or not source or source["status"] != "settled" or unsettled:
            raise EmbedError("handoff_source_not_committed", 409)
    completed = seal_application_turn(operation)
    result = await ApplicationHandoffService(service.store).handoff(pending.actor, pending.request,
        frame_instance_id=pending.frame_instance_id, note_origin="assistant", completed_turn=completed,
        channel_admission=pending.channel_admission,
        move_in_transaction=(pending.preparation.commit if pending.preparation is not None else None))
    return {"conversation_id": result["conversation_id"], "assistant_id": result["assistant_id"],
            "source_conversation_id": conversation_id, "operation_id": pending.request.operation_id,
            "reload_url": result.get("reload_url"), "completed": True,
            **({"phone_handoff": result["phone_handoff"]} if "phone_handoff" in result else {}),
            **({"application_channel": result["application_channel"]} if "application_channel" in result else {})}
