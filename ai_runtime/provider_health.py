from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
from threading import RLock
from typing import Any

import aiohttp
import orjson

from log_config import logger


PROVIDER_OPERATIONAL = "operational"
PROVIDER_SUSPECTED = "suspected"
PROVIDER_DEGRADED = "degraded"
PROVIDER_RECOVERING = "recovering"

_EVENT_TTL_SECONDS = 15 * 60
_MAX_EVENTS_PER_PROVIDER = 300
_BASE_CHECK_INTERVAL_SECONDS = 5 * 60
_SUSPECTED_CHECK_INTERVAL_SECONDS = 90
_HTTP_TIMEOUT_SECONDS = 8

_PROVIDER_DISPLAY_NAMES = {
    "openai": "OpenAI",
    "gptsub": "ChatGPT subscription",
    "anthropic": "Claude",
    "google": "Gemini",
    "openrouter": "OpenRouter",
    "xai": "xAI",
    "minimax": "MiniMax",
    "kimi": "Kimi",
}

_SUSPICIOUS_STATUS_CODES = {408, 409, 425, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 529}

_NORMAL_ERROR_HINTS = (
    "api key required",
    "api_keys_required",
    "authentication",
    "authorization",
    "billing",
    "blocked by moderation",
    "creator markup",
    "does not support",
    "exceeds",
    "file attachments are not supported",
    "insufficient balance",
    "invalid api key",
    "invalid_request_error",
    "invalid request",
    "insufficient_quota",
    "manual thinking budget",
    "maximum context",
    "context_length_exceeded",
    "moderation",
    "model not found",
    "not authorized",
    "payload",
    "pdf",
    "permission",
    "policy",
    "quota",
    "rate limit",
    "safety",
    "too large",
    "unsupported",
)

_SUSPICIOUS_ERROR_HINTS = (
    "502",
    "503",
    "504",
    "connection error",
    "connection reset",
    "dns",
    "empty response",
    "gateway",
    "internal_error",
    "overloaded",
    "read timeout",
    "server error",
    "server_error",
    "service unavailable",
    "stream interrupted",
    "tls",
    "took too long",
    "timeout",
    "timed out",
    "upstream",
)


@dataclass
class ProviderHealthEvent:
    timestamp: float
    kind: str
    suspicious: bool = False
    reason_code: str = ""
    status_code: int | None = None
    model: str | None = None
    byok: bool = False


@dataclass
class ProviderHealthState:
    provider: str
    status: str = PROVIDER_OPERATIONAL
    source: str = "local"
    message: str = ""
    official_indicator: str | None = None
    official_incident: str | None = None
    last_checked_at: float | None = None
    last_activity_at: float | None = None
    last_status_change_at: float = field(default_factory=time.time)
    events: deque[ProviderHealthEvent] = field(default_factory=lambda: deque(maxlen=_MAX_EVENTS_PER_PROVIDER))


@dataclass(frozen=True)
class ErrorClassification:
    category: str
    reason_code: str
    suspicious: bool


_state_lock = RLock()
_states: dict[str, ProviderHealthState] = {}
_check_tasks: dict[str, asyncio.Task] = {}


def provider_display_name(provider: str | None) -> str:
    provider_key = normalize_provider_key(provider)
    return _PROVIDER_DISPLAY_NAMES.get(provider_key, provider_key or "AI provider")


def normalize_provider_key(value: str | None) -> str:
    provider = (value or "").strip().lower()
    aliases = {
        "anthropic": "anthropic",
        "claude": "anthropic",
        "gemini": "google",
        "google": "google",
        "gpt": "openai",
        "o1": "openai",
        "openai": "openai",
        "openai (gpt)": "openai",
        "openai (o1)": "openai",
        "openai responses": "openai",
        "openrouter": "openrouter",
        "x-ai": "xai",
        "xai": "xai",
        "xai (grok)": "xai",
        "minimax": "minimax",
        "kimi": "kimi",
        "moonshot": "kimi",
    }
    return aliases.get(provider, provider)


def provider_from_machine(machine: str | None, model: str | None = None) -> str:
    return normalize_provider_key(machine)


