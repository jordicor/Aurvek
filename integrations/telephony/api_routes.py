"""Authenticated owner API for contacts, bindings and one-shot phone calls."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import os
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.routing import APIRoute
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.background import BackgroundTask

from auth import get_current_user
from integrations.embed.models import EmbedError
from integrations.telephony.repository import (
    TelephonyConflictError,
    TelephonyNotFoundError,
    TelephonyRepository,
    TelephonyStateError,
)
from integrations.telephony.purge import (
    PhoneDataPurgeFailure,
    PhoneDataPurgeRepository,
    PhoneDataPurgeService,
)
from integrations.telephony.recording_storage import (
    PrivateRecordingPathError,
    resolve_private_recording_path,
)
from integrations.telephony.message_audio import (
    PhoneMessageAudioRange,
    open_pcmu_range_as_wav_chunks,
    wav_content_length,
)
from integrations.telephony.schemas import CALL_TERMINAL_STATUSES, PhoneCallStatus
from integrations.telephony.twilio_client import AsyncTwilioVoiceClient
from integrations.telephony.user_service import (
    PhoneCountryBlockedError,
    PhoneNumberValidationUnavailable,
    PhoneUserServiceError,
    PhoneUserUnavailableError,
    UserPhoneService,
)
from models import User
from request_security import validate_mutation_request


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


class ContactCreate(_Payload):
    display_name: Annotated[str, Field(min_length=1, max_length=200)]
    e164: Annotated[str, Field(min_length=8, max_length=16)]
    timezone_name: Annotated[str, Field(min_length=1, max_length=100)]


class ContactPatch(_Payload):
    display_name: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    e164: Annotated[str | None, Field(min_length=8, max_length=16)] = None
    timezone_name: Annotated[str | None, Field(min_length=1, max_length=100)] = None

    @model_validator(mode="after")
    def require_change(self) -> "ContactPatch":
        if not self.model_fields_set:
            raise ValueError("At least one contact field is required")
        return self


class BindingCreate(_Payload):
    contact_id: int = Field(gt=0)
    preferred_number_id: int | None = Field(default=None, gt=0)
    allow_inbound: bool = True
    allow_outbound: bool = True


class BindingPatch(_Payload):
    preferred_number_id: Annotated[int, Field(gt=0)] | None = None
    allow_inbound: bool | None = None
    allow_outbound: bool | None = None

    @model_validator(mode="after")
    def require_change(self) -> "BindingPatch":
        if not self.model_fields_set:
            raise ValueError("At least one binding field is required")
        return self


class SelfBindingCreate(_Payload):
    timezone_name: Annotated[str, Field(min_length=1, max_length=100)]


class CallCreate(_Payload):
    idempotency_key: Annotated[str, Field(min_length=1, max_length=128)]
    scheduled_at: Annotated[str | None, Field(max_length=40)] = None
    timezone_name: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    fold: Literal[0, 1] | None = None
    recording_override: bool | None = None
    amd_override: bool | None = None

    @model_validator(mode="after")
    def validate_schedule_fields(self) -> "CallCreate":
        if self.scheduled_at is None and (
            self.timezone_name is not None or self.fold is not None
        ):
            raise ValueError("timezone_name and fold require scheduled_at")
        return self


class JobReschedule(_Payload):
    scheduled_at: Annotated[str, Field(min_length=1, max_length=40)]
    timezone_name: Annotated[str, Field(min_length=1, max_length=100)]
    fold: Literal[0, 1] | None = None


VoiceClientFactory = Callable[[], AsyncTwilioVoiceClient]


class _TelephonyMutationRejected(Exception):
    def __init__(self, response: Response) -> None:
        self.response = response


class _PhoneAPIError(HTTPException):
    def __init__(self, *, status_code: int, detail: str, error_code: str) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.error_code = error_code


def _phone_error_response(status_code: int, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": str(exc), "error_code": str(exc.error_code)},
    )


async def _validate_user_telephony_request(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> None:
    """Apply authentication and same-origin CSRF once to the whole owner router."""

    _require_user(current_user)
    if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return
    rejection = validate_mutation_request(request)
    if rejection is not None:
        raise _TelephonyMutationRejected(rejection)


class _UserTelephonyRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        original = super().get_route_handler()

        async def handled(request: Request) -> Response:
            try:
                return await original(request)
            except _TelephonyMutationRejected as exc:
                return exc.response
            except _PhoneAPIError as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail, "error_code": exc.error_code},
                )
            except EmbedError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.code) from None
            except PhoneCountryBlockedError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except (PhoneNumberValidationUnavailable, PhoneUserUnavailableError) as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except (PhoneDataPurgeFailure, PrivateRecordingPathError) as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except TelephonyNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except (
                TelephonyConflictError,
                TelephonyStateError,
                PhoneUserServiceError,
            ) as exc:
                if hasattr(exc, "error_code"):
                    return _phone_error_response(409, exc)
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                if hasattr(exc, "error_code"):
                    return _phone_error_response(400, exc)
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        return handled


def _default_voice_client() -> AsyncTwilioVoiceClient:
    sid = os.getenv("TWILIO_SID", "").strip()
    secret = os.getenv("TWILIO_AUTH", "").strip()
    if not sid or not secret:
        raise PhoneUserUnavailableError("Telephony provider is not configured")
    return AsyncTwilioVoiceClient(sid, secret)


def _require_user(current_user: User | None) -> User:
    if current_user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return current_user


def _contact_json(row: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: row.get(key)
        for key in (
            "id",
            "display_name",
            "timezone_name",
            "binding_id",
            "conversation_id",
            "created_at",
            "updated_at",
        )
    }
    result["masked_e164"] = _mask_e164(row.get("e164"))
    return result


def _number_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "masked_e164": _mask_e164(row.get("e164")),
        "friendly_name": row.get("friendly_name"),
        "inbound_enabled": bool(row.get("inbound_enabled")),
        "is_outbound_default": bool(row.get("is_outbound_default")),
    }


def _binding_json(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = {
        key: row.get(key)
        for key in (
            "id",
            "conversation_id",
            "contact_id",
            "display_name",
            "timezone_name",
            "preferred_number_id",
            "created_at",
            "updated_at",
        )
    }
    result["masked_e164"] = _mask_e164(row.get("e164"))
    result["preferred_number_masked"] = _mask_e164(
        row.get("preferred_number_e164")
    )
    result["allow_inbound"] = bool(row.get("allow_inbound"))
    result["allow_outbound"] = bool(row.get("allow_outbound"))
    return result


def _job_json(row: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: row.get(key)
        for key in (
            "id",
            "conversation_id",
            "binding_id",
            "call_id",
            "scheduled_at_utc",
            "timezone_name",
            "origin",
            "status",
            "last_error_code",
            "last_error_detail",
            "created_at",
            "updated_at",
            "completed_at",
        )
    }
    result["recording_override"] = _nullable_bool(row.get("recording_override"))
    result["amd_override"] = _nullable_bool(row.get("amd_override"))
    return result


def _call_json(row: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: row.get(key)
        for key in (
            "id",
            "job_id",
            "conversation_id",
            "direction",
            "status",
            "answered_by",
            "initiated_at",
            "ringing_at",
            "answered_at",
            "ended_at",
            "duration_seconds",
            "termination_reason",
            "estimated_cost",
            "final_cost",
            "currency",
            "created_at",
            "updated_at",
        )
    }
    result["from_masked"] = _mask_e164(row.get("from_e164"))
    result["to_masked"] = _mask_e164(row.get("to_e164"))
    result["recording_enabled"] = bool(row.get("recording_enabled"))
    result["amd_enabled"] = bool(row.get("amd_enabled"))
    return result


def _history_job_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "id",
            "call_id",
            "conversation_id",
            "conversation_title",
            "prompt_name",
            "scheduled_at_utc",
            "timezone_name",
            "origin",
            "status",
            "created_at",
            "updated_at",
            "completed_at",
        )
    }


def _history_call_json(row: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: row.get(key)
        for key in (
            "id",
            "job_id",
            "conversation_id",
            "conversation_title",
            "prompt_name",
            "direction",
            "status",
            "initiated_at",
            "ringing_at",
            "answered_at",
            "ended_at",
            "duration_seconds",
            "estimated_cost",
            "final_cost",
            "currency",
            "created_at",
            "updated_at",
        )
    }
    result["recording_enabled"] = bool(row.get("recording_enabled"))
    return result


def _purge_json(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = {
        key: row.get(key)
        for key in (
            "id",
            "call_id_snapshot",
            "purge_scope",
            "status",
            "attempt_count",
            "created_at",
            "updated_at",
            "completed_at",
        )
    }
    if row.get("status") == "needs_attention":
        result["error_code"] = "needs_administrator_attention"
        result["error"] = "Deletion requires administrator attention."
    return result


def _nullable_bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _mask_e164(value: Any) -> str | None:
    phone = str(value or "").strip()
    if not phone:
        return None
    return f"••••{phone[-4:]}"


async def _conversation_gate(
    repository: TelephonyRepository,
    *,
    user_id: int,
    conversation_id: int,
    mutate: bool,
) -> dict[str, Any]:
    state = await repository.get_owned_conversation_phone_state(
        owner_user_id=user_id,
        conversation_id=conversation_id,
    )
    if state["is_incognito"]:
        raise _PhoneAPIError(
            status_code=403,
            detail="Phone access is unavailable in incognito conversations",
            error_code="conversation_incognito",
        )
    if mutate and state["locked"]:
        raise _PhoneAPIError(
            status_code=403,
            detail="Conversation is locked",
            error_code="conversation_locked",
        )
    from integrations.applications.phone import phone_scope
    from integrations.embed.models import EmbedError
    async with repository.connection_factory(readonly=True) as connection:
        try:
            await phone_scope(connection, user_id, conversation_id)
        except EmbedError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from None
    return state


async def _visible_phone_rows(repository, user_id, rows):
    from integrations.applications.phone import phone_scope
    from integrations.embed.models import EmbedError
    visible = []
    async with repository.connection_factory(readonly=True) as connection:
        for row in rows:
            if row.get('conversation_id') is not None:
                try:
                    await phone_scope(connection, user_id, row['conversation_id'])
                except EmbedError:
                    continue
            visible.append(row)
    return visible


async def _conversation_reductive_gate(
    repository: TelephonyRepository,
    *,
    user_id: int,
    conversation_id: int,
) -> None:
    """Verify ownership without blocking owner-requested cancel or hangup."""

    await repository.get_owned_conversation_phone_state(
        owner_user_id=user_id,
        conversation_id=conversation_id,
    )



async def hangup_owned_call(repository, voice_client_factory, *, owner_user_id, call_id):
    """Shared durable hangup control after native or application authorization."""
    current = await repository.get_owned_call(owner_user_id=owner_user_id, call_id=call_id)
    await _conversation_reductive_gate(
        repository,
        user_id=owner_user_id,
        conversation_id=int(current["conversation_id"]),
    )
    call, claim = await repository.claim_owned_hangup_request(
        owner_user_id=owner_user_id,
        call_id=call_id,
        retry_unresolved=True,
    )
    if not claim.claimed:
        confirmed_terminal = (
            call["status"] in {
                status.value for status in CALL_TERMINAL_STATUSES
            }
            and call["status"] != PhoneCallStatus.UNRESOLVED.value
        )
        state = (
            "already_terminal"
            if confirmed_terminal
            else "already_requested"
        )
        return {"requested": False, "state": state}
    if claim.attempt_token is None:
        raise TelephonyStateError("Phone hangup attempt is missing its fencing token")

    async def recover_failed_result_persistence() -> str | None:
        """Fence a post-REST persistence failure before it escapes."""

        try:
            unresolved = await repository.mark_owned_hangup_unresolved(
                owner_user_id=owner_user_id,
                call_id=call_id,
                attempt_token=claim.attempt_token,
            )
        except Exception:
            return None
        if unresolved:
            return None
        try:
            attempt_state = await repository.get_owned_hangup_attempt_state(
                owner_user_id=owner_user_id,
                call_id=call_id,
            )
            reconciled_call = await repository.get_owned_call(
                owner_user_id=owner_user_id,
                call_id=call_id,
            )
        except Exception:
            return None
        callback_confirmed = (
            reconciled_call["status"] in {
                status.value for status in CALL_TERMINAL_STATUSES
            }
            and reconciled_call["status"] != PhoneCallStatus.UNRESOLVED.value
        )
        if callback_confirmed and attempt_state == "confirmed":
            return "callback_confirmed"
        if attempt_state == "accepted":
            return "provider_requested"
        return None

    from integrations.telephony.account_routing import client_for_call
    async with repository.connection_factory(readonly=True) as connection:
        client = await client_for_call(call, legacy_factory=voice_client_factory,
            allow_inactive=True, connection=connection)
    response_state = "provider_requested"
    try:
        provider_changed = await client.end_call_once(
            str(call["provider_call_sid"])
        )
    except Exception as exc:
        unresolved = await repository.mark_owned_hangup_unresolved(
            owner_user_id=owner_user_id,
            call_id=call_id,
            attempt_token=claim.attempt_token,
        )
        if not unresolved:
            reconciled = await repository.get_owned_call(
                owner_user_id=owner_user_id,
                call_id=call_id,
            )
            callback_confirmed = (
                reconciled["status"] in {
                    status.value for status in CALL_TERMINAL_STATUSES
                }
                and reconciled["status"] != PhoneCallStatus.UNRESOLVED.value
            )
            if callback_confirmed:
                return {"requested": True, "state": "callback_confirmed"}
        raise HTTPException(
            status_code=502,
            detail="Provider hangup request could not be confirmed",
        ) from exc
    else:
        try:
            if type(provider_changed) is not bool:
                raise TelephonyStateError(
                    "Provider hangup returned an invalid result"
                )
            if provider_changed:
                persisted = await repository.mark_owned_hangup_accepted(
                    owner_user_id=owner_user_id,
                    call_id=call_id,
                    attempt_token=claim.attempt_token,
                )
            else:
                persisted = (
                    await repository.reconcile_owned_hangup_provider_absent(
                        owner_user_id=owner_user_id,
                        call_id=call_id,
                        attempt_token=claim.attempt_token,
                    )
                )
                response_state = "provider_absent_reconciled"
            if not persisted:
                raise TelephonyStateError(
                    "Provider hangup result lost its durable fence"
                )
        except asyncio.CancelledError:
            await recover_failed_result_persistence()
            raise
        except Exception:
            recovered_state = await recover_failed_result_persistence()
            if recovered_state is None:
                raise
            response_state = recovered_state
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            await close()
    return {"requested": True, "state": response_state}


def create_user_telephony_router(
    service: UserPhoneService | None = None,
    *,
    voice_client_factory: VoiceClientFactory = _default_voice_client,
    purge_service: PhoneDataPurgeService | None = None,
) -> APIRouter:
    repository = service.repository if service is not None else TelephonyRepository()
    active_service = service or UserPhoneService(repository)
    active_purge = purge_service or PhoneDataPurgeService(
        PhoneDataPurgeRepository(repository.connection_factory)
    )
    router = APIRouter(
        route_class=_UserTelephonyRoute,
        dependencies=[Depends(_validate_user_telephony_request)],
    )

    @router.get("/api/telephony/me")
    async def get_my_telephony_state(
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        state = await repository.get_profile_phone_state(owner_user_id=user.id)
        binding = await repository.get_active_profile_binding(owner_user_id=user.id)
        return {
            "phone": {
                "configured": bool(state["configured"]),
                "verified": bool(state["verified"]),
                "eligible": bool(state["eligible"]),
                "verification_bypassed": bool(state["verification_bypassed"]),
                "masked": _mask_e164(state.get("e164")),
            },
            "active_binding": (
                {
                    "id": int(binding["id"]),
                    "conversation_id": int(binding["conversation_id"]),
                }
                if binding is not None
                else None
            ),
        }

    @router.get("/api/telephony/contacts")
    async def list_contacts(
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        rows = await repository.list_contacts(owner_user_id=user.id)
        rows = await _visible_phone_rows(repository, user.id, rows)
        return {"contacts": [_contact_json(row) for row in rows]}

    @router.get("/api/telephony/numbers")
    async def list_numbers(
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        _require_user(current_user)
        rows = await repository.list_enabled_numbers()
        return {"numbers": [_number_json(row) for row in rows]}

    @router.post("/api/telephony/contacts", status_code=201)
    async def create_contact(
        payload: ContactCreate,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        phone = active_service.normalize_contact_number(payload.e164)
        row = await repository.create_contact(
            owner_user_id=user.id,
            display_name=payload.display_name,
            e164=phone,
            timezone_name=payload.timezone_name,
            enforce_profile_phone=True,
        )
        return {"contact": _contact_json(row)}

    @router.get("/api/telephony/contacts/{contact_id}")
    async def get_contact(
        contact_id: int,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        row = await repository.get_contact(
            owner_user_id=user.id, contact_id=contact_id
        )
        return {"contact": _contact_json(row)}

    @router.patch("/api/telephony/contacts/{contact_id}")
    async def patch_contact(
        contact_id: int,
        payload: ContactPatch,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        existing = await repository.get_contact(
            owner_user_id=user.id, contact_id=contact_id
        )
        phone = (
            active_service.normalize_contact_number(payload.e164)
            if payload.e164 is not None
            else str(existing["e164"])
        )
        row = await repository.update_contact(
            owner_user_id=user.id,
            contact_id=contact_id,
            display_name=payload.display_name or str(existing["display_name"]),
            e164=phone,
            timezone_name=payload.timezone_name or str(existing["timezone_name"]),
            enforce_profile_phone=True,
        )
        return {"contact": _contact_json(row)}

    @router.delete("/api/telephony/contacts/{contact_id}")
    async def delete_contact(
        contact_id: int,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        await repository.delete_contact(owner_user_id=user.id, contact_id=contact_id)
        return {"deleted": True}

    @router.get("/api/conversations/{conversation_id}/phone-bindings")
    async def get_binding(
        conversation_id: int,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        await _conversation_gate(
            repository, user_id=user.id, conversation_id=conversation_id, mutate=False
        )
        row = await repository.get_active_binding(
            owner_user_id=user.id, conversation_id=conversation_id
        )
        return {"binding": _binding_json(row)}

    @router.post("/api/conversations/{conversation_id}/phone-bindings", status_code=201)
    async def create_binding(
        conversation_id: int,
        payload: BindingCreate,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        await _conversation_gate(
            repository, user_id=user.id, conversation_id=conversation_id, mutate=True
        )
        await repository.assign_binding(
            owner_user_id=user.id,
            conversation_id=conversation_id,
            contact_id=payload.contact_id,
            preferred_number_id=payload.preferred_number_id,
            allow_inbound=payload.allow_inbound,
            allow_outbound=payload.allow_outbound,
            enforce_profile_phone=True,
        )
        row = await repository.get_active_binding(
            owner_user_id=user.id, conversation_id=conversation_id
        )
        return {"binding": _binding_json(row)}

    @router.post(
        "/api/conversations/{conversation_id}/phone-bindings/self",
        status_code=201,
    )
    async def create_self_binding(
        conversation_id: int,
        payload: SelfBindingCreate,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        await _conversation_gate(
            repository, user_id=user.id, conversation_id=conversation_id, mutate=True
        )
        state = await repository.require_profile_phone(owner_user_id=user.id)
        contact = await repository.create_contact(
            owner_user_id=user.id,
            display_name="Profile phone",
            e164=str(state["e164"]),
            timezone_name=payload.timezone_name,
            enforce_profile_phone=True,
        )
        await repository.assign_binding(
            owner_user_id=user.id,
            conversation_id=conversation_id,
            contact_id=int(contact["id"]),
            preferred_number_id=None,
            allow_inbound=True,
            allow_outbound=True,
            enforce_profile_phone=True,
            preserve_existing_direction_flags=True,
        )
        row = await repository.get_active_binding(
            owner_user_id=user.id, conversation_id=conversation_id
        )
        return {"binding": _binding_json(row)}

    @router.patch(
        "/api/conversations/{conversation_id}/phone-bindings/{binding_id}"
    )
    async def patch_binding(
        conversation_id: int,
        binding_id: int,
        payload: BindingPatch,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        await _conversation_gate(
            repository, user_id=user.id, conversation_id=conversation_id, mutate=True
        )
        existing = await repository.get_active_binding(
            owner_user_id=user.id, conversation_id=conversation_id
        )
        if existing is None or int(existing["id"]) != int(binding_id):
            raise TelephonyNotFoundError("Active phone binding not found")
        preferred = existing["preferred_number_id"]
        if "preferred_number_id" in payload.model_fields_set:
            preferred = payload.preferred_number_id
        row = await repository.update_binding(
            owner_user_id=user.id,
            conversation_id=conversation_id,
            binding_id=binding_id,
            preferred_number_id=preferred,
            allow_inbound=(
                bool(existing["allow_inbound"])
                if payload.allow_inbound is None
                else payload.allow_inbound
            ),
            allow_outbound=(
                bool(existing["allow_outbound"])
                if payload.allow_outbound is None
                else payload.allow_outbound
            ),
            enforce_profile_phone=True,
        )
        return {"binding": _binding_json(row)}

    @router.delete(
        "/api/conversations/{conversation_id}/phone-bindings/{binding_id}"
    )
    async def delete_binding(
        conversation_id: int,
        binding_id: int,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        await _conversation_reductive_gate(
            repository, user_id=user.id, conversation_id=conversation_id
        )
        await repository.remove_binding(
            owner_user_id=user.id,
            conversation_id=conversation_id,
            binding_id=binding_id,
        )
        return {"deleted": True}

    @router.post("/api/conversations/{conversation_id}/phone-calls", status_code=201)
    async def create_call(
        conversation_id: int,
        payload: CallCreate,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        await _conversation_gate(
            repository, user_id=user.id, conversation_id=conversation_id, mutate=True
        )
        job, created = await active_service.create_call_job(
            owner_user_id=user.id,
            conversation_id=conversation_id,
            idempotency_key=payload.idempotency_key,
            scheduled_at=payload.scheduled_at,
            timezone_name=payload.timezone_name,
            fold=payload.fold,
            recording_override=payload.recording_override,
            amd_override=payload.amd_override,
        )
        public = await repository.get_owned_job(owner_user_id=user.id, job_id=job["id"])
        return {"created": created, "job": _job_json(public)}

    @router.get("/api/conversations/{conversation_id}/phone-calls")
    async def list_conversation_calls(
        conversation_id: int,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        await _conversation_gate(
            repository, user_id=user.id, conversation_id=conversation_id, mutate=False
        )
        calls = await repository.list_owned_calls(
            owner_user_id=user.id, conversation_id=conversation_id, limit=limit
        )
        jobs = await repository.list_owned_jobs(
            owner_user_id=user.id, conversation_id=conversation_id, limit=limit
        )
        return {
            "calls": [_call_json(row) for row in calls],
            "jobs": [_job_json(row) for row in jobs],
        }

    @router.get("/api/telephony/calls")
    async def list_my_phone_history(
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        calls = await repository.list_owned_calls(
            owner_user_id=user.id, limit=limit
        )
        jobs = await repository.list_owned_jobs(
            owner_user_id=user.id, limit=limit
        )
        calls = await _visible_phone_rows(repository, user.id, calls)
        jobs = await _visible_phone_rows(repository, user.id, jobs)
        return {
            "calls": [_history_call_json(row) for row in calls],
            "jobs": [_history_job_json(row) for row in jobs],
        }

    @router.get("/api/phone-calls/{call_id}")
    async def get_call(
        call_id: str,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        row = await repository.get_owned_call(owner_user_id=user.id, call_id=call_id)
        await _conversation_gate(
            repository,
            user_id=user.id,
            conversation_id=int(row["conversation_id"]),
            mutate=False,
        )
        return {"call": _call_json(row)}

    @router.get("/api/phone-calls/{call_id}/recording", response_model=None)
    async def get_call_recording(
        call_id: str,
        track: Literal["mixed", "participant", "assistant"] = "mixed",
        download: bool = False,
        current_user: User = Depends(get_current_user),
    ) -> Response:
        user = _require_user(current_user)
        recording = await active_purge.repository.get_owned_recording(
            owner_user_id=user.id,
            call_id=call_id,
            track=track,
        )
        await _conversation_gate(
            repository,
            user_id=user.id,
            conversation_id=int(recording["conversation_id"]),
            mutate=False,
        )
        path = resolve_private_recording_path(
            call_id,
            str(recording["path"]),
            root=active_purge.recording_root,
        )
        if not path.is_file() or path.is_symlink():
            raise TelephonyNotFoundError("Phone recording not found")
        media_type = "audio/mpeg" if track == "mixed" else "audio/basic"
        return FileResponse(
            path,
            media_type=media_type,
            filename=(f"phone-call-{call_id}-{track}{path.suffix}" if download else None),
            headers={"Cache-Control": "private, no-store, max-age=0"},
        )

    @router.get("/api/phone-messages/{message_id}/audio", response_model=None)
    async def get_phone_message_audio(
        message_id: int,
        current_user: User = Depends(get_current_user),
    ) -> Response:
        user = _require_user(current_user)
        audio = await active_purge.repository.get_owned_message_audio(
            owner_user_id=user.id,
            message_id=message_id,
        )
        await _conversation_gate(
            repository,
            user_id=user.id,
            conversation_id=int(audio["conversation_id"]),
            mutate=False,
        )
        path = resolve_private_recording_path(
            str(audio["call_id"]),
            str(audio["path"]),
            root=active_purge.recording_root,
        )
        if path.is_symlink():
            raise TelephonyNotFoundError("Phone message audio not found")
        try:
            audio_range = PhoneMessageAudioRange(
                start_byte=int(audio["start_byte"]),
                end_byte=int(audio["end_byte"]),
            )
            body = open_pcmu_range_as_wav_chunks(path, audio_range)
        except (OSError, TypeError, ValueError):
            raise TelephonyNotFoundError("Phone message audio not found")
        return StreamingResponse(
            body,
            media_type="audio/wav",
            headers={
                "Cache-Control": "private, no-store, max-age=0",
                "Content-Length": str(wav_content_length(audio_range)),
                "X-Content-Type-Options": "nosniff",
            },
            background=BackgroundTask(body.close),
        )

    @router.delete("/api/phone-calls/{call_id}", status_code=202)
    async def delete_call(
        call_id: str,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        conversation_id = await active_purge.repository.owned_call_conversation_id(
            owner_user_id=user.id,
            call_id=call_id,
        )
        await _conversation_reductive_gate(
            repository,
            user_id=user.id,
            conversation_id=conversation_id,
        )
        request_result = await active_purge.repository.request_owned_call_purge(
            owner_user_id=user.id,
            call_id=call_id,
        )
        return {
            "accepted": True,
            "created": request_result.created,
            "already_deleted": request_result.already_deleted,
            "purge": _purge_json(request_result.job),
        }

    @router.delete("/api/phone-calls/{call_id}/recording", status_code=202)
    async def delete_call_recording(
        call_id: str,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        conversation_id = await active_purge.repository.owned_call_conversation_id(
            owner_user_id=user.id,
            call_id=call_id,
        )
        await _conversation_reductive_gate(
            repository,
            user_id=user.id,
            conversation_id=conversation_id,
        )
        request_result = await active_purge.repository.request_owned_recording_purge(
            owner_user_id=user.id,
            call_id=call_id,
        )
        return {
            "accepted": True,
            "created": request_result.created,
            "already_deleted": request_result.already_deleted,
            "purge": _purge_json(request_result.job),
        }

    @router.post("/api/phone-calls/{call_id}/hangup")
    async def hangup_call(
        call_id: str,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        return await hangup_owned_call(repository, voice_client_factory,
            owner_user_id=user.id, call_id=call_id)

    @router.post("/api/phone-call-jobs/{job_id}/cancel")
    async def cancel_job(
        job_id: str,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        job = await repository.get_owned_job(owner_user_id=user.id, job_id=job_id)
        await _conversation_reductive_gate(
            repository,
            user_id=user.id,
            conversation_id=int(job["conversation_id"]),
        )
        if job["status"] == "canceled":
            return {"canceled": False, "state": "already_canceled"}
        if job["status"] != "scheduled":
            raise _PhoneAPIError(
                status_code=409,
                detail="Only an unclaimed scheduled call can be canceled",
                error_code="scheduled_call_started",
            )
        canceled = await active_service.outbound_service.cancel_call(
            owner_user_id=user.id, job_id=job_id
        )
        if not canceled:
            raise _PhoneAPIError(
                status_code=409,
                detail="The scheduled call was claimed concurrently",
                error_code="scheduled_call_started",
            )
        return {"canceled": True, "state": "canceled"}

    @router.post("/api/phone-call-jobs/{job_id}/reschedule")
    async def reschedule_job(
        job_id: str,
        payload: JobReschedule,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, Any]:
        user = _require_user(current_user)
        job = await repository.get_owned_job(owner_user_id=user.id, job_id=job_id)
        await _conversation_gate(
            repository,
            user_id=user.id,
            conversation_id=int(job["conversation_id"]),
            mutate=True,
        )
        if job["status"] != "scheduled":
            raise _PhoneAPIError(
                status_code=409,
                detail="Only an unclaimed scheduled call can be rescheduled",
                error_code="scheduled_call_started",
            )
        from integrations.telephony.user_service import parse_local_schedule

        instant = parse_local_schedule(
            payload.scheduled_at, payload.timezone_name, fold=payload.fold
        )
        updated = await active_service.outbound_service.reschedule_call(
            owner_user_id=user.id,
            job_id=job_id,
            scheduled_at=instant,
            timezone_name=payload.timezone_name,
        )
        if not updated:
            raise _PhoneAPIError(
                status_code=409,
                detail="The scheduled call was claimed concurrently",
                error_code="scheduled_call_started",
            )
        public = await repository.get_owned_job(owner_user_id=user.id, job_id=job_id)
        return {"rescheduled": True, "job": _job_json(public)}

    return router


router = create_user_telephony_router()


__all__ = ["create_user_telephony_router", "router"]
