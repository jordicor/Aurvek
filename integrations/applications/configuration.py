"""Operator configuration over the existing application services, not a public API."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, model_validator

from integrations.embed.models import AppConfig, StrictModel
from .accounts import AccountPolicy, ApplicationAccountService
from .http_tools import ApplicationHTTPToolService, HTTPToolConfig
from .models import AssistantConfig, LocalId
from .service import ApplicationService


class FundingConfig(StrictModel):
    mode: Literal["native", "sponsored"] = "sponsored"
    payer_user_id: int | None = Field(default=None, strict=True, gt=0)
    monthly_limit: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    active: bool = Field(default=True, strict=True)

    @model_validator(mode="after")
    def valid_payer(self):
        if self.mode == "sponsored" and self.active and self.payer_user_id is None:
            raise ValueError("Sponsored funding requires payer_user_id")
        if self.mode == "native" and (self.payer_user_id is not None or self.monthly_limit is not None):
            raise ValueError("Native funding uses the native payer and limits")
        return self


class ApplicationConfiguration(StrictModel):
    app: AppConfig
    assistants: list[AssistantConfig] = Field(min_length=1, max_length=64)
    entry_assistant_id: LocalId
    account_policy: AccountPolicy | None = None
    funding: FundingConfig | None = None
    http_tools: list[HTTPToolConfig] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def valid_references(self):
        assistants = {item.assistant_id: item for item in self.assistants}
        if len(assistants) != len(self.assistants):
            raise ValueError("Assistant aliases must be unique")
        entry = assistants.get(self.entry_assistant_id)
        if entry is None or not entry.enabled:
            raise ValueError("Entry assistant must be included and enabled")
        if "default" in assistants and assistants["default"].prompt_id != self.app.prompt_id:
            raise ValueError("Default assistant must retain the app prompt")
        names = {item.name for item in self.http_tools}
        if len(names) != len(self.http_tools):
            raise ValueError("HTTP tool names must be unique")
        for tool in self.http_tools:
            if not set(tool.assistant_ids) <= assistants.keys():
                raise ValueError("HTTP tool references an assistant outside this configuration")
        if self.account_policy and not set(self.account_policy.assistant_ids) <= assistants.keys():
            raise ValueError("Account policy references an assistant outside this configuration")
        return self


async def read_configuration(store, app_id):
    """Return operator-owned configuration only; no secrets, users or transcripts."""
    async with store.connection(readonly=True) as db:
        _, app = await store._app(db, app_id, active=False)
        async def rows(sql):
            return [dict(row) for row in await (await db.execute(sql, (app_id,))).fetchall()]
        assistants = await rows("SELECT config_json FROM APPLICATION_ASSISTANTS WHERE app_id=? ORDER BY assistant_id")
        settings = await rows("SELECT entry_assistant_id FROM APPLICATION_SETTINGS WHERE app_id=?")
        policy = await rows("SELECT config_json FROM APPLICATION_ACCOUNT_POLICIES WHERE app_id=?")
        tools = await rows("SELECT config_json FROM APPLICATION_HTTP_TOOLS WHERE app_id=? ORDER BY name")
        funding = await rows("""SELECT mode,payer_user_id,monthly_limit,active FROM APPLICATION_FUNDING_GRANTS
            WHERE app_id=? AND context_id='' AND beneficiary_ref='' AND subject=''""")
    return {"app": app.model_dump(), "assistants": [json.loads(row["config_json"]) for row in assistants],
        "entry_assistant_id": settings[0]["entry_assistant_id"],
        "account_policy": json.loads(policy[0]["config_json"]) if policy else None,
        "funding": FundingConfig.model_validate({**funding[0], "active": bool(funding[0]["active"])}).model_dump() if funding else None,
        "http_tools": [json.loads(row["config_json"]) for row in tools]}


async def apply_configuration(store, config: ApplicationConfiguration, secret_provider):
    """Validate first; each existing service applies its own transactional update.

    Omitted policies and tools are preserved. Disable explicitly to remove access.
    Reapplying unchanged configuration does not revoke sessions or funding versions.
    """
    app_id = config.app.app_id
    service = ApplicationService(store)
    async with store.connection(readonly=True) as db:
        for assistant in config.assistants:
            if not await (await db.execute("SELECT 1 FROM PROMPTS WHERE id=?", (assistant.prompt_id,))).fetchone():
                raise ValueError(f"Prompt not found for assistant {assistant.assistant_id}")
        old = await (await db.execute("SELECT config_json FROM EMBED_APPS WHERE app_id=?", (app_id,))).fetchone()
        old_app = AppConfig.model_validate_json(old["config_json"]) if old else None
    prior = await read_configuration(store, app_id) if old_app else None
    if old_app != config.app:
        await store.register_app(config.app, None if old_app else secret_provider())
    await service.register_assistants(app_id, config.assistants, config.entry_assistant_id)
    if config.account_policy is not None and (not prior
            or prior["account_policy"] != config.account_policy.model_dump()
            or prior["app"]["capabilities"] != config.app.capabilities):
        await ApplicationAccountService(store).configure_policy(app_id, config.account_policy)
    tools = ApplicationHTTPToolService(service)
    for tool in config.http_tools:
        await tools.configure_http_tool(app_id, tool)
    if config.funding is not None and (not prior or prior["funding"] != config.funding.model_dump()):
        from .billing import configure_funding
        await configure_funding(app_id, **config.funding.model_dump())
    return {"app_id": app_id, "enabled": config.app.enabled,
        "assistants_updated": len(config.assistants), "http_tools_updated": len(config.http_tools)}
