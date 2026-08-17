"""Thin Aurvek adapter for Atagia sidecar memory integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import logging
import os
import time
from typing import Any, Literal, Protocol
from urllib.parse import quote

from memory.health import record_memory_failure, record_memory_success

logger = logging.getLogger(__name__)

TransportName = Literal["auto", "local", "http"]
DEFAULT_ASSISTANT_MODE = "personal_assistant"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_PLATFORM_ID = "aurvek"
_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
_VALID_TRANSPORTS: set[str] = {"auto", "local", "http"}


class AtagiaClientProtocol(Protocol):
    """Subset of the generic Atagia client used by Aurvek."""

    async def create_user(self, user_id: str) -> None:
        """Create the user if needed."""

    async def create_conversation(
        self,
        user_id: str,
        conversation_id: str | None,
        *,
        user_persona_id: str | None = None,
        platform_id: str | None = None,
        character_id: str | None = None,
        mode: str | None = None,
        incognito: bool | None = None,
    ) -> str:
        """Create or reuse an Atagia conversation."""

    async def get_context(
        self,
        user_id: str,
        conversation_id: str,
        message: str,
        mode: str | None = None,
        occurred_at: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        message_id: str | None = None,
        source_seq: int | str | None = None,
        ingest_origin: str | None = None,
        confirmation_strategy: str | None = None,
        memory_privacy_mode: str | None = None,
        *,
        operational_profile: str | None = None,
        operational_signals: dict[str, Any] | None = None,
        user_persona_id: str | None = None,
        platform_id: str | None = None,
        character_id: str | None = None,
        incognito: bool | None = None,
    ) -> Any:
        """Return memory context for a host-managed LLM call."""

    async def add_response(
        self,
        user_id: str,
        conversation_id: str,
        text: str,
        occurred_at: str | None = None,
        *,
        message_id: str | None = None,
        source_seq: int | str | None = None,
        ingest_origin: str | None = None,
        confirmation_strategy: str | None = None,
        memory_privacy_mode: str | None = None,
        operational_profile: str | None = None,
        operational_signals: dict[str, Any] | None = None,
        user_persona_id: str | None = None,
        platform_id: str | None = None,
        character_id: str | None = None,
        mode: str | None = None,
        incognito: bool | None = None,
    ) -> None:
        """Persist a host-generated assistant response."""

    async def ingest_message(
        self,
        user_id: str,
        conversation_id: str,
        role: Literal["user", "assistant"],
        text: str,
        mode: str | None = None,
        occurred_at: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        message_id: str | None = None,
        source_seq: int | str | None = None,
        ingest_origin: str | None = None,
        confirmation_strategy: str | None = None,
        memory_privacy_mode: str | None = None,
        *,
        operational_profile: str | None = None,
        operational_signals: dict[str, Any] | None = None,
        user_persona_id: str | None = None,
        platform_id: str | None = None,
        character_id: str | None = None,
        incognito: bool | None = None,
    ) -> None:
        """Persist one historical or sidecar message."""

    async def close(self) -> None:
        """Close transport resources."""

    async def flush(
        self,
        timeout_seconds: float = 30.0,
        user_id: str | None = None,
    ) -> bool:
        """Wait for pending Atagia sidecar work."""

    async def get_worker_control(self) -> Any:
        """Return the current Atagia background-processing control state."""

    async def set_worker_control(
        self,
        mode: str,
        *,
        reason: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> Any:
        """Set the Atagia background-processing control state."""


ClientFactory = Callable[..., Awaitable[AtagiaClientProtocol]]
ConfigLoader = Callable[[], Awaitable["AtagiaBridgeConfig"]]


@dataclass(frozen=True, slots=True)
class AtagiaBridgeConfig:
    """Aurvek-owned Atagia bridge settings."""

    enabled: bool = False
    transport: TransportName = "auto"
    db_path: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    admin_api_key: str | None = None
    assistant_mode: str = DEFAULT_ASSISTANT_MODE
    platform_id: str = DEFAULT_PLATFORM_ID
    character_id: str | None = None
    user_persona_id: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    operational_profile: str | None = None
    operational_signals: dict[str, Any] = field(default_factory=dict)
    incognito: bool = False
    memory_privacy_mode: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AtagiaBridgeConfig":
        env = environ or os.environ
        transport = _parse_transport(env.get("ATAGIA_TRANSPORT", "auto"))
        return cls(
            enabled=_parse_bool(env.get("ATAGIA_ENABLED")),
            transport=transport,
            db_path=_clean_optional(env.get("ATAGIA_DB_PATH") or env.get("ATAGIA_SQLITE_PATH")),
            base_url=_clean_optional(env.get("ATAGIA_BASE_URL")),
            api_key=_clean_optional(env.get("ATAGIA_SERVICE_API_KEY")),
            admin_api_key=_clean_optional(env.get("ATAGIA_ADMIN_API_KEY")),
            assistant_mode=(
                _clean_optional(env.get("ATAGIA_MODE"))
                or _clean_optional(env.get("ATAGIA_ASSISTANT_MODE"))
                or DEFAULT_ASSISTANT_MODE
            ),
            platform_id=(
                _clean_optional(env.get("ATAGIA_PLATFORM_ID"))
                or DEFAULT_PLATFORM_ID
            ),
            character_id=_clean_optional(env.get("ATAGIA_CHARACTER_ID")),
            user_persona_id=_clean_optional(env.get("ATAGIA_USER_PERSONA_ID")),
            timeout_seconds=_parse_timeout(env.get("ATAGIA_TIMEOUT_SECONDS")),
            operational_profile=_clean_optional(env.get("ATAGIA_OPERATIONAL_PROFILE")),
            incognito=False,
            memory_privacy_mode=_clean_optional(env.get("ATAGIA_MEMORY_PRIVACY_MODE")),
        )


class AtagiaBridge:
    """Aurvek-specific wrapper around Atagia's generic sidecar bridge."""

    def __init__(
        self,
        config: AtagiaBridgeConfig | None = None,
        *,
        client_factory: ClientFactory | None = None,
        config_loader: ConfigLoader | None = None,
    ) -> None:
        self.config = config or AtagiaBridgeConfig.from_env()
        self._client_factory = client_factory
        self._config_loader = config_loader
        self._sidecar_bridge: Any | None = None
        self._sidecar_config: AtagiaBridgeConfig | None = None
        self._last_error: Any | None = None

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    async def is_enabled(self) -> bool:
        """Resolve the loader-backed config and report whether the bridge is on.

        The `enabled` property only reflects the constructor/env snapshot; callers
        gating on bridge availability (deletion guards, remediation) must use this
        so the answer matches what erase_user will actually see.
        """
        return (await self._get_config()).enabled

    @property
    def last_error(self) -> Any | None:
        """Structured details for the latest fail-open Atagia operation."""
        return self._last_error

    async def ensure_user_and_conversation(
        self,
        user_id: int | str,
        conversation_id: int | str,
        *,
        prompt_id: int | str | None = None,
        incognito: bool | None = None,
    ) -> str | None:
        """Ensure Atagia resources exist, returning the Atagia conversation id."""
        config = await self._get_config()
        if not config.enabled:
            self._last_error = None
            return None
        try:
            sidecar = await self._ensure_sidecar_bridge(config)
            namespace = _aurvek_namespace(user_id, conversation_id, prompt_id)
            result = await sidecar.ensure_user_and_conversation(
                namespace["user_id"],
                namespace["conversation_id"],
                user_persona_id=config.user_persona_id,
                platform_id=config.platform_id,
                character_id=_resolve_character_id(config, prompt_id),
                mode=config.assistant_mode,
                incognito=_resolve_incognito(config, incognito),
            )
            self._last_error = None if result is not None else getattr(sidecar, "last_error", None)
            return result
        except Exception as exc:
            self._last_error = _bridge_exception("ensure_user_and_conversation", exc)
            logger.warning(
                "Atagia ensure_user_and_conversation failed; continuing without sidecar memory",
                exc_info=True,
            )
            return None

    async def get_context_for_turn(
        self,
        user_id: int | str,
        conversation_id: int | str,
        message_text: str,
        *,
        occurred_at: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        prompt_id: int | str | None = None,
        message_id: int | str | None = None,
        source_seq: int | str | None = None,
        ingest_origin: str | None = None,
        confirmation_strategy: str | None = None,
        memory_privacy_mode: str | None = None,
        incognito: bool | None = None,
    ) -> Any | None:
        """Return Atagia context for one user turn, or None on disabled/error."""
        started_at = time.perf_counter()
        config = await self._get_config()
        if not config.enabled or not message_text:
            self._last_error = None
            return None
        try:
            sidecar = await self._ensure_sidecar_bridge(config)
            namespace = _aurvek_namespace(user_id, conversation_id, prompt_id)
            result = await sidecar.get_context_for_turn(
                namespace["user_id"],
                namespace["conversation_id"],
                message_text,
                occurred_at=occurred_at,
                attachments=attachments,
                user_persona_id=config.user_persona_id,
                platform_id=config.platform_id,
                character_id=_resolve_character_id(config, prompt_id),
                mode=config.assistant_mode,
                operational_profile=config.operational_profile,
                operational_signals=config.operational_signals,
                incognito=_resolve_incognito(config, incognito),
                message_id=_aurvek_message_id(message_id) if message_id is not None else None,
                source_seq=_resolve_source_seq(source_seq),
                ingest_origin=ingest_origin,
                confirmation_strategy=confirmation_strategy,
                memory_privacy_mode=memory_privacy_mode,
            )
            self._last_error = None if result is not None else getattr(sidecar, "last_error", None)
            latency_ms = _elapsed_ms(started_at)
            if self._last_error:
                record_memory_failure(
                    "atagia",
                    "get_context",
                    message=_bridge_error_text(self._last_error),
                    latency_ms=latency_ms,
                )
            else:
                record_memory_success("atagia", "get_context", latency_ms=latency_ms)
            return result
        except Exception as exc:
            self._last_error = _bridge_exception("get_context_for_turn", exc)
            record_memory_failure(
                "atagia",
                "get_context",
                exception=exc,
                latency_ms=_elapsed_ms(started_at),
            )
            logger.warning(
                "Atagia get_context failed; falling back to Aurvek context",
                exc_info=True,
            )
            return None

    async def record_assistant_response(
        self,
        user_id: int | str,
        conversation_id: int | str,
        response_text: str,
        *,
        occurred_at: str | None = None,
        prompt_id: int | str | None = None,
        message_id: int | str | None = None,
        source_seq: int | str | None = None,
        ingest_origin: str | None = None,
        confirmation_strategy: str | None = None,
        memory_privacy_mode: str | None = None,
        incognito: bool | None = None,
    ) -> bool:
        """Persist the assistant response in Atagia, returning success."""
        started_at = time.perf_counter()
        config = await self._get_config()
        if not config.enabled or not response_text:
            self._last_error = None
            return False
        try:
            sidecar = await self._ensure_sidecar_bridge(config)
            namespace = _aurvek_namespace(user_id, conversation_id, prompt_id)
            result = await sidecar.record_assistant_response(
                namespace["user_id"],
                namespace["conversation_id"],
                response_text,
                occurred_at=occurred_at,
                user_persona_id=config.user_persona_id,
                platform_id=config.platform_id,
                character_id=_resolve_character_id(config, prompt_id),
                mode=config.assistant_mode,
                operational_profile=config.operational_profile,
                operational_signals=config.operational_signals,
                incognito=_resolve_incognito(config, incognito),
                message_id=_aurvek_message_id(message_id) if message_id is not None else None,
                source_seq=_resolve_source_seq(source_seq),
                ingest_origin=ingest_origin,
                confirmation_strategy=confirmation_strategy,
                memory_privacy_mode=memory_privacy_mode,
            )
            self._last_error = None if result else getattr(sidecar, "last_error", None)
            latency_ms = _elapsed_ms(started_at)
            if self._last_error:
                record_memory_failure(
                    "atagia",
                    "record_response",
                    message=_bridge_error_text(self._last_error),
                    latency_ms=latency_ms,
                )
            elif result:
                record_memory_success("atagia", "record_response", latency_ms=latency_ms)
            return result
        except Exception as exc:
            self._last_error = _bridge_exception("record_assistant_response", exc)
            record_memory_failure(
                "atagia",
                "record_response",
                exception=exc,
                latency_ms=_elapsed_ms(started_at),
            )
            logger.warning(
                "Atagia add_response failed; continuing without sidecar persistence",
                exc_info=True,
            )
            return False

    async def ingest_message(
        self,
        user_id: int | str,
        conversation_id: int | str,
        role: Literal["user", "assistant"],
        text: str,
        *,
        occurred_at: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        prompt_id: int | str | None = None,
        message_id: int | str | None = None,
        source_seq: int | str | None = None,
        ingest_origin: str | None = None,
        confirmation_strategy: str | None = None,
        memory_privacy_mode: str | None = None,
        incognito: bool | None = None,
    ) -> bool:
        """Persist a historical Aurvek message in Atagia, returning success."""
        config = await self._get_config()
        if not config.enabled or not text:
            self._last_error = None
            return False
        try:
            sidecar = await self._ensure_sidecar_bridge(config)
            namespace = _aurvek_namespace(user_id, conversation_id, prompt_id)
            result = await sidecar.ingest_message(
                namespace["user_id"],
                namespace["conversation_id"],
                role,
                text,
                occurred_at=occurred_at,
                attachments=attachments,
                user_persona_id=config.user_persona_id,
                platform_id=config.platform_id,
                character_id=_resolve_character_id(config, prompt_id),
                mode=config.assistant_mode,
                operational_profile=config.operational_profile,
                operational_signals=config.operational_signals,
                incognito=_resolve_incognito(config, incognito),
                message_id=_aurvek_message_id(message_id) if message_id is not None else None,
                source_seq=_resolve_source_seq(source_seq),
                ingest_origin=ingest_origin,
                confirmation_strategy=confirmation_strategy,
                memory_privacy_mode=memory_privacy_mode,
            )
            self._last_error = None if result else getattr(sidecar, "last_error", None)
            return result
        except Exception as exc:
            self._last_error = _bridge_exception("ingest_message", exc)
            logger.warning(
                "Atagia ingest_message failed; continuing without sidecar persistence",
                exc_info=True,
            )
            return False

    async def get_memory_preferences(self, user_id: int | str) -> dict[str, Any]:
        """Return Atagia user memory preferences, fail-open to defaults."""
        started_at = time.perf_counter()
        config = await self._get_config()
        if not config.enabled:
            self._last_error = None
            return _unavailable_memory_preferences(user_id, "Atagia is disabled.")
        try:
            preferences = await _get_memory_preferences_via_transport(
                config,
                _aurvek_user_id(user_id),
            )
            self._last_error = None
            preferences["available"] = True
            record_memory_success(
                "atagia",
                "preferences",
                latency_ms=_elapsed_ms(started_at),
            )
            return preferences
        except Exception as exc:
            self._last_error = _bridge_exception("get_memory_preferences", exc)
            record_memory_failure(
                "atagia",
                "preferences",
                exception=exc,
                latency_ms=_elapsed_ms(started_at),
            )
            logger.warning("Atagia memory preference fetch failed", exc_info=True)
            return _unavailable_memory_preferences(user_id, str(exc))

    async def set_memory_preferences(
        self,
        user_id: int | str,
        *,
        remember_across_chats: bool | None = None,
        remember_across_devices: bool | None = None,
        memory_privacy_mode: str | None = None,
    ) -> dict[str, Any]:
        """Persist Atagia user memory preferences."""
        started_at = time.perf_counter()
        config = await self._get_config()
        if not config.enabled:
            self._last_error = None
            return _unavailable_memory_preferences(user_id, "Atagia is disabled.")
        try:
            preferences = await _set_memory_preferences_via_transport(
                config,
                _aurvek_user_id(user_id),
                remember_across_chats=remember_across_chats,
                remember_across_devices=remember_across_devices,
                memory_privacy_mode=memory_privacy_mode,
            )
            self._last_error = None
            preferences["available"] = True
            record_memory_success(
                "atagia",
                "preferences_update",
                latency_ms=_elapsed_ms(started_at),
            )
            return preferences
        except Exception as exc:
            self._last_error = _bridge_exception("set_memory_preferences", exc)
            record_memory_failure(
                "atagia",
                "preferences_update",
                exception=exc,
                latency_ms=_elapsed_ms(started_at),
            )
            logger.warning("Atagia memory preference update failed", exc_info=True)
            return _unavailable_memory_preferences(user_id, str(exc))

    async def purge_conversation(
        self,
        user_id: int | str,
        conversation_id: int | str,
        *,
        prompt_id: int | str | None = None,
        incognito: bool | None = None,
    ) -> bool:
        """Best-effort hard purge of a host conversation in Atagia."""
        config = await self._get_config()
        if not config.enabled:
            self._last_error = None
            return False
        try:
            namespace = _aurvek_namespace(user_id, conversation_id, prompt_id)
            await _purge_conversation_via_transport(
                config,
                user_id=str(namespace["user_id"]),
                conversation_id=str(namespace["conversation_id"]),
                character_id=_resolve_character_id(config, prompt_id),
                incognito=_resolve_incognito(config, incognito),
            )
            self._last_error = None
            return True
        except Exception as exc:
            self._last_error = _bridge_exception("purge_conversation", exc)
            logger.warning(
                "Atagia conversation purge failed for conversation_id=%s",
                conversation_id,
                exc_info=True,
            )
            return False

    async def erase_user(self, user_id: int | str) -> dict[str, Any]:
        """Erase all Atagia data for one Aurvek user (fail-closed).

        Unlike the fail-open memory operations, account erasure must be
        authoritative: the local account cascade cannot proceed unless the remote
        purge is confirmed. A response of "already erased"/tombstoned/not found is
        treated as idempotent success; any other failure raises so the caller can
        abort without losing the references needed to retry.
        """
        config = await self._get_config()
        if not config.enabled:
            raise RuntimeError("Atagia bridge is disabled; cannot erase user")
        atagia_user_id = _aurvek_user_id(user_id)
        try:
            report = await _erase_user_via_transport(config, atagia_user_id)
            self._last_error = None
            return report
        except Exception as exc:
            self._last_error = _bridge_exception("erase_user", exc)
            logger.error(
                "Atagia erase_user failed for user_id=%s; aborting deletion",
                user_id,
                exc_info=True,
            )
            raise

    async def test_connection(self) -> tuple[bool, str]:
        """Best-effort admin connection check using real Atagia resources."""
        started_at = time.perf_counter()
        config = await self._get_config()
        if not config.enabled:
            self._last_error = None
            return False, "Atagia is disabled."
        try:
            sidecar = await self._ensure_sidecar_bridge(config)
            ok, message = await sidecar.test_connection()
            self._last_error = None if ok else getattr(sidecar, "last_error", None)
            latency_ms = _elapsed_ms(started_at)
            if ok:
                record_memory_success("atagia", "test_connection", latency_ms=latency_ms)
            else:
                record_memory_failure(
                    "atagia",
                    "test_connection",
                    message=message or _bridge_error_text(self._last_error),
                    latency_ms=latency_ms,
                    unavailable=True,
                )
            return ok, message
        except Exception as exc:
            self._last_error = _bridge_exception("test_connection", exc)
            record_memory_failure(
                "atagia",
                "test_connection",
                exception=exc,
                latency_ms=_elapsed_ms(started_at),
                unavailable=True,
            )
            logger.warning("Atagia connection test failed", exc_info=True)
            return False, str(exc)

    async def flush(
        self,
        timeout_seconds: float | None = None,
        *,
        user_id: int | str | None = None,
    ) -> bool:
        """Wait for pending Atagia sidecar work, returning success."""
        config = await self._get_config()
        if not config.enabled:
            self._last_error = None
            return False
        try:
            sidecar = await self._ensure_sidecar_bridge(config)
            result = await sidecar.flush(
                timeout_seconds or config.timeout_seconds,
                user_id=_aurvek_user_id(user_id) if user_id is not None else None,
            )
            self._last_error = None if result else getattr(sidecar, "last_error", None)
            return result
        except Exception as exc:
            self._last_error = _bridge_exception("flush", exc)
            logger.warning(
                "Atagia flush failed; continuing without blocking Aurvek",
                exc_info=True,
            )
            return False

    async def get_worker_control(self) -> Any | None:
        """Return Atagia processing control state, or None on disabled/error."""
        config = await self._get_config()
        if not config.enabled:
            self._last_error = None
            return None
        try:
            sidecar = await self._ensure_sidecar_bridge(config)
            state = await sidecar.get_worker_control()
            self._last_error = None if state is not None else getattr(sidecar, "last_error", None)
            return state
        except Exception as exc:
            self._last_error = _bridge_exception("get_worker_control", exc)
            logger.warning(
                "Atagia get_worker_control failed; continuing without admin state",
                exc_info=True,
            )
            return None

    async def set_worker_control(
        self,
        mode: str,
        *,
        reason: str | None = None,
        timeout_seconds: float | None = None,
    ) -> Any | None:
        """Set Atagia processing control state, fail-open on transport errors."""
        config = await self._get_config()
        if not config.enabled:
            self._last_error = None
            return None
        try:
            sidecar = await self._ensure_sidecar_bridge(config)
            state = await sidecar.set_worker_control(
                mode,
                reason=reason,
                timeout_seconds=timeout_seconds or config.timeout_seconds,
            )
            self._last_error = None if state is not None else getattr(sidecar, "last_error", None)
            return state
        except Exception as exc:
            self._last_error = _bridge_exception("set_worker_control", exc)
            logger.warning(
                "Atagia set_worker_control failed; continuing host operation",
                exc_info=True,
            )
            return None

    async def pause_new_jobs(self, *, reason: str | None = None) -> Any | None:
        """Ask Atagia to store new messages without creating new jobs."""
        return await self.set_worker_control("pause_new_jobs", reason=reason)

    async def drain_and_pause(
        self,
        *,
        reason: str | None = None,
        timeout_seconds: float | None = None,
    ) -> Any | None:
        """Ask Atagia to drain queued work, then stay paused."""
        return await self.set_worker_control(
            "drain_and_pause",
            reason=reason,
            timeout_seconds=timeout_seconds,
        )

    async def hard_pause(self, *, reason: str | None = None) -> Any | None:
        """Ask Atagia workers to stop claiming any further work."""
        return await self.set_worker_control("hard_pause", reason=reason)

    async def resume_processing(self, *, reason: str | None = None) -> Any | None:
        """Resume normal Atagia background processing."""
        return await self.set_worker_control("active", reason=reason)

    async def close(self) -> None:
        if self._sidecar_bridge is None:
            return
        await self._sidecar_bridge.close()
        self._sidecar_bridge = None
        self._sidecar_config = None

    async def _get_config(self) -> AtagiaBridgeConfig:
        if self._config_loader is None:
            return self.config
        loaded = await self._config_loader()
        if loaded != self.config:
            await self.close()
            self.config = loaded
        return self.config

    async def _ensure_sidecar_bridge(self, config: AtagiaBridgeConfig) -> Any:
        if self._sidecar_bridge is not None and self._sidecar_config == config:
            return self._sidecar_bridge

        await self.close()
        sidecar_cls, sidecar_config_cls = _load_sidecar_bridge_classes()
        sidecar_config = sidecar_config_cls(
            enabled=config.enabled,
            transport=config.transport,
            db_path=config.db_path,
            base_url=config.base_url,
            api_key=config.api_key,
            admin_api_key=config.admin_api_key,
            mode=config.assistant_mode,
            user_persona_id=config.user_persona_id,
            platform_id=config.platform_id,
            character_id=config.character_id,
            timeout_seconds=config.timeout_seconds,
            operational_profile=config.operational_profile,
            operational_signals=config.operational_signals,
            incognito=False,
            memory_privacy_mode=config.memory_privacy_mode,
        )
        kwargs: dict[str, Any] = {}
        if self._client_factory is not None:
            kwargs["client_factory"] = self._client_factory
        self._sidecar_bridge = sidecar_cls(sidecar_config, **kwargs)
        self._sidecar_config = config
        return self._sidecar_bridge


