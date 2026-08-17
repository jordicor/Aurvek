import io
import os
import re
import base64
import asyncio
import secrets
import stripe
import orjson
import logging
import json as json_mod
from pathlib import Path
from typing import Optional, List
from unicodedata import normalize
from cachetools import LRUCache
from PIL import Image as PilImage
from fastapi import APIRouter, Depends, HTTPException, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse

from models import User, Pack
from auth import get_current_user
from billing.discounts import (
    DiscountError,
    claim_discount_usage_for_checkout,
    decrement_discount_usage,
    restore_discount_usage_for_checkout,
    validate_discount_code,
)
from database import (
    get_db_connection,
    create_pack, get_pack, get_user_packs, update_pack, delete_pack,
    get_pack_items, get_public_pack_items, add_pack_item, remove_pack_item, reorder_pack_items,
    get_available_prompts_for_pack,
    count_user_packs, count_user_packs_today, get_public_packs,
    get_pack_by_public_id, create_pack_purchase, get_pack_purchase_by_reference,
    get_pack_purchases,
)
from common import (
    MAX_PACKS_PER_USER, MAX_PACK_ITEMS, MIN_PACK_ITEMS_TO_PUBLISH,
    PACK_CREATION_RATE_LIMIT, MAX_PACK_TAGS, MAX_TAG_LENGTH, MAX_PACK_PRICE,
    MIN_PACK_PAID_PRICE, MAX_FREE_INITIAL_BALANCE,
    MAX_COVER_FULLSIZE_WIDTH,
    PACK_STATUSES, DATA_DIR, MAX_IMAGE_UPLOAD_SIZE, MAX_IMAGE_PIXELS,
    MODERATION_COST_FIXED, MODERATION_MIN_BALANCE,
    STRIPE_SECRET_KEY, GOOGLE_CLIENT_ID,
    get_template_context, slugify, generate_public_id, generate_user_hash,
    sanitize_name, templates, get_balance, deduct_balance,
    validate_path_within_directory, get_pricing_config,
    fix_landing_seo_tags,
)
from save_images import resize_image_cover
from security_guard_llm import check_security, is_security_guard_enabled
from marketplace.landing.wizard import is_claude_available, list_prompt_files, list_welcome_files, delete_all_landing_files, delete_all_welcome_files
from marketplace.landing.jobs import start_job, get_job, get_active_job_for_pack, get_active_welcome_job_for_pack
from marketplace.landing.isolation import (
    build_creator_content_url,
    creator_content_unavailable_response,
    is_creator_content_request,
    sign_content_token,
    verify_content_token,
)
from marketplace.services.entitlements import (
    active_entitlement_condition,
    grant_pack_entitlement,
    revoke_entitlement,
    user_has_pack_access as user_has_pack_entitlement_access,
)
from mobile.client import (
    ios_purchase_blocked,
    ios_purchase_disabled_response,
    purchase_metadata_for_request,
)
from prompts import create_pack_directory, get_pack_path, get_pack_components_dir
from marketplace.services.landing_registration import (
    sanitize_landing_reg_config,
    validate_shared_landing_config,
)
from ranking import maybe_trigger_recalculation
from marketplace.config import (
    marketplace_public_landings_enabled,
    require_checkout_enabled,
    require_creator_tools_enabled,
    require_discovery_enabled,
    require_public_landings_enabled,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Pack Landing Page LRU Cache
# ---------------------------------------------------------------------------

PACK_LANDING_CACHE_SIZE = int(os.getenv("PACK_LANDING_CACHE_SIZE", "5000"))
PACK_LANDING_CACHE_WARMUP = int(os.getenv("PACK_LANDING_CACHE_WARMUP", "500"))
_pack_landing_cache: LRUCache = LRUCache(maxsize=PACK_LANDING_CACHE_SIZE)
_pack_landing_cache_locks: dict = {}  # Singleflight locks per public_id
_pack_landing_cache_stats = {"hits": 0, "misses": 0}


# ---------------------------------------------------------------------------
# Pack Landing Cache Functions
# ---------------------------------------------------------------------------

async def get_pack_landing_cached(public_id: str) -> dict:
    """
    Get pack landing data with smart LRU caching.

    - Cache HIT: O(1), zero DB queries
    - Cache MISS: 1 query, result cached for future requests
    - LRU eviction: Least recently used entries removed when cache is full
    - Singleflight: Concurrent requests for same public_id share one DB query

    Returns:
        dict with pack data and pre-built filesystem path, or None if not found.
    """
    require_public_landings_enabled()

    global _pack_landing_cache_stats

    # Fast path: cache hit
    cached = _pack_landing_cache.get(public_id)
    if cached is not None:
        _pack_landing_cache_stats["hits"] += 1
        return cached

    # Singleflight: get or create lock for this public_id (atomic via setdefault)
    lock = _pack_landing_cache_locks.setdefault(public_id, asyncio.Lock())

    async with lock:
        # Double-check after acquiring lock (another request may have populated cache)
        cached = _pack_landing_cache.get(public_id)
        if cached is not None:
            _pack_landing_cache_stats["hits"] += 1
            return cached

        # Query DB
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(f"""
                SELECT p.id, p.name, p.slug, p.description, p.cover_image,
                       p.is_paid, p.price, p.status, p.is_public,
                       p.has_custom_landing, p.created_by_user_id, p.tags,
                       u.username
                FROM PACKS p
                JOIN USERS u ON p.created_by_user_id = u.id
                WHERE p.public_id = ?
            """, (public_id,))
            row = await cursor.fetchone()

        if not row:
            return None

        pack_id = row[0]
        pack_name = row[1]
        username = row[12]
        path = _build_pack_filesystem_path(username, pack_id, pack_name)

        result = {
            "pack_id": pack_id,
            "pack_name": pack_name,
            "slug": row[2],
            "description": row[3],
            "cover_image": row[4],
            "is_paid": row[5],
            "price": row[6],
            "status": row[7],
            "is_public": row[8],
            "has_custom_landing": row[9],
            "created_by_user_id": row[10],
            "tags": row[11],
            "username": username,
            "path": path,
        }

        _pack_landing_cache[public_id] = result
        _pack_landing_cache_stats["misses"] += 1

        # Cleanup stale locks (only remove those not in active cache)
        if len(_pack_landing_cache_locks) > PACK_LANDING_CACHE_SIZE * 2:
            cached_keys = set(_pack_landing_cache.keys())
            stale = [k for k in _pack_landing_cache_locks if k not in cached_keys]
            for k in stale:
                _pack_landing_cache_locks.pop(k, None)

        return result


def invalidate_pack_landing_cache(public_id: str):
    """
    Remove a public_id from the pack landing cache.
    Call this when a pack is updated, deleted, published, or unpublished.
    """
    _pack_landing_cache.pop(public_id, None)


async def warmup_pack_landing_cache():
    """
    Pre-load published packs into cache on startup.
    Uses active pack entitlements to prioritize popular packs.
    """
    if not marketplace_public_landings_enabled():
        logger.info("Pack landing cache warmup disabled (marketplace public landings disabled)")
        return

    if PACK_LANDING_CACHE_WARMUP <= 0:
        logger.info("Pack landing cache warmup disabled (PACK_LANDING_CACHE_WARMUP=0)")
        return

    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(f"""
                SELECT p.id, p.name, p.slug, p.description, p.cover_image,
                       p.is_paid, p.price, p.status, p.is_public,
                       p.has_custom_landing, p.created_by_user_id, p.tags,
                       p.public_id, u.username,
                       COALESCE(COUNT(e.id), 0) AS access_count
                FROM PACKS p
                JOIN USERS u ON p.created_by_user_id = u.id
                LEFT JOIN ENTITLEMENTS e ON e.asset_type = 'pack'
                    AND e.asset_id = p.id
                    AND {active_entitlement_condition("e")}
                WHERE p.public_id IS NOT NULL AND p.status = 'published'
                GROUP BY p.id
                ORDER BY access_count DESC
                LIMIT ?
            """, (PACK_LANDING_CACHE_WARMUP,))

            count = 0
            async for row in cursor:
                pack_id = row[0]
                pack_name = row[1]
                public_id = row[12]
                username = row[13]
                path = _build_pack_filesystem_path(username, pack_id, pack_name)

                _pack_landing_cache[public_id] = {
                    "pack_id": pack_id,
                    "pack_name": pack_name,
                    "slug": row[2],
                    "description": row[3],
                    "cover_image": row[4],
                    "is_paid": row[5],
                    "price": row[6],
                    "status": row[7],
                    "is_public": row[8],
                    "has_custom_landing": row[9],
                    "created_by_user_id": row[10],
                    "tags": row[11],
                    "username": username,
                    "path": path,
                }
                count += 1

        logger.info(f"Pack landing cache warmed: {count} entries (top by access count)")

    except Exception as e:
        logger.error(f"Failed to warm pack landing cache: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _require_admin_or_user(current_user: User):
    """Raise 403 if user is not admin or user (elevated role)."""
    if not await current_user.is_admin and not await current_user.is_user:
        raise HTTPException(status_code=403, detail="Access denied")


async def _require_pack_owner(pack_row, current_user: User):
    """Raise 404 if pack not found, 403 if user is not owner (unless admin)."""
    if not pack_row:
        raise HTTPException(status_code=404, detail="Pack not found")
    if not await current_user.is_admin and pack_row["created_by_user_id"] != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")


def _valid_landing_resource_path(resource_path: str) -> bool:
    if not resource_path or "\\" in resource_path or resource_path.startswith("/"):
        return False
    return ".." not in Path(resource_path).parts


async def _can_preview_pack(request: Request, creator_id: int) -> bool:
    current_user = await get_current_user(request)
    if current_user is None:
        return False
    return bool(await current_user.is_admin or int(current_user.id) == int(creator_id))


async def _require_wizard_security_check(text: str, *, label: str, pack_id: int) -> None:
    """Run the wizard guard fail-closed; the OS sandbox remains the boundary."""
    try:
        result = await check_security(text)
    except Exception as exc:
        logger.error(
            "Security Guard error for %s on pack %s (blocking): %s",
            label,
            pack_id,
            exc,
        )
        raise HTTPException(
            status_code=503,
            detail="AI Wizard security check is temporarily unavailable",
        ) from exc

    if not result.get("checked"):
        logger.error(
            "Security Guard unavailable for %s on pack %s: %s",
            label,
            pack_id,
            result.get("reason", "unknown error"),
        )
        raise HTTPException(
            status_code=503,
            detail="AI Wizard security check is temporarily unavailable",
        )
    if not result.get("allowed"):
        logger.warning(
            "Security Guard BLOCKED %s for pack %s: %s",
            label,
            pack_id,
            result.get("reason", "blocked"),
        )
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Your request was blocked by security check",
                "reason": result.get("reason", "blocked"),
            },
        )


def _validate_tags(tags_input) -> Optional[str]:
    """Validate and sanitize tags (accepts JSON string or list). Returns cleaned JSON or None."""
    if not tags_input:
        return None
    if isinstance(tags_input, list):
        tags = tags_input
    elif isinstance(tags_input, str):
        if not tags_input.strip():
            return None
        try:
            tags = orjson.loads(tags_input)
        except orjson.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid tags JSON")
    else:
        raise HTTPException(status_code=400, detail="Invalid tags format")
    if not isinstance(tags, list):
        raise HTTPException(status_code=400, detail="Tags must be a JSON array")
    if len(tags) > MAX_PACK_TAGS:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_PACK_TAGS} tags allowed")
    cleaned = []
    for tag in tags:
        if not isinstance(tag, str):
            continue
        tag = tag.strip()[:MAX_TAG_LENGTH]
        tag = re.sub(r'[<>"\'&]', '', tag)
        if tag and tag not in cleaned:
            cleaned.append(tag)
    return orjson.dumps(cleaned).decode("utf-8") if cleaned else None


def _inject_pack_analytics(html_content: str, pack_id: int) -> str:
    """Inject analytics tracking script into pack landing HTML."""
    if "_aurvek_analytics_loaded" in html_content:
        return html_content  # Already injected

    analytics_script = f"""
<!-- Aurvek Analytics Tracking -->
<script>
(function() {{
    if (window._aurvek_analytics_loaded) return;
    window._aurvek_analytics_loaded = true;
    fetch('/api/analytics/track-visit', {{
        method: 'POST',
        mode: 'no-cors',
        headers: {{'Content-Type': 'text/plain;charset=UTF-8'}},
        body: JSON.stringify({{
            pack_id: {pack_id},
            page_path: window.location.pathname,
            referrer: document.referrer || ''
        }}),
        credentials: 'include'
    }}).catch(function(e) {{ console.log('Analytics:', e); }});
}})();
</script>
"""

    # Try to inject before </body>
    for tag in ["</body>", "</BODY>"]:
        if tag in html_content:
            return html_content.replace(tag, analytics_script + tag)

    # Fallback: append at end
    return html_content + analytics_script


# ---------------------------------------------------------------------------
# Admin pages (HTML)
# ---------------------------------------------------------------------------

@router.get("/admin/packs", response_class=HTMLResponse)
async def admin_packs_list(request: Request, current_user: User = Depends(get_current_user)):
    require_creator_tools_enabled()

    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request})
    await _require_admin_or_user(current_user)

    is_admin = await current_user.is_admin
    async with get_db_connection(readonly=True) as conn:
        packs = await get_user_packs(conn, current_user.id, is_admin=is_admin)

    context = await get_template_context(request, current_user)
    context["packs"] = packs
    return templates.TemplateResponse("admin_packs.html", context)


@router.get("/admin/packs/new", response_class=HTMLResponse)
async def admin_pack_new(request: Request, current_user: User = Depends(get_current_user)):
    require_creator_tools_enabled()

    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request})
    await _require_admin_or_user(current_user)

    pricing_config = await get_pricing_config()
    context = await get_template_context(request, current_user)
    context["pack"] = None
    context["pack_items"] = []
    context["commission_rate"] = pricing_config["commission"]
    context["max_free_initial_balance"] = MAX_FREE_INITIAL_BALANCE
    context["max_initial_balance"] = MAX_FREE_INITIAL_BALANCE
    context["welcome_message_content"] = ""
    return templates.TemplateResponse("admin_packs_edit.html", context)


