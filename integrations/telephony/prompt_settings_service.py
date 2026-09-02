"""Manage prompt-owned phone policy and atomically activated phone audio.

The prompt editor may save deterministic call policy without a TTS provider.
Greeting and technical-notice changes are deliberately separate: they are
staged as an immutable revision and become visible only through
:class:`PhoneAudioCacheService`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
import json
import re
from typing import Any

from ai_runtime.voice_resolution import (
    CanonicalVoice,
    CanonicalVoiceResolutionError,
    provider_from_service_name,
    resolve_prompt_voice,
)
from ai_runtime.reasoning import ReasoningValidationError, resolve_and_validate
from database import get_db_connection
from integrations.telephony.audio_cache_service import (
    AudioCacheActivationPlan,
    PhoneAudioCacheService,
    _await_non_abandonable,
)
from integrations.telephony.config import load_telephony_config
from integrations.telephony.greetings import (
    GREETING_DIRECTIONS,
    GreetingDefinition,
    GreetingInput,
    PROMPT_TECHNICAL_NOTICE_KEYS,
    load_greeting_revision,
    normalize_literal_text,
    normalize_phone_cache_tts_profile,
    stage_greeting_revision,
)
from integrations.telephony.settings import DEFAULT_WARNING_MILESTONES_SECONDS
from integrations.telephony.technical_notices import (
    load_prompt_technical_notice_revision,
    load_technical_notice_revision,
    stage_prompt_technical_notice_revision,
)
from tools.tts_config import get_tts_profile
from llm_catalog import normalize_capabilities


_STT_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_AUDIO_STATUSES = frozenset(
    {"not_generated", "pending", "ready", "failed", "needs_attention"}
)
_SUPPORTED_PHONE_TTS_PROVIDERS = frozenset({"elevenlabs", "openai"})
_BLOCKED_PHONE_LLM_MACHINES = frozenset({"GPTSub", "GranSabio"})
_OPENAI_REALTIME_VOICES = frozenset(
    {
        "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer",
        "verse", "marin", "cedar",
    }
)


class PromptPhoneSettingsError(ValueError):
    """Submitted prompt phone policy is invalid."""


class PromptPhoneSettingsNotFound(PromptPhoneSettingsError):
    """The target prompt does not exist."""


class PromptPhoneAudioUnavailable(RuntimeError):
    """No production-safe renderer/notice source has been registered."""


@dataclass(frozen=True, slots=True)
class GreetingPhraseUpdate:
    literal_text: str
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class GreetingListUpdate:
    mode: str
    phrases: tuple[GreetingPhraseUpdate, ...] = ()
    fixed_index: int | None = None


@dataclass(frozen=True, slots=True)
class TechnicalNoticeUpdate:
    mode: str
    notices: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class PromptPhonePolicyUpdate:
    stt_locale: str
    max_duration_seconds: int | None
    warning_milestones_seconds: tuple[int, ...]
    silence_prompt_seconds: int | None
    silence_hangup_seconds: int | None
    ai_initiation_mode: str
    recording_default: bool
    amd_default: bool
    endpointing_ms: int | None = None
    interruptible: bool = True
    interrupt_sensitivity: str = "normal"
    ignore_backchannels: bool = True


@dataclass(frozen=True, slots=True)
class PromptPhoneAIUpdate:
    llm_id: int | None = None
    reasoning_selection: Mapping[str, Any] | None = None
    realtime_voice: str | None = None


@dataclass(frozen=True, slots=True)
class PromptAudioActivationBackend:
    """Narrow integration point supplied by the administrative TTS phase."""

    cache_service: PhoneAudioCacheService
    technical_notices: Mapping[str, str]
    global_audio_revision: int | None = None


ConnectionFactory = Callable[
    [bool], AbstractAsyncContextManager[Any]
]
AudioBackendFactory = Callable[
    [Any, int], Awaitable[PromptAudioActivationBackend]
]


_registered_audio_backend_factory: AudioBackendFactory | None = None


def register_prompt_audio_backend(factory: AudioBackendFactory | None) -> None:
    """Register the complete renderer + durable technical-copy source.

    Passing ``None`` restores the fail-closed state.  There is intentionally no
    placeholder renderer or invented technical copy in this module.
    """

    global _registered_audio_backend_factory
    _registered_audio_backend_factory = factory


async def _registered_backend(conn: Any, prompt_id: int) -> PromptAudioActivationBackend:
    factory = _registered_audio_backend_factory
    if factory is None:
        raise PromptPhoneAudioUnavailable(
            "Phone audio generation is not configured"
        )
    backend = await factory(conn, prompt_id)
    if not isinstance(backend, PromptAudioActivationBackend):
        raise PromptPhoneAudioUnavailable(
            "Phone audio generation is not configured correctly"
        )
    if set(backend.technical_notices) != set(PROMPT_TECHNICAL_NOTICE_KEYS):
        raise PromptPhoneAudioUnavailable(
            "Phone technical notice copy is incomplete"
        )
    for text in backend.technical_notices.values():
        normalize_literal_text(text)
    if backend.global_audio_revision is not None:
        try:
            global_audio_revision = int(backend.global_audio_revision)
        except (TypeError, ValueError) as exc:
            raise PromptPhoneAudioUnavailable(
                "Phone audio generation returned an invalid global revision"
            ) from exc
        if (
            isinstance(backend.global_audio_revision, bool)
            or global_audio_revision <= 0
            or global_audio_revision != backend.global_audio_revision
        ):
            raise PromptPhoneAudioUnavailable(
                "Phone audio generation returned an invalid global revision"
            )
    return backend


def _default_connection(readonly: bool) -> AbstractAsyncContextManager[Any]:
    return get_db_connection(readonly=readonly)


def _positive_billing_user_id(value: Any) -> int:
    if isinstance(value, bool):
        raise PromptPhoneSettingsError(
            "Authenticated billing user is required to generate phone audio"
        )
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise PromptPhoneSettingsError(
            "Authenticated billing user is required to generate phone audio"
        ) from exc
    if normalized <= 0 or normalized != value:
        raise PromptPhoneSettingsError(
            "Authenticated billing user is required to generate phone audio"
        )
    return normalized


class PromptPhoneSettingsService:
    """Persistence boundary used by the prompt settings API."""

    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory = _default_connection,
        audio_backend_factory: AudioBackendFactory = _registered_backend,
    ) -> None:
        self._connection_factory = connection_factory
        self._audio_backend_factory = audio_backend_factory

    async def get(self, prompt_id: int) -> dict[str, Any]:
        async with self._connection_factory(True) as conn:
            await _require_prompt(conn, prompt_id)
            config = await load_telephony_config(conn=conn)
            settings = await _load_settings_row(conn, prompt_id)
            response = _settings_response(settings, config)
            response.update(await _prompt_phone_ai_response(conn, prompt_id))
            response.update(await _current_voice_response(conn, prompt_id))
            response["greetings"] = await _active_greeting_response(
                conn, prompt_id=prompt_id, settings=settings
            )
            response["technical_notices"] = await _active_technical_notice_response(
                conn, prompt_id=prompt_id, settings=settings
            )
            response["audio_cache"] = await _audio_status_response(
                conn, prompt_id=prompt_id, settings=settings
            )
            return response

    async def update(
        self,
        prompt_id: int,
        *,
        policy: PromptPhonePolicyUpdate,
        greetings: Mapping[str, GreetingListUpdate],
        technical_notices: TechnicalNoticeUpdate | None = None,
        billing_user_id: int | None = None,
        voice_id: int | None = None,
        phone_llm_id: int | None = None,
        phone_reasoning_selection: Mapping[str, Any] | None = None,
        phone_realtime_voice: str | None = None,
    ) -> dict[str, Any]:
        normalized_greetings = _normalize_greeting_updates(greetings)
        normalized_notices = _normalize_technical_notice_update(technical_notices)
        if voice_id is not None:
            async with self._connection_factory(True) as conn:
                await _require_prompt(conn, prompt_id)
                await _load_requested_voice(conn, voice_id)
        await self._save_policy(
            prompt_id,
            policy,
            PromptPhoneAIUpdate(
                llm_id=phone_llm_id,
                reasoning_selection=phone_reasoning_selection,
                realtime_voice=phone_realtime_voice,
            ),
        )

        async with self._connection_factory(True) as conn:
            settings = await _load_settings_row(conn, prompt_id)
            greetings_changed = await _greetings_changed(
                conn,
                prompt_id=prompt_id,
                settings=settings,
                submitted=normalized_greetings,
            )
            voice_changed = await _voice_change_requested(
                conn,
                prompt_id=prompt_id,
                requested_voice_id=voice_id,
            )
            notices_changed = await _technical_notices_changed(
                conn,
                prompt_id=prompt_id,
                settings=settings,
                submitted=normalized_notices,
            )
        if not greetings_changed and not voice_changed and not notices_changed:
            return await self.get(prompt_id)

        payer_id = _positive_billing_user_id(billing_user_id)

        # Resolve the backend before staging anything.  A missing product
        # renderer must return 503 without leaving a fake pending revision.
        async with self._connection_factory(False) as conn:
            backend = await self._audio_backend_factory(conn, prompt_id)
            profile = normalize_phone_cache_tts_profile(
                await get_tts_profile("external")
            )
            revision, definitions, effective_notices, voice, commit_voice_change = (
                await _stage_audio_revision(
                    conn,
                    prompt_id=prompt_id,
                    submitted=normalized_greetings,
                    submitted_notices=normalized_notices,
                    updated_by=payer_id,
                    requested_voice_id=voice_id,
                    expected_global_audio_revision=backend.global_audio_revision,
                )
            )
            plan = AudioCacheActivationPlan(
                scope="prompt",
                prompt_id=prompt_id,
                revision=revision,
                voice=voice,
                profile=profile,
                greetings=definitions,
                technical_notices=(
                    backend.technical_notices
                    if effective_notices is None
                    else effective_notices
                ),
                billing_user_id=payer_id,
                greeting_modes={
                    direction: normalized_greetings[direction].mode
                    for direction in GREETING_DIRECTIONS
                },
                commit_voice_change=commit_voice_change,
            )
            try:
                await backend.cache_service.generate_and_activate(conn, plan)
            except BaseException:
                await _await_non_abandonable(
                    _release_unowned_staged_revision(
                        conn,
                        prompt_id=prompt_id,
                        revision=revision,
                    )
                )
                raise
        return await self.get(prompt_id)

    async def _save_policy(
        self,
        prompt_id: int,
        policy: PromptPhonePolicyUpdate,
        phone_ai: PromptPhoneAIUpdate,
    ) -> PromptPhonePolicyUpdate:
        async with self._connection_factory(False) as conn:
            await _require_prompt(conn, prompt_id)
            config = await load_telephony_config(conn=conn)
            normalized = _normalize_policy(policy, config)
            normalized_phone_ai = await _normalize_phone_ai_update(
                conn,
                prompt_id=prompt_id,
                submitted=phone_ai,
            )
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await conn.execute(
                    """
                    INSERT INTO PROMPT_PHONE_SETTINGS(
                        prompt_id,stt_locale,endpointing_ms,interruptible,
                        interrupt_sensitivity,ignore_backchannels,
                        max_duration_seconds,
                        warning_milestones_json,silence_prompt_seconds,
                        silence_hangup_seconds,ai_initiation_mode,
                        recording_default,amd_default,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                    ON CONFLICT(prompt_id) DO UPDATE SET
                        stt_locale=excluded.stt_locale,
                        endpointing_ms=excluded.endpointing_ms,
                        interruptible=excluded.interruptible,
                        interrupt_sensitivity=excluded.interrupt_sensitivity,
                        ignore_backchannels=excluded.ignore_backchannels,
                        max_duration_seconds=excluded.max_duration_seconds,
                        warning_milestones_json=excluded.warning_milestones_json,
                        silence_prompt_seconds=excluded.silence_prompt_seconds,
                        silence_hangup_seconds=excluded.silence_hangup_seconds,
                        ai_initiation_mode=excluded.ai_initiation_mode,
                        recording_default=excluded.recording_default,
                        amd_default=excluded.amd_default,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        prompt_id,
                        normalized.stt_locale,
                        normalized.endpointing_ms,
                        int(normalized.interruptible),
                        normalized.interrupt_sensitivity,
                        int(normalized.ignore_backchannels),
                        normalized.max_duration_seconds,
                        json.dumps(
                            normalized.warning_milestones_seconds,
                            separators=(",", ":"),
                        ),
                        normalized.silence_prompt_seconds,
                        normalized.silence_hangup_seconds,
                        normalized.ai_initiation_mode,
                        int(normalized.recording_default),
                        int(normalized.amd_default),
                    ),
                )
                if normalized_phone_ai is not None:
                    await conn.execute(
                        """
                        UPDATE PROMPTS
                        SET phone_llm_id=?,phone_reasoning_json=?,
                            phone_realtime_voice=?
                        WHERE id=?
                        """,
                        (
                            normalized_phone_ai.llm_id,
                            (
                                None
                                if normalized_phone_ai.reasoning_selection is None
                                else json.dumps(
                                    normalized_phone_ai.reasoning_selection,
                                    ensure_ascii=True,
                                    separators=(",", ":"),
                                )
                            ),
                            normalized_phone_ai.realtime_voice,
                            prompt_id,
                        ),
                    )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
            return normalized


