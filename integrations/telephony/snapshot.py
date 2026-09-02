"""Immutable conversational/voice configuration captured for one phone call."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from typing import Any, Mapping

from ai_runtime.reasoning import (
    ReasoningValidationError,
    parse_reasoning_selection,
    resolve_and_validate,
)
from ai_runtime.voice_resolution import CanonicalVoice, resolve_prompt_voice
from database import get_db_connection
from integrations.telephony.settings import (
    EffectivePhoneSettings,
    resolve_effective_phone_settings,
)
from integrations.telephony.greetings import (
    PHONE_CACHE_MP3_FORMAT,
    normalize_phone_cache_tts_profile,
)
from llm_catalog import normalize_capabilities
from tools.tts_config import TTSProfile, VALID_FORMATS, get_tts_profile


_SUPPORTED_PHONE_TTS_PROVIDERS = frozenset({"elevenlabs", "openai"})
_VALID_OUTPUT_FORMATS = frozenset(item[0] for item in VALID_FORMATS)
ELEVENLABS_PHONE_TTS_MODEL_ID = "eleven_flash_v2_5"
ELEVENLABS_PHONE_TTS_OUTPUT_FORMAT = "ulaw_8000"
OPENAI_REALTIME_DEFAULT_VOICE = "marin"
OPENAI_REALTIME_VOICES = frozenset(
    {
        "alloy",
        "ash",
        "ballad",
        "coral",
        "echo",
        "sage",
        "shimmer",
        "verse",
        "marin",
        "cedar",
    }
)


class PhoneSnapshotError(ValueError):
    """A call cannot safely reproduce its captured runtime configuration."""


class PhoneAudioRevisionUnavailable(PhoneSnapshotError):
    """No complete activated phone-audio revision can be captured."""


@dataclass(frozen=True, slots=True)
class ConversationPhoneSnapshot:
    conversation_id: int
    owner_user_id: int
    prompt_id: int
    # ``llm_id`` remains the captured conversation-model fence.  Phone may use
    # another model, but changing CONVERSATIONS.llm_id still invalidates a turn.
    llm_id: int
    runtime_llm_id: int
    runtime_kind: str
    runtime_model: str
    reasoning_selection: dict[str, Any]
    phone_realtime_voice: str | None
    canonical_voice: CanonicalVoice
    tts_profile: TTSProfile
    phone_settings: dict[str, Any]
    audio_revision: int
    captured_at: str

    def as_dict(self) -> dict[str, Any]:
        voice = asdict(self.canonical_voice)
        profile = _serialize_tts_profile(self.tts_profile)
        live_profile = _phone_live_tts_profile(
            self.canonical_voice.provider,
            self.tts_profile,
        )
        # Effective phone settings remain top-level because the durable job
        # repository resolves recording/AMD from this same immutable snapshot.
        return {
            **self.phone_settings,
            "conversation_id": self.conversation_id,
            "owner_user_id": self.owner_user_id,
            "prompt_id": self.prompt_id,
            "llm_id": self.llm_id,
            "runtime_llm_id": self.runtime_llm_id,
            "runtime_kind": self.runtime_kind,
            "runtime_model": self.runtime_model,
            "reasoning_selection": dict(self.reasoning_selection),
            "phone_realtime_voice": self.phone_realtime_voice,
            "canonical_voice": voice,
            "provider_key": self.canonical_voice.provider,
            "provider_voice_id": self.canonical_voice.voice_code,
            "tts_profile": profile,
            "live_tts_profile": _serialize_tts_profile(live_profile),
            "audio_revision": _positive_audio_revision(self.audio_revision),
            "captured_at": self.captured_at,
        }


async def build_conversation_phone_snapshot(
    conversation_id: int,
    *,
    expected_owner_user_id: int | None = None,
    conn: Any | None = None,
) -> ConversationPhoneSnapshot:
    """Resolve one canonical prompt/model/voice/settings snapshot atomically."""

    if conn is not None:
        return await _build_snapshot(
            conn,
            conversation_id,
            expected_owner_user_id=expected_owner_user_id,
        )
    async with get_db_connection(readonly=True) as db_conn:
        return await _build_snapshot(
            db_conn,
            conversation_id,
            expected_owner_user_id=expected_owner_user_id,
        )


async def _build_snapshot(
    conn: Any,
    conversation_id: int,
    *,
    expected_owner_user_id: int | None,
) -> ConversationPhoneSnapshot:
    cursor = await conn.execute(
        """
        SELECT c.id, c.user_id, c.llm_id,
               COALESCE(c.role_id, ud.current_prompt_id) AS prompt_id,
               COALESCE(c.locked, 0) AS locked,
               COALESCE(c.is_incognito, 0) AS is_incognito,
               COALESCE(u.is_enabled, 0) AS owner_enabled
        FROM CONVERSATIONS c
        JOIN USERS u ON u.id=c.user_id
        LEFT JOIN USER_DETAILS ud ON ud.user_id=c.user_id
        WHERE c.id=?
        """,
        (int(conversation_id),),
    )
    row = await cursor.fetchone()
    if row is None:
        raise PhoneSnapshotError("Conversation not found")
    values = dict(row)
    owner_user_id = int(values["user_id"])
    if (
        expected_owner_user_id is not None
        and owner_user_id != int(expected_owner_user_id)
    ):
        raise PhoneSnapshotError("Conversation is not owned by this user")
    if not bool(values["owner_enabled"]):
        raise PhoneSnapshotError("Conversation owner is disabled")
    if bool(values["locked"]):
        raise PhoneSnapshotError("Conversation is locked")
    if bool(values["is_incognito"]):
        raise PhoneSnapshotError("Incognito conversations cannot use phone calls")
    if values["prompt_id"] is None:
        raise PhoneSnapshotError("Conversation has no prompt")
    if values["llm_id"] is None:
        raise PhoneSnapshotError("Conversation has no model")

    prompt_id = int(values["prompt_id"])
    (
        runtime_llm_id,
        runtime_kind,
        runtime_model,
        reasoning_selection,
        phone_realtime_voice,
    ) = await _resolve_phone_model_selection(
        conn,
        prompt_id=prompt_id,
        conversation_llm_id=int(values["llm_id"]),
    )
    voice = await resolve_prompt_voice(prompt_id, conn=conn)
    if voice.provider not in _SUPPORTED_PHONE_TTS_PROVIDERS:
        raise PhoneSnapshotError(
            f"Canonical TTS provider {voice.provider!r} is unsupported for phone audio"
        )
    phone_settings = await resolve_effective_phone_settings(
        prompt_id,
        conn=conn,
    )
    audio_revision = await _resolve_active_audio_revision(conn, prompt_id)
    profile = normalize_phone_cache_tts_profile(
        await get_tts_profile("external")
    )
    _validate_tts_profile(profile)
    _validate_live_tts_profile(
        _phone_live_tts_profile(voice.provider, profile),
        provider=voice.provider,
    )
    return ConversationPhoneSnapshot(
        conversation_id=int(values["id"]),
        owner_user_id=owner_user_id,
        prompt_id=prompt_id,
        llm_id=int(values["llm_id"]),
        runtime_llm_id=runtime_llm_id,
        runtime_kind=runtime_kind,
        runtime_model=runtime_model,
        reasoning_selection=reasoning_selection,
        phone_realtime_voice=phone_realtime_voice,
        canonical_voice=voice,
        tts_profile=profile,
        phone_settings=phone_settings.as_dict(),
        audio_revision=audio_revision,
        captured_at=datetime.now(UTC).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
    )


async def _resolve_phone_model_selection(
    conn: Any,
    *,
    prompt_id: int,
    conversation_llm_id: int,
) -> tuple[int, str, str, dict[str, Any], str | None]:
    configuration = await _load_prompt_model_configuration(conn, prompt_id)
    forced_llm_id = configuration["forced_llm_id"]
    forced_reasoning_raw = configuration["forced_reasoning_json"]
    phone_llm_id = configuration["phone_llm_id"]
    phone_reasoning_raw = configuration["phone_reasoning_json"]
    phone_realtime_voice_raw = configuration["phone_realtime_voice"]

    if forced_reasoning_raw is not None and forced_llm_id is None:
        raise PhoneSnapshotError(
            "Prompt forced reasoning requires a forced model"
        )

    runtime_llm_id = (
        int(phone_llm_id)
        if phone_llm_id is not None
        else int(conversation_llm_id)
    )
    model = await _load_phone_runtime_model(conn, runtime_llm_id)

    if phone_reasoning_raw is not None:
        reasoning_raw = phone_reasoning_raw
    elif phone_llm_id is None and forced_reasoning_raw is not None:
        reasoning_raw = forced_reasoning_raw
    else:
        reasoning_raw = None

    try:
        selection = parse_reasoning_selection(_loads_reasoning(reasoning_raw))
        validated = resolve_and_validate(selection, model["capabilities"])
    except (ReasoningValidationError, TypeError, ValueError) as exc:
        raise PhoneSnapshotError(
            "Prompt phone reasoning is invalid for its runtime model"
        ) from exc
    runtime_kind = str(model["runtime_kind"])
    if runtime_kind == "openai_realtime":
        phone_realtime_voice = str(
            phone_realtime_voice_raw or OPENAI_REALTIME_DEFAULT_VOICE
        ).strip().lower()
        if phone_realtime_voice not in OPENAI_REALTIME_VOICES:
            raise PhoneSnapshotError("Prompt Realtime voice is invalid")
    else:
        # A stale voice value cannot alter the standard canonical-voice path.
        phone_realtime_voice = None
    return (
        runtime_llm_id,
        runtime_kind,
        str(model["model"]),
        validated.to_dict(),
        phone_realtime_voice,
    )


async def _load_prompt_model_configuration(
    conn: Any,
    prompt_id: int,
) -> dict[str, Any]:
    cursor = await conn.execute("PRAGMA table_info(PROMPTS)")
    columns = {str(row[1]) for row in await cursor.fetchall()}
    names = (
        "forced_llm_id",
        "forced_reasoning_json",
        "phone_llm_id",
        "phone_reasoning_json",
        "phone_realtime_voice",
    )
    projections = [
        name if name in columns else f"NULL AS {name}"
        for name in names
    ]
    cursor = await conn.execute(
        f"SELECT {', '.join(projections)} FROM PROMPTS WHERE id=?",
        (int(prompt_id),),
    )
    row = await cursor.fetchone()
    if row is None:
        raise PhoneSnapshotError("Conversation prompt was not found")
    return {name: row[index] for index, name in enumerate(names)}


async def _load_phone_runtime_model(conn: Any, llm_id: int) -> dict[str, Any]:
    cursor = await conn.execute("PRAGMA table_info(LLM)")
    columns = {str(row[1]) for row in await cursor.fetchall()}

    def column(name: str, fallback: str = "NULL") -> str:
        return name if name in columns else f"{fallback} AS {name}"

    enabled_projection = (
        "COALESCE(enabled,1) AS enabled"
        if "enabled" in columns
        else "1 AS enabled"
    )
    cursor = await conn.execute(
        f"""
        SELECT id,machine,model,
               {enabled_projection},
               {column('provider_key')},
               {column('provider_model_id')},
               {column('raw_metadata_json')},
               {column('capabilities_json')},
               {column('manual_overrides_json')}
        FROM LLM WHERE id=?
        """,
        (int(llm_id),),
    )
    row = await cursor.fetchone()
    if row is None:
        raise PhoneSnapshotError("Phone runtime model was not found")
    values = tuple(row)
    machine = str(values[1] or "")
    model_id = str(values[2] or "")
    if not bool(values[3]):
        raise PhoneSnapshotError("Phone runtime model is disabled")
    if machine in {"GPTSub", "GranSabio"}:
        raise PhoneSnapshotError(
            f"{machine} cannot be used as a phone runtime model"
        )
    capabilities = normalize_capabilities(
        values[4] or machine,
        values[5] or model_id,
        _loads_object(values[6]),
        _loads_object(values[7]),
        _loads_object(values[8]),
    )
    runtime = capabilities.get("runtime") or {}
    runtime_kind = str(runtime.get("kind") or "")
    if runtime_kind not in {"standard", "openai_realtime"}:
        raise PhoneSnapshotError("Phone runtime model type is unsupported")
    return {
        "id": int(values[0]),
        "machine": machine,
        "model": model_id,
        "capabilities": capabilities,
        "runtime_kind": runtime_kind,
    }


def _loads_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise PhoneSnapshotError("Stored model capabilities are invalid") from exc
    if not isinstance(decoded, dict):
        raise PhoneSnapshotError("Stored model capabilities are invalid")
    return decoded


def _loads_reasoning(value: Any) -> Mapping[str, Any] | str | None:
    if value is None or isinstance(value, Mapping):
        return value
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise PhoneSnapshotError("Stored prompt reasoning is invalid") from exc
    if not isinstance(decoded, (dict, str)):
        raise PhoneSnapshotError("Stored prompt reasoning is invalid")
    return decoded


async def _resolve_active_audio_revision(conn: Any, prompt_id: int) -> int:
    """Capture the one activated cache revision used by this call snapshot."""

    cursor = await conn.execute(
        "SELECT active_audio_revision FROM PROMPT_PHONE_SETTINGS WHERE prompt_id=?",
        (int(prompt_id),),
    )
    row = await cursor.fetchone()
    value = None if row is None else row[0]
    if value is None:
        cursor = await conn.execute(
            "SELECT value FROM SYSTEM_CONFIG "
            "WHERE key='telephony_global_audio_revision'"
        )
        row = await cursor.fetchone()
        value = None if row is None else row[0]
    return _positive_audio_revision(value)


def canonical_voice_from_snapshot(values: Mapping[str, Any]) -> CanonicalVoice:
    raw = values.get("canonical_voice")
    if not isinstance(raw, Mapping):
        raise PhoneSnapshotError("Call snapshot has no canonical voice")
    try:
        voice = CanonicalVoice(
            id=int(raw["id"]),
            voice_code=_bounded_text(raw["voice_code"], "voice_code", 400),
            name=_bounded_text(raw["name"], "voice name", 400),
            tts_service=int(raw["tts_service"]),
            service_name=_bounded_text(raw["service_name"], "service name", 200),
            provider=_bounded_text(raw["provider"], "provider", 100).lower(),
            inherited_default=_strict_bool(raw["inherited_default"], "inherited_default"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, PhoneSnapshotError):
            raise
        raise PhoneSnapshotError("Call snapshot canonical voice is invalid") from exc
    if voice.id <= 0 or voice.tts_service <= 0:
        raise PhoneSnapshotError("Call snapshot canonical voice identifiers are invalid")
    if voice.provider not in _SUPPORTED_PHONE_TTS_PROVIDERS:
        raise PhoneSnapshotError("Call snapshot TTS provider is unsupported")
    if values.get("provider_key") != voice.provider:
        raise PhoneSnapshotError("Call snapshot provider identity does not match")
    if values.get("provider_voice_id") != voice.voice_code:
        raise PhoneSnapshotError("Call snapshot voice identity does not match")
    return voice


def runtime_llm_id_from_snapshot(values: Mapping[str, Any]) -> int:
    """Return the frozen phone model, inheriting legacy snapshot ``llm_id``."""

    raw = values.get("runtime_llm_id", values.get("llm_id"))
    if isinstance(raw, bool):
        raise PhoneSnapshotError("Call snapshot runtime model is invalid")
    try:
        llm_id = int(raw)
    except (TypeError, ValueError) as exc:
        raise PhoneSnapshotError("Call snapshot runtime model is invalid") from exc
    if llm_id <= 0:
        raise PhoneSnapshotError("Call snapshot runtime model is invalid")
    return llm_id


def runtime_kind_from_snapshot(values: Mapping[str, Any]) -> str:
    """Return the captured runtime route; legacy calls use standard dispatch."""

    kind = str(values.get("runtime_kind") or "standard").strip().lower()
    if kind not in {"standard", "openai_realtime"}:
        raise PhoneSnapshotError("Call snapshot runtime kind is invalid")
    return kind


def runtime_model_from_snapshot(values: Mapping[str, Any]) -> str:
    """Return the frozen provider model used by a native phone runtime."""

    model = str(values.get("runtime_model") or "").strip()
    if not model or len(model) > 200:
        raise PhoneSnapshotError("Call snapshot runtime model is invalid")
    return model


def realtime_voice_from_snapshot(values: Mapping[str, Any]) -> str | None:
    """Return the OpenAI voice only for a native Realtime call snapshot."""

    if runtime_kind_from_snapshot(values) != "openai_realtime":
        return None
    voice = str(
        values.get("phone_realtime_voice") or OPENAI_REALTIME_DEFAULT_VOICE
    ).strip().lower()
    if voice not in OPENAI_REALTIME_VOICES:
        raise PhoneSnapshotError("Call snapshot Realtime voice is invalid")
    return voice


def reasoning_selection_from_snapshot(values: Mapping[str, Any]) -> dict[str, Any]:
    """Return a canonical selection; old snapshots use provider defaults."""

    try:
        return parse_reasoning_selection(
            values.get("reasoning_selection")
        ).to_dict()
    except ReasoningValidationError as exc:
        raise PhoneSnapshotError(
            "Call snapshot reasoning selection is invalid"
        ) from exc


def tts_profile_from_snapshot(values: Mapping[str, Any]) -> TTSProfile:
    raw = values.get("tts_profile")
    if not isinstance(raw, Mapping):
        raise PhoneSnapshotError("Call snapshot has no TTS profile")
    profile = _parse_tts_profile(raw)
    _validate_tts_profile(profile)
    return profile


def live_tts_profile_from_snapshot(values: Mapping[str, Any]) -> TTSProfile:
    """Return the captured live profile without changing legacy call snapshots.

    Older durable jobs have only ``tts_profile``.  They retain that exact
    provider/profile path, while newly captured ElevenLabs calls explicitly
    select Flash and Twilio-native raw PCMU.
    """

    raw = values.get("live_tts_profile")
    if raw is None:
        return tts_profile_from_snapshot(values)
    if not isinstance(raw, Mapping):
        raise PhoneSnapshotError("Call snapshot live TTS profile is invalid")
    profile = _parse_tts_profile(raw)
    provider = canonical_voice_from_snapshot(values).provider
    _validate_live_tts_profile(profile, provider=provider)
    return profile


def _parse_tts_profile(raw: Mapping[str, Any]) -> TTSProfile:
    try:
        schedule_raw = raw.get("chunk_schedule", [120, 160, 250, 290])
        if not isinstance(schedule_raw, (list, tuple)) or not schedule_raw:
            raise PhoneSnapshotError("Call snapshot chunk schedule is invalid")
        schedule = [int(item) for item in schedule_raw]
        if any(item <= 0 or item > 10_000 for item in schedule):
            raise PhoneSnapshotError("Call snapshot chunk schedule is invalid")
        profile = TTSProfile(
            model_id=_bounded_text(raw["model_id"], "TTS model", 200),
            output_format=_bounded_text(raw["output_format"], "TTS format", 100),
            stability=float(raw["stability"]),
            similarity_boost=float(raw["similarity_boost"]),
            ws_enabled=_strict_bool(raw.get("ws_enabled", False), "ws_enabled"),
            chunk_schedule=schedule,
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, PhoneSnapshotError):
            raise
        raise PhoneSnapshotError("Call snapshot TTS profile is invalid") from exc
    return profile


def phone_settings_from_snapshot(
    values: Mapping[str, Any],
) -> EffectivePhoneSettings:
    try:
        milestones_raw = values["warning_milestones_seconds"]
        if not isinstance(milestones_raw, (list, tuple)):
            raise PhoneSnapshotError("Call snapshot warning milestones are invalid")
        milestones = tuple(int(item) for item in milestones_raw)
        maximum = int(values["max_duration_seconds"])
        if maximum <= 0 or any(
            item <= 0 or item >= maximum for item in milestones
        ):
            raise PhoneSnapshotError("Call snapshot phone duration is invalid")
        silence_prompt = _optional_positive_int(
            values.get("silence_prompt_seconds"),
            "silence_prompt_seconds",
        )
        silence_hangup = _optional_positive_int(
            values.get("silence_hangup_seconds"),
            "silence_hangup_seconds",
        )
        if (silence_prompt is None) != (silence_hangup is None):
            raise PhoneSnapshotError("Call snapshot silence settings are incomplete")
        settings = EffectivePhoneSettings(
            stt_locale=_bounded_text(values["stt_locale"], "STT locale", 100),
            endpointing_ms=_bounded_int(
                values.get("endpointing_ms", 700), "endpointing_ms", 300, 3_000
            ),
            barge_in_confirmation_ms=_bounded_int(
                values.get("barge_in_confirmation_ms", 350),
                "barge_in_confirmation_ms",
                100,
                2_000,
            ),
            interruptible=_strict_bool(
                values.get("interruptible", True), "interruptible"
            ),
            interrupt_sensitivity=_bounded_choice(
                values.get("interrupt_sensitivity", "normal"),
                "interrupt sensitivity",
                {"low", "normal", "high"},
            ),
            ignore_backchannels=_strict_bool(
                values.get("ignore_backchannels", True), "ignore_backchannels"
            ),
            max_duration_seconds=maximum,
            warning_milestones_seconds=milestones,
            silence_prompt_seconds=silence_prompt,
            silence_hangup_seconds=silence_hangup,
            ai_initiation_mode=_bounded_choice(
                values["ai_initiation_mode"],
                "ai initiation mode",
                {"on_request", "proactive", "disabled"},
            ),
            inbound_greeting_mode=_bounded_choice(
                values["inbound_greeting_mode"],
                "inbound greeting mode",
                {"inherit", "fixed", "random"},
            ),
            outbound_greeting_mode=_bounded_choice(
                values["outbound_greeting_mode"],
                "outbound greeting mode",
                {"inherit", "fixed", "random"},
            ),
            recording_default=_strict_bool(
                values["recording_default"], "recording_default"
            ),
            amd_default=_strict_bool(values["amd_default"], "amd_default"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, PhoneSnapshotError):
            raise
        raise PhoneSnapshotError("Call snapshot phone settings are invalid") from exc
    return settings


def _validate_tts_profile(profile: TTSProfile) -> None:
    if (
        profile.output_format not in _VALID_OUTPUT_FORMATS
        or profile.output_format != PHONE_CACHE_MP3_FORMAT
    ):
        raise PhoneSnapshotError("Call snapshot TTS output format is unsupported")
    if not 0 <= float(profile.stability) <= 1:
        raise PhoneSnapshotError("Call snapshot TTS stability is invalid")
    if not 0 <= float(profile.similarity_boost) <= 1:
        raise PhoneSnapshotError("Call snapshot TTS similarity is invalid")


def _validate_live_tts_profile(profile: TTSProfile, *, provider: str) -> None:
    if provider == "elevenlabs":
        if (
            profile.model_id != ELEVENLABS_PHONE_TTS_MODEL_ID
            or profile.output_format != ELEVENLABS_PHONE_TTS_OUTPUT_FORMAT
        ):
            raise PhoneSnapshotError(
                "Call snapshot ElevenLabs live TTS profile is unsupported"
            )
    else:
        _validate_tts_profile(profile)
    if not 0 <= float(profile.stability) <= 1:
        raise PhoneSnapshotError("Call snapshot TTS stability is invalid")
    if not 0 <= float(profile.similarity_boost) <= 1:
        raise PhoneSnapshotError("Call snapshot TTS similarity is invalid")


def _phone_live_tts_profile(
    provider: str,
    cache_profile: TTSProfile,
) -> TTSProfile:
    if str(provider).strip().lower() != "elevenlabs":
        return cache_profile
    return TTSProfile(
        model_id=ELEVENLABS_PHONE_TTS_MODEL_ID,
        output_format=ELEVENLABS_PHONE_TTS_OUTPUT_FORMAT,
        stability=cache_profile.stability,
        similarity_boost=cache_profile.similarity_boost,
        ws_enabled=False,
        chunk_schedule=list(cache_profile.chunk_schedule),
    )


def _serialize_tts_profile(profile: TTSProfile) -> dict[str, Any]:
    return {
        "model_id": profile.model_id,
        "output_format": profile.output_format,
        "stability": profile.stability,
        "similarity_boost": profile.similarity_boost,
        "ws_enabled": bool(profile.ws_enabled),
        "chunk_schedule": list(profile.chunk_schedule),
    }


def _bounded_text(value: Any, field: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 32 for character in normalized)
    ):
        raise PhoneSnapshotError(f"Call snapshot {field} is invalid")
    return normalized


def _strict_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    raise PhoneSnapshotError(f"Call snapshot {field} is invalid")


def _optional_positive_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PhoneSnapshotError(f"Call snapshot {field} is invalid") from exc
    if parsed <= 0:
        raise PhoneSnapshotError(f"Call snapshot {field} is invalid")
    return parsed


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise PhoneSnapshotError(f"Call snapshot {field} is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PhoneSnapshotError(f"Call snapshot {field} is invalid") from exc
    if not minimum <= parsed <= maximum:
        raise PhoneSnapshotError(f"Call snapshot {field} is invalid")
    return parsed


def _positive_audio_revision(value: Any) -> int:
    if isinstance(value, bool):
        raise PhoneAudioRevisionUnavailable(
            "Phone audio cache has no active revision"
        )
    try:
        revision = int(value)
    except (TypeError, ValueError) as exc:
        raise PhoneAudioRevisionUnavailable(
            "Phone audio cache has no active revision"
        ) from exc
    if revision <= 0:
        raise PhoneAudioRevisionUnavailable(
            "Phone audio cache revision is invalid"
        )
    return revision


def _bounded_choice(value: Any, field: str, allowed: set[str]) -> str:
    normalized = _bounded_text(value, field, 100).lower()
    if normalized not in allowed:
        raise PhoneSnapshotError(f"Call snapshot {field} is invalid")
    return normalized


__all__ = [
    "ConversationPhoneSnapshot",
    "ELEVENLABS_PHONE_TTS_MODEL_ID",
    "ELEVENLABS_PHONE_TTS_OUTPUT_FORMAT",
    "OPENAI_REALTIME_DEFAULT_VOICE",
    "OPENAI_REALTIME_VOICES",
    "PhoneAudioRevisionUnavailable",
    "PhoneSnapshotError",
    "build_conversation_phone_snapshot",
    "canonical_voice_from_snapshot",
    "live_tts_profile_from_snapshot",
    "phone_settings_from_snapshot",
    "realtime_voice_from_snapshot",
    "reasoning_selection_from_snapshot",
    "runtime_kind_from_snapshot",
    "runtime_model_from_snapshot",
    "runtime_llm_id_from_snapshot",
    "tts_profile_from_snapshot",
]
