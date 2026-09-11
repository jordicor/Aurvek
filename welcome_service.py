"""
Welcome Service - World resolution and serving for welcome pages.

Handles determining which welcome world to show for a user,
serving the welcome world HTML with navbar injection, and
managing the product switcher data.
"""

import os
import json
import logging
from html import escape
from fastapi.responses import HTMLResponse
from database import get_db_connection
from marketplace.landing.isolation import (
    build_creator_content_url,
    creator_content_unavailable_response,
    safe_iframe_url,
    sign_content_token,
)
from marketplace.services.entitlements import active_entitlement_condition
from marketplace.services.entitlements import user_has_pack_access as user_has_pack_entitlement_access
from marketplace.services.entitlements import user_has_prompt_access as user_has_prompt_entitlement_access
from prompts import get_prompt_info, get_prompt_path, get_pack_path
from common import templates
from i18n import get_translator, template_context

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _get_pack_info(pack_id: int) -> dict | None:
    """Get pack info needed for filesystem path resolution."""
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """SELECT p.name, p.created_by_user_id, u.username AS created_by_username
               FROM PACKS p
               JOIN USERS u ON p.created_by_user_id = u.id
               WHERE p.id = ?""",
            (pack_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


def _welcome_exists(entity_path: str) -> bool:
    """Check whether a welcome/index.html file exists for an entity."""
    return os.path.isfile(os.path.join(entity_path, "welcome", "index.html"))


async def _get_prompt_path_safe(prompt_id: int) -> str | None:
    """Return the filesystem path for a prompt, or None on failure."""
    try:
        info = await get_prompt_info(prompt_id)
        return get_prompt_path(prompt_id, info)
    except Exception:
        return None


async def _get_pack_path_safe(pack_id: int) -> str | None:
    """Return the filesystem path for a pack, or None on failure."""
    try:
        info = await _get_pack_info(pack_id)
        if info is None:
            return None
        return get_pack_path(pack_id, info)
    except Exception:
        return None


async def build_world(world_type: str, world_id: int) -> dict | None:
    """Build a world dict if the entity exists and has a welcome page."""
    if world_type == "prompt":
        path = await _get_prompt_path_safe(world_id)
    elif world_type == "pack":
        path = await _get_pack_path_safe(world_id)
    else:
        return None

    if path is None or not _welcome_exists(path):
        return None

    # Fetch the name
    try:
        if world_type == "prompt":
            info = await get_prompt_info(world_id)
        else:
            info = await _get_pack_info(world_id)
        name = info["name"] if info else str(world_id)
    except Exception:
        name = str(world_id)

    return {"type": world_type, "id": world_id, "name": name, "path": path}



async def user_has_pack_access(user_id: int, pack_id: int) -> bool:
    """Check if a user currently has access to a pack."""
    async with get_db_connection(readonly=True) as conn:
        return await user_has_pack_entitlement_access(conn, user_id=user_id, pack_id=pack_id)


async def user_has_prompt_access(user, prompt_id: int) -> bool:
    """Check if a user can access a prompt (permission, pack, or all_prompts_access)."""
    if user.all_prompts_access:
        return True
    async with get_db_connection(readonly=True) as conn:
        return await user_has_prompt_entitlement_access(conn, user_id=user.id, prompt_id=prompt_id)


async def _get_user_packs(user_id: int) -> list[dict]:
    """Return all packs a user has access to (id, name, cover_image)."""
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            f"""SELECT p.id, p.name, p.cover_image
               FROM PACKS p
               JOIN ENTITLEMENTS e ON e.asset_type = 'pack' AND e.asset_id = p.id
               WHERE e.user_id = ?
               AND {active_entitlement_condition("e")}
               ORDER BY e.created_at DESC, e.id DESC""",
            (user_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]


async def _get_user_standalone_prompts(user_id: int) -> list[dict]:
    """Return prompts the user has access to that are NOT part of any pack they access."""
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            f"""SELECT DISTINCT pr.id, pr.name
               FROM PROMPTS pr
               WHERE (
                   EXISTS (
                       SELECT 1 FROM PROMPT_PERMISSIONS pp
                       WHERE pp.prompt_id = pr.id
                         AND pp.user_id = ?
                         AND pp.permission_level IN ('owner', 'edit')
                   )
                   OR EXISTS (
                       SELECT 1 FROM ENTITLEMENTS e_prompt
                       WHERE e_prompt.user_id = ?
                         AND e_prompt.asset_type = 'prompt'
                         AND e_prompt.asset_id = pr.id
                         AND {active_entitlement_condition("e_prompt")}
                   )
               )
               AND pr.id NOT IN (
                   SELECT pi.prompt_id FROM PACK_ITEMS pi
                   JOIN ENTITLEMENTS e_pack ON e_pack.asset_type = 'pack'
                       AND e_pack.asset_id = pi.pack_id
                   WHERE e_pack.user_id = ?
                     AND pi.is_active = 1
                     AND (pi.disable_at IS NULL OR julianday(pi.disable_at) > julianday('now'))
                     AND {active_entitlement_condition("e_pack")}
               )
               ORDER BY pr.name""",
            (user_id, user_id, user_id),
        )
        return [dict(r) for r in await cursor.fetchall()]


async def _get_all_prompts_with_welcome(user_id: int) -> list[dict]:
    """For users with all_prompts_access, return prompts that have welcome pages."""
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """SELECT id, name FROM PROMPTS
               WHERE has_welcome_page = 1
               ORDER BY name""",
        )
        return [dict(r) for r in await cursor.fetchall()]