def provider_from_label(label: str | None) -> str:
    label_lower = (label or "").strip().lower()
    if "openrouter" in label_lower:
        return "openrouter"
    if "gptsub" in label_lower:
        # The subscription (GPTSub) path must NOT collapse into the main "openai"
        # health bucket -- its errors/successes are tracked separately so a GPTSub
        # stall never degrades the main GPT provider signal.
        return "gptsub"
    if "openai" in label_lower or "gpt" in label_lower or "o1" in label_lower:
        return "openai"
    if "claude" in label_lower or "anthropic" in label_lower:
        return "anthropic"
    if "gemini" in label_lower or "google" in label_lower:
        return "google"
    if "xai" in label_lower or "grok" in label_lower:
        return "xai"
    if "minimax" in label_lower:
        return "minimax"
    if "kimi" in label_lower or "moonshot" in label_lower:
        return "kimi"
    return normalize_provider_key(label)


def classify_provider_error(
    message: str | None = None,
    *,
    status_code: int | None = None,
    exception: BaseException | None = None,
) -> ErrorClassification:
    text = " ".join(str(part) for part in (message, exception) if part).lower()

    if status_code in _SUSPICIOUS_STATUS_CODES:
        return ErrorClassification("suspicious", f"http_{status_code}", True)

    if status_code in {400, 401, 403, 404, 413, 422}:
        return ErrorClassification("normal", f"http_{status_code}", False)

    if status_code == 429:
        for hint in _SUSPICIOUS_ERROR_HINTS:
            if hint in text:
                return ErrorClassification("suspicious", hint.replace(" ", "_"), True)
        return ErrorClassification("normal", "quota_or_rate_limit", False)

    if isinstance(exception, (asyncio.TimeoutError, aiohttp.ClientError)):
        return ErrorClassification("suspicious", exception.__class__.__name__.lower(), True)

    for hint in _NORMAL_ERROR_HINTS:
        if hint in text:
            return ErrorClassification("normal", hint.replace(" ", "_"), False)

    for hint in _SUSPICIOUS_ERROR_HINTS:
        if hint in text:
            return ErrorClassification("suspicious", hint.replace(" ", "_"), True)

    if status_code and status_code >= 500:
        return ErrorClassification("suspicious", f"http_{status_code}", True)

    return ErrorClassification("unknown", "unknown", False)


async def record_provider_success(
    provider: str | None,
    *,
    model: str | None = None,
    byok: bool = False,
) -> dict[str, Any]:
    provider_key = normalize_provider_key(provider)
    if not provider_key:
        return {}
    snapshot = _record_event(
        provider_key,
        ProviderHealthEvent(
            timestamp=time.time(),
            kind="success",
            model=model,
            byok=byok,
        ),
    )
    maybe_schedule_provider_check(provider_key)
    return snapshot


async def record_provider_success_for_label(
    provider_label: str | None,
    *,
    model: str | None = None,
    byok: bool = False,
) -> dict[str, Any]:
    return await record_provider_success(provider_from_label(provider_label), model=model, byok=byok)


async def record_provider_error(
    provider: str | None,
    *,
    message: str | None = None,
    status_code: int | None = None,
    exception: BaseException | None = None,
    model: str | None = None,
    byok: bool = False,
) -> dict[str, Any]:
    provider_key = normalize_provider_key(provider)
    if not provider_key:
        return {}
    classification = classify_provider_error(message, status_code=status_code, exception=exception)
    snapshot = _record_event(
        provider_key,
        ProviderHealthEvent(
            timestamp=time.time(),
            kind="error",
            suspicious=classification.suspicious,
            reason_code=classification.reason_code,
            status_code=status_code,
            model=model,
            byok=byok,
        ),
    )
    if classification.suspicious:
        maybe_schedule_provider_check(provider_key, force=True)
    else:
        maybe_schedule_provider_check(provider_key)
    return snapshot


async def record_provider_error_for_label(
    provider_label: str | None,
    *,
    message: str | None = None,
    status_code: int | None = None,
    exception: BaseException | None = None,
    model: str | None = None,
    byok: bool = False,
) -> dict[str, Any]:
    return await record_provider_error(
        provider_from_label(provider_label),
        message=message,
        status_code=status_code,
        exception=exception,
        model=model,
        byok=byok,
    )


def touch_provider_activity(provider: str | None) -> dict[str, Any]:
    provider_key = normalize_provider_key(provider)
    if not provider_key:
        return {}
    now = time.time()
    with _state_lock:
        state = _get_state_locked(provider_key)
        state.last_activity_at = now
        _prune_events_locked(state, now)
        _recalculate_status_locked(state, now)
        snapshot = _public_snapshot_locked(state)
    maybe_schedule_provider_check(provider_key)
    return snapshot


