"""Native creator administration for application Twilio connections."""
from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import Field, SecretStr

from auth import get_current_user
from common import get_template_context, templates
from i18n import get_translator
from integrations.embed.models import EmbedError, StrictModel
from integrations.telephony.integrations import get_twilio_integration_service
from request_security import ensure_csrf_token, validate_mutation_request


class ConnectionRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def secured(request):
            try:
                response = await handler(request)
            except EmbedError as exc:
                response = JSONResponse({'detail': get_translator(request).t('application_twilio.error.' + exc.code)}, status_code=exc.status_code)
            except RequestValidationError:
                # Pydantic errors may contain the submitted Auth Token as input.
                response = JSONResponse({'detail': get_translator(request).t('application_twilio.error.twilio_credentials_invalid')}, status_code=400)
            response.headers['Cache-Control'] = 'no-store'
            return response
        return secured


class Change(StrictModel):
    expected_version: int = Field(strict=True, gt=0)


class Credentials(StrictModel):
    account_sid: str = Field(pattern=r'^AC[0-9a-fA-F]{32}$')
    auth_token: SecretStr
    expected_version: int | None = Field(default=None, strict=True, gt=0)


class Activation(Change):
    number_sid: str = Field(pattern=r'^PN[0-9a-fA-F]{32}$')
    confirm_replace: bool = Field(default=False, strict=True)
    phone_auth: str = Field(default='pin', pattern=r'^(pin|linked_caller)$')
    phone_language: str = Field(default='en', pattern=r'^(en|es|ja|fr|pt|it|de)$')
    allow_outbound: bool = Field(default=False, strict=True)


router = APIRouter(prefix='/applications/manage', route_class=ConnectionRoute)


async def _actor(request, user, *, mutation=False):
    from integrations.embed.context import is_embed_request
    if user is None or is_embed_request(request) or getattr(user, 'embed_principal', None) is not None:
        raise EmbedError('application_owner_required', 403)
    if mutation:
        rejected = validate_mutation_request(request)
        if rejected is not None:
            return rejected
    return {'user_id': user.id, 'is_admin': bool(await user.is_admin)}


@router.get('')
async def applications(request: Request, user=Depends(get_current_user)):
    actor = await _actor(request, user)
    context = await get_template_context(request, user)
    context.update(applications=await get_twilio_integration_service().list_apps(**actor))
    return templates.TemplateResponse('applications/manage.html', context)


@router.get('/{app_id}/twilio')
async def connection_page(app_id: str, request: Request, user=Depends(get_current_user)):
    actor = await _actor(request, user)
    state = await get_twilio_integration_service().state(app_id, **actor)
    context = await get_template_context(request, user)
    context.update(twilio_state=state, connection_csrf=ensure_csrf_token(request))
    return templates.TemplateResponse('applications/twilio.html', context)


@router.post('/{app_id}/twilio/credentials')
async def connect(app_id: str, body: Credentials, request: Request, user=Depends(get_current_user)):
    actor = await _actor(request, user, mutation=True)
    if isinstance(actor, JSONResponse):
        return actor
    return await get_twilio_integration_service().connect(app_id, account_sid=body.account_sid,
        auth_token=body.auth_token.get_secret_value(), expected_version=body.expected_version, **actor)


@router.post('/{app_id}/twilio/sync')
async def sync(app_id: str, body: Change, request: Request, user=Depends(get_current_user)):
    actor = await _actor(request, user, mutation=True)
    if isinstance(actor, JSONResponse):
        return actor
    return await get_twilio_integration_service().sync_numbers(app_id, **body.model_dump(), **actor)


@router.post('/{app_id}/twilio/activate')
async def activate(app_id: str, body: Activation, request: Request, user=Depends(get_current_user)):
    actor = await _actor(request, user, mutation=True)
    if isinstance(actor, JSONResponse):
        return actor
    return await get_twilio_integration_service().activate_number(app_id, **body.model_dump(), **actor)


@router.post('/{app_id}/twilio/disconnect')
async def disconnect(app_id: str, body: Change, request: Request, user=Depends(get_current_user)):
    actor = await _actor(request, user, mutation=True)
    if isinstance(actor, JSONResponse):
        return actor
    return await get_twilio_integration_service().disconnect(app_id, **body.model_dump(), **actor)
