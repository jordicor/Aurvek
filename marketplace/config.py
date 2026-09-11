"""Feature flags for Aurvek marketplace surfaces."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from fastapi import HTTPException


_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


class MarketplaceConfigError(ValueError):
    """Stable validation code; the owning UI translates at its request boundary."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class MarketplaceFlagDefinition:
    key: str
    env_var: str
    label: str
    description: str
    default: bool = True


@dataclass(frozen=True, slots=True)
class MarketplaceFlags:
    enabled: bool
    public_landings_enabled: bool
    checkout_enabled: bool
    storefronts_enabled: bool
    discovery_enabled: bool
    creator_tools_enabled: bool


@dataclass(frozen=True, slots=True)
class MarketplaceFlagState:
    key: str
    env_var: str
    label: str
    description: str
    default: bool
    desired: bool
    effective: bool
    source: str
    env_override: bool
    env_value: str | None
    stored_value: str | None


MARKETPLACE_FLAG_DEFINITIONS = (
    MarketplaceFlagDefinition(
        key="marketplace_enabled",
        env_var="MARKETPLACE_ENABLED",
        label="Marketplace",
        description="Master control for all public and commercial marketplace surfaces.",
    ),
    MarketplaceFlagDefinition(
        key="marketplace_public_landings_enabled",
        env_var="MARKETPLACE_PUBLIC_LANDINGS_ENABLED",
        label="Public landings",
        description="Prompt and pack landing pages, including public landing assets.",
    ),
    MarketplaceFlagDefinition(
        key="marketplace_checkout_enabled",
        env_var="MARKETPLACE_CHECKOUT_ENABLED",
        label="Checkout",
        description="Purchases, free acquisitions, Stripe fulfillment, and paid access grants.",
    ),
    MarketplaceFlagDefinition(
        key="marketplace_storefronts_enabled",
        env_var="MARKETPLACE_STOREFRONTS_ENABLED",
        label="Storefronts",
        description="Creator storefront pages and storefront management.",
    ),
    MarketplaceFlagDefinition(
        key="marketplace_discovery_enabled",
        env_var="MARKETPLACE_DISCOVERY_ENABLED",
        label="Discovery",
        description="Explore pages, public catalogs, and public marketplace APIs.",
    ),
    MarketplaceFlagDefinition(
        key="marketplace_creator_tools_enabled",
        env_var="MARKETPLACE_CREATOR_TOOLS_ENABLED",
        label="Creator tools",
        description="Publishing, landing builders, analytics, earnings, and creator controls.",
    ),
)
MARKETPLACE_CONFIG_KEYS = tuple(item.key for item in MARKETPLACE_FLAG_DEFINITIONS)
_FLAG_BY_KEY = {item.key: item for item in MARKETPLACE_FLAG_DEFINITIONS}

_runtime_values: dict[str, bool] = {}
_runtime_raw_values: dict[str, str] = {}
_runtime_loaded = False


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return default


def _bool_to_text(value: bool) -> str:
    return "true" if value else "false"


def _strict_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise MarketplaceConfigError(f"Invalid boolean value: {value!r}", "invalid_boolean")


def _definition_for_env_var(env_var: str) -> MarketplaceFlagDefinition:
    for definition in MARKETPLACE_FLAG_DEFINITIONS:
        if definition.env_var == env_var:
            return definition
    raise KeyError(env_var)


def _resolved_flag(definition: MarketplaceFlagDefinition) -> tuple[bool, str, str | None, str | None]:
    env_value = os.getenv(definition.env_var)
    stored_value = _runtime_raw_values.get(definition.key)
    if env_value is not None:
        return _parse_bool(env_value, definition.default), "environment", env_value, stored_value
    if definition.key in _runtime_values:
        return _runtime_values[definition.key], "system_config", None, stored_value
    return definition.default, "default", None, stored_value


def _raw_flag(env_var: str, default: bool = True) -> bool:
    try:
        definition = _definition_for_env_var(env_var)
    except KeyError:
        return _parse_bool(os.getenv(env_var), default)
    value, _, _, _ = _resolved_flag(definition)
    return value


def load_marketplace_config_values(values: Mapping[str, Any] | None) -> None:
    """Load persisted marketplace values into the sync runtime cache."""
    global _runtime_values, _runtime_raw_values, _runtime_loaded

    next_raw: dict[str, str] = {}
    next_values: dict[str, bool] = {}
    for key, value in (values or {}).items():
        if key not in _FLAG_BY_KEY or value is None:
            continue
        raw = str(value).strip()
        definition = _FLAG_BY_KEY[key]
        next_raw[key] = raw
        next_values[key] = _parse_bool(raw, definition.default)

    _runtime_raw_values = next_raw
    _runtime_values = next_values
    _runtime_loaded = True


def update_marketplace_config_values(values: Mapping[str, Any]) -> None:
    """Patch persisted values into the sync runtime cache after an admin save."""
    merged_raw = dict(_runtime_raw_values)
    for key, value in values.items():
        if key not in _FLAG_BY_KEY:
            continue
        merged_raw[key] = _bool_to_text(_strict_bool(value))
    load_marketplace_config_values(merged_raw)


def reset_marketplace_config_values() -> None:
    """Clear runtime values. Intended for tests."""
    global _runtime_values, _runtime_raw_values, _runtime_loaded
    _runtime_values = {}
    _runtime_raw_values = {}
    _runtime_loaded = False


