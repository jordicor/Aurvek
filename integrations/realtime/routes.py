"""Owner-authorized real-time settings, independent of PSTN audio activation."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from auth import get_current_user
from database import get_db_connection
from models import User
from prompts import can_manage_prompt
from request_security import validate_mutation_request
from .configuration import save_settings, settings_response


class ReasoningSelectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["default", "off", "auto", "minimal", "low", "medium", "high", "xhigh", "max", "custom"] = "default"
    budget_tokens: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_budget(self):
        if (self.mode == "custom") != (self.budget_tokens is not None):
            raise ValueError("Only custom thinking requires a token budget")
        return self


class RealtimeSettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    llm_id: int | None = Field(default=None, gt=0)
    backend_llm_id: int | None = Field(default=None, gt=0)
    reasoning_selection: ReasoningSelectionPayload | None = None
    realtime_voice: str | None = Field(default=None, min_length=1, max_length=80)


router = APIRouter()


async def require_manager(user, prompt_id):
    if user is None or not user.is_enabled:
        raise HTTPException(401, "Authentication required")
    if not await can_manage_prompt(user.id, prompt_id, await user.is_admin):
        raise HTTPException(403, "Access denied")


@router.get("/api/prompts/{prompt_id}/realtime-settings")
async def get_settings(prompt_id: int, current_user: User = Depends(get_current_user)):
    await require_manager(current_user, prompt_id)
    async with get_db_connection(readonly=True) as connection:
        return await settings_response(connection, prompt_id)


@router.put("/api/prompts/{prompt_id}/realtime-settings", response_model=None)
async def update_settings(prompt_id: int, request: Request, payload: RealtimeSettingsPayload,
                          current_user: User = Depends(get_current_user)):
    await require_manager(current_user, prompt_id)
    rejection = validate_mutation_request(request)
    if rejection is not None:
        return rejection
    try:
        async with get_db_connection() as connection:
            return await save_settings(connection, prompt_id, **payload.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