_default_bridge: AtagiaBridge | None = None
_erasure_bridge: AtagiaBridge | None = None


def get_atagia_bridge() -> AtagiaBridge:
    """Return the process-wide Atagia bridge singleton."""
    global _default_bridge
    if _default_bridge is None:
        _default_bridge = AtagiaBridge(config_loader=_default_config_loader)
    return _default_bridge


def get_atagia_erasure_bridge() -> AtagiaBridge:
    """Return the bridge used for account-erasure operations.

    Gated on data custody (atagia_enabled) rather than on the active runtime
    memory provider: erasure must keep working while the provider is switched
    off but the Atagia store still holds user data.
    """
    global _erasure_bridge
    if _erasure_bridge is None:
        _erasure_bridge = AtagiaBridge(config_loader=_erasure_config_loader)
    return _erasure_bridge


async def get_context_for_turn(
    user_id: int | str,
    conversation_id: int | str,
    message_text: str,
    *,
    occurred_at: str | None = None,
    prompt_id: int | str | None = None,
    message_id: int | str | None = None,
    source_seq: int | str | None = None,
    ingest_origin: str | None = None,
    confirmation_strategy: str | None = None,
    memory_privacy_mode: str | None = None,
    incognito: bool | None = None,
) -> Any | None:
    """Module-level convenience wrapper for chat integration."""
    return await get_atagia_bridge().get_context_for_turn(
        user_id,
        conversation_id,
        message_text,
        occurred_at=occurred_at,
        prompt_id=prompt_id,
        message_id=message_id,
        source_seq=source_seq,
        ingest_origin=ingest_origin,
        confirmation_strategy=confirmation_strategy,
        memory_privacy_mode=memory_privacy_mode,
        incognito=incognito,
    )


