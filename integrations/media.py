import asyncio
import io

import aiohttp
import httpx
import requests
from fastapi import HTTPException
from pydub import AudioSegment

from billing.usage_reservations import (
    BillingReservationError,
    InsufficientBalanceError,
    mark_fixed_usage_provider_succeeded,
    refund_fixed_usage,
    reserve_fixed_usage,
    settle_fixed_usage,
)
from clients import deepgram, stt_engine, stt_fallback_enabled
from common import Cost, load_service_costs
from log_config import logger
from tools.tts_load_balancer import get_elevenlabs_key


DEFAULT_STT_LANGUAGE = "es"
_STT_PROVIDER_DEFAULT_COSTS = {
    "deepgram": 0.0059,
    "elevenlabs": 0.005,
}
_STT_PROVIDER_COST_KEYS = {
    "deepgram": "STT_COST_PER_MINUTE_DEEPGRAM",
    "elevenlabs": "STT_COST_PER_MINUTE_ELEVENLABS",
}
_STT_PROVIDER_SERVICE_KEYS = {
    "deepgram": "STT_SERVICE_ID_DEEPGRAM",
    "elevenlabs": "STT_SERVICE_ID_ELEVENLABS",
}


class BillableSTTProviderError(RuntimeError):
    """The provider completed billable work but no transcript was usable."""


async def get_stt_billing_config(
    engine: str,
    *,
    configured_engine: str,
) -> tuple[float, int | None]:
    """Return the rate and service that belong to one STT provider attempt."""
    normalized_engine = str(engine or "").strip().lower()
    normalized_configured_engine = str(configured_engine or "").strip().lower()
    if normalized_engine not in _STT_PROVIDER_DEFAULT_COSTS:
        raise BillingReservationError(
            f"Unsupported speech-to-text engine: {engine}"
        )

    if normalized_engine == normalized_configured_engine:
        rate = Cost.STT_COST_PER_MINUTE
        service_id = Cost.STT_SERVICE_ID
    else:
        costs = await load_service_costs()
        rate = costs.get(
            _STT_PROVIDER_COST_KEYS[normalized_engine],
            _STT_PROVIDER_DEFAULT_COSTS[normalized_engine],
        )
        service_id = costs.get(_STT_PROVIDER_SERVICE_KEYS[normalized_engine])

    try:
        normalized_rate = float(rate)
        normalized_service_id = (
            int(service_id) if service_id is not None else None
        )
    except (TypeError, ValueError) as exc:
        raise BillingReservationError(
            f"Billing is not configured for STT provider {normalized_engine}"
        ) from exc
    if normalized_rate <= 0:
        raise BillingReservationError(
            f"Billing is not configured for STT provider {normalized_engine}"
        )
    return normalized_rate, normalized_service_id


async def refund_stt_attempt(
    reservation_id: str,
    *,
    context: str,
    suppress_billing_error: bool = False,
) -> None:
    """Release a reservation when an STT provider produced no billable work."""
    try:
        refunded = await refund_fixed_usage(reservation_id)
        if not refunded:
            raise BillingReservationError(
                "Speech-to-text reservation could not be refunded"
            )
    except BillingReservationError as exc:
        logger.exception("Could not refund %s STT reservation", context)
        if not suppress_billing_error:
            raise HTTPException(
                status_code=503,
                detail="Speech-to-text billing is temporarily unavailable",
            ) from exc


async def settle_stt_attempt(
    reservation_id: str,
    *,
    context: str,
) -> None:
    """Settle provider work without refunding a possibly billable result."""
    try:
        marked = await mark_fixed_usage_provider_succeeded(
            reservation_id,
            purpose="stt",
        )
        if not marked:
            raise BillingReservationError(
                "Speech-to-text reservation is no longer active"
            )
        settled = await settle_fixed_usage(reservation_id)
    except Exception as exc:
        logger.exception("Could not settle %s STT reservation", context)
        raise HTTPException(
            status_code=503,
            detail="Speech-to-text billing is temporarily unavailable",
        ) from exc
    if not settled:
        raise HTTPException(
            status_code=503,
            detail="Speech-to-text billing is temporarily unavailable",
        )


async def finalize_failed_stt_attempt(
    reservation_id: str,
    error: BaseException,
    *,
    context: str,
) -> None:
    """Settle a billable provider response, otherwise release its reservation."""
    if isinstance(error, BillableSTTProviderError):
        await settle_stt_attempt(
            reservation_id,
            context=context,
        )
        return
    await refund_stt_attempt(
        reservation_id,
        context=context,
        suppress_billing_error=not isinstance(error, Exception),
    )


async def reserve_stt_attempt(
    *,
    user_id: int,
    engine: str,
    configured_engine: str,
    duration_min: float,
    context: str,
) -> str:
    """Reserve the configured cost for one concrete STT provider attempt."""
    try:
        rate, service_id = await get_stt_billing_config(
            engine,
            configured_engine=configured_engine,
        )
        return await reserve_fixed_usage(
            user_id=user_id,
            purpose="stt",
            amount=rate * duration_min,
            service_id=service_id,
            usage_quantity=duration_min,
        )
    except InsufficientBalanceError:
        raise HTTPException(status_code=402, detail="Insufficient balance")
    except BillingReservationError as exc:
        logger.error("Could not reserve %s STT usage: %s", context, exc)
        raise HTTPException(
            status_code=503,
            detail="Speech-to-text billing is temporarily unavailable",
        ) from exc


