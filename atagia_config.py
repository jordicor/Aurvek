"""Configuration helpers for the Atagia memory integration."""

from __future__ import annotations

import ipaddress
import os
import time
from typing import Any
from urllib.parse import urlparse

import database
from atagia_bridge import (
    DEFAULT_ASSISTANT_MODE,
    DEFAULT_PLATFORM_ID,
    DEFAULT_TIMEOUT_SECONDS,
    AtagiaBridgeConfig,
)
from log_config import logger


_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_VALID_TRANSPORTS = {"auto", "local", "http"}
_CONFIG_CACHE_TTL_SECONDS = 300
_config_cache: dict[str, str] | None = None
_config_cache_time = 0.0


def _env_defaults() -> dict[str, str]:
    return {
        "atagia_enabled": _bool_to_text(_parse_bool(os.getenv("ATAGIA_ENABLED"))),
        "atagia_transport": _clean(os.getenv("ATAGIA_TRANSPORT")) or "auto",
        "atagia_db_path": _clean(os.getenv("ATAGIA_DB_PATH")) or "db/atagia.db",
        "atagia_base_url": _clean(os.getenv("ATAGIA_BASE_URL")) or "",
        "atagia_service_api_key": _clean(os.getenv("ATAGIA_SERVICE_API_KEY")) or "",
        "atagia_admin_api_key": _clean(os.getenv("ATAGIA_ADMIN_API_KEY")) or "",
        "atagia_mode": (
            _clean(os.getenv("ATAGIA_MODE"))
            or _clean(os.getenv("ATAGIA_ASSISTANT_MODE"))
            or DEFAULT_ASSISTANT_MODE
        ),
        "atagia_assistant_mode": (
            _clean(os.getenv("ATAGIA_MODE"))
            or _clean(os.getenv("ATAGIA_ASSISTANT_MODE"))
            or DEFAULT_ASSISTANT_MODE
        ),
        "atagia_platform_id": _clean(os.getenv("ATAGIA_PLATFORM_ID")) or DEFAULT_PLATFORM_ID,
        "atagia_character_id": _clean(os.getenv("ATAGIA_CHARACTER_ID")) or "",
        "atagia_user_persona_id": _clean(os.getenv("ATAGIA_USER_PERSONA_ID")) or "",
        "atagia_operational_profile": _clean(os.getenv("ATAGIA_OPERATIONAL_PROFILE")) or "",
        "atagia_incognito": "false",
        "atagia_timeout_seconds": (
            _clean(os.getenv("ATAGIA_TIMEOUT_SECONDS"))
            or str(DEFAULT_TIMEOUT_SECONDS)
        ),
    }


async def get_atagia_config() -> dict[str, str]:
    """Load Atagia config from SYSTEM_CONFIG, falling back to env/defaults."""
    global _config_cache, _config_cache_time

    now = time.time()
    if _config_cache is not None and (now - _config_cache_time) < _CONFIG_CACHE_TTL_SECONDS:
        return dict(_config_cache)

    config = _env_defaults()
    try:
        async with database.get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT key, value FROM SYSTEM_CONFIG WHERE key LIKE 'atagia_%'"
            )
            rows = await cursor.fetchall()
            for row in rows:
                config[str(row[0])] = "" if row[1] is None else str(row[1])
    except Exception as exc:
        logger.error("Failed to load Atagia config from DB: %s", exc)

    normalized = _normalize_config(config)
    _config_cache = normalized
    _config_cache_time = now
    return dict(normalized)


async def get_atagia_bridge_config() -> AtagiaBridgeConfig:
    """Return bridge settings derived from admin config."""
    config = await get_atagia_config()
    enabled_override = None
    try:
        from memory.config import get_active_memory_provider

        enabled_override = (
            _parse_bool(config.get("atagia_enabled"))
            and await get_active_memory_provider() == "atagia"
        )
    except Exception:
        enabled_override = None
    return bridge_config_from_mapping(config, enabled_override=enabled_override)


async def get_atagia_erasure_bridge_config() -> AtagiaBridgeConfig:
    """Return bridge settings for account-erasure operations.

    Erasure availability is a data-custody question, not a runtime one: user
    data already stored in Atagia remains under custody while the active memory
    provider is switched away from it, and account deletion must still be able
    to purge that data. Only atagia_enabled (plus transport settings) governs
    this config -- the active-provider gate applied by get_atagia_bridge_config
    deliberately does NOT.
    """
    config = await get_atagia_config()
    return bridge_config_from_mapping(config)


