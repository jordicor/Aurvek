"""Thin authenticated backend API; paid call handling remains native."""
from fastapi import APIRouter, Depends
from pydantic import Field

from integrations.embed.models import AppConfig, EmbedError
from integrations.embed.identity import get_embed_store
from integrations.telephony.api_routes import CallCreate, JobReschedule
from integrations.telephony.repository import TelephonyNotFoundError, TelephonyConflictError, TelephonyStateError
from integrations.telephony.user_service import PhoneUserServiceError, PhoneUserUnavailableError, PhoneCountryBlockedError
from .authentication import application_backend_client
from .channel_api import AccountReference, ChannelRoute
from .models import CONTRACT_VERSION
from .phone_controls import ApplicationPhoneControls


class PhoneReference(AccountReference):
    conversation_id: int = Field(strict=True, gt=0)


class PhoneBinding(PhoneReference):
    link_id: str = Field(min_length=16, max_length=128)


class PhoneCreate(PhoneReference, CallCreate):
    pass


class PhoneJob(PhoneReference):
    job_id: str = Field(min_length=1, max_length=128)


class PhoneReschedule(PhoneJob, JobReschedule):
    pass


class PhoneHangup(PhoneReference):
    call_id: str = Field(min_length=1, max_length=128)


def controls():
    return ApplicationPhoneControls(get_embed_store())


async def invoke(method, app, body):
    try:
        value = await getattr(controls(), method)(app.app_id, **body.model_dump())
        return {'contract_version': CONTRACT_VERSION, **value}
    except TelephonyNotFoundError:
        raise EmbedError('not_found', 404) from None
    except PhoneCountryBlockedError:
        raise EmbedError('application_phone_country_unavailable', 403) from None
    except PhoneUserUnavailableError:
        raise EmbedError('application_phone_unavailable', 503) from None
    except (TelephonyConflictError, TelephonyStateError, PhoneUserServiceError):
        raise EmbedError('application_phone_conflict', 409) from None
    except ValueError:
        raise EmbedError('invalid_request', 400) from None


router = APIRouter(prefix='/channels/phone', route_class=ChannelRoute)


@router.post('/binding')
async def bind(body: PhoneBinding, app: AppConfig = Depends(application_backend_client)):
    return await invoke('bind', app, body)


@router.post('/state')
async def state(body: PhoneReference, app: AppConfig = Depends(application_backend_client)):
    return await invoke('state', app, body)


@router.post('/calls')
async def create(body: PhoneCreate, app: AppConfig = Depends(application_backend_client)):
    return await invoke('create', app, body)


@router.post('/cancel')
async def cancel(body: PhoneJob, app: AppConfig = Depends(application_backend_client)):
    return await invoke('cancel', app, body)


@router.post('/reschedule')
async def reschedule(body: PhoneReschedule, app: AppConfig = Depends(application_backend_client)):
    return await invoke('reschedule', app, body)


@router.post('/hangup')
async def hangup(body: PhoneHangup, app: AppConfig = Depends(application_backend_client)):
    return await invoke('hangup', app, body)