@router.get("/admin/packs/edit/{pack_id}", response_class=HTMLResponse)
async def admin_pack_edit(request: Request, pack_id: int, current_user: User = Depends(get_current_user)):
    require_creator_tools_enabled()

    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request})
    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)
        items = await get_pack_items(conn, pack_id)

        # Get welcome message content (avoids a separate HTTP request from the frontend)
        async with conn.execute(
            "SELECT content FROM WELCOME_MESSAGES WHERE entity_type = 'pack' AND entity_id = ? AND is_active = 1",
            (pack_id,)
        ) as cursor:
            wm_row = await cursor.fetchone()
            welcome_message_content = wm_row[0] if wm_row else ""

    context = await get_template_context(request, current_user)
    pack_dict = dict(pack_row)
    # Parse JSON fields so the template receives dicts, not strings
    if pack_dict.get("landing_reg_config") and isinstance(pack_dict["landing_reg_config"], str):
        try:
            pack_dict["landing_reg_config"] = orjson.loads(pack_dict["landing_reg_config"])
        except Exception:
            pack_dict["landing_reg_config"] = {}
    if pack_dict.get("tags") and isinstance(pack_dict["tags"], str):
        try:
            pack_dict["tags"] = orjson.loads(pack_dict["tags"])
        except Exception:
            pack_dict["tags"] = []
    context["pack"] = pack_dict
    context["pack_items"] = [dict(item) for item in items]

    # Compute initial_balance cap for the UI hint
    pricing_config = await get_pricing_config()
    context["commission_rate"] = pricing_config["commission"]
    context["max_free_initial_balance"] = MAX_FREE_INITIAL_BALANCE
    if pack_dict.get("is_paid") and pack_dict.get("price", 0) > 0:
        context["max_initial_balance"] = round(pack_dict["price"] * (1 - pricing_config["commission"]), 2)
    else:
        context["max_initial_balance"] = MAX_FREE_INITIAL_BALANCE

    context["welcome_message_content"] = welcome_message_content
    return templates.TemplateResponse("admin_packs_edit.html", context)


# ---------------------------------------------------------------------------
# Pack CRUD API
# ---------------------------------------------------------------------------

@router.post("/api/packs")
async def api_create_pack(request: Request, current_user: User = Depends(get_current_user)):
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Pack name is required")

    slug = slugify(body.get("slug") or name)
    if not slug:
        raise HTTPException(status_code=400, detail="Invalid slug")

    description = (body.get("description") or "").strip()
    is_paid = bool(body.get("is_paid", False))
    try:
        price = float(body.get("price", 0.0)) if is_paid else 0.0
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid price value")
    if price < 0 or price > MAX_PACK_PRICE:
        raise HTTPException(status_code=400, detail=f"Price must be between 0 and {MAX_PACK_PRICE}")
    if is_paid and price < MIN_PACK_PAID_PRICE:
        raise HTTPException(status_code=400, detail=f"Minimum price for paid packs is ${MIN_PACK_PAID_PRICE:.2f}")

    tags_json = _validate_tags(body.get("tags"))

    # Extract and sanitize landing_reg_config (new packs are draft, use free cap)
    lrc_json = None
    lrc = body.get("landing_reg_config")
    if lrc is not None:
        if isinstance(lrc, str):
            try:
                lrc = orjson.loads(lrc)
            except Exception:
                lrc = None
        if isinstance(lrc, dict):
            lrc = sanitize_landing_reg_config(lrc, max_initial_balance=MAX_FREE_INITIAL_BALANCE)
            lrc = await validate_shared_landing_config(lrc)
            if lrc.get("billing_mode") == "user_pays":
                creator_balance = await get_balance(current_user.id)
                if creator_balance <= 0:
                    raise HTTPException(
                        status_code=400,
                        detail="You need a positive balance to enable 'user pays' mode"
                    )
            lrc_json = orjson.dumps(lrc).decode("utf-8")

    async with get_db_connection() as conn:
        # Rate limits
        total = await count_user_packs(conn, current_user.id)
        if total >= MAX_PACKS_PER_USER:
            raise HTTPException(status_code=400, detail=f"Maximum {MAX_PACKS_PER_USER} packs allowed")

        today_count = await count_user_packs_today(conn, current_user.id)
        if today_count >= PACK_CREATION_RATE_LIMIT:
            raise HTTPException(status_code=429, detail="Pack creation rate limit exceeded. Try again tomorrow.")

        # Check slug uniqueness
        cursor = await conn.execute("SELECT id FROM PACKS WHERE slug = ?", (slug,))
        if await cursor.fetchone():
            raise HTTPException(status_code=400, detail="A pack with this slug already exists")

        public_id = generate_public_id()
        pack_id = await create_pack(
            conn, name=name, slug=slug, description=description,
            created_by_user_id=current_user.id, is_paid=is_paid,
            price=price, tags=tags_json, public_id=public_id,
            landing_reg_config=lrc_json,
        )

    return JSONResponse({"id": pack_id, "slug": slug, "public_id": public_id, "message": "Pack created"})


@router.get("/api/packs")
async def api_list_packs(request: Request, current_user: User = Depends(get_current_user)):
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    is_admin = await current_user.is_admin
    async with get_db_connection(readonly=True) as conn:
        packs = await get_user_packs(conn, current_user.id, is_admin=is_admin)

    return JSONResponse([dict(p) for p in packs])


@router.get("/api/packs/{pack_id}")
async def api_get_pack(pack_id: int, current_user: User = Depends(get_current_user)):
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)
        items = await get_pack_items(conn, pack_id)

    result = dict(pack_row)
    result["items"] = [dict(i) for i in items]
    return JSONResponse(result)


@router.put("/api/packs/{pack_id}")
async def api_update_pack(pack_id: int, request: Request, current_user: User = Depends(get_current_user)):
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection() as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

        body = await request.json()
        fields = {}

        if "name" in body:
            name = (body["name"] or "").strip()
            if not name:
                raise HTTPException(status_code=400, detail="Pack name is required")
            fields["name"] = name

        if "slug" in body:
            slug = slugify(body["slug"] or body.get("name", ""))
            if not slug:
                raise HTTPException(status_code=400, detail="Invalid slug")
            # Check uniqueness (exclude current pack)
            cursor = await conn.execute(
                "SELECT id FROM PACKS WHERE slug = ? AND id != ?", (slug, pack_id)
            )
            if await cursor.fetchone():
                raise HTTPException(status_code=400, detail="A pack with this slug already exists")
            fields["slug"] = slug

        if "description" in body:
            fields["description"] = (body["description"] or "").strip()

        if "price" in body:
            try:
                price = float(body["price"])
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="Invalid price value")
            if price < 0 or price > MAX_PACK_PRICE:
                raise HTTPException(status_code=400, detail=f"Price must be between 0 and {MAX_PACK_PRICE}")
            fields["price"] = price

        if "is_paid" in body:
            fields["is_paid"] = bool(body["is_paid"])
            if not fields["is_paid"]:
                fields["price"] = 0.0

        # Resolve final is_paid/price for downstream validations
        final_is_paid = fields.get("is_paid", pack_row["is_paid"])
        final_price = fields.get("price", pack_row["price"])

        if final_is_paid and final_price < MIN_PACK_PAID_PRICE:
            raise HTTPException(status_code=400, detail=f"Minimum price for paid packs is ${MIN_PACK_PAID_PRICE:.2f}")

        if "tags" in body:
            fields["tags"] = _validate_tags(
                orjson.dumps(body["tags"]).decode("utf-8") if isinstance(body["tags"], list) else body["tags"]
            )

        # Compute initial_balance cap based on pack pricing
        if final_is_paid and final_price > 0:
            pricing_config = await get_pricing_config()
            ib_cap = round(final_price * (1 - pricing_config["commission"]), 2)
        else:
            ib_cap = MAX_FREE_INITIAL_BALANCE

        if "landing_reg_config" in body:
            lrc = body["landing_reg_config"]
            if isinstance(lrc, str):
                try:
                    lrc = orjson.loads(lrc)
                except Exception:
                    lrc = {}
            if not isinstance(lrc, dict):
                lrc = {}
            lrc = sanitize_landing_reg_config(lrc, max_initial_balance=ib_cap)
            lrc = await validate_shared_landing_config(lrc)
        else:
            lrc = None
            # Re-validate existing config if price/is_paid changed (cap may have tightened)
            if "is_paid" in fields or "price" in fields:
                existing_config = pack_row.get("landing_reg_config")
                if existing_config:
                    if isinstance(existing_config, str):
                        try:
                            existing_config = orjson.loads(existing_config)
                        except Exception:
                            existing_config = {}
                    if isinstance(existing_config, dict):
                        existing_ib = existing_config.get("initial_balance", 0)
                        if existing_ib and float(existing_ib) > ib_cap:
                            existing_config["initial_balance"] = ib_cap
                            lrc = existing_config

        if lrc is not None:
            lrc = await validate_shared_landing_config(lrc)
            # Validate user_pays requires creator to have positive balance
            if lrc.get("billing_mode") == "user_pays":
                creator_balance = await get_balance(pack_row["created_by_user_id"])
                if creator_balance <= 0:
                    raise HTTPException(
                        status_code=400,
                        detail="Pack creator needs a positive balance to enable 'user pays' mode"
                    )
            fields["landing_reg_config"] = orjson.dumps(lrc).decode("utf-8")

        if fields:
            await update_pack(conn, pack_id, **fields)

    # Rename landing directory if sanitized name changed
    if "name" in fields and sanitize_name(fields["name"]) != sanitize_name(pack_row["name"]):
        username = pack_row["created_by_username"]
        old_dir = _build_pack_filesystem_path(username, pack_id, pack_row["name"])
        new_dir = _build_pack_filesystem_path(username, pack_id, fields["name"])
        if old_dir.exists() and not new_dir.exists():
            old_dir.rename(new_dir)

            # Fix cover_image path: directory and filenames embed the sanitized name
            if pack_row.get("cover_image"):
                old_sanitized = sanitize_name(pack_row["name"])
                new_sanitized = sanitize_name(fields["name"])
                img_dir = new_dir / "static" / "img"
                if img_dir.exists():
                    for f in img_dir.iterdir():
                        if old_sanitized in f.name:
                            f.rename(img_dir / f.name.replace(old_sanitized, new_sanitized))
                new_cover = pack_row["cover_image"].replace(old_sanitized, new_sanitized)
                async with get_db_connection() as upd_conn:
                    await update_pack(upd_conn, pack_id, cover_image=new_cover)

    # Invalidate cache after update (public_id from the fetched pack_row)
    if pack_row["public_id"]:
        invalidate_pack_landing_cache(pack_row["public_id"])

    return JSONResponse({"message": "Pack updated"})


@router.delete("/api/packs/{pack_id}")
async def api_delete_pack(pack_id: int, current_user: User = Depends(get_current_user)):
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection() as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

        # Only allow deletion of draft packs or packs with no active access
        if pack_row["status"] != "draft":
            cursor = await conn.execute(
                f"""SELECT COUNT(*) FROM ENTITLEMENTS e
                    WHERE e.asset_type = 'pack' AND e.asset_id = ?
                      AND {active_entitlement_condition("e")}""",
                (pack_id,)
            )
            access_count = (await cursor.fetchone())[0]
            if access_count > 0:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot delete a pack with active users. Remove all access first."
                )

        # Invalidate cache before deletion
        if pack_row["public_id"]:
            invalidate_pack_landing_cache(pack_row["public_id"])

        await conn.execute("DELETE FROM WELCOME_MESSAGES WHERE entity_type = 'pack' AND entity_id = ?", (pack_id,))
        await delete_pack(conn, pack_id)

    return JSONResponse({"message": "Pack deleted"})


# ---------------------------------------------------------------------------
# Pack items API
# ---------------------------------------------------------------------------

@router.post("/api/packs/{pack_id}/items")
async def api_add_pack_item(pack_id: int, request: Request, current_user: User = Depends(get_current_user)):
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection() as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

        body = await request.json()
        prompt_id = body.get("prompt_id")
        if not prompt_id:
            raise HTTPException(status_code=400, detail="prompt_id is required")

        # Check item count limit
        if pack_row["item_count"] >= MAX_PACK_ITEMS:
            raise HTTPException(status_code=400, detail=f"Maximum {MAX_PACK_ITEMS} items per pack")

        # Verify prompt exists and allows packs
        cursor = await conn.execute(
            "SELECT id, allow_in_packs FROM PROMPTS WHERE id = ?", (prompt_id,)
        )
        prompt_row = await cursor.fetchone()
        if not prompt_row:
            raise HTTPException(status_code=404, detail="Prompt not found")
        if not prompt_row["allow_in_packs"]:
            raise HTTPException(status_code=400, detail="This prompt does not allow pack inclusion")

        # Check not already active in pack (filter disable_at consistently with get_available_prompts_for_pack)
        cursor = await conn.execute(
            "SELECT id FROM PACK_ITEMS WHERE pack_id = ? AND prompt_id = ? AND is_active = 1 AND (disable_at IS NULL OR disable_at > datetime('now'))",
            (pack_id, prompt_id),
        )
        if await cursor.fetchone():
            raise HTTPException(status_code=400, detail="Prompt already in this pack")

        # Remove any stale/expired row to avoid UNIQUE constraint violation on re-add
        await conn.execute(
            "DELETE FROM PACK_ITEMS WHERE pack_id = ? AND prompt_id = ? AND (is_active = 0 OR (disable_at IS NOT NULL AND disable_at <= datetime('now')))",
            (pack_id, prompt_id),
        )

        item_id = await add_pack_item(conn, pack_id, prompt_id)

    return JSONResponse({"id": item_id, "message": "Prompt added to pack"})