def bridge_config_from_mapping(
    config: dict[str, Any],
    *,
    enabled_override: bool | None = None,
) -> AtagiaBridgeConfig:
    transport = str(config.get("atagia_transport") or "auto").strip().lower()
    if transport not in _VALID_TRANSPORTS:
        transport = "auto"

    enabled = (
        enabled_override
        if enabled_override is not None
        else _parse_bool(config.get("atagia_enabled"))
    )
    return AtagiaBridgeConfig(
        enabled=bool(enabled),
        transport=transport,  # type: ignore[arg-type]
        db_path=_clean(config.get("atagia_db_path")),
        base_url=_clean(config.get("atagia_base_url")),
        api_key=_clean(config.get("atagia_service_api_key")),
        admin_api_key=_clean(config.get("atagia_admin_api_key")),
        assistant_mode=(
            _clean(config.get("atagia_mode"))
            or _clean(config.get("atagia_assistant_mode"))
            or DEFAULT_ASSISTANT_MODE
        ),
        platform_id=_clean(config.get("atagia_platform_id")) or DEFAULT_PLATFORM_ID,
        character_id=_clean(config.get("atagia_character_id")),
        user_persona_id=_clean(config.get("atagia_user_persona_id")),
        timeout_seconds=_parse_timeout(config.get("atagia_timeout_seconds")),
        operational_profile=_clean(config.get("atagia_operational_profile")),
        incognito=False,
    )


async def save_atagia_admin_config(payload: dict[str, Any]) -> dict[str, str]:
    """Persist admin form values. Blank API key means keep the current secret."""
    current = await get_atagia_config()
    updates = _admin_payload_to_config_updates(payload, current)

    async with database.get_db_connection() as conn:
        columns = await _system_config_columns(conn)
        for key, value in updates.items():
            await _upsert_system_config(conn, columns, key, value)
        await conn.commit()

    invalidate_atagia_config_cache()
    try:
        from memory.config import invalidate_memory_config_cache

        invalidate_memory_config_cache()
    except Exception:
        pass
    fresh = await get_atagia_config()
    return {key: fresh.get(key, "") for key in updates}


async def reset_atagia_admin_config() -> dict[str, str]:
    """Remove saved Atagia overrides so env/default values take effect again."""
    async with database.get_db_connection() as conn:
        await conn.execute("DELETE FROM SYSTEM_CONFIG WHERE key LIKE 'atagia\\_%' ESCAPE '\\'")
        await conn.commit()

    invalidate_atagia_config_cache()
    try:
        from memory.config import invalidate_memory_config_cache

        invalidate_memory_config_cache()
    except Exception:
        pass
    return await get_atagia_config()


async def preview_bridge_config_from_admin_payload(payload: dict[str, Any]) -> AtagiaBridgeConfig:
    """Build a connection-test config from unsaved admin values."""
    current = await get_atagia_config()
    updates = _admin_payload_to_config_updates(payload, current)
    preview = dict(current)
    preview.update(updates)
    return bridge_config_from_mapping(preview, enabled_override=True)


def template_config(config: dict[str, str]) -> dict[str, Any]:
    api_key = config.get("atagia_service_api_key", "")
    admin_api_key = config.get("atagia_admin_api_key", "")
    return {
        "enabled": _parse_bool(config.get("atagia_enabled")),
        "transport": config.get("atagia_transport", "auto"),
        "db_path": config.get("atagia_db_path", "db/atagia.db"),
        "base_url": config.get("atagia_base_url", ""),
        "service_api_key_masked": mask_secret(api_key),
        "has_service_api_key": bool(api_key),
        "admin_api_key_masked": mask_secret(admin_api_key),
        "has_admin_api_key": bool(admin_api_key),
        "mode": config.get("atagia_mode", DEFAULT_ASSISTANT_MODE),
        "assistant_mode": config.get("atagia_mode", config.get("atagia_assistant_mode", DEFAULT_ASSISTANT_MODE)),
        "platform_id": config.get("atagia_platform_id", DEFAULT_PLATFORM_ID),
        "character_id": config.get("atagia_character_id", ""),
        "user_persona_id": config.get("atagia_user_persona_id", ""),
        "operational_profile": config.get("atagia_operational_profile", ""),
        "timeout_seconds": config.get("atagia_timeout_seconds", str(DEFAULT_TIMEOUT_SECONDS)),
    }


def invalidate_atagia_config_cache() -> None:
    global _config_cache, _config_cache_time
    _config_cache = None
    _config_cache_time = 0.0


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) < 16:
        return "****"
    return f"{value[:8]}...{value[-4:]}"


def validate_atagia_base_url(url: str) -> tuple[bool, str]:
    if not url:
        return False, "Base URL is required for HTTP transport."

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False, "Only http:// and https:// schemes are allowed."
    if not parsed.hostname:
        return False, "Base URL must include a host."

    try:
        ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return False, "Host must be an IP literal for SSRF protection."

    if not (ip.is_loopback or ip.is_private) and parsed.scheme != "https":
        return False, "Public Atagia URLs must use https://."
    return True, ""


