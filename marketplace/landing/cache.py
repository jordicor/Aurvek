import asyncio
import os

from cachetools import LRUCache
from fastapi import HTTPException

from database import get_db_connection
from log_config import logger
from marketplace.config import require_public_landings_enabled, marketplace_public_landings_enabled
from marketplace.landing.paths import build_prompt_filesystem_path


LANDING_CACHE_SIZE = int(os.getenv("LANDING_CACHE_SIZE", "10000"))
LANDING_CACHE_WARMUP = int(os.getenv("LANDING_CACHE_WARMUP", "1000"))

_landing_path_cache: LRUCache = LRUCache(maxsize=LANDING_CACHE_SIZE)
_landing_cache_locks: dict = {}
_landing_cache_stats = {"hits": 0, "misses": 0}


def get_landing_cache_stats() -> dict:
    """Return prompt landing cache stats for status/admin reporting."""
    total_requests = _landing_cache_stats["hits"] + _landing_cache_stats["misses"]
    hit_rate = _landing_cache_stats["hits"] / max(1, total_requests)
    return {
        "size": len(_landing_path_cache),
        "max_size": LANDING_CACHE_SIZE,
        "hits": _landing_cache_stats["hits"],
        "misses": _landing_cache_stats["misses"],
        "hit_rate": round(hit_rate * 100, 2),
    }


async def warmup_landing_cache():
    """
    Pre-load the most visited prompts into cache on startup.
    Uses analytics data to prioritize popular landings.
    """
    if not marketplace_public_landings_enabled():
        logger.info("Landing cache warmup disabled (marketplace public landings disabled)")
        return

    if LANDING_CACHE_WARMUP <= 0:
        logger.info("Landing cache warmup disabled (LANDING_CACHE_WARMUP=0)")
        return

    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT p.public_id, p.id, p.name, p.is_unlisted,
                       p.public, p.has_landing_page, p.landing_trusted, u.username,
                       COALESCE(COUNT(a.id), 0) as visit_count
                FROM PROMPTS p
                JOIN USERS u ON p.created_by_user_id = u.id
                LEFT JOIN LANDING_PAGE_ANALYTICS a ON a.prompt_id = p.id
                WHERE p.public_id IS NOT NULL
                GROUP BY p.id, p.public_id, p.name, p.is_unlisted,
                         p.public, p.has_landing_page, p.landing_trusted, u.username
                ORDER BY visit_count DESC
                LIMIT ?
                """,
                (LANDING_CACHE_WARMUP,),
            )

            count = 0
            async for row in cursor:
                (public_id, prompt_id, name, is_unlisted,
                 public, has_landing_page, landing_trusted, username, _) = row
                path = build_prompt_filesystem_path(username, prompt_id, name)
                _landing_path_cache[public_id] = {
                    "prompt_id": prompt_id,
                    "prompt_name": name,
                    "is_unlisted": is_unlisted or 0,
                    "public": public or 0,
                    "has_landing_page": has_landing_page or 0,
                    "landing_trusted": landing_trusted or 0,
                    "username": username,
                    "path": path,
                }
                count += 1

        logger.info(f"Landing cache warmed: {count} entries (top by visits)")

    except Exception as e:
        logger.error(f"Failed to warm landing cache: {e}")


async def get_landing_path_cached(public_id: str) -> dict:
    """
    Get landing path with smart LRU caching.

    The query resolves the landing's identity, filesystem path AND the flags the
    serving routes need to enforce the access policy: ``public``,
    ``has_landing_page`` and ``landing_trusted``. It deliberately does NOT filter
    on those flags -- the authenticated admin preview path must still be able to
    resolve non-public landings. Enforcement (public = 1 AND has_landing_page = 1
    for public access; trusted vs. isolated serving) happens in the routes.
    """
    require_public_landings_enabled()

    cached = _landing_path_cache.get(public_id)
    if cached is not None:
        _landing_cache_stats["hits"] += 1
        return cached

    if public_id not in _landing_cache_locks:
        _landing_cache_locks[public_id] = asyncio.Lock()

    lock = _landing_cache_locks[public_id]

    async with lock:
        cached = _landing_path_cache.get(public_id)
        if cached is not None:
            _landing_cache_stats["hits"] += 1
            return cached

        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT p.id, p.name, p.is_unlisted,
                       p.public, p.has_landing_page, p.landing_trusted, u.username
                FROM PROMPTS p
                JOIN USERS u ON p.created_by_user_id = u.id
                WHERE p.public_id = ?
                """,
                (public_id,),
            )
            row = await cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Prompt not found")

        prompt_id, name, is_unlisted, public, has_landing_page, landing_trusted, username = row
        path = build_prompt_filesystem_path(username, prompt_id, name)

        result = {
            "prompt_id": prompt_id,
            "prompt_name": name,
            "is_unlisted": is_unlisted or 0,
            "public": public or 0,
            "has_landing_page": has_landing_page or 0,
            "landing_trusted": landing_trusted or 0,
            "username": username,
            "path": path,
        }

        _landing_path_cache[public_id] = result
        _landing_cache_stats["misses"] += 1

        if len(_landing_cache_locks) > LANDING_CACHE_SIZE * 2:
            _landing_cache_locks.clear()

        return result


def invalidate_landing_cache(public_id: str):
    """
    Remove a public_id from the landing cache.
    Call this when a prompt is updated or deleted.
    """
    _landing_path_cache.pop(public_id, None)
