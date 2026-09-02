"""Provider-exact, durable renderer for private phone audio caches.

The cache activation service owns the final MP3/PCMU files and database
activation.  This module owns the expensive provider work that precedes it:

* ElevenLabs voices use the timestamped TTS endpoint and its *original*
  character alignment.
* OpenAI voices use OpenAI TTS and then ElevenLabs forced alignment.  Missing
  alignment capability is a hard error; the voice is never substituted.
* Provider billing is reserved before the request and settled exactly once.
* A render fingerprint identifies a durable private source artifact, allowing
  retries and later cache revisions to reuse completed provider work.

The provider APIs used here have no idempotency key for these operations.  A
timeout or cancellation after a request has started is therefore fenced as
``needs_attention`` instead of being replayed automatically.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import inspect
import json
import math
import os
from io import BytesIO
from pathlib import Path
import tempfile
from typing import Any, Protocol
from urllib.parse import quote

import aiohttp

from ai_runtime.voice_resolution import CanonicalVoice
from integrations.telephony.audio_cache_service import (
    PHONE_CACHE_OPENAI_MODEL,
    RenderedMp3,
)
from integrations.telephony.greetings import (
    PHONE_CACHE_MP3_FORMAT,
    PhoneTextAlignment,
    normalize_literal_text,
)
from tools.tts_config import TTSProfile


ELEVENLABS_TTS_WITH_TIMESTAMPS_URL = (
    "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"
)
ELEVENLABS_FORCED_ALIGNMENT_URL = (
    "https://api.elevenlabs.io/v1/forced-alignment"
)
OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"

DEFAULT_RENDER_ARTIFACT_ROOT = (
    Path(__file__).resolve().parents[2] / "data" / "phone_audio_render_artifacts"
)
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_TOTAL_TIMEOUT_SECONDS = 90.0
MAX_TTS_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_ALIGNMENT_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ERROR_DETAIL_BYTES = 4_096


class PhoneAudioRendererError(RuntimeError):
    """A phone cache render could not be completed safely."""


class PhoneAudioRendererNeedsAttention(PhoneAudioRendererError):
    """Provider outcome is ambiguous and must not be replayed automatically."""


class ProviderTransportError(PhoneAudioRendererError):
    """A request failed without a definitive HTTP response."""


class ProviderResponseTooLarge(PhoneAudioRendererError):
    """A provider response exceeded the configured hard limit."""


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    status: int
    body: bytes
    content_type: str | None = None


class ProviderTransport(Protocol):
    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        response_limit: int,
    ) -> ProviderResponse: ...

    async def post_multipart(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        fields: Mapping[str, str],
        file_field: str,
        file_name: str,
        file_content_type: str,
        file_bytes: bytes,
        response_limit: int,
    ) -> ProviderResponse: ...


class AioHttpProviderTransport:
    """Small bounded aiohttp adapter; it never logs bodies or credentials."""

    def __init__(
        self,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
    ) -> None:
        if connect_timeout <= 0 or total_timeout <= 0:
            raise ValueError("provider timeouts must be positive")
        self._timeout = aiohttp.ClientTimeout(
            total=float(total_timeout),
            connect=float(connect_timeout),
            sock_connect=float(connect_timeout),
        )

    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        response_limit: int,
    ) -> ProviderResponse:
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(
                    url,
                    headers=dict(headers),
                    json=dict(payload),
                ) as response:
                    body = await _read_bounded_response(response, response_limit)
                    return ProviderResponse(
                        status=int(response.status),
                        body=body,
                        content_type=response.headers.get("Content-Type"),
                    )
        except ProviderResponseTooLarge:
            raise
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as exc:
            raise ProviderTransportError("provider request outcome is unknown") from exc

    async def post_multipart(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        fields: Mapping[str, str],
        file_field: str,
        file_name: str,
        file_content_type: str,
        file_bytes: bytes,
        response_limit: int,
    ) -> ProviderResponse:
        form = aiohttp.FormData()
        for key, value in fields.items():
            form.add_field(str(key), str(value))
        form.add_field(
            file_field,
            file_bytes,
            filename=file_name,
            content_type=file_content_type,
        )
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(
                    url,
                    headers=dict(headers),
                    data=form,
                ) as response:
                    body = await _read_bounded_response(response, response_limit)
                    return ProviderResponse(
                        status=int(response.status),
                        body=body,
                        content_type=response.headers.get("Content-Type"),
                    )
        except ProviderResponseTooLarge:
            raise
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as exc:
            raise ProviderTransportError("provider request outcome is unknown") from exc


async def _read_bounded_response(response: Any, limit: int) -> bytes:
    if isinstance(limit, bool) or int(limit) <= 0:
        raise ValueError("response limit must be positive")
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > int(limit):
                raise ProviderResponseTooLarge("provider response is too large")
        except ValueError:
            pass
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        size += len(chunk)
        if size > int(limit):
            raise ProviderResponseTooLarge("provider response is too large")
        chunks.append(bytes(chunk))
    return b"".join(chunks)


class BillingAdapter(Protocol):
    async def reserve_tts(
        self,
        *,
        user_id: int,
        provider: str,
        characters: int,
    ) -> str: ...

    async def reserve_alignment(
        self,
        *,
        user_id: int,
        duration_seconds: float,
    ) -> str: ...

    async def claim(
        self, reservation_id: str, *, user_id: int, purpose: str
    ) -> bool: ...

    async def mark_succeeded(
        self, reservation_id: str, *, user_id: int, purpose: str
    ) -> bool: ...

    async def settle(self, reservation_id: str) -> bool: ...

    async def refund(self, reservation_id: str) -> bool: ...


class AurvekTtsBillingAdapter:
    """Use Aurvek's durable fixed-usage reservation primitives for TTS work."""

    async def reserve_tts(
        self,
        *,
        user_id: int,
        provider: str,
        characters: int,
    ) -> str:
        from billing.usage_reservations import reserve_fixed_usage
        from common import Cost

        rate, service_id = Cost.get_tts_service(provider)
        amount = float(rate) * int(characters)
        if (
            service_id is None
            or int(service_id) <= 0
            or not math.isfinite(amount)
            or amount <= 0
        ):
            raise PhoneAudioRendererError(
                f"TTS billing is not configured for {provider}"
            )
        return await reserve_fixed_usage(
            user_id=int(user_id),
            purpose="tts",
            amount=amount,
            service_id=int(service_id),
            usage_quantity=float(characters),
        )

    async def reserve_alignment(
        self,
        *,
        user_id: int,
        duration_seconds: float,
    ) -> str:
        from billing.usage_reservations import reserve_fixed_usage
        from common import Cost

        duration = float(duration_seconds)
        if not math.isfinite(duration) or duration <= 0:
            raise PhoneAudioRendererError("forced-alignment duration is invalid")
        rate, service_id = Cost.get_stt_service("elevenlabs")
        minutes = duration / 60.0
        amount = rate * minutes
        if (
            service_id is None
            or int(service_id) <= 0
            or not math.isfinite(amount)
            or amount <= 0
        ):
            raise PhoneAudioRendererError(
                "ElevenLabs forced-alignment billing is not configured"
            )
        return await reserve_fixed_usage(
            user_id=int(user_id),
            purpose="stt",
            amount=amount,
            service_id=int(service_id),
            usage_quantity=minutes,
        )

    async def claim(
        self, reservation_id: str, *, user_id: int, purpose: str
    ) -> bool:
        from billing.usage_reservations import claim_fixed_usage_provider

        return await claim_fixed_usage_provider(
            reservation_id,
            purpose=purpose,
            user_id=int(user_id),
        )

    async def mark_succeeded(
        self, reservation_id: str, *, user_id: int, purpose: str
    ) -> bool:
        from billing.usage_reservations import mark_fixed_usage_provider_succeeded

        return await mark_fixed_usage_provider_succeeded(
            reservation_id,
            purpose=purpose,
            user_id=int(user_id),
        )

    async def settle(self, reservation_id: str) -> bool:
        from billing.usage_reservations import settle_fixed_usage

        return await settle_fixed_usage(reservation_id)

    async def refund(self, reservation_id: str) -> bool:
        from billing.usage_reservations import refund_fixed_usage

        return await refund_fixed_usage(reservation_id)


