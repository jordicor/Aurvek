from fastapi import APIRouter

from integrations import platform_routes
from integrations.elevenlabs import admin_routes as elevenlabs_admin_routes
from integrations.elevenlabs import routes as elevenlabs_routes
from integrations.elevenlabs import sdk_routes as elevenlabs_sdk_routes
from integrations.devices import admin_routes as devices_admin_routes
from integrations.devices import routes as devices_routes
from integrations.telegram import admin_routes as telegram_admin_routes
from integrations.telegram import routes as telegram_routes
from integrations.telephony import api_routes as telephony_api_routes
from integrations.telephony import admin_routes as telephony_admin_routes
from integrations.telephony import prompt_settings_routes as telephony_prompt_settings_routes
from integrations.telephony import routes as telephony_provider_routes
from integrations.telephony.prompt_audio_backend import (
    register_production_phone_audio_backends,
)
from integrations.whatsapp import admin_routes as whatsapp_admin_routes
from integrations.whatsapp import routes as whatsapp_routes
from integrations.messaging_voice_notes import routes as messaging_voice_routes


router = APIRouter()
router.include_router(platform_routes.router)
router.include_router(elevenlabs_sdk_routes.router)
router.include_router(elevenlabs_admin_routes.router)
router.include_router(elevenlabs_routes.router)
router.include_router(devices_admin_routes.router)
router.include_router(devices_routes.router)
router.include_router(whatsapp_admin_routes.router)
router.include_router(whatsapp_routes.router)
router.include_router(telegram_admin_routes.router)
router.include_router(telegram_routes.router)
router.include_router(messaging_voice_routes.router)
router.include_router(telephony_api_routes.router)
router.include_router(telephony_admin_routes.router)
router.include_router(telephony_prompt_settings_routes.router)
router.include_router(telephony_provider_routes.router)


def _register_telephony_audio_backends() -> None:
    register_production_phone_audio_backends()


router.add_event_handler("startup", _register_telephony_audio_backends)
