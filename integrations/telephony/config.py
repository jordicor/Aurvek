"""Typed global configuration for the native phone channel.

Only operational defaults live in ``SYSTEM_CONFIG``.  Prompt-specific policy
is stored in ``PROMPT_PHONE_SETTINGS`` and resolved by the telephony service.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Any, Mapping

from database import get_db_connection


PREFIX = "telephony_"
SUPPORTED_TRANSPORT = "media_streams"
SUPPORTED_STT_PROVIDER = "elevenlabs"
SUPPORTED_STT_MODEL = "scribe_v2_realtime"


DEFAULT_CONFIG: dict[str, str] = {
    "telephony_enabled": "0",
    "telephony_transport": SUPPORTED_TRANSPORT,
    "telephony_stt_provider": SUPPORTED_STT_PROVIDER,
    "telephony_stt_model": SUPPORTED_STT_MODEL,
    "telephony_stt_language": "multi",
    "telephony_endpointing_ms": "700",
    "telephony_barge_in_confirmation_ms": "350",
    "telephony_max_call_seconds": "14400",
    "telephony_allowed_countries": '["US","ES"]',
    "telephony_recording_default": "0",
    "telephony_amd_default": "0",
    "telephony_reconnect_attempts": "2",
    "telephony_silence_check_seconds": "60",
    "telephony_silence_hangup_seconds": "60",
    "telephony_scheduler_jitter_seconds": "10",
    "telephony_max_concurrent_dispatches": "10",
}

# Durable subsystem state shares SYSTEM_CONFIG for atomic SQLite updates but
# is not an administrator-facing configuration value.  Keep this exclusion
# explicit so genuine unknown ``telephony_*`` config keys still fail closed.
INTERNAL_STATE_CONFIG_KEYS = frozenset(
    {
        "telephony_global_audio_revision",
        "telephony_numbers_last_sync_at",
    }
)


class TelephonyConfigError(ValueError):
    """Raised when stored or submitted phone configuration is invalid."""


@dataclass(frozen=True, slots=True)
class TelephonyConfig:
    enabled: bool = False
    transport: str = SUPPORTED_TRANSPORT
    stt_provider: str = SUPPORTED_STT_PROVIDER
    stt_model: str = SUPPORTED_STT_MODEL
    stt_language: str = "multi"
    endpointing_ms: int = 700
    barge_in_confirmation_ms: int = 350
    max_call_seconds: int = 14_400
    allowed_countries: tuple[str, ...] = ("US", "ES")
    recording_default: bool = False
    amd_default: bool = False
    reconnect_attempts: int = 2
    silence_check_seconds: int = 60
    silence_hangup_seconds: int = 60
    scheduler_jitter_seconds: int = 10
    max_concurrent_dispatches: int = 10

    def public_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["allowed_countries"] = list(self.allowed_countries)
        return result


def _parse_bool(value: Any, *, key: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise TelephonyConfigError(f"{key} must be a boolean")


def _parse_int(
    value: Any,
    *,
    key: str,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TelephonyConfigError(f"{key} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise TelephonyConfigError(f"{key} must be between {minimum} and {maximum}")
    return parsed


def _parse_countries(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise TelephonyConfigError(
                "telephony_allowed_countries must be a JSON array"
            ) from exc
    if not isinstance(value, (list, tuple)) or not value:
        raise TelephonyConfigError(
            "telephony_allowed_countries must contain at least one country"
        )
    countries: list[str] = []
    for item in value:
        country = str(item).strip().upper()
        if len(country) != 2 or not country.isalpha():
            raise TelephonyConfigError(
                "telephony_allowed_countries must use ISO alpha-2 codes"
            )
        if country not in countries:
            countries.append(country)
    return tuple(countries)


def parse_telephony_config(values: Mapping[str, Any] | None) -> TelephonyConfig:
    merged: dict[str, Any] = dict(DEFAULT_CONFIG)
    if values:
        unknown = set(values) - set(DEFAULT_CONFIG)
        if unknown:
            raise TelephonyConfigError(
                "Unsupported telephony configuration: " + ", ".join(sorted(unknown))
            )
        merged.update(values)

    transport = str(merged["telephony_transport"]).strip().lower()
    if transport != SUPPORTED_TRANSPORT:
        raise TelephonyConfigError("Only media_streams transport is supported")
    stt_provider = str(merged["telephony_stt_provider"]).strip().lower()
    if stt_provider != SUPPORTED_STT_PROVIDER:
        raise TelephonyConfigError("Only ElevenLabs streaming STT is supported")
    stt_model = str(merged["telephony_stt_model"]).strip().lower()
    if stt_model != SUPPORTED_STT_MODEL:
        raise TelephonyConfigError(
            "Only scribe_v2_realtime streaming STT is supported"
        )

    stt_language = str(merged["telephony_stt_language"]).strip()
    if not stt_language:
        raise TelephonyConfigError("telephony_stt_language cannot be empty")

    return TelephonyConfig(
        enabled=_parse_bool(merged["telephony_enabled"], key="telephony_enabled"),
        transport=transport,
        stt_provider=stt_provider,
        stt_model=stt_model,
        stt_language=stt_language,
        endpointing_ms=_parse_int(
            merged["telephony_endpointing_ms"],
            key="telephony_endpointing_ms",
            minimum=300,
            maximum=3_000,
        ),
        barge_in_confirmation_ms=_parse_int(
            merged["telephony_barge_in_confirmation_ms"],
            key="telephony_barge_in_confirmation_ms",
            minimum=100,
            maximum=2_000,
        ),
        max_call_seconds=_parse_int(
            merged["telephony_max_call_seconds"],
            key="telephony_max_call_seconds",
            minimum=60,
            maximum=86_400,
        ),
        allowed_countries=_parse_countries(merged["telephony_allowed_countries"]),
        recording_default=_parse_bool(
            merged["telephony_recording_default"],
            key="telephony_recording_default",
        ),
        amd_default=_parse_bool(
            merged["telephony_amd_default"], key="telephony_amd_default"
        ),
        reconnect_attempts=_parse_int(
            merged["telephony_reconnect_attempts"],
            key="telephony_reconnect_attempts",
            minimum=0,
            maximum=2,
        ),
        silence_check_seconds=_parse_int(
            merged["telephony_silence_check_seconds"],
            key="telephony_silence_check_seconds",
            minimum=0,
            maximum=14_400,
        ),
        silence_hangup_seconds=_parse_int(
            merged["telephony_silence_hangup_seconds"],
            key="telephony_silence_hangup_seconds",
            minimum=0,
            maximum=14_400,
        ),
        scheduler_jitter_seconds=_parse_int(
            merged["telephony_scheduler_jitter_seconds"],
            key="telephony_scheduler_jitter_seconds",
            minimum=1,
            maximum=60,
        ),
        max_concurrent_dispatches=_parse_int(
            merged["telephony_max_concurrent_dispatches"],
            key="telephony_max_concurrent_dispatches",
            minimum=1,
            maximum=100,
        ),
    )


async def _load_values(conn: Any) -> dict[str, str]:
    cursor = await conn.execute(
        "SELECT key, value FROM SYSTEM_CONFIG WHERE key GLOB 'telephony_*'"
    )
    return {
        str(row["key"]): str(row["value"])
        for row in await cursor.fetchall()
        if str(row["key"]) not in INTERNAL_STATE_CONFIG_KEYS
    }


async def load_telephony_config(*, conn: Any | None = None) -> TelephonyConfig:
    if conn is not None:
        return parse_telephony_config(await _load_values(conn))
    async with get_db_connection(readonly=True) as db_conn:
        return parse_telephony_config(await _load_values(db_conn))


def serialize_config_updates(values: Mapping[str, Any]) -> dict[str, str]:
    """Validate a partial admin update and return only submitted storage keys."""
    unknown = set(values) - set(DEFAULT_CONFIG)
    if unknown:
        raise TelephonyConfigError(
            "Unsupported telephony configuration: " + ", ".join(sorted(unknown))
        )
    merged = dict(DEFAULT_CONFIG)
    merged.update(values)
    parsed = parse_telephony_config(merged)
    public = parsed.public_dict()
    serialized = {
        "telephony_enabled": "1" if public["enabled"] else "0",
        "telephony_transport": public["transport"],
        "telephony_stt_provider": public["stt_provider"],
        "telephony_stt_model": public["stt_model"],
        "telephony_stt_language": public["stt_language"],
        "telephony_endpointing_ms": str(public["endpointing_ms"]),
        "telephony_barge_in_confirmation_ms": str(
            public["barge_in_confirmation_ms"]
        ),
        "telephony_max_call_seconds": str(public["max_call_seconds"]),
        "telephony_allowed_countries": json.dumps(
            public["allowed_countries"], separators=(",", ":")
        ),
        "telephony_recording_default": "1" if public["recording_default"] else "0",
        "telephony_amd_default": "1" if public["amd_default"] else "0",
        "telephony_reconnect_attempts": str(public["reconnect_attempts"]),
        "telephony_silence_check_seconds": str(public["silence_check_seconds"]),
        "telephony_silence_hangup_seconds": str(public["silence_hangup_seconds"]),
        "telephony_scheduler_jitter_seconds": str(public["scheduler_jitter_seconds"]),
        "telephony_max_concurrent_dispatches": str(
            public["max_concurrent_dispatches"]
        ),
    }
    return {key: serialized[key] for key in values}