@router.delete("/api/packs/{pack_id}/items/{prompt_id}")
async def api_remove_pack_item(pack_id: int, prompt_id: int, current_user: User = Depends(get_current_user)):
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection() as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)
        await remove_pack_item(conn, pack_id, prompt_id)

    return JSONResponse({"message": "Prompt removed from pack"})


@router.put("/api/packs/{pack_id}/items/reorder")
async def api_reorder_pack_items(pack_id: int, request: Request, current_user: User = Depends(get_current_user)):
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection() as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

        body = await request.json()
        ordered_ids = body.get("prompt_ids", [])
        if not isinstance(ordered_ids, list):
            raise HTTPException(status_code=400, detail="prompt_ids must be a list")

        await reorder_pack_items(conn, pack_id, ordered_ids)

    return JSONResponse({"message": "Items reordered"})


@router.get("/api/packs/{pack_id}/available-prompts")
async def api_available_prompts(pack_id: int, search: str = "", current_user: User = Depends(get_current_user)):
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)
        prompts = await get_available_prompts_for_pack(conn, pack_id, search=search)

    return JSONResponse([dict(p) for p in prompts])


# ---------------------------------------------------------------------------
# Publish / Unpublish
# ---------------------------------------------------------------------------

@router.post("/api/packs/{pack_id}/publish")
async def api_publish_pack(pack_id: int, current_user: User = Depends(get_current_user)):
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection() as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

        if pack_row["item_count"] < MIN_PACK_ITEMS_TO_PUBLISH:
            raise HTTPException(
                status_code=400,
                detail=f"Pack needs at least {MIN_PACK_ITEMS_TO_PUBLISH} prompts to publish"
            )

        if not pack_row["name"] or not pack_row["name"].strip():
            raise HTTPException(status_code=400, detail="Pack name is required to publish")

        if not pack_row["description"] or not pack_row["description"].strip():
            raise HTTPException(status_code=400, detail="Pack description is required to publish")

        # Re-validate initial_balance cap at publish time
        if pack_row.get("is_paid") and pack_row.get("price", 0) > 0:
            pricing_config = await get_pricing_config()
            ib_cap = round(pack_row["price"] * (1 - pricing_config["commission"]), 2)
        else:
            ib_cap = MAX_FREE_INITIAL_BALANCE

        existing_lrc = pack_row.get("landing_reg_config")
        if existing_lrc:
            if isinstance(existing_lrc, str):
                try:
                    existing_lrc = orjson.loads(existing_lrc)
                except Exception:
                    existing_lrc = {}
            if isinstance(existing_lrc, dict):
                existing_lrc = await validate_shared_landing_config(existing_lrc)
                current_ib = float(existing_lrc.get("initial_balance", 0))
                if current_ib > ib_cap:
                    logger.warning(
                        "Pack %s publish: clamping initial_balance from %.2f to %.2f",
                        pack_id, current_ib, ib_cap
                    )
                    existing_lrc["initial_balance"] = ib_cap
                    await conn.execute(
                        "UPDATE PACKS SET landing_reg_config = ? WHERE id = ?",
                        (orjson.dumps(existing_lrc).decode("utf-8"), pack_id)
                    )
                    await conn.commit()

    # LLM moderation check before publishing
    if await is_security_guard_enabled():
        # Moderation cost is charged to the pack creator, not the acting user
        creator_id = pack_row["created_by_user_id"]

        # Check creator has enough balance to cover moderation cost
        creator_balance = await get_balance(creator_id)
        if creator_balance < MODERATION_MIN_BALANCE:
            raise HTTPException(
                status_code=400,
                detail="Insufficient balance for content moderation",
            )

        tags_str = ""
        if pack_row["tags"]:
            try:
                tags_list = json_mod.loads(pack_row["tags"]) if isinstance(pack_row["tags"], str) else pack_row["tags"]
                tags_str = " ".join(tags_list)
            except Exception:
                pass

        content_to_check = f"{pack_row['name']} {pack_row['description'] or ''} {tags_str}"
        security_result = await check_security(content_to_check)

        # Fail-closed: if moderation service errored, do NOT publish
        if not security_result.get("checked"):
            logger.error(
                "Moderation service unavailable for pack %s (creator %s): %s",
                pack_id, creator_id, security_result.get("reason", "unknown"),
            )
            raise HTTPException(
                status_code=503,
                detail="Content moderation service is temporarily unavailable. Please try again later.",
            )

        # Check ran successfully - charge cost to the pack creator
        deducted = await deduct_balance(creator_id, MODERATION_COST_FIXED)
        if not deducted:
            raise HTTPException(
                status_code=402,
                detail="Failed to charge moderation cost. Please check your balance and try again.",
            )
        logger.info(
            "Moderation cost $%.4f charged to creator %s (pack %s)",
            MODERATION_COST_FIXED, creator_id, pack_id,
        )

        if not security_result.get("allowed"):
            # Rejected by moderation - update status and rejection_reason directly
            async with get_db_connection() as conn:
                await conn.execute(
                    "UPDATE PACKS SET status = 'rejected', rejection_reason = ? WHERE id = ?",
                    (security_result.get("reason", "Content rejected by moderation"), pack_id),
                )
                await conn.commit()

            # Invalidate cache since status changed
            if pack_row["public_id"]:
                invalidate_pack_landing_cache(pack_row["public_id"])

            raise HTTPException(
                status_code=403,
                detail={
                    "message": "Pack rejected by content moderation",
                    "reason": security_result.get("reason", ""),
                    "threat_level": security_result.get("threat_level", "unknown"),
                },
            )

    # Passed moderation (or guard not enabled) - publish the pack
    async with get_db_connection() as conn:
        await update_pack(conn, pack_id, status="published", is_public=True)
        # Clear any previous rejection reason on successful publish
        await conn.execute(
            "UPDATE PACKS SET rejection_reason = NULL WHERE id = ?", (pack_id,)
        )
        await conn.commit()

    # Invalidate cache after status change
    if pack_row["public_id"]:
        invalidate_pack_landing_cache(pack_row["public_id"])

    return JSONResponse({"message": "Pack published"})


@router.post("/api/packs/{pack_id}/unpublish")
async def api_unpublish_pack(pack_id: int, current_user: User = Depends(get_current_user)):
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection() as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)
        await update_pack(conn, pack_id, status="draft", is_public=False)

    # Invalidate cache after status change
    if pack_row["public_id"]:
        invalidate_pack_landing_cache(pack_row["public_id"])

    return JSONResponse({"message": "Pack unpublished"})


# ---------------------------------------------------------------------------
# Cover Image Upload / Delete
# ---------------------------------------------------------------------------

_ALLOWED_COVER_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}


def _save_pack_cover_variants(
    content: bytes,
    *,
    img_dir: Path,
    pack_id: int,
    sanitized_name: str,
    old_cover_image: str | None,
) -> None:
    """Validate and persist all cover variants in one worker-thread block."""
    try:
        image = PilImage.open(io.BytesIO(content))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid image file") from exc

    with image:
        if image.format not in _ALLOWED_COVER_FORMATS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported image format: {image.format}. "
                    "Allowed: JPEG, PNG, WEBP, GIF"
                ),
            )

        width, height = image.size
        if width * height > MAX_IMAGE_PIXELS:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Image dimensions too large. Maximum is "
                    f"{MAX_IMAGE_PIXELS:,} pixels"
                ),
            )

        image.load()

        if old_cover_image:
            for label in ("240", "512", "fullsize"):
                old_path = DATA_DIR / f"{old_cover_image}_{label}.webp"
                if old_path.is_file():
                    old_path.unlink()

        img_dir.mkdir(parents=True, exist_ok=True)
        fullsize_width = min(image.width, MAX_COVER_FULLSIZE_WIDTH)
        for size, label in zip(
            (240, 512, fullsize_width),
            ("240", "512", "fullsize"),
        ):
            resized = resize_image_cover(image.copy(), size)
            file_path = img_dir / f"{pack_id}_{sanitized_name}_{label}.webp"
            resized.save(str(file_path), "WEBP")


@router.post("/api/packs/{pack_id}/cover-image")
async def api_upload_cover_image(
    pack_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Upload a cover image for a pack (240, 512, fullsize at 16:9)."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    # Read file content
    content = await file.read()

    # Validate file size
    if len(content) > MAX_IMAGE_UPLOAD_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"Image too large. Maximum size is {MAX_IMAGE_UPLOAD_SIZE // (1024 * 1024)}MB",
        )

    # Build directory path
    pack_dir = _build_pack_filesystem_path(
        pack_row["username"] if "username" in pack_row.keys() else None,
        pack_id,
        pack_row["name"],
    )
    # If username is not directly on pack_row, fetch it
    if pack_dir is None or "username" not in pack_row.keys():
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT username FROM USERS WHERE id = ?",
                (pack_row["created_by_user_id"],),
            )
            user_row = await cursor.fetchone()
            if not user_row:
                raise HTTPException(status_code=404, detail="Pack owner not found")
            pack_dir = _build_pack_filesystem_path(user_row[0], pack_id, pack_row["name"])

    img_dir = pack_dir / "static" / "img"
    sanitized = sanitize_name(pack_row["name"])

    await asyncio.to_thread(
        _save_pack_cover_variants,
        content,
        img_dir=img_dir,
        pack_id=pack_id,
        sanitized_name=sanitized,
        old_cover_image=pack_row["cover_image"],
    )

    # Build base URL (without size suffix or extension)
    hash_prefix1, hash_prefix2, user_hash = generate_user_hash(
        pack_row["username"] if "username" in pack_row.keys() else user_row[0]
    )
    padded_id = f"{pack_id:07d}"
    base_image_url = (
        f"users/{hash_prefix1}/{hash_prefix2}/{user_hash}"
        f"/packs/{padded_id[:3]}/{padded_id[3:]}_{sanitized}"
        f"/static/img/{pack_id}_{sanitized}"
    )

    # Update database
    async with get_db_connection() as conn:
        await update_pack(conn, pack_id, cover_image=base_image_url)

    # Invalidate landing cache
    if pack_row["public_id"]:
        invalidate_pack_landing_cache(pack_row["public_id"])

    # Return the servable URL (not the internal filesystem path)
    cover_url = f"/api/packs/{pack_id}/cover/512"
    return JSONResponse({"cover_image": cover_url, "message": "Cover image uploaded"})


@router.get("/api/packs/{pack_id}/cover/{size}")
async def serve_pack_cover(
    pack_id: int,
    size: str,
    current_user: Optional[User] = Depends(get_current_user),
):
    """Serve a pack cover image. Public access for published packs,
    admin/user owner access for drafts."""
    if size not in ("240", "512", "fullsize"):
        raise HTTPException(status_code=400, detail="Invalid size. Use 240, 512, or fullsize")

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)

    if not pack_row or not pack_row["cover_image"]:
        raise HTTPException(status_code=404, detail="No cover image")

    is_public_published = pack_row["status"] == "published" and pack_row["is_public"]
    # Allow admin (any pack) or owner to see covers even when public serving is disabled.
    is_authorized = False
    if current_user is not None:
        is_owner = pack_row["created_by_user_id"] == current_user.id
        is_authorized = (await current_user.is_admin) or is_owner

    if not is_authorized:
        if not is_public_published or not marketplace_public_landings_enabled():
            raise HTTPException(status_code=404, detail="No cover image")

    # cover_image stores the internal base path; append size + extension
    image_path = DATA_DIR / f"{pack_row['cover_image']}_{size}.webp"

    if not image_path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(
        image_path,
        media_type="image/webp",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.delete("/api/packs/{pack_id}/cover-image")
async def api_delete_cover_image(
    pack_id: int,
    current_user: User = Depends(get_current_user),
):
    """Delete all cover image files for a pack and clear the DB field."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    if not pack_row["cover_image"]:
        return JSONResponse({"message": "No cover image to delete"})

    # Use the stored cover_image path (kept in sync by rename handler)
    for label in ("240", "512", "fullsize"):
        file_path = DATA_DIR / f"{pack_row['cover_image']}_{label}.webp"
        if file_path.is_file():
            os.remove(file_path)

    # Clear database field
    async with get_db_connection() as conn:
        await update_pack(conn, pack_id, cover_image=None)

    # Invalidate landing cache
    if pack_row["public_id"]:
        invalidate_pack_landing_cache(pack_row["public_id"])

    return JSONResponse({"message": "Cover image deleted"})


# ---------------------------------------------------------------------------
# Pack Landing Page Management (Phase 2C)
# ---------------------------------------------------------------------------
# Endpoints for managing custom landing pages for packs:
#   - Admin config page
#   - AI Wizard (generate / modify / status / active-job)
#   - Files (list / delete all)
#   - Pages CRUD (create / delete / edit / save)
#   - Components CRUD (list / edit / save / create / delete)
#   - Images CRUD (list / upload / delete)
# ---------------------------------------------------------------------------

ALLOWED_COMPONENT_TYPES = {"html", "css", "js"}
ALLOWED_IMAGE_EXTENSIONS = {"webp", "jpg", "jpeg", "png", "gif", "ico"}


def _secure_filename(filename: str) -> str:
    """Sanitize filename to prevent path traversal and invalid characters."""
    filename = normalize("NFKD", filename).encode("ASCII", "ignore").decode("ASCII")
    filename = filename.replace("\\", "/")
    filename = filename.split("/")[-1]
    while ".." in filename:
        filename = filename.replace("..", "")
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", filename)
    filename = filename.lstrip(".").strip().replace(" ", "_")
    filename = filename[:160]
    if not filename:
        filename = "unnamed_file"
    return filename


def _allowed_image_file(filename: str) -> bool:
    """Check if filename has an allowed image extension."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def _save_pack_landing_image(
    content: bytes,
    *,
    original_filename: str,
    requested_name: str,
    img_dir: Path,
) -> str:
    """Validate and persist one pack landing image off the event loop."""
    filename = _secure_filename(requested_name)
    ext = Path(original_filename).suffix.lower()
    ext_clean = ext.lstrip(".")
    if ext_clean not in ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"File extension {ext} not allowed")
    if not filename.lower().endswith(
        tuple("." + allowed_ext for allowed_ext in ALLOWED_IMAGE_EXTENSIONS)
    ):
        filename += ext

    img_dir.mkdir(parents=True, exist_ok=True)
    file_path = validate_path_within_directory(filename, img_dir)

    try:
        with PilImage.open(io.BytesIO(content)) as verified_image:
            verified_image.verify()
        with PilImage.open(io.BytesIO(content)) as decoded_image:
            width, height = decoded_image.size
            if width * height > MAX_IMAGE_PIXELS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Image {original_filename} dimensions too large. Maximum is "
                        f"{MAX_IMAGE_PIXELS:,} pixels"
                    ),
                )
            decoded_image.load()
            converted_image = (
                decoded_image.copy()
                if ext in {".jpg", ".jpeg", ".png"}
                else None
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid image file: {original_filename}",
        ) from exc

    if converted_image is not None:
        webp_path = file_path.with_suffix(".webp")
        converted_image.save(str(webp_path), "WEBP")
        return webp_path.name

    file_path.write_bytes(content)
    return file_path.name


