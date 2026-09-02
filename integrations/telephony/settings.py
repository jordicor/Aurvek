"""Effective prompt settings for Aurvek's native phone channel.

This module performs only deterministic resolution.  It does not persist
runtime state, start timers, or talk to a telephony provider.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Any, Mapping

from database import get_db_connection
from .config import TelephonyConfig, load_telephony_config


DEFAULT_WARNING_MILESTONES_SECONDS = (900, 300, 180, 60)


class TelephonySettingsError(ValueError):
    """Raised when stored prompt phone settings cannot be resolved safely."""


@dataclass(frozen=True, slots=True)
class EffectivePhoneSettings:
    stt_locale: str
    endpointing_ms: int
    barge_in_confirmation_ms: int
    interruptible: bool
    interrupt_sensitivity: str
    ignore_backchannels: bool
    max_duration_seconds: int
    warning_milestones_seconds: tuple[int, ...]
    silence_prompt_seconds: int | None
    silence_hangup_seconds: int | None
    ai_initiation_mode: str
    inbound_greeting_mode: str
    outbound_greeting_mode: str
    recording_default: bool
    amd_default: bool

    @property
    def silence_enabled(self) -> bool:
        return (
            self.silence_prompt_seconds is not None
            and self.silence_hangup_seconds is not None
        )

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["warning_milestones_seconds"] = list(
            self.warning_milestones_seconds
        )
        result["silence_enabled"] = self.silence_enabled
        return result


def _positive_int(value: Any, *, key: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TelephonySettingsError(f"{key} must be an integer") from exc
    if parsed <= 0:
        raise TelephonySettingsError(f"{key} must be greater than zero")
    return parsed


def _stored_bool(value: Any, *, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise TelephonySettingsError(f"{key} must be a boolean")


def _choice(value: Any, *, key: str, allowed: set[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in allowed:
        raise TelephonySettingsError(
            f"{key} must be one of: {', '.join(sorted(allowed))}"
        )
    return normalized


def _parse_milestones(value: Any, *, max_duration_seconds: int) -> tuple[int, ...]:
    if value is None:
        value = DEFAULT_WARNING_MILESTONES_SECONDS
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise TelephonySettingsError(
                "warning_milestones_json must be a JSON array"
            ) from exc
    if not isinstance(value, (list, tuple)):
        raise TelephonySettingsError(
            "warning_milestones_json must be a JSON array"
        )

    parsed = {
        _positive_int(item, key="warning milestone")
        for item in value
    }
    # A warning at or above the whole call duration would fire immediately and
    # is not useful.  Shorter prompt limits therefore retain only reachable
    # milestones.
    return tuple(
        milestone
        for milestone in sorted(parsed, reverse=True)
        if milestone < max_duration_seconds
    )


def resolve_phone_settings(
    global_config: TelephonyConfig,
    prompt_values: Mapping[str, Any] | None,
) -> EffectivePhoneSettings:
    """Merge one prompt row over administrative limits.

    No prompt row means full inheritance.  An existing prompt row with both
    silence columns NULL explicitly disables silence handling; positive values
    override only when they remain within the administrative maxima.
    """
    values = dict(prompt_values) if prompt_values is not None else None

    endpointing_value = values.get("endpointing_ms") if values else None
    endpointing_ms = global_config.endpointing_ms
    if endpointing_value is not None:
        endpointing_ms = _positive_int(endpointing_value, key="endpointing_ms")
        if not 300 <= endpointing_ms <= 3_000:
            raise TelephonySettingsError(
                "endpointing_ms must be between 300 and 3000"
            )

    interruptible = (
        _stored_bool(values.get("interruptible", 1), key="interruptible")
        if values is not None
        else True
    )
    interrupt_sensitivity = _choice(
        values.get("interrupt_sensitivity", "normal")
        if values is not None
        else "normal",
        key="interrupt_sensitivity",
        allowed={"low", "normal", "high"},
    )
    sensitivity_factor = {"low": 1.75, "normal": 1.0, "high": 0.5}[
        interrupt_sensitivity
    ]
    barge_in_confirmation_ms = max(
        100,
        min(2_000, round(global_config.barge_in_confirmation_ms * sensitivity_factor)),
    )
    ignore_backchannels = (
        _stored_bool(
            values.get("ignore_backchannels", 1), key="ignore_backchannels"
        )
        if values is not None
        else True
    )

    prompt_max = values.get("max_duration_seconds") if values else None
    max_duration = global_config.max_call_seconds
    if prompt_max is not None:
        max_duration = _positive_int(prompt_max, key="max_duration_seconds")
        if max_duration > global_config.max_call_seconds:
            raise TelephonySettingsError(
                "max_duration_seconds cannot exceed the administrative maximum"
            )

    milestones_value = (
        values.get("warning_milestones_json")
        if values is not None
        else DEFAULT_WARNING_MILESTONES_SECONDS
    )
    milestones = _parse_milestones(
        milestones_value,
        max_duration_seconds=max_duration,
    )

    if values is None:
        silence_prompt = global_config.silence_check_seconds or None
        silence_hangup = global_config.silence_hangup_seconds or None
    else:
        raw_prompt = values.get("silence_prompt_seconds")
        raw_hangup = values.get("silence_hangup_seconds")
        if raw_prompt is None and raw_hangup is None:
            silence_prompt = None
            silence_hangup = None
        elif raw_prompt is None or raw_hangup is None:
            raise TelephonySettingsError(
                "silence prompt and hangup seconds must both be set or both be NULL"
            )
        else:
            silence_prompt = _positive_int(
                raw_prompt, key="silence_prompt_seconds"
            )
            silence_hangup = _positive_int(
                raw_hangup, key="silence_hangup_seconds"
            )
            if silence_prompt > global_config.silence_check_seconds:
                raise TelephonySettingsError(
                    "silence_prompt_seconds cannot exceed the administrative maximum"
                )
            if silence_hangup > global_config.silence_hangup_seconds:
                raise TelephonySettingsError(
                    "silence_hangup_seconds cannot exceed the administrative maximum"
                )

    stt_locale = str(
        values.get("stt_locale", "auto") if values is not None else "auto"
    ).strip()
    if not stt_locale:
        raise TelephonySettingsError("stt_locale cannot be empty")
    if stt_locale.lower() == "auto":
        stt_locale = global_config.stt_language

    ai_initiation_mode = _choice(
        values.get("ai_initiation_mode", "on_request")
        if values is not None
        else "on_request",
        key="ai_initiation_mode",
        allowed={"on_request", "proactive", "disabled"},
    )
    inbound_greeting_mode = _choice(
        values.get("inbound_greeting_mode", "inherit")
        if values is not None
        else "inherit",
        key="inbound_greeting_mode",
        allowed={"inherit", "fixed", "random"},
    )
    outbound_greeting_mode = _choice(
        values.get("outbound_greeting_mode", "inherit")
        if values is not None
        else "inherit",
        key="outbound_greeting_mode",
        allowed={"inherit", "fixed", "random"},
    )

    return EffectivePhoneSettings(
        stt_locale=stt_locale,
        endpointing_ms=endpointing_ms,
        barge_in_confirmation_ms=barge_in_confirmation_ms,
        interruptible=interruptible,
        interrupt_sensitivity=interrupt_sensitivity,
        ignore_backchannels=ignore_backchannels,
        max_duration_seconds=max_duration,
        warning_milestones_seconds=milestones,
        silence_prompt_seconds=silence_prompt,
        silence_hangup_seconds=silence_hangup,
        ai_initiation_mode=ai_initiation_mode,
        inbound_greeting_mode=inbound_greeting_mode,
        outbound_greeting_mode=outbound_greeting_mode,
        recording_default=(
            _stored_bool(
                values.get("recording_default", 0),
                key="recording_default",
            )
            if values is not None
            else global_config.recording_default
        ),
        amd_default=(
            _stored_bool(
                values.get("amd_default", 0),
                key="amd_default",
            )
            if values is not None
            else global_config.amd_default
        ),
    )


async def _load_prompt_values(conn: Any, prompt_id: int) -> dict[str, Any] | None:
    cursor = await conn.execute(
        """
        SELECT stt_locale, endpointing_ms, interruptible,
               interrupt_sensitivity, ignore_backchannels,
               max_duration_seconds, warning_milestones_json,
               silence_prompt_seconds, silence_hangup_seconds,
               ai_initiation_mode, inbound_greeting_mode,
               outbound_greeting_mode, recording_default, amd_default
        FROM PROMPT_PHONE_SETTINGS
        WHERE prompt_id = ?
        """,
        (int(prompt_id),),
    )
    row = await cursor.fetchone()
    return dict(row) if row is not None else None


async def resolve_effective_phone_settings(
    prompt_id: int,
    *,
    global_config: TelephonyConfig | None = None,
    conn: Any | None = None,
) -> EffectivePhoneSettings:
    if conn is not None:
        config = global_config or await load_telephony_config(conn=conn)
        return resolve_phone_settings(
            config,
            await _load_prompt_values(conn, prompt_id),
        )

    async with get_db_connection(readonly=True) as db_conn:
        config = global_config or await load_telephony_config(conn=db_conn)
        return resolve_phone_settings(
            config,
            await _load_prompt_values(db_conn, prompt_id),
        )


__all__ = [
    "DEFAULT_WARNING_MILESTONES_SECONDS",
    "EffectivePhoneSettings",
    "TelephonySettingsError",
    "resolve_effective_phone_settings",
    "resolve_phone_settings",
]