async def record_assistant_response(
    user_id: int | str,
    conversation_id: int | str,
    response_text: str,
    *,
    occurred_at: str | None = None,
    prompt_id: int | str | None = None,
    message_id: int | str | None = None,
    source_seq: int | str | None = None,
    ingest_origin: str | None = None,
    confirmation_strategy: str | None = None,
    memory_privacy_mode: str | None = None,
    incognito: bool | None = None,
) -> bool:
    """Module-level convenience wrapper for response persistence."""
    return await get_atagia_bridge().record_assistant_response(
        user_id,
        conversation_id,
        response_text,
        occurred_at=occurred_at,
        prompt_id=prompt_id,
        message_id=message_id,
        source_seq=source_seq,
        ingest_origin=ingest_origin,
        confirmation_strategy=confirmation_strategy,
        memory_privacy_mode=memory_privacy_mode,
        incognito=incognito,
    )


async def ingest_message(
    user_id: int | str,
    conversation_id: int | str,
    role: Literal["user", "assistant"],
    text: str,
    *,
    occurred_at: str | None = None,
    prompt_id: int | str | None = None,
    message_id: int | str | None = None,
    source_seq: int | str | None = None,
    ingest_origin: str | None = None,
    confirmation_strategy: str | None = None,
    memory_privacy_mode: str | None = None,
    incognito: bool | None = None,
) -> bool:
    """Module-level convenience wrapper for historical message persistence."""
    return await get_atagia_bridge().ingest_message(
        user_id,
        conversation_id,
        role,
        text,
        occurred_at=occurred_at,
        prompt_id=prompt_id,
        message_id=message_id,
        source_seq=source_seq,
        ingest_origin=ingest_origin,
        confirmation_strategy=confirmation_strategy,
        memory_privacy_mode=memory_privacy_mode,
        incognito=incognito,
    )


