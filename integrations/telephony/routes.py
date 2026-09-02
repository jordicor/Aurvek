"""Fail-closed Twilio Voice webhooks and Media Streams endpoint."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import base64
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import os
import struct
from typing import Any

import orjson
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from twilio.twiml.voice_response import VoiceResponse

from auth import get_user_by_id
from database import get_db_connection
from integrations.telephony.callbacks import (
    TwilioCallbackError,
    callback_dedupe_key,
    normalize_call_status_callback,
    normalize_recording_status_callback,
    normalize_stream_status_callback,
    sanitize_twilio_callback,
)
from integrations.telephony.cached_playback import PhoneCachedAudioBackend
from integrations.telephony.billing import (
    PhoneBillingError,
    PhoneBillingExhausted,
    PhoneBillingService,
    PhoneConnectBillingGate,
)
from integrations.telephony.config import load_telephony_config
from integrations.telephony.elevenlabs_realtime import ApiKeyProvider
from integrations.telephony.greetings import CachedPhoneAudio
from integrations.telephony.provider_repository import TelephonyProviderRepository
from integrations.telephony.purge import PhoneDataPurgeRepository
from integrations.telephony.repository import (
    TelephonyConflictError,
    TelephonyInboundUnavailableError,
    TelephonyNotFoundError,
    TelephonyStateError,
)
from integrations.telephony.security import (
    TwilioCanonicalURLConfigurationError,
    TwilioSignatureVerifier,
    canonical_twilio_url,
)
from integrations.telephony.session import (
    GreetingLoader,
    NoticeLoader,
    PhoneMediaSession,
    PhoneMediaSessionContext,
    PhoneMediaSessionError,
)
from integrations.telephony.schemas import CALL_TERMINAL_STATUSES, PhoneCallStatus
from integrations.telephony.snapshot import runtime_kind_from_snapshot
from integrations.telephony.twilio_client import AsyncTwilioVoiceClient
from integrations.telephony.twiml import (
    MAX_RECONNECT_ATTEMPTS,
    MediaStreamCorrelation,
    build_media_stream_twiml,
)
from log_config import logger
from tools.tts_load_balancer import (
    elevenlabs_keys_ready,
    get_elevenlabs_key,
    has_elevenlabs_keys,
)


VOICE_INBOUND_PATH = "/webhooks/twilio/voice/inbound"
VOICE_INBOUND_STATUS_PATH = "/webhooks/twilio/voice/inbound-status"
VOICE_TWIML_PATH = "/webhooks/twilio/voice/twiml/{token}"
VOICE_STATUS_PATH = "/webhooks/twilio/voice/status/{token}"
VOICE_CONNECT_ACTION_PATH = (
    "/webhooks/twilio/voice/connect-action/{token}/{stream_attempt}"
)
VOICE_STREAM_STATUS_PATH = "/webhooks/twilio/voice/stream-status/{token}"
VOICE_AMD_PATH = "/webhooks/twilio/voice/amd/{token}"
VOICE_RECORDING_PATH = "/webhooks/twilio/voice/recording/{token}"
PRIVATE_UNKNOWN_AUDIO_PATH = "/webhooks/twilio/voice/private-audio/{token}"
PRIVATE_INBOUND_UNAVAILABLE_AUDIO_PATH = (
    "/webhooks/twilio/voice/private-inbound-unavailable-audio/{token}"
)
PRIVATE_CALL_AUDIO_PATH = "/webhooks/twilio/voice/call-audio/{token}"
MEDIA_STREAM_PATH = "/ws/twilio/media-stream"


ReadinessCheck = Callable[[], Awaitable[bool]]
SessionFactory = Callable[[PhoneMediaSessionContext], PhoneMediaSession]
ContextReadiness = Callable[[PhoneMediaSessionContext], Awaitable[bool]]
UnknownNoticeLoader = Callable[[], Awaitable[CachedPhoneAudio]]
CallNoticeAssetLoader = Callable[
    [PhoneMediaSessionContext, str], Awaitable[CachedPhoneAudio]
]


def _system_openai_api_key() -> str | None:
    """Resolve the server-owned key without storing it in call state."""

    from common import openai_key

    return openai_key


def _runtime_kind_from_call(call: Mapping[str, Any]) -> str:
    raw_snapshot = call.get("config_snapshot_json")
    snapshot = orjson.loads(raw_snapshot)
    if not isinstance(snapshot, Mapping):
        raise ValueError("Phone call snapshot is invalid")
    return runtime_kind_from_snapshot(snapshot)


@dataclass(slots=True)
class TelephonyProviderRuntime:
    account_sid: str
    auth_token: str
    repository: TelephonyProviderRepository
    readiness_check: ReadinessCheck
    elevenlabs_api_key_provider: ApiKeyProvider | None = get_elevenlabs_key
    openai_api_key_provider: ApiKeyProvider | None = _system_openai_api_key
    notice_loader: NoticeLoader | None = None
    greeting_loader: GreetingLoader | None = None
    context_readiness: ContextReadiness | None = None
    unknown_notice_loader: UnknownNoticeLoader | None = None
    inbound_unavailable_notice_loader: UnknownNoticeLoader | None = None
    call_notice_asset_loader: CallNoticeAssetLoader | None = None
    session_factory: SessionFactory | None = None
    voice_client: AsyncTwilioVoiceClient | None = None
    purge_repository: PhoneDataPurgeRepository | None = None
    billing_service: PhoneBillingService | None = None
    connect_billing_gate: PhoneConnectBillingGate | None = None

    def signature_verifier(self) -> TwilioSignatureVerifier | None:
        if not self.auth_token:
            return None
        return TwilioSignatureVerifier(self.auth_token)

    async def hangup(self, call_sid: str) -> bool:
        if self.voice_client is None:
            raise RuntimeError("Twilio Voice client is unavailable")
        return await self.voice_client.end_call_once(call_sid)

    async def hangup_durable(
        self,
        call: Mapping[str, Any],
        *,
        reason: str,
        target_status: PhoneCallStatus,
        origin: str,
    ) -> None:
        if str(call.get("status")) in {
            PhoneCallStatus.COMPLETED.value,
            PhoneCallStatus.BUSY.value,
            PhoneCallStatus.NO_ANSWER.value,
            PhoneCallStatus.MACHINE.value,
            PhoneCallStatus.FAILED.value,
            PhoneCallStatus.CANCELED.value,
        }:
            return
        call_id = str(call["id"])
        call_sid = str(call["provider_call_sid"])
        claim = await self.repository.record_hangup_requested(
            call_id=call_id,
            provider_call_sid=call_sid,
            reason=reason,
            target_status=target_status,
            origin=origin,
            retry_unresolved=True,
        )
        if not claim.claimed:
            return
        assert claim.attempt_token is not None

        async def record_failure_state() -> str | None:
            try:
                unresolved = await self.repository.mark_hangup_unresolved(
                    call_id=call_id,
                    provider_call_sid=call_sid,
                    reason=claim.reason,
                    attempt_token=claim.attempt_token,
                )
            except Exception:
                unresolved = False
            if unresolved:
                return "unresolved"
            try:
                return await self.repository.get_hangup_attempt_state(
                    call_id=call_id,
                    provider_call_sid=call_sid,
                )
            except Exception:
                return None

        try:
            provider_call_existed = await self.hangup(call_sid)
            if (
                provider_call_existed is not True
                and provider_call_existed is not False
            ):
                raise RuntimeError("Twilio hangup adapter returned an invalid result")
        except Exception:
            state = await record_failure_state()
            if state in {"accepted", "confirmed"}:
                # Another fenced result or a signed callback won while this
                # REST request was in flight.
                return
            raise

        try:
            if provider_call_existed is False:
                reconciled = (
                    await self.repository.reconcile_hangup_provider_absent(
                        call_id=call_id,
                        provider_call_sid=call_sid,
                        attempt_token=claim.attempt_token,
                    )
                )
                if not reconciled:
                    state = await self.repository.get_hangup_attempt_state(
                        call_id=call_id,
                        provider_call_sid=call_sid,
                    )
                    if state not in {"accepted", "confirmed"}:
                        raise TelephonyStateError(
                            "Provider-absent hangup lost its durable fence"
                        )
                return
            accepted = await self.repository.mark_hangup_accepted(
                call_id=call_id,
                provider_call_sid=call_sid,
                attempt_token=claim.attempt_token,
            )
            if not accepted:
                state = await self.repository.get_hangup_attempt_state(
                    call_id=call_id,
                    provider_call_sid=call_sid,
                )
                if state not in {"accepted", "confirmed"}:
                    raise TelephonyStateError(
                        "Provider hangup acceptance lost its durable fence"
                    )
        except Exception:
            state = await record_failure_state()
            if state in {"accepted", "confirmed"}:
                return
            raise

    async def call_ready(self, call: Mapping[str, Any]) -> bool:
        if self.context_readiness is None:
            return False
        try:
            context = PhoneMediaSessionContext.from_call(
                call,
                account_sid=self.account_sid,
                stream_attempt=int(call.get("reconnect_count", 0)),
            )
            runtime_kind = runtime_kind_from_snapshot(context.call_snapshot)
            stt_provider = "openai" if runtime_kind == "openai_realtime" else "elevenlabs"
            if runtime_kind == "openai_realtime":
                if not callable(self.openai_api_key_provider):
                    return False
                if (
                    self.openai_api_key_provider is _system_openai_api_key
                    and not str(_system_openai_api_key() or "").strip()
                ):
                    return False
            else:
                if not callable(self.elevenlabs_api_key_provider):
                    return False
                if self.elevenlabs_api_key_provider is get_elevenlabs_key:
                    if not has_elevenlabs_keys() or not elevenlabs_keys_ready():
                        return False
            if (
                self.billing_service is not None
                and not await self.billing_service.rate_available(
                    call=call,
                    provider=stt_provider,
                    component_type="stt",
                )
            ):
                return False
            return bool(await self.context_readiness(context))
        except Exception:
            return False

    def build_session(self, context: PhoneMediaSessionContext) -> PhoneMediaSession:
        if self.session_factory is not None:
            return self.session_factory(context)
        if runtime_kind_from_snapshot(context.call_snapshot) == "openai_realtime":
            if self.openai_api_key_provider is None:
                raise PhoneMediaSessionError(
                    "OpenAI Realtime is not configured"
                )
            return PhoneMediaSession.with_openai_realtime_key_provider(
                context,
                openai_api_key_provider=self.openai_api_key_provider,
                repository=self.repository,
                current_user_loader=get_user_by_id,
                hangup_call=self.hangup,
                notice_loader=self.notice_loader,
                greeting_loader=self.greeting_loader,
                billing_service=self.billing_service,
            )
        if self.elevenlabs_api_key_provider is None:
            raise PhoneMediaSessionError(
                "ElevenLabs Scribe realtime STT is not configured"
            )
        return PhoneMediaSession.with_elevenlabs_key_provider(
            context,
            elevenlabs_api_key_provider=self.elevenlabs_api_key_provider,
            repository=self.repository,
            current_user_loader=get_user_by_id,
            hangup_call=self.hangup,
            notice_loader=self.notice_loader,
            greeting_loader=self.greeting_loader,
            billing_service=self.billing_service,
        )


class _NoopDeletedCallbackRepository:
    """Test-adapter fallback for legacy fake provider repositories."""

    async def is_deleted_provider_call(self, _provider_call_sid: str) -> bool:
        return False

    async def is_deleted_callback(self, _token: str, _provider_call_sid: str) -> bool:
        return False

    async def capture_late_recording(self, **_kwargs: Any) -> bool:
        return False


async def _default_readiness() -> bool:
    try:
        config = await load_telephony_config()
    except Exception:
        return False
    if not config.enabled:
        return False
    # Fail closed until administration has resolved the canonical global voice
    # and activated at least one technical-notice cache revision.  Prompt-level
    # greeting readiness is checked when that cache service is wired in.
    try:
        async with get_db_connection(readonly=True) as conn:
            default_cursor = await conn.execute(
                "SELECT COUNT(*) FROM VOICES WHERE is_default=1 AND COALESCE(deprecated,0)=0"
            )
            notice_cursor = await conn.execute(
                """
                SELECT COUNT(*) FROM PHONE_PROMPT_AUDIO_CACHE
                WHERE prompt_id IS NULL AND asset_kind='technical_notice'
                  AND status='ready' AND source_mp3_path IS NOT NULL
                  AND pcmu_path IS NOT NULL
                """
            )
            default_count = int((await default_cursor.fetchone())[0])
            notice_count = int((await notice_cursor.fetchone())[0])
    except Exception:
        return False
    return default_count == 1 and notice_count > 0


def _default_runtime() -> TelephonyProviderRuntime:
    account_sid = os.getenv("TWILIO_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH", "").strip()
    voice_client = (
        AsyncTwilioVoiceClient(account_sid, auth_token)
        if account_sid and auth_token
        else None
    )
    repository = TelephonyProviderRepository()
    cache_backend = PhoneCachedAudioBackend(repository)
    billing_service = PhoneBillingService()

    async def ready() -> bool:
        return await _default_readiness() and await cache_backend.global_ready()

    return TelephonyProviderRuntime(
        account_sid=account_sid,
        auth_token=auth_token,
        repository=repository,
        readiness_check=ready,
        elevenlabs_api_key_provider=get_elevenlabs_key,
        openai_api_key_provider=_system_openai_api_key,
        notice_loader=cache_backend.load_notice,
        greeting_loader=cache_backend.load_greeting,
        context_readiness=cache_backend.probe_context,
        unknown_notice_loader=cache_backend.load_unknown_notice,
        inbound_unavailable_notice_loader=(
            cache_backend.load_inbound_unavailable_notice
        ),
        call_notice_asset_loader=cache_backend.load_notice_asset,
        voice_client=voice_client,
        billing_service=billing_service,
        connect_billing_gate=PhoneConnectBillingGate(service=billing_service),
    )


def create_telephony_provider_router(
    runtime: TelephonyProviderRuntime | None = None,
) -> APIRouter:
    active = runtime or _default_runtime()
    if active.purge_repository is not None:
        purge_repository = active.purge_repository
    elif isinstance(active.repository, TelephonyProviderRepository):
        purge_repository = PhoneDataPurgeRepository(
            active.repository.connection_factory
        )
    else:
        purge_repository = _NoopDeletedCallbackRepository()
    router = APIRouter()

    async def runtime_ready() -> bool:
        if (
            not active.account_sid
            or active.signature_verifier() is None
            or (
                active.elevenlabs_api_key_provider is None
                and active.openai_api_key_provider is None
            )
            or active.voice_client is None
            or active.notice_loader is None
            or active.greeting_loader is None
            or active.context_readiness is None
            or active.unknown_notice_loader is None
            or active.call_notice_asset_loader is None
            or active.billing_service is None
            or active.connect_billing_gate is None
        ):
            return False
        try:
            return bool(await active.readiness_check())
        except Exception:
            return False

    async def require_ready() -> None:
        if not await runtime_ready():
            raise HTTPException(status_code=503, detail="Telephony is not ready")

    def callback_billing_service() -> PhoneBillingService:
        if active.billing_service is None:
            raise HTTPException(
                status_code=503, detail="Telephone billing is unavailable"
            )
        return active.billing_service

    async def balance_exhausted_response(
        call: Mapping[str, Any], *, stream_attempt: int
    ) -> Response:
        try:
            claim = await active.repository.record_hangup_requested(
                call_id=str(call["id"]),
                provider_call_sid=str(call["provider_call_sid"]),
                reason="balance_exhausted",
                target_status=PhoneCallStatus.COMPLETED,
                origin="billing_gate",
                retry_unresolved=False,
            )
            if (
                claim.reason != "balance_exhausted"
                or claim.target_status != PhoneCallStatus.COMPLETED.value
            ):
                return _hangup_twiml()
            context = PhoneMediaSessionContext.from_call(
                call,
                account_sid=active.account_sid,
                stream_attempt=int(stream_attempt),
            )
            assert active.call_notice_asset_loader is not None
            asset = await active.call_notice_asset_loader(
                context, "balance_exhausted"
            )
            audio_token = _build_private_call_audio_token(
                purpose="balance_exhausted",
                dispatch_token=str(call["dispatch_token"]),
                call_id=str(call["id"]),
                cache_id=int(asset.cache_id),
                audio_revision=int(asset.audio_revision),
                stream_attempt=int(stream_attempt),
                secret=active.auth_token,
            )
            audio_url = canonical_twilio_url(
                PRIVATE_CALL_AUDIO_PATH.format(token=audio_token)
            )
        except Exception:
            logger.exception("Telephone balance notice could not be prepared")
            return _hangup_twiml(status_code=503)
        return _notice_hangup_twiml(audio_url)

    async def billed_media_response(
        call: Mapping[str, Any],
        response: Response,
        *,
        stream_attempt: int,
        include_pstn: bool,
    ) -> Response:
        gate = active.connect_billing_gate
        if gate is None:
            raise HTTPException(
                status_code=503, detail="Telephone billing is unavailable"
            )
        try:
            stt_provider = (
                "openai"
                if _runtime_kind_from_call(call) == "openai_realtime"
                else "elevenlabs"
            )
            # This is deliberately the last await on the successful path.  The
            # TwiML was built first, so no fallible work remains after provider
            # coverage crosses its durable boundary.
            await gate.prepare(
                call_id=str(call["id"]),
                stream_attempt=int(stream_attempt),
                call_elapsed_seconds=0.0,
                include_pstn=bool(include_pstn),
                include_stt=True,
                stt_provider=stt_provider,
            )
        except PhoneBillingExhausted:
            return await balance_exhausted_response(
                call, stream_attempt=stream_attempt
            )
        except PhoneBillingError:
            raise HTTPException(
                status_code=503, detail="Telephone billing could not be prepared"
            ) from None
        return response

    async def billed_reconnect_response(
        call: Mapping[str, Any], completed_attempt: int
    ) -> Response:
        next_attempt = int(completed_attempt) + 1
        response = _reconnect_twiml_response(call, completed_attempt)
        return await billed_media_response(
            call,
            response,
            stream_attempt=next_attempt,
            include_pstn=False,
        )

    async def signed_form(
        request: Request, *, runtime_required: bool = True
    ) -> tuple[Any, dict[str, Any]]:
        if runtime_required:
            await require_ready()
        elif not active.account_sid or active.signature_verifier() is None:
            raise HTTPException(status_code=503, detail="Telephony is not ready")
        form = await request.form()
        signature = request.headers.get("X-Twilio-Signature", "")
        verifier = active.signature_verifier()
        assert verifier is not None
        try:
            valid = verifier.validate_http(
                path=request.url.path,
                signature=signature,
                form_params=form,
                raw_query_string=request.scope.get("query_string", b""),
            )
        except (TwilioCanonicalURLConfigurationError, ValueError):
            logger.exception("Twilio canonical HTTP URL is not configured safely")
            raise HTTPException(status_code=503, detail="Telephony URL is not ready") from None
        if not valid:
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")
        return form, {str(key): form[key] for key in form}

    @router.post(VOICE_INBOUND_PATH)
    async def inbound_voice(request: Request) -> Response:
        _, params = await signed_form(request)
        call_sid = str(params.get("CallSid") or "")
        caller = str(params.get("From") or "")
        called = str(params.get("To") or "")
        if await purge_repository.is_deleted_provider_call(call_sid):
            return _hangup_twiml()
        try:
            call, _ = await active.repository.create_inbound_call(
                provider_call_sid=call_sid,
                caller_e164=caller,
                called_e164=called,
            )
        except TelephonyInboundUnavailableError:
            if active.inbound_unavailable_notice_loader is None:
                return _hangup_twiml(status_code=503)
            try:
                asset = await active.inbound_unavailable_notice_loader()
                audio_token = _build_private_audio_token(
                    asset.cache_id, secret=active.auth_token
                )
                audio_url = canonical_twilio_url(
                    PRIVATE_INBOUND_UNAVAILABLE_AUDIO_PATH.format(
                        token=audio_token
                    )
                )
            except Exception:
                return _hangup_twiml(status_code=503)
            return _notice_hangup_twiml(audio_url)
        except TelephonyNotFoundError:
            assert active.unknown_notice_loader is not None
            try:
                asset = await active.unknown_notice_loader()
                audio_token = _build_private_audio_token(
                    asset.cache_id, secret=active.auth_token
                )
                audio_url = canonical_twilio_url(
                    PRIVATE_UNKNOWN_AUDIO_PATH.format(token=audio_token)
                )
            except Exception:
                return _hangup_twiml(status_code=503)
            return _unknown_caller_twiml(audio_url)
        except (TelephonyConflictError, ValueError):
            return _hangup_twiml()
        if not await active.call_ready(call):
            try:
                await active.hangup_durable(
                    call,
                    reason="phone_audio_cache_unavailable",
                    target_status=PhoneCallStatus.FAILED,
                    origin="readiness",
                )
            except Exception:
                pass
            return _hangup_twiml(status_code=503)
        attempt = int(call.get("reconnect_count", 0))
        response = _media_twiml_response(call)
        return await billed_media_response(
            call,
            response,
            stream_attempt=attempt,
            include_pstn=True,
        )

    @router.post(VOICE_TWIML_PATH)
    async def outbound_twiml(token: str, request: Request) -> Response:
        _, params = await signed_form(request)
        try:
            call = await active.repository.reconcile_outbound_twiml(
                dispatch_token=token,
                provider_call_sid=str(params.get("CallSid") or ""),
            )
        except (TelephonyNotFoundError, TelephonyConflictError, ValueError):
            return _hangup_twiml()
        if not await active.call_ready(call):
            try:
                await active.hangup_durable(
                    call,
                    reason="phone_audio_cache_unavailable",
                    target_status=PhoneCallStatus.FAILED,
                    origin="readiness",
                )
            except Exception:
                pass
            return _hangup_twiml(status_code=503)
        attempt = int(call.get("reconnect_count", 0))
        response = _media_twiml_response(call)
        return await billed_media_response(
            call,
            response,
            stream_attempt=attempt,
            include_pstn=False,
        )

    @router.get(PRIVATE_UNKNOWN_AUDIO_PATH)
    async def private_unknown_audio(token: str, request: Request) -> Response:
        del request
        await require_ready()
        cache_id = _parse_private_audio_token(token, secret=active.auth_token)
        assert active.unknown_notice_loader is not None
        asset = await active.unknown_notice_loader()
        if int(asset.cache_id) != cache_id:
            raise HTTPException(status_code=404, detail="Phone audio is unavailable")
        try:
            content = _pcmu_wave(asset.read_pcmu())
        except Exception:
            raise HTTPException(
                status_code=404, detail="Phone audio is unavailable"
            ) from None
        if not content:
            raise HTTPException(status_code=404, detail="Phone audio is unavailable")
        return Response(
            content=content,
            media_type="audio/wav",
            headers={"Cache-Control": "private, no-store, max-age=0"},
        )

    @router.get(PRIVATE_INBOUND_UNAVAILABLE_AUDIO_PATH)
    async def private_inbound_unavailable_audio(
        token: str, request: Request
    ) -> Response:
        del request
        await require_ready()
        cache_id = _parse_private_audio_token(token, secret=active.auth_token)
        if active.inbound_unavailable_notice_loader is None:
            raise HTTPException(status_code=503, detail="Telephony is not ready")
        asset = await active.inbound_unavailable_notice_loader()
        if int(asset.cache_id) != cache_id:
            raise HTTPException(status_code=404, detail="Phone audio is unavailable")
        try:
            content = _pcmu_wave(asset.read_pcmu())
        except Exception:
            raise HTTPException(
                status_code=404, detail="Phone audio is unavailable"
            ) from None
        if not content:
            raise HTTPException(status_code=404, detail="Phone audio is unavailable")
        return Response(
            content=content,
            media_type="audio/wav",
            headers={"Cache-Control": "private, no-store, max-age=0"},
        )

    @router.get(PRIVATE_CALL_AUDIO_PATH)
    async def private_call_audio(token: str, request: Request) -> Response:
        del request
        if not active.auth_token or active.call_notice_asset_loader is None:
            raise HTTPException(status_code=503, detail="Telephony is not ready")
        scope = _parse_private_call_audio_token(token, secret=active.auth_token)
        call = await active.repository.get_call_by_dispatch_token(
            scope["dispatch_token"]
        )
        if (
            call is None
            or str(call.get("id")) != scope["call_id"]
            or str(call.get("status")) not in {
                "queued",
                "initiated",
                "ringing",
                "in_progress",
                "dispatch_unknown",
            }
            or int(call.get("reconnect_count", -1)) != scope["stream_attempt"]
        ):
            raise HTTPException(status_code=404, detail="Phone audio is unavailable")
        if scope["purpose"] == "reconnect_failed":
            outcome = await active.repository.get_stream_attempt_result(
                call_id=scope["call_id"],
                stream_attempt=scope["stream_attempt"],
            )
            if (
                scope["stream_attempt"] != MAX_RECONNECT_ATTEMPTS
                or not _is_exhausted_websocket_failure(outcome)
            ):
                raise HTTPException(
                    status_code=404, detail="Phone audio is unavailable"
                )
        elif scope["purpose"] == "balance_exhausted":
            hangup = await active.repository.get_hangup_attempt(
                call_id=scope["call_id"],
                provider_call_sid=str(call["provider_call_sid"]),
            )
            if (
                hangup is None
                or str(hangup.get("reason")) != "balance_exhausted"
                or str(hangup.get("target_status"))
                != PhoneCallStatus.COMPLETED.value
                or str(hangup.get("origin")) != "billing_gate"
                or str(hangup.get("state"))
                not in {"in_flight", "accepted", "confirmed"}
            ):
                raise HTTPException(
                    status_code=404, detail="Phone audio is unavailable"
                )
        else:  # The parser is fail-closed; keep the route equally explicit.
            raise HTTPException(status_code=404, detail="Phone audio is unavailable")
        try:
            context = PhoneMediaSessionContext.from_call(
                call,
                account_sid=active.account_sid,
                stream_attempt=scope["stream_attempt"],
            )
            assert active.call_notice_asset_loader is not None
            asset = await active.call_notice_asset_loader(
                context, scope["purpose"]
            )
            if (
                int(asset.cache_id) != scope["cache_id"]
                or int(asset.audio_revision) != scope["audio_revision"]
                or int(context.call_snapshot["audio_revision"])
                != scope["audio_revision"]
                or asset.technical_notice_key != scope["purpose"]
            ):
                raise ValueError("private call audio scope changed")
            content = _pcmu_wave(asset.read_pcmu())
        except Exception:
            raise HTTPException(
                status_code=404, detail="Phone audio is unavailable"
            ) from None
        if not content:
            raise HTTPException(status_code=404, detail="Phone audio is unavailable")
        return Response(
            content=content,
            media_type="audio/wav",
            headers={"Cache-Control": "private, no-store, max-age=0"},
        )

    @router.post(VOICE_STATUS_PATH)
    async def voice_status(token: str, request: Request) -> Response:
        _, params = await signed_form(request, runtime_required=False)
        try:
            event = normalize_call_status_callback(params)
            if await purge_repository.is_deleted_callback(token, event.call_sid):
                return Response(status_code=204)
            call, _ = await active.repository.record_call_status(
                event, dispatch_token=token
            )
            if (
                call is not None
                and event.status in CALL_TERMINAL_STATUSES
                and event.duration_seconds is not None
            ):
                await callback_billing_service().reconcile_signed_twilio_duration(
                    call_id=str(call["id"]),
                    component_type="pstn",
                    duration_seconds=event.duration_seconds,
                    external_usage_id=f"twilio:call-duration:{event.call_sid}",
                )
        except TwilioCallbackError:
            raise HTTPException(status_code=422, detail="Invalid call status callback") from None
        except TelephonyConflictError:
            raise HTTPException(status_code=409, detail="Call callback conflict") from None
        except PhoneBillingError:
            raise HTTPException(
                status_code=503, detail="Telephone billing reconciliation failed"
            ) from None
        return Response(status_code=204)

    @router.post(VOICE_INBOUND_STATUS_PATH)
    async def inbound_voice_status(request: Request) -> Response:
        """Reconcile inbound terminal state by signed provider CallSid.

        Incoming calls have no Aurvek dispatch token before Twilio invokes the
        number webhook.  Their number-level StatusCallback is therefore a
        static signed route and correlates only through the provider CallSid.
        Unknown or deleted calls intentionally receive the same empty success.
        """

        _, params = await signed_form(request, runtime_required=False)
        try:
            event = normalize_call_status_callback(params)
            if await purge_repository.is_deleted_provider_call(event.call_sid):
                return Response(status_code=204)
            call, _ = await active.repository.record_call_status(
                event,
                expected_direction="inbound",
            )
            if (
                call is not None
                and event.status in CALL_TERMINAL_STATUSES
                and event.duration_seconds is not None
            ):
                await callback_billing_service().reconcile_signed_twilio_duration(
                    call_id=str(call["id"]),
                    component_type="pstn",
                    duration_seconds=event.duration_seconds,
                    external_usage_id=f"twilio:call-duration:{event.call_sid}",
                )
        except TwilioCallbackError:
            raise HTTPException(
                status_code=422, detail="Invalid call status callback"
            ) from None
        except PhoneBillingError:
            raise HTTPException(
                status_code=503,
                detail="Telephone billing reconciliation failed",
            ) from None
        return Response(status_code=204)

    @router.post(VOICE_STREAM_STATUS_PATH)
    async def stream_status(token: str, request: Request) -> Response:
        _, params = await signed_form(request, runtime_required=False)
        try:
            event = normalize_stream_status_callback(params)
            if await purge_repository.is_deleted_callback(token, event.call_sid):
                return Response(status_code=204)
            await _require_token_call(active.repository, token, event.call_sid)
            await active.repository.record_stream_status(event)
        except TwilioCallbackError:
            raise HTTPException(status_code=422, detail="Invalid stream callback") from None
        return Response(status_code=204)

    @router.post(VOICE_RECORDING_PATH)
    async def recording_status(token: str, request: Request) -> Response:
        _, params = await signed_form(request, runtime_required=False)
        try:
            event = normalize_recording_status_callback(params)
            call, _ = await active.repository.record_recording_status(
                event,
                dispatch_token=token,
            )
            if call is None:
                return Response(status_code=204)
            if event.status == "completed" and event.duration_seconds is not None:
                await callback_billing_service().reconcile_signed_twilio_duration(
                    call_id=str(call["id"]),
                    component_type="recording",
                    duration_seconds=event.duration_seconds,
                    external_usage_id=(
                        f"twilio:recording-duration:{event.recording_sid}"
                    ),
                )
        except TwilioCallbackError:
            raise HTTPException(status_code=422, detail="Invalid recording callback") from None
        except PhoneBillingError:
            raise HTTPException(
                status_code=503, detail="Telephone billing reconciliation failed"
            ) from None
        return Response(status_code=204)

    @router.post(VOICE_AMD_PATH)
    async def amd_status(token: str, request: Request) -> Response:
        _, params = await signed_form(request, runtime_required=False)
        answered_by = str(params.get("AnsweredBy") or "").strip().lower()
        sanitized = sanitize_twilio_callback(params)
        dedupe = callback_dedupe_key("amd", sanitized)
        call_sid = str(params.get("CallSid") or "")
        if await purge_repository.is_deleted_callback(token, call_sid):
            return Response(status_code=204)
        call = await active.repository.get_call_by_dispatch_token(token)
        if call is None:
            if await purge_repository.is_deleted_callback(token, call_sid):
                return Response(status_code=204)
            raise HTTPException(status_code=404, detail="Phone call not found")
        if str(call.get("provider_call_sid")) != call_sid:
            raise HTTPException(status_code=404, detail="Phone call not found")
        # Keep the signed AMD payload visible even for human/unknown outcomes.
        await active.repository.append_provider_event(
            call_id=str(call["id"]),
            provider_call_sid=str(call["provider_call_sid"]),
            dedupe_key=dedupe,
            event_type="amd",
            payload=sanitized,
        )
        billing_error = False
        try:
            await callback_billing_service().reconcile_signed_twilio_amd(
                call_id=str(call["id"]),
                external_usage_id=dedupe,
            )
        except Exception:
            logger.exception("Telephone AMD billing reconciliation failed")
            billing_error = True
        if answered_by == "fax" or answered_by.startswith("machine"):
            # Duplicate signed callbacks are allowed to retry an unresolved
            # idempotent hangup; the durable request/confirmation events keep
            # it one logical operation.
            try:
                await active.hangup_durable(
                    call,
                    reason="machine",
                    target_status=PhoneCallStatus.MACHINE,
                    origin="amd",
                )
            except Exception:
                raise HTTPException(
                    status_code=503,
                    detail="Provider hangup needs reconciliation",
                ) from None
        if billing_error:
            raise HTTPException(
                status_code=503, detail="Telephone billing reconciliation failed"
            )
        return Response(status_code=204)

    @router.post(VOICE_CONNECT_ACTION_PATH)
    async def connect_action(
        token: str, stream_attempt: int, request: Request
    ) -> Response:
        _, params = await signed_form(request, runtime_required=False)
        if not 0 <= stream_attempt <= MAX_RECONNECT_ATTEMPTS:
            return _hangup_twiml()
        call = await active.repository.get_call_by_dispatch_token(token)
        if call is None or str(call.get("provider_call_sid")) != str(params.get("CallSid")):
            return _hangup_twiml()
        sanitized = sanitize_twilio_callback(params)
        current_attempt = int(call.get("reconnect_count", -1))
        if stream_attempt > current_attempt:
            return _hangup_twiml()
        if stream_attempt < current_attempt:
            # The action URL is scoped to the stream which just ended.  Twilio
            # executes one TwiML document at a time, so every delivery of that
            # action must replay the exact durable continuation originally
            # chosen for it -- never instructions for a later stream.
            return await billed_reconnect_response(call, stream_attempt)
        outcome = await active.repository.get_stream_attempt_result(
            call_id=str(call["id"]),
            stream_attempt=stream_attempt,
        )
        if stream_attempt == MAX_RECONNECT_ATTEMPTS:
            if current_attempt != MAX_RECONNECT_ATTEMPTS:
                return _hangup_twiml()
            if not _is_exhausted_websocket_failure(outcome):
                return _hangup_twiml()
            try:
                # Fence the terminal intent before returning TwiML, but do not
                # issue a REST hangup: Twilio must first play the cached asset.
                # The later signed terminal callback confirms this common
                # latch and maps completion to the intended failed outcome.
                claim = await active.repository.record_hangup_requested(
                    call_id=str(call["id"]),
                    provider_call_sid=str(call["provider_call_sid"]),
                    reason="reconnect_failed",
                    target_status=PhoneCallStatus.FAILED,
                    origin="connect_action",
                    retry_unresolved=False,
                )
                if (
                    claim.reason != "reconnect_failed"
                    or claim.target_status != PhoneCallStatus.FAILED.value
                ):
                    return _hangup_twiml()
                context = PhoneMediaSessionContext.from_call(
                    call,
                    account_sid=active.account_sid,
                    stream_attempt=stream_attempt,
                )
                assert active.call_notice_asset_loader is not None
                asset = await active.call_notice_asset_loader(
                    context, "reconnect_failed"
                )
                audio_token = _build_private_call_audio_token(
                    purpose="reconnect_failed",
                    dispatch_token=str(call["dispatch_token"]),
                    call_id=str(call["id"]),
                    cache_id=int(asset.cache_id),
                    audio_revision=int(asset.audio_revision),
                    stream_attempt=stream_attempt,
                    secret=active.auth_token,
                )
                audio_url = canonical_twilio_url(
                    PRIVATE_CALL_AUDIO_PATH.format(token=audio_token)
                )
            except Exception:
                return _hangup_twiml(status_code=503)
            return _notice_hangup_twiml(audio_url)
        await require_ready()
        if outcome is None:
            return _hangup_twiml(status_code=503)
        reconnect = await active.repository.prepare_reconnect(
            provider_call_sid=str(call["provider_call_sid"]),
            stream_attempt=stream_attempt,
            dedupe_key=callback_dedupe_key("connect_action", sanitized),
            payload=sanitized,
        )
        if reconnect is None:
            # A differently keyed concurrent callback may have won the CAS
            # after our initial read.  Re-read the authoritative call and
            # replay that attempt's identical continuation when it did.
            latest = await active.repository.get_call_by_dispatch_token(token)
            if (
                latest is None
                or str(latest.get("provider_call_sid"))
                != str(params.get("CallSid"))
                or int(latest.get("reconnect_count", -1)) < stream_attempt + 1
            ):
                return _hangup_twiml()
            return await billed_reconnect_response(latest, stream_attempt)
        return await billed_reconnect_response(reconnect, stream_attempt)

    @router.websocket(MEDIA_STREAM_PATH)
    async def media_stream(websocket: WebSocket) -> None:
        try:
            if not await runtime_ready():
                await websocket.close(code=1013)
                return
            verifier = active.signature_verifier()
            assert verifier is not None
            valid = verifier.validate_websocket(
                path=websocket.url.path,
                signature=websocket.headers.get("x-twilio-signature", ""),
                raw_query_string=websocket.scope.get("query_string", b""),
            )
            if not valid:
                await websocket.close(code=1008)
                return
        except (TwilioCanonicalURLConfigurationError, ValueError):
            await websocket.close(code=1013)
            return

        await websocket.accept()
        try:
            connected_raw = await websocket.receive_text()
            start_raw = await websocket.receive_text()
            peek = _peek_start(start_raw)
            call = await active.repository.get_call_by_dispatch_token(
                peek["correlation_token"]
            )
            if (
                call is None
                or call.get("provider_call_sid") != peek["call_sid"]
                or int(call.get("reconnect_count", -1)) != peek["stream_attempt"]
            ):
                await websocket.close(code=1008)
                return
            if not await active.call_ready(call):
                await websocket.close(code=1013)
                return
            context = PhoneMediaSessionContext.from_call(
                call,
                account_sid=active.account_sid,
                stream_attempt=peek["stream_attempt"],
            )
            session = active.build_session(context)
            result = await session.run(
                websocket,
                initial_messages=(connected_raw, start_raw),
            )
            if not result.attempt_result_published:
                await active.repository.record_stream_attempt_result(
                    call_id=context.call_id,
                    provider_call_sid=context.provider_call_sid,
                    stream_attempt=context.stream_attempt,
                    reason=result.reason,
                    reconnectable=result.reconnectable,
                    internal_failure=result.internal_failure,
                )
        except (PhoneMediaSessionError, TelephonyStateError, TelephonyConflictError, ValueError):
            logger.exception("Twilio Media Streams session closed after a safe failure")
            await websocket.close(code=1011)
        except Exception:
            logger.exception("Unexpected Twilio Media Streams session failure")
            await websocket.close(code=1011)

    return router


async def _require_token_call(
    repository: TelephonyProviderRepository,
    token: str,
    call_sid: str,
) -> Mapping[str, Any]:
    call = await repository.get_call_by_dispatch_token(token)
    if call is None or str(call.get("provider_call_sid")) != str(call_sid):
        raise HTTPException(status_code=404, detail="Phone call not found")
    return call


def _media_twiml_response(call: Mapping[str, Any]) -> Response:
    token = str(call["dispatch_token"])
    attempt = int(call.get("reconnect_count", 0))
    if not 0 <= attempt <= MAX_RECONNECT_ATTEMPTS:
        return _hangup_twiml(status_code=409)
    xml = build_media_stream_twiml(
        stream_url=canonical_twilio_url(MEDIA_STREAM_PATH, websocket=True),
        connect_action_url=canonical_twilio_url(
            VOICE_CONNECT_ACTION_PATH.format(
                token=token, stream_attempt=attempt
            )
        ),
        stream_status_callback_url=canonical_twilio_url(
            VOICE_STREAM_STATUS_PATH.format(token=token)
        ),
        correlation=MediaStreamCorrelation(token, attempt),
    )
    return Response(content=xml, media_type="application/xml")


def _reconnect_twiml_response(
    call: Mapping[str, Any], completed_attempt: int
) -> Response:
    """Replay the immutable continuation selected for one Connect action.

    Twilio's action webhook replaces the completed ``<Connect>`` document.
    There is no provider-supported no-op TwiML for an already accepted action,
    so retries and concurrent deliveries for the same signed attempt must see
    byte-identical instructions for ``attempt + 1``.
    """

    continuation = dict(call)
    continuation["reconnect_count"] = int(completed_attempt) + 1
    return _media_twiml_response(continuation)


def _unknown_caller_twiml(notice_url: str | None) -> Response:
    response = VoiceResponse()
    if notice_url:
        response.play(notice_url)
    response.hangup()
    return Response(content=str(response), media_type="application/xml")


def _notice_hangup_twiml(notice_url: str) -> Response:
    response = VoiceResponse()
    response.play(notice_url)
    response.hangup()
    return Response(content=str(response), media_type="application/xml")


def _is_exhausted_websocket_failure(outcome: Mapping[str, Any] | None) -> bool:
    return bool(
        outcome
        and outcome.get("internal_failure") is True
        and outcome.get("reconnectable") is False
        and str(outcome.get("reason")) == "websocket_closed"
        and int(outcome.get("stream_attempt", -1)) == MAX_RECONNECT_ATTEMPTS
    )


def _build_private_audio_token(
    cache_id: int,
    *,
    secret: str,
    now: datetime | None = None,
) -> str:
    if not secret:
        raise ValueError("private audio secret is unavailable")
    expires = int(
        ((now or datetime.now(UTC)) + timedelta(minutes=5)).timestamp()
    )
    payload = f"{int(cache_id)}:{expires}".encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + b":" + signature).decode("ascii").rstrip("=")


def _parse_private_audio_token(
    token: str,
    *,
    secret: str,
    now: datetime | None = None,
) -> int:
    if not secret or len(str(token)) > 256:
        raise HTTPException(status_code=404, detail="Phone audio is unavailable")
    encoded = str(token)
    if not encoded or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in encoded
    ):
        raise HTTPException(status_code=404, detail="Phone audio is unavailable")
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        canonical = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        if not hmac.compare_digest(encoded, canonical):
            raise ValueError("private audio token is not canonical")
        cache_raw, expires_raw, supplied = raw.split(b":", 2)
        payload = cache_raw + b":" + expires_raw
        expected = hmac.new(
            secret.encode("utf-8"), payload, hashlib.sha256
        ).digest()
        cache_id = int(cache_raw)
        expires = int(expires_raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=404, detail="Phone audio is unavailable"
        ) from None
    observed = int((now or datetime.now(UTC)).timestamp())
    if cache_id <= 0 or expires < observed or expires > observed + 5 * 60 + 5:
        raise HTTPException(status_code=404, detail="Phone audio is unavailable")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=404, detail="Phone audio is unavailable")
    return cache_id


def _build_private_call_audio_token(
    *,
    purpose: str,
    dispatch_token: str,
    call_id: str,
    cache_id: int,
    audio_revision: int,
    stream_attempt: int,
    secret: str,
    now: datetime | None = None,
) -> str:
    if not secret:
        raise ValueError("private audio secret is unavailable")
    expires = int(((now or datetime.now(UTC)) + timedelta(minutes=5)).timestamp())
    normalized_purpose = str(purpose)
    if normalized_purpose not in {"balance_exhausted", "reconnect_failed"}:
        raise ValueError("private call audio purpose is invalid")
    if not 0 <= int(stream_attempt) <= MAX_RECONNECT_ATTEMPTS:
        raise ValueError("private call audio stream attempt is invalid")
    payload = orjson.dumps(
        [
            normalized_purpose,
            str(dispatch_token),
            str(call_id),
            int(cache_id),
            int(audio_revision),
            int(stream_attempt),
            expires,
        ]
    )
    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")


def _parse_private_call_audio_token(
    token: str,
    *,
    secret: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    encoded = str(token)
    if (
        not secret
        or not encoded
        or len(encoded) > 768
        or any(
            character
            not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in encoded
        )
    ):
        raise HTTPException(status_code=404, detail="Phone audio is unavailable")
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        canonical = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        if not hmac.compare_digest(encoded, canonical) or len(raw) <= 32:
            raise ValueError("private call audio token is not canonical")
        payload, supplied = raw[:-32], raw[-32:]
        expected = hmac.new(
            secret.encode("utf-8"), payload, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(supplied, expected):
            raise ValueError("private call audio token signature is invalid")
        values = orjson.loads(payload)
        if not isinstance(values, list) or len(values) != 7:
            raise ValueError("private call audio token payload is invalid")
        (
            purpose,
            dispatch_token,
            call_id,
            cache_raw,
            revision_raw,
            attempt_raw,
            expires_raw,
        ) = values
        if (
            not isinstance(purpose, str)
            or purpose not in {"balance_exhausted", "reconnect_failed"}
            or not isinstance(dispatch_token, str)
            or not isinstance(call_id, str)
        ):
            raise ValueError("private call audio scope is invalid")
        cache_id = int(cache_raw)
        audio_revision = int(revision_raw)
        stream_attempt = int(attempt_raw)
        expires = int(expires_raw)
    except (TypeError, ValueError, orjson.JSONDecodeError):
        raise HTTPException(
            status_code=404, detail="Phone audio is unavailable"
        ) from None
    observed = int((now or datetime.now(UTC)).timestamp())
    if (
        not dispatch_token
        or not call_id
        or cache_id <= 0
        or audio_revision <= 0
        or not 0 <= stream_attempt <= MAX_RECONNECT_ATTEMPTS
        or expires < observed
        or expires > observed + 5 * 60 + 5
    ):
        raise HTTPException(status_code=404, detail="Phone audio is unavailable")
    return {
        "purpose": purpose,
        "dispatch_token": dispatch_token,
        "call_id": call_id,
        "cache_id": cache_id,
        "audio_revision": audio_revision,
        "stream_attempt": stream_attempt,
    }


def _pcmu_wave(audio: bytes) -> bytes:
    """Wrap private headerless PCMU in a Twilio-playable G.711 WAV."""

    raw = bytes(audio)
    if not raw:
        raise ValueError("PCMU audio is empty")
    padding = b"\x00" if len(raw) % 2 else b""
    fmt = struct.pack("<HHIIHH", 7, 1, 8_000, 8_000, 1, 8)
    fact = struct.pack("<I", len(raw))
    body = (
        b"WAVE"
        + b"fmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"fact"
        + struct.pack("<I", len(fact))
        + fact
        + b"data"
        + struct.pack("<I", len(raw))
        + raw
        + padding
    )
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _hangup_twiml(*, status_code: int = 200) -> Response:
    response = VoiceResponse()
    response.hangup()
    return Response(
        content=str(response),
        media_type="application/xml",
        status_code=status_code,
    )


def _peek_start(raw: str | bytes) -> dict[str, Any]:
    """Read only lookup keys; MediaStreamParser revalidates the whole message."""

    if isinstance(raw, str):
        encoded = raw.encode("utf-8")
    else:
        encoded = bytes(raw)
    if not encoded or len(encoded) > 262_144:
        raise ValueError("invalid Media Streams start message")
    try:
        payload = orjson.loads(encoded)
        start = payload["start"]
        custom = start["customParameters"]
        call_sid = start["callSid"]
        token = custom["correlation_token"]
        attempt_raw = custom["stream_attempt"]
    except (orjson.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("invalid Media Streams start message") from exc
    if not isinstance(call_sid, str) or not isinstance(token, str):
        raise ValueError("invalid Media Streams start lookup fields")
    if attempt_raw not in {"0", "1", "2"}:
        raise ValueError("invalid Media Streams start attempt")
    return {
        "call_sid": call_sid,
        "correlation_token": token,
        "stream_attempt": int(attempt_raw),
    }


_provider_runtime = _default_runtime()
router = create_telephony_provider_router(_provider_runtime)


def get_provider_runtime() -> TelephonyProviderRuntime:
    """Return the process runtime captured by the registered provider routes."""

    return _provider_runtime


__all__ = [
    "MEDIA_STREAM_PATH",
    "TelephonyProviderRuntime",
    "create_telephony_provider_router",
    "get_provider_runtime",
    "router",
]
