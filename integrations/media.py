import asyncio
from dataclasses import dataclass
import math
from pathlib import Path
import tempfile

import aiohttp
import httpx
from fastapi import HTTPException
from pydub.utils import mediainfo_json

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
from database import get_db_connection
from log_config import logger
from tools.tts_load_balancer import get_elevenlabs_key
from user_languages import load_user_preferred_languages
from integrations.applications.messaging import current_messaging_state, check_current_messaging
from integrations.embed.models import EmbedError


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


@dataclass(frozen=True, slots=True)
class ExternalTranscriptionResult:
    text: str
    provider: str
    model: str
    duration_seconds: float


async def _load_primary_stt_language(user_id: int | None) -> str | None:
    """Return the user's primary language, or None for provider autodetection."""
    if user_id is None:
        return None
    async with get_db_connection(readonly=True) as conn:
        state = current_messaging_state()
        if state is not None:
            from integrations.applications.profile import load_profile
            preferred_languages = (await load_profile(conn, state.admission.scope.app_id, state.admission.scope.subject)).preferred_languages
        else:
            preferred_languages = await load_user_preferred_languages(conn, user_id)
    return preferred_languages[0] if preferred_languages else None


def _probe_audio_duration_seconds(audio_content: bytes) -> float:
    """Probe compressed audio metadata without decoding it to PCM in memory."""
    with tempfile.TemporaryDirectory(prefix="aurvek-stt-") as temp_dir:
        audio_path = Path(temp_dir) / "audio.bin"
        audio_path.write_bytes(audio_content)
        info = mediainfo_json(str(audio_path))

    candidates = [
        (info.get("format") or {}).get("duration"),
        *(
            stream.get("duration")
            for stream in info.get("streams", [])
            if isinstance(stream, dict) and stream.get("codec_type") == "audio"
        ),
    ]
    for candidate in candidates:
        try:
            duration = float(candidate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            return duration
    raise ValueError("Audio duration could not be determined")


async def download_external_media(
    media_url: str,
    *,
    max_bytes: int | None = None,
    total_timeout: float = 300.0,
) -> bytes:
    """Download a caller-validated URL with one deadline and an optional byte cap."""
    if max_bytes is not None and max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if not math.isfinite(total_timeout) or total_timeout <= 0:
        raise ValueError("total_timeout must be finite and positive")

    headers = {"Accept-Encoding": "identity"} if max_bytes is not None else None
    timeout = httpx.Timeout(total_timeout, connect=min(10.0, total_timeout))
    chunk_size = min(65536, max_bytes + 1) if max_bytes is not None else 65536
    async with asyncio.timeout(total_timeout):
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            request = client.build_request("GET", media_url)
            for redirect_count in range(21):
                response = await client.send(request, stream=True, follow_redirects=False)
                try:
                    # Automatic redirects read intermediate bodies without our cap.
                    if response.next_request is not None:
                        if redirect_count == 20:
                            raise httpx.TooManyRedirects(
                                "Exceeded maximum allowed redirects", request=request
                            )
                        request = response.next_request
                        continue
                    if response.status_code != 200:
                        raise HTTPException(400, "Error downloading media file")
                    if max_bytes is not None:
                        # Reject compression before decoding can expand one chunk
                        # beyond the cap; media providers should honor identity.
                        encoding = response.headers.get("content-encoding", "").strip().lower()
                        if encoding not in ("", "identity"):
                            raise HTTPException(400, "Compressed media download is not supported")
                        try:
                            content_length = int(response.headers.get("content-length", ""))
                        except ValueError:
                            content_length = None
                        if content_length is not None and content_length > max_bytes:
                            raise HTTPException(413, "Media file exceeds download limit")
                    content = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=chunk_size):
                        if max_bytes is not None and len(content) + len(chunk) > max_bytes:
                            raise HTTPException(413, "Media file exceeds download limit")
                        content.extend(chunk)
                    return bytes(content)
                finally:
                    await response.aclose()


async def download_external_audio(
    media_url: str,
    *,
    max_bytes: int | None = None,
    total_timeout: float = 300.0,
) -> bytes:
    """Download audio once for transcription and retention, preserving audio errors."""
    try:
        content = await download_external_media(
            media_url, max_bytes=max_bytes, total_timeout=total_timeout
        )
    except HTTPException as exc:
        if exc.status_code == 400:
            raise HTTPException(400, "Error downloading audio file") from exc
        raise
    if not content:
        raise HTTPException(400, "No audio")
    return content


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
    provider_started: bool | None = None,
) -> None:
    """Settle a billable provider response, otherwise release its reservation."""
    if isinstance(error, BillableSTTProviderError):
        await settle_stt_attempt(
            reservation_id,
            context=context,
        )
        return
    if provider_started and getattr(error, "status_code", None) not in {400, 401, 403, 404, 413, 422, 429}:
        # The request may have run. Keep its original payer's hold for reconciliation.
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