def _admin_payload_to_config_updates(
    payload: dict[str, Any],
    current: dict[str, str],
    *,
    preserve_blank_secret: bool = True,
) -> dict[str, str]:
    transport = str(payload.get("transport", current.get("atagia_transport", "auto"))).strip().lower()
    if transport not in _VALID_TRANSPORTS:
        raise ValueError("Invalid Atagia transport.")

    timeout = _parse_timeout(payload.get("timeout_seconds", current.get("atagia_timeout_seconds")))
    enabled = _parse_bool(payload.get("enabled", current.get("atagia_enabled")))
    base_url = _clean(payload.get("base_url", current.get("atagia_base_url"))) or ""
    if transport == "http" and (enabled or base_url):
        ok, err = validate_atagia_base_url(base_url)
        if not ok:
            raise ValueError(err)

    mode = (
        _clean(payload.get("mode"))
        or _clean(payload.get("assistant_mode"))
        or _clean(current.get("atagia_mode"))
        or _clean(current.get("atagia_assistant_mode"))
        or DEFAULT_ASSISTANT_MODE
    )
    updates = {
        "atagia_enabled": _bool_to_text(enabled),
        "atagia_transport": transport,
        "atagia_db_path": _clean(payload.get("db_path", current.get("atagia_db_path"))) or "db/atagia.db",
        "atagia_base_url": base_url,
        "atagia_mode": mode,
        "atagia_assistant_mode": mode,
        "atagia_platform_id": (
            _clean(payload.get("platform_id", current.get("atagia_platform_id")))
            or DEFAULT_PLATFORM_ID
        ),
        "atagia_character_id": _clean(payload.get("character_id", current.get("atagia_character_id"))) or "",
        "atagia_user_persona_id": _clean(payload.get("user_persona_id", current.get("atagia_user_persona_id"))) or "",
        "atagia_operational_profile": (
            _clean(payload.get("operational_profile", current.get("atagia_operational_profile")))
            or ""
        ),
        "atagia_incognito": "false",
        "atagia_timeout_seconds": str(timeout),
    }

    api_key = _clean(payload.get("service_api_key"))
    clear_service_api_key = _parse_bool(payload.get("clear_service_api_key"))
    if api_key or clear_service_api_key or not preserve_blank_secret:
        updates["atagia_service_api_key"] = api_key or ""

    admin_api_key = _clean(payload.get("admin_api_key"))
    clear_admin_api_key = _parse_bool(payload.get("clear_admin_api_key"))
    if admin_api_key or clear_admin_api_key or not preserve_blank_secret:
        updates["atagia_admin_api_key"] = admin_api_key or ""

    return updates


def _normalize_config(config: dict[str, str]) -> dict[str, str]:
    normalized = dict(_env_defaults())
    normalized.update(config)
    transport = normalized.get("atagia_transport", "auto").strip().lower()
    normalized["atagia_transport"] = transport if transport in _VALID_TRANSPORTS else "auto"
    normalized["atagia_enabled"] = _bool_to_text(_parse_bool(normalized.get("atagia_enabled")))
    normalized["atagia_timeout_seconds"] = str(_parse_timeout(normalized.get("atagia_timeout_seconds")))
    mode = (
        _clean(normalized.get("atagia_mode"))
        or _clean(normalized.get("atagia_assistant_mode"))
        or DEFAULT_ASSISTANT_MODE
    )
    normalized["atagia_mode"] = mode
    normalized["atagia_assistant_mode"] = mode
    normalized["atagia_platform_id"] = _clean(normalized.get("atagia_platform_id")) or DEFAULT_PLATFORM_ID
    normalized["atagia_character_id"] = _clean(normalized.get("atagia_character_id")) or ""
    normalized["atagia_user_persona_id"] = _clean(normalized.get("atagia_user_persona_id")) or ""
    normalized["atagia_operational_profile"] = _clean(normalized.get("atagia_operational_profile")) or ""
    normalized["atagia_incognito"] = "false"
    normalized["atagia_db_path"] = _clean(normalized.get("atagia_db_path")) or "db/atagia.db"
    normalized["atagia_base_url"] = _clean(normalized.get("atagia_base_url")) or ""
    normalized["atagia_service_api_key"] = _clean(normalized.get("atagia_service_api_key")) or ""
    normalized["atagia_admin_api_key"] = _clean(normalized.get("atagia_admin_api_key")) or ""
    return normalized


async def _system_config_columns(conn: Any) -> set[str]:
    cursor = await conn.execute("PRAGMA table_info(SYSTEM_CONFIG)")
    rows = await cursor.fetchall()
    return {str(row[1]) for row in rows}


async def _upsert_system_config(conn: Any, columns: set[str], key: str, value: str) -> None:
    if "updated_at" in columns:
        await conn.execute(
            "UPDATE SYSTEM_CONFIG SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?",
            (value, key),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (key, value),
        )
    else:
        await conn.execute(
            "UPDATE SYSTEM_CONFIG SET value = ? WHERE key = ?",
            (value, key),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)",
            (key, value),
        )


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _TRUE_VALUES


def _bool_to_text(value: bool) -> str:
    return "true" if value else "false"


def _parse_timeout(value: Any) -> float:
    try:
        timeout = float(str(value or "").strip())
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    if timeout <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return timeout


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None
