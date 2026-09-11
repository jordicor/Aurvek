"""Prepare a live phone destination and join its move to the F3 transaction."""
from __future__ import annotations

import json
from dataclasses import dataclass

from integrations.embed.models import EmbedError
from integrations.embed.store import _one
from .billing import admit_handoff_funding
from .channel_models import ChannelAdmission
from .models import HandoffRequest


def _comparable_snapshot(snapshot):
    # A newly requested conversation is materialized only by the final F3 move.
    return {key: value for key, value in snapshot.items()
            if key not in {"captured_at", "conversation_id", "_application"}}


async def _destination_snapshot(connection, target, call):
    from integrations.telephony.snapshot import build_conversation_phone_snapshot
    if not all(target.capabilities.get(key) for key in ("text", "phone", "stt", "tts")):
        raise EmbedError("application_capability_denied", 403)
    try:
        snapshot = await build_conversation_phone_snapshot(target.conversation_id,
            expected_owner_user_id=target.user_id, conn=connection)
    except ValueError as exc:
        raise EmbedError("handoff_phone_configuration_unavailable", 409) from exc
    values = snapshot.as_dict()
    original = json.loads(call["config_snapshot_json"])
    for key in ("max_duration_seconds", "warning_milestones_seconds"):
        values[key] = original[key]
    return values


@dataclass
class PreparedPhoneHandoff:
    request: HandoffRequest
    repository: object
    call_id: str
    expected_epoch: int
    lease_owner: str
    snapshot: dict
    transport: object

    async def close(self):
        # The session candidate retains the applied client and closes only an
        # unused one. It also releases its input locks when a move is rejected.
        await self.transport.close()

    async def commit(self, connection, source, target, *, channel_admission, replay=False):
        from integrations.telephony.repository import active_call
        call = await _one(connection, "SELECT * FROM PHONE_CALLS WHERE id=? AND deleted_at IS NULL", (self.call_id,))
        if call is None:
            raise EmbedError("handoff_phone_unavailable", 409)
        current = active_call(call)
        if replay:
            config = json.loads(current["config_snapshot_json"])
            if (current["conversation_id"] != target.conversation_id
                    or config.get("_application", {}).get("handoff_operation_id") != self.request.operation_id):
                raise EmbedError("handoff_phone_changed", 409)
            return {"phone_handoff": {"call_id": self.call_id,
                "conversation_id": target.conversation_id,
                "foreground_epoch": current["foreground_fencing_token"]}}
        if (current["conversation_id"] != source.conversation_id
                or current["owner_user_id"] != source.user_id or channel_admission is None):
            raise EmbedError("handoff_phone_changed", 409)
        snapshot = await _destination_snapshot(connection, target, call)
        if _comparable_snapshot(snapshot) != _comparable_snapshot(self.snapshot):
            raise EmbedError("handoff_phone_configuration_changed", 409)
        operation = await admit_handoff_funding(connection, target, snapshot["runtime_llm_id"])
        snapshot["_application"] = {
            "channel": channel_admission.as_dict(),
            "billing_operation_id": operation.operation_id,
            "handoff_operation_id": self.request.operation_id,
        }
        await self.transport.validate()
        from integrations.telephony.repository import TelephonyConflictError, TelephonyStateError
        try:
            moved = await self.repository.move_call_context(connection,
                call_id=self.call_id, source_conversation_id=source.conversation_id,
                destination_conversation_id=target.conversation_id,
                expected_epoch=self.expected_epoch, lease_owner=self.lease_owner,
                config_snapshot=snapshot)
        except (TelephonyConflictError, TelephonyStateError) as exc:
            raise EmbedError("handoff_phone_changed", 409) from exc
        return {"phone_handoff": {"call_id": self.call_id,
            "conversation_id": target.conversation_id,
            "foreground_epoch": moved["foreground_fencing_token"]}}


async def prepare_phone_handoff(context, request, *, service, repository,
                               call_id, expected_epoch, lease_owner, prepare_transport):
    """Resolve native settings and funding before any spoken transfer promise.

    The preview transaction is rolled back, including a possible new empty
    conversation. F3 creates that destination only if the source turn succeeds.
    Provider preparation runs after releasing the database transaction.
    """
    from .handoff import prepare_handoff_destination
    from .channels import ApplicationChannelService
    from integrations.telephony.repository import TelephonyRepository, active_call
    # Media sessions supply the provider repository; native context moves live
    # in TelephonyRepository and use the same database/transaction.
    repository = TelephonyRepository(repository.connection_factory)
    async def prepare(connection, source, target):
        call = await _one(connection, "SELECT * FROM PHONE_CALLS WHERE id=? AND deleted_at IS NULL", (call_id,))
        if call is None:
            raise EmbedError("handoff_phone_unavailable", 409)
        current = active_call(call)
        if (current["conversation_id"] != source.conversation_id
                or current["owner_user_id"] != source.user_id
                or current["foreground_fencing_token"] != expected_epoch
                or current["foreground_lease_owner"] != lease_owner):
            raise EmbedError("handoff_phone_changed", 409)
        attribution = json.loads(current["config_snapshot_json"]).get("_application")
        if not attribution:
            raise EmbedError("application_channel_required", 403)
        admission = ChannelAdmission.from_dict(attribution["channel"])
        await ApplicationChannelService(service.store).revalidate(
            connection, admission, require_current_identity=True, capability="phone")
        foreground = await _one(connection, """SELECT 1 FROM PHONE_CONVERSATION_FOREGROUND
            WHERE conversation_id=? AND current_call_id IS NOT NULL""", (target.conversation_id,))
        if foreground:
            raise EmbedError("activity_in_progress", 409)
        snapshot = await _destination_snapshot(connection, target, call)
        await admit_handoff_funding(connection, target, snapshot["runtime_llm_id"])
        return snapshot
    request, snapshot = await prepare_handoff_destination(service, context, request,
        prepare_in_transaction=prepare)
    try:
        transport = await prepare_transport(snapshot)
    except EmbedError:
        raise
    except Exception as exc:
        raise EmbedError("handoff_voice_unavailable", 409) from exc
    return PreparedPhoneHandoff(request, repository, call_id, expected_epoch,
        lease_owner, snapshot, transport)