def get_provider_health(provider: str | None) -> dict[str, Any]:
    provider_key = normalize_provider_key(provider)
    if not provider_key:
        return {}
    now = time.time()
    with _state_lock:
        state = _get_state_locked(provider_key)
        _prune_events_locked(state, now)
        _recalculate_status_locked(state, now)
        return _public_snapshot_locked(state)


def get_provider_health_for_machine(machine: str | None, model: str | None = None) -> dict[str, Any]:
    return get_provider_health(provider_from_machine(machine, model))


def get_provider_health_for_label(label: str | None) -> dict[str, Any]:
    return get_provider_health(provider_from_label(label))


def provider_health_for_error_payload(provider_label: str | None) -> dict[str, Any]:
    health = get_provider_health_for_label(provider_label)
    if not should_surface_provider_health(health):
        return {}
    return {
        "provider_health": health,
        "provider": health["provider"],
        "provider_status": health["status"],
        "provider_health_message": health["message"],
    }


def should_surface_provider_health(health: dict[str, Any] | None) -> bool:
    if not health:
        return False
    return health.get("status") in {PROVIDER_SUSPECTED, PROVIDER_DEGRADED, PROVIDER_RECOVERING}


def append_external_error_note(message: str, provider: str | None) -> str:
    health = get_provider_health(provider)
    if not should_surface_provider_health(health):
        return message
    note = (
        f"Note: we are detecting recent errors from the selected AI provider "
        f"({health['provider_name']}), so it may fail temporarily."
    )
    if note in message:
        return message
    return f"{message.rstrip()}\n\n{note}"


def reset_provider_health_state() -> None:
    """Test helper: reset in-memory provider health state."""
    with _state_lock:
        _states.clear()
        for task in _check_tasks.values():
            task.cancel()
        _check_tasks.clear()


def maybe_schedule_provider_check(provider: str | None, *, force: bool = False) -> None:
    provider_key = normalize_provider_key(provider)
    if not provider_key:
        return

    with _state_lock:
        state = _get_state_locked(provider_key)
        now = time.time()
        if not force and not _official_check_due_locked(state, now):
            return
        task = _check_tasks.get(provider_key)
        if task and not task.done():
            return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    task = loop.create_task(_run_official_check(provider_key))
    with _state_lock:
        _check_tasks[provider_key] = task


def _record_event(provider: str, event: ProviderHealthEvent) -> dict[str, Any]:
    with _state_lock:
        state = _get_state_locked(provider)
        state.last_activity_at = event.timestamp
        state.events.append(event)
        _prune_events_locked(state, event.timestamp)
        _recalculate_status_locked(state, event.timestamp)
        return _public_snapshot_locked(state)


def _get_state_locked(provider: str) -> ProviderHealthState:
    state = _states.get(provider)
    if not state:
        state = ProviderHealthState(provider=provider)
        _states[provider] = state
    return state


def _prune_events_locked(state: ProviderHealthState, now: float | None = None) -> None:
    cutoff = (now or time.time()) - _EVENT_TTL_SECONDS
    while state.events and state.events[0].timestamp < cutoff:
        state.events.popleft()