async def _get_pack_dir_and_info(pack_id: int, pack_row) -> tuple:
    """
    Get pack directory path and owner username. Creates directory if needed.

    Returns:
        (pack_dir: Path, username: str)
    """
    username = pack_row.get("username") if hasattr(pack_row, "get") else (
        pack_row["username"] if "username" in pack_row.keys() else None
    )
    if not username:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT username FROM USERS WHERE id = ?",
                (pack_row["created_by_user_id"],),
            )
            user_row = await cursor.fetchone()
            if not user_row:
                raise HTTPException(status_code=404, detail="Pack owner not found")
            username = user_row[0]

    pack_dir = _build_pack_filesystem_path(username, pack_id, pack_row["name"])
    return pack_dir, username


async def _update_has_custom_landing(pack_id: int, pack_dir: Path, public_id: str = None):
    """Check if home.html exists and update the has_custom_landing flag."""
    has_custom = (pack_dir / "home.html").is_file()
    async with get_db_connection() as conn:
        await conn.execute(
            "UPDATE PACKS SET has_custom_landing = ? WHERE id = ?",
            (has_custom, pack_id),
        )
        await conn.commit()
    if public_id:
        invalidate_pack_landing_cache(public_id)


def _ensure_pack_directories(pack_dir: Path):
    """Ensure standard subdirectories exist for a pack landing page."""
    for sub in [
        pack_dir / "templates" / "components",
        pack_dir / "static" / "css",
        pack_dir / "static" / "js",
        pack_dir / "static" / "img",
    ]:
        os.makedirs(str(sub), exist_ok=True)


async def _build_pack_product_description(pack_row, pack_id: int) -> str:
    """Build a product description string including pack description and prompt names."""
    description = pack_row["description"] or ""
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """SELECT p.name FROM PACK_ITEMS pi
               JOIN PROMPTS p ON pi.prompt_id = p.id
               WHERE pi.pack_id = ? AND pi.is_active = 1
               AND (pi.disable_at IS NULL OR pi.disable_at > datetime('now'))
               ORDER BY pi.display_order""",
            (pack_id,),
        )
        prompt_names = [row[0] async for row in cursor]
    if prompt_names:
        description += "\n\nThis pack includes: " + ", ".join(prompt_names)
    return description


# ---- Admin page route for landing config ----

@router.get("/admin/packs/{pack_id}/landing", response_class=HTMLResponse)
async def admin_pack_landing_config(
    request: Request,
    pack_id: int,
    current_user: User = Depends(get_current_user),
):
    """Configuration page for Pack Landing Pages."""
    require_creator_tools_enabled()

    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request})

    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    pack_dir, username = await _get_pack_dir_and_info(pack_id, pack_row)

    # List existing pages (.html files in root)
    pages = []
    has_home_page = False
    if pack_dir.exists():
        for f in os.listdir(str(pack_dir)):
            if f.endswith(".html") and os.path.isfile(str(pack_dir / f)):
                page_name = f[:-5]
                is_home = page_name == "home"
                if is_home:
                    has_home_page = True
                pages.append({
                    "name": page_name,
                    "url_path": "/" if is_home else f"/{page_name}",
                    "is_home": is_home,
                })
    pages.sort(key=lambda p: (not p["is_home"], p["name"]))

    # List components (HTML, CSS, JS)
    components = {"html": [], "css": [], "js": []}
    components_dir = str(pack_dir / "templates" / "components")
    if os.path.exists(components_dir):
        for f in os.listdir(components_dir):
            if f.endswith(".html"):
                components["html"].append(f[:-5])
    css_dir = str(pack_dir / "static" / "css")
    if os.path.exists(css_dir):
        for f in os.listdir(css_dir):
            if f.endswith(".css"):
                components["css"].append(f[:-4])
    js_dir = str(pack_dir / "static" / "js")
    if os.path.exists(js_dir):
        for f in os.listdir(js_dir):
            if f.endswith(".js"):
                components["js"].append(f[:-3])

    # Build public URL
    public_id = pack_row["public_id"]
    slug = pack_row["slug"] or slugify(pack_row["name"])
    base_url = str(request.base_url).rstrip("/")
    public_url_path = f"/pack/{public_id}/{slug}/" if public_id else "#"
    public_url_full = f"{base_url}{public_url_path}" if public_id else "#"

    pack_dict = dict(pack_row)
    wizard_available, _ = is_claude_available()
    context = await get_template_context(request, current_user)
    context.update({
        "pack": pack_dict,
        "pages": pages,
        "components": components,
        "has_home_page": has_home_page,
        "public_url": public_url_full,
        "public_url_path": public_url_path,
        "wizard_available": wizard_available,
    })
    return templates.TemplateResponse("pack_landing_config.html", context)


# ---- AI Wizard: Generate ----

@router.post("/api/landing/pack/{pack_id}/ai/generate")
async def pack_ai_wizard_generate(
    request: Request,
    pack_id: int,
    current_user: User = Depends(get_current_user),
):
    """Start a background job to generate a landing page for a pack via AI Wizard."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    # The wizard is available only through a verified OS sandbox runner.
    claude_available, _ = is_claude_available()
    if not claude_available:
        raise HTTPException(
            status_code=503,
            detail="AI Wizard is disabled until a verified OS sandbox is configured",
        )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    description = (body.get("description") or "").strip()
    if not description or len(description) < 20:
        raise HTTPException(status_code=400, detail="Description must be at least 20 characters")

    style = body.get("style", "modern")
    if style not in ("modern", "minimalist", "corporate", "creative"):
        style = "modern"
    primary_color = body.get("primary_color", "#3B82F6")
    secondary_color = body.get("secondary_color", "#10B981")
    language = body.get("language", "es")
    if language not in ("es", "en"):
        language = "es"

    try:
        timeout_minutes = int(body.get("timeout_minutes", 5))
    except (ValueError, TypeError):
        timeout_minutes = 5
    timeout_minutes = max(1, min(60, timeout_minutes))
    timeout_seconds = timeout_minutes * 60

    await _require_wizard_security_check(
        description,
        label="pack landing wizard",
        pack_id=pack_id,
    )

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    # Check for active job
    existing_job = get_active_job_for_pack(pack_id)
    if existing_job:
        raise HTTPException(status_code=409, detail={
            "message": "A job is already running for this pack",
            "existing_task_id": existing_job["task_id"],
        })

    pack_dir, username = await _get_pack_dir_and_info(pack_id, pack_row)

    # Create directory if it doesn't exist
    if not pack_dir.exists():
        create_pack_directory(username, pack_id, pack_row["name"])

    # Build product context
    product_description = await _build_pack_product_description(pack_row, pack_id)

    # Build absolute landing URL for SEO meta tags (canonical, og:url, og:image, twitter:image)
    landing_url = ""
    primary_domain = os.getenv("PRIMARY_APP_DOMAIN", "")
    pack_public_id = pack_row["public_id"] if pack_row["public_id"] else ""
    if primary_domain and pack_public_id:
        pack_slug = pack_row["slug"] if pack_row["slug"] else slugify(pack_row["name"])
        landing_url = f"https://{primary_domain}/pack/{pack_public_id}/{pack_slug}/"

    params = {
        "description": description,
        "style": style,
        "primary_color": primary_color,
        "secondary_color": secondary_color,
        "language": language,
        "timeout": timeout_seconds,
        "product_name": pack_row["name"],
        "ai_system_prompt": "",
        "product_description": product_description,
        "landing_url": landing_url,
    }

    logger.info("Starting AI wizard job for pack %s, user %s, timeout=%ss", pack_id, current_user.id, timeout_seconds)
    result = start_job(
        prompt_id=0,
        job_type="generate",
        prompt_dir=str(pack_dir),
        params=params,
        timeout_seconds=timeout_seconds,
        pack_id=pack_id,
    )

    if result.get("success"):
        logger.info("AI wizard job started for pack %s: task_id=%s", pack_id, result["task_id"])
        return JSONResponse({
            "success": True,
            "message": "Job started",
            "task_id": result["task_id"],
            "status": result["status"],
        })

    logger.error("Failed to start AI wizard job for pack %s: %s", pack_id, result.get("error"))
    raise HTTPException(status_code=500, detail=result.get("error", "Failed to start job"))


# ---- AI Wizard: Modify ----

@router.post("/api/landing/pack/{pack_id}/ai/modify")
async def pack_ai_wizard_modify(
    request: Request,
    pack_id: int,
    current_user: User = Depends(get_current_user),
):
    """Start a background job to modify an existing pack landing page via AI Wizard."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    claude_available, _ = is_claude_available()
    if not claude_available:
        raise HTTPException(
            status_code=503,
            detail="AI Wizard is disabled until a verified OS sandbox is configured",
        )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    instructions = (body.get("instructions") or "").strip()
    if not instructions or len(instructions) < 10:
        raise HTTPException(status_code=400, detail="Instructions must be at least 10 characters")

    try:
        timeout_minutes = int(body.get("timeout_minutes", 5))
    except (ValueError, TypeError):
        timeout_minutes = 5
    timeout_minutes = max(1, min(60, timeout_minutes))
    timeout_seconds = timeout_minutes * 60

    await _require_wizard_security_check(
        instructions,
        label="pack landing modify",
        pack_id=pack_id,
    )

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    existing_job = get_active_job_for_pack(pack_id)
    if existing_job:
        raise HTTPException(status_code=409, detail={
            "message": "A job is already running for this pack",
            "existing_task_id": existing_job["task_id"],
        })

    pack_dir, username = await _get_pack_dir_and_info(pack_id, pack_row)

    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail="Pack directory not found")

    # Check if there are files to modify
    files = list_prompt_files(str(pack_dir))
    if files["total_count"] == 0:
        raise HTTPException(status_code=400, detail="No files to modify. Use 'Create new' instead.")

    product_description = await _build_pack_product_description(pack_row, pack_id)

    # Build absolute landing URL for SEO meta tags (canonical, og:url, og:image, twitter:image)
    landing_url = ""
    primary_domain = os.getenv("PRIMARY_APP_DOMAIN", "")
    pack_public_id = pack_row["public_id"] if pack_row["public_id"] else ""
    if primary_domain and pack_public_id:
        pack_slug = pack_row["slug"] if pack_row["slug"] else slugify(pack_row["name"])
        landing_url = f"https://{primary_domain}/pack/{pack_public_id}/{pack_slug}/"

    params = {
        "instructions": instructions,
        "timeout": timeout_seconds,
        "product_name": pack_row["name"],
        "ai_system_prompt": "",
        "product_description": product_description,
        "landing_url": landing_url,
    }

    logger.info("Starting modify wizard job for pack %s, user %s, timeout=%ss", pack_id, current_user.id, timeout_seconds)
    result = start_job(
        prompt_id=0,
        job_type="modify",
        prompt_dir=str(pack_dir),
        params=params,
        timeout_seconds=timeout_seconds,
        pack_id=pack_id,
    )

    if result.get("success"):
        logger.info("Modify wizard job started for pack %s: task_id=%s", pack_id, result["task_id"])
        return JSONResponse({
            "success": True,
            "message": "Job started",
            "task_id": result["task_id"],
            "status": result["status"],
        })

    logger.error("Failed to start modify wizard job for pack %s: %s", pack_id, result.get("error"))
    raise HTTPException(status_code=500, detail=result.get("error", "Failed to start job"))


# ---- AI Wizard: Status ----

@router.get("/api/landing/pack/{pack_id}/ai/status/{task_id}")
async def pack_ai_wizard_status(
    pack_id: int,
    task_id: str,
    current_user: User = Depends(get_current_user),
):
    """Get the status of a pack landing page generation/modification job."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    job = get_job(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.get("pack_id") != pack_id:
        raise HTTPException(status_code=403, detail="Job does not belong to this pack")

    response = {
        "success": True,
        "task_id": job["task_id"],
        "status": job["status"],
        "type": job.get("type"),
        "started_at": job.get("started_at"),
        "updated_at": job.get("updated_at"),
        "completed_at": job.get("completed_at"),
    }

    if job["status"] == "completed":
        response["files_created"] = job.get("files_created", [])
        # Update has_custom_landing based on whether home.html exists now
        pack_dir, _ = await _get_pack_dir_and_info(pack_id, pack_row)
        await _update_has_custom_landing(pack_id, pack_dir, pack_row.get("public_id"))
    elif job["status"] in ("failed", "timeout"):
        response["error"] = job.get("error")

    return JSONResponse(response)


# ---- AI Wizard: Active Job ----

@router.get("/api/landing/pack/{pack_id}/ai/active-job")
async def pack_ai_wizard_active_job(
    pack_id: int,
    current_user: User = Depends(get_current_user),
):
    """Check if there's an active (pending/running) job for this pack."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    job = get_active_job_for_pack(pack_id)
    if job:
        return JSONResponse({
            "success": True,
            "has_active_job": True,
            "task_id": job["task_id"],
            "status": job["status"],
            "type": job.get("type"),
        })
    return JSONResponse({"success": True, "has_active_job": False})