async def _get_pack_member_prompts(pack_id: int) -> list[dict]:
    """Get member prompts of a pack with their welcome page status."""
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """SELECT pi.prompt_id AS id, pr.name, pi.display_order
               FROM PACK_ITEMS pi
               JOIN PROMPTS pr ON pr.id = pi.prompt_id
               WHERE pi.pack_id = ? AND pi.is_active = 1
               ORDER BY pi.display_order, pr.name""",
            (pack_id,),
        )
        members = []
        for row in await cursor.fetchall():
            path = await _get_prompt_path_safe(row["id"])
            has_welcome = path is not None and _welcome_exists(path)
            members.append({
                "id": row["id"],
                "name": row["name"],
                "has_welcome": has_welcome,
            })
        return members


async def _get_prompt_parent_pack(user_id: int, prompt_id: int) -> dict | None:
    """Find the first pack a user can access that contains this prompt and has a welcome page."""
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            f"""SELECT p.id, p.name
               FROM PACK_ITEMS pi
               JOIN PACKS p ON p.id = pi.pack_id
               JOIN ENTITLEMENTS e ON e.asset_type = 'pack' AND e.asset_id = p.id
               WHERE pi.prompt_id = ? AND e.user_id = ?
               AND pi.is_active = 1
               AND {active_entitlement_condition("e")}
               ORDER BY e.created_at ASC, e.id ASC
               LIMIT 5""",
            (prompt_id, user_id),
        )
        rows = await cursor.fetchall()
        for row in rows:
            path = await _get_pack_path_safe(row["id"])
            if path and _welcome_exists(path):
                return {"type": "pack", "id": row["id"], "name": row["name"]}
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def serve_welcome_world(request, user, world: dict) -> HTMLResponse:
    """
    Serve a welcome world page with the floating Aurvek navbar injected.

    Returns an HTMLResponse with the welcome HTML enriched with the
    product switcher data and navbar overlay.
    """
    translator = get_translator(request, user)
    access_token = sign_content_token(
        {
            "purpose": "welcome",
            "entity_type": world["type"],
            "entity_id": int(world["id"]),
        },
        ttl_seconds=8 * 60 * 60,
    )
    if not access_token:
        return creator_content_unavailable_response(translator)

    isolated_path = (
        f"/_aurvek/welcome/{world['type']}/{int(world['id'])}/{access_token}/"
    )
    iframe_url = build_creator_content_url(isolated_path)
    if not iframe_url:
        return creator_content_unavailable_response(translator)

    # Build navbar HTML
    navbar_html = render_aurvek_world_navbar(request)

    # Build switcher data
    switcher_data = await get_world_switcher_data(user, world)
    switcher_json = json.dumps(switcher_data).replace("</", "<\\/")
    switcher_script = f"<script>window.__aurvekWorlds = {switcher_json};</script>"
    title = escape(str(world.get("name") or translator.t("public_shell.welcome")))
    wrapper = f"""<!doctype html>
<html lang="{translator.language}">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <meta name="robots" content="noindex,nofollow">
    <title>{title}</title>
    <style>
        html,body{{height:100%;margin:0;background:#0b0d12;overflow:hidden}}
        .aurvek-world-frame{{position:fixed;inset:0;border:0;width:100%;height:100%}}
    </style>
    {switcher_script}
</head>
<body>
    <iframe class="aurvek-world-frame" src="{safe_iframe_url(iframe_url)}"
        title="{title}" sandbox="allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-top-navigation-by-user-activation"
        referrerpolicy="no-referrer"></iframe>
    {navbar_html}
</body>
</html>"""
    return HTMLResponse(wrapper, headers={"Cache-Control": "no-store", "Content-Language": translator.language})


def render_aurvek_world_navbar(request) -> str:
    """Evaluate the owned shell only; the creator document stays in its iframe."""
    return templates.env.get_template("aurvek_world_navbar.html").render(
        request=request, **template_context(request), i18n_domains=["public_shell"]
    )


async def get_world_switcher_data(user, current_world: dict) -> dict:
    """
    Build the data structure consumed by the product switcher dropdown.

    Returns a dict with 'current' (the active world) and 'products'
    (all user products with their welcome page availability).
    """
    products = []

    # Packs
    packs = await _get_user_packs(user.id)
    for pack in packs:
        path = await _get_pack_path_safe(pack["id"])
        has_welcome = path is not None and _welcome_exists(path)
        products.append({
            "type": "pack",
            "id": pack["id"],
            "name": pack["name"],
            "avatar_url": pack.get("cover_image"),
            "has_welcome": has_welcome,
        })

    # Standalone prompts
    if user.all_prompts_access:
        prompts = await _get_all_prompts_with_welcome(user.id)
    else:
        prompts = await _get_user_standalone_prompts(user.id)

    for prompt in prompts:
        path = await _get_prompt_path_safe(prompt["id"])
        has_welcome = path is not None and _welcome_exists(path)
        products.append({
            "type": "prompt",
            "id": prompt["id"],
            "name": prompt["name"],
            "avatar_url": None,
            "has_welcome": has_welcome,
        })

    current = {
        "type": current_world["type"],
        "id": current_world["id"],
        "name": current_world["name"],
        "avatar_url": None,
    }

    # Pack sub-navigation: include member prompts when viewing a pack
    if current_world["type"] == "pack":
        current["members"] = await _get_pack_member_prompts(current_world["id"])

    # Prompt parent pack: include parent pack info when viewing a prompt
    if current_world["type"] == "prompt":
        parent_pack = await _get_prompt_parent_pack(user.id, current_world["id"])
        if parent_pack:
            current["parent_pack"] = parent_pack

    return {
        "current": current,
        "products": products,
    }