def _recalculate_status_locked(state: ProviderHealthState, now: float) -> None:
    recent_60 = [event for event in state.events if event.timestamp >= now - 60]
    recent_5m = [event for event in state.events if event.timestamp >= now - 300]
    suspicious_60 = [event for event in recent_60 if event.kind == "error" and event.suspicious]
    suspicious_5m = [event for event in recent_5m if event.kind == "error" and event.suspicious]
    successes_2m = [event for event in state.events if event.kind == "success" and event.timestamp >= now - 120]

    if state.official_indicator in {"minor", "major", "critical"}:
        _set_status_locked(state, PROVIDER_DEGRADED, "official_status")
        state.message = _official_degraded_message(state)
        return

    if len(suspicious_5m) >= 5:
        _set_status_locked(state, PROVIDER_DEGRADED, "local_errors")
        state.message = _local_degraded_message(state, len(suspicious_5m))
        return

    if len(recent_5m) >= 10 and len(suspicious_5m) >= 3 and (len(suspicious_5m) / len(recent_5m)) >= 0.2:
        _set_status_locked(state, PROVIDER_DEGRADED, "local_errors")
        state.message = _local_degraded_message(state, len(suspicious_5m))
        return

    if len(suspicious_60) >= 3:
        _set_status_locked(state, PROVIDER_SUSPECTED, "local_errors")
        state.message = _local_suspected_message(state)
        return

    if state.status in {PROVIDER_SUSPECTED, PROVIDER_DEGRADED} and successes_2m:
        _set_status_locked(state, PROVIDER_RECOVERING, "local_successes")
        state.message = (
            f"{provider_display_name(state.provider)} recently had connection errors, "
            "but successful responses are being seen again."
        )
        return

    if state.status == PROVIDER_RECOVERING and len(successes_2m) >= 3 and not suspicious_60:
        _set_status_locked(state, PROVIDER_OPERATIONAL, "local_successes")
        state.message = ""
        return

    if state.status in {PROVIDER_SUSPECTED, PROVIDER_DEGRADED} and not suspicious_5m:
        _set_status_locked(state, PROVIDER_OPERATIONAL, "local")
        state.message = ""
        return

    if state.status == PROVIDER_RECOVERING and not suspicious_5m and not successes_2m:
        _set_status_locked(state, PROVIDER_OPERATIONAL, "local")
        state.message = ""
        return

    if state.status == PROVIDER_OPERATIONAL:
        state.source = "local"
        state.message = ""


def _set_status_locked(state: ProviderHealthState, status: str, source: str) -> None:
    if state.status != status:
        state.status = status
        state.last_status_change_at = time.time()
    state.source = source


def _official_check_due_locked(state: ProviderHealthState, now: float) -> bool:
    if not state.last_activity_at:
        return False
    interval = (
        _SUSPECTED_CHECK_INTERVAL_SECONDS
        if state.status in {PROVIDER_SUSPECTED, PROVIDER_DEGRADED, PROVIDER_RECOVERING}
        else _BASE_CHECK_INTERVAL_SECONDS
    )
    if state.last_checked_at is None:
        return True
    return (now - state.last_checked_at) >= interval


async def _run_official_check(provider: str) -> None:
    try:
        result = await _fetch_official_status(provider)
        with _state_lock:
            state = _get_state_locked(provider)
            state.last_checked_at = time.time()
            state.official_indicator = result.get("indicator")
            state.official_incident = result.get("incident")
            if result.get("source"):
                state.source = result["source"]
            if state.official_indicator in {"minor", "major", "critical"}:
                _set_status_locked(state, PROVIDER_DEGRADED, "official_status")
                state.message = _official_degraded_message(state)
            elif state.status == PROVIDER_DEGRADED and state.source == "official_status":
                _set_status_locked(state, PROVIDER_RECOVERING, "official_status")
                state.message = (
                    f"{provider_display_name(provider)} no longer reports an active incident. "
                    "We are watching for successful responses."
                )
            elif state.status == PROVIDER_OPERATIONAL:
                state.message = ""
    except Exception as exc:
        logger.warning("[provider_health] Official check failed for %s: %s", provider, exc)
        with _state_lock:
            state = _get_state_locked(provider)
            state.last_checked_at = time.time()
    finally:
        current_task = asyncio.current_task()
        with _state_lock:
            task = _check_tasks.get(provider)
            if task is current_task:
                _check_tasks.pop(provider, None)


async def _fetch_official_status(provider: str) -> dict[str, Any]:
    if provider == "openai":
        return await _fetch_statuspage_json("https://status.openai.com/api/v2/status.json")
    if provider == "anthropic":
        return await _fetch_statuspage_json("https://status.claude.com/api/v2/status.json")
    if provider == "google":
        return await _fetch_google_cloud_incidents()
    if provider == "openrouter":
        return await _fetch_openrouter_status()
    if provider == "xai":
        return await _fetch_xai_status()
    return {"indicator": None, "source": "unsupported"}


async def _fetch_statuspage_json(url: str) -> dict[str, Any]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECONDS)) as session:
        async with session.get(url) as response:
            body = await response.read()
            response.raise_for_status()
            payload = orjson.loads(body)
    status = payload.get("status") or {}
    indicator = status.get("indicator")
    description = status.get("description")
    return {
        "indicator": "none" if indicator in (None, "", "none") else indicator,
        "incident": description,
        "source": "official_status",
    }