# ============= Welcome Page Wizard Endpoints (Packs) =============

# ---- Welcome Wizard: Generate ----

@router.post("/api/welcome/pack/{pack_id}/ai/generate")
async def pack_welcome_wizard_generate(
    request: Request,
    pack_id: int,
    current_user: User = Depends(get_current_user),
):
    """Start a background job to generate a welcome page for a pack via AI Wizard."""
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    # The wizard is available only through a verified OS sandbox runner.
    claude_available, _ = is_claude_available()
    if not claude_available:
        raise HTTPException(
            status_code=503,
            detail="AI Wizard is disabled until a verified OS sandbox is configured",
        )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    description = (body.get("description") or "").strip()
    if not description or len(description) < 20:
        raise HTTPException(status_code=400, detail="Description must be at least 20 characters")

    style = body.get("style", "modern")
    if style not in ("modern", "minimalist", "corporate", "creative"):
        style = "modern"
    primary_color = body.get("primary_color", "#3B82F6")
    secondary_color = body.get("secondary_color", "#10B981")
    language = body.get("language", "es")
    if language not in ("es", "en"):
        language = "es"

    try:
        timeout_minutes = int(body.get("timeout_minutes", 5))
    except (ValueError, TypeError):
        timeout_minutes = 5
    timeout_minutes = max(1, min(60, timeout_minutes))
    timeout_seconds = timeout_minutes * 60

    await _require_wizard_security_check(
        description,
        label="pack welcome wizard",
        pack_id=pack_id,
    )

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    # Check for active welcome job
    existing_job = get_active_welcome_job_for_pack(pack_id)
    if existing_job:
        raise HTTPException(status_code=409, detail={
            "message": "A welcome job is already running for this pack",
            "existing_task_id": existing_job["task_id"],
        })

    pack_dir, username = await _get_pack_dir_and_info(pack_id, pack_row)

    # Create directory if it doesn't exist
    if not pack_dir.exists():
        create_pack_directory(username, pack_id, pack_row["name"])

    # Build product context
    product_description = await _build_pack_product_description(pack_row, pack_id)

    params = {
        "description": description,
        "style": style,
        "primary_color": primary_color,
        "secondary_color": secondary_color,
        "language": language,
        "timeout": timeout_seconds,
        "product_name": pack_row["name"],
        "ai_system_prompt": "",
        "product_description": product_description,
        "chat_url": "/chat",
        "avatar_path": "",
    }

    logger.info("Starting welcome wizard job for pack %s, user %s, timeout=%ss", pack_id, current_user.id, timeout_seconds)
    result = start_job(
        prompt_id=0,
        job_type="generate",
        prompt_dir=str(pack_dir),
        params=params,
        timeout_seconds=timeout_seconds,
        pack_id=pack_id,
        target="welcome",
    )

    if result.get("success"):
        logger.info("Welcome wizard job started for pack %s: task_id=%s", pack_id, result["task_id"])
        return JSONResponse({
            "success": True,
            "message": "Job started",
            "task_id": result["task_id"],
            "status": result["status"],
        })

    logger.error("Failed to start welcome wizard job for pack %s: %s", pack_id, result.get("error"))
    raise HTTPException(status_code=500, detail=result.get("error", "Failed to start job"))


# ---- Welcome Wizard: Modify ----

@router.post("/api/welcome/pack/{pack_id}/ai/modify")
async def pack_welcome_wizard_modify(
    request: Request,
    pack_id: int,
    current_user: User = Depends(get_current_user),
):
    """Start a background job to modify an existing pack welcome page via AI Wizard."""
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    claude_available, _ = is_claude_available()
    if not claude_available:
        raise HTTPException(
            status_code=503,
            detail="AI Wizard is disabled until a verified OS sandbox is configured",
        )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    instructions = (body.get("instructions") or "").strip()
    if not instructions or len(instructions) < 10:
        raise HTTPException(status_code=400, detail="Instructions must be at least 10 characters")

    try:
        timeout_minutes = int(body.get("timeout_minutes", 5))
    except (ValueError, TypeError):
        timeout_minutes = 5
    timeout_minutes = max(1, min(60, timeout_minutes))
    timeout_seconds = timeout_minutes * 60

    await _require_wizard_security_check(
        instructions,
        label="pack welcome modify",
        pack_id=pack_id,
    )

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    existing_job = get_active_welcome_job_for_pack(pack_id)
    if existing_job:
        raise HTTPException(status_code=409, detail={
            "message": "A welcome job is already running for this pack",
            "existing_task_id": existing_job["task_id"],
        })

    pack_dir, username = await _get_pack_dir_and_info(pack_id, pack_row)

    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail="Pack directory not found")

    # Check if there are welcome files to modify
    files = list_welcome_files(str(pack_dir))
    if files["total_count"] == 0:
        raise HTTPException(status_code=400, detail="No welcome files to modify. Use 'Create new' instead.")

    product_description = await _build_pack_product_description(pack_row, pack_id)

    params = {
        "instructions": instructions,
        "timeout": timeout_seconds,
        "product_name": pack_row["name"],
        "ai_system_prompt": "",
        "product_description": product_description,
        "chat_url": "/chat",
        "avatar_path": "",
    }

    logger.info("Starting welcome modify wizard job for pack %s, user %s, timeout=%ss", pack_id, current_user.id, timeout_seconds)
    result = start_job(
        prompt_id=0,
        job_type="modify",
        prompt_dir=str(pack_dir),
        params=params,
        timeout_seconds=timeout_seconds,
        pack_id=pack_id,
        target="welcome",
    )

    if result.get("success"):
        logger.info("Welcome modify wizard job started for pack %s: task_id=%s", pack_id, result["task_id"])
        return JSONResponse({
            "success": True,
            "message": "Job started",
            "task_id": result["task_id"],
            "status": result["status"],
        })

    logger.error("Failed to start welcome modify wizard job for pack %s: %s", pack_id, result.get("error"))
    raise HTTPException(status_code=500, detail=result.get("error", "Failed to start job"))


# ---- Welcome Wizard: Status ----

@router.get("/api/welcome/pack/{pack_id}/ai/status/{task_id}")
async def pack_welcome_wizard_status(
    pack_id: int,
    task_id: str,
    current_user: User = Depends(get_current_user),
):
    """Get the status of a pack welcome page generation/modification job."""
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    job = get_job(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.get("pack_id") != pack_id:
        raise HTTPException(status_code=403, detail="Job does not belong to this pack")

    response = {
        "success": True,
        "task_id": job["task_id"],
        "status": job["status"],
        "type": job.get("type"),
        "started_at": job.get("started_at"),
        "updated_at": job.get("updated_at"),
        "completed_at": job.get("completed_at"),
    }

    if job["status"] == "completed":
        response["files_created"] = job.get("files_created", [])
        # Update has_welcome_page flag in PACKS table
        try:
            async with get_db_connection() as db:
                await db.execute("UPDATE PACKS SET has_welcome_page = 1 WHERE id = ?", (pack_id,))
                await db.commit()
        except Exception as e:
            # Column may not exist yet until migration runs
            logger.warning("Could not update has_welcome_page for pack %s: %s", pack_id, e)
    elif job["status"] in ("failed", "timeout"):
        response["error"] = job.get("error")

    return JSONResponse(response)


# ---- Welcome Wizard: Active Job ----

@router.get("/api/welcome/pack/{pack_id}/ai/active-job")
async def pack_welcome_wizard_active_job(
    pack_id: int,
    current_user: User = Depends(get_current_user),
):
    """Check if there's an active (pending/running) welcome job for this pack."""
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    job = get_active_welcome_job_for_pack(pack_id)
    if job:
        return JSONResponse({
            "success": True,
            "has_active_job": True,
            "task_id": job["task_id"],
            "status": job["status"],
            "type": job.get("type"),
        })
    return JSONResponse({"success": True, "has_active_job": False})


# ---- Welcome Files: List ----

@router.get("/api/welcome/pack/{pack_id}/files")
async def pack_welcome_list_files(
    pack_id: int,
    current_user: User = Depends(get_current_user),
):
    """List all files in the pack's welcome page directory."""
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    pack_dir, _ = await _get_pack_dir_and_info(pack_id, pack_row)

    if not pack_dir.exists():
        return JSONResponse({
            "success": True,
            "files": {"pages": [], "css": [], "js": [], "images": [], "other": [], "total_count": 0},
        })

    files = list_welcome_files(str(pack_dir))
    return JSONResponse({"success": True, "files": files})


# ---- Welcome Files: Delete All ----

@router.delete("/api/welcome/pack/{pack_id}/files")
async def pack_welcome_delete_all_files(
    pack_id: int,
    current_user: User = Depends(get_current_user),
):
    """Delete all welcome page files for a pack (preserves images)."""
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    pack_dir, _ = await _get_pack_dir_and_info(pack_id, pack_row)

    if not pack_dir.exists():
        return JSONResponse({"success": True, "message": "No files to delete", "deleted_count": 0})

    logger.info("Deleting welcome files for pack %s, user %s", pack_id, current_user.id)
    result = delete_all_welcome_files(str(pack_dir), keep_images=True)

    if result["success"]:
        try:
            async with get_db_connection() as db:
                await db.execute("UPDATE PACKS SET has_welcome_page = 0 WHERE id = ?", (pack_id,))
                await db.commit()
        except Exception as e:
            logger.warning("Could not update has_welcome_page for pack %s: %s", pack_id, e)
        return JSONResponse({
            "success": True,
            "message": result.get("message", "Files deleted"),
            "deleted_count": result.get("deleted_count", 0),
        })

    raise HTTPException(status_code=500, detail=result.get("error", "Failed to delete files"))


# ---- Files: List ----

@router.get("/api/landing/pack/{pack_id}/files")
async def pack_landing_list_files(
    pack_id: int,
    current_user: User = Depends(get_current_user),
):
    """List all files in the pack's landing page directory."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    pack_dir, _ = await _get_pack_dir_and_info(pack_id, pack_row)

    if not pack_dir.exists():
        return JSONResponse({
            "success": True,
            "files": {"pages": [], "css": [], "js": [], "images": [], "other": [], "total_count": 0},
        })

    files = list_prompt_files(str(pack_dir))
    return JSONResponse({"success": True, "files": files})


# ---- Files: Delete All ----

@router.delete("/api/landing/pack/{pack_id}/files")
async def pack_landing_delete_all_files(
    pack_id: int,
    current_user: User = Depends(get_current_user),
):
    """Delete all landing page files for a pack (preserves images)."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    pack_dir, _ = await _get_pack_dir_and_info(pack_id, pack_row)

    if not pack_dir.exists():
        return JSONResponse({"success": True, "message": "No files to delete", "deleted_count": 0})

    logger.info("Deleting landing files for pack %s, user %s", pack_id, current_user.id)
    result = delete_all_landing_files(str(pack_dir), keep_images=True)

    # Update has_custom_landing to False since we just deleted everything
    await _update_has_custom_landing(pack_id, pack_dir, pack_row.get("public_id"))

    if result["success"]:
        return JSONResponse({
            "success": True,
            "message": result.get("message", "Files deleted"),
            "deleted_count": result.get("deleted_count", 0),
        })

    raise HTTPException(status_code=500, detail=result.get("error", "Failed to delete files"))


# ---- Pages: Create ----

@router.post("/api/landing/pack/{pack_id}/pages")
async def pack_landing_create_page(
    request: Request,
    pack_id: int,
    current_user: User = Depends(get_current_user),
):
    """Create a new HTML page in the pack landing directory."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    page_name = (body.get("page_name") or "").strip().lower()
    if not page_name or not re.match(r"^[a-zA-Z0-9_-]+$", page_name):
        raise HTTPException(status_code=400, detail="Invalid page name")

    pack_dir, username = await _get_pack_dir_and_info(pack_id, pack_row)

    # Ensure directory exists
    if not pack_dir.exists():
        create_pack_directory(username, pack_id, pack_row["name"])

    page_path = pack_dir / f"{page_name}.html"
    if page_path.exists():
        raise HTTPException(status_code=400, detail="Page already exists")

    # Create with basic template
    page_path.write_text(
        f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{page_name.capitalize()}</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-50 min-h-screen">
    <div class="container mx-auto px-4 py-8">
        <h1 class="text-3xl font-bold text-gray-800">{page_name.capitalize()}</h1>
        <p class="mt-4 text-gray-600">Edit this page to add your content.</p>
    </div>
</body>
</html>""",
        encoding="utf-8",
    )

    # Update has_custom_landing if we just created home
    if page_name == "home":
        await _update_has_custom_landing(pack_id, pack_dir, pack_row.get("public_id"))

    return JSONResponse({"success": True, "message": f"Page '{page_name}' created successfully"})


# ---- Pages: Delete ----