@dataclass(frozen=True, slots=True)
class _Attempt:
    id: int
    mode: str

    @property
    def reused(self) -> bool:
        return self.mode == "complete"


class PhoneAudioRenderAttemptRepository:
    """Durable fencing for non-idempotent provider operations."""

    def __init__(self, connection_factory: Callable[..., Any] | None = None) -> None:
        if connection_factory is None:
            from database import get_db_connection

            connection_factory = get_db_connection
        self._connection_factory = connection_factory

    async def acquire(
        self,
        *,
        cache_key: str,
        render_fingerprint: str,
        activation_id: str,
        billing_user_id: int,
        provider: str,
        reusable_artifact_ready: bool,
        source_artifact_ready: bool,
    ) -> _Attempt:
        alignment_initial = "not_required" if provider == "elevenlabs" else "pending"
        alignment_completed = (
            "not_required" if provider == "elevenlabs" else "succeeded"
        )
        async with self._connection_factory() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cache_cursor = await conn.execute(
                    "SELECT status FROM PHONE_PROMPT_AUDIO_CACHE WHERE cache_key=?",
                    (cache_key,),
                )
                cache_row = await cache_cursor.fetchone()
                activation_cursor = await conn.execute(
                    "SELECT status FROM VOICE_CANONICAL_ACTIVATIONS WHERE id=?",
                    (activation_id,),
                )
                activation_row = await activation_cursor.fetchone()
                if cache_row is None or str(cache_row[0]) != "pending":
                    raise PhoneAudioRendererError("phone cache render target is not pending")
                if activation_row is None or str(activation_row[0]) != "pending":
                    raise PhoneAudioRendererError("phone cache activation is not pending")

                current_cursor = await conn.execute(
                    """
                    SELECT * FROM PHONE_AUDIO_RENDER_ATTEMPTS
                    WHERE cache_key=? AND render_fingerprint=?
                    """,
                    (cache_key, render_fingerprint),
                )
                current = await current_cursor.fetchone()
                if current is not None:
                    if (
                        int(current["billing_user_id"]) != int(billing_user_id)
                        or str(current["activation_id"]) != activation_id
                    ):
                        raise PhoneAudioRendererError(
                            "render attempt ownership does not match"
                        )
                    if current["completed_at"] is not None and reusable_artifact_ready:
                        await conn.commit()
                        return _Attempt(id=int(current["id"]), mode="complete")
                    if current["completed_at"] is not None:
                        await conn.execute(
                            """
                            UPDATE PHONE_AUDIO_RENDER_ATTEMPTS
                            SET needs_attention=1,last_error=?,updated_at=CURRENT_TIMESTAMP
                            WHERE id=?
                            """,
                            (
                                "completed render artifact is missing",
                                int(current["id"]),
                            ),
                        )
                        await conn.commit()
                        raise PhoneAudioRendererNeedsAttention(
                            "completed render artifact is missing"
                        )
                    await conn.rollback()
                    if bool(current["needs_attention"]) or str(
                        current["provider_state"]
                    ) in {"in_flight", "ambiguous", "succeeded"}:
                        raise PhoneAudioRendererNeedsAttention(
                            "phone audio render cannot be replayed safely"
                        )
                    raise PhoneAudioRendererError(
                        "phone audio render attempt already failed"
                    )

                fingerprint_cursor = await conn.execute(
                    """
                    SELECT * FROM PHONE_AUDIO_RENDER_ATTEMPTS
                    WHERE render_fingerprint=?
                    ORDER BY completed_at IS NOT NULL DESC,id DESC
                    """,
                    (render_fingerprint,),
                )
                same_work = await fingerprint_cursor.fetchall()
                unsafe_elevenlabs_successes = [
                    row
                    for row in same_work
                    if provider == "elevenlabs"
                    and str(row["provider_state"]) == "succeeded"
                    and row["completed_at"] is None
                ]
                if unsafe_elevenlabs_successes:
                    await conn.executemany(
                        """
                        UPDATE PHONE_AUDIO_RENDER_ATTEMPTS
                        SET needs_attention=1,last_error=?,updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                        """,
                        (
                            (
                                "successful ElevenLabs render has no usable "
                                "original alignment",
                                int(row["id"]),
                            )
                            for row in unsafe_elevenlabs_successes
                        ),
                    )
                    await conn.commit()
                    raise PhoneAudioRendererNeedsAttention(
                        "equivalent ElevenLabs provider work cannot be replayed safely"
                    )
                completed = next(
                    (row for row in same_work if row["completed_at"] is not None),
                    None,
                )
                if completed is not None:
                    if not reusable_artifact_ready:
                        await conn.execute(
                            """
                            UPDATE PHONE_AUDIO_RENDER_ATTEMPTS
                            SET needs_attention=1,last_error=?,updated_at=CURRENT_TIMESTAMP
                            WHERE id=?
                            """,
                            ("completed render artifact is missing", int(completed["id"])),
                        )
                        await conn.commit()
                        raise PhoneAudioRendererNeedsAttention(
                            "completed render artifact is missing"
                        )
                    cursor = await conn.execute(
                        """
                        INSERT INTO PHONE_AUDIO_RENDER_ATTEMPTS(
                            cache_key,render_fingerprint,billing_user_id,activation_id,
                            provider_state,alignment_state,provider_succeeded_at,
                            alignment_succeeded_at,completed_at
                        ) VALUES(?,?,?,?,'succeeded',?,CURRENT_TIMESTAMP,
                                 CASE WHEN ?='succeeded' THEN CURRENT_TIMESTAMP END,
                                 CURRENT_TIMESTAMP)
                        """,
                        (
                            cache_key,
                            render_fingerprint,
                            int(billing_user_id),
                            activation_id,
                            alignment_completed,
                            alignment_completed,
                        ),
                    )
                    attempt_id = int(cursor.lastrowid)
                    await conn.commit()
                    return _Attempt(id=attempt_id, mode="complete")

                if any(
                    str(row["provider_state"]) in {"in_flight", "ambiguous"}
                    or str(row["alignment_state"])
                    in {"in_flight", "ambiguous", "succeeded"}
                    or bool(row["needs_attention"])
                    for row in same_work
                ):
                    await conn.rollback()
                    raise PhoneAudioRendererNeedsAttention(
                        "equivalent provider work is unresolved"
                    )

                reusable_source = next(
                    (
                        row
                        for row in same_work
                        if provider == "openai"
                        and str(row["provider_state"]) == "succeeded"
                        and str(row["alignment_state"]) in {"pending", "failed"}
                    ),
                    None,
                )
                if reusable_source is not None:
                    if not source_artifact_ready:
                        await conn.execute(
                            """
                            UPDATE PHONE_AUDIO_RENDER_ATTEMPTS
                            SET needs_attention=1,last_error=?,updated_at=CURRENT_TIMESTAMP
                            WHERE id=?
                            """,
                            (
                                "successful OpenAI source artifact is missing",
                                int(reusable_source["id"]),
                            ),
                        )
                        await conn.commit()
                        raise PhoneAudioRendererNeedsAttention(
                            "successful OpenAI source artifact is missing"
                        )
                    cursor = await conn.execute(
                        """
                        INSERT INTO PHONE_AUDIO_RENDER_ATTEMPTS(
                            cache_key,render_fingerprint,billing_user_id,activation_id,
                            provider_state,alignment_state,provider_succeeded_at
                        ) VALUES(?,?,?,?,'succeeded','pending',CURRENT_TIMESTAMP)
                        """,
                        (
                            cache_key,
                            render_fingerprint,
                            int(billing_user_id),
                            activation_id,
                        ),
                    )
                    attempt_id = int(cursor.lastrowid)
                    await conn.commit()
                    return _Attempt(id=attempt_id, mode="alignment_only")

                cursor = await conn.execute(
                    """
                    INSERT INTO PHONE_AUDIO_RENDER_ATTEMPTS(
                        cache_key,render_fingerprint,billing_user_id,activation_id,
                        provider_state,alignment_state,provider_started_at
                    ) VALUES(?,?,?,?,'in_flight',?,CURRENT_TIMESTAMP)
                    """,
                    (
                        cache_key,
                        render_fingerprint,
                        int(billing_user_id),
                        activation_id,
                        alignment_initial,
                    ),
                )
                attempt_id = int(cursor.lastrowid)
                await conn.commit()
                return _Attempt(id=attempt_id, mode="fresh")
            except BaseException:
                if conn.in_transaction:
                    await conn.rollback()
                raise

    async def set_tts_reservation(
        self,
        attempt_id: int,
        *,
        tts_reservation_id: str,
    ) -> None:
        await self._update_one(
            """
            UPDATE PHONE_AUDIO_RENDER_ATTEMPTS
            SET tts_reservation_id=?,updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND provider_state='in_flight'
              AND tts_reservation_id IS NULL
            """,
            (tts_reservation_id, int(attempt_id)),
        )

    async def set_alignment_reservation(
        self,
        attempt_id: int,
        *,
        alignment_reservation_id: str,
    ) -> None:
        await self._update_one(
            """
            UPDATE PHONE_AUDIO_RENDER_ATTEMPTS
            SET alignment_reservation_id=?,updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND provider_state='succeeded'
              AND alignment_state='pending' AND alignment_reservation_id IS NULL
            """,
            (alignment_reservation_id, int(attempt_id)),
        )

    async def provider_succeeded(self, attempt_id: int) -> None:
        await self._update_one(
            """
            UPDATE PHONE_AUDIO_RENDER_ATTEMPTS
            SET provider_state='succeeded',provider_succeeded_at=CURRENT_TIMESTAMP,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND provider_state='in_flight'
            """,
            (int(attempt_id),),
        )

    async def alignment_started(self, attempt_id: int) -> None:
        await self._update_one(
            """
            UPDATE PHONE_AUDIO_RENDER_ATTEMPTS
            SET alignment_state='in_flight',alignment_started_at=CURRENT_TIMESTAMP,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND provider_state='succeeded' AND alignment_state='pending'
            """,
            (int(attempt_id),),
        )

    async def alignment_succeeded(self, attempt_id: int) -> None:
        await self._update_one(
            """
            UPDATE PHONE_AUDIO_RENDER_ATTEMPTS
            SET alignment_state='succeeded',alignment_succeeded_at=CURRENT_TIMESTAMP,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND provider_state='succeeded' AND alignment_state='in_flight'
            """,
            (int(attempt_id),),
        )

    async def completed(self, attempt_id: int) -> None:
        await self._update_one(
            """
            UPDATE PHONE_AUDIO_RENDER_ATTEMPTS
            SET completed_at=CURRENT_TIMESTAMP,last_error=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND provider_state='succeeded'
              AND alignment_state IN ('succeeded','not_required')
            """,
            (int(attempt_id),),
        )

    async def fail_known(
        self,
        attempt_id: int,
        *,
        stage: str,
        error: BaseException | str,
    ) -> None:
        if stage not in {"provider", "alignment"}:
            raise ValueError("invalid render failure stage")
        if stage == "provider":
            assignment = "provider_state='failed'"
        else:
            assignment = "alignment_state='failed'"
        await self._update_one(
            f"""
            UPDATE PHONE_AUDIO_RENDER_ATTEMPTS
            SET {assignment},last_error=?,updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (_bounded_error(error), int(attempt_id)),
        )

    async def ambiguous(
        self,
        attempt_id: int,
        *,
        stage: str,
        error: BaseException | str,
    ) -> None:
        if stage not in {"provider", "alignment"}:
            raise ValueError("invalid render ambiguity stage")
        assignment = (
            "provider_state='ambiguous'"
            if stage == "provider"
            else "alignment_state='ambiguous'"
        )
        await self._update_one(
            f"""
            UPDATE PHONE_AUDIO_RENDER_ATTEMPTS
            SET {assignment},needs_attention=1,last_error=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (_bounded_error(error), int(attempt_id)),
        )

    async def needs_attention(
        self,
        attempt_id: int,
        *,
        error: BaseException | str,
    ) -> None:
        await self._update_one(
            """
            UPDATE PHONE_AUDIO_RENDER_ATTEMPTS
            SET needs_attention=1,last_error=?,updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (_bounded_error(error), int(attempt_id)),
        )

    async def _update_one(self, statement: str, parameters: tuple[Any, ...]) -> None:
        async with self._connection_factory() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await conn.execute(statement, parameters)
                if cursor.rowcount != 1:
                    raise PhoneAudioRendererNeedsAttention(
                        "render attempt changed concurrently"
                    )
                await conn.commit()
            except BaseException:
                if conn.in_transaction:
                    await conn.rollback()
                raise


class PhoneAudioCacheRenderer:
    """Production ``Mp3Renderer`` implementation for phone cache activation."""

    def __init__(
        self,
        *,
        repository: PhoneAudioRenderAttemptRepository | None = None,
        transport: ProviderTransport | None = None,
        billing: BillingAdapter | None = None,
        artifact_root: str | Path = DEFAULT_RENDER_ARTIFACT_ROOT,
        elevenlabs_key_getter: Callable[..., str | None] | None = None,
        openai_key_getter: Callable[[], str | None] | None = None,
        mp3_duration_probe: Callable[[bytes], float] | None = None,
    ) -> None:
        self._repository = repository or PhoneAudioRenderAttemptRepository()
        self._transport = transport or AioHttpProviderTransport()
        self._billing = billing or AurvekTtsBillingAdapter()
        self._artifact_root = Path(artifact_root).resolve()
        self._elevenlabs_key_getter = (
            elevenlabs_key_getter or _get_elevenlabs_key
        )
        self._openai_key_getter = openai_key_getter or _get_openai_key
        self._openai_model = PHONE_CACHE_OPENAI_MODEL
        self._mp3_duration_probe = mp3_duration_probe or _mp3_duration_seconds

    async def __call__(
        self,
        *,
        literal_text: str,
        voice: CanonicalVoice,
        profile: TTSProfile,
        billing_user_id: int,
        activation_id: str,
        cache_key: str,
        render_fingerprint: str,
    ) -> RenderedMp3:
        text = normalize_literal_text(literal_text)
        provider = str(voice.provider or "").strip().lower()
        if provider not in {"elevenlabs", "openai"}:
            raise PhoneAudioRendererError(
                f"canonical TTS provider is unsupported: {provider or 'missing'}"
            )
        if profile.output_format != PHONE_CACHE_MP3_FORMAT:
            raise PhoneAudioRendererError("phone cache TTS must request MP3")
        if not voice.voice_code:
            raise PhoneAudioRendererError("canonical provider voice is missing")
        _validate_render_identity(
            activation_id=activation_id,
            cache_key=cache_key,
            render_fingerprint=render_fingerprint,
            billing_user_id=billing_user_id,
        )

        artifact = _ArtifactStore(self._artifact_root, render_fingerprint)
        reusable = await asyncio.to_thread(artifact.load_if_valid, text)
        reusable_source = await asyncio.to_thread(artifact.load_source_if_valid)
        attempt = await self._repository.acquire(
            cache_key=cache_key,
            render_fingerprint=render_fingerprint,
            activation_id=activation_id,
            billing_user_id=int(billing_user_id),
            provider=provider,
            reusable_artifact_ready=reusable is not None,
            source_artifact_ready=reusable_source is not None,
        )
        if attempt.reused:
            if reusable is None:
                raise PhoneAudioRendererNeedsAttention(
                    "completed render artifact is unavailable"
                )
            return reusable
        if attempt.mode == "alignment_only":
            if provider != "openai" or reusable_source is None:
                raise PhoneAudioRendererNeedsAttention(
                    "reusable OpenAI source artifact is unavailable"
                )
            try:
                eleven_key = _required_key(
                    await asyncio.to_thread(self._elevenlabs_key_getter),
                    "ElevenLabs",
                )
            except asyncio.CancelledError as exc:
                await _record_known_failure_owned(
                    self._repository,
                    attempt.id,
                    stage="alignment",
                    error=exc,
                )
                raise
            except BaseException as exc:
                await _record_known_failure_owned(
                    self._repository,
                    attempt.id,
                    stage="alignment",
                    error=exc,
                )
                raise
            return await self._align_openai_source(
                attempt_id=attempt.id,
                text=text,
                audio=reusable_source,
                elevenlabs_key=eleven_key,
                billing_user_id=int(billing_user_id),
                artifact=artifact,
            )

        try:
            elevenlabs_voice_id = (
                voice.voice_code if provider == "elevenlabs" else None
            )
            eleven_key = _required_key(
                await asyncio.to_thread(
                    _invoke_elevenlabs_key_getter,
                    self._elevenlabs_key_getter,
                    elevenlabs_voice_id,
                ),
                "ElevenLabs",
            )
            openai_key = None
            if provider == "openai":
                openai_key = _required_key(
                    await asyncio.to_thread(self._openai_key_getter),
                    "OpenAI",
                )
        except asyncio.CancelledError as exc:
            await _record_known_failure_owned(
                self._repository,
                attempt.id,
                stage="provider",
                error=exc,
            )
            raise
        except BaseException as exc:
            await _record_known_failure_owned(
                self._repository,
                attempt.id,
                stage="provider",
                error=exc,
            )
            raise

        tts_reservation: str | None = None
        try:
            tts_reservation = await self._billing.reserve_tts(
                user_id=int(billing_user_id),
                provider=provider,
                characters=len(text),
            )
            await self._repository.set_tts_reservation(
                attempt.id,
                tts_reservation_id=tts_reservation,
            )
        except asyncio.CancelledError as exc:
            if tts_reservation:
                await _refund_and_fail_known_owned(
                    self._billing,
                    tts_reservation,
                    repository=self._repository,
                    attempt_id=attempt.id,
                    stage="provider",
                    error=exc,
                )
            else:
                await _record_known_failure_owned(
                    self._repository,
                    attempt.id,
                    stage="provider",
                    error=exc,
                )
            raise
        except BaseException as exc:
            if tts_reservation:
                await _refund_and_fail_known_owned(
                    self._billing,
                    tts_reservation,
                    repository=self._repository,
                    attempt_id=attempt.id,
                    stage="provider",
                    error=exc,
                )
            else:
                await _record_known_failure_owned(
                    self._repository,
                    attempt.id,
                    stage="provider",
                    error=exc,
                )
            raise PhoneAudioRendererError("could not reserve phone TTS usage") from exc

        if provider == "elevenlabs":
            return await self._render_elevenlabs(
                attempt_id=attempt.id,
                text=text,
                voice=voice,
                profile=profile,
                api_key=eleven_key,
                billing_user_id=int(billing_user_id),
                reservation_id=tts_reservation,
                artifact=artifact,
            )
        return await self._render_openai(
            attempt_id=attempt.id,
            text=text,
            voice=voice,
            openai_key=str(openai_key),
            elevenlabs_key=eleven_key,
            billing_user_id=int(billing_user_id),
            tts_reservation_id=tts_reservation,
            artifact=artifact,
        )

    async def _render_elevenlabs(
        self,
        *,
        attempt_id: int,
        text: str,
        voice: CanonicalVoice,
        profile: TTSProfile,
        api_key: str,
        billing_user_id: int,
        reservation_id: str,
        artifact: _ArtifactStore,
    ) -> RenderedMp3:
        try:
            await _claim_or_attention(
                self._billing,
                reservation_id,
                billing_user_id,
                "ElevenLabs TTS",
                purpose="tts",
            )
        except asyncio.CancelledError as exc:
            await _await_owned(
                self._repository.ambiguous(
                    attempt_id, stage="provider", error=exc
                )
            )
            raise
        except BaseException as exc:
            await self._repository.ambiguous(
                attempt_id, stage="provider", error=exc
            )
            raise
        try:
            response = await self._transport.post_json(
                (
                    ELEVENLABS_TTS_WITH_TIMESTAMPS_URL.format(
                        voice_id=quote(voice.voice_code, safe="")
                    )
                    + f"?output_format={PHONE_CACHE_MP3_FORMAT}"
                ),
                headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                payload={
                    "text": text,
                    "model_id": profile.model_id,
                    "voice_settings": {
                        "stability": float(profile.stability),
                        "similarity_boost": float(profile.similarity_boost),
                    },
                },
                response_limit=MAX_TTS_RESPONSE_BYTES,
            )
        except asyncio.CancelledError as exc:
            await _await_owned(
                self._repository.ambiguous(
                    attempt_id, stage="provider", error=exc
                )
            )
            raise
        except (ProviderTransportError, ProviderResponseTooLarge) as exc:
            await self._repository.ambiguous(
                attempt_id, stage="provider", error=exc
            )
            raise PhoneAudioRendererNeedsAttention(
                "ElevenLabs TTS result is ambiguous"
            ) from exc

        if not 200 <= response.status < 300:
            error = PhoneAudioRendererError(
                f"ElevenLabs TTS failed ({response.status}): "
                f"{_response_excerpt(response.body)}"
            )
            await _refund_and_fail_known_owned(
                self._billing,
                reservation_id,
                repository=self._repository,
                attempt_id=attempt_id,
                stage="provider",
                error=error,
            )
            raise error

        await _settle_and_transition_owned(
            self._billing,
            reservation_id,
            billing_user_id,
            repository=self._repository,
            attempt_id=attempt_id,
            purpose="tts",
            transition="provider",
        )
        try:
            audio, alignment = _parse_elevenlabs_tts_response(response.body, text)
        except BaseException as exc:
            await _record_known_failure_owned(
                self._repository,
                attempt_id,
                stage="alignment",
                error=exc,
                needs_attention=True,
            )
            raise PhoneAudioRendererError(
                "ElevenLabs returned unusable timestamped audio"
            ) from exc
        return await self._publish_completed_artifact(
            attempt_id=attempt_id,
            text=text,
            audio=audio,
            alignment=alignment,
            artifact=artifact,
            preserve_existing_source=False,
            invalid_message="ElevenLabs returned unusable timestamped audio",
        )

    async def _render_openai(
        self,
        *,
        attempt_id: int,
        text: str,
        voice: CanonicalVoice,
        openai_key: str,
        elevenlabs_key: str,
        billing_user_id: int,
        tts_reservation_id: str,
        artifact: _ArtifactStore,
    ) -> RenderedMp3:
        try:
            await _claim_or_attention(
                self._billing,
                tts_reservation_id,
                billing_user_id,
                "OpenAI TTS",
                purpose="tts",
            )
        except asyncio.CancelledError as exc:
            await _await_owned(
                self._repository.ambiguous(
                    attempt_id, stage="provider", error=exc
                )
            )
            raise
        except BaseException as exc:
            await self._repository.ambiguous(
                attempt_id, stage="provider", error=exc
            )
            raise
        try:
            response = await self._transport.post_json(
                OPENAI_TTS_URL,
                headers={
                    "Authorization": f"Bearer {openai_key}",
                    "Content-Type": "application/json",
                },
                payload={
                    "model": self._openai_model,
                    "input": text,
                    "voice": voice.voice_code,
                    "response_format": "mp3",
                },
                response_limit=MAX_TTS_RESPONSE_BYTES,
            )
        except asyncio.CancelledError as exc:
            await _await_owned(
                self._repository.ambiguous(
                    attempt_id, stage="provider", error=exc
                )
            )
            raise
        except (ProviderTransportError, ProviderResponseTooLarge) as exc:
            await self._repository.ambiguous(
                attempt_id, stage="provider", error=exc
            )
            raise PhoneAudioRendererNeedsAttention(
                "OpenAI TTS result is ambiguous"
            ) from exc

        if not 200 <= response.status < 300:
            error = PhoneAudioRendererError(
                f"OpenAI TTS failed ({response.status}): "
                f"{_response_excerpt(response.body)}"
            )
            await _refund_and_fail_known_owned(
                self._billing,
                tts_reservation_id,
                repository=self._repository,
                attempt_id=attempt_id,
                stage="provider",
                error=error,
            )
            raise error

        await _settle_and_transition_owned(
            self._billing,
            tts_reservation_id,
            billing_user_id,
            repository=self._repository,
            attempt_id=attempt_id,
            purpose="tts",
            transition="provider",
        )
        try:
            _validate_mp3(response.body)
            await _write_source_owned(artifact, response.body)
        except asyncio.CancelledError as exc:
            await _await_owned(
                self._repository.ambiguous(
                    attempt_id, stage="alignment", error=exc
                )
            )
            raise
        except BaseException as exc:
            await _record_known_failure_owned(
                self._repository,
                attempt_id,
                stage="alignment",
                error=exc,
            )
            raise PhoneAudioRendererError("OpenAI returned invalid MP3 audio") from exc

        return await self._align_openai_source(
            attempt_id=attempt_id,
            text=text,
            audio=response.body,
            elevenlabs_key=elevenlabs_key,
            billing_user_id=billing_user_id,
            artifact=artifact,
        )

    async def _align_openai_source(
        self,
        *,
        attempt_id: int,
        text: str,
        audio: bytes,
        elevenlabs_key: str,
        billing_user_id: int,
        artifact: _ArtifactStore,
    ) -> RenderedMp3:
        try:
            duration_seconds = await asyncio.to_thread(
                self._mp3_duration_probe, audio
            )
        except asyncio.CancelledError as exc:
            await _await_owned(
                self._repository.ambiguous(
                    attempt_id, stage="alignment", error=exc
                )
            )
            raise
        except BaseException as exc:
            await _record_known_failure_owned(
                self._repository,
                attempt_id,
                stage="alignment",
                error=exc,
            )
            raise PhoneAudioRendererError("OpenAI returned invalid MP3 audio") from exc

        alignment_reservation_id: str | None = None
        try:
            alignment_reservation_id = await self._billing.reserve_alignment(
                user_id=int(billing_user_id),
                duration_seconds=duration_seconds,
            )
            await self._repository.set_alignment_reservation(
                attempt_id,
                alignment_reservation_id=alignment_reservation_id,
            )
        except asyncio.CancelledError as exc:
            if alignment_reservation_id:
                await _refund_and_fail_known_owned(
                    self._billing,
                    alignment_reservation_id,
                    repository=self._repository,
                    attempt_id=attempt_id,
                    stage="alignment",
                    error=exc,
                )
            else:
                await _record_known_failure_owned(
                    self._repository,
                    attempt_id,
                    stage="alignment",
                    error=exc,
                )
            raise
        except BaseException as exc:
            if alignment_reservation_id:
                await _refund_and_fail_known_owned(
                    self._billing,
                    alignment_reservation_id,
                    repository=self._repository,
                    attempt_id=attempt_id,
                    stage="alignment",
                    error=exc,
                )
            else:
                await _record_known_failure_owned(
                    self._repository,
                    attempt_id,
                    stage="alignment",
                    error=exc,
                )
            raise PhoneAudioRendererError(
                "could not reserve forced-alignment usage"
            ) from exc

        try:
            await _await_owned(self._repository.alignment_started(attempt_id))
        except asyncio.CancelledError as exc:
            await _await_owned(
                self._repository.needs_attention(attempt_id, error=exc)
            )
            raise
        except BaseException as exc:
            await _await_owned(
                self._repository.needs_attention(attempt_id, error=exc)
            )
            raise PhoneAudioRendererNeedsAttention(
                "forced alignment start could not be recorded"
            ) from exc
        if alignment_reservation_id is None:
            raise PhoneAudioRendererNeedsAttention(
                "forced-alignment reservation identity is missing"
            )
        try:
            await _claim_or_attention(
                self._billing,
                alignment_reservation_id,
                billing_user_id,
                "ElevenLabs forced alignment",
                purpose="stt",
            )
        except asyncio.CancelledError as exc:
            await _await_owned(
                self._repository.ambiguous(
                    attempt_id, stage="alignment", error=exc
                )
            )
            raise
        except BaseException as exc:
            await self._repository.ambiguous(
                attempt_id, stage="alignment", error=exc
            )
            raise
        try:
            alignment_response = await self._transport.post_multipart(
                ELEVENLABS_FORCED_ALIGNMENT_URL,
                headers={"xi-api-key": elevenlabs_key},
                fields={"text": text},
                file_field="file",
                file_name="phone-cache.mp3",
                file_content_type="audio/mpeg",
                file_bytes=audio,
                response_limit=MAX_ALIGNMENT_RESPONSE_BYTES,
            )
        except asyncio.CancelledError as exc:
            await _await_owned(
                self._repository.ambiguous(
                    attempt_id, stage="alignment", error=exc
                )
            )
            raise
        except (ProviderTransportError, ProviderResponseTooLarge) as exc:
            await self._repository.ambiguous(
                attempt_id, stage="alignment", error=exc
            )
            raise PhoneAudioRendererNeedsAttention(
                "forced alignment result is ambiguous"
            ) from exc

        if not 200 <= alignment_response.status < 300:
            error = PhoneAudioRendererError(
                f"ElevenLabs forced alignment failed ({alignment_response.status}): "
                f"{_response_excerpt(alignment_response.body)}"
            )
            await _refund_and_fail_known_owned(
                self._billing,
                alignment_reservation_id,
                repository=self._repository,
                attempt_id=attempt_id,
                stage="alignment",
                error=error,
            )
            raise error

        await _settle_and_transition_owned(
            self._billing,
            alignment_reservation_id,
            billing_user_id,
            repository=self._repository,
            attempt_id=attempt_id,
            purpose="stt",
            transition="alignment",
        )
        try:
            alignment = _parse_forced_alignment_response(
                alignment_response.body, text
            )
        except BaseException as exc:
            await _record_known_failure_owned(
                self._repository,
                attempt_id,
                stage="alignment",
                error=exc,
                needs_attention=True,
            )
            raise PhoneAudioRendererError(
                "ElevenLabs returned invalid forced alignment"
            ) from exc
        return await self._publish_completed_artifact(
            attempt_id=attempt_id,
            text=text,
            audio=audio,
            alignment=alignment,
            artifact=artifact,
            preserve_existing_source=True,
            invalid_message="ElevenLabs returned invalid forced alignment",
        )

    async def _publish_completed_artifact(
        self,
        *,
        attempt_id: int,
        text: str,
        audio: bytes,
        alignment: PhoneTextAlignment,
        artifact: _ArtifactStore,
        preserve_existing_source: bool,
        invalid_message: str,
    ) -> RenderedMp3:
        try:
            rendered = await _write_artifact_owned(
                artifact,
                audio,
                alignment,
                preserve_existing_source=preserve_existing_source,
            )
        except asyncio.CancelledError as exc:
            published = await asyncio.to_thread(artifact.load_if_valid, text)
            if published is None:
                await _await_owned(
                    self._repository.needs_attention(attempt_id, error=exc)
                )
            else:
                await _await_owned(self._repository.completed(attempt_id))
            raise
        except BaseException as exc:
            await _record_known_failure_owned(
                self._repository,
                attempt_id,
                stage="alignment",
                error=exc,
                needs_attention=True,
            )
            raise PhoneAudioRendererError(invalid_message) from exc
        try:
            await _await_owned(self._repository.completed(attempt_id))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await _await_owned(
                self._repository.needs_attention(attempt_id, error=exc)
            )
            raise PhoneAudioRendererNeedsAttention(
                "published phone audio completion could not be recorded"
            ) from exc
        return rendered


async def _claim_or_attention(
    billing: BillingAdapter,
    reservation_id: str,
    user_id: int,
    label: str,
    *,
    purpose: str,
) -> None:
    try:
        claimed = await billing.claim(
            reservation_id,
            user_id=int(user_id),
            purpose=purpose,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise PhoneAudioRendererNeedsAttention(
            f"could not fence {label} billing"
        ) from exc
    if not claimed:
        raise PhoneAudioRendererNeedsAttention(
            f"{label} billing was already claimed"
        )


async def _settle_and_transition_owned(
    billing: BillingAdapter,
    reservation_id: str,
    user_id: int,
    *,
    repository: PhoneAudioRenderAttemptRepository,
    attempt_id: int,
    purpose: str,
    transition: str,
) -> None:
    if transition not in {"provider", "alignment"}:
        raise ValueError("invalid provider success transition")

    async def operation() -> None:
        settlement_error: BaseException | None = None
        try:
            marked = await billing.mark_succeeded(
                reservation_id,
                user_id=int(user_id),
                purpose=purpose,
            )
            if not marked:
                raise PhoneAudioRendererNeedsAttention(
                    "provider success could not be recorded"
                )
            settled = await billing.settle(reservation_id)
            if not settled:
                raise PhoneAudioRendererNeedsAttention(
                    "provider usage could not be settled"
                )
        except BaseException as exc:
            settlement_error = exc

        try:
            if transition == "provider":
                await repository.provider_succeeded(attempt_id)
            else:
                await repository.alignment_succeeded(attempt_id)
        except BaseException as exc:
            await repository.needs_attention(attempt_id, error=exc)
            raise PhoneAudioRendererNeedsAttention(
                "provider success transition could not be recorded"
            ) from exc

        if settlement_error is not None:
            await repository.needs_attention(attempt_id, error=settlement_error)
            raise PhoneAudioRendererNeedsAttention(
                "provider succeeded but billing settlement needs attention"
            ) from settlement_error

    try:
        await _await_owned(operation())
    except asyncio.CancelledError as exc:
        await _await_owned(repository.needs_attention(attempt_id, error=exc))
        raise


async def _record_known_failure_owned(
    repository: PhoneAudioRenderAttemptRepository,
    attempt_id: int,
    *,
    stage: str,
    error: BaseException | str,
    needs_attention: bool = False,
) -> None:
    async def operation() -> None:
        await repository.fail_known(attempt_id, stage=stage, error=error)
        if needs_attention:
            await repository.needs_attention(attempt_id, error=error)

    await _await_owned(operation())


async def _refund_and_fail_known_owned(
    billing: BillingAdapter,
    reservation_id: str,
    *,
    repository: PhoneAudioRenderAttemptRepository,
    attempt_id: int,
    stage: str,
    error: BaseException | str,
) -> None:
    async def operation() -> None:
        await _refund_quietly(billing, reservation_id)
        await repository.fail_known(attempt_id, stage=stage, error=error)

    await _await_owned(operation())


async def _refund_quietly(
    billing: BillingAdapter, reservation_id: str
) -> None:
    try:
        await billing.refund(reservation_id)
    except Exception:
        # The durable active reservation is deliberately left for the normal
        # stale-reservation reconciler.  The provider call is never replayed.
        return


def _parse_elevenlabs_tts_response(
    body: bytes, literal_text: str
) -> tuple[bytes, PhoneTextAlignment]:
    payload = _json_object(body)
    encoded = payload.get("audio_base64")
    if not isinstance(encoded, str) or not encoded:
        raise PhoneAudioRendererError("ElevenLabs audio payload is missing")
    try:
        audio = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PhoneAudioRendererError("ElevenLabs audio payload is invalid") from exc
    if len(audio) > MAX_TTS_RESPONSE_BYTES:
        raise ProviderResponseTooLarge("decoded TTS audio is too large")
    _validate_mp3(audio)
    alignment = _alignment_from_parallel_arrays(
        payload.get("alignment"), literal_text
    )
    return audio, alignment


def _alignment_from_parallel_arrays(
    value: Any, literal_text: str
) -> PhoneTextAlignment:
    if not isinstance(value, dict):
        raise PhoneAudioRendererError("original character alignment is missing")
    characters = value.get("characters")
    starts = value.get("character_start_times_seconds")
    ends = value.get("character_end_times_seconds")
    if not isinstance(characters, list) or not isinstance(starts, list) or not isinstance(ends, list):
        raise PhoneAudioRendererError("character alignment is invalid")
    if "".join(str(item) for item in characters) != literal_text:
        raise PhoneAudioRendererError(
            "provider original alignment does not match literal text"
        )
    if not (len(characters) == len(starts) == len(ends) == len(literal_text)):
        raise PhoneAudioRendererError("character alignment is incomplete")
    return _build_alignment(literal_text, starts, ends)


def _parse_forced_alignment_response(
    body: bytes, literal_text: str
) -> PhoneTextAlignment:
    payload = _json_object(body)
    characters = payload.get("characters")
    if not isinstance(characters, list):
        raise PhoneAudioRendererError("forced character alignment is missing")
    text_parts: list[str] = []
    starts: list[Any] = []
    ends: list[Any] = []
    for item in characters:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise PhoneAudioRendererError("forced character alignment is invalid")
        part = item["text"]
        if len(part) != 1:
            raise PhoneAudioRendererError(
                "forced alignment did not return one literal character per item"
            )
        text_parts.append(part)
        starts.append(item.get("start"))
        ends.append(item.get("end"))
    if "".join(text_parts) != literal_text:
        raise PhoneAudioRendererError(
            "forced alignment does not match literal text"
        )
    return _build_alignment(literal_text, starts, ends)


def _build_alignment(
    literal_text: str,
    starts_seconds: list[Any],
    ends_seconds: list[Any],
) -> PhoneTextAlignment:
    starts: list[int] = []
    ends: list[int] = []
    previous_end = 0
    for index, (start_value, end_value) in enumerate(
        zip(starts_seconds, ends_seconds, strict=True)
    ):
        if isinstance(start_value, bool) or isinstance(end_value, bool):
            raise PhoneAudioRendererError("alignment timing is invalid")
        try:
            start_seconds = float(start_value)
            end_seconds = float(end_value)
        except (TypeError, ValueError) as exc:
            raise PhoneAudioRendererError("alignment timing is invalid") from exc
        if (
            not math.isfinite(start_seconds)
            or not math.isfinite(end_seconds)
            or start_seconds < 0
            or start_seconds > end_seconds
        ):
            raise PhoneAudioRendererError(
                f"alignment timing is invalid at character {index}"
            )
        start_ms = math.ceil(start_seconds * 1000)
        end_ms = math.ceil(end_seconds * 1000)
        if end_ms < previous_end:
            raise PhoneAudioRendererError(
                f"alignment timing is not monotonic at character {index}"
            )
        starts.append(start_ms)
        ends.append(end_ms)
        previous_end = end_ms
    return PhoneTextAlignment(
        text=literal_text,
        character_start_ms=tuple(starts),
        character_end_ms=tuple(ends),
    )


class _ArtifactStore:
    def __init__(self, root: Path, fingerprint: str) -> None:
        directory = root / fingerprint[:2] / fingerprint[2:4]
        self._directory = directory
        self.mp3_path = directory / f"{fingerprint}.mp3"
        self.alignment_path = directory / f"{fingerprint}.alignment.json"
        self.ready_path = directory / f"{fingerprint}.ready.json"

    def load_source_if_valid(self) -> bytes | None:
        if not self.mp3_path.is_file():
            return None
        try:
            if self.mp3_path.stat().st_size > MAX_TTS_RESPONSE_BYTES:
                return None
            audio = self.mp3_path.read_bytes()
            _validate_mp3(audio)
            return audio
        except (OSError, PhoneAudioRendererError):
            return None

    def load_if_valid(self, literal_text: str) -> RenderedMp3 | None:
        if not (
            self.mp3_path.is_file()
            and self.alignment_path.is_file()
            and self.ready_path.is_file()
        ):
            return None
        try:
            audio = self.mp3_path.read_bytes()
            alignment_bytes = self.alignment_path.read_bytes()
            ready = json.loads(self.ready_path.read_text(encoding="utf-8"))
            if ready != {
                "alignment_sha256": hashlib.sha256(alignment_bytes).hexdigest(),
                "audio_sha256": hashlib.sha256(audio).hexdigest(),
                "version": 1,
            }:
                return None
            _validate_mp3(audio)
            alignment_payload = json.loads(alignment_bytes)
            alignment = PhoneTextAlignment(
                text=alignment_payload["text"],
                character_start_ms=tuple(alignment_payload["character_start_ms"]),
                character_end_ms=tuple(alignment_payload["character_end_ms"]),
            )
            if alignment.text != literal_text or len(alignment.character_end_ms) != len(literal_text):
                return None
            _validate_alignment_structure(alignment)
            return RenderedMp3(path=self.mp3_path, alignment=alignment)
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def write(
        self,
        audio: bytes,
        alignment: PhoneTextAlignment,
        *,
        preserve_existing_source: bool = False,
    ) -> RenderedMp3:
        _validate_mp3(audio)
        _validate_alignment_structure(alignment)
        _ensure_private_directory(self._directory)
        alignment_bytes = json.dumps(
            {
                "character_end_ms": list(alignment.character_end_ms),
                "character_start_ms": list(alignment.character_start_ms),
                "text": alignment.text,
                "version": 1,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        ready_bytes = json.dumps(
            {
                "alignment_sha256": hashlib.sha256(alignment_bytes).hexdigest(),
                "audio_sha256": hashlib.sha256(audio).hexdigest(),
                "version": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        temporary_paths: list[Path] = []
        publication_complete = False
        try:
            audio_temp: Path | None = None
            if preserve_existing_source:
                if not self.mp3_path.is_file() or self.mp3_path.read_bytes() != audio:
                    raise PhoneAudioRendererError(
                        "durable OpenAI source artifact changed before publication"
                    )
            else:
                audio_temp = _write_private_temp(self._directory, audio)
                temporary_paths.append(audio_temp)
            alignment_temp = _write_private_temp(self._directory, alignment_bytes)
            temporary_paths.append(alignment_temp)
            ready_temp = _write_private_temp(self._directory, ready_bytes)
            temporary_paths.append(ready_temp)
            if audio_temp is not None:
                os.replace(audio_temp, self.mp3_path)
                temporary_paths.remove(audio_temp)
            os.replace(alignment_temp, self.alignment_path)
            temporary_paths.remove(alignment_temp)
            # The marker is published last; readers ignore incomplete pairs.
            os.replace(ready_temp, self.ready_path)
            temporary_paths.remove(ready_temp)
            publication_complete = True
        finally:
            for path in temporary_paths:
                path.unlink(missing_ok=True)
            if not publication_complete:
                self.ready_path.unlink(missing_ok=True)
                self.alignment_path.unlink(missing_ok=True)
                if not preserve_existing_source:
                    self.mp3_path.unlink(missing_ok=True)
        return RenderedMp3(path=self.mp3_path, alignment=alignment)

    def write_source(self, audio: bytes) -> Path:
        _validate_mp3(audio)
        if len(audio) > MAX_TTS_RESPONSE_BYTES:
            raise ProviderResponseTooLarge("TTS audio is too large")
        _ensure_private_directory(self._directory)
        temporary_path = _write_private_temp(self._directory, audio)
        try:
            os.replace(temporary_path, self.mp3_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return self.mp3_path


async def _write_artifact_owned(
    artifact: _ArtifactStore,
    audio: bytes,
    alignment: PhoneTextAlignment,
    *,
    preserve_existing_source: bool,
) -> RenderedMp3:
    task = asyncio.create_task(
        asyncio.to_thread(
            artifact.write,
            audio,
            alignment,
            preserve_existing_source=preserve_existing_source,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except BaseException as exc:
            raise exc
        raise


async def _write_source_owned(artifact: _ArtifactStore, audio: bytes) -> Path:
    task = asyncio.create_task(asyncio.to_thread(artifact.write_source, audio))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except BaseException as exc:
            raise exc
        raise


async def _await_owned(awaitable: Any) -> Any:
    """Finish one owned durable mutation before propagating cancellation."""

    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except BaseException as exc:
            raise exc
        raise


def _write_private_temp(directory: Path, content: bytes) -> Path:
    with tempfile.NamedTemporaryFile(
        prefix=".phone-render-",
        suffix=".tmp",
        dir=directory,
        delete=False,
    ) as handle:
        path = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _validate_alignment_structure(alignment: PhoneTextAlignment) -> None:
    if (
        not isinstance(alignment, PhoneTextAlignment)
        or len(alignment.character_start_ms) != len(alignment.text)
        or len(alignment.character_end_ms) != len(alignment.text)
    ):
        raise PhoneAudioRendererError("character alignment is incomplete")
    previous_end = 0
    for start, end in zip(
        alignment.character_start_ms,
        alignment.character_end_ms,
        strict=True,
    ):
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or start > end
            or end < previous_end
        ):
            raise PhoneAudioRendererError("character alignment is invalid")
        previous_end = end


def _validate_mp3(audio: bytes) -> None:
    if not isinstance(audio, bytes) or len(audio) <= 3:
        raise PhoneAudioRendererError("provider returned empty MP3 audio")
    if audio[:3] != b"ID3" and not (
        len(audio) >= 2 and audio[0] == 0xFF and audio[1] & 0xE0 == 0xE0
    ):
        raise PhoneAudioRendererError("provider did not return MP3 audio")


def _mp3_duration_seconds(audio: bytes) -> float:
    from pydub import AudioSegment

    _validate_mp3(audio)
    segment = AudioSegment.from_file(BytesIO(audio), format="mp3")
    duration = float(len(segment)) / 1000.0
    if not math.isfinite(duration) or duration <= 0:
        raise PhoneAudioRendererError("provider returned zero-duration MP3 audio")
    return duration


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PhoneAudioRendererError("provider returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise PhoneAudioRendererError("provider returned invalid JSON")
    return value


def _validate_render_identity(
    *,
    activation_id: str,
    cache_key: str,
    render_fingerprint: str,
    billing_user_id: int,
) -> None:
    if not isinstance(activation_id, str) or not activation_id or len(activation_id) > 128:
        raise PhoneAudioRendererError("activation identity is invalid")
    if not isinstance(cache_key, str) or not cache_key or len(cache_key) > 220:
        raise PhoneAudioRendererError("cache identity is invalid")
    if (
        not isinstance(render_fingerprint, str)
        or len(render_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in render_fingerprint)
    ):
        raise PhoneAudioRendererError("render fingerprint is invalid")
    if isinstance(billing_user_id, bool) or int(billing_user_id) <= 0:
        raise PhoneAudioRendererError("billing user is invalid")


def _required_key(value: Any, provider: str) -> str:
    key = str(value or "").strip()
    if not key:
        raise PhoneAudioRendererError(f"{provider} API key is not configured")
    return key


def _invoke_elevenlabs_key_getter(
    getter: Callable[..., str | None],
    voice_id: str | None,
) -> str | None:
    if voice_id is None:
        return getter()
    try:
        signature = inspect.signature(getter)
    except (TypeError, ValueError):
        return getter(voice_id)
    try:
        signature.bind(voice_id)
    except TypeError:
        return getter()
    return getter(voice_id)


def _get_elevenlabs_key(voice_id: str | None = None) -> str | None:
    from tools.tts_load_balancer import get_elevenlabs_key

    return get_elevenlabs_key(voice_id=voice_id)


def _get_openai_key() -> str | None:
    from common import openai_key

    return openai_key


def _response_excerpt(body: bytes) -> str:
    return body[:MAX_ERROR_DETAIL_BYTES].decode("utf-8", errors="replace").strip()


def _bounded_error(value: BaseException | str) -> str:
    text = str(value or "phone audio render failed").strip()
    return text[:2_000]


__all__ = [
    "AioHttpProviderTransport",
    "AurvekTtsBillingAdapter",
    "BillingAdapter",
    "PhoneAudioCacheRenderer",
    "PhoneAudioRenderAttemptRepository",
    "PhoneAudioRendererError",
    "PhoneAudioRendererNeedsAttention",
    "ProviderResponse",
    "ProviderResponseTooLarge",
    "ProviderTransport",
    "ProviderTransportError",
]