async def _fetch_google_cloud_incidents() -> dict[str, Any]:
    url = "https://status.cloud.google.com/incidents.json"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECONDS)) as session:
        async with session.get(url) as response:
            body = await response.read()
            response.raise_for_status()
            payload = orjson.loads(body)
    now = time.time()
    active_incidents = []
    for incident in payload if isinstance(payload, list) else []:
        if incident.get("end"):
            continue
        haystack = " ".join(
            str(incident.get(key) or "")
            for key in ("external_desc", "begin", "modified", "most_recent_update", "service_name")
        ).lower()
        for product in incident.get("affected_products") or []:
            if isinstance(product, dict):
                haystack += " " + " ".join(str(v or "") for v in product.values()).lower()
            else:
                haystack += " " + str(product).lower()
        if any(term in haystack for term in ("vertex ai gemini", "gemini api", "generative ai", "ai studio")):
            active_incidents.append(incident)
    if active_incidents:
        incident = active_incidents[0]
        return {
            "indicator": "major",
            "incident": incident.get("external_desc") or incident.get("id") or "Google reports an active Gemini-related incident.",
            "source": "official_status",
        }
    return {"indicator": "none", "incident": None, "source": "official_status", "checked_at": now}


async def _fetch_openrouter_status() -> dict[str, Any]:
    html = await _fetch_text("https://status.openrouter.ai/")
    lowered = html.lower()
    if "all systems operational" in lowered:
        return {"indicator": "none", "incident": None, "source": "official_status"}
    if any(term in lowered for term in ("degraded", "partial outage", "major outage", "incident")):
        text = _compact_html_text(html)
        return {
            "indicator": "major",
            "incident": text[:160] or "OpenRouter status page does not report all systems operational.",
            "source": "official_status",
        }
    return {"indicator": None, "incident": None, "source": "official_status"}


async def _fetch_xai_status() -> dict[str, Any]:
    try:
        html = await _fetch_text("https://status.x.ai/")
    except Exception:
        feed = await _fetch_text("https://status.x.ai/feed.xml")
        if "<item>" in feed.lower():
            return {"indicator": None, "incident": None, "source": "official_status"}
        raise
    lowered = html.lower()
    if "all systems operational" in lowered:
        return {"indicator": "none", "incident": None, "source": "official_status"}
    if any(term in lowered for term in ("degraded", "partial outage", "major outage", "incident")):
        return {
            "indicator": "major",
            "incident": _compact_html_text(html)[:160] or "xAI status page reports an incident.",
            "source": "official_status",
        }
    return {"indicator": None, "incident": None, "source": "official_status"}


async def _fetch_text(url: str) -> str:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECONDS)) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.text()


def _official_degraded_message(state: ProviderHealthState) -> str:
    provider_name = provider_display_name(state.provider)
    if state.official_incident:
        return f"{provider_name} reports an API incident. This model may fail temporarily or respond more slowly."
    return f"{provider_name} reports degraded service. This model may fail temporarily or respond more slowly."


def _local_suspected_message(state: ProviderHealthState) -> str:
    provider_name = provider_display_name(state.provider)
    return (
        f"We are detecting recent connection errors with {provider_name}. "
        "This model may fail temporarily or take longer than usual."
    )


def _local_degraded_message(state: ProviderHealthState, count: int) -> str:
    provider_name = provider_display_name(state.provider)
    return (
        f"We are detecting repeated recent errors with {provider_name}. "
        "This model may fail temporarily or take longer than usual."
    )


def _public_snapshot_locked(state: ProviderHealthState) -> dict[str, Any]:
    recent = list(state.events)
    suspicious = [event for event in recent if event.kind == "error" and event.suspicious]
    return {
        "provider": state.provider,
        "provider_name": provider_display_name(state.provider),
        "status": state.status,
        "source": state.source,
        "message": state.message,
        "official_indicator": state.official_indicator,
        "official_incident": state.official_incident,
        "last_checked_at": state.last_checked_at,
        "last_activity_at": state.last_activity_at,
        "last_status_change_at": state.last_status_change_at,
        "recent_event_count": len(recent),
        "recent_suspicious_error_count": len(suspicious),
        "surface": state.status in {PROVIDER_SUSPECTED, PROVIDER_DEGRADED, PROVIDER_RECOVERING},
    }


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)


def _compact_html_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return " ".join(parser.parts).strip()