@router.delete("/api/landing/pack/{pack_id}/pages/{page_name}")
async def pack_landing_delete_page(
    pack_id: int,
    page_name: str,
    current_user: User = Depends(get_current_user),
):
    """Delete an HTML page from the pack landing directory."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    if not page_name or not re.match(r"^[a-zA-Z0-9_-]+$", page_name):
        raise HTTPException(status_code=400, detail="Invalid page name")

    if page_name.lower() == "home":
        raise HTTPException(status_code=400, detail="Cannot delete the home page")

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    pack_dir, _ = await _get_pack_dir_and_info(pack_id, pack_row)
    page_path = pack_dir / f"{page_name}.html"

    if not page_path.is_file():
        raise HTTPException(status_code=404, detail="Page not found")

    page_path.unlink()
    return JSONResponse({"success": True, "message": f"Page '{page_name}' deleted successfully"})


# ---- Pages: Edit (HTML editor page) ----

@router.get("/landing/pack/{pack_id}/pages/{section}/edit", response_class=HTMLResponse)
async def pack_landing_edit_page(
    request: Request,
    pack_id: int,
    section: str,
    current_user: User = Depends(get_current_user),
):
    """Render the CodeMirror editor for a pack landing page."""
    require_creator_tools_enabled()

    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request})

    if not re.match(r"^[a-zA-Z0-9_-]+$", section):
        raise HTTPException(status_code=400, detail="Invalid section name")

    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    pack_dir, username = await _get_pack_dir_and_info(pack_id, pack_row)

    # Ensure directory exists
    if not pack_dir.exists():
        create_pack_directory(username, pack_id, pack_row["name"])

    # Validate path is within pack directory
    validated_path = validate_path_within_directory(f"{section}.html", pack_dir)
    file_path = str(validated_path)

    # Create the folder structure if it doesn't exist
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    # If file doesn't exist, create with default content
    if not os.path.exists(file_path):
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(f"<h1>Welcome to the {section} page</h1>")

    with open(file_path, "r", encoding="utf-8") as f:
        section_content = f.read()

    # Build prompt_info-like dict for template compatibility
    pack_info = {"name": pack_row["name"], "created_by_username": username}

    # Build pack public URL for TEST button
    public_id = pack_row.get("public_id")
    slug = pack_row.get("slug") or slugify(pack_row["name"])
    base_url = str(request.base_url).rstrip("/")
    pack_public_url = f"{base_url}/pack/{public_id}/{slug}/" if public_id else ""

    context = await get_template_context(request, current_user)
    context.update({
        "content": section_content,
        "prompt_id": pack_id,  # Template compatibility
        "section": section,
        "prompt_info": pack_info,
        "is_pack": True,
        "pack_public_url": pack_public_url,
    })
    return templates.TemplateResponse("web/web_edit.html", context)


# ---- Pages: Save ----

@router.put("/api/landing/pack/{pack_id}/pages/{section}")
async def pack_landing_save_page(
    request: Request,
    pack_id: int,
    section: str,
    encodedContent: str = Form(...),
    current_user: User = Depends(get_current_user),
):
    """Save a pack landing page from the CodeMirror editor (base64-encoded content)."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if not re.match(r"^[a-zA-Z0-9_-]+$", section):
        raise HTTPException(status_code=400, detail="Invalid section name")

    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    pack_dir, username = await _get_pack_dir_and_info(pack_id, pack_row)

    # Ensure directory exists
    if not pack_dir.exists():
        create_pack_directory(username, pack_id, pack_row["name"])

    # Decode base64 content
    content = base64.b64decode(encodedContent).decode("utf-8")
    content = re.sub(r"\n\s*\n", "\n", content.strip())
    content = re.sub(r"\r\n", "\n", content)

    # Fix SEO metadata with absolute URLs when saving the home page
    if section.lower() == "home":
        primary_domain = os.getenv("PRIMARY_APP_DOMAIN", "")
        public_id = pack_row.get("public_id")
        if primary_domain and public_id:
            pack_slug = pack_row.get("slug") or slugify(pack_row["name"])
            canonical = f"https://{primary_domain}/pack/{public_id}/{pack_slug}/"
            content = fix_landing_seo_tags(content, canonical, canonical)

    # Validate path
    validated_path = validate_path_within_directory(f"{section}.html", pack_dir)
    os.makedirs(os.path.dirname(str(validated_path)), exist_ok=True)

    with open(str(validated_path), "w", encoding="utf-8") as f:
        f.write(content)

    # Update has_custom_landing if section is home
    if section.lower() == "home":
        await _update_has_custom_landing(pack_id, pack_dir, pack_row.get("public_id"))

    return JSONResponse({"success": True, "message": "Changes saved successfully"})


# ---- Components: List page ----

@router.get("/landing/pack/{pack_id}/components", response_class=HTMLResponse)
async def pack_landing_list_components(
    request: Request,
    pack_id: int,
    current_user: User = Depends(get_current_user),
):
    """Render the components list page for a pack."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    pack_dir, _ = await _get_pack_dir_and_info(pack_id, pack_row)
    _ensure_pack_directories(pack_dir)

    def list_files(directory, extension):
        if os.path.exists(directory):
            return [f[: -len(extension)] for f in os.listdir(directory) if f.endswith(extension)]
        return []

    components_by_type = {
        "html": list_files(str(pack_dir / "templates" / "components"), ".html"),
        "css": list_files(str(pack_dir / "static" / "css"), ".css"),
        "js": list_files(str(pack_dir / "static" / "js"), ".js"),
    }

    context = await get_template_context(request, current_user)
    context.update({
        "components_by_type": components_by_type,
        "prompt_id": pack_id,  # Template compatibility
        "prompt_name": pack_row["name"],
        "title": f"Components for {pack_row['name']}",
        "is_pack": True,
    })
    return templates.TemplateResponse("web/components_list.html", context)


# ---- Components: Edit page ----

@router.get("/landing/pack/{pack_id}/components/{component_type}/{component_name}/edit", response_class=HTMLResponse)
async def pack_landing_edit_component(
    request: Request,
    pack_id: int,
    component_type: str,
    component_name: str,
    current_user: User = Depends(get_current_user),
):
    """Render the editor for a pack landing component."""
    require_creator_tools_enabled()

    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request})

    if component_type not in ALLOWED_COMPONENT_TYPES:
        raise HTTPException(status_code=400, detail="Invalid component type")

    component_name = _secure_filename(component_name)

    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    pack_dir, username = await _get_pack_dir_and_info(pack_id, pack_row)
    base_dir = pack_dir

    if component_type == "html":
        target_dir = base_dir / "templates" / "components"
        filename = f"{component_name}.html"
    elif component_type == "css":
        target_dir = base_dir / "static" / "css"
        filename = f"{component_name}.css"
    elif component_type == "js":
        target_dir = base_dir / "static" / "js"
        filename = f"{component_name}.js"

    validated_path = validate_path_within_directory(filename, target_dir)

    if not validated_path.exists():
        raise HTTPException(status_code=404, detail="Component not found")

    with open(str(validated_path), "r", encoding="utf-8") as f:
        component_content = f.read()

    context = await get_template_context(request, current_user)
    context.update({
        "content": component_content,
        "component_name": component_name,
        "component_type": component_type,
        "prompt_id": pack_id,  # Template compatibility
        "prompt_name": pack_row["name"],
        "title": f"Edit {component_type.upper()} Component: {component_name} for {pack_row['name']}",
        "is_pack": True,
    })
    return templates.TemplateResponse("web/component_edit.html", context)


# ---- Components: Save ----

@router.put("/api/landing/pack/{pack_id}/components/{component_type}/{component_name}")
async def pack_landing_save_component(
    request: Request,
    pack_id: int,
    component_type: str,
    component_name: str,
    encodedContent: str = Form(...),
    current_user: User = Depends(get_current_user),
):
    """Save a pack landing component (base64-encoded content)."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if component_type not in ALLOWED_COMPONENT_TYPES:
        raise HTTPException(status_code=400, detail="Invalid component type")

    component_name = _secure_filename(component_name)

    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    pack_dir, _ = await _get_pack_dir_and_info(pack_id, pack_row)
    _ensure_pack_directories(pack_dir)

    if component_type == "html":
        target_dir = pack_dir / "templates" / "components"
        filename = f"{component_name}.html"
    elif component_type == "css":
        target_dir = pack_dir / "static" / "css"
        filename = f"{component_name}.css"
    elif component_type == "js":
        target_dir = pack_dir / "static" / "js"
        filename = f"{component_name}.js"

    validated_path = validate_path_within_directory(filename, target_dir)

    content = base64.b64decode(encodedContent).decode("utf-8")
    content = re.sub(r"\n\s*\n", "\n", content.strip())
    content = re.sub(r"\r\n", "\n", content)

    with open(str(validated_path), "w", encoding="utf-8") as f:
        f.write(content)

    return JSONResponse({"success": True, "message": "Component saved successfully"})


# ---- Components: Create ----

@router.post("/api/landing/pack/{pack_id}/components")
async def pack_landing_create_component(
    request: Request,
    pack_id: int,
    current_user: User = Depends(get_current_user),
):
    """Create a new component file for a pack landing page."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    component_type = (body.get("component_type") or "").strip()
    component_name = (body.get("component_name") or "").strip()

    if component_type not in ALLOWED_COMPONENT_TYPES:
        raise HTTPException(status_code=400, detail="Invalid component type")
    if not component_name:
        raise HTTPException(status_code=400, detail="Component name is required")

    component_name = _secure_filename(component_name)

    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    pack_dir, _ = await _get_pack_dir_and_info(pack_id, pack_row)
    _ensure_pack_directories(pack_dir)

    if component_type == "html":
        target_dir = pack_dir / "templates" / "components"
        filename = f"{component_name}.html"
    elif component_type == "css":
        target_dir = pack_dir / "static" / "css"
        filename = f"{component_name}.css"
    elif component_type == "js":
        target_dir = pack_dir / "static" / "js"
        filename = f"{component_name}.js"

    os.makedirs(str(target_dir), exist_ok=True)
    validated_path = validate_path_within_directory(filename, target_dir)

    if validated_path.exists():
        raise HTTPException(status_code=400, detail="Component already exists")

    with open(str(validated_path), "w", encoding="utf-8") as f:
        if component_type == "html":
            f.write("<div>\n    <!-- Your component content here -->\n</div>")
        elif component_type == "css":
            f.write("/* Your CSS styles here */")
        elif component_type == "js":
            f.write("// Your JavaScript code here")

    return JSONResponse({
        "success": True,
        "message": "Component created successfully",
        "redirect_url": f"/landing/pack/{pack_id}/components",
    })


# ---- Components: Delete ----

@router.delete("/api/landing/pack/{pack_id}/components/{component_type}/{component_name}")
async def pack_landing_delete_component(
    pack_id: int,
    component_type: str,
    component_name: str,
    current_user: User = Depends(get_current_user),
):
    """Delete a component file from a pack landing page."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if component_type not in {"html", "css", "js"}:
        raise HTTPException(status_code=400, detail="Invalid component type")

    if not component_name or not re.match(r"^[a-zA-Z0-9_-]+$", component_name):
        raise HTTPException(status_code=400, detail="Invalid component name")

    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    pack_dir, _ = await _get_pack_dir_and_info(pack_id, pack_row)

    if component_type == "html":
        file_path = pack_dir / "templates" / "components" / f"{component_name}.html"
    elif component_type == "css":
        file_path = pack_dir / "static" / "css" / f"{component_name}.css"
    elif component_type == "js":
        file_path = pack_dir / "static" / "js" / f"{component_name}.js"

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Component not found")

    file_path.unlink()
    return JSONResponse({"success": True, "message": f"Component '{component_name}' deleted successfully"})


# ---- Images: List ----

@router.get("/api/landing/pack/{pack_id}/images")
async def pack_landing_list_images(
    pack_id: int,
    current_user: User = Depends(get_current_user),
):
    """List images in the pack's static/img/ directory."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    pack_dir, _ = await _get_pack_dir_and_info(pack_id, pack_row)
    img_dir = pack_dir / "static" / "img"

    public_id = pack_row["public_id"]
    slug = pack_row["slug"] or slugify(pack_row["name"])

    images = []
    if img_dir.is_dir() and public_id:
        for filename in os.listdir(str(img_dir)):
            if filename.lower().endswith(tuple(ALLOWED_IMAGE_EXTENSIONS)):
                image_url = f"/pack/{public_id}/{slug}/static/img/{filename}"
                images.append({
                    "id": filename,
                    "name": filename,
                    "url": image_url,
                })

    return JSONResponse({"images": images})


# ---- Images: Upload ----

@router.post("/api/landing/pack/{pack_id}/images")
async def pack_landing_upload_images(
    pack_id: int,
    images: List[UploadFile] = File(...),
    names: List[str] = Form(...),
    current_user: User = Depends(get_current_user),
):
    """Upload images to the pack's static/img/ directory."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    pack_dir, username = await _get_pack_dir_and_info(pack_id, pack_row)
    img_dir = pack_dir / "static" / "img"

    uploaded_files = []
    for image, name in zip(images, names):
        if not image or not _allowed_image_file(image.filename):
            raise HTTPException(status_code=400, detail=f"Invalid file format: {image.filename}")

        # Validate image
        content = await image.read()
        if len(content) > MAX_IMAGE_UPLOAD_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"Image {image.filename} too large. Maximum size is {MAX_IMAGE_UPLOAD_SIZE // (1024 * 1024)}MB",
            )

        stored_filename = await asyncio.to_thread(
            _save_pack_landing_image,
            content,
            original_filename=image.filename,
            requested_name=name,
            img_dir=img_dir,
        )
        uploaded_files.append({
            "id": stored_filename,
            "name": stored_filename,
            "url": f"/pack/{pack_row['public_id']}/{pack_row['slug']}/static/img/{stored_filename}",
        })

    return JSONResponse({
        "message": f"Successfully uploaded {len(uploaded_files)} images",
        "images": uploaded_files,
    })


# ---- Images: Delete ----

