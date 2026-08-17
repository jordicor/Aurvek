"""Marketplace discovery and public catalog routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from auth import get_current_user, unauthenticated_response
from captcha_service import get_captcha_config
from common import (
    AVATAR_TOKEN_EXPIRE_HOURS,
    CLOUDFLARE_BASE_URL,
    GOOGLE_CLIENT_ID,
    get_template_context,
    slugify,
    templates,
)
from database import get_db_connection
from marketplace.config import require_discovery_enabled
from marketplace.services.entitlements import active_entitlement_condition
from mobile.client import purchase_metadata_for_request
from models import User
from ranking import maybe_trigger_recalculation
from save_images import generate_img_token


router = APIRouter()


@router.get("/api/public-prompts")
async def get_public_prompts(current_user: User = Depends(get_current_user)) -> List[dict]:
    require_discovery_enabled()

    if current_user is None:
        return unauthenticated_response()

    async with get_db_connection(readonly=True) as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT id, name, description, image
                FROM PROMPTS
                WHERE public = TRUE
                ORDER BY name
                """
            )
            public_prompts = await cursor.fetchall()

    return [{"id": p[0], "name": p[1], "description": p[2], "image": p[3]} for p in public_prompts]


@router.get("/explore", response_class=HTMLResponse)
async def explore_page(request: Request, current_user: User = Depends(get_current_user)):
    """Render the Prompt Explorer page."""
    require_discovery_enabled()

    if current_user is None:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "captcha": get_captcha_config(),
                "google_oauth_available": bool(GOOGLE_CLIENT_ID),
            },
        )

    context = await get_template_context(request, current_user)
    return templates.TemplateResponse("explore.html", context)


