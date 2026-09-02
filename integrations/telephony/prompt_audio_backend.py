"""Production composition for prompt and global telephone audio caches.

Importing this module performs no database, filesystem, credential, or network
I/O.  Application startup explicitly calls
``register_production_phone_audio_backends``; provider work remains deferred
until an administrator or prompt update actually requests rendering.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_runtime.voice_resolution import CanonicalVoice, provider_from_service_name
from database import get_db_connection
from integrations.telephony.admin_service import (
    GlobalAudioPublication,
    GlobalAudioPublisher,
    GlobalAudioPublishResult,
)
from integrations.telephony.audio_cache_renderer import PhoneAudioCacheRenderer
from integrations.telephony.audio_cache_service import (
    AudioCacheActivationPlan,
    PhoneAudioCacheService,
)
from integrations.telephony.greetings import (
    GLOBAL_AUDIO_REVISION_CONFIG_KEY,
    GLOBAL_TECHNICAL_NOTICE_KEYS,
    GREETING_DIRECTIONS,
    PROMPT_TECHNICAL_NOTICE_KEYS,
    PhoneGreetingConfigurationError,
    load_greeting_revision,
    normalize_phone_cache_tts_profile,
)
from integrations.telephony.prompt_settings_service import (
    AudioBackendFactory,
    PromptAudioActivationBackend,
    PromptPhoneAudioUnavailable,
    register_prompt_audio_backend,
)
from integrations.telephony.technical_notices import (
    load_prompt_technical_notice_revision,
    load_technical_notice_revision,
)
from tools.tts_config import get_tts_profile


SUPPORTED_PHONE_TTS_PROVIDERS = frozenset({"elevenlabs", "openai"})


@dataclass(frozen=True, slots=True)
class ProductionPhoneAudioBackends:
    prompt_factory: AudioBackendFactory
    global_publisher: GlobalAudioPublisher
    cache_service: Any


def _build_prompt_factory(cache_service: Any) -> AudioBackendFactory:
    async def factory(conn: Any, _prompt_id: int) -> PromptAudioActivationBackend:
        try:
            active_revision = await _load_active_global_audio_revision(conn)
            revision = await load_technical_notice_revision(
                conn,
                revision=active_revision,
            )
            notices = {
                key: revision.notices[key]
                for key in sorted(PROMPT_TECHNICAL_NOTICE_KEYS)
            }
        except (KeyError, PhoneGreetingConfigurationError) as exc:
            raise PromptPhoneAudioUnavailable(
                "Phone technical notice copy is incomplete"
            ) from exc
        return PromptAudioActivationBackend(
            cache_service=cache_service,
            technical_notices=notices,
            global_audio_revision=active_revision,
        )

    return factory


def build_production_prompt_audio_backend_factory(
    *,
    renderer: Any | None = None,
    cache_root: str | Path | None = None,
) -> AudioBackendFactory:
    """Compose one provider renderer, cache service, and durable notice source."""

    active_renderer = renderer if renderer is not None else PhoneAudioCacheRenderer()
    cache_options: dict[str, Any] = {"renderer": active_renderer}
    if cache_root is not None:
        cache_options["cache_root"] = cache_root
    cache_service = PhoneAudioCacheService(**cache_options)

    return _build_prompt_factory(cache_service)


def build_production_phone_audio_backends(
    *,
    renderer: Any | None = None,
    cache_root: str | Path | None = None,
    cache_service: Any | None = None,
    connection_factory: Callable[..., Any] = get_db_connection,
    profile_loader: Callable[[str], Any] = get_tts_profile,
) -> ProductionPhoneAudioBackends:
    """Compose prompt and global backends over one cache service instance."""

    active_cache_service = cache_service
    if active_cache_service is None:
        active_renderer = renderer if renderer is not None else PhoneAudioCacheRenderer()
        cache_options: dict[str, Any] = {"renderer": active_renderer}
        if cache_root is not None:
            cache_options["cache_root"] = cache_root
        active_cache_service = PhoneAudioCacheService(**cache_options)

    prompt_factory = _build_prompt_factory(active_cache_service)

    async def global_publisher(
        publication: GlobalAudioPublication,
    ) -> GlobalAudioPublishResult:
        async with connection_factory(False) as conn:
            target_voice = await _load_canonical_voice(
                conn,
                publication.voice_id,
                inherited_default=True,
            )
            notices = await _load_notice_revision(conn, publication.revision)
            if notices != dict(publication.notices):
                raise PhoneGreetingConfigurationError(
                    "Global technical notice revision changed before rendering"
                )
            global_greetings = await _load_global_greetings(
                conn,
                publication.revision,
            )
            profile = normalize_phone_cache_tts_profile(
                await profile_loader("external")
            )
            cursor = await conn.execute(
                "SELECT id FROM VOICES WHERE COALESCE(is_default,0)=1 ORDER BY id"
            )
            current_default_ids = tuple(
                int(row["id"]) for row in await cursor.fetchall()
            )
            global_plan = AudioCacheActivationPlan(
                scope="global",
                prompt_id=None,
                revision=publication.revision,
                voice=target_voice,
                profile=profile,
                greetings=global_greetings,
                technical_notices={
                    key: notices[key]
                    for key in sorted(GLOBAL_TECHNICAL_NOTICE_KEYS)
                },
                billing_user_id=publication.billing_user_id,
                greeting_modes={
                    direction: publication.greetings[direction].mode
                    for direction in GREETING_DIRECTIONS
                },
                commit_voice_change=current_default_ids != (target_voice.id,),
            )
            prompt_plans = await _build_dependent_prompt_plans(
                conn,
                publication=publication,
                target_voice=target_voice,
                profile=profile,
                global_greetings=global_greetings,
                notices=notices,
            )
            results = await active_cache_service.generate_and_activate_global_bundle(
                conn,
                global_plan,
                prompt_plans,
            )
        if not results or results[0].scope != "global":
            raise PhoneGreetingConfigurationError(
                "Global audio cache service returned no global activation"
            )
        return GlobalAudioPublishResult(
            revision=publication.revision,
            activation_id=str(results[0].activation_id),
            status="activated",
        )

    return ProductionPhoneAudioBackends(
        prompt_factory=prompt_factory,
        global_publisher=global_publisher,
        cache_service=active_cache_service,
    )


async def _load_canonical_voice(
    conn: Any,
    voice_id: int,
    *,
    inherited_default: bool,
) -> CanonicalVoice:
    cursor = await conn.execute(
        """
        SELECT v.id,v.name,v.voice_code,v.tts_service,
               COALESCE(v.deprecated,0) AS deprecated,s.name AS service_name
        FROM VOICES v LEFT JOIN SERVICES s ON s.id=v.tts_service
        WHERE v.id=?
        """,
        (int(voice_id),),
    )
    row = await cursor.fetchone()
    service_name = "" if row is None else str(row["service_name"] or "").strip()
    provider = provider_from_service_name(service_name)
    if (
        row is None
        or bool(row["deprecated"])
        or not str(row["voice_code"] or "").strip()
        or row["tts_service"] is None
        or not service_name
        or provider not in SUPPORTED_PHONE_TTS_PROVIDERS
    ):
        raise PhoneGreetingConfigurationError("Canonical phone voice is unavailable")
    return CanonicalVoice(
        id=int(row["id"]),
        voice_code=str(row["voice_code"]).strip(),
        name=str(row["name"] or row["voice_code"]),
        tts_service=int(row["tts_service"]),
        service_name=service_name,
        provider=provider,
        inherited_default=inherited_default,
    )


async def _load_notice_revision(conn: Any, revision: int) -> dict[str, str]:
    loaded = await load_technical_notice_revision(conn, revision=int(revision))
    return dict(loaded.notices)


async def _load_active_global_audio_revision(conn: Any) -> int:
    cursor = await conn.execute(
        "SELECT value FROM SYSTEM_CONFIG WHERE key=?",
        (GLOBAL_AUDIO_REVISION_CONFIG_KEY,),
    )
    row = await cursor.fetchone()
    try:
        revision = int(row["value"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PhoneGreetingConfigurationError(
            "Global phone audio has not been activated"
        ) from exc
    if revision <= 0:
        raise PhoneGreetingConfigurationError(
            "Global phone audio has not been activated"
        )
    return revision


async def _load_global_greetings(
    conn: Any,
    revision: int,
) -> tuple[Any, ...]:
    definitions: list[Any] = []
    for direction in sorted(GREETING_DIRECTIONS):
        loaded = await load_greeting_revision(
            conn,
            scope="global",
            prompt_id=None,
            revision=revision,
            direction=direction,
        )
        if not loaded:
            raise PhoneGreetingConfigurationError(
                "Global phone greeting revision is incomplete"
            )
        definitions.extend(loaded)
    return tuple(definitions)


async def _build_dependent_prompt_plans(
    conn: Any,
    *,
    publication: GlobalAudioPublication,
    target_voice: CanonicalVoice,
    profile: Any,
    global_greetings: tuple[Any, ...],
    notices: Mapping[str, str],
) -> tuple[AudioCacheActivationPlan, ...]:
    cursor = await conn.execute(
        """
        SELECT p.id,p.voice_id,
               COALESCE(s.inbound_greeting_mode,'inherit') AS inbound_mode,
               COALESCE(s.outbound_greeting_mode,'inherit') AS outbound_mode,
               s.active_audio_revision
        FROM PROMPTS p
        LEFT JOIN PROMPT_PHONE_SETTINGS s ON s.prompt_id=p.id
        ORDER BY p.id
        """
    )
    rows = [dict(row) for row in await cursor.fetchall()]
    expected_revisions = publication.prompt_active_revisions
    if expected_revisions is not None:
        normalized_expected = {
            int(prompt_id): (
                None if active_revision is None else int(active_revision)
            )
            for prompt_id, active_revision in expected_revisions.items()
        }
        row_ids = {int(row["id"]) for row in rows}
        if set(normalized_expected) != row_ids:
            raise PhoneGreetingConfigurationError(
                "Dependent prompt state changed before global audio rendering"
            )
    else:
        normalized_expected = {}
    plans: list[AudioCacheActivationPlan] = []
    inherited_notices = {
        key: notices[key] for key in sorted(PROMPT_TECHNICAL_NOTICE_KEYS)
    }
    for row in rows:
        prompt_id = int(row["id"])
        custom_notices = await load_prompt_technical_notice_revision(
            conn,
            prompt_id=prompt_id,
            revision=publication.revision,
        )
        prompt_notices = (
            inherited_notices
            if custom_notices is None
            else dict(custom_notices.notices)
        )
        voice = (
            target_voice
            if row["voice_id"] is None
            else await _load_canonical_voice(
                conn,
                int(row["voice_id"]),
                inherited_default=False,
            )
        )
        greeting_modes = {
            "inbound": str(row["inbound_mode"]),
            "outbound": str(row["outbound_mode"]),
        }
        definitions: list[Any] = []
        for direction in sorted(GREETING_DIRECTIONS):
            mode = greeting_modes[direction]
            if mode == "inherit":
                definitions.extend(
                    item for item in global_greetings if item.direction == direction
                )
                continue
            active_revision = row["active_audio_revision"]
            if active_revision is None:
                raise PhoneGreetingConfigurationError(
                    "A dependent prompt has no active custom greeting revision"
                )
            loaded = await load_greeting_revision(
                conn,
                scope="prompt",
                prompt_id=prompt_id,
                revision=publication.revision,
                direction=direction,
            )
            if not loaded:
                raise PhoneGreetingConfigurationError(
                    "A dependent prompt custom greeting list is incomplete"
                )
            definitions.extend(loaded)
        plans.append(
            AudioCacheActivationPlan(
                scope="prompt",
                prompt_id=prompt_id,
                revision=publication.revision,
                voice=voice,
                profile=profile,
                greetings=tuple(definitions),
                technical_notices=prompt_notices,
                billing_user_id=publication.billing_user_id,
                greeting_modes=greeting_modes,
                commit_voice_change=False,
                expected_active_revision=normalized_expected.get(prompt_id),
                enforce_prompt_state_fence=expected_revisions is not None,
            )
        )
    return tuple(plans)


def register_production_prompt_audio_backend(
    *,
    renderer: Any | None = None,
    cache_root: str | Path | None = None,
) -> AudioBackendFactory:
    """Register the production backend without starting provider or file I/O."""

    factory = build_production_prompt_audio_backend_factory(
        renderer=renderer,
        cache_root=cache_root,
    )
    register_prompt_audio_backend(factory)
    return factory


def register_production_phone_audio_backends(
    **kwargs: Any,
) -> ProductionPhoneAudioBackends:
    """Register both production boundaries over the same cache service."""

    backends = build_production_phone_audio_backends(**kwargs)
    register_prompt_audio_backend(backends.prompt_factory)
    from integrations.telephony.admin_routes import register_global_audio_publisher

    register_global_audio_publisher(backends.global_publisher)
    return backends


__all__ = [
    "ProductionPhoneAudioBackends",
    "build_production_phone_audio_backends",
    "build_production_prompt_audio_backend_factory",
    "register_production_phone_audio_backends",
    "register_production_prompt_audio_backend",
]