async def erase_atagia_user(user_id: int | str) -> dict[str, Any]:
    """Module-level convenience wrapper for account erasure (fail-closed)."""
    return await get_atagia_bridge().erase_user(user_id)


async def flush_atagia_bridge(
    timeout_seconds: float | None = None,
    *,
    user_id: int | str | None = None,
) -> bool:
    """Flush pending Atagia work if the configured transport supports it."""
    return await get_atagia_bridge().flush(timeout_seconds, user_id=user_id)


async def get_worker_control() -> Any | None:
    """Return Atagia processing control state if available."""
    return await get_atagia_bridge().get_worker_control()


async def set_worker_control(
    mode: str,
    *,
    reason: str | None = None,
    timeout_seconds: float | None = None,
) -> Any | None:
    """Set Atagia processing control state if available."""
    return await get_atagia_bridge().set_worker_control(
        mode,
        reason=reason,
        timeout_seconds=timeout_seconds,
    )


async def close_atagia_bridge() -> None:
    """Close the process-wide bridge client if it has been initialized."""
    if _default_bridge is not None:
        await _default_bridge.close()


async def reset_atagia_bridge() -> None:
    """Close and drop the process-wide bridges so fresh config is loaded."""
    global _default_bridge, _erasure_bridge
    if _default_bridge is not None:
        await _default_bridge.close()
    _default_bridge = None
    if _erasure_bridge is not None:
        await _erasure_bridge.close()
    _erasure_bridge = None


