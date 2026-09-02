"""Canonical, provider-neutral prompt voice resolution.

The prompt's ``PROMPTS.voice_id`` is authoritative.  A prompt without an
explicit voice inherits the single global ``VOICES.is_default`` row.  This
module deliberately fails closed when that invariant is not satisfied; voice
channels must never pick an arbitrary row or silently switch providers.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from database import get_db_connection


class CanonicalVoiceResolutionError(ValueError):
    """A visible configuration error that prevents safe voice playback."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CanonicalVoice:
    id: int
    voice_code: str
    name: str
    tts_service: int
    service_name: str
    provider: str
    inherited_default: bool

    @property
    def elevenlabs_webrtc_compatible(self) -> bool:
        """Whether ElevenLabs Agents can reproduce this exact canonical voice."""
        return self.provider == "elevenlabs" and bool(self.voice_code)


def provider_from_service_name(service_name: str) -> str:
    """Return a stable provider key from the configured TTS service name."""
    normalized = str(service_name or "").strip().lower()
    if "elevenlabs" in normalized:
        return "elevenlabs"
    if "openai" in normalized:
        return "openai"
    normalized = re.sub(r"^tts[-_\s]*", "", normalized)
    return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_") or "unknown"


def require_elevenlabs_webrtc_compatible(voice: CanonicalVoice) -> CanonicalVoice:
    """Fail closed unless WebRTC can use the exact canonical provider and ID."""
    if not voice.elevenlabs_webrtc_compatible:
        raise CanonicalVoiceResolutionError(
            "elevenlabs_webrtc_voice_incompatible",
            "Browser voice calls are unavailable because ElevenLabs cannot reproduce "
            f"the canonical {voice.provider} voice exactly.",
        )
    return voice


async def _voice_from_row(row: Any, *, inherited_default: bool) -> CanonicalVoice:
    if row is None or row["voice_id"] is None:
        raise CanonicalVoiceResolutionError(
            "canonical_voice_missing",
            "The canonical voice is missing from the voice catalogue.",
        )
    if bool(row["deprecated"]):
        raise CanonicalVoiceResolutionError(
            "canonical_voice_deprecated",
            "The canonical voice is no longer available. Choose another canonical voice.",
        )

    voice_code = str(row["voice_code"] or "").strip()
    if not voice_code:
        raise CanonicalVoiceResolutionError(
            "canonical_voice_code_missing",
            "The canonical voice has no provider voice ID configured.",
        )

    service_name = str(row["service_name"] or "").strip()
    if row["tts_service"] is None or not service_name:
        raise CanonicalVoiceResolutionError(
            "canonical_voice_provider_missing",
            "The canonical voice has no valid TTS provider configured.",
        )

    return CanonicalVoice(
        id=int(row["voice_id"]),
        voice_code=voice_code,
        name=str(row["voice_name"] or voice_code),
        tts_service=int(row["tts_service"]),
        service_name=service_name,
        provider=provider_from_service_name(service_name),
        inherited_default=inherited_default,
    )


async def _resolve_default_voice(conn) -> CanonicalVoice:
    cursor = await conn.execute(
        """
        SELECT v.id AS voice_id, v.name AS voice_name, v.voice_code,
               v.tts_service, COALESCE(v.deprecated, 0) AS deprecated,
               s.name AS service_name
        FROM VOICES v
        LEFT JOIN SERVICES s ON s.id = v.tts_service
        WHERE COALESCE(v.is_default, 0) = 1
        ORDER BY v.id ASC
        """
    )
    rows = await cursor.fetchall()
    if len(rows) != 1:
        raise CanonicalVoiceResolutionError(
            "canonical_voice_default_count",
            "Voice is unavailable until exactly one global default voice is selected.",
        )
    return await _voice_from_row(rows[0], inherited_default=True)


async def resolve_default_voice(*, conn=None) -> CanonicalVoice:
    """Resolve the one global default voice, with no arbitrary fallback."""
    if conn is not None:
        return await _resolve_default_voice(conn)
    async with get_db_connection(readonly=True) as db_conn:
        return await _resolve_default_voice(db_conn)


async def _resolve_prompt_voice(conn, prompt_id: int) -> CanonicalVoice:
    cursor = await conn.execute(
        """
        SELECT p.id AS prompt_id, p.voice_id AS configured_voice_id,
               v.id AS voice_id, v.name AS voice_name, v.voice_code,
               v.tts_service, COALESCE(v.deprecated, 0) AS deprecated,
               s.name AS service_name
        FROM PROMPTS p
        LEFT JOIN VOICES v ON v.id = p.voice_id
        LEFT JOIN SERVICES s ON s.id = v.tts_service
        WHERE p.id = ?
        """,
        (int(prompt_id),),
    )
    row = await cursor.fetchone()
    if row is None:
        raise CanonicalVoiceResolutionError(
            "prompt_not_found",
            "The conversation prompt no longer exists.",
        )
    if row["configured_voice_id"] is None:
        return await _resolve_default_voice(conn)
    if row["voice_id"] is None:
        raise CanonicalVoiceResolutionError(
            "canonical_voice_missing",
            "The prompt's canonical voice is missing from the voice catalogue.",
        )
    return await _voice_from_row(row, inherited_default=False)


async def resolve_prompt_voice(prompt_id: int, *, conn=None) -> CanonicalVoice:
    """Resolve a prompt's explicit voice or the single global default voice."""
    if conn is not None:
        return await _resolve_prompt_voice(conn, prompt_id)
    async with get_db_connection(readonly=True) as db_conn:
        return await _resolve_prompt_voice(db_conn, prompt_id)


async def _resolve_catalog_voice(conn, voice_code: str) -> CanonicalVoice | None:
    cursor = await conn.execute(
        """
        SELECT v.id AS voice_id, v.name AS voice_name, v.voice_code,
               v.tts_service, COALESCE(v.deprecated, 0) AS deprecated,
               s.name AS service_name
        FROM VOICES v
        LEFT JOIN SERVICES s ON s.id = v.tts_service
        WHERE v.voice_code = ?
        ORDER BY v.id ASC
        """,
        (str(voice_code or "").strip(),),
    )
    rows = await cursor.fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise CanonicalVoiceResolutionError(
            "voice_code_ambiguous",
            "The requested voice ID maps to more than one TTS provider.",
        )
    return await _voice_from_row(rows[0], inherited_default=False)


async def resolve_catalog_voice(voice_code: str, *, conn=None) -> CanonicalVoice | None:
    """Resolve a non-prompt voice through the same provider catalogue."""
    if conn is not None:
        return await _resolve_catalog_voice(conn, voice_code)
    async with get_db_connection(readonly=True) as db_conn:
        return await _resolve_catalog_voice(db_conn, voice_code)
