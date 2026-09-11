"""Operator-only adoption of an explicitly selected native interview.

Run from the Aurvek runtime directory; dry-run is the default. This module can
also be copied outside the checkout and run with that runtime on PYTHONPATH.
It does not initialize schemas, create accounts, activate apps or call providers.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import Field

from integrations.applications.models import AssistantConfig
from integrations.applications.schema import legacy_context_id
from integrations.applications.service import ApplicationService
from integrations.embed.models import EmbedError, InterviewBrief, StrictModel
from integrations.embed.store import EmbedStore, _digest, _json, _one


class AdoptInterviewRequest(StrictModel):
    app_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    subject: str = Field(min_length=1, max_length=256)
    user_id: int = Field(gt=0, strict=True)
    prompt_id: int = Field(gt=0, strict=True)
    conversation_id: int = Field(gt=0, strict=True)
    external_project_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._~-]+$")
    idempotency_key: str = Field(
        min_length=16, max_length=128, pattern=r"^[A-Za-z0-9._~-]+$")
    brief: InterviewBrief


async def adopt_existing_interview(
    store: EmbedStore, request: AdoptInterviewRequest, *, apply: bool = False,
) -> dict:
    """Validate and optionally bind a native chat, preserving all native data."""
    request = AdoptInterviewRequest.model_validate(request)
    if request.brief.revision != 1:
        raise EmbedError("initial_revision_required", 400)
    brief_json = _json(request.brief.model_dump())
    # Match ensure_interview's operation identity for its normal retry path.
    request_hash = _digest(_json({
        "project": request.external_project_id, "brief": request.brief.model_dump(),
    }))
    service = ApplicationService(store)
    async with store.connection(readonly=not apply) as connection:
        await connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        try:
            # Preparing a disabled app is allowed; adoption never enables it.
            _, app = await store._app(connection, request.app_id, active=False)
            if app.prompt_id != request.prompt_id:
                raise EmbedError("prompt_conflict", 409)
            _, member = await store._live(
                connection, request.app_id, request.user_id, request.subject)
            native = await _one(connection, """SELECT id,user_id,role_id,locked,
                is_incognito,hidden_from_history,purge_on_close
                FROM CONVERSATIONS WHERE id=?""", (request.conversation_id,))
            if (not native or native["user_id"] != request.user_id
                    or native["role_id"] != request.prompt_id):
                raise EmbedError("conversation_conflict", 409)
            if any(native[key] for key in (
                    "is_incognito", "hidden_from_history", "purge_on_close")):
                raise EmbedError("conversation_private", 409)
            if native["locked"]:
                raise EmbedError("conversation_locked", 409)
            if not await _one(connection, "SELECT id FROM PROMPTS WHERE id=?",
                              (request.prompt_id,)):
                raise EmbedError("prompt_unavailable", 409)
            assistant = await _one(connection, """SELECT * FROM APPLICATION_ASSISTANTS
                WHERE app_id=? AND assistant_id='default'""", (request.app_id,))
            if not assistant or assistant["prompt_id"] != request.prompt_id:
                raise EmbedError("assistant_prompt_conflict", 409)
            config = AssistantConfig.model_validate_json(assistant["config_json"])
            if not config.enabled or config.prompt_id != request.prompt_id:
                raise EmbedError("assistant_unavailable", 403)
            user = await store._load_user(request.user_id)
            if not user:
                raise EmbedError("unauthenticated")
            if store._permission_checker:
                permitted = await store._permission_checker(
                    user, request.prompt_id, connection)
            else:
                from prompts import can_user_access_prompt
                async with connection.cursor() as cursor:
                    permitted = await can_user_access_prompt(
                        user, request.prompt_id, cursor)
            if not permitted:
                raise EmbedError("membership_inactive", 403)

            row = {
                "app_id": request.app_id, "subject": request.subject,
                "external_project_id": request.external_project_id,
                "conversation_id": request.conversation_id,
                "user_id": request.user_id, "prompt_id": request.prompt_id,
                "brief_json": brief_json, "brief_revision": 1, "state": "active",
                "created_at": store.now(), "updated_at": store.now(),
            }
            context_id = legacy_context_id(row)
            participant = await _one(connection, """SELECT * FROM APPLICATION_PARTICIPANTS
                WHERE app_id=? AND subject=? AND context_ref=?""",
                (request.app_id, request.subject, request.external_project_id))
            context = await _one(connection,
                "SELECT * FROM APPLICATION_CONTEXTS WHERE context_id=?", (context_id,))
            if participant and (
                    participant["context_id"] != context_id or not participant["active"]):
                raise EmbedError("context_conflict", 409)
            if context and (context["app_id"] != request.app_id or not context["active"]):
                raise EmbedError("context_conflict", 409)
            if context and not participant:
                prior_alias = await _one(connection, """SELECT context_ref FROM APPLICATION_PARTICIPANTS
                    WHERE app_id=? AND context_id=? AND subject=?""",
                    (request.app_id, context_id, request.subject))
                if prior_alias:
                    raise EmbedError("context_conflict", 409)
            if participant and participant["assistant_ids_json"] is not None:
                if "default" not in json.loads(participant["assistant_ids_json"]):
                    raise EmbedError("assistant_unavailable", 403)
            capabilities = [
                app.capabilities, config.capabilities,
                json.loads(member["capabilities_json"]),
                json.loads(participant["capabilities_json"]) if participant else {"text": True},
            ]
            if not all(source.get("text") is True for source in capabilities):
                raise EmbedError("capability_unavailable", 403)

            existing = await _one(connection, """SELECT * FROM EMBED_INTERVIEWS
                WHERE app_id=? AND subject=? AND external_project_id=?""",
                (request.app_id, request.subject, request.external_project_id))
            other = await _one(connection,
                "SELECT * FROM EMBED_INTERVIEWS WHERE conversation_id=?",
                (request.conversation_id,))
            expected = {key: row[key] for key in (
                "app_id", "subject", "external_project_id",
                "conversation_id", "user_id", "prompt_id",
            )}
            for previous in (existing, other):
                if previous and (
                        any(previous[key] != value for key, value in expected.items())
                        or previous["state"] != "active"):
                    raise EmbedError("interview_conflict", 409)
            binding = await _one(connection,
                "SELECT * FROM APPLICATION_CONVERSATIONS WHERE conversation_id=?",
                (request.conversation_id,))
            expected_binding = {
                "app_id": request.app_id, "subject": request.subject,
                "user_id": request.user_id, "prompt_id": request.prompt_id,
                "context_id": context_id, "assistant_id": "default",
            }
            if binding and any(
                    binding[key] != value for key, value in expected_binding.items()):
                raise EmbedError("application_conflict", 409)
            revision = await _one(connection, """SELECT brief_json FROM EMBED_BRIEF_REVISIONS
                WHERE conversation_id=? AND revision=1""", (request.conversation_id,))
            if ((revision and revision["brief_json"] != brief_json)
                    or (existing and not revision)):
                raise EmbedError("revision_conflict", 409)
            operation = await _one(connection, """SELECT * FROM EMBED_IDEMPOTENCY
                WHERE app_id=? AND subject=? AND idempotency_key=?""",
                (request.app_id, request.subject, request.idempotency_key))
            if operation and (
                    operation["external_project_id"] != request.external_project_id
                    or operation["request_hash"] != request_hash):
                raise EmbedError("operation_conflict", 409)

            if apply:
                if not existing:
                    await connection.execute("""INSERT INTO EMBED_INTERVIEWS
                        (app_id,subject,external_project_id,user_id,conversation_id,prompt_id,
                         brief_json,brief_revision,state,created_at,updated_at)
                        VALUES (:app_id,:subject,:external_project_id,:user_id,:conversation_id,
                         :prompt_id,:brief_json,:brief_revision,:state,:created_at,:updated_at)""", row)
                if not revision:
                    await connection.execute(
                        "INSERT INTO EMBED_BRIEF_REVISIONS VALUES (?,1,?,?)",
                        (request.conversation_id, brief_json, store.now()))
                if not operation:
                    await connection.execute("INSERT INTO EMBED_IDEMPOTENCY VALUES (?,?,?,?,?)",
                        (request.app_id, request.subject, request.idempotency_key,
                         request.external_project_id, request_hash))
                await service.adopt_legacy(connection, existing or row)
                adopted = await service.find_binding(connection, request.conversation_id)
                if not adopted or any(
                        adopted[key] != value for key, value in expected_binding.items()):
                    raise EmbedError("application_conflict", 409)
                if app.enabled:
                    resolved = await service.authorize_runtime_conversation(
                        connection, request.user_id, request.conversation_id)
                    if resolved is None or not resolved.capabilities.get("text"):
                        raise EmbedError("capability_unavailable", 403)
                await connection.commit()
            else:
                await connection.rollback()
            return {
                "issuer": app.issuer, **expected,
                "conversation_id": str(request.conversation_id),
                "context_id": context_id,
                "brief_revision": (existing or row)["brief_revision"],
                "applied": apply, "created": apply and not bool(existing),
                "already_bound": bool(existing and binding), "app_enabled": app.enabled,
            }
        except BaseException:
            await connection.rollback()
            raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bind an explicitly selected native interview; dry-run by default.")
    parser.add_argument("request", type=Path,
                        help="Request JSON file, or - to read JSON from standard input")
    parser.add_argument("--apply", action="store_true",
                        help="Commit the binding without changing native content")
    args = parser.parse_args()
    request_json = (sys.stdin.read() if args.request == Path("-")
                    else args.request.read_text(encoding="utf-8"))
    request = AdoptInterviewRequest.model_validate_json(request_json)
    result = asyncio.run(adopt_existing_interview(EmbedStore(), request, apply=args.apply))
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