def _normalize_policy(policy: PromptPhonePolicyUpdate, config: Any) -> PromptPhonePolicyUpdate:
    locale = str(policy.stt_locale or "").strip()
    if locale.lower() == "auto":
        locale = "auto"
    elif not _STT_LOCALE_RE.fullmatch(locale):
        raise PromptPhoneSettingsError(
            "stt_locale must be 'auto' or a fixed language locale"
        )

    endpointing_ms = policy.endpointing_ms
    if endpointing_ms is not None:
        if (
            isinstance(endpointing_ms, bool)
            or not isinstance(endpointing_ms, int)
            or not 300 <= endpointing_ms <= 3_000
        ):
            raise PromptPhoneSettingsError(
                "endpointing_ms must be between 300 and 3000 milliseconds"
            )
    sensitivity = str(policy.interrupt_sensitivity or "").strip().lower()
    if sensitivity not in {"low", "normal", "high"}:
        raise PromptPhoneSettingsError("invalid interrupt sensitivity")

    maximum = policy.max_duration_seconds
    effective_maximum = config.max_call_seconds
    if maximum is not None:
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 60:
            raise PromptPhoneSettingsError(
                "max_duration_seconds must be at least 60 seconds"
            )
        if maximum > config.max_call_seconds:
            raise PromptPhoneSettingsError(
                "max_duration_seconds cannot exceed the administrative maximum"
            )
        effective_maximum = maximum

    milestones: list[int] = []
    seen: set[int] = set()
    if len(policy.warning_milestones_seconds) > 12:
        raise PromptPhoneSettingsError("too many duration warning milestones")
    for value in policy.warning_milestones_seconds:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise PromptPhoneSettingsError(
                "duration warning milestones must be positive seconds"
            )
        if value >= effective_maximum:
            raise PromptPhoneSettingsError(
                "duration warning milestones must be shorter than the call limit"
            )
        if value in seen:
            raise PromptPhoneSettingsError(
                "duration warning milestones cannot be repeated"
            )
        seen.add(value)
        milestones.append(value)
    milestones.sort(reverse=True)

    silence_prompt = policy.silence_prompt_seconds
    silence_hangup = policy.silence_hangup_seconds
    if (silence_prompt is None) != (silence_hangup is None):
        raise PromptPhoneSettingsError(
            "silence prompt and hangup seconds must both be set or both be disabled"
        )
    if silence_prompt is not None and silence_hangup is not None:
        for name, value, administrative in (
            ("silence_prompt_seconds", silence_prompt, config.silence_check_seconds),
            ("silence_hangup_seconds", silence_hangup, config.silence_hangup_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise PromptPhoneSettingsError(f"{name} must be positive")
            if administrative <= 0 or value > administrative:
                raise PromptPhoneSettingsError(
                    f"{name} cannot exceed the administrative maximum"
                )
        if silence_prompt + silence_hangup >= effective_maximum:
            raise PromptPhoneSettingsError(
                "combined silence timeouts must be shorter than the call limit"
            )

    initiation = str(policy.ai_initiation_mode or "").strip().lower()
    if initiation not in {"on_request", "proactive", "disabled"}:
        raise PromptPhoneSettingsError("invalid AI phone initiation mode")
    return PromptPhonePolicyUpdate(
        stt_locale=locale,
        endpointing_ms=endpointing_ms,
        interruptible=bool(policy.interruptible),
        interrupt_sensitivity=sensitivity,
        ignore_backchannels=bool(policy.ignore_backchannels),
        max_duration_seconds=maximum,
        warning_milestones_seconds=tuple(milestones),
        silence_prompt_seconds=silence_prompt,
        silence_hangup_seconds=silence_hangup,
        ai_initiation_mode=initiation,
        recording_default=bool(policy.recording_default),
        amd_default=bool(policy.amd_default),
    )


def _normalize_greeting_updates(
    submitted: Mapping[str, GreetingListUpdate],
) -> dict[str, GreetingListUpdate]:
    if set(submitted) != set(GREETING_DIRECTIONS):
        raise PromptPhoneSettingsError("inbound and outbound greetings are required")
    result: dict[str, GreetingListUpdate] = {}
    for direction in GREETING_DIRECTIONS:
        item = submitted[direction]
        mode = str(item.mode or "").strip().lower()
        if mode not in {"inherit", "fixed", "random"}:
            raise PromptPhoneSettingsError(f"invalid {direction} greeting mode")
        phrases = tuple(
            GreetingPhraseUpdate(
                literal_text=normalize_literal_text(phrase.literal_text),
                enabled=bool(phrase.enabled),
            )
            for phrase in item.phrases
        )
        if len(phrases) > 50:
            raise PromptPhoneSettingsError("a greeting list cannot exceed 50 phrases")
        fixed = item.fixed_index
        if mode == "inherit":
            if phrases or fixed is not None:
                raise PromptPhoneSettingsError(
                    "inherited greetings cannot define prompt phrases"
                )
        elif not phrases or not any(phrase.enabled for phrase in phrases):
            raise PromptPhoneSettingsError(
                "a replacement greeting list needs an enabled phrase"
            )
        elif mode == "fixed":
            if fixed is None or isinstance(fixed, bool) or not 0 <= fixed < len(phrases):
                raise PromptPhoneSettingsError("fixed greeting selection is missing")
            if not phrases[fixed].enabled:
                raise PromptPhoneSettingsError("the fixed greeting must be enabled")
        elif fixed is not None:
            raise PromptPhoneSettingsError(
                "random greeting selection cannot define a fixed phrase"
            )
        result[direction] = GreetingListUpdate(mode, phrases, fixed)
    return result


def _normalize_technical_notice_update(
    submitted: TechnicalNoticeUpdate | None,
) -> TechnicalNoticeUpdate | None:
    if submitted is None:
        return None
    mode = str(submitted.mode or "").strip().lower()
    if mode not in {"inherit", "custom"}:
        raise PromptPhoneSettingsError("invalid technical notice mode")
    try:
        raw_notices = dict(submitted.notices)
    except (TypeError, ValueError) as exc:
        raise PromptPhoneSettingsError("technical notices are invalid") from exc
    if mode == "inherit":
        if raw_notices:
            raise PromptPhoneSettingsError(
                "inherited technical notices cannot define prompt copy"
            )
        return TechnicalNoticeUpdate(mode="inherit", notices={})
    expected = set(PROMPT_TECHNICAL_NOTICE_KEYS)
    actual = set(raw_notices)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise PromptPhoneSettingsError(
            "custom technical notices must define exactly the seven prompt keys "
            f"(missing={missing}, extra={extra})"
        )
    normalized = {
        key: normalize_literal_text(raw_notices[key])
        for key in sorted(PROMPT_TECHNICAL_NOTICE_KEYS)
    }
    return TechnicalNoticeUpdate(mode="custom", notices=normalized)


async def _require_prompt(conn: Any, prompt_id: int) -> None:
    if isinstance(prompt_id, bool) or int(prompt_id) <= 0:
        raise PromptPhoneSettingsNotFound("Prompt not found")
    cursor = await conn.execute("SELECT 1 FROM PROMPTS WHERE id=?", (int(prompt_id),))
    if await cursor.fetchone() is None:
        raise PromptPhoneSettingsNotFound("Prompt not found")


async def _prompt_columns(conn: Any) -> set[str]:
    cursor = await conn.execute("PRAGMA table_info(PROMPTS)")
    return {str(row[1]) for row in await cursor.fetchall()}


def _json_object(value: Any, *, label: str) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise PromptPhoneSettingsError(f"stored {label} is invalid") from exc
    if not isinstance(decoded, dict):
        raise PromptPhoneSettingsError(f"stored {label} is invalid")
    return decoded


async def _load_phone_llm(conn: Any, llm_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    cursor = await conn.execute(
        """
        SELECT id,machine,model,provider_key,provider_model_id,
               raw_metadata_json,capabilities_json,manual_overrides_json,
               COALESCE(enabled,1) AS enabled
        FROM LLM WHERE id=?
        """,
        (int(llm_id),),
    )
    row = await cursor.fetchone()
    if row is None:
        raise PromptPhoneSettingsError("Selected phone AI model does not exist")
    item = dict(row)
    if not bool(item["enabled"]):
        raise PromptPhoneSettingsError("Selected phone AI model is disabled")
    if item["machine"] in _BLOCKED_PHONE_LLM_MACHINES:
        raise PromptPhoneSettingsError(
            "Selected AI model cannot be used for phone calls"
        )

    def safe_object(value: Any) -> dict[str, Any]:
        if not value:
            return {}
        try:
            decoded = json.loads(str(value))
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    capabilities = normalize_capabilities(
        item.get("provider_key") or item.get("machine"),
        item.get("provider_model_id") or item.get("model"),
        safe_object(item.get("raw_metadata_json")),
        safe_object(item.get("capabilities_json")),
        safe_object(item.get("manual_overrides_json")),
    )
    if "phone" not in capabilities.get("runtime", {}).get("channels", []):
        raise PromptPhoneSettingsError(
            "Selected AI model is not available for phone calls"
        )
    return item, capabilities


async def _normalize_phone_ai_update(
    conn: Any,
    *,
    prompt_id: int,
    submitted: PromptPhoneAIUpdate,
) -> PromptPhoneAIUpdate | None:
    columns = await _prompt_columns(conn)
    required = {
        "phone_llm_id", "phone_reasoning_json", "phone_realtime_voice",
        "forced_llm_id", "forced_reasoning_json",
    }
    if not required <= columns:
        if (
            submitted.llm_id is None
            and submitted.reasoning_selection is None
            and submitted.realtime_voice is None
        ):
            return None
        raise PromptPhoneSettingsError(
            "Prompt phone AI settings migration has not been applied"
        )

    cursor = await conn.execute(
        "SELECT forced_llm_id FROM PROMPTS WHERE id=?",
        (prompt_id,),
    )
    prompt_row = await cursor.fetchone()
    if prompt_row is None:
        raise PromptPhoneSettingsNotFound("Prompt not found")
    forced_llm_id = prompt_row["forced_llm_id"]

    selected_capabilities: dict[str, Any] | None = None
    if submitted.llm_id is not None:
        if isinstance(submitted.llm_id, bool) or int(submitted.llm_id) <= 0:
            raise PromptPhoneSettingsError("Selected phone AI model is invalid")
        _, selected_capabilities = await _load_phone_llm(conn, int(submitted.llm_id))

    normalized_reasoning: dict[str, Any] | None = None
    if submitted.reasoning_selection is not None:
        target_llm_id = submitted.llm_id or forced_llm_id
        if target_llm_id is None:
            raise PromptPhoneSettingsError(
                "Select a specific phone or prompt model before setting thinking"
            )
        if submitted.llm_id is not None:
            target_capabilities = selected_capabilities
        else:
            _, target_capabilities = await _load_phone_llm(
                conn, int(target_llm_id)
            )
        try:
            normalized_reasoning = resolve_and_validate(
                submitted.reasoning_selection,
                target_capabilities,
            ).to_dict()
        except ReasoningValidationError as exc:
            raise PromptPhoneSettingsError(f"Invalid phone thinking: {exc}") from exc

    runtime_kind = (
        selected_capabilities.get("runtime", {}).get("kind")
        if selected_capabilities is not None
        else "standard"
    )
    if runtime_kind == "openai_realtime":
        realtime_voice = str(submitted.realtime_voice or "marin").strip().lower()
        if realtime_voice not in _OPENAI_REALTIME_VOICES:
            raise PromptPhoneSettingsError(
                "Select a supported OpenAI Realtime voice"
            )
    else:
        if submitted.realtime_voice is not None:
            raise PromptPhoneSettingsError(
                "An OpenAI phone voice requires an OpenAI Realtime model"
            )
        realtime_voice = None

    return PromptPhoneAIUpdate(
        llm_id=submitted.llm_id,
        reasoning_selection=normalized_reasoning,
        realtime_voice=realtime_voice,
    )


async def _prompt_phone_ai_response(conn: Any, prompt_id: int) -> dict[str, Any]:
    columns = await _prompt_columns(conn)
    required = {
        "phone_llm_id", "phone_reasoning_json", "phone_realtime_voice",
        "forced_llm_id", "forced_reasoning_json",
    }
    if not required <= columns:
        return {
            "phone_llm_id": None,
            "phone_reasoning_selection": None,
            "phone_realtime_voice": None,
            "inherited_llm_id": None,
            "inherited_reasoning_selection": None,
        }
    cursor = await conn.execute(
        """
        SELECT phone_llm_id,phone_reasoning_json,phone_realtime_voice,
               forced_llm_id,forced_reasoning_json
        FROM PROMPTS WHERE id=?
        """,
        (prompt_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise PromptPhoneSettingsNotFound("Prompt not found")
    return {
        "phone_llm_id": row["phone_llm_id"],
        "phone_reasoning_selection": _json_object(
            row["phone_reasoning_json"], label="phone thinking"
        ),
        "phone_realtime_voice": row["phone_realtime_voice"],
        "inherited_llm_id": row["forced_llm_id"],
        "inherited_reasoning_selection": _json_object(
            row["forced_reasoning_json"], label="prompt thinking"
        ),
    }


async def _load_settings_row(conn: Any, prompt_id: int) -> dict[str, Any] | None:
    cursor = await conn.execute(
        "SELECT * FROM PROMPT_PHONE_SETTINGS WHERE prompt_id=?", (prompt_id,)
    )
    row = await cursor.fetchone()
    return None if row is None else dict(row)


def _settings_response(settings: Mapping[str, Any] | None, config: Any) -> dict[str, Any]:
    if settings is None:
        default_policy = _default_policy(config)
        return {
            "stt_locale": default_policy.stt_locale,
            "endpointing_ms": default_policy.endpointing_ms,
            "administrative_endpointing_ms": config.endpointing_ms,
            "interruptible": default_policy.interruptible,
            "interrupt_sensitivity": default_policy.interrupt_sensitivity,
            "ignore_backchannels": default_policy.ignore_backchannels,
            "max_duration_seconds": default_policy.max_duration_seconds,
            "administrative_max_duration_seconds": config.max_call_seconds,
            "warning_milestones_seconds": list(
                default_policy.warning_milestones_seconds
            ),
            "silence_enabled": default_policy.silence_prompt_seconds is not None,
            "silence_prompt_seconds": default_policy.silence_prompt_seconds,
            "silence_hangup_seconds": default_policy.silence_hangup_seconds,
            "ai_initiation_mode": default_policy.ai_initiation_mode,
            "recording_default": default_policy.recording_default,
            "amd_default": default_policy.amd_default,
        }
    try:
        milestones = json.loads(str(settings["warning_milestones_json"]))
    except (TypeError, ValueError) as exc:
        raise PromptPhoneSettingsError("stored duration milestones are invalid") from exc
    silence_prompt = settings["silence_prompt_seconds"]
    silence_hangup = settings["silence_hangup_seconds"]
    return {
        "stt_locale": str(settings["stt_locale"]),
        "endpointing_ms": settings["endpointing_ms"],
        "administrative_endpointing_ms": config.endpointing_ms,
        "interruptible": bool(settings["interruptible"]),
        "interrupt_sensitivity": str(settings["interrupt_sensitivity"]),
        "ignore_backchannels": bool(settings["ignore_backchannels"]),
        "max_duration_seconds": settings["max_duration_seconds"],
        "administrative_max_duration_seconds": config.max_call_seconds,
        "warning_milestones_seconds": milestones,
        "silence_enabled": silence_prompt is not None and silence_hangup is not None,
        "silence_prompt_seconds": silence_prompt,
        "silence_hangup_seconds": silence_hangup,
        "ai_initiation_mode": str(settings["ai_initiation_mode"]),
        "recording_default": bool(settings["recording_default"]),
        "amd_default": bool(settings["amd_default"]),
    }


def _default_policy(config: Any) -> PromptPhonePolicyUpdate:
    """Return inherited prompt defaults that the update validator accepts.

    Administrative values remain untouched.  Prompt defaults simply omit
    unreachable warnings and disable the silence pair when the administrative
    pair cannot complete before the call limit.
    """

    effective_maximum = config.max_call_seconds
    milestones = tuple(
        value
        for value in DEFAULT_WARNING_MILESTONES_SECONDS
        if value < effective_maximum
    )
    silence_prompt = config.silence_check_seconds or None
    silence_hangup = config.silence_hangup_seconds or None
    if (
        silence_prompt is None
        or silence_hangup is None
        or silence_prompt + silence_hangup >= effective_maximum
    ):
        silence_prompt = None
        silence_hangup = None
    return _normalize_policy(
        PromptPhonePolicyUpdate(
            stt_locale="auto",
            endpointing_ms=None,
            interruptible=True,
            interrupt_sensitivity="normal",
            ignore_backchannels=True,
            max_duration_seconds=None,
            warning_milestones_seconds=milestones,
            silence_prompt_seconds=silence_prompt,
            silence_hangup_seconds=silence_hangup,
            ai_initiation_mode="on_request",
            recording_default=config.recording_default,
            amd_default=config.amd_default,
        ),
        config,
    )


async def _active_greeting_response(
    conn: Any,
    *,
    prompt_id: int,
    settings: Mapping[str, Any] | None,
) -> dict[str, Any]:
    active_revision = None if settings is None else settings["active_audio_revision"]
    result: dict[str, Any] = {}
    for direction in GREETING_DIRECTIONS:
        mode = (
            "inherit"
            if settings is None
            else str(settings[f"{direction}_greeting_mode"])
        )
        definitions: Sequence[GreetingDefinition] = ()
        if mode != "inherit" and active_revision is not None:
            definitions = await load_greeting_revision(
                conn,
                scope="prompt",
                prompt_id=prompt_id,
                revision=int(active_revision),
                direction=direction,
            )
        result[direction] = _greeting_list_json(mode, definitions)
    return result


async def _active_technical_notice_response(
    conn: Any,
    *,
    prompt_id: int,
    settings: Mapping[str, Any] | None,
) -> dict[str, Any]:
    custom: Mapping[str, str] = {}
    active_revision = None if settings is None else settings["active_audio_revision"]
    if active_revision is not None:
        revision = await load_prompt_technical_notice_revision(
            conn,
            prompt_id=prompt_id,
            revision=int(active_revision),
        )
        if revision is not None:
            custom = revision.notices
    if custom:
        notices = {
            key: custom[key] for key in sorted(PROMPT_TECHNICAL_NOTICE_KEYS)
        }
        return {
            "mode": "custom",
            "notices": notices,
            "effective_notices": dict(notices),
        }
    effective = await _active_global_prompt_technical_notices(conn)
    return {
        "mode": "inherit",
        "notices": {},
        "effective_notices": effective,
    }


async def _active_global_prompt_technical_notices(conn: Any) -> dict[str, str]:
    cursor = await conn.execute(
        "SELECT value FROM SYSTEM_CONFIG "
        "WHERE key='telephony_global_audio_revision'"
    )
    row = await cursor.fetchone()
    if row is None or row["value"] in {None, ""}:
        return {}
    try:
        revision = int(row["value"])
    except (TypeError, ValueError) as exc:
        raise PromptPhoneSettingsError(
            "global phone audio revision is invalid"
        ) from exc
    if revision <= 0:
        return {}
    configured = await load_technical_notice_revision(conn, revision=revision)
    return {
        key: configured.notices[key]
        for key in sorted(PROMPT_TECHNICAL_NOTICE_KEYS)
    }


async def _audio_status_response(
    conn: Any,
    *,
    prompt_id: int,
    settings: Mapping[str, Any] | None,
) -> dict[str, Any]:
    cursor = await conn.execute(
        """
        SELECT audio_revision,status,last_error,created_at,ready_at,activated_at
        FROM VOICE_CANONICAL_ACTIVATIONS
        WHERE scope='prompt' AND prompt_id=?
        ORDER BY audio_revision DESC,created_at DESC,id DESC LIMIT 1
        """,
        (prompt_id,),
    )
    latest = await cursor.fetchone()
    status = "not_generated" if settings is None else str(settings["audio_cache_status"])
    if status not in _AUDIO_STATUSES:
        raise PromptPhoneSettingsError("stored phone audio status is invalid")
    return {
        "status": status,
        "active_revision": None if settings is None else settings["active_audio_revision"],
        "pending_revision": None if settings is None else settings["pending_audio_revision"],
        "last_attempt": (
            None
            if latest is None
            else {
                "revision": int(latest["audio_revision"]),
                "status": str(latest["status"]),
                "last_error": latest["last_error"],
                "created_at": latest["created_at"],
                "ready_at": latest["ready_at"],
                "activated_at": latest["activated_at"],
            }
        ),
    }


def _greeting_list_json(
    mode: str, definitions: Sequence[GreetingDefinition]
) -> dict[str, Any]:
    ordered = sorted(definitions, key=lambda item: (item.display_order, item.id))
    fixed_index = next(
        (index for index, item in enumerate(ordered) if item.fixed), None
    )
    if mode == "fixed" and fixed_index is None and len(ordered) == 1:
        # Preserve compatibility with data staged before fixed selections were
        # made immutable per revision.  A one-row fixed list is unambiguous.
        fixed_index = 0
    return {
        "mode": mode,
        "fixed_index": fixed_index,
        "phrases": [
            {"literal_text": item.literal_text, "enabled": item.enabled}
            for item in ordered
        ],
    }


async def _greetings_changed(
    conn: Any,
    *,
    prompt_id: int,
    settings: Mapping[str, Any] | None,
    submitted: Mapping[str, GreetingListUpdate],
) -> bool:
    current = await _active_greeting_response(
        conn, prompt_id=prompt_id, settings=settings
    )
    for direction in GREETING_DIRECTIONS:
        expected = submitted[direction]
        actual = current[direction]
        if expected.mode != actual["mode"] or expected.fixed_index != actual["fixed_index"]:
            return True
        phrases = tuple(
            GreetingPhraseUpdate(item["literal_text"], bool(item["enabled"]))
            for item in actual["phrases"]
        )
        if expected.phrases != phrases:
            return True
    return False


async def _technical_notices_changed(
    conn: Any,
    *,
    prompt_id: int,
    settings: Mapping[str, Any] | None,
    submitted: TechnicalNoticeUpdate | None,
) -> bool:
    if submitted is None:
        return False
    current = await _active_technical_notice_response(
        conn,
        prompt_id=prompt_id,
        settings=settings,
    )
    if submitted.mode != current["mode"]:
        return True
    if submitted.mode == "custom":
        return dict(submitted.notices) != current["notices"]
    return False


async def _voice_change_requested(
    conn: Any,
    *,
    prompt_id: int,
    requested_voice_id: int | None,
) -> bool:
    if requested_voice_id is None:
        return False
    normalized_voice_id = _positive_voice_id(requested_voice_id)
    cursor = await conn.execute(
        "SELECT voice_id FROM PROMPTS WHERE id=?",
        (prompt_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise PromptPhoneSettingsNotFound("Prompt not found")
    configured = row["voice_id"]
    return configured is None or int(configured) != normalized_voice_id


async def _current_voice_response(conn: Any, prompt_id: int) -> dict[str, Any]:
    try:
        voice = await resolve_prompt_voice(prompt_id, conn=conn)
    except CanonicalVoiceResolutionError as exc:
        cursor = await conn.execute(
            "SELECT voice_id FROM PROMPTS WHERE id=?",
            (prompt_id,),
        )
        prompt = await cursor.fetchone()
        if prompt is None:
            raise PromptPhoneSettingsNotFound("Prompt not found") from exc
        voice_id = prompt["voice_id"]
        if voice_id is None:
            cursor = await conn.execute(
                """
                SELECT id,voice_code FROM VOICES
                WHERE COALESCE(is_default,0)=1 ORDER BY id
                """
            )
            defaults = await cursor.fetchall()
            row = defaults[0] if len(defaults) == 1 else None
        else:
            cursor = await conn.execute(
                "SELECT id,voice_code FROM VOICES WHERE id=?",
                (voice_id,),
            )
            row = await cursor.fetchone()
        return {
            "voice_id": None if row is None else int(row["id"]),
            "voice_code": None if row is None else str(row["voice_code"] or ""),
            "voice_available": False,
            "voice_error": str(exc),
        }
    return {
        "voice_id": voice.id,
        "voice_code": voice.voice_code,
        "voice_available": True,
        "voice_error": None,
    }


def _positive_voice_id(value: Any) -> int:
    if isinstance(value, bool):
        raise PromptPhoneSettingsError("voice_id must identify an available voice")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise PromptPhoneSettingsError(
            "voice_id must identify an available voice"
        ) from exc
    if normalized <= 0 or normalized != value:
        raise PromptPhoneSettingsError("voice_id must identify an available voice")
    return normalized


async def _load_requested_voice(conn: Any, voice_id: int) -> CanonicalVoice:
    cursor = await conn.execute(
        """
        SELECT v.id AS voice_id,v.name AS voice_name,v.voice_code,
               v.tts_service,COALESCE(v.deprecated,0) AS deprecated,
               s.name AS service_name
        FROM VOICES v
        LEFT JOIN SERVICES s ON s.id=v.tts_service
        WHERE v.id=?
        """,
        (_positive_voice_id(voice_id),),
    )
    row = await cursor.fetchone()
    if row is None or bool(row["deprecated"]):
        raise PromptPhoneSettingsError("Selected voice is unavailable")
    voice_code = str(row["voice_code"] or "").strip()
    service_name = str(row["service_name"] or "").strip()
    if not voice_code or row["tts_service"] is None or not service_name:
        raise PromptPhoneSettingsError("Selected voice is unavailable")
    provider = provider_from_service_name(service_name)
    if provider not in _SUPPORTED_PHONE_TTS_PROVIDERS:
        raise PromptPhoneSettingsError(
            "Selected voice provider is unsupported for phone audio"
        )
    return CanonicalVoice(
        id=int(row["voice_id"]),
        voice_code=voice_code,
        name=str(row["voice_name"] or voice_code),
        tts_service=int(row["tts_service"]),
        service_name=service_name,
        provider=provider,
        inherited_default=False,
    )


async def _next_audio_revision(conn: Any, prompt_id: int) -> int:
    cursor = await conn.execute(
        """
        SELECT COALESCE(MAX(revision),0) AS revision
        FROM (
            SELECT revision FROM PROMPT_PHONE_GREETINGS WHERE prompt_id=?
            UNION ALL
            SELECT revision FROM PROMPT_PHONE_TECHNICAL_NOTICE_DEFINITIONS
            WHERE prompt_id=?
            UNION ALL
            SELECT revision FROM PHONE_PROMPT_AUDIO_CACHE WHERE prompt_id=?
            UNION ALL
            SELECT audio_revision AS revision FROM VOICE_CANONICAL_ACTIVATIONS
            WHERE prompt_id=?
        )
        """,
        (prompt_id, prompt_id, prompt_id, prompt_id),
    )
    return int((await cursor.fetchone())["revision"]) + 1


async def _stage_audio_revision(
    conn: Any,
    *,
    prompt_id: int,
    submitted: Mapping[str, GreetingListUpdate],
    submitted_notices: TechnicalNoticeUpdate | None,
    updated_by: int,
    requested_voice_id: int | None,
    expected_global_audio_revision: int | None = None,
) -> tuple[
    int,
    tuple[GreetingDefinition, ...],
    Mapping[str, str] | None,
    CanonicalVoice,
    bool,
]:
    await conn.execute("BEGIN IMMEDIATE")
    try:
        await _require_prompt(conn, prompt_id)
        if expected_global_audio_revision is not None:
            active_global_revision = await _global_audio_revision(conn)
            if active_global_revision != int(expected_global_audio_revision):
                raise PromptPhoneSettingsError(
                    "Global phone audio changed while this update was being staged; retry"
                )
        cursor = await conn.execute(
            "SELECT pending_audio_revision,active_audio_revision "
            "FROM PROMPT_PHONE_SETTINGS WHERE prompt_id=?",
            (prompt_id,),
        )
        settings = await cursor.fetchone()
        if settings is not None and settings["pending_audio_revision"] is not None:
            raise PromptPhoneSettingsError(
                "Another phone audio activation is already pending"
            )
        if requested_voice_id is None:
            voice = await resolve_prompt_voice(prompt_id, conn=conn)
            commit_voice_change = False
        else:
            voice = await _load_requested_voice(conn, requested_voice_id)
            cursor = await conn.execute(
                "SELECT voice_id FROM PROMPTS WHERE id=?",
                (prompt_id,),
            )
            row = await cursor.fetchone()
            configured_voice_id = None if row is None else row["voice_id"]
            commit_voice_change = (
                configured_voice_id is None
                or int(configured_voice_id) != voice.id
            )
        revision = await _next_audio_revision(conn, prompt_id)
        notice_update = submitted_notices
        if notice_update is None:
            active_revision = (
                None if settings is None else settings["active_audio_revision"]
            )
            active_custom = None
            if active_revision is not None:
                active_custom = await load_prompt_technical_notice_revision(
                    conn,
                    prompt_id=prompt_id,
                    revision=int(active_revision),
                )
            notice_update = (
                TechnicalNoticeUpdate("inherit", {})
                if active_custom is None
                else TechnicalNoticeUpdate("custom", dict(active_custom.notices))
            )
        if notice_update.mode == "custom":
            staged_notices = await stage_prompt_technical_notice_revision(
                conn,
                prompt_id=prompt_id,
                revision=revision,
                notices=notice_update.notices,
                updated_by=updated_by,
            )
            effective_notices: Mapping[str, str] | None = dict(
                staged_notices.notices
            )
        else:
            effective_notices = None
        definitions: list[GreetingDefinition] = []
        for direction in sorted(GREETING_DIRECTIONS):
            item = submitted[direction]
            await stage_greeting_revision(
                conn,
                scope="prompt",
                prompt_id=prompt_id,
                revision=revision,
                direction=direction,
                mode=item.mode,
                greetings=tuple(
                    GreetingInput(phrase.literal_text, phrase.enabled)
                    for phrase in item.phrases
                ),
                fixed_index=item.fixed_index,
            )
            if item.mode == "inherit":
                inherited_revision = await _global_audio_revision(conn)
                inherited = await load_greeting_revision(
                    conn,
                    scope="global",
                    prompt_id=None,
                    revision=inherited_revision,
                    direction=direction,
                )
                if not inherited:
                    raise PromptPhoneSettingsError(
                        f"global {direction} greetings are not active"
                    )
                definitions.extend(inherited)
            else:
                definitions.extend(
                    await load_greeting_revision(
                        conn,
                        scope="prompt",
                        prompt_id=prompt_id,
                        revision=revision,
                        direction=direction,
                    )
                )
        await conn.execute(
            """
            INSERT INTO PROMPT_PHONE_SETTINGS(
                prompt_id,pending_audio_revision,audio_cache_status,updated_at
            ) VALUES(?,?,'pending',CURRENT_TIMESTAMP)
            ON CONFLICT(prompt_id) DO UPDATE SET
                pending_audio_revision=excluded.pending_audio_revision,
                audio_cache_status='pending',updated_at=CURRENT_TIMESTAMP
            """,
            (prompt_id, revision),
        )
        await conn.commit()
        return (
            revision,
            tuple(definitions),
            effective_notices,
            voice,
            commit_voice_change,
        )
    except BaseException:
        await conn.rollback()
        raise


async def _release_unowned_staged_revision(
    conn: Any,
    *,
    prompt_id: int,
    revision: int,
) -> None:
    """Release a reservation only when the cache activator never acquired it."""

    await conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = await conn.execute(
            """
            SELECT 1 FROM VOICE_CANONICAL_ACTIVATIONS
            WHERE scope='prompt' AND prompt_id=? AND audio_revision=?
            LIMIT 1
            """,
            (prompt_id, revision),
        )
        if await cursor.fetchone() is None:
            await conn.execute(
                """
                UPDATE PROMPT_PHONE_SETTINGS
                SET pending_audio_revision=NULL,audio_cache_status='failed',
                    updated_at=CURRENT_TIMESTAMP
                WHERE prompt_id=? AND pending_audio_revision=?
                """,
                (prompt_id, revision),
            )
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise


async def _global_audio_revision(conn: Any) -> int:
    cursor = await conn.execute(
        "SELECT value FROM SYSTEM_CONFIG WHERE key='telephony_global_audio_revision'"
    )
    row = await cursor.fetchone()
    try:
        revision = int(row["value"] if row is not None else 0)
    except (TypeError, ValueError) as exc:
        raise PromptPhoneSettingsError(
            "global phone greeting revision is invalid"
        ) from exc
    if revision <= 0:
        raise PromptPhoneSettingsError("global phone greetings are not active")
    return revision


__all__ = [
    "AudioBackendFactory",
    "GreetingListUpdate",
    "GreetingPhraseUpdate",
    "PromptAudioActivationBackend",
    "PromptPhoneAIUpdate",
    "PromptPhoneAudioUnavailable",
    "PromptPhonePolicyUpdate",
    "PromptPhoneSettingsError",
    "PromptPhoneSettingsNotFound",
    "PromptPhoneSettingsService",
    "TechnicalNoticeUpdate",
    "register_prompt_audio_backend",
]
