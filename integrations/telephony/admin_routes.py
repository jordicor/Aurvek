"""Admin-only UI and mutation API for native telephone operations."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

import admin_audit
from auth import get_current_user
from captcha_service import get_captcha_config
from common import GOOGLE_CLIENT_ID, get_template_context, templates
from integrations.telephony.admin_service import (
    AdminGreetingList,
    AdminGreetingPhrase,
    GlobalAudioPublisher,
    TelephonyAdminConflict,
    TelephonyAdminError,
    TelephonyAdminMaterializedError,
    TelephonyAdminNotFound,
    TelephonyAdminService,
    TelephonyAdminUnavailable,
)
from integrations.telephony.security import TwilioCanonicalURLConfigurationError
from integrations.telephony.purge import PhoneDataPurgeRepository
from models import User
from request_security import (
    ensure_csrf_token,
    validate_mutation_request,
)


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class TelephonyConfigPayload(_Payload):
    telephony_enabled: bool
    telephony_transport: Literal["media_streams"] = "media_streams"
    telephony_stt_provider: Literal["elevenlabs"] = "elevenlabs"
    telephony_stt_model: Literal["scribe_v2_realtime"] = "scribe_v2_realtime"
    telephony_stt_language: Annotated[str, Field(min_length=1, max_length=40)] = "multi"
    telephony_endpointing_ms: int = Field(default=700, ge=300, le=3_000)
    telephony_barge_in_confirmation_ms: int = Field(
        default=350, ge=100, le=2_000
    )
    telephony_max_call_seconds: int = Field(ge=60, le=86_400)
    telephony_allowed_countries: list[
        Annotated[str, Field(pattern=r"^[A-Za-z]{2}$")]
    ] = Field(min_length=1, max_length=249)
    telephony_recording_default: bool = False
    telephony_amd_default: bool = False
    telephony_reconnect_attempts: int = Field(ge=0, le=2)
    telephony_silence_check_seconds: int = Field(ge=0, le=14_400)
    telephony_silence_hangup_seconds: int = Field(ge=0, le=14_400)
    telephony_scheduler_jitter_seconds: int = Field(ge=1, le=60)
    telephony_max_concurrent_dispatches: int = Field(ge=1, le=100)


class TelephonyNumberPayload(_Payload):
    enabled: bool
    inbound_enabled: bool
    is_outbound_default: bool
    confirm_affected_bindings: bool = False
    repair_webhook: bool = False


class PhoneBillingRatePayload(_Payload):
    provider: Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_-]+$")]
    component_type: Literal["pstn", "transport", "stt", "tts", "amd", "recording"]
    direction: Literal["", "inbound", "outbound"] = ""
    from_country: Annotated[str, Field(pattern=r"^(?:[A-Z]{2})?$")] = ""
    to_country: Annotated[str, Field(pattern=r"^(?:[A-Z]{2})?$")] = ""
    unit: Literal["minute", "character", "call"]
    provider_rate_per_unit: float = Field(ge=0)
    customer_rate_per_unit: float = Field(ge=0)
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3,8}$")]
    service_id: int | None = Field(default=None, gt=0)
    active: bool = True


class GlobalGreetingPhrasePayload(_Payload):
    literal_text: Annotated[str, Field(min_length=1, max_length=2_000)]
    enabled: bool = True


class GlobalGreetingListPayload(_Payload):
    mode: Literal["fixed", "random"]
    phrases: list[GlobalGreetingPhrasePayload] = Field(min_length=1, max_length=50)
    fixed_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_selection(self) -> "GlobalGreetingListPayload":
        if not any(item.enabled for item in self.phrases):
            raise ValueError("At least one greeting phrase must be enabled")
        if self.mode == "fixed":
            if (
                self.fixed_index is None
                or self.fixed_index >= len(self.phrases)
                or not self.phrases[self.fixed_index].enabled
            ):
                raise ValueError("A valid enabled fixed greeting is required")
        elif self.fixed_index is not None:
            raise ValueError("Random greetings do not use fixed_index")
        return self


class GlobalAudioPayload(_Payload):
    voice_id: int = Field(gt=0)
    inbound: GlobalGreetingListPayload
    outbound: GlobalGreetingListPayload
    notices: dict[str, Annotated[str, Field(min_length=1, max_length=2_000)]]


class PhoneDataPurgeRetryPayload(_Payload):
    expected_attempt_count: int = Field(ge=0)
    resolution: Literal["reconcile_by_purge"]


class ConfirmedMutationPayload(_Payload):
    confirmed: Literal[True]


class AdminJobReschedulePayload(ConfirmedMutationPayload):
    scheduled_at: Annotated[str, Field(min_length=1, max_length=40)]
    timezone_name: Annotated[str, Field(min_length=1, max_length=100)]
    fold: Literal[0, 1] | None = None


class AdminPaidTestCallPayload(_Payload):
    confirm_paid_call: Literal[True]
    conversation_id: int = Field(gt=0)
    idempotency_key: Annotated[str, Field(min_length=8, max_length=128)]


async def _require_admin(current_user: User | None) -> User:
    if current_user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")
    return current_user


def _greeting_list(payload: GlobalGreetingListPayload) -> AdminGreetingList:
    return AdminGreetingList(
        mode=payload.mode,
        phrases=tuple(
            AdminGreetingPhrase(item.literal_text, item.enabled)
            for item in payload.phrases
        ),
        fixed_index=payload.fixed_index,
    )


def _error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, TelephonyAdminNotFound):
        status = 404
    elif isinstance(exc, TelephonyAdminConflict):
        status = 409
    elif isinstance(exc, TelephonyAdminUnavailable):
        status = 503
    else:
        status = 400
    return JSONResponse(status_code=status, content={"detail": str(exc)})


async def _validated_mutation(
    request: Request,
    current_user: User | None,
) -> User | JSONResponse:
    user = await _require_admin(current_user)
    rejection = validate_mutation_request(request)
    return rejection if rejection is not None else user


def create_telephony_admin_router(
    service: TelephonyAdminService | None = None,
    purge_repository: PhoneDataPurgeRepository | None = None,
) -> APIRouter:
    active_service = service or TelephonyAdminService()
    active_purge_repository = purge_repository or PhoneDataPurgeRepository()
    router = APIRouter()

    @router.get("/admin/telephony", response_class=HTMLResponse)
    async def admin_telephony(
        request: Request,
        current_user: User = Depends(get_current_user),
    ):
        if current_user is None:
            return templates.TemplateResponse(
                "login.html",
                {
                    "request": request,
                    "captcha": get_captcha_config(),
                    "google_oauth_available": bool(GOOGLE_CLIENT_ID),
                },
            )
        await _require_admin(current_user)
        try:
            dashboard = await active_service.dashboard()
            error = request.query_params.get("error")
        except TelephonyAdminError as exc:
            dashboard = None
            error = str(exc)
        context = await get_template_context(request, current_user)
        context.update(
            {
                "telephony": dashboard,
                "telephony_csrf_token": ensure_csrf_token(request),
                "message": request.query_params.get("message"),
                "error": error,
            }
        )
        return templates.TemplateResponse("admin_telephony.html", context)

    @router.get("/admin/telephony/status")
    async def telephony_status(
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        await _require_admin(current_user)
        return await active_service.dashboard()

    @router.get("/admin/telephony/operations", response_model=None)
    async def telephony_operations(
        resource: Literal["calls", "jobs"],
        status: Annotated[str | None, Query(max_length=40)] = None,
        line_id: Annotated[int | None, Query(gt=0)] = None,
        contact_id: Annotated[int | None, Query(gt=0)] = None,
        owner_user_id: Annotated[int | None, Query(gt=0)] = None,
        conversation_id: Annotated[int | None, Query(gt=0)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
        current_user: User = Depends(get_current_user),
    ) -> Any:
        await _require_admin(current_user)
        try:
            return await active_service.list_operations(
                resource=resource,
                status=status,
                line_id=line_id,
                contact_id=contact_id,
                owner_user_id=owner_user_id,
                conversation_id=conversation_id,
                limit=limit,
                offset=offset,
            )
        except TelephonyAdminError as exc:
            return _error_response(exc)

    @router.get("/admin/telephony/calls/{call_id}", response_model=None)
    async def telephony_call_detail(
        call_id: str,
        current_user: User = Depends(get_current_user),
    ) -> Any:
        await _require_admin(current_user)
        try:
            return await active_service.call_detail(call_id)
        except TelephonyAdminError as exc:
            return _error_response(exc)

    @router.get("/admin/telephony/calls/{call_id}/recording", response_model=None)
    async def telephony_call_recording(
        call_id: str,
        track: Literal["mixed", "participant", "assistant"] = "mixed",
        current_user: User = Depends(get_current_user),
    ) -> Any:
        await _require_admin(current_user)
        try:
            path, media_type = await active_service.recording_file(call_id, track)
        except TelephonyAdminError as exc:
            return _error_response(exc)
        return FileResponse(
            path,
            media_type=media_type,
            headers={"Cache-Control": "private, no-store, max-age=0"},
        )

    @router.post("/admin/telephony/config", response_model=None)
    async def save_telephony_config(
        request: Request,
        payload: TelephonyConfigPayload,
        current_user: User = Depends(get_current_user),
    ) -> Any:
        checked = await _validated_mutation(request, current_user)
        if isinstance(checked, JSONResponse):
            return checked
        try:
            result = await active_service.save_config(payload.model_dump())
        except TelephonyAdminError as exc:
            return _error_response(exc)
        await admin_audit.log_admin_action(
            checked.id,
            "telephony_config_updated",
            request=request,
            target_resource_type="telephony_config",
            details="Native telephone configuration updated",
        )
        return {"status": "ok", "config": result}

    @router.post("/admin/telephony/numbers/sync", response_model=None)
    async def sync_telephony_numbers(
        request: Request,
        current_user: User = Depends(get_current_user),
    ) -> Any:
        checked = await _validated_mutation(request, current_user)
        if isinstance(checked, JSONResponse):
            return checked
        try:
            result = await active_service.sync_numbers()
        except TelephonyAdminError as exc:
            return _error_response(exc)
        await admin_audit.log_admin_action(
            checked.id,
            "telephony_numbers_synced",
            request=request,
            target_resource_type="telephony_numbers",
            details=f"Synchronized {result['synced']} owned numbers",
        )
        return {"status": "ok", **result}

    @router.patch("/admin/telephony/numbers/{number_id}", response_model=None)
    async def configure_telephony_number(
        number_id: int,
        request: Request,
        payload: TelephonyNumberPayload,
        current_user: User = Depends(get_current_user),
    ) -> Any:
        checked = await _validated_mutation(request, current_user)
        if isinstance(checked, JSONResponse):
            return checked
        try:
            result = await active_service.configure_number(
                number_id,
                **payload.model_dump(),
            )
        except (TelephonyAdminError, TwilioCanonicalURLConfigurationError) as exc:
            return _error_response(exc)
        await admin_audit.log_admin_action(
            checked.id,
            "telephony_number_updated",
            request=request,
            target_resource_type="telephony_number",
            target_resource_id=number_id,
            details=(
                f"enabled={result['enabled']};inbound={result['inbound_enabled']};"
                f"default={result['is_outbound_default']}"
            ),
        )
        return {"status": "ok", "number": result}

    @router.post("/admin/telephony/global-audio", response_model=None)
    async def publish_global_audio(
        request: Request,
        payload: GlobalAudioPayload,
        current_user: User = Depends(get_current_user),
    ) -> Any:
        checked = await _validated_mutation(request, current_user)
        if isinstance(checked, JSONResponse):
            return checked
        try:
            result = await active_service.publish_global_audio(
                billing_user_id=checked.id,
                voice_id=payload.voice_id,
                greetings={
                    "inbound": _greeting_list(payload.inbound),
                    "outbound": _greeting_list(payload.outbound),
                },
                notices=payload.notices,
            )
        except TelephonyAdminError as exc:
            return _error_response(exc)
        await admin_audit.log_admin_action(
            checked.id,
            "telephony_global_audio_activated",
            request=request,
            target_resource_type="telephony_audio_revision",
            target_resource_id=result["revision"],
            details=f"activation={result['activation_id']}",
        )
        return {"status": "ok", **result}

    @router.post("/admin/telephony/billing-rates", response_model=None)
    async def save_phone_billing_rate(
        request: Request,
        payload: PhoneBillingRatePayload,
        current_user: User = Depends(get_current_user),
    ) -> Any:
        checked = await _validated_mutation(request, current_user)
        if isinstance(checked, JSONResponse):
            return checked
        try:
            result = await active_service.save_billing_rate(payload.model_dump())
        except TelephonyAdminError as exc:
            return _error_response(exc)
        await admin_audit.log_admin_action(
            checked.id,
            "telephony_billing_rate_updated",
            request=request,
            target_resource_type="phone_billing_rate",
            target_resource_id=result["id"],
            details=(
                f"{result['provider']}:{result['component_type']}:"
                f"{result['direction'] or '*'}:{result['from_country'] or '*'}:"
                f"{result['to_country'] or '*'};active={result['active']}"
            ),
        )
        return {"status": "ok", "rate": result}

    @router.post(
        "/admin/telephony/purge-jobs/{job_id}/retry", response_model=None
    )
    async def retry_phone_data_purge(
        job_id: str,
        request: Request,
        payload: PhoneDataPurgeRetryPayload,
        current_user: User = Depends(get_current_user),
    ) -> Any:
        checked = await _validated_mutation(request, current_user)
        if isinstance(checked, JSONResponse):
            return checked
        retried = await active_purge_repository.retry(
            job_id=job_id,
            expected_attempt_count=payload.expected_attempt_count,
            resolution=payload.resolution,
        )
        if not retried:
            raise HTTPException(
                status_code=409,
                detail="Purge job state changed; refresh before retrying",
            )
        await admin_audit.log_admin_action(
            checked.id,
            "phone_data_purge_retry_confirmed",
            request=request,
            target_resource_type="phone_data_purge_job",
            details=(
                f"job={job_id};attempt={payload.expected_attempt_count};"
                f"resolution={payload.resolution}"
            ),
        )
        return {"status": "scheduled", "job_id": job_id}

    @router.post("/admin/telephony/jobs/{job_id}/cancel", response_model=None)
    async def cancel_phone_job(
        job_id: str,
        request: Request,
        payload: ConfirmedMutationPayload,
        current_user: User = Depends(get_current_user),
    ) -> Any:
        checked = await _validated_mutation(request, current_user)
        if isinstance(checked, JSONResponse):
            return checked
        try:
            result = await active_service.cancel_job(job_id)
        except TelephonyAdminError as exc:
            return _error_response(exc)
        await admin_audit.log_admin_action(
            checked.id,
            "phone_call_job_admin_canceled",
            request=request,
            target_resource_type="phone_call_job",
            details=f"job={job_id};state={result['state']}",
        )
        return {"status": "ok", **result}

    @router.post("/admin/telephony/jobs/{job_id}/reschedule", response_model=None)
    async def reschedule_phone_job(
        job_id: str,
        request: Request,
        payload: AdminJobReschedulePayload,
        current_user: User = Depends(get_current_user),
    ) -> Any:
        checked = await _validated_mutation(request, current_user)
        if isinstance(checked, JSONResponse):
            return checked
        try:
            result = await active_service.reschedule_job(
                job_id,
                scheduled_at=payload.scheduled_at,
                timezone_name=payload.timezone_name,
                fold=payload.fold,
            )
        except (TelephonyAdminError, ValueError) as exc:
            return _error_response(exc)
        await admin_audit.log_admin_action(
            checked.id,
            "phone_call_job_admin_rescheduled",
            request=request,
            target_resource_type="phone_call_job",
            details=f"job={job_id};timezone={payload.timezone_name}",
        )
        return {"status": "ok", **result}

    async def _admin_hangup(
        call_id: str,
        request: Request,
        current_user: User,
        *,
        retry_unresolved: bool,
    ) -> Any:
        checked = await _validated_mutation(request, current_user)
        if isinstance(checked, JSONResponse):
            return checked
        try:
            result = await active_service.hangup_call(
                call_id, retry_unresolved=retry_unresolved
            )
        except TelephonyAdminMaterializedError as exc:
            await admin_audit.log_admin_action(
                checked.id,
                "phone_call_hangup_admin_materialized_failure",
                request=request,
                target_resource_type="phone_call",
                details=(
                    f"call={call_id};outcome=failed;"
                    f"durable_state={exc.materialized_state}"
                ),
            )
            return _error_response(exc)
        except TelephonyAdminError as exc:
            return _error_response(exc)
        await admin_audit.log_admin_action(
            checked.id,
            (
                "phone_call_hangup_admin_retry"
                if retry_unresolved
                else "phone_call_hangup_admin_requested"
            ),
            request=request,
            target_resource_type="phone_call",
            details=f"call={call_id};state={result['state']}",
        )
        return {"status": "ok", **result}

    @router.post("/admin/telephony/calls/{call_id}/hangup", response_model=None)
    async def hangup_phone_call(
        call_id: str,
        request: Request,
        payload: ConfirmedMutationPayload,
        current_user: User = Depends(get_current_user),
    ) -> Any:
        return await _admin_hangup(
            call_id, request, current_user, retry_unresolved=False
        )

    @router.post(
        "/admin/telephony/calls/{call_id}/hangup/retry", response_model=None
    )
    async def retry_phone_call_hangup(
        call_id: str,
        request: Request,
        payload: ConfirmedMutationPayload,
        current_user: User = Depends(get_current_user),
    ) -> Any:
        return await _admin_hangup(
            call_id, request, current_user, retry_unresolved=True
        )

    @router.post("/admin/telephony/diagnostics/resync", response_model=None)
    async def resync_phone_diagnostics(
        request: Request,
        payload: ConfirmedMutationPayload,
        current_user: User = Depends(get_current_user),
    ) -> Any:
        checked = await _validated_mutation(request, current_user)
        if isinstance(checked, JSONResponse):
            return checked
        try:
            result = await active_service.resync_diagnostics()
        except TelephonyAdminMaterializedError as exc:
            await admin_audit.log_admin_action(
                checked.id,
                "telephony_diagnostics_resync_materialized_failure",
                request=request,
                target_resource_type="telephony_runtime",
                details=(
                    "outcome=failed;"
                    f"durable_state={exc.materialized_state}"
                ),
            )
            return _error_response(exc)
        except TelephonyAdminError as exc:
            return _error_response(exc)
        await admin_audit.log_admin_action(
            checked.id,
            "telephony_diagnostics_resynchronized",
            request=request,
            target_resource_type="telephony_runtime",
            details="Runtime, number inventory and readiness reconciled",
        )
        return {"status": "ok", **result}

    @router.post("/admin/telephony/test-call", response_model=None)
    async def create_paid_phone_test_call(
        request: Request,
        payload: AdminPaidTestCallPayload,
        current_user: User = Depends(get_current_user),
    ) -> Any:
        checked = await _validated_mutation(request, current_user)
        if isinstance(checked, JSONResponse):
            return checked
        try:
            result = await active_service.create_paid_test_call(
                conversation_id=payload.conversation_id,
                idempotency_key=payload.idempotency_key,
            )
        except TelephonyAdminError as exc:
            return _error_response(exc)
        await admin_audit.log_admin_action(
            checked.id,
            "telephony_paid_test_call_created",
            request=request,
            target_resource_type="phone_call_job",
            details=(
                f"conversation={payload.conversation_id};job={result['job_id']};"
                f"created={result['created']}"
            ),
        )
        return {"status": "scheduled", **result}

    return router


_default_service = TelephonyAdminService()
router = create_telephony_admin_router(_default_service)


def register_global_audio_publisher(publisher: GlobalAudioPublisher) -> None:
    """Attach the production renderer before global audio mutations are used."""

    _default_service.register_global_audio_publisher(publisher)


__all__ = [
    "GlobalAudioPayload",
    "PhoneBillingRatePayload",
    "PhoneDataPurgeRetryPayload",
    "TelephonyConfigPayload",
    "TelephonyNumberPayload",
    "create_telephony_admin_router",
    "register_global_audio_publisher",
    "router",
]
