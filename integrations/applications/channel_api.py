"""Common channel controls over the authenticated application backend API."""
from fastapi import APIRouter, Depends
from pydantic import Field

from integrations.embed.api import PrivateRoute
from integrations.embed.models import AppConfig, StrictModel
from .authentication import application_backend_client
from .channel_models import LinkInvitationRequest
from .channels import get_application_channel_service
from .models import CONTRACT_VERSION, ExternalReference, LocalId, OperationId


class ChannelRoute(PrivateRoute):
    contract_version = CONTRACT_VERSION


class AccountReference(StrictModel):
    external_user_id: ExternalReference


class LinkChange(AccountReference):
    link_id: str = Field(min_length=16, max_length=128)
    expected_version: int = Field(strict=True, gt=0)


class PinChange(LinkChange):
    access_pin: str = Field(pattern=r"^[0-9]{6,10}$")


class ContextGrant(AccountReference):
    context_ref: ExternalReference | None = None
    assistant_ids: list[LocalId] | None = Field(default=None, max_length=64)


class ContextSelection(AccountReference):
    challenge_id: str = Field(min_length=16, max_length=128)
    choice_id: str = Field(pattern=r"^[0-9]{1,3}$")
    operation_id: OperationId


router = APIRouter(prefix="/channels", route_class=ChannelRoute)


@router.post("/invitations")
async def issue_invitation(body: LinkInvitationRequest, app: AppConfig = Depends(application_backend_client)):
    return {"contract_version": CONTRACT_VERSION, **await get_application_channel_service().issue_invitation(app.app_id, body)}


@router.post("/links")
async def links(body: AccountReference, app: AppConfig = Depends(application_backend_client)):
    return {"contract_version": CONTRACT_VERSION, "links": await get_application_channel_service().link_state(app.app_id, body.external_user_id)}


@router.post("/unlink")
async def unlink(body: LinkChange, app: AppConfig = Depends(application_backend_client)):
    return {"contract_version": CONTRACT_VERSION, **await get_application_channel_service().unlink(
        app.app_id, body.external_user_id, body.link_id, expected_version=body.expected_version)}


@router.post("/access-pin")
async def access_pin(body: PinChange, app: AppConfig = Depends(application_backend_client)):
    return {"contract_version": CONTRACT_VERSION, **await get_application_channel_service().reset_access_pin(
        app.app_id, body.external_user_id, body.link_id, body.access_pin, expected_version=body.expected_version)}


@router.post("/context")
async def grant_context(body: ContextGrant, app: AppConfig = Depends(application_backend_client)):
    context_id = await get_application_channel_service().grant_account_context(
        app.app_id, body.external_user_id, body.context_ref, assistant_ids=body.assistant_ids)
    return {"contract_version": CONTRACT_VERSION, "context_id": context_id}


@router.post("/options")
async def options(body: AccountReference, app: AppConfig = Depends(application_backend_client)):
    return {"contract_version": CONTRACT_VERSION, **await get_application_channel_service().account_options(app.app_id, body.external_user_id)}


@router.post("/context-selection")
async def select_context(body: ContextSelection, app: AppConfig = Depends(application_backend_client)):
    result = await get_application_channel_service().select_context_for_account(app.app_id, body.external_user_id,
        body.challenge_id, body.choice_id, body.operation_id)
    return {"contract_version": CONTRACT_VERSION, "status": result.status,
            "conversation_id": result.admission.scope.conversation_id if result.admission else None}