async def transcribe_with_elevenlabs(
    audio_content: bytes = None,
    media_url: str = None,
    language_code: str | None = None,
    before_provider=None,
):
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
            if language_code:
                form_data.add_field("language_code", language_code)
            form_data.add_field("file", audio_content, filename="audio.webm", content_type="audio/webm")

            if before_provider is not None:
                await before_provider()
            async with session.post(url, headers=headers, data=form_data) as response:
                if response.status != 200:
                    error_text = await response.text()
                    error = RuntimeError(f"ElevenLabs STT API error: {response.status}")
                    error.status_code = response.status
                    raise error
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
    language_code: str | None = None,
    before_provider=None,
):
    try:
        options = {
            "model": "nova-2",
            "smart_format": True,
            "punctuate": True,
            "language": language_code or DEFAULT_STT_LANGUAGE,
        }
        if before_provider is not None:
            await before_provider()
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
    result = await transcribe_external_audio_detailed(
        user_id=user_id,
        media_url=media_url,
        audio_content=audio_content,
        user_agent=user_agent,
    )
    return result.text


async def transcribe_external_audio_detailed(
    *,
    user_id: int,
    media_url: str = None,
    audio_content: bytes = None,
    user_agent: str = None,
    preferred_engine: str | None = None,
    duration_seconds: float | None = None,
) -> ExternalTranscriptionResult:
    if media_url:
        audio_content = await download_external_audio(media_url)

    if not audio_content:
        raise HTTPException(status_code=400, detail="No audio")

    if duration_seconds is None:
        try:
            audio_duration = await asyncio.to_thread(
                _probe_audio_duration_seconds,
                audio_content,
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail="Audio duration could not be determined",
            ) from exc
    else:
        try:
            audio_duration = float(duration_seconds)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid audio duration") from exc
        if not math.isfinite(audio_duration) or audio_duration <= 0:
            raise HTTPException(status_code=400, detail="Invalid audio duration")

    duration_min = audio_duration / 60
    configured_engine = (
        "deepgram" if str(stt_engine).strip().lower() == "deepgram" else "elevenlabs"
    )
    primary_engine = (
        str(preferred_engine).strip().lower()
        if preferred_engine is not None
        else configured_engine
    )
    if primary_engine not in {"deepgram", "elevenlabs"}:
        raise HTTPException(status_code=400, detail="Unsupported speech-to-text engine")
    language_code = await _load_primary_stt_language(user_id)

    async def transcribe_with_engine(engine: str, reservation_id, attempt):
        extra = {}
        if current_messaging_state() is not None:
            async def before_provider():
                from billing.usage_reservations import claim_fixed_usage_provider
                await check_current_messaging("stt")
                if not await claim_fixed_usage_provider(reservation_id, purpose="stt", user_id=user_id):
                    attempt["started"] = True  # Another claimant owns any settlement/refund.
                    raise BillingReservationError("STT provider attempt was already claimed")
                await check_current_messaging("stt")
                attempt["started"] = True
            extra["before_provider"] = before_provider
        if engine == "elevenlabs":
            return await transcribe_with_elevenlabs(
                audio_content=audio_content,
                language_code=language_code,
                **extra,
            )
        return await transcribe_with_deepgram(
            audio_content=audio_content,
            user_agent=user_agent,
            language_code=language_code,
            **extra,
        )

    await check_current_messaging("stt")
    primary_attempt = {"started": False if current_messaging_state() is not None else None}
    primary_reservation_id = await reserve_stt_attempt(
        user_id=user_id,
        engine=primary_engine,
        configured_engine=configured_engine,
        duration_min=duration_min,
        context=f"external {primary_engine}",
    )
    try:
        prompt = await transcribe_with_engine(primary_engine, primary_reservation_id, primary_attempt)
    except BaseException as primary_error:
        await finalize_failed_stt_attempt(
            primary_reservation_id,
            primary_error,
            context=f"failed external {primary_engine}",
            provider_started=primary_attempt["started"],
        )
        if (
            not isinstance(primary_error, Exception)
            or isinstance(primary_error, EmbedError)
            or not stt_fallback_enabled
            or primary_engine == "elevenlabs"
        ):
            raise

        fallback_engine = "elevenlabs"
        await check_current_messaging("stt")
        fallback_attempt = {"started": False if current_messaging_state() is not None else None}
        fallback_reservation_id = await reserve_stt_attempt(
            user_id=user_id,
            engine=fallback_engine,
            configured_engine=configured_engine,
            duration_min=duration_min,
            context=f"external fallback {fallback_engine}",
        )
        try:
            prompt = await transcribe_with_engine(fallback_engine, fallback_reservation_id, fallback_attempt)
        except BaseException as fallback_error:
            await finalize_failed_stt_attempt(
                fallback_reservation_id,
                fallback_error,
                context=f"failed external fallback {fallback_engine}",
                provider_started=fallback_attempt["started"],
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
        return ExternalTranscriptionResult(
            text=prompt,
            provider=fallback_engine,
            model="scribe_v2",
            duration_seconds=audio_duration,
        )

    await settle_stt_attempt(
        primary_reservation_id,
        context=f"external {primary_engine}",
    )
    return ExternalTranscriptionResult(
        text=prompt,
        provider=primary_engine,
        model="nova-2" if primary_engine == "deepgram" else "scribe_v2",
        duration_seconds=audio_duration,
    )


async def transcribe_external_request(request, audio=None, user_id: int = None, media_url: str = None):
    user_agent = request.headers.get("user-agent") if request else None
    audio_content = await audio.read() if audio else None
    return await transcribe_external_audio(
        user_id=user_id,
        media_url=media_url,
        audio_content=audio_content,
        user_agent=user_agent,
    )
