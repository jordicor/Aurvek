"""Authenticated prompt-management API for native phone settings."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_runtime.voice_resolution import CanonicalVoiceResolutionError
from auth import get_current_user
from integrations.telephony.audio_cache_service import PhoneAudioCacheBuildError
from integrations.telephony.greetings import (
    PROMPT_TECHNICAL_NOTICE_KEYS,
    PhoneGreetingConfigurationError,
)
from integrations.telephony.prompt_settings_service import (
    GreetingListUpdate,
    GreetingPhraseUpdate,
    PromptPhoneAudioUnavailable,
    PromptPhonePolicyUpdate,
    PromptPhoneSettingsError,
    PromptPhoneSettingsNotFound,
    PromptPhoneSettingsService,
    TechnicalNoticeUpdate,
)
from models import User
from prompts import can_manage_prompt
from request_security import validate_mutation_request


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


class GreetingPhrasePayload(_Payload):
    literal_text: Annotated[str, Field(min_length=1, max_length=2_000)]
    enabled: bool = True


class GreetingListPayload(_Payload):
    mode: Literal["inherit", "fixed", "random"]
    phrases: list[GreetingPhrasePayload] = Field(default_factory=list, max_length=50)
    fixed_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_selection(self) -> "GreetingListPayload":
        if self.mode == "inherit" and (self.phrases or self.fixed_index is not None):
            raise ValueError("Inherited greetings cannot define prompt phrases")
        if self.mode in {"fixed", "random"} and not self.phrases:
            raise ValueError("Replacement greeting lists cannot be empty")
        if self.mode == "fixed":
            if self.fixed_index is None or self.fixed_index >= len(self.phrases):
                raise ValueError("A fixed greeting selection is required")
            if not self.phrases[self.fixed_index].enabled:
                raise ValueError("The fixed greeting must be enabled")
        elif self.fixed_index is not None:
            raise ValueError("Only fixed mode accepts fixed_index")
        if self.mode != "inherit" and not any(item.enabled for item in self.phrases):
            raise ValueError("At least one greeting phrase must be enabled")
        return self


class TechnicalNoticePayload(_Payload):
    mode: Literal["inherit", "custom"]
    notices: dict[
        str,
        Annotated[str, Field(min_length=1, max_length=2_000)],
    ] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_notice_set(self) -> "TechnicalNoticePayload":
        if self.mode == "inherit":
            if self.notices:
                raise ValueError(
                    "Inherited technical notices cannot define prompt copy"
                )
            return self
        expected = set(PROMPT_TECHNICAL_NOTICE_KEYS)
        actual = set(self.notices)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ValueError(
                "Custom technical notices must define exactly the seven prompt "
                f"keys (missing={missing}, extra={extra})"
            )
        return self


class ReasoningSelectionPayload(_Payload):
    mode: Literal[
        "default", "off", "auto", "minimal", "low", "medium", "high",
        "xhigh", "max", "custom",
    ] = "default"
    budget_tokens: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_budget(self) -> "ReasoningSelectionPayload":
        if self.mode == "custom" and self.budget_tokens is None:
            raise ValueError("Custom thinking requires a token budget")
        if self.mode != "custom" and self.budget_tokens is not None:
            raise ValueError("Only custom thinking accepts a token budget")
        return self


class PromptPhoneSettingsPayload(_Payload):
    voice_id: int | None = Field(default=None, gt=0)
    llm_id: int | None = Field(default=None, gt=0)
    reasoning_selection: ReasoningSelectionPayload | None = None
    realtime_voice: Literal[
        "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer",
        "verse", "marin", "cedar",
    ] | None = None
    stt_locale: Annotated[str, Field(min_length=2, max_length=40)] = "auto"
    endpointing_ms: int | None = Field(default=None, ge=300, le=3_000)
    interruptible: bool = True
    interrupt_sensitivity: Literal["low", "normal", "high"] = "normal"
    ignore_backchannels: bool = True
    max_duration_seconds: int | None = Field(default=None, ge=60)
    warning_milestones_seconds: list[int] = Field(
        default_factory=lambda: [900, 300, 180, 60], max_length=12
    )
    silence_enabled: bool = True
    silence_prompt_seconds: int | None = Field(default=60, ge=1)
    silence_hangup_seconds: int | None = Field(default=60, ge=1)
    ai_initiation_mode: Literal["on_request", "proactive", "disabled"] = (
        "on_request"
    )
    recording_default: bool = False
    amd_default: bool = False
    inbound_greeting: GreetingListPayload
    outbound_greeting: GreetingListPayload
    technical_notices: TechnicalNoticePayload | None = None

    @model_validator(mode="after")
    def validate_silence(self) -> "PromptPhoneSettingsPayload":
        if self.silence_enabled:
            if self.silence_prompt_seconds is None or self.silence_hangup_seconds is None:
                raise ValueError("Both silence timeouts are required when enabled")
        elif self.silence_prompt_seconds is not None or self.silence_hangup_seconds is not None:
            raise ValueError("Disabled silence handling must use null timeouts")
        return self


class _PromptPhoneRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        original = super().get_route_handler()

        async def handled(request: Request) -> Response:
            try:
                return await original(request)
            except PromptPhoneSettingsNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except PromptPhoneAudioUnavailable as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except PhoneAudioCacheBuildError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            except CanonicalVoiceResolutionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except (PromptPhoneSettingsError, PhoneGreetingConfigurationError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        return handled


async def _require_prompt_manager(current_user: User | None, prompt_id: int) -> User:
    if current_user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    is_admin = await current_user.is_admin
    if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
        raise HTTPException(status_code=403, detail="Access denied")
    return current_user


def _greeting_update(payload: GreetingListPayload) -> GreetingListUpdate:
    return GreetingListUpdate(
        mode=payload.mode,
        phrases=tuple(
            GreetingPhraseUpdate(item.literal_text, item.enabled)
            for item in payload.phrases
        ),
        fixed_index=payload.fixed_index,
    )


def _technical_notice_update(
    payload: TechnicalNoticePayload | None,
) -> TechnicalNoticeUpdate | None:
    if payload is None:
        return None
    return TechnicalNoticeUpdate(
        mode=payload.mode,
        notices=dict(payload.notices),
    )


def create_prompt_phone_settings_router(
    service: PromptPhoneSettingsService | None = None,
) -> APIRouter:
    active_service = service or PromptPhoneSettingsService()
    router = APIRouter(route_class=_PromptPhoneRoute)

    @router.get("/api/prompts/{prompt_id}/phone-settings")
    async def get_prompt_phone_settings(
        prompt_id: int,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        await _require_prompt_manager(current_user, prompt_id)
        return await active_service.get(prompt_id)

    @router.put("/api/prompts/{prompt_id}/phone-settings", response_model=None)
    async def update_prompt_phone_settings(
        prompt_id: int,
        request: Request,
        payload: PromptPhoneSettingsPayload,
        current_user: User = Depends(get_current_user),
    ) -> Any:
        await _require_prompt_manager(current_user, prompt_id)
        csrf_rejection = validate_mutation_request(request)
        if csrf_rejection is not None:
            return csrf_rejection
        silence_prompt = (
            payload.silence_prompt_seconds if payload.silence_enabled else None
        )
        silence_hangup = (
            payload.silence_hangup_seconds if payload.silence_enabled else None
        )
        return await active_service.update(
            prompt_id,
            billing_user_id=current_user.id,
            voice_id=payload.voice_id,
            phone_llm_id=payload.llm_id,
            phone_reasoning_selection=(
                None
                if payload.reasoning_selection is None
                else payload.reasoning_selection.model_dump(exclude_none=True)
            ),
            phone_realtime_voice=payload.realtime_voice,
            policy=PromptPhonePolicyUpdate(
                stt_locale=payload.stt_locale,
                endpointing_ms=payload.endpointing_ms,
                interruptible=payload.interruptible,
                interrupt_sensitivity=payload.interrupt_sensitivity,
                ignore_backchannels=payload.ignore_backchannels,
                max_duration_seconds=payload.max_duration_seconds,
                warning_milestones_seconds=tuple(
                    payload.warning_milestones_seconds
                ),
                silence_prompt_seconds=silence_prompt,
                silence_hangup_seconds=silence_hangup,
                ai_initiation_mode=payload.ai_initiation_mode,
                recording_default=payload.recording_default,
                amd_default=payload.amd_default,
            ),
            greetings={
                "inbound": _greeting_update(payload.inbound_greeting),
                "outbound": _greeting_update(payload.outbound_greeting),
            },
            technical_notices=_technical_notice_update(payload.technical_notices),
        )

    return router


router = create_prompt_phone_settings_router()


__all__ = [
    "GreetingListPayload",
    "GreetingPhrasePayload",
    "PromptPhoneSettingsPayload",
    "ReasoningSelectionPayload",
    "TechnicalNoticePayload",
    "create_prompt_phone_settings_router",
    "router",
]
