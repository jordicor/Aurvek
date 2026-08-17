"""In-memory warm-up cache for chat context snapshots.

The warm-up path prepares read-only context before the user submits a message.
It never stores draft text and never calls an LLM.
"""

import asyncio
import os
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from itertools import islice
from typing import Any

from cachetools import TTLCache


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


_CACHE_SIZE = _env_int("CHAT_WARMUP_CACHE_SIZE", 512)
_TTL_SECONDS = _env_int("CHAT_WARMUP_TTL_SECONDS", 45)


@dataclass(frozen=True)
class WarmupCacheKey:
    user_id: int
    conversation_id: int
    llm_id: int
    effective_prompt_id: int
    active_extension_id: int
    last_message_id: int
    mode: str
    multi_ai_model_ids: tuple[int, ...] = ()


_warmup_cache: TTLCache = TTLCache(maxsize=_CACHE_SIZE, ttl=_TTL_SECONDS)
_singleflight_locks: dict[WarmupCacheKey, asyncio.Lock] = {}
_singleflight_guard = asyncio.Lock()
_stats = {
    "hits": 0,
    "misses": 0,
    "prepared": 0,
    "skipped": 0,
    "errors": 0,
    "consumed": 0,
}


def is_enabled() -> bool:
    return os.getenv("CHAT_WARMUP_ENABLED", "1").lower() not in {"0", "false", "no", "off"}


def get_ttl_seconds() -> int:
    return _TTL_SECONDS


def normalize_model_ids(value: Any) -> tuple[int, ...]:
    if value is None:
        return ()

    if isinstance(value, str):
        return ()

    if not isinstance(value, Iterable):
        return ()

    normalized = []
    seen = set()
    for item in islice(value, 16):
        try:
            model_id = int(item)
        except (TypeError, ValueError):
            continue
        if model_id <= 0 or model_id in seen:
            continue
        seen.add(model_id)
        normalized.append(model_id)
    return tuple(normalized)


def get_snapshot(key: WarmupCacheKey) -> dict[str, Any] | None:
    if not is_enabled():
        mark_skipped()
        return None

    snapshot = _warmup_cache.get(key)
    if snapshot is None:
        _stats["misses"] += 1
        return None

    _stats["hits"] += 1
    return snapshot


def put_snapshot(key: WarmupCacheKey, snapshot: dict[str, Any]) -> None:
    if not is_enabled():
        mark_skipped()
        return

    _warmup_cache[key] = snapshot
    _stats["prepared"] += 1


async def get_or_prepare(
    key: WarmupCacheKey,
    prepare_snapshot: Callable[[], Awaitable[dict[str, Any] | None]],
) -> tuple[dict[str, Any] | None, str]:
    if not is_enabled():
        mark_skipped()
        return None, "disabled"

    cached = _warmup_cache.get(key)
    if cached is not None:
        _stats["hits"] += 1
        return cached, "hit"

    _stats["misses"] += 1

    async with _singleflight_guard:
        lock = _singleflight_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _singleflight_locks[key] = lock

    async with lock:
        cached = _warmup_cache.get(key)
        if cached is not None:
            _stats["hits"] += 1
            return cached, "hit"

        snapshot = await prepare_snapshot()
        if snapshot is None:
            mark_skipped()
            return None, "skipped"

        snapshot.setdefault("created_at", time.time())
        _warmup_cache[key] = snapshot
        _stats["prepared"] += 1
        return snapshot, "prepared"


def mark_skipped() -> None:
    _stats["skipped"] += 1


def mark_error() -> None:
    _stats["errors"] += 1


def mark_consumed() -> None:
    _stats["consumed"] += 1


def get_stats() -> dict[str, int]:
    return dict(_stats)


def clear_warmup_cache() -> None:
    _warmup_cache.clear()
    _singleflight_locks.clear()
    for key in _stats:
        _stats[key] = 0
