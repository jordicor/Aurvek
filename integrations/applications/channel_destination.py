"""Explicit host selection of a proven channel's next conversation."""

import json
import secrets

from integrations.embed.models import EmbedError, StrictModel
from integrations.embed.store import _digest, _json, _one
from .models import ConversationId, LocalId, OperationId


class ChannelDestination(StrictModel):
    operation_id: OperationId
    receiver_id: LocalId
    conversation_id: ConversationId


async def select_destination(store, principal, body):
    from .accounts import AccountOperation, ApplicationAccountService
    from .activity import handoff_boundary
    from .channels import ApplicationChannelService
    from .contacts import contact_state, ensure_phone_adapters
    from .profile import ApplicationProfileService

    service = ApplicationChannelService(store)
    # Fence before mutating routes; no in-progress call or model turn is redirected.
    async with handoff_boundary(body.conversation_id) as boundary:
        async with store.transaction() as connection:
            account = await ApplicationProfileService(store)._account(
                connection, principal
            )
            target = await service.applications._authorize_actor_destination(
                connection, principal, body.conversation_id
            )
            receiver = await _one(
                connection,
                "SELECT * FROM APPLICATION_CHANNEL_RECEIVERS WHERE app_id=? AND receiver_id=? AND enabled=1",
                (principal.app_id, body.receiver_id),
            )
            if not receiver or not target.capabilities.get(receiver["channel"]):
                raise EmbedError("channel_receiver_unavailable", 403)
            digest = _digest(
                _json({"kind": "channel_destination", "body": body.model_dump()})
            )
            prior = await _one(
                connection,
                """SELECT * FROM APPLICATION_ACCOUNT_OPERATIONS
                WHERE app_id=? AND external_user_id=? AND operation_id=?""",
                (principal.app_id, account["external_user_id"], body.operation_id),
            )
            if prior:
                if prior["request_hash"] != digest:
                    raise EmbedError("idempotency_conflict", 409)
                return json.loads(prior["result_json"])
            contact = await contact_state(
                connection, principal.app_id, principal.subject
            )
            if not contact["verified"]:
                raise EmbedError("phone_proof_required", 403)
            await ensure_phone_adapters(
                connection,
                principal.app_id,
                principal.subject,
                contact["phone_number"],
                store.now(),
            )
            link = await _one(
                connection,
                "SELECT link_id FROM APPLICATION_CHANNEL_LINKS WHERE receiver_id=? AND subject=? AND active=1",
                (body.receiver_id, principal.subject),
            )
            if not link:
                raise EmbedError("channel_connection_required", 403)
            link = await service._link(connection, link["link_id"])
            routes = await (
                await connection.execute(
                    "SELECT * FROM APPLICATION_CHANNEL_ROUTES WHERE link_id=? AND active=1",
                    (link["link_id"],),
                )
            ).fetchall()
            for route in routes:
                if route["conversation_id"] != target.conversation_id:
                    boundary.include(route["conversation_id"])
            await connection.execute(
                "UPDATE APPLICATION_CHANNEL_LINKS SET default_context_id=? WHERE link_id=?",
                (target.context_id, link["link_id"]),
            )
            # Phone sessions are CallSid-bound and retain their frozen route.
            # The selected exact destination is used when a new inbound session starts.
            await connection.execute(
                """INSERT INTO APPLICATION_CHANNEL_DESTINATIONS VALUES (?,?,?)
                ON CONFLICT(link_id) DO UPDATE SET conversation_id=excluded.conversation_id,operation_id=excluded.operation_id""",
                (link["link_id"], target.conversation_id, body.operation_id),
            )
            if receiver["channel"] != "phone":
                await connection.execute(
                    """INSERT INTO APPLICATION_CHANNEL_ROUTES
                    (route_id,receiver_id,link_id,provider_identity,session_key,conversation_id,created_at,updated_at,last_operation_id)
                    VALUES (?,?,?,?,'messaging',?,?,?,?) ON CONFLICT(receiver_id,provider_identity,session_key)
                    DO UPDATE SET link_id=excluded.link_id,conversation_id=excluded.conversation_id,
                    active=1,version=version+1,updated_at=excluded.updated_at,last_operation_id=excluded.last_operation_id""",
                    (
                        secrets.token_urlsafe(24),
                        body.receiver_id,
                        link["link_id"],
                        link["provider_identity"],
                        target.conversation_id,
                        store.now(),
                        store.now(),
                        body.operation_id,
                    ),
                )
            result = {
                "status": "selected",
                "receiver_id": body.receiver_id,
                "link_id": link["link_id"],
                "channel": receiver["channel"],
                "receiver_key": receiver["receiver_key"],
                "open_url": (
                    "https://wa.me/" + receiver["receiver_key"].lstrip("+")
                    if receiver["channel"] == "whatsapp"
                    else "tel:" + receiver["receiver_key"]
                    if receiver["channel"] == "phone"
                    else None
                ),
                "conversation_id": target.conversation_id,
                "context_id": target.context_id,
                "assistant_id": target.assistant_id,
            }
            return await ApplicationAccountService(store)._save(
                connection,
                principal.app_id,
                AccountOperation(
                    external_user_id=account["external_user_id"],
                    operation_id=body.operation_id,
                ),
                digest,
                result,
            )
