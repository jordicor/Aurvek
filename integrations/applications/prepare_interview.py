"""Operator preparation of an independent application account and interview.

The consumer supplies persistent UUIDs. Planning is read-only; applying never
activates an app, copies credentials, calls a provider or enables consumption.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated
from uuid import UUID

from pydantic import Field, field_validator

from integrations.embed.models import EmbedError, InterviewBrief, StrictModel
from integrations.embed.store import EmbedStore, _one, _json
from .accounts import AccountPolicy, ApplicationAccountService, ProvisionAccountRequest
from .models import LocalId, OperationId
from .service import ApplicationService


class PrepareInterviewRequest(StrictModel):
    app_id: LocalId
    external_user_id: str
    external_project_id: str
    operation_id: OperationId
    source_user_id: int = Field(strict=True, gt=0)
    source_conversation_id: int = Field(strict=True, gt=0)
    brief: InterviewBrief
    capabilities: dict[str, Annotated[bool, Field(strict=True)]] = Field(
        default_factory=lambda: {"text": True}, max_length=64)

    @field_validator("external_user_id", "external_project_id")
    @classmethod
    def persistent_uuid(cls, value):
        # UUIDs must originate in the consumer; never generate an alternate ID.
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("A canonical persistent consumer UUID is required")
        return value


async def prepare_interview(store, request: PrepareInterviewRequest, *, apply=False):
    request = PrepareInterviewRequest.model_validate(request)
    if request.brief.revision != 1 or request.capabilities.get("text") is not True:
        raise EmbedError("invalid_preparation", 400)
    accounts = ApplicationAccountService(store)
    service = ApplicationService(store)
    async with store.connection(readonly=True) as db:
        _, app = await store._app(db, request.app_id, active=False)
        source = await _one(db, "SELECT * FROM CONVERSATIONS WHERE id=?", (request.source_conversation_id,))
        if not source or source["user_id"] != request.source_user_id or source["role_id"] != app.prompt_id:
            raise EmbedError("source_conflict", 409)
        if any(source.get(key) for key in ("is_incognito", "hidden_from_history", "purge_on_close", "locked")):
            raise EmbedError("source_unavailable", 409)
        row = await _one(db, "SELECT config_json FROM APPLICATION_ACCOUNT_POLICIES WHERE app_id=?", (request.app_id,))
        policy = AccountPolicy.model_validate_json(row["config_json"]) if row else AccountPolicy()
        if (not policy.allow_provision or not policy.admit_new_accounts or policy.allow_link
                or policy.assistant_ids != ["default"]):
            raise EmbedError("independent_account_policy_required", 409)
        assistant = await _one(db, "SELECT config_json,prompt_id FROM APPLICATION_ASSISTANTS WHERE app_id=? AND assistant_id='default'", (request.app_id,))
        config = json.loads(assistant["config_json"]) if assistant else {}
        if not assistant or assistant["prompt_id"] != app.prompt_id or not config.get("enabled", True):
            raise EmbedError("assistant_unavailable", 409)
        for key, enabled in request.capabilities.items():
            if enabled and not all(layer.get(key) is True for layer in (
                    policy.capabilities, app.capabilities, config.get("capabilities", {}))):
                raise EmbedError("capability_unavailable", 403)
        model = await _one(db, "SELECT id,enabled FROM LLM WHERE id=?", (policy.llm_id,))
        if not model or not model["enabled"]:
            raise EmbedError("model_unavailable", 409)
        binding = await _one(db, """SELECT a.*,s.user_id FROM APPLICATION_ACCOUNTS a
            JOIN EMBED_SUBJECTS s USING(subject) WHERE a.app_id=? AND a.external_user_id=?""",
            (request.app_id, request.external_user_id))
        if binding and (not binding["created_here"] or binding["user_id"] == request.source_user_id):
            raise EmbedError("independent_account_required", 409)
        prior = await _one(db, """SELECT request_json,state FROM APPLICATION_INTERVIEW_IMPORTS
            WHERE app_id=? AND operation_id=?""", (request.app_id, request.operation_id))
        if prior:
            previous = json.loads(prior["request_json"])
            if prior["state"] != "copied" or any(previous[key] != request.model_dump()[key]
                    for key in ("external_user_id", "external_project_id", "source_user_id",
                                "source_conversation_id", "brief")):
                raise EmbedError("operation_conflict", 409)
        plan = {"app_id": request.app_id, "external_user_id": request.external_user_id,
                "external_project_id": request.external_project_id,
                "source_user_id": request.source_user_id,
                "source_conversation_id": request.source_conversation_id,
                "prompt_id": app.prompt_id, "llm_id": policy.llm_id,
                "app_enabled": app.enabled, "account_exists": binding is not None,
                "consumption": "unavailable_until_sponsored_funding", "applied": False}
    if not apply:
        return plan
    # The normal account operation provides atomic opaque creation and replay.
    # It does not copy source profile fields, passwords or contacts.
    profile = (await accounts.status(request.app_id, request.external_user_id) if binding
               else await accounts.provision(request.app_id, ProvisionAccountRequest(
                   external_user_id=request.external_user_id,
                   operation_id="prepare-account:" + request.external_user_id)))
    async with store.connection(readonly=True) as db:
        participant = await _one(db, """SELECT * FROM APPLICATION_PARTICIPANTS
            WHERE app_id=? AND subject=? AND context_ref=?""",
            (request.app_id, profile["subject"], request.external_project_id))
        if participant and (not participant["active"]
                or json.loads(participant["capabilities_json"]) != request.capabilities
                or json.loads(participant["assistant_ids_json"] or "null") != ["default"]):
            raise EmbedError("context_conflict", 409)
    context_id = participant["context_id"] if participant else await service.grant_context(
        request.app_id, profile["subject"], request.external_project_id,
        capabilities=request.capabilities, assistant_ids=["default"])
    async with store.transaction() as db:
        # A later account decision owns all of its stories. Preparation replay
        # must not recreate the original context-level consumption blocker.
        funding = await _one(db, """SELECT mode,active FROM APPLICATION_FUNDING_GRANTS
            WHERE app_id=? AND subject=?""", (request.app_id, profile['subject']))
        if funding is None:
            # NULL payer is intentional while inactive; admission rejects it.
            await db.execute("""INSERT OR IGNORE INTO APPLICATION_FUNDING_GRANTS
                (grant_id,app_id,context_id,mode,payer_user_id,monthly_limit,active)
                VALUES (?, ?, ?, 'sponsored', NULL, 0, 0)""",
                ("prepared:" + context_id, request.app_id, context_id))
            funding = await _one(db, """SELECT mode,active FROM APPLICATION_FUNDING_GRANTS
                WHERE app_id=? AND context_id=? AND beneficiary_ref='' AND subject=''""", (request.app_id, context_id))
        if funding["mode"] != "sponsored":
            raise EmbedError("sponsored_funding_required", 409)
    from .interview_copy import ApplicationInterviewCopyService, InterviewCopyRequest
    copied = await ApplicationInterviewCopyService(store).copy(InterviewCopyRequest(
        operation_id=request.operation_id, app_id=request.app_id,
        external_user_id=request.external_user_id, expected_subject=profile["subject"],
        external_project_id=request.external_project_id, expected_context_id=context_id,
        assistant_id="default", source_user_id=request.source_user_id,
        source_conversation_id=request.source_conversation_id, brief=request.brief))
    return {**copied, "applied": True,
            "consumption": "sponsored" if funding["active"] else "unavailable_until_sponsored_funding"}


def main():
    from log_config import cli_diagnostics_to_stderr
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path, help="Private request JSON file, or - for stdin")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--manifest", type=Path, help="Private manifest output; never a public/static path")
    args = parser.parse_args()
    value = sys.stdin.read() if args.request == Path("-") else args.request.read_text(encoding="utf-8")
    request = PrepareInterviewRequest.model_validate_json(value)
    with cli_diagnostics_to_stderr():
        result = asyncio.run(prepare_interview(EmbedStore(), request, apply=args.apply))
    manifest = result.pop("manifest", None)
    if manifest is not None and args.manifest:
        args.manifest.write_text(_json(manifest) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
