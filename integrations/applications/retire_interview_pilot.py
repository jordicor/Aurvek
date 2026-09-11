"""Selective operator cutover after the consumer confirms its replacement works.

Normally the live backend closes old delegations with end-app-session first.
An owner may explicitly waive missing historical closure evidence; the receipt
records that decision. This operation cannot certify or stop server activity,
and always refuses to proceed while recorded delegations remain valid.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import Field

from integrations.embed.models import EmbedError, StrictModel
from integrations.embed.store import EmbedStore, _one, _json
from .accounts import ApplicationAccountService
from .models import ConversationId, ExternalReference, LocalId, OperationId


class RetirePilotRequest(StrictModel):
    app_id: LocalId
    subject: ExternalReference
    source_user_id: ConversationId
    source_conversation_id: ConversationId
    external_project_id: ExternalReference
    replacement_operation_id: OperationId
    consumer_cutover_confirmed: bool = Field(default=False, strict=True)
    live_session_closure_confirmed: bool = Field(default=False, strict=True)
    owner_session_closure_waiver: str | None = Field(default=None, min_length=1, max_length=1000)


async def retire_interview_pilot(store, request, *, apply=False):
    request = RetirePilotRequest.model_validate(request)
    async with store.connection(readonly=not apply) as db:
        await db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        try:
            native = await _one(db, "SELECT user_id FROM CONVERSATIONS WHERE id=?", (request.source_conversation_id,))
            if not native or native["user_id"] != request.source_user_id:
                raise EmbedError("pilot_source_conflict", 409)
            imported = await _one(db, """SELECT * FROM APPLICATION_INTERVIEW_IMPORTS
                WHERE app_id=? AND operation_id=? AND state='copied'""",
                (request.app_id, request.replacement_operation_id))
            if not imported:
                raise EmbedError("replacement_unavailable", 409)
            receipt = json.loads(imported["result_json"])["receipt"]
            source = json.loads(imported["request_json"])
            if (source["source_user_id"] != request.source_user_id
                    or source["source_conversation_id"] != request.source_conversation_id
                    or receipt["subject"] == request.subject):
                raise EmbedError("replacement_conflict", 409)
            from .service import ApplicationService
            replacement = await ApplicationService(store).find_binding(db, int(receipt["conversation_id"]))
            if (not replacement or replacement["subject"] != receipt["subject"]
                    or replacement["app_id"] != request.app_id
                    or replacement["context_id"] != receipt["context_id"]):
                raise EmbedError("replacement_unavailable", 409)
            member = await _one(db, """SELECT m.*,s.user_id FROM EMBED_MEMBERSHIPS m
                JOIN EMBED_SUBJECTS s USING(subject) WHERE m.app_id=? AND m.subject=?""",
                (request.app_id, request.subject))
            if not member or member["user_id"] != request.source_user_id:
                raise EmbedError("pilot_membership_conflict", 409)
            binding = await _one(db, "SELECT * FROM APPLICATION_CONVERSATIONS WHERE conversation_id=?", (request.source_conversation_id,))
            interview = await _one(db, "SELECT * FROM EMBED_INTERVIEWS WHERE conversation_id=?", (request.source_conversation_id,))
            if binding and (binding["app_id"] != request.app_id or binding["subject"] != request.subject):
                raise EmbedError("pilot_binding_conflict", 409)
            if interview and (interview["app_id"] != request.app_id or interview["subject"] != request.subject
                              or interview["external_project_id"] != request.external_project_id):
                raise EmbedError("pilot_binding_conflict", 409)
            args = (request.app_id, request.subject)
            others = await _one(db, """SELECT 1 FROM APPLICATION_CONVERSATIONS
                WHERE app_id=? AND subject=? AND conversation_id!=? UNION ALL
                SELECT 1 FROM EMBED_INTERVIEWS WHERE app_id=? AND subject=? AND conversation_id!=?""",
                (*args, request.source_conversation_id, *args, request.source_conversation_id))
            if others:
                raise EmbedError("pilot_has_other_interviews", 409)
            sessions = await _one(db, """SELECT COUNT(*) AS total FROM EMBED_DELEGATED_SESSIONS
                WHERE app_id=? AND subject=? AND revoked_at IS NULL AND expires_at>?""", (*args, store.now()))
            channels = await _one(db, """SELECT COUNT(*) AS total FROM APPLICATION_CHANNEL_LINKS
                WHERE app_id=? AND subject=?""", args)
            # This retirement is scoped to the text-only pilot. A provisioned
            # account or channel needs its ordinary membership cleanup first.
            account = await _one(db, "SELECT 1 FROM APPLICATION_ACCOUNTS WHERE app_id=? AND subject=?", args)
            blockers = []
            if sessions["total"]:
                blockers.append("close_live_delegations")
            if channels["total"] or account:
                blockers.append("use_membership_channel_cleanup")
            if not request.consumer_cutover_confirmed:
                blockers.append("confirm_consumer_cutover")
            closure_waiver = (request.owner_session_closure_waiver or "").strip()
            if not request.live_session_closure_confirmed and not closure_waiver:
                blockers.append("confirm_live_end_app_session_closed")
            result = {"app_id": request.app_id, "source_user_id": request.source_user_id,
                      "source_conversation_id": request.source_conversation_id,
                      "replacement_receipt": receipt, "blockers": blockers,
                      "applied": False, "already_retired": not binding and not interview and not member["active"],
                      "live_session_closure_confirmed": request.live_session_closure_confirmed,
                      "owner_session_closure_waiver": closure_waiver or None,
                      "preserved": ["native_account", "native_sessions", "conversation", "messages",
                                    "files", "native_permissions", "memory", "billing_history"]}
            if apply:
                if blockers:
                    raise EmbedError("pilot_cutover_not_ready", 409)
                # Keep inactive membership/context and source memory provenance
                # for audit. Only remove the exact integration bindings.
                await db.execute("""UPDATE EMBED_MEMBERSHIPS SET active=0,version=version+1,updated_at=?
                    WHERE app_id=? AND subject=? AND active=1""", (store.now(), *args))
                await db.execute("""UPDATE EMBED_DELEGATED_SESSIONS SET revoked_at=COALESCE(revoked_at,?)
                    WHERE app_id=? AND subject=?""", (store.now(), *args))
                await ApplicationAccountService(store)._revoke_grants(db, request.app_id, request.source_user_id)
                await db.execute("UPDATE APPLICATION_PARTICIPANTS SET active=0 WHERE app_id=? AND subject=?", args)
                for table in ("EMBED_TICKETS", "EMBED_FRAMES"):
                    await db.execute(f"DELETE FROM {table} WHERE app_id=? AND subject=? AND conversation_id=?",
                                     (*args, request.source_conversation_id))
                await db.execute("DELETE FROM APPLICATION_CONVERSATIONS WHERE conversation_id=? AND app_id=? AND subject=?",
                                 (request.source_conversation_id, *args))
                await db.execute("DELETE FROM EMBED_INTERVIEWS WHERE app_id=? AND subject=? AND external_project_id=?",
                                 (*args, request.external_project_id))
                await db.execute("DELETE FROM EMBED_IDEMPOTENCY WHERE app_id=? AND subject=? AND external_project_id=?",
                                 (*args, request.external_project_id))
                # Retain brief revisions as import history; native chat does not
                # load them without its embed binding.
                await db.commit()
                result["applied"] = True
            else:
                await db.rollback()
            return result
        except BaseException:
            await db.rollback()
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    raw = sys.stdin.read() if args.request == Path("-") else args.request.read_text(encoding="utf-8")
    request = RetirePilotRequest.model_validate_json(raw)
    print(_json(asyncio.run(retire_interview_pilot(EmbedStore(), request, apply=args.apply))))


if __name__ == "__main__":
    main()
