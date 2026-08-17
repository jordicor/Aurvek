import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import aiohttp
import aiosqlite

from common import claude_key, gemini_key, minimax_key, moonshot_key, openai_key, openrouter_key, xai_key

logger = logging.getLogger(__name__)


PROVIDER_KEY_BY_MACHINE = {
    "GPT": "openai",
    "O1": "openai",
    "Claude": "anthropic",
    "Gemini": "google",
    "xAI": "xai",
    "OpenRouter": "openrouter",
    "MiniMax": "minimax",
    "Kimi": "kimi",
    "GranSabio": "gransabio",
}

MACHINE_BY_PROVIDER_KEY = {
    "openai": "GPT",
    "anthropic": "Claude",
    "google": "Gemini",
    "xai": "xAI",
    "openrouter": "OpenRouter",
    "minimax": "MiniMax",
    "kimi": "Kimi",
    "moonshot": "Kimi",
    "ollama": "Ollama",
    "gransabio": "GranSabio",
    "manual": "Manual",
}

PROVIDER_KEY_ALIASES = {
    "moonshot": "kimi",
}

FULL_SYNC_PROVIDERS = {"openrouter", "xai", "google", "minimax", "kimi"}
DISCOVERY_ASSISTED_PROVIDERS = {"openai", "anthropic"}
SYNC_PROVIDERS = FULL_SYNC_PROVIDERS | DISCOVERY_ASSISTED_PROVIDERS
SYNC_PROVIDER_ORDER = ("openrouter", "openai", "anthropic", "google", "xai", "minimax", "kimi")

OPENROUTER_PROVIDER_PREFIX = {
    "openai": "openai",
    "anthropic": "anthropic",
}

_VERSION_DOT_BETWEEN_DIGITS = re.compile(r"(\d)\.(\d)")
_VERSION_DASH_BETWEEN_DIGITS = re.compile(r"(\d)-(\d)")
_ANTHROPIC_DATE_SUFFIX = re.compile(r"-\d{8}$")

CATALOG_COLUMNS = """
    id, machine, model, input_token_cost, output_token_cost, vision,
    provider_key, provider_model_id, display_name, description,
    context_window_tokens, max_input_tokens, max_output_tokens,
    enabled, sync_source, sync_status, last_synced_at,
    raw_metadata_json, capabilities_json, manual_overrides_json
"""

_sync_lock = asyncio.Lock()


class LlmCatalogError(RuntimeError):
    """Raised for catalog sync errors that should be shown to admins."""


def normalize_provider_key(machine_or_provider: str | None) -> str:
    value = (machine_or_provider or "manual").strip()
    if value in PROVIDER_KEY_BY_MACHINE:
        return PROVIDER_KEY_BY_MACHINE[value]
    lowered = value.lower()
    lowered = PROVIDER_KEY_ALIASES.get(lowered, lowered)
    if lowered in MACHINE_BY_PROVIDER_KEY:
        return lowered
    return lowered or "manual"


def machine_for_provider(provider_key: str | None, fallback: str | None = None) -> str:
    key = normalize_provider_key(provider_key)
    return MACHINE_BY_PROVIDER_KEY.get(key) or fallback or (provider_key or "Manual")


def is_sync_managed(row: dict[str, Any]) -> bool:
    source = (row.get("sync_source") or "").lower()
    return source not in {"", "manual"}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float:
    try:
        if value in (None, ""):
            return 0.0
        parsed = float(value)
        return parsed if parsed > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _price_per_million(value: Any) -> float:
    price = _as_float(value)
    return price * 1_000_000 if price > 0 else 0.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    data = dict(row)
    data["vision"] = bool(data.get("vision"))
    data["enabled"] = bool(data.get("enabled", 1))
    data["input_token_cost"] = float(data.get("input_token_cost") or 0.0)
    data["output_token_cost"] = float(data.get("output_token_cost") or 0.0)
    data["capabilities"] = _json_loads(data.get("capabilities_json"), {})
    data["raw_metadata"] = _json_loads(data.get("raw_metadata_json"), {})
    data["manual_overrides"] = _json_loads(data.get("manual_overrides_json"), {})
    data["metadata_source"] = (
        data["capabilities"].get("metadata_source")
        or data.get("sync_source")
        or "manual"
    )
    data["sync_managed"] = is_sync_managed(data)
    data["needs_review"] = bool(
        data["sync_managed"]
        and (
            data.get("sync_status") == "needs_review"
            or not _as_int(data.get("max_output_tokens"))
        )
    )
    return data