def normalize_marketplace_config_updates(payload: Mapping[str, Any]) -> dict[str, bool]:
    raw_flags = payload.get("flags", payload)
    if not isinstance(raw_flags, Mapping):
        raise MarketplaceConfigError("Marketplace config payload must include a flags object.", "flags_object_required")

    unknown = sorted(str(key) for key in raw_flags if str(key) not in _FLAG_BY_KEY)
    if unknown:
        raise MarketplaceConfigError("Unknown marketplace flag: " + ", ".join(unknown), "unknown_flag")

    updates: dict[str, bool] = {}
    for definition in MARKETPLACE_FLAG_DEFINITIONS:
        if definition.key in raw_flags:
            updates[definition.key] = _strict_bool(raw_flags[definition.key])

    if not updates:
        raise MarketplaceConfigError("No marketplace flags provided.", "no_flags")

    return updates


def marketplace_config_has_env_override(key: str) -> bool:
    definition = _FLAG_BY_KEY[key]
    return os.getenv(definition.env_var) is not None


def get_marketplace_config_state() -> dict[str, Any]:
    master_definition = _FLAG_BY_KEY["marketplace_enabled"]
    master_desired, master_source, master_env_value, master_stored_value = _resolved_flag(master_definition)
    master_state = MarketplaceFlagState(
        key=master_definition.key,
        env_var=master_definition.env_var,
        label=master_definition.label,
        description=master_definition.description,
        default=master_definition.default,
        desired=master_desired,
        effective=master_desired,
        source=master_source,
        env_override=master_env_value is not None,
        env_value=master_env_value,
        stored_value=master_stored_value,
    )

    states = [master_state]
    for definition in MARKETPLACE_FLAG_DEFINITIONS[1:]:
        desired, source, env_value, stored_value = _resolved_flag(definition)
        states.append(
            MarketplaceFlagState(
                key=definition.key,
                env_var=definition.env_var,
                label=definition.label,
                description=definition.description,
                default=definition.default,
                desired=desired,
                effective=master_desired and desired,
                source=source,
                env_override=env_value is not None,
                env_value=env_value,
                stored_value=stored_value,
            )
        )

    effective_subflags = [state.effective for state in states[1:]]
    if not master_state.effective:
        status = "disabled"
        status_label = "Disabled"
        status_class = "danger"
    elif all(effective_subflags):
        status = "enabled"
        status_label = "Enabled"
        status_class = "success"
    else:
        status = "partial"
        status_label = "Partial"
        status_class = "warning"

    return {
        "status": status,
        "status_label": status_label,
        "status_class": status_class,
        "runtime_loaded": _runtime_loaded,
        "has_env_overrides": any(state.env_override for state in states),
        "flags": [asdict(state) for state in states],
    }


def marketplace_config_value_to_text(value: bool) -> str:
    return _bool_to_text(value)


def marketplace_enabled() -> bool:
    return _raw_flag("MARKETPLACE_ENABLED", True)


def _effective_subflag(name: str, default: bool = True) -> bool:
    return marketplace_enabled() and _raw_flag(name, default)


def marketplace_public_landings_enabled() -> bool:
    return _effective_subflag("MARKETPLACE_PUBLIC_LANDINGS_ENABLED")


def marketplace_checkout_enabled() -> bool:
    return _effective_subflag("MARKETPLACE_CHECKOUT_ENABLED")


def marketplace_storefronts_enabled() -> bool:
    return _effective_subflag("MARKETPLACE_STOREFRONTS_ENABLED")


def marketplace_discovery_enabled() -> bool:
    return _effective_subflag("MARKETPLACE_DISCOVERY_ENABLED")


def marketplace_creator_tools_enabled() -> bool:
    return _effective_subflag("MARKETPLACE_CREATOR_TOOLS_ENABLED")


def get_marketplace_flags() -> MarketplaceFlags:
    return MarketplaceFlags(
        enabled=marketplace_enabled(),
        public_landings_enabled=marketplace_public_landings_enabled(),
        checkout_enabled=marketplace_checkout_enabled(),
        storefronts_enabled=marketplace_storefronts_enabled(),
        discovery_enabled=marketplace_discovery_enabled(),
        creator_tools_enabled=marketplace_creator_tools_enabled(),
    )


def require_public_landings_enabled(translator=None) -> None:
    if not marketplace_public_landings_enabled():
        raise HTTPException(status_code=404, detail=translator.t("marketplace_admin.response.not_found") if translator else "Not found")


def require_discovery_enabled(translator=None) -> None:
    if not marketplace_discovery_enabled():
        raise HTTPException(status_code=404, detail=translator.t("marketplace_admin.response.not_found") if translator else "Not found")


def require_storefronts_enabled(translator=None) -> None:
    if not marketplace_storefronts_enabled():
        raise HTTPException(status_code=404, detail=translator.t("marketplace_admin.response.not_found") if translator else "Not found")


def require_checkout_enabled(translator=None) -> None:
    if not marketplace_checkout_enabled():
        detail = translator.t("marketplace.checkout.not_found") if translator else "Not found"
        raise HTTPException(status_code=404, detail=detail)


def require_creator_tools_enabled(translator=None) -> None:
    if not marketplace_creator_tools_enabled():
        raise HTTPException(status_code=404, detail=translator.t("marketplace_admin.response.not_found") if translator else "Not found")