async def transcribe_with_elevenlabs(audio_content: bytes = None, media_url: str = None):
    try:
        eleven_key = get_elevenlabs_key()
        if not eleven_key:
            raise Exception("No ElevenLabs API key available")

        url = "https://api.elevenlabs.io/v1/speech-to-text"
        headers = {"xi-api-key": eleven_key}

        async with aiohttp.ClientSession() as session:
            if media_url:
                async with session.get(media_url) as response:
                    if response.status != 200:
                        raise Exception(f"Error downloading audio from URL: {response.status}")
                    audio_content = await response.read()

            if not audio_content:
                raise Exception("No audio content available")

            form_data = aiohttp.FormData()
            form_data.add_field("model_id", "scribe_v2")
            form_data.add_field("file", audio_content, filename="audio.webm", content_type="audio/webm")

            async with session.post(url, headers=headers, data=form_data) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"ElevenLabs API error: {response.status} - {error_text}")
                try:
                    result = await response.json()
                    return result.get("text", "")
                except Exception as exc:
                    raise BillableSTTProviderError(
                        "ElevenLabs returned an unusable STT response"
                    ) from exc
    except Exception as e:
        logger.error(f"Error transcribing with ElevenLabs: {str(e)}")
        raise


async def transcribe_with_deepgram(
    audio_content: bytes = None,
    media_url: str = None,
    user_agent: str = None,
):
    try:
        options = {
            "model": "nova-2",
            "smart_format": True,
            "punctuate": True,
            "language": DEFAULT_STT_LANGUAGE,
        }
        if media_url:
            response = await deepgram.listen.asyncprerecorded.v("1").transcribe_url(
                {"url": media_url},
                options,
            )
        elif audio_content:
            response = await deepgram.listen.asyncprerecorded.v("1").transcribe_file(
                {"buffer": audio_content},
                options,
                timeout=httpx.Timeout(300.0, connect=10.0),
            )
        else:
            raise Exception("No audio content or media URL provided")

        try:
            result = response.to_dict()
            if not result:
                raise ValueError("No response from Deepgram")
            return result["results"]["channels"][0]["alternatives"][0][
                "transcript"
            ]
        except Exception as exc:
            raise BillableSTTProviderError(
                "Deepgram returned an unusable STT response"
            ) from exc
    except Exception as e:
        logger.error(f"Error transcribing with Deepgram: {str(e)}")
        raise


async def transcribe_external_audio(
    *,
    user_id: int,
    media_url: str = None,
    audio_content: bytes = None,
    user_agent: str = None,
):
    if media_url:
        response = await asyncio.to_thread(requests.get, media_url, timeout=(5, 30))
        if response.status_code != 200:
            raise HTTPException(status_code=400, detail="Error downloading audio file")
        audio_content = response.content

    if not audio_content:
        raise HTTPException(status_code=400, detail="No audio")

    audio_segment = await asyncio.to_thread(
        AudioSegment.from_file,
        io.BytesIO(audio_content),
    )
    audio_duration = audio_segment.duration_seconds
    if audio_duration <= 0:
        raise HTTPException(status_code=400, detail="No audio")

    duration_min = audio_duration / 60
    primary_engine = (
        "elevenlabs" if str(stt_engine).lower() == "elevenlabs" else "deepgram"
    )

    async def transcribe_with_engine(engine: str):
        if engine == "elevenlabs":
            return await transcribe_with_elevenlabs(audio_content=audio_content)
        return await transcribe_with_deepgram(
            audio_content=audio_content,
            user_agent=user_agent,
        )

    primary_reservation_id = await reserve_stt_attempt(
        user_id=user_id,
        engine=primary_engine,
        configured_engine=primary_engine,
        duration_min=duration_min,
        context=f"external {primary_engine}",
    )
    try:
        prompt = await transcribe_with_engine(primary_engine)
    except BaseException as primary_error:
        await finalize_failed_stt_attempt(
            primary_reservation_id,
            primary_error,
            context=f"failed external {primary_engine}",
        )
        if not isinstance(primary_error, Exception) or not stt_fallback_enabled:
            raise

        fallback_engine = (
            "deepgram" if primary_engine == "elevenlabs" else "elevenlabs"
        )
        fallback_reservation_id = await reserve_stt_attempt(
            user_id=user_id,
            engine=fallback_engine,
            configured_engine=primary_engine,
            duration_min=duration_min,
            context=f"external fallback {fallback_engine}",
        )
        try:
            prompt = await transcribe_with_engine(fallback_engine)
        except BaseException as fallback_error:
            await finalize_failed_stt_attempt(
                fallback_reservation_id,
                fallback_error,
                context=f"failed external fallback {fallback_engine}",
            )
            if not isinstance(fallback_error, Exception):
                raise
            logger.error(
                "Both external STT engines failed. Primary: %s, Fallback: %s",
                primary_error,
                fallback_error,
            )
            raise primary_error from fallback_error

        await settle_stt_attempt(
            fallback_reservation_id,
            context=f"external fallback {fallback_engine}",
        )
        return prompt

    await settle_stt_attempt(
        primary_reservation_id,
        context=f"external {primary_engine}",
    )
    return prompt


async def transcribe_external_request(request, audio=None, user_id: int = None, media_url: str = None):
    user_agent = request.headers.get("user-agent") if request else None
    audio_content = await audio.read() if audio else None
    return await transcribe_external_audio(
        user_id=user_id,
        media_url=media_url,
        audio_content=audio_content,
        user_agent=user_agent,
    )