def build_capabilities(vision: bool, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    capabilities = dict(extra or {})
    capabilities["vision"] = bool(vision)
    return capabilities


def build_manual_insert_metadata(machine: str, model: str, vision: bool) -> dict[str, Any]:
    provider_key = normalize_provider_key(machine)
    return {
        "provider_key": provider_key,
        "provider_model_id": model,
        "display_name": model,
        "enabled": 1,
        "sync_source": "manual",
        "sync_status": "manual",
        "raw_metadata_json": "{}",
        "capabilities_json": _json_dumps(build_capabilities(vision)),
        "manual_overrides_json": "{}",
    }


async def get_catalog(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cursor = await conn.execute(f"SELECT {CATALOG_COLUMNS} FROM LLM ORDER BY COALESCE(display_name, model)")
    return [row_to_dict(row) for row in await cursor.fetchall()]


async def get_provider_catalog_view(
    conn: aiosqlite.Connection,
    provider_key: str,
    remote_models: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return remote models enriched with matching local catalog state."""
    provider = normalize_provider_key(provider_key)
    if provider not in SYNC_PROVIDERS:
        raise LlmCatalogError(f"Provider '{provider}' does not support API sync")

    normalized_remote = [
        _normalize_remote_model(provider, item)
        for item in (remote_models if remote_models is not None else await fetch_remote_models(provider))
    ]

    cursor = await conn.execute(
        f"""
        SELECT {CATALOG_COLUMNS}
        FROM LLM
        WHERE provider_key = ? OR machine = ?
        ORDER BY COALESCE(display_name, model)
        """,
        (provider, machine_for_provider(provider)),
    )
    local_rows = [row_to_dict(row) for row in await cursor.fetchall()]
    local_by_model_id = {
        row.get("provider_model_id") or row.get("model"): row
        for row in local_rows
    }

    models: list[dict[str, Any]] = []
    seen_model_ids: set[str] = set()
    for remote in normalized_remote:
        model_id = remote.get("provider_model_id") or remote.get("model")
        if not model_id:
            continue
        seen_model_ids.add(model_id)
        local = local_by_model_id.get(model_id)
        item = _remote_for_admin(remote)
        if local:
            item.update({
                "local_id": local["id"],
                "enabled": local["enabled"],
                "local_sync_status": local.get("sync_status"),
                "sync_status": local.get("sync_status") or item.get("sync_status"),
                "last_synced_at": local.get("last_synced_at"),
                "local_only": False,
                "remote_available": True,
                "catalog_context_window_tokens": local.get("context_window_tokens"),
                "catalog_max_output_tokens": local.get("max_output_tokens"),
                "catalog_input_token_cost": local.get("input_token_cost"),
                "catalog_output_token_cost": local.get("output_token_cost"),
            })
            item["needs_review"] = bool(local.get("needs_review") or _remote_needs_review(item))
        else:
            item.update({
                "local_id": None,
                "enabled": False,
                "local_sync_status": "new",
                "last_synced_at": None,
                "local_only": False,
                "remote_available": True,
            })
            item["needs_review"] = _remote_needs_review(item)
        models.append(item)

    for local in local_rows:
        model_id = local.get("provider_model_id") or local.get("model")
        if not model_id or model_id in seen_model_ids:
            continue
        models.append({
            "provider_key": provider,
            "machine": local.get("machine") or machine_for_provider(provider),
            "provider_model_id": model_id,
            "model": local.get("model") or model_id,
            "display_name": local.get("display_name") or local.get("model") or model_id,
            "description": local.get("description"),
            "context_window_tokens": local.get("context_window_tokens"),
            "max_input_tokens": local.get("max_input_tokens"),
            "max_output_tokens": local.get("max_output_tokens"),
            "input_token_cost": local.get("input_token_cost"),
            "output_token_cost": local.get("output_token_cost"),
            "vision": bool(local.get("vision")),
            "capabilities": local.get("capabilities") or {},
            "raw_metadata": local.get("raw_metadata") or {},
            "sync_status": local.get("sync_status") or "stale",
            "metadata_source": local.get("metadata_source") or local.get("sync_source") or "manual",
            "needs_review": bool(local.get("needs_review")),
            "local_id": local["id"],
            "enabled": local["enabled"],
            "local_sync_status": local.get("sync_status"),
            "last_synced_at": local.get("last_synced_at"),
            "local_only": True,
            "remote_available": False,
        })

    models.sort(key=lambda item: (
        1 if item.get("local_only") else 0,
        (item.get("display_name") or item.get("provider_model_id") or "").lower(),
    ))
    return {
        "provider": provider,
        "remote_count": len(normalized_remote),
        "local_count": len(local_rows),
        "models": models,
    }


async def get_selector_llms(
    conn: aiosqlite.Connection,
    preserve_ids: list[int] | set[int] | tuple[int, ...] | None = None,
    include_gransabio: bool = False,
    gptsub_models: list[str] | set[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Build the enabled-LLM selector list.

    ``gptsub_models`` is the current user's saved live Codex catalog. Omitting it is
    fail-safe HIDE; a boolean "linked" flag is insufficient because shared LLM rows
    are the union across accounts. Preserved GPTSub ids are still hidden unless that
    model belongs to this user's catalog. Preserve behavior for every other machine
    is unchanged.
    """
    preserved_set = set()
    for value in preserve_ids or []:
        try:
            if value is not None and str(value).strip():
                preserved_set.add(int(value))
        except (TypeError, ValueError):
            continue
    preserved = sorted(preserved_set)
    params: list[Any] = []
    conditions = []
    if not include_gransabio:
        conditions.append("machine != 'GranSabio'")

    enabled_clause = "COALESCE(enabled, 1) = 1"
    if preserved:
        placeholders = ",".join("?" for _ in preserved)
        enabled_clause = f"({enabled_clause} OR id IN ({placeholders}))"
        params.extend(preserved)
    conditions.append(enabled_clause)

    # GPTSub is personal even though its backing LLM rows are shared. Never use a
    # union-wide visible boolean and never preserve an unauthorized GPTSub id.
    allowed_gptsub_models = sorted(
        {
            model
            for model in gptsub_models or []
            if isinstance(model, str) and model and len(model) <= 128
        }
    )
    gptsub_terms = ["machine != 'GPTSub'"]
    if allowed_gptsub_models:
        placeholders = ",".join("?" for _ in allowed_gptsub_models)
        gptsub_terms.append(f"model IN ({placeholders})")
        params.extend(allowed_gptsub_models)
    conditions.append("(" + " OR ".join(gptsub_terms) + ")")

    where_sql = " AND ".join(conditions)
    cursor = await conn.execute(
        f"""
        SELECT id, machine, model, vision, enabled, display_name
        FROM LLM
        WHERE {where_sql}
        ORDER BY machine, COALESCE(display_name, model)
        """,
        params,
    )
    return [dict(row) for row in await cursor.fetchall()]


async def fetch_remote_models(provider_key: str) -> list[dict[str, Any]]:
    provider = normalize_provider_key(provider_key)
    if provider == "openrouter":
        return await _fetch_openrouter_models()
    if provider == "google":
        return await _fetch_google_models()
    if provider == "xai":
        return await _fetch_xai_models()
    if provider == "minimax":
        return await _fetch_minimax_models()
    if provider == "kimi":
        return await _fetch_kimi_models()
    if provider == "openai":
        return await _fetch_openai_models()
    if provider == "anthropic":
        return await _fetch_anthropic_models()
    raise LlmCatalogError(f"Provider '{provider_key}' does not support API sync")


async def sync_provider(
    conn: aiosqlite.Connection,
    provider_key: str,
    selected_model_ids: list[str] | None = None,
    remote_models: list[dict[str, Any]] | None = None,
    disabled_model_ids: list[str] | None = None,
) -> dict[str, Any]:
    provider = normalize_provider_key(provider_key)
    if provider not in SYNC_PROVIDERS:
        raise LlmCatalogError(f"Provider '{provider}' does not support API sync")

    async with _sync_lock:
        normalized_remote = [
            _normalize_remote_model(provider, item)
            for item in (remote_models if remote_models is not None else await fetch_remote_models(provider))
        ]
        remote_by_id = {
            model["provider_model_id"]: model
            for model in normalized_remote
            if model.get("provider_model_id")
        }

        selective_sync = selected_model_ids is not None
        selected_set = set(selected_model_ids or [])
        disabled_set = set(disabled_model_ids or [])
        if not selective_sync:
            selected_set = set(remote_by_id.keys())

        cursor = await conn.execute(
            """
            SELECT id, provider_model_id, model, manual_overrides_json
            FROM LLM
            WHERE provider_key = ? OR machine = ?
            """,
            (provider, machine_for_provider(provider)),
        )
        existing_rows = await cursor.fetchall()
        existing_by_id = {
            (row["provider_model_id"] or row["model"]): row
            for row in existing_rows
        }

        now = _utc_now()
        added = 0
        updated = 0
        disabled = 0
        stale = 0
        skipped = 0

        for model_id in selected_set:
            remote = remote_by_id.get(model_id)
            if not remote:
                skipped += 1
                continue

            existing = existing_by_id.get(model_id)
            if existing:
                overrides = _json_loads(existing["manual_overrides_json"], {})
                updates = _build_update_payload(
                    remote,
                    overrides,
                    now,
                    enabled=1 if selective_sync else None,
                )
                assignments = ", ".join(f"{key} = ?" for key in updates)
                await conn.execute(
                    f"UPDATE LLM SET {assignments} WHERE id = ?",
                    [*updates.values(), existing["id"]],
                )
                updated += 1
            else:
                payload = _build_insert_payload(remote, now, enabled=1 if selective_sync else 0)
                columns = ", ".join(payload.keys())
                placeholders = ", ".join("?" for _ in payload)
                await conn.execute(
                    f"INSERT INTO LLM ({columns}) VALUES ({placeholders})",
                    list(payload.values()),
                )
                added += 1

        if disabled_set:
            for model_id in disabled_set:
                row = existing_by_id.get(model_id)
                if not row:
                    continue
                await conn.execute(
                    """
                    UPDATE LLM
                    SET enabled = 0,
                        sync_status = CASE WHEN sync_status = 'stale' THEN 'stale' ELSE 'synced' END,
                        last_synced_at = ?
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                disabled += 1
        elif not selective_sync:
            missing_remote = [
                row for model_id, row in existing_by_id.items()
                if model_id not in remote_by_id
            ]
            for row in missing_remote:
                await conn.execute(
                    "UPDATE LLM SET sync_status = 'stale', last_synced_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                stale += 1

        return {
            "provider": provider,
            "added": added,
            "updated": updated,
            "disabled": disabled,
            "stale": stale,
            "skipped": skipped,
            "remote_count": len(normalized_remote),
            "selected_count": len(selected_set),
        }


async def sync_all_providers(conn: aiosqlite.Connection) -> dict[str, Any]:
    results = {}
    for provider in SYNC_PROVIDER_ORDER:
        try:
            remote_models = await fetch_remote_models(provider)
            results[provider] = await sync_provider(conn, provider, remote_models=remote_models)
            await conn.commit()
        except Exception as exc:
            try:
                await conn.rollback()
            except Exception:
                pass
            logger.exception("[llm_catalog] sync_all_providers failed for provider '%s'", provider)
            results[provider] = {"error": str(exc)}
    return results


async def set_model_enabled(conn: aiosqlite.Connection, llm_id: int, enabled: bool) -> dict[str, Any]:
    cursor = await conn.execute(
        "SELECT id, machine, model FROM LLM WHERE id = ?",
        (llm_id,),
    )
    row = await cursor.fetchone()
    if not row:
        raise LlmCatalogError("LLM not found")
    if str(row["machine"] or "").strip().casefold() == "gptsub":
        raise LlmCatalogError(
            "GPTSub model state is managed by linked-account catalog synchronization"
        )
    if row["machine"] == "GranSabio" and row["model"] == "gransabio-pipeline":
        raise LlmCatalogError("System LLM cannot be disabled")

    await conn.execute("UPDATE LLM SET enabled = ? WHERE id = ?", (1 if enabled else 0, llm_id))
    return {"id": llm_id, "enabled": enabled}


def merge_manual_overrides(existing_json: str | None, fields: list[str]) -> str:
    overrides = _json_loads(existing_json, {})
    for field in fields:
        overrides[field] = True
    return _json_dumps(overrides)


async def _request_json(
    url: str,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            headers=headers or {},
            params=params,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            if response.status >= 400:
                text = await response.text()
                raise LlmCatalogError(f"API error {response.status}: {text[:500]}")
            return await response.json()


async def _fetch_openrouter_models() -> list[dict[str, Any]]:
    if not openrouter_key:
        raise LlmCatalogError("OpenRouter API key not configured")
    data = await _request_json(
        "https://openrouter.ai/api/v1/models",
        headers={"Authorization": f"Bearer {openrouter_key}"},
    )
    return [_normalize_openrouter_model(item) for item in data.get("data", [])]


async def _fetch_google_models() -> list[dict[str, Any]]:
    if not gemini_key:
        raise LlmCatalogError("Gemini API key not configured")
    data = await _request_json(
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": gemini_key},
    )
    return [_normalize_google_model(item) for item in data.get("models", [])]


async def _fetch_xai_models() -> list[dict[str, Any]]:
    if not xai_key:
        raise LlmCatalogError("xAI API key not configured")
    data = await _request_json(
        "https://api.x.ai/v1/language-models",
        headers={"Authorization": f"Bearer {xai_key}"},
    )
    models = data.get("models") or data.get("data") or []
    return [_normalize_xai_model(item) for item in models]


async def _fetch_minimax_models() -> list[dict[str, Any]]:
    if not minimax_key:
        raise LlmCatalogError("MiniMax API key not configured")
    data = await _request_json(
        "https://api.minimax.io/v1/models",
        headers={"Authorization": f"Bearer {minimax_key}"},
    )
    models = data.get("data") or data.get("models") or []
    return [_normalize_minimax_model(item) for item in models]


async def _fetch_kimi_models() -> list[dict[str, Any]]:
    if not moonshot_key:
        raise LlmCatalogError("Kimi API key not configured")
    data = await _request_json(
        "https://api.moonshot.ai/v1/models",
        headers={"Authorization": f"Bearer {moonshot_key}"},
    )
    models = data.get("data") or data.get("models") or []
    return [_normalize_kimi_model(item) for item in models]


async def _fetch_openai_models() -> list[dict[str, Any]]:
    if not openai_key:
        raise LlmCatalogError("OpenAI API key not configured")
    data = await _request_json(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {openai_key}"},
    )
    models = [_normalize_discovery_model("openai", item) for item in data.get("data", [])]
    return await _enrich_discovery_models("openai", models)


async def _fetch_anthropic_models() -> list[dict[str, Any]]:
    if not claude_key:
        raise LlmCatalogError("Anthropic API key not configured")
    data = await _request_json(
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": claude_key, "anthropic-version": "2023-06-01"},
    )
    models = [_normalize_anthropic_model(item) for item in data.get("data", [])]
    return await _enrich_discovery_models("anthropic", models)


def _normalize_openrouter_model(item: dict[str, Any]) -> dict[str, Any]:
    model_id = item.get("id") or item.get("model") or ""
    pricing = item.get("pricing") or {}
    architecture = item.get("architecture") or {}
    input_modalities = architecture.get("input_modalities") or []
    output_modalities = architecture.get("output_modalities") or []
    top_provider = item.get("top_provider") or {}
    vision = "image" in input_modalities
    return {
        "provider_key": "openrouter",
        "machine": "OpenRouter",
        "provider_model_id": model_id,
        "model": model_id,
        "display_name": item.get("name") or model_id,
        "description": item.get("description"),
        "context_window_tokens": _as_int(item.get("context_length")),
        "max_input_tokens": _as_int(item.get("context_length")),
        "max_output_tokens": _as_int(top_provider.get("max_completion_tokens") or item.get("max_completion_tokens")),
        "input_token_cost": _price_per_million(pricing.get("prompt")),
        "output_token_cost": _price_per_million(pricing.get("completion")),
        "vision": vision,
        "capabilities": build_capabilities(
            vision,
            {
                "input_modalities": input_modalities,
                "output_modalities": output_modalities,
                "metadata_source": "openrouter",
            },
        ),
        "raw_metadata": item,
        "sync_status": "synced",
        "metadata_source": "openrouter",
    }


def _normalize_google_model(item: dict[str, Any]) -> dict[str, Any]:
    raw_name = item.get("name") or ""
    model_id = raw_name.removeprefix("models/") or item.get("model") or raw_name
    supported_methods = item.get("supportedGenerationMethods") or []
    vision = "generateContent" in supported_methods
    input_limit = _as_int(item.get("inputTokenLimit"))
    output_limit = _as_int(item.get("outputTokenLimit"))
    return {
        "provider_key": "google",
        "machine": "Gemini",
        "provider_model_id": model_id,
        "model": model_id,
        "display_name": item.get("displayName") or model_id,
        "description": item.get("description"),
        "context_window_tokens": input_limit,
        "max_input_tokens": input_limit,
        "max_output_tokens": output_limit,
        "input_token_cost": 0.0,
        "output_token_cost": 0.0,
        "vision": vision,
        "capabilities": build_capabilities(
            vision,
            {
                "supported_generation_methods": supported_methods,
                "metadata_source": "provider_api",
            },
        ),
        "raw_metadata": item,
        "metadata_source": "provider_api",
        "sync_status": "needs_review",
    }


def _normalize_xai_model(item: dict[str, Any]) -> dict[str, Any]:
    model_id = item.get("id") or item.get("model") or item.get("name") or ""
    input_cost = item.get("input_token_cost") or item.get("input_price") or item.get("prompt_price")
    output_cost = item.get("output_token_cost") or item.get("output_price") or item.get("completion_price")
    context_window = _as_int(item.get("context_window") or item.get("context_length") or item.get("input_tokens"))
    max_output = _as_int(item.get("output_tokens") or item.get("max_output_tokens"))
    return {
        "provider_key": "xai",
        "machine": "xAI",
        "provider_model_id": model_id,
        "model": model_id,
        "display_name": item.get("display_name") or item.get("name") or model_id,
        "description": item.get("description"),
        "context_window_tokens": context_window,
        "max_input_tokens": context_window,
        "max_output_tokens": max_output,
        "input_token_cost": _as_float(input_cost),
        "output_token_cost": _as_float(output_cost),
        "vision": bool(item.get("vision") or item.get("supports_vision")),
        "capabilities": build_capabilities(
            bool(item.get("vision") or item.get("supports_vision")),
            {"metadata_source": "provider_api"},
        ),
        "raw_metadata": item,
        "metadata_source": "provider_api",
        "sync_status": "synced" if context_window or max_output or input_cost or output_cost else "needs_review",
    }


def _pricing_cost_per_million(item: dict[str, Any], *names: str) -> float:
    pricing = item.get("pricing") or {}
    for name in names:
        direct = _as_float(item.get(name))
        if direct:
            return direct
    for name in names:
        nested = pricing.get(name)
        if nested is not None:
            if name in {"prompt", "completion", "input", "output"}:
                nested_price = _price_per_million(nested)
            else:
                nested_price = _as_float(nested)
            if nested_price:
                return nested_price
    return 0.0


def _normalize_minimax_model(item: dict[str, Any]) -> dict[str, Any]:
    model_id = item.get("id") or item.get("model") or item.get("name") or ""
    model_lower = model_id.lower()
    is_m3 = "minimax-m3" in model_lower or model_lower == "m3"
    context_window = _as_int(
        item.get("context_window")
        or item.get("context_length")
        or item.get("max_context_tokens")
        or item.get("input_tokens")
    ) or (1_000_000 if is_m3 else None)
    max_output = _as_int(
        item.get("output_tokens")
        or item.get("max_output_tokens")
        or item.get("max_completion_tokens")
    ) or (131_072 if is_m3 else None)
    input_cost = _pricing_cost_per_million(item, "input_token_cost", "input_price", "prompt", "prompt_price")
    output_cost = _pricing_cost_per_million(item, "output_token_cost", "output_price", "completion", "completion_price")
    if is_m3 and not input_cost:
        input_cost = 0.30
    if is_m3 and not output_cost:
        output_cost = 1.20
    vision = bool(item.get("vision") or item.get("supports_vision") or is_m3)
    return {
        "provider_key": "minimax",
        "machine": "MiniMax",
        "provider_model_id": model_id,
        "model": model_id,
        "display_name": item.get("display_name") or item.get("displayName") or item.get("name") or model_id,
        "description": item.get("description"),
        "context_window_tokens": context_window,
        "max_input_tokens": context_window,
        "max_output_tokens": max_output,
        "input_token_cost": input_cost,
        "output_token_cost": output_cost,
        "vision": vision,
        "capabilities": build_capabilities(
            vision,
            {
                "thinking": is_m3,
                "reasoning_split": is_m3,
                "metadata_source": "provider_api",
            },
        ),
        "raw_metadata": item,
        "metadata_source": "provider_api",
        "sync_status": "synced" if context_window and max_output and input_cost and output_cost else "needs_review",
    }


def _normalize_kimi_model(item: dict[str, Any]) -> dict[str, Any]:
    model_id = item.get("id") or item.get("model") or item.get("name") or ""
    model_lower = model_id.lower().replace("_", "-")
    is_k2 = model_lower.startswith("kimi-k2") or "kimi-k2" in model_lower
    is_k27 = "kimi-k2.7" in model_lower or "kimi-k2-7" in model_lower
    context_window = _as_int(
        item.get("context_window")
        or item.get("context_length")
        or item.get("max_context_tokens")
        or item.get("input_tokens")
    ) or (256_000 if is_k2 else None)
    max_output = _as_int(
        item.get("output_tokens")
        or item.get("max_output_tokens")
        or item.get("max_completion_tokens")
    ) or (32_768 if is_k2 else None)
    input_cost = _pricing_cost_per_million(item, "input_token_cost", "input_price", "prompt", "prompt_price")
    output_cost = _pricing_cost_per_million(item, "output_token_cost", "output_price", "completion", "completion_price")
    if is_k27 and not input_cost:
        input_cost = 0.95
    if is_k27 and not output_cost:
        output_cost = 4.00
    vision = bool(item.get("vision") or item.get("supports_vision") or is_k27)
    return {
        "provider_key": "kimi",
        "machine": "Kimi",
        "provider_model_id": model_id,
        "model": model_id,
        "display_name": item.get("display_name") or item.get("displayName") or item.get("name") or model_id,
        "description": item.get("description"),
        "context_window_tokens": context_window,
        "max_input_tokens": context_window,
        "max_output_tokens": max_output,
        "input_token_cost": input_cost,
        "output_token_cost": output_cost,
        "vision": vision,
        "capabilities": build_capabilities(
            vision,
            {
                "thinking": is_k2,
                "preserve_thinking": is_k27,
                "fixed_temperature": is_k2,
                "metadata_source": "provider_api",
            },
        ),
        "raw_metadata": item,
        "metadata_source": "provider_api",
        "sync_status": "synced" if context_window and max_output and input_cost and output_cost else "needs_review",
    }


def _normalize_anthropic_model(item: dict[str, Any]) -> dict[str, Any]:
    model_id = item.get("id") or item.get("model") or ""
    capabilities = item.get("capabilities") or {}
    image_input = capabilities.get("image_input")
    if isinstance(image_input, dict) and "supported" in image_input:
        vision: bool | None = bool(image_input["supported"])
    else:
        vision = None

    max_output = _as_int(item.get("max_tokens") or item.get("max_output_tokens"))
    max_input = _as_int(item.get("max_input_tokens"))
    return {
        "provider_key": "anthropic",
        "machine": "Claude",
        "provider_model_id": model_id,
        "model": model_id,
        "display_name": item.get("display_name") or model_id,
        "description": item.get("description"),
        "context_window_tokens": max_input,
        "max_input_tokens": max_input,
        "max_output_tokens": max_output,
        "input_token_cost": 0.0,
        "output_token_cost": 0.0,
        "vision": vision,
        "capabilities": build_capabilities(bool(vision), {"metadata_source": "provider_api"}),
        "raw_metadata": item,
        "metadata_source": "provider_api",
        "sync_status": "needs_review",
    }


def _normalize_discovery_model(provider: str, item: dict[str, Any]) -> dict[str, Any]:
    model_id = item.get("id") or item.get("model") or item.get("name") or ""
    return {
        "provider_key": provider,
        "machine": machine_for_provider(provider),
        "provider_model_id": model_id,
        "model": model_id,
        "display_name": item.get("display_name") or item.get("displayName") or model_id,
        "description": item.get("description"),
        "context_window_tokens": None,
        "max_input_tokens": None,
        "max_output_tokens": None,
        "input_token_cost": 0.0,
        "output_token_cost": 0.0,
        "vision": False,
        "capabilities": build_capabilities(False, {"metadata_source": "provider_api"}),
        "raw_metadata": item,
        "metadata_source": "provider_api",
        "sync_status": "needs_review",
    }


def _normalize_remote_model(provider: str, item: dict[str, Any]) -> dict[str, Any]:
    if item.get("provider_key") and item.get("provider_model_id"):
        return item
    if provider == "openrouter":
        if "input_price" in item or "context_length" in item:
            model_id = item.get("id") or item.get("model") or ""
            return {
                "provider_key": "openrouter",
                "machine": "OpenRouter",
                "provider_model_id": model_id,
                "model": model_id,
                "display_name": item.get("name") or model_id,
                "description": item.get("description"),
                "context_window_tokens": _as_int(item.get("context_length")),
                "max_input_tokens": _as_int(item.get("context_length")),
                "max_output_tokens": _as_int(item.get("max_output_tokens")),
                "input_token_cost": _as_float(item.get("input_price")),
                "output_token_cost": _as_float(item.get("output_price")),
                "vision": bool(item.get("vision")),
                "capabilities": build_capabilities(bool(item.get("vision"))),
                "raw_metadata": item,
                "metadata_source": "openrouter",
                "sync_status": "synced",
            }
        return _normalize_openrouter_model(item)
    if provider == "google":
        return _normalize_google_model(item)
    if provider == "xai":
        return _normalize_xai_model(item)
    if provider == "minimax":
        return _normalize_minimax_model(item)
    if provider == "kimi":
        return _normalize_kimi_model(item)
    if provider in DISCOVERY_ASSISTED_PROVIDERS:
        return _normalize_discovery_model(provider, item)
    raise LlmCatalogError(f"Cannot normalize provider '{provider}'")


def _build_insert_payload(remote: dict[str, Any], now: str, enabled: int) -> dict[str, Any]:
    return {
        "machine": remote["machine"],
        "model": remote["model"],
        "input_token_cost": remote.get("input_token_cost", 0.0),
        "output_token_cost": remote.get("output_token_cost", 0.0),
        "vision": 1 if remote.get("vision") else 0,
        "provider_key": remote["provider_key"],
        "provider_model_id": remote["provider_model_id"],
        "display_name": remote.get("display_name") or remote["model"],
        "description": remote.get("description"),
        "context_window_tokens": remote.get("context_window_tokens"),
        "max_input_tokens": remote.get("max_input_tokens"),
        "max_output_tokens": remote.get("max_output_tokens"),
        "enabled": enabled,
        "sync_source": remote["provider_key"],
        "sync_status": remote.get("sync_status") or "synced",
        "last_synced_at": now,
        "raw_metadata_json": _json_dumps(remote.get("raw_metadata") or {}),
        "capabilities_json": _json_dumps(remote.get("capabilities") or build_capabilities(remote.get("vision"))),
        "manual_overrides_json": "{}",
    }


def _build_update_payload(
    remote: dict[str, Any],
    overrides: dict[str, Any],
    now: str,
    enabled: int | None,
) -> dict[str, Any]:
    payload = {
        "machine": remote["machine"],
        "model": remote["model"],
        "provider_key": remote["provider_key"],
        "provider_model_id": remote["provider_model_id"],
        "sync_source": remote["provider_key"],
        "sync_status": remote.get("sync_status") or "synced",
        "last_synced_at": now,
        "raw_metadata_json": _json_dumps(remote.get("raw_metadata") or {}),
        "manual_overrides_json": _json_dumps(overrides or {}),
    }
    if enabled is not None:
        payload["enabled"] = enabled

    provider_owned = {
        "display_name": remote.get("display_name") or remote["model"],
        "description": remote.get("description"),
        "context_window_tokens": remote.get("context_window_tokens"),
        "max_input_tokens": remote.get("max_input_tokens"),
        "max_output_tokens": remote.get("max_output_tokens"),
        "input_token_cost": remote.get("input_token_cost", 0.0),
        "output_token_cost": remote.get("output_token_cost", 0.0),
        "vision": 1 if remote.get("vision") else 0,
        "capabilities_json": _json_dumps(remote.get("capabilities") or build_capabilities(remote.get("vision"))),
    }
    for field, value in provider_owned.items():
        if field == "capabilities_json" and overrides.get("vision"):
            continue
        if not overrides.get(field):
            payload[field] = value
    return payload


def _remote_for_admin(remote: dict[str, Any]) -> dict[str, Any]:
    item = dict(remote)
    capabilities = item.get("capabilities") or {}
    item["metadata_source"] = (
        item.get("metadata_source")
        or capabilities.get("metadata_source")
        or item.get("provider_key")
        or "provider_api"
    )
    item["needs_review"] = _remote_needs_review(item)
    return item


def _remote_needs_review(remote: dict[str, Any]) -> bool:
    return bool(
        remote.get("sync_status") == "needs_review"
        or not _as_int(remote.get("max_output_tokens"))
    )


def _anthropic_match_variants(native_id: str) -> list[str]:
    """Return candidate OpenRouter suffixes for an Anthropic native model id."""
    if not native_id:
        return []

    seen: set[str] = set()
    ordered: list[str] = []

    def add(candidate: str) -> None:
        if candidate and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)

    add(native_id)
    add(_VERSION_DASH_BETWEEN_DIGITS.sub(r"\1.\2", native_id))
    add(_VERSION_DOT_BETWEEN_DIGITS.sub(r"\1-\2", native_id))

    undated = _ANTHROPIC_DATE_SUFFIX.sub("", native_id)
    if undated != native_id:
        add(undated)
        add(_VERSION_DASH_BETWEEN_DIGITS.sub(r"\1.\2", undated))
        add(_VERSION_DOT_BETWEEN_DIGITS.sub(r"\1-\2", undated))

    return ordered


async def _enrich_discovery_models(provider: str, models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enrich discovery-only provider rows with exact OpenRouter metadata matches."""
    prefix = OPENROUTER_PROVIDER_PREFIX.get(provider)
    if not prefix or not openrouter_key or not models:
        return models

    try:
        openrouter_models = await _fetch_openrouter_models()
    except Exception:
        logger.warning(
            "[llm_catalog] OpenRouter enrichment fetch failed for provider '%s'; skipping.",
            provider,
            exc_info=True,
        )
        return models

    openrouter_by_native_id: dict[str, dict[str, Any]] = {}
    prefix_with_sep = f"{prefix}/"
    for openrouter_model in openrouter_models:
        openrouter_id = openrouter_model.get("provider_model_id") or ""
        if openrouter_id.startswith(prefix_with_sep):
            native_id = openrouter_id[len(prefix_with_sep):]
            if native_id:
                openrouter_by_native_id[native_id] = openrouter_model

    enriched_models = []
    for model in models:
        native_id = model.get("provider_model_id") or ""
        if provider == "anthropic":
            candidates = _anthropic_match_variants(native_id)
        else:
            candidates = [native_id] if native_id else []
        enrichment = next(
            (openrouter_by_native_id[candidate] for candidate in candidates if candidate in openrouter_by_native_id),
            None,
        )
        if enrichment:
            enriched_models.append(_apply_openrouter_enrichment(model, enrichment))
        else:
            enriched_models.append(model)
    return enriched_models


def _apply_openrouter_enrichment(model: dict[str, Any], enrichment: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(model)
    is_anthropic = (model.get("provider_key") or "").lower() == "anthropic"
    caps_and_vision = ("context_window_tokens", "max_input_tokens", "max_output_tokens", "vision")
    pricing_and_desc = ("input_token_cost", "output_token_cost", "description")

    for field in pricing_and_desc:
        value = enrichment.get(field)
        if value not in (None, "", 0, 0.0):
            enriched[field] = value

    for field in caps_and_vision:
        native_value = model.get(field)
        or_value = enrichment.get(field)
        if field == "vision":
            has_native = isinstance(native_value, bool)
        else:
            has_native = native_value not in (None, "", 0, 0.0)
        if is_anthropic and has_native:
            continue
        if or_value not in (None, "", 0, 0.0) or field == "vision":
            enriched[field] = or_value

    capabilities = dict(enrichment.get("capabilities") or {})
    capabilities["metadata_source"] = "openrouter_enrichment"
    final_vision = bool(enriched.get("vision"))
    enriched["vision"] = final_vision
    capabilities["vision"] = final_vision

    enriched["capabilities"] = capabilities
    enriched["raw_metadata"] = {
        "provider": model.get("raw_metadata") or {},
        "openrouter": enrichment.get("raw_metadata") or {},
    }
    enriched["metadata_source"] = "openrouter_enrichment"
    has_cap = bool(_as_int(enriched.get("max_output_tokens")))
    has_pricing = (
        _as_float(enriched.get("input_token_cost")) > 0
        and _as_float(enriched.get("output_token_cost")) > 0
    )
    enriched["sync_status"] = "synced" if (has_cap and has_pricing) else "needs_review"
    return enriched