async def _default_config_loader() -> AtagiaBridgeConfig:
    from atagia_config import get_atagia_bridge_config

    return await get_atagia_bridge_config()


async def _erasure_config_loader() -> AtagiaBridgeConfig:
    from atagia_config import get_atagia_erasure_bridge_config

    return await get_atagia_erasure_bridge_config()


def _load_sidecar_bridge_classes() -> tuple[Any, Any]:
    from atagia.integrations import SidecarBridge, SidecarBridgeConfig

    return SidecarBridge, SidecarBridgeConfig


def _aurvek_namespace(
    user_id: int | str,
    conversation_id: int | str,
    prompt_id: int | str | None = None,
) -> dict[str, str | None]:
    try:
        from atagia.integrations import AurvekNamespace

        namespace = AurvekNamespace.from_ids(
            user_id=user_id,
            conversation_id=conversation_id,
            prompt_id=prompt_id,
        )
        return {
            "user_id": namespace.user_id,
            "conversation_id": namespace.conversation_id,
            "character_id": namespace.character_id,
        }
    except Exception:
        return {
            "user_id": _fallback_aurvek_user_id(user_id),
            "conversation_id": _fallback_aurvek_conversation_id(conversation_id),
            "character_id": (
                _fallback_aurvek_prompt_character_id(prompt_id)
                if prompt_id is not None
                else None
            ),
        }