@router.delete("/api/landing/pack/{pack_id}/images/{image_id}")
async def pack_landing_delete_image(
    pack_id: int,
    image_id: str,
    current_user: User = Depends(get_current_user),
):
    """Delete an image from a pack landing page's static/img/ directory."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)

    pack_dir, _ = await _get_pack_dir_and_info(pack_id, pack_row)
    img_dir = pack_dir / "static" / "img"

    safe_name = _secure_filename(image_id)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid image filename")

    validated_path = validate_path_within_directory(safe_name, img_dir)
    if not validated_path.exists():
        raise HTTPException(status_code=404, detail="Image not found")

    validated_path.unlink()
    return JSONResponse({"success": True, "message": "Image deleted successfully"})


# ---------------------------------------------------------------------------
# Manual grant / revoke access
# ---------------------------------------------------------------------------

@router.post("/api/users/{user_id}/packs/{pack_id}/grant")
async def api_grant_pack_access(user_id: int, pack_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")

    async with get_db_connection() as conn:
        pack_row = await get_pack(conn, pack_id)
        if not pack_row:
            raise HTTPException(status_code=404, detail="Pack not found")

        cursor = await conn.execute("SELECT id FROM USERS WHERE id = ?", (user_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="User not found")

        await grant_pack_entitlement(
            conn,
            user_id=user_id,
            pack_id=pack_id,
            source="admin_grant",
            source_ref_type="admin_pack_grant",
            source_ref_id=f"{user_id}:{pack_id}",
            metadata={"route": "api_grant_pack_access"},
            created_by_user_id=current_user.id,
            reactivate_inactive=True,
        )

        # Record creator relationship
        creator_id = pack_row["created_by_user_id"] if pack_row else None
        if creator_id:
            try:
                from common import upsert_creator_relationship
                ucr_cursor = await conn.cursor()
                await upsert_creator_relationship(ucr_cursor, user_id, creator_id, 'assigned_by', 'pack', pack_id)
            except Exception as ucr_err:
                logger.warning(f"Could not record creator relationship for grant: {ucr_err}")
        await conn.commit()

    return JSONResponse({"message": "Access granted"})


@router.delete("/api/users/{user_id}/packs/{pack_id}/grant")
async def api_revoke_pack_access(user_id: int, pack_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")

    async with get_db_connection() as conn:
        await revoke_entitlement(
            conn,
            user_id=user_id,
            asset_type="pack",
            asset_id=pack_id,
            status="revoked",
            revoked_by_user_id=current_user.id,
            metadata={"route": "api_revoke_pack_access"},
        )
        await conn.commit()

    return JSONResponse({"message": "Access revoked"})


# ---------------------------------------------------------------------------
# Public explorer API (for Phase 1D, endpoint ready now)
# ---------------------------------------------------------------------------

@router.get("/api/explore/packs")
async def api_explore_packs(
    request: Request,
    search: str = "",
    page: int = 1,
    limit: int = 24,
    mine: int = 0,
    current_user: User = Depends(get_current_user),
):
    require_discovery_enabled()

    if page < 1:
        page = 1
    if limit < 1 or limit > 48:
        limit = 24

    # Piggyback ranking recalculation trigger
    await maybe_trigger_recalculation()

    user_id = current_user.id if current_user else None
    async with get_db_connection(readonly=True) as conn:
        packs, total = await get_public_packs(conn, search=search, page=page, limit=limit, user_id=user_id, mine_only=bool(mine))

    pack_payloads = []
    for pack in packs:
        pack_data = dict(pack)
        user_has_access = bool(pack_data.get("user_has_access"))
        pack_payloads.append(
            {
                **pack_data,
                "has_landing_page": bool(pack_data["has_custom_landing"]),
                **purchase_metadata_for_request(
                    request,
                    is_paid=bool(pack_data.get("is_paid")),
                    user_has_access=user_has_access,
                    price=pack_data.get("price"),
                ),
            }
        )

    return JSONResponse({
        "packs": pack_payloads,
        "total": total,
        "page": page,
        "pages": (total + limit - 1) // limit if total > 0 else 1,
    })


@router.get("/api/explore/packs/{pack_id}/items")
async def api_explore_pack_items(pack_id: int):
    """Return items for a published public pack (used by explorer modal)."""
    require_discovery_enabled()

    async with get_db_connection(readonly=True) as conn:
        pack = await get_pack(conn, pack_id)
        if not pack or pack["status"] != "published" or not pack["is_public"]:
            raise HTTPException(status_code=404, detail="Pack not found")
        items = await get_public_pack_items(conn, pack_id)
    return JSONResponse([dict(i) for i in items])


@router.post("/api/packs/{pack_id}/claim-free")
async def api_claim_free_pack(pack_id: int, current_user: User = Depends(get_current_user)):
    """Claim a free pack for the logged-in user."""
    require_checkout_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    async with get_db_connection() as conn:
        pack = await get_pack(conn, pack_id)
        if not pack or pack["status"] != "published" or not pack["is_public"]:
            raise HTTPException(status_code=404, detail="Pack not found")

        if pack["is_paid"]:
            raise HTTPException(status_code=400, detail="This pack requires a purchase")

        has_access = await user_has_pack_entitlement_access(conn, user_id=current_user.id, pack_id=pack_id)
        if has_access:
            return JSONResponse({"message": "You already have access to this pack", "redirect": "/chat"})

        active_cursor = await conn.execute(
            """SELECT 1 FROM PACK_ITEMS
               WHERE pack_id = ? AND is_active = 1
               AND (disable_at IS NULL OR disable_at > datetime('now'))
               LIMIT 1""",
            (pack_id,)
        )
        if not await active_cursor.fetchone():
            raise HTTPException(status_code=400, detail="This pack is currently unavailable")

        await grant_pack_entitlement(
            conn,
            user_id=current_user.id,
            pack_id=pack_id,
            source="free_claim",
            source_ref_type="free_pack_claim",
            source_ref_id=f"{current_user.id}:{pack_id}",
            metadata={"route": "api_claim_free_pack"},
            created_by_user_id=pack["created_by_user_id"],
        )

        # Record creator relationship
        creator_id = pack["created_by_user_id"] if pack else None
        if creator_id:
            try:
                from common import upsert_creator_relationship
                ucr_cursor = await conn.cursor()
                await upsert_creator_relationship(ucr_cursor, current_user.id, creator_id, 'purchased_from', 'pack', pack_id)
            except Exception as ucr_err:
                logger.warning(f"Could not record creator relationship for free claim: {ucr_err}")
        await conn.commit()

    return JSONResponse({"message": "Pack claimed successfully", "redirect": "/chat"})


@router.get("/api/packs/{pack_id}/check-access")
async def check_pack_access_endpoint(pack_id: int, current_user: User = Depends(get_current_user)):
    """Read-only check: does the current user have access to this pack?"""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    async with get_db_connection(readonly=True) as conn:
        has_access = await user_has_pack_entitlement_access(conn, user_id=current_user.id, pack_id=pack_id)

    return {"has_access": has_access}


@router.post("/api/packs/{pack_id}/purchase")
async def api_purchase_pack(pack_id: int, request: Request, current_user: User = Depends(get_current_user)):
    """Create a Stripe Checkout Session to purchase a paid pack."""
    require_checkout_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if ios_purchase_blocked(request):
        return ios_purchase_disabled_response()

    try:
        body = await request.json()
    except Exception:
        body = {}

    discount_code = str(body.get("discount_code", "")).strip() if body.get("discount_code") else ""

    async with get_db_connection() as conn:
        pack = await get_pack(conn, pack_id)
        if not pack or pack["status"] != "published" or not pack["is_public"]:
            raise HTTPException(status_code=404, detail="Pack not found")

        if not pack["is_paid"]:
            raise HTTPException(status_code=400, detail="This pack is free. Use the claim endpoint instead.")

        if pack["created_by_user_id"] == current_user.id:
            raise HTTPException(status_code=400, detail="You cannot purchase your own pack")

        has_access = await user_has_pack_entitlement_access(conn, user_id=current_user.id, pack_id=pack_id)
        if has_access:
            return JSONResponse({"message": "You already have access to this pack", "redirect": "/chat"})

        active_cursor = await conn.execute(
            """SELECT 1 FROM PACK_ITEMS
               WHERE pack_id = ? AND is_active = 1
               AND (disable_at IS NULL OR disable_at > datetime('now'))
               LIMIT 1""",
            (pack_id,)
        )
        if not await active_cursor.fetchone():
            raise HTTPException(status_code=400, detail="This pack is currently unavailable")

        original_price = float(pack["price"])
        final_amount = original_price
        discount_value = 0

        # Validate and apply discount code
        if discount_code:
            try:
                discount = await validate_discount_code(discount_code, original_price, conn=conn)
            except DiscountError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
            discount_value = discount.discount_value
            final_amount = discount.final_amount

    # Reject amounts between $0.01-$0.49: below Stripe minimum but not a full discount
    if 0 < final_amount < 0.50:
        raise HTTPException(
            status_code=400,
            detail=f"Final price after discount (${final_amount:.2f}) is below the minimum processing amount ($0.50). The discount must either cover the full price or leave at least $0.50."
        )

    # True 100% discount (final_amount == 0): process immediately without Stripe
    if final_amount == 0:
        async with get_db_connection() as conn:
            # Atomic transaction: purchase + access grant + config + discount decrement
            await conn.execute("BEGIN IMMEDIATE")
            try:
                if discount_code:
                    try:
                        discount = await validate_discount_code(discount_code, original_price, conn=conn)
                    except DiscountError as exc:
                        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
                    if discount.final_amount != 0:
                        raise HTTPException(status_code=400, detail="Discount does not fully cover this payment")

                # Inline create_pack_purchase (avoid intermediate commit)
                purchase_cursor = await conn.execute(
                    """INSERT INTO PACK_PURCHASES
                       (buyer_user_id, pack_id, amount, currency, payment_method, payment_reference, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (current_user.id, pack_id, 0.0, "USD", "free",
                     f"discount_{discount_code}_user_{current_user.id}", "completed"),
                )

                await grant_pack_entitlement(
                    conn,
                    user_id=current_user.id,
                    pack_id=pack_id,
                    source="discount_claim",
                    source_ref_type="pack_purchase",
                    source_ref_id=purchase_cursor.lastrowid,
                    metadata={
                        "payment_method": "free",
                        "discount_code": discount_code or None,
                        "amount": 0,
                    },
                    created_by_user_id=pack["created_by_user_id"],
                )

                # Apply landing_reg_config to buyer
                await _apply_pack_config_to_user(conn, pack, current_user.id, discount_pct=100)

                # Set current_prompt_id to first active prompt in pack
                first_prompt_cursor = await conn.execute(
                    "SELECT prompt_id FROM PACK_ITEMS WHERE pack_id = ? AND is_active = 1 AND (disable_at IS NULL OR disable_at > datetime('now')) ORDER BY display_order LIMIT 1",
                    (pack_id,)
                )
                first_prompt_row = await first_prompt_cursor.fetchone()
                if first_prompt_row:
                    await conn.execute(
                        "UPDATE USER_DETAILS SET current_prompt_id = ? WHERE user_id = ?",
                        (first_prompt_row[0], current_user.id)
                    )

                # Record transaction for audit trail (pack purchased at $0)
                bal_cur = await conn.execute(
                    "SELECT balance FROM USER_DETAILS WHERE user_id = ?",
                    (current_user.id,)
                )
                bal_row = await bal_cur.fetchone()
                cur_balance = bal_row[0] if bal_row else 0
                await conn.execute('''
                    INSERT INTO TRANSACTIONS
                    (user_id, type, amount, balance_before, balance_after,
                     description, reference_id, discount_code)
                    VALUES (?, 'pack_purchase', 0, ?, ?, ?, ?, ?)
                ''', (
                    current_user.id,
                    cur_balance,
                    cur_balance,
                    f'Free pack purchase (100% discount): pack_id={pack_id}',
                    f'discount_{discount_code}_user_{current_user.id}',
                    discount_code if discount_code else None
                ))

                await decrement_discount_usage(conn, discount_code)

                # Record creator relationship
                pack_creator_id = pack["created_by_user_id"] if pack else None
                if pack_creator_id:
                    try:
                        from common import upsert_creator_relationship
                        await upsert_creator_relationship(conn, current_user.id, pack_creator_id, 'purchased_from', 'pack', pack_id)
                    except Exception as ucr_err:
                        logger.warning(f"Could not record creator relationship for 100% discount purchase: {ucr_err}")

                await conn.commit()
            except Exception:
                await conn.execute("ROLLBACK")
                raise

            # Creator earnings: 0 (nothing was paid)
            logger.info(f"Free purchase (100% discount): user={current_user.id}, pack={pack_id}, code={discount_code}")

        return JSONResponse({
            "message": "Pack claimed successfully with discount",
            "redirect": "/chat",
            "free_purchase": True
        })

    # Stripe is only needed when there's an actual charge
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Payment service is not configured")

    # Create Stripe Checkout Session
    base_url = str(request.base_url).rstrip('/')
    discount_claimed = False
    discount_claim_reference = None
    try:
        if discount_code:
            discount_claim_reference = f"discount-claim-{secrets.token_hex(16)}"
            try:
                discount = await claim_discount_usage_for_checkout(discount_code, original_price)
            except DiscountError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
            discount_claimed = True
            discount_value = discount.discount_value
            final_amount = discount.final_amount
            if final_amount == 0:
                raise HTTPException(
                    status_code=400,
                    detail="Discount now fully covers this purchase. Please retry to claim it without Stripe.",
                )
            if 0 < final_amount < 0.50:
                raise HTTPException(
                    status_code=400,
                    detail=f"Final price after discount (${final_amount:.2f}) is below the minimum processing amount ($0.50). The discount must either cover the full price or leave at least $0.50."
                )

        session = await asyncio.to_thread(
            stripe.checkout.Session.create,
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'unit_amount': int(final_amount * 100),
                    'product_data': {
                        'name': pack["name"],
                        'description': (pack["description"] or "AI prompt pack")[:500],
                    },
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=f"{base_url}/pack-purchase-success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}/pack/{pack['public_id']}/{pack['slug']}/?cancelled=true",
            metadata={
                'type': 'pack_purchase',
                'pack_id': str(pack_id),
                'buyer_user_id': str(current_user.id),
                'original_price': str(original_price),
                'final_amount': str(final_amount),
                'discount_code': discount_code,
                'discount_value': str(discount_value),
                'discount_claimed': '1' if discount_claimed else '0',
                'discount_claim_reference': discount_claim_reference or '',
            }
        )
        return JSONResponse({"checkout_url": session.url})

    except HTTPException:
        if discount_claimed:
            await restore_discount_usage_for_checkout(
                discount_code,
                reference_id=discount_claim_reference,
                user_id=current_user.id,
            )
        raise
    except stripe.error.StripeError as e:
        if discount_claimed:
            await restore_discount_usage_for_checkout(
                discount_code,
                reference_id=discount_claim_reference,
                user_id=current_user.id,
            )
        logger.error(f"Stripe error creating pack checkout: {e}")
        raise HTTPException(status_code=500, detail="Payment service error")


