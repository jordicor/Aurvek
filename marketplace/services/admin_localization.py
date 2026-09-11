"""Request-bound human presentation of marketplace controls; flag values stay intact."""

from marketplace.config import get_marketplace_config_state


FLAG_KEYS = {
    "marketplace_enabled": "marketplace",
    "marketplace_public_landings_enabled": "public_landings",
    "marketplace_checkout_enabled": "checkout",
    "marketplace_storefronts_enabled": "storefronts",
    "marketplace_discovery_enabled": "discovery",
    "marketplace_creator_tools_enabled": "creator_tools",
}
CONFIG_ERRORS = {
    "invalid_boolean": "marketplace_admin.config.invalid_boolean",
    "flags_object_required": "marketplace_admin.config.flags_object_required",
    "unknown_flag": "marketplace_admin.config.unknown_flag",
    "no_flags": "marketplace_admin.config.no_flags",
}


def localized_config(translator):
    state = get_marketplace_config_state()
    for flag in state["flags"]:
        prefix = "marketplace_admin.flags." + FLAG_KEYS[flag["key"]]
        flag["label"] = translator.t(prefix + ".label")
        flag["description"] = translator.t(prefix + ".description")
        flag["source_label"] = translator.t("marketplace_admin.source." + flag["source"])
    state["status_label"] = translator.t("marketplace_admin.ui." + state["status"])
    return state