def _resolve_character_id(
    config: AtagiaBridgeConfig,
    prompt_id: int | str | None,
) -> str | None:
    if config.character_id:
        return config.character_id
    if prompt_id is not None:
        try:
            from atagia.integrations import aurvek_prompt_character_id

            return aurvek_prompt_character_id(prompt_id)
        except Exception:
            return _fallback_aurvek_prompt_character_id(prompt_id)
    return config.character_id


def _resolve_incognito(
    config: AtagiaBridgeConfig,
    incognito: bool | None,
) -> bool:
    return bool(incognito)


def _aurvek_message_id(value: int | str) -> str:
    text = str(value).strip()
    if text.startswith("aurvek:msg:"):
        return text
    try:
        from atagia.integrations import aurvek_message_id

        return aurvek_message_id(text)
    except Exception:
        return f"aurvek:msg:{_id_part(text)}"


def _resolve_source_seq(value: int | str | None) -> int | None:
    if value is None:
        return None
    resolved = int(value)
    if resolved < 1:
        raise ValueError("source_seq must be a positive integer")
    return resolved


def _bridge_exception(operation: str, exc: Exception) -> dict[str, str]:
    message = str(exc)
    if (
        isinstance(exc, TypeError)
        and "unexpected keyword argument 'proxies'" in message
    ):
        message = (
            f"{message}. This is an Atagia/httpx compatibility issue: the "
            "Atagia package is calling httpx.AsyncClient(proxies=...), which "
            "was removed in httpx 0.28. Update Atagia to use the current httpx "
            "API, or run Atagia with httpx<0.28."
        )
    return {
        "operation": operation,
        "error_type": exc.__class__.__name__,
        "message": message,
    }


def _bridge_error_text(error: Any) -> str:
    if not error:
        return ""
    if isinstance(error, Mapping):
        return str(error.get("message") or error)
    return str(error)


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 1)


def _aurvek_user_id(value: int | str) -> str:
    text = str(value).strip()
    if text.startswith("aurvek:user:"):
        return text
    try:
        from atagia.integrations import aurvek_user_id

        return aurvek_user_id(text)
    except Exception:
        return _fallback_aurvek_user_id(text)


