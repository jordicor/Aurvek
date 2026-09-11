"""Application operations over the existing authenticated backend transport."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import Field, field_validator

from i18n import LANGUAGES

from integrations.embed.api import PrivateRoute
from integrations.embed.identity import get_embed_store
from integrations.embed.models import AppConfig, DelegatedRequest, ExchangeCodeRequest, StrictModel

from .models import CONTRACT_VERSION, ConversationId, EntryPreferenceRequest, ExternalReference, HandoffRequest, LocalId, OpenConversationRequest
from .service import ApplicationService
from .profile import ConversationalProfileUpdate, ApplicationProfileService
from .channel_destination import ChannelDestination
from .context_source import ApplicationContextSourceService, SourceReference, SourceUpdate
from .authentication import application_backend_client as backend_client, issue_account_session
from .accounts import (
    ApplicationAccountService, LinkAccountRequest, MembershipUpdateRequest,
    PhoneChallengeRequest, PhoneCodeRequest, PhoneUpdateRequest,
    ProfileUpdateRequest, ProvisionAccountRequest,
)


class ApplicationRoute(PrivateRoute):
    contract_version = CONTRACT_VERSION

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def application_handler(request):
            from phone_verification import PhoneVerificationError
            try:
                return await handler(request)
            except PhoneVerificationError as exc:
                return JSONResponse({"contract_version": CONTRACT_VERSION, "error": exc.code},
                                    status_code=exc.status_code,
                                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        return application_handler


class OpenBody(DelegatedRequest):
    conversation: OpenConversationRequest


class BootstrapBody(DelegatedRequest):
    conversation_id: ConversationId
    parent_origin: str = Field(max_length=256)
    embed_origin: str = Field(max_length=256)
    ui_language: str = Field(default="en", max_length=16)
    frame_instance_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("ui_language")
    @classmethod
    def validate_ui_language(cls, value: str) -> str:
        if value not in LANGUAGES:
            raise ValueError("Unsupported UI language")
        return value


class AccountReference(StrictModel):
    external_user_id: ExternalReference


class ProfileBootstrapBody(DelegatedRequest):
    parent_origin: str = Field(max_length=256)
    embed_origin: str = Field(max_length=256)
    ui_language: str = Field(default='en', max_length=16)
    frame_instance_id: str = Field(min_length=16, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')


class ConversationalProfileBody(DelegatedRequest, ConversationalProfileUpdate):
    pass


class PrivateContextBody(DelegatedRequest):
    assistant_id: LocalId


class DestinationBody(DelegatedRequest, ChannelDestination):
    pass


class ContextSourceStateBody(DelegatedRequest, SourceReference):
    pass


class ContextSourceBody(DelegatedRequest, SourceUpdate):
    pass


class LinkBody(DelegatedRequest, LinkAccountRequest):
    pass


class ProfileBody(DelegatedRequest, ProfileUpdateRequest):
    pass


class PhoneChallengeBody(DelegatedRequest, PhoneChallengeRequest):
    pass


class PhoneCodeBody(DelegatedRequest, PhoneCodeRequest):
    pass


class PhoneUpdateBody(DelegatedRequest, PhoneUpdateRequest):
    pass


class HandoffBody(DelegatedRequest, HandoffRequest):
    frame_instance_id: str | None = Field(default=None, min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class StateBody(DelegatedRequest):
    conversation_id: ConversationId


class EntryBody(DelegatedRequest, EntryPreferenceRequest):
    pass


def account_service():
    from clients import async_twilio
    from common import service_sid
    return ApplicationAccountService(get_embed_store(), twilio_client=async_twilio, service_sid=service_sid)


def account_response(result):
    return {"contract_version": CONTRACT_VERSION, **result}


router = APIRouter(prefix="/api/applications/v1", route_class=ApplicationRoute)


@router.post('/context-source/state')
async def context_source_state(body: ContextSourceStateBody, app: AppConfig = Depends(backend_client)):
    store = get_embed_store()
    principal = await store.inspect_identity(app.app_id, body.delegated_credential)
    return account_response(await ApplicationContextSourceService(store).state(principal, body))


@router.post('/context-source/update')
async def context_source_update(body: ContextSourceBody, app: AppConfig = Depends(backend_client)):
    store = get_embed_store()
    principal = await store.inspect_identity(app.app_id, body.delegated_credential)
    value = SourceUpdate.model_validate(body.model_dump(exclude={'delegated_credential'}))
    return account_response(await ApplicationContextSourceService(store).update(principal, value))


@router.post('/channels/select-destination')
async def select_channel_destination(body: DestinationBody, app: AppConfig = Depends(backend_client)):
    from .channel_destination import select_destination
    store = get_embed_store()
    principal = await store.inspect_identity(app.app_id, body.delegated_credential)
    return account_response(await select_destination(store, principal,
        ChannelDestination.model_validate(body.model_dump(exclude={'delegated_credential'}))))


@router.post('/ensure-private-context')
async def ensure_private_context(body: PrivateContextBody, app: AppConfig = Depends(backend_client)):
    from .channels import ApplicationChannelService
    from .profile import ApplicationProfileService
    store = get_embed_store()
    principal = await store.inspect_identity(app.app_id, body.delegated_credential)
    async with store.connection(readonly=True) as connection:
        account = await ApplicationProfileService(store)._account(connection, principal)
        # An alias is not permission to use its underlying prompt.
        from integrations.embed.store import _one
        from .models import AssistantConfig
        row = await _one(connection, 'SELECT config_json FROM APPLICATION_ASSISTANTS WHERE app_id=? AND assistant_id=?', (app.app_id, body.assistant_id))
        if not row or not AssistantConfig.model_validate_json(row['config_json']).enabled:
            from integrations.embed.models import EmbedError
            raise EmbedError('assistant_unavailable', 403)
        config = AssistantConfig.model_validate_json(row['config_json'])
        user = await store._load_user(principal.user_id)
        if store._permission_checker:
            permitted = await store._permission_checker(user, config.prompt_id, connection)
        else:
            from prompts import can_user_access_prompt
            async with connection.cursor() as cursor:
                permitted = await can_user_access_prompt(user, config.prompt_id, cursor)
        if not permitted:
            from integrations.embed.models import EmbedError
            raise EmbedError('membership_inactive', 403)
    context_ref = 'private-assistant:' + body.assistant_id
    context_id = await ApplicationChannelService(store).grant_account_context(app.app_id, account['external_user_id'], context_ref, assistant_ids=[body.assistant_id])
    async with store.connection(readonly=True) as connection:
        await ApplicationService(store)._scope(connection, app.app_id, principal.subject, principal.user_id, context_id, body.assistant_id)
    return {'contract_version': CONTRACT_VERSION, 'context_ref': context_ref, 'context_id': context_id, 'assistant_id': body.assistant_id}


@router.post('/issue-profile-bootstrap')
async def issue_profile_bootstrap(body: ProfileBootstrapBody, app: AppConfig = Depends(backend_client)):
    from .profile_frames import ProfileFrames
    store = get_embed_store()
    principal = await store.inspect_identity(app.app_id, body.delegated_credential)
    return await ProfileFrames(store).issue(principal, body.parent_origin, body.embed_origin, body.ui_language, body.frame_instance_id)


@router.post('/accounts/profile-state')
async def profile_state(body: DelegatedRequest, app: AppConfig = Depends(backend_client)):
    store = get_embed_store()
    principal = await store.inspect_identity(app.app_id, body.delegated_credential)
    return account_response(await ApplicationProfileService(store).state(principal))


@router.post('/accounts/conversational-profile')
async def conversational_profile(body: ConversationalProfileBody, app: AppConfig = Depends(backend_client)):
    store = get_embed_store()
    principal = await store.inspect_identity(app.app_id, body.delegated_credential)
    value = ConversationalProfileUpdate.model_validate(body.model_dump(exclude={'delegated_credential'}))
    return account_response(await ApplicationProfileService(store).update(principal, value))


@router.post("/exchange-code")
async def exchange_code(body: ExchangeCodeRequest, app: AppConfig = Depends(backend_client)):
    result = await get_embed_store().exchange_code(
        app.app_id, body.code, body.code_verifier, body.redirect_uri, application=True)
    return {**result, "contract_version": CONTRACT_VERSION}


@router.post("/inspect-access")
async def inspect_access(body: DelegatedRequest, app: AppConfig = Depends(backend_client)):
    store = get_embed_store()
    identity = await store.inspect_identity(app.app_id, body.delegated_credential)
    # Assistant/conversation capabilities are resolved only for a destination.
    return {"contract_version": CONTRACT_VERSION, "app_id": identity.app_id,
            "subject": identity.subject, "status": "active", "expires_at": identity.expires_at}


@router.post("/end-app-session")
async def end_app_session(body: DelegatedRequest, app: AppConfig = Depends(backend_client)):
    from integrations.embed.activity import close_delegated_session
    result = await close_delegated_session(get_embed_store(), app.app_id, body.delegated_credential)
    return {"contract_version": CONTRACT_VERSION, **result}


@router.post("/open-conversation")
async def open_conversation(body: OpenBody, app: AppConfig = Depends(backend_client)):
    store = get_embed_store()
    identity = await store.inspect_identity(app.app_id, body.delegated_credential)
    return await ApplicationService(store).open_conversation(identity, body.conversation)


@router.post("/issue-embed-bootstrap")
async def issue_embed_bootstrap(body: BootstrapBody, app: AppConfig = Depends(backend_client)):
    store = get_embed_store()
    identity = await store.inspect_identity(app.app_id, body.delegated_credential)
    result = await store.issue_bootstrap(
        identity, None, body.conversation_id, body.parent_origin, body.embed_origin,
        body.ui_language, body.frame_instance_id, binding_kind="application")
    return {**result, "contract_version": CONTRACT_VERSION,
            "frame_contract_version": "aurvek_embed.v1"}


@router.post("/handoff")
async def handoff(body: HandoffBody, app: AppConfig = Depends(backend_client)):
    from .handoff import ApplicationHandoffService
    store = get_embed_store()
    identity = await store.inspect_identity(app.app_id, body.delegated_credential)
    value = HandoffRequest.model_validate(body.model_dump(exclude={"delegated_credential", "frame_instance_id"}))
    return await ApplicationHandoffService(store).handoff(identity, value, frame_instance_id=body.frame_instance_id)


@router.post("/assistant-state")
async def assistant_state(body: StateBody, app: AppConfig = Depends(backend_client)):
    from .handoff import ApplicationHandoffService
    store = get_embed_store()
    identity = await store.inspect_identity(app.app_id, body.delegated_credential)
    return await ApplicationHandoffService(store).state(identity, body.conversation_id)


@router.post("/entry-preference")
async def entry_preference(body: EntryBody, app: AppConfig = Depends(backend_client)):
    from .handoff import ApplicationHandoffService
    store = get_embed_store()
    identity = await store.inspect_identity(app.app_id, body.delegated_credential)
    value = EntryPreferenceRequest.model_validate(body.model_dump(exclude={"delegated_credential"}))
    return await ApplicationHandoffService(store).set_entry_preference(identity, value)


@router.post("/accounts/provision")
async def provision_account(body: ProvisionAccountRequest, app: AppConfig = Depends(backend_client)):
    return account_response(await account_service().provision(app.app_id, body))


@router.post("/accounts/link")
async def link_account(body: LinkBody, app: AppConfig = Depends(backend_client)):
    identity = await get_embed_store().inspect_identity(app.app_id, body.delegated_credential)
    request = LinkAccountRequest.model_validate(body.model_dump(exclude={"delegated_credential"}))
    return account_response(await account_service().link(identity, request))


@router.post("/accounts/status")
async def account_status(body: AccountReference, app: AppConfig = Depends(backend_client)):
    return account_response(await account_service().status(app.app_id, body.external_user_id))


@router.post("/accounts/session")
async def account_session(body: AccountReference, app: AppConfig = Depends(backend_client)):
    return account_response(await issue_account_session(get_embed_store(), app.app_id, body.external_user_id))


@router.post("/accounts/profile")
async def update_account_profile(body: ProfileBody, app: AppConfig = Depends(backend_client)):
    identity = await get_embed_store().inspect_identity(app.app_id, body.delegated_credential)
    request = ProfileUpdateRequest.model_validate(body.model_dump(exclude={"delegated_credential"}))
    return account_response(await account_service().update_profile(identity, request))


@router.post("/accounts/membership")
async def update_account_membership(body: MembershipUpdateRequest, app: AppConfig = Depends(backend_client)):
    accounts = account_service()
    result = await accounts.set_membership(app.app_id, body)
    if not result["active"]:
        from .account_cleanup import cleanup_membership
        result.update(await cleanup_membership(app.app_id, result["subject"]))
    return account_response(result)


@router.post("/accounts/phone/request")
async def request_phone(body: PhoneChallengeBody, request: Request, app: AppConfig = Depends(backend_client)):
    identity = await get_embed_store().inspect_identity(app.app_id, body.delegated_credential)
    value = PhoneChallengeRequest.model_validate(body.model_dump(exclude={"delegated_credential"}))
    return account_response(await account_service().request_phone(identity, value,
        request_ip=request.client.host, recent_auth_time=identity.source_auth_time))


@router.post("/accounts/phone/verify")
async def verify_phone(body: PhoneCodeBody, app: AppConfig = Depends(backend_client)):
    identity = await get_embed_store().inspect_identity(app.app_id, body.delegated_credential)
    value = PhoneCodeRequest.model_validate(body.model_dump(exclude={"delegated_credential"}))
    return account_response(await account_service().verify_phone(identity, value, recent_auth_time=identity.source_auth_time))


@router.post("/accounts/phone/update")
async def update_phone(body: PhoneUpdateBody, app: AppConfig = Depends(backend_client)):
    identity = await get_embed_store().inspect_identity(app.app_id, body.delegated_credential)
    value = PhoneUpdateRequest.model_validate(body.model_dump(exclude={"delegated_credential"}))
    result = await account_service().update_phone(identity, value, recent_auth_time=identity.source_auth_time)
    from .account_cleanup import cleanup_membership
    result.update(await cleanup_membership(identity.app_id, identity.subject))
    return account_response(result)


@router.post("/accounts/initial-phone/request")
async def request_initial_phone(body: PhoneChallengeRequest, request: Request, app: AppConfig = Depends(backend_client)):
    return account_response(await account_service().request_initial_phone(app.app_id, body, request_ip=request.client.host))


@router.post("/accounts/initial-phone/verify")
async def verify_initial_phone(body: PhoneCodeRequest, app: AppConfig = Depends(backend_client)):
    return account_response(await account_service().verify_initial_phone(app.app_id, body))


@router.post("/accounts/initial-phone/update")
async def update_initial_phone(body: PhoneUpdateRequest, app: AppConfig = Depends(backend_client)):
    accounts = account_service()
    result = await accounts.update_initial_phone(app.app_id, body)
    async with accounts.store.connection(readonly=True) as connection:
        binding = await accounts._binding(connection, app.app_id, body.external_user_id)
    from .account_cleanup import cleanup_membership
    result.update(await cleanup_membership(app.app_id, binding["subject"]))
    return account_response(result)


from .channel_api import router as channel_router
router.include_router(channel_router)
from .phone_api import router as phone_router
router.include_router(phone_router)