@router.get("/api/packs/{pack_id}/purchases")
async def api_get_pack_purchases(pack_id: int, current_user: User = Depends(get_current_user)):
    """Return purchase history for a pack (admin or pack creator only)."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_admin_or_user(current_user)

    async with get_db_connection(readonly=True) as conn:
        pack_row = await get_pack(conn, pack_id)
        await _require_pack_owner(pack_row, current_user)
        rows = await get_pack_purchases(conn, pack_id)

    purchases = [dict(r) for r in rows]
    total_revenue = sum(
        p["amount"] for p in purchases if p["status"] == "completed"
    )

    return JSONResponse({
        "purchases": purchases,
        "total_count": len(purchases),
        "total_revenue": round(total_revenue, 2),
    })


async def _apply_pack_config_to_user(conn, pack, user_id, discount_pct=0):
    """Apply a pack's landing_reg_config to a user after purchase.
    discount_pct: 0-100, scales initial_balance proportionally.
    NOTE: Does NOT commit -- caller is responsible for committing the transaction."""
    import json as _json
    # Ensure pack is a dict (sqlite3.Row does not have .get())
    if not isinstance(pack, dict):
        pack = dict(pack)
    lrc_raw = pack.get("landing_reg_config") or pack.get("landing_registration_config")
    if not lrc_raw:
        return

    try:
        lrc = _json.loads(lrc_raw) if isinstance(lrc_raw, str) else lrc_raw
    except Exception:
        return

    # Read current values to enforce "only expand, never restrict"
    ud_cursor = await conn.execute(
        """SELECT public_prompts_access, billing_account_id,
                  allow_file_upload, allow_image_generation
           FROM USER_DETAILS WHERE user_id = ?""",
        (user_id,)
    )
    ud_row = await ud_cursor.fetchone()
    cur_public = ud_row[0] if ud_row else 0
    cur_billing = ud_row[1] if ud_row else None
    cur_file = ud_row[2] if ud_row else 0
    cur_imggen = ud_row[3] if ud_row else 0

    updates = []
    params = []

    ib = float(lrc.get("initial_balance", 0))
    if ib > 0:
        # Scale initial_balance by discount: 100% discount = $0 initial_balance
        scaled_ib = ib * (1 - discount_pct / 100) if discount_pct > 0 else ib
        if scaled_ib > 0:
            updates.append("balance = balance + ?")
            params.append(scaled_ib)

    if lrc.get("billing_mode") == "user_pays":
        creator_balance = await get_balance(pack["created_by_user_id"])
        if creator_balance <= 0:
            raise HTTPException(
                status_code=503,
                detail="This pack is temporarily unavailable"
            )
        # Only set billing_account_id if not already managed by someone else
        if cur_billing is None:
            updates.append("billing_account_id = ?")
            params.append(pack["created_by_user_id"])
            # Set billing limits when newly assigning billing account
            bl = lrc.get("billing_limit")
            if bl is not None:
                updates.append("billing_limit = ?")
                params.append(float(bl))
            bla = lrc.get("billing_limit_action", "block")
            updates.append("billing_limit_action = ?")
            params.append(bla)
            bara = lrc.get("billing_auto_refill_amount", 10.0)
            updates.append("billing_auto_refill_amount = ?")
            params.append(float(bara))
            bml = lrc.get("billing_max_limit")
            if bml is not None:
                updates.append("billing_max_limit = ?")
                params.append(float(bml))
        else:
            logger.warning(
                "Pack config: billing_account_id not overwritten for user %s (already set to %s)",
                user_id, cur_billing
            )

    # Only expand boolean permissions, never restrict
    if "public_prompts_access" in lrc:
        if lrc["public_prompts_access"] and not cur_public:
            updates.append("public_prompts_access = 1")
        elif not lrc["public_prompts_access"] and cur_public:
            logger.warning("Pack config: skipping public_prompts_access downgrade for user %s", user_id)

    if "allow_file_upload" in lrc:
        if lrc["allow_file_upload"] and not cur_file:
            updates.append("allow_file_upload = 1")
        elif not lrc["allow_file_upload"] and cur_file:
            logger.warning("Pack config: skipping allow_file_upload downgrade for user %s", user_id)

    if "allow_image_generation" in lrc:
        if lrc["allow_image_generation"] and not cur_imggen:
            updates.append("allow_image_generation = 1")
        elif not lrc["allow_image_generation"] and cur_imggen:
            logger.warning("Pack config: skipping allow_image_generation downgrade for user %s", user_id)

    if updates:
        params.append(user_id)
        sql = f"UPDATE USER_DETAILS SET {', '.join(updates)} WHERE user_id = ?"
        await conn.execute(sql, params)


# ---------------------------------------------------------------------------
# Pack Landing Pages (Public)
# ---------------------------------------------------------------------------

_MEDIA_TYPES = {
    '.css': 'text/css', '.js': 'application/javascript',
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.gif': 'image/gif', '.svg': 'image/svg+xml', '.webp': 'image/webp',
    '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf',
    '.ico': 'image/x-icon', '.mp3': 'audio/mpeg', '.mp4': 'video/mp4',
    '.webm': 'video/webm', '.pdf': 'application/pdf',
}


def _build_pack_filesystem_path(username: str, pack_id: int, pack_name: str) -> Path:
    """Build the filesystem path to a pack's landing page directory."""
    hash_prefix1, hash_prefix2, user_hash = generate_user_hash(username)
    padded_id = f"{pack_id:07d}"
    safe_name = sanitize_name(pack_name)
    return (
        DATA_DIR / "users" / hash_prefix1 / hash_prefix2 / user_hash
        / "packs" / padded_id[:3] / f"{padded_id[3:]}_{safe_name}"
    )


def _landing_404() -> HTMLResponse:
    """Minimal 404 page for pack landings."""
    return HTMLResponse(
        content='<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">'
        '<title>404 - Not Found</title><style>'
        'body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;'
        'display:flex;align-items:center;justify-content:center;min-height:100vh;'
        'margin:0;background:#f5f5f5;color:#333}'
        '.c{text-align:center;padding:2rem}'
        'h1{font-size:6rem;margin:0;color:#ccc}p{font-size:1.2rem;color:#666}'
        '</style></head><body><div class="c"><h1>404</h1><p>Page not found</p></div></body></html>',
        status_code=404,
    )


async def _get_landing_pack(public_id: str):
    """Fetch and validate a pack for landing page display. Returns pack row or raises 404."""
    if not re.match(r'^[a-zA-Z0-9]{8}$', public_id):
        return None
    async with get_db_connection(readonly=True) as conn:
        pack = await get_pack_by_public_id(conn, public_id)
    if not pack:
        return None
    if pack["status"] != "published" or not pack["is_public"]:
        return None
    return pack


@router.get("/pack/{public_id}/{slug}")
async def pack_landing_redirect_trailing_slash(public_id: str, slug: str):
    """Redirect /pack/x/y -> /pack/x/y/ so relative URLs work correctly."""
    require_public_landings_enabled()

    return RedirectResponse(url=f"/pack/{public_id}/{slug}/", status_code=301)


@router.get("/pack/{public_id}/{slug}/register", response_class=HTMLResponse)
async def pack_register_page(request: Request, public_id: str, slug: str):
    """Registration page served from pack landing. Full implementation in Phase 1D-3."""
    require_public_landings_enabled()

    pack = await _get_landing_pack(public_id)
    if not pack:
        return _landing_404()

    if slug != pack["slug"]:
        return _landing_404()

    # Phase 1D-3: full registration page will be implemented here
    # For now redirect to the pack landing (which has an inline registration section)
    return RedirectResponse(url=f"/pack/{public_id}/{pack['slug']}/", status_code=302)


@router.get("/pack/{public_id}/{slug}/static/{resource_path:path}")
async def pack_landing_static(
    public_id: str,
    slug: str,
    resource_path: str,
    request: Request = None,
):
    """Serve static resources (CSS, JS, images) for custom pack landing pages."""
    try:
        require_public_landings_enabled()

        if not re.match(r'^[a-zA-Z0-9]{8}$', public_id):
            return _landing_404()
        if not _valid_landing_resource_path(resource_path):
            return _landing_404()

        cached = await get_pack_landing_cached(public_id)
        if not cached:
            return _landing_404()

        if cached["status"] != "published" or not cached["is_public"]:
            return _landing_404()

        if slug != cached["slug"]:
            return _landing_404()
        if not cached["has_custom_landing"]:
            return _landing_404()

        if not is_creator_content_request(request):
            isolated_url = build_creator_content_url(
                f"/pack/{public_id}/{slug}/static/{resource_path}"
            )
            if not isolated_url:
                return creator_content_unavailable_response()
            return RedirectResponse(isolated_url, status_code=302)

        pack_dir = cached["path"]
        static_path = pack_dir / "static" / resource_path

        # Path traversal protection (check BEFORE any filesystem access)
        try:
            static_path.resolve().relative_to((pack_dir / "static").resolve())
        except ValueError:
            return _landing_404()

        if not static_path.is_file():
            return _landing_404()

        media_type = _MEDIA_TYPES.get(static_path.suffix.lower(), 'application/octet-stream')
        return FileResponse(
            static_path,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=3600"},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving pack landing static: {e}")
        return _landing_404()


@router.get("/pack/{public_id}/{slug}/", response_class=HTMLResponse)
async def pack_landing_page(request: Request, public_id: str, slug: str):
    """
    Serve the pack landing page.
    If has_custom_landing=true and home.html exists, serve the custom file.
    Otherwise render the default Jinja2 template with pack data.
    """
    try:
        require_public_landings_enabled()

        if not re.match(r'^[a-zA-Z0-9]{8}$', public_id):
            return _landing_404()

        cached = await get_pack_landing_cached(public_id)
        if not cached:
            return _landing_404()

        # Only serve published, public packs
        if cached["status"] != "published" or not cached["is_public"]:
            return _landing_404()

        if slug != cached["slug"]:
            return _landing_404()

        pack_id = cached["pack_id"]
        is_preview = request.query_params.get("preview") == "1"
        is_embed = request.query_params.get("embed") == "1"

        # Check for custom landing page first
        if cached["has_custom_landing"]:
            if is_creator_content_request(request):
                if is_preview:
                    expected = {
                        "purpose": "pack_preview",
                        "pack_id": int(pack_id),
                    }
                    if verify_content_token(
                        request.query_params.get("preview_token"),
                        expected=expected,
                    ) is None:
                        return _landing_404()
            elif is_preview:
                if not await _can_preview_pack(request, cached["created_by_user_id"]):
                    raise HTTPException(status_code=403, detail="Access denied")
                preview_token = sign_content_token(
                    {"purpose": "pack_preview", "pack_id": int(pack_id)},
                    ttl_seconds=300,
                )
                isolated_url = build_creator_content_url(
                    f"/pack/{public_id}/{slug}/",
                    {"preview": 1, "preview_token": preview_token},
                )
                if not isolated_url or not preview_token:
                    return creator_content_unavailable_response()
                return RedirectResponse(
                    isolated_url,
                    status_code=302,
                    headers={"Cache-Control": "no-store"},
                )
            else:
                isolated_url = build_creator_content_url(
                    f"/pack/{public_id}/{slug}/",
                    {"embed": 1} if is_embed else None,
                )
                if not isolated_url:
                    return creator_content_unavailable_response()
                return RedirectResponse(isolated_url, status_code=302)

            html_path = cached["path"] / "home.html"
            if html_path.is_file():
                html_content = html_path.read_text(encoding="utf-8")
                if not is_preview:
                    html_content = _inject_pack_analytics(html_content, pack_id)
                return HTMLResponse(content=html_content)

        if is_creator_content_request(request):
            return _landing_404()
        if is_preview and not await _can_preview_pack(request, cached["created_by_user_id"]):
            raise HTTPException(status_code=403, detail="Access denied")

        # Default: render Jinja2 template (pack_items still queried fresh)
        async with get_db_connection(readonly=True) as conn:
            items = await get_pack_items(conn, pack_id)

        tags = []
        if cached["tags"]:
            try:
                tags = orjson.loads(cached["tags"]) if isinstance(cached["tags"], str) else cached["tags"]
            except Exception:
                pass

        pack_dict = {
            "id": pack_id,
            "public_id": public_id,
            "name": cached["pack_name"],
            "slug": cached["slug"],
            "description": cached["description"],
            "cover_image": cached["cover_image"],
            "is_paid": cached["is_paid"],
            "price": cached["price"],
            "status": cached["status"],
            "is_public": cached["is_public"],
            "has_custom_landing": cached["has_custom_landing"],
            "created_by_user_id": cached["created_by_user_id"],
            "tags": cached["tags"],
            "created_by_username": cached["username"],
        }
        site_url = str(request.base_url).rstrip("/")
        context = {
            "request": request,
            "pack": pack_dict,
            "items": [dict(item) for item in items],
            "tags": tags,
            "is_paid": bool(pack_dict.get("is_paid")),
            "price_display": f"${pack_dict['price']:.2f}" if pack_dict.get("is_paid") else "FREE",
            "base_url": f"/pack/{public_id}/{slug}",
            "site_url": site_url,
            "google_oauth_available": bool(GOOGLE_CLIENT_ID),
        }

        # Render template, inject analytics, and return as HTMLResponse
        template_response = templates.TemplateResponse("pack_landing_default.html", context)
        template_html = template_response.body.decode("utf-8")
        if not is_preview:
            template_html = _inject_pack_analytics(template_html, pack_id)
        return HTMLResponse(content=template_html)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving pack landing page: {e}")
        return _landing_404()