def _fallback_aurvek_user_id(value: int | str) -> str:
    text = str(value).strip()
    if text.startswith("aurvek:user:"):
        return text
    return f"aurvek:user:{_id_part(text)}"


def _fallback_aurvek_conversation_id(value: int | str) -> str:
    text = str(value).strip()
    if text.startswith("aurvek:conv:"):
        return text
    return f"aurvek:conv:{_id_part(text)}"


def _fallback_aurvek_prompt_character_id(value: int | str) -> str:
    text = str(value).strip()
    if text.startswith("prompt:"):
        return text
    return f"prompt:{_id_part(text)}"


async def _get_memory_preferences_via_transport(
    config: AtagiaBridgeConfig,
    atagia_user_id: str,
) -> dict[str, Any]:
    async def _local(engine: Any) -> dict[str, Any]:
        await engine.create_user(atagia_user_id)
        return _normalize_preferences(await engine.get_memory_preferences(atagia_user_id))

    if _resolve_transport(config) == "http":
        return await _http_get_memory_preferences(config, atagia_user_id)
    return await _with_local_engine(config, _local)


async def _set_memory_preferences_via_transport(
    config: AtagiaBridgeConfig,
    atagia_user_id: str,
    *,
    remember_across_chats: bool | None,
    remember_across_devices: bool | None,
    memory_privacy_mode: str | None,
) -> dict[str, Any]:
    async def _local(engine: Any) -> dict[str, Any]:
        await engine.create_user(atagia_user_id)
        return _normalize_preferences(
            await engine.set_memory_preferences(
                atagia_user_id,
                remember_across_chats=remember_across_chats,
                remember_across_devices=remember_across_devices,
                memory_privacy_mode=memory_privacy_mode,
            )
        )

    if _resolve_transport(config) == "http":
        return await _http_set_memory_preferences(
            config,
            atagia_user_id,
            remember_across_chats=remember_across_chats,
            remember_across_devices=remember_across_devices,
            memory_privacy_mode=memory_privacy_mode,
        )
    return await _with_local_engine(config, _local)


async def _purge_conversation_via_transport(
    config: AtagiaBridgeConfig,
    *,
    user_id: str,
    conversation_id: str,
    character_id: str | None,
    incognito: bool,
) -> None:
    async def _local(engine: Any) -> None:
        try:
            await engine.close_conversation(
                user_id,
                conversation_id,
                purge=True,
                confirmation="PURGE_ON_CLOSE",
            )
        except Exception:
            await engine.delete_conversation(
                user_id,
                conversation_id,
                confirmation="DELETE_CONVERSATION",
            )

    if _resolve_transport(config) == "http":
        await _http_purge_conversation(
            config,
            user_id=user_id,
            conversation_id=conversation_id,
            character_id=character_id,
            incognito=incognito,
        )
        return
    await _with_local_engine(config, _local)


ATAGIA_ERASE_CONFIRMATION = "ERASE_ALL_DATA"


async def _erase_user_via_transport(
    config: AtagiaBridgeConfig,
    atagia_user_id: str,
) -> dict[str, Any]:
    async def _local(engine: Any) -> dict[str, Any]:
        report = await engine.erase_user_data(
            atagia_user_id,
            confirmation=ATAGIA_ERASE_CONFIRMATION,
        )
        return _normalize_erasure_report(report, atagia_user_id)

    if _resolve_transport(config) == "http":
        return await _http_erase_user(config, atagia_user_id)
    return await _with_local_engine(config, _local)


async def _http_erase_user(
    config: AtagiaBridgeConfig,
    atagia_user_id: str,
) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(
        base_url=(config.base_url or "").rstrip("/"),
        timeout=config.timeout_seconds,
    ) as client:
        response = await client.post(
            f"/v1/users/{_atagia_path_segment(atagia_user_id)}/erase",
            json={
                "user_id": atagia_user_id,
                "confirmation": ATAGIA_ERASE_CONFIRMATION,
            },
            headers=_http_headers(config, atagia_user_id),
        )
        # The erase endpoint reports a missing/already-erased user as HTTP 200
        # with already_erased=true, so any error status (including 404 from a
        # misrouted base_url) must fail closed rather than fake success.
        response.raise_for_status()
        return _normalize_erasure_report(response.json(), atagia_user_id)


def _normalize_erasure_report(value: Any, atagia_user_id: str) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        data = value.model_dump()
    elif isinstance(value, dict):
        data = dict(value)
    else:
        data = {
            "user_id": getattr(value, "user_id", atagia_user_id),
            "already_erased": bool(getattr(value, "already_erased", False)),
        }
    data.setdefault("user_id", atagia_user_id)
    data.setdefault("already_erased", False)
    return data


async def _with_local_engine(
    config: AtagiaBridgeConfig,
    operation: Callable[[Any], Awaitable[Any]],
) -> Any:
    from atagia import Atagia

    engine = Atagia(db_path=config.db_path or "db/atagia.db")
    await engine.setup()
    try:
        return await operation(engine)
    finally:
        await engine.close()


async def _http_get_memory_preferences(
    config: AtagiaBridgeConfig,
    atagia_user_id: str,
) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(
        base_url=(config.base_url or "").rstrip("/"),
        timeout=config.timeout_seconds,
    ) as client:
        await _http_ensure_user(client, config, atagia_user_id)
        response = await client.get(
            f"/v1/users/{_atagia_path_segment(atagia_user_id)}/memory-preferences",
            headers=_http_headers(config, atagia_user_id),
        )
        response.raise_for_status()
        return _normalize_preferences(response.json())