@router.get("/api/explore/categories")
async def explore_categories(current_user: User = Depends(get_current_user)):
    """Get all categories available for prompt filtering."""
    require_discovery_enabled()

    if current_user is None:
        return unauthenticated_response()

    async with get_db_connection(readonly=True) as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT c.id, c.name, c.icon, c.is_age_restricted,
                       COUNT(p.id) as prompt_count
                FROM CATEGORIES c
                LEFT JOIN PROMPT_CATEGORIES pc ON c.id = pc.category_id
                LEFT JOIN PROMPTS p ON pc.prompt_id = p.id AND p.public = 1 AND p.is_unlisted = 0
                GROUP BY c.id
                HAVING prompt_count > 0
                ORDER BY c.display_order
                """
            )
            rows = await cursor.fetchall()

    return [
        {
            "id": row[0],
            "name": row[1],
            "icon": row[2],
            "is_age_restricted": bool(row[3]),
            "count": row[4],
        }
        for row in rows
    ]


@router.get("/api/explore/prompts")
async def explore_prompts(
    request: Request,
    current_user: User = Depends(get_current_user),
    category: int = None,
    search: str = None,
    page: int = 1,
    limit: int = 24,
    mine: int = 0,
    favorites: int = 0,
):
    """Get paginated public prompts with optional filtering."""
    require_discovery_enabled()

    if current_user is None:
        return unauthenticated_response()

    await maybe_trigger_recalculation()

    page = max(1, page)
    limit = min(max(1, limit), 48)
    offset = (page - 1) * limit

    async with get_db_connection(readonly=True) as conn:
        async with conn.cursor() as cursor:
            if mine:
                where_clauses = [
                    """(
                    EXISTS (
                        SELECT 1 FROM PROMPT_PERMISSIONS pp
                        WHERE pp.prompt_id = p.id AND pp.user_id = ? AND pp.permission_level = 'owner'
                    )
                    OR (
                        NOT EXISTS (
                            SELECT 1 FROM PROMPT_PERMISSIONS pp2
                            WHERE pp2.prompt_id = p.id AND pp2.permission_level = 'owner'
                        )
                        AND p.created_by_user_id = ?
                    )
                )"""
                ]
                params = [current_user.id, current_user.id]
            elif favorites:
                where_clauses = [
                    "p.public = 1",
                    "p.is_unlisted = 0",
                    "EXISTS (SELECT 1 FROM FAVORITE_PROMPTS fp2 WHERE fp2.user_id = ? AND fp2.prompt_id = p.id)",
                ]
                params = [current_user.id]
            else:
                where_clauses = ["p.public = 1", "p.is_unlisted = 0"]
                params = []

            if category and not mine and not favorites:
                where_clauses.append(
                    "EXISTS (SELECT 1 FROM PROMPT_CATEGORIES pc2 WHERE pc2.prompt_id = p.id AND pc2.category_id = ?)"
                )
                params.append(category)

            if search and search.strip():
                search_term = f"%{search.strip()}%"
                where_clauses.append("(p.name LIKE ? OR p.description LIKE ?)")
                params.extend([search_term, search_term])

            if not mine:
                where_clauses.append(
                    """
                    NOT EXISTS (
                        SELECT 1 FROM PROMPT_CATEGORIES pc_age
                        JOIN CATEGORIES c_age ON pc_age.category_id = c_age.id
                        WHERE pc_age.prompt_id = p.id AND c_age.is_age_restricted = 1
                        AND ? = 0
                    )
                    """
                )
                show_age_restricted = 1 if category else 0
                if category:
                    await cursor.execute("SELECT is_age_restricted FROM CATEGORIES WHERE id = ?", (category,))
                    cat_row = await cursor.fetchone()
                    show_age_restricted = 1 if (cat_row and cat_row[0]) else (1 if category else 0)
                params.append(show_age_restricted)

            where_sql = " AND ".join(where_clauses)

            count_sql = f"SELECT COUNT(DISTINCT p.id) FROM PROMPTS p WHERE {where_sql}"
            await cursor.execute(count_sql, params)
            total = (await cursor.fetchone())[0]

            user_id = current_user.id
            order_clause = "ORDER BY p.ranking_score DESC, p.created_at DESC" if not mine and not favorites else "ORDER BY p.created_at DESC"
            query = f"""
                SELECT p.id, p.name, p.description, p.image, p.public_id,
                       p.created_at, p.is_paid,
                       p.public as is_public, p.is_unlisted,
                       u.username as creator_name,
                       CASE WHEN fp.user_id IS NOT NULL THEN 1 ELSE 0 END as is_favorite,
                       CASE
                           WHEN EXISTS (SELECT 1 FROM PROMPT_PERMISSIONS pp_m WHERE pp_m.prompt_id = p.id AND pp_m.user_id = ? AND pp_m.permission_level = 'owner') THEN 1
                           WHEN NOT EXISTS (SELECT 1 FROM PROMPT_PERMISSIONS pp_m2 WHERE pp_m2.prompt_id = p.id AND pp_m2.permission_level = 'owner') AND p.created_by_user_id = ? THEN 1
                           ELSE 0
                       END as is_mine,
                       p.purchase_price,
                       CASE
                           WHEN EXISTS (SELECT 1 FROM PROMPT_PERMISSIONS pp_a WHERE pp_a.prompt_id = p.id AND pp_a.user_id = ? AND pp_a.permission_level IN ('owner', 'edit')) THEN 1
                           WHEN EXISTS (SELECT 1 FROM ENTITLEMENTS e_prompt WHERE e_prompt.user_id = ? AND e_prompt.asset_type = 'prompt' AND e_prompt.asset_id = p.id AND {active_entitlement_condition("e_prompt")}) THEN 1
                           WHEN EXISTS (SELECT 1 FROM ENTITLEMENTS e_pack JOIN PACK_ITEMS pi ON e_pack.asset_id = pi.pack_id WHERE e_pack.user_id = ? AND e_pack.asset_type = 'pack' AND pi.prompt_id = p.id AND pi.is_active = 1 AND (pi.disable_at IS NULL OR pi.disable_at > datetime('now')) AND {active_entitlement_condition("e_pack")}) THEN 1
                           ELSE 0
                       END as user_has_access,
                       p.has_landing_page
                FROM PROMPTS p
                LEFT JOIN USERS u ON p.created_by_user_id = u.id
                LEFT JOIN FAVORITE_PROMPTS fp ON fp.prompt_id = p.id AND fp.user_id = ?
                WHERE {where_sql}
                {order_clause}
                LIMIT ? OFFSET ?
            """
            await cursor.execute(query, [user_id, user_id, user_id, user_id, user_id, user_id] + params + [limit, offset])
            rows = await cursor.fetchall()

            prompt_ids = [row[0] for row in rows]
            prompt_categories = {}
            if prompt_ids:
                placeholders = ",".join("?" * len(prompt_ids))
                await cursor.execute(
                    f"""
                    SELECT pc.prompt_id, c.id, c.name, c.icon
                    FROM PROMPT_CATEGORIES pc
                    JOIN CATEGORIES c ON pc.category_id = c.id
                    WHERE pc.prompt_id IN ({placeholders})
                    ORDER BY c.display_order
                    """,
                    prompt_ids,
                )
                cat_rows = await cursor.fetchall()
                for cat_row in cat_rows:
                    pid = cat_row[0]
                    if pid not in prompt_categories:
                        prompt_categories[pid] = []
                    prompt_categories[pid].append(
                        {
                            "id": cat_row[1],
                            "name": cat_row[2],
                            "icon": cat_row[3],
                        }
                    )

    current_time = datetime.now(timezone.utc)
    new_expiration = current_time + timedelta(hours=AVATAR_TOKEN_EXPIRE_HOURS)
    prompts = []
    for row in rows:
        image_url = None
        image_fullsize_url = None
        if row[3]:
            img_base = f"{row[3]}_128.webp"
            token = generate_img_token(img_base, new_expiration, current_user)
            image_url = f"{CLOUDFLARE_BASE_URL}{img_base}?token={token}"
            img_full = f"{row[3]}_fullsize.webp"
            token_full = generate_img_token(img_full, new_expiration, current_user)
            image_fullsize_url = f"{CLOUDFLARE_BASE_URL}{img_full}?token={token_full}"

        slug = slugify(row[1]) if row[1] else ""
        prompts.append(
            {
                "id": row[0],
                "name": row[1],
                "description": row[2],
                "image_url": image_url,
                "image_fullsize_url": image_fullsize_url,
                "public_id": row[4],
                "created_at": row[5],
                "is_paid": bool(row[6]),
                "is_public": bool(row[7]),
                "is_unlisted": bool(row[8]),
                "creator_name": row[9],
                "is_favorite": bool(row[10]),
                "is_mine": bool(row[11]),
                "slug": slug,
                "categories": prompt_categories.get(row[0], []),
                "purchase_price": row[12],
                "user_has_access": bool(row[13]),
                "has_landing_page": bool(row[14]),
                **purchase_metadata_for_request(
                    request,
                    is_paid=bool(row[6]),
                    user_has_access=bool(row[13]),
                    price=row[12],
                ),
            }
        )

    total_pages = (total + limit - 1) // limit
    return {
        "prompts": prompts,
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
    }
