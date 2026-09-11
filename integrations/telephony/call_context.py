"""Resolve the current destination without changing a call's original attribution."""

from collections.abc import Mapping
from typing import Any


def active_call(call: Mapping[str, Any]) -> dict[str, Any]:
    """Return the runtime view; the supplied transport/history snapshot is untouched."""
    result = dict(call)
    if result.get("active_conversation_id") is not None:
        result["conversation_id"] = int(result["active_conversation_id"])
    if result.get("active_config_snapshot_json") is not None:
        result["config_snapshot_json"] = result["active_config_snapshot_json"]
    return result