async def _http_set_memory_preferences(
    config: AtagiaBridgeConfig,
    atagia_user_id: str,
    *,
    remember_across_chats: bool | None,
    remember_across_devices: bool | None,
    memory_privacy_mode: str | None,
) -> dict[str, Any]:
    import httpx

    payload = {
        key: value
        for key, value in {
            "remember_across_chats": remember_across_chats,
            "remember_across_devices": remember_across_devices,
            "memory_privacy_mode": memory_privacy_mode,
        }.items()
        if value is not None
    }
    async with httpx.AsyncClient(
        base_url=(config.base_url or "").rstrip("/"),
        timeout=config.timeout_seconds,
    ) as client:
        await _http_ensure_user(client, config, atagia_user_id)
        response = await client.put(
            f"/v1/users/{_atagia_path_segment(atagia_user_id)}/memory-preferences",
            json=payload,
            headers=_http_headers(config, atagia_user_id),
        )
        response.raise_for_status()
        return _normalize_preferences(response.json())


async def _http_purge_conversation(
    config: AtagiaBridgeConfig,
    *,
    user_id: str,
    conversation_id: str,
    character_id: str | None,
    incognito: bool,
) -> None:
    import httpx

    payload_base = {
        "user_id": user_id,
        "platform_id": config.platform_id,
        "user_persona_id": config.user_persona_id,
        "character_id": character_id,
        "incognito": incognito,
    }
    async with httpx.AsyncClient(
        base_url=(config.base_url or "").rstrip("/"),
        timeout=config.timeout_seconds,
    ) as client:
        try:
            response = await client.post(
                f"/v1/conversations/{_atagia_path_segment(conversation_id)}/close",
                json={
                    **payload_base,
                    "purge": True,
                    "confirmation": "PURGE_ON_CLOSE",
                },
                headers=_http_headers(config, user_id),
            )
            if response.status_code == 404:
                return
            response.raise_for_status()
        except Exception:
            response = await client.post(
                f"/v1/conversations/{_atagia_path_segment(conversation_id)}/delete",
                json={**payload_base, "confirmation": "DELETE_CONVERSATION"},
                headers=_http_headers(config, user_id),
            )
            if response.status_code == 404:
                return
            response.raise_for_status()


async def _http_ensure_user(
    client: Any,
    config: AtagiaBridgeConfig,
    atagia_user_id: str,
) -> None:
    response = await client.post(
        "/v1/users",
        json={"user_id": atagia_user_id},
        headers=_http_headers(config, atagia_user_id),
    )
    response.raise_for_status()


def _http_headers(config: AtagiaBridgeConfig, atagia_user_id: str) -> dict[str, str]:
    if not config.base_url:
        raise ValueError("Atagia base URL is required for HTTP transport")
    if not config.api_key:
        raise ValueError("Atagia service API key is required for HTTP transport")
    return {
        "Authorization": f"Bearer {config.api_key}",
        "X-Atagia-User-Id": atagia_user_id,
    }


def _resolve_transport(config: AtagiaBridgeConfig) -> str:
    if config.transport == "auto":
        return "http" if config.base_url else "local"
    return config.transport


def _atagia_path_segment(value: str) -> str:
    try:
        from atagia.transport_ids import encode_path_id

        return encode_path_id(value)
    except Exception:
        return quote(value, safe="")


def _normalize_preferences(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        data = value.model_dump()
    elif isinstance(value, dict):
        data = dict(value)
    else:
        data = {
            "user_id": getattr(value, "user_id", ""),
            "remember_across_chats": getattr(value, "remember_across_chats", True),
            "remember_across_devices": getattr(value, "remember_across_devices", True),
            "memory_privacy_mode": getattr(value, "memory_privacy_mode", "balanced"),
        }
    data.setdefault("remember_across_chats", True)
    data.setdefault("remember_across_devices", True)
    data.setdefault("memory_privacy_mode", "balanced")
    privacy_mode = data.get("memory_privacy_mode")
    if hasattr(privacy_mode, "value"):
        privacy_mode = privacy_mode.value
    data["memory_privacy_mode"] = (
        privacy_mode if privacy_mode in {"balanced", "trusted_private"} else "balanced"
    )
    return data


def _unavailable_memory_preferences(
    user_id: int | str,
    message: str,
) -> dict[str, Any]:
    return {
        "user_id": _aurvek_user_id(user_id),
        "remember_across_chats": True,
        "remember_across_devices": True,
        "memory_privacy_mode": "balanced",
        "available": False,
        "message": message,
    }


def _parse_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE_VALUES


def _parse_bool_default(value: str | None, default: bool) -> bool:
    normalized = (value or "").strip().lower()
    if not normalized:
        return default
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    logger.warning("Invalid boolean value %r; using default %s", value, default)
    return default


def _parse_transport(value: str | None) -> TransportName:
    transport = (value or "auto").strip().lower()
    if transport not in _VALID_TRANSPORTS:
        logger.warning("Unknown ATAGIA_TRANSPORT=%r; using auto", value)
        return "auto"
    return transport  # type: ignore[return-value]


def _parse_timeout(value: str | None) -> float:
    if value is None or not value.strip():
        return DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = float(value)
    except ValueError:
        logger.warning("Invalid ATAGIA_TIMEOUT_SECONDS=%r; using default", value)
        return DEFAULT_TIMEOUT_SECONDS
    if timeout <= 0:
        logger.warning("Invalid ATAGIA_TIMEOUT_SECONDS=%r; using default", value)
        return DEFAULT_TIMEOUT_SECONDS
    return timeout


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _id_part(value: int | str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("Aurvek IDs must be non-empty")
    return text
