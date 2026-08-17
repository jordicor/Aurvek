import orjson
from typing import Optional

from fastapi import Request
from fastapi.responses import HTMLResponse

from common import templates
from database import get_db_connection
from log_config import logger
from marketplace.config import marketplace_checkout_enabled, marketplace_public_landings_enabled
from marketplace.services.entitlements import (
    grant_pack_entitlement,
    user_has_pack_access as user_has_pack_entitlement_access,
)
from marketplace.services.landing_registration import (
    DEFAULT_LANDING_REGISTRATION_CONFIG,
    strip_personal_subscription_default,
)


async def get_prompt_for_registration(public_id: str) -> Optional[dict]:
    """
    Get prompt info for registration/login page context.
    Returns None if prompt not found.
    """
    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT p.id, p.name, p.description, p.image, p.public_id,
                       u.username as owner_username
                FROM PROMPTS p
                JOIN USERS u ON p.created_by_user_id = u.id
                WHERE p.public_id = ?
                """,
                (public_id,),
            )
            result = await cursor.fetchone()

        if not result:
            return None

        return {
            "id": result[0],
            "name": result[1],
            "description": result[2],
            "image": result[3],
            "public_id": result[4],
            "owner_username": result[5],
        }
    except Exception as e:
        logger.error(f"Error getting prompt for registration: {e}")
        return None


async def resolve_pack_oauth_context(pack_id):
    """
    Resolve pack context for Google OAuth registration.
    Returns tuple: pack_id, first_prompt_id, is_paid, landing_config,
    pack_owner_id, paid_pack_landing_url.
    """
    if not marketplace_public_landings_enabled() or not marketplace_checkout_enabled():
        return (None, None, False, None, None, None)

    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT landing_reg_config, created_by_user_id, status, is_public, is_paid, public_id, slug FROM PACKS WHERE id = ?",
                (pack_id,),
            )
            pack_config_row = await cursor.fetchone()

            if not pack_config_row or pack_config_row[2] != "published" or not pack_config_row[3]:
                return (None, None, False, None, None, None)

            if pack_config_row[4]:
                paid_pack_landing_url = f"/pack/{pack_config_row[5]}/{pack_config_row[6]}/"
                return (pack_id, None, True, None, None, paid_pack_landing_url)

            prompt_cursor = await conn.execute(
                """SELECT prompt_id FROM PACK_ITEMS
                   WHERE pack_id = ? AND is_active = 1
                   AND (disable_at IS NULL OR disable_at > datetime('now'))
                   ORDER BY display_order ASC LIMIT 1""",
                (pack_id,),
            )
            active_prompt = await prompt_cursor.fetchone()
            if not active_prompt:
                return (None, None, False, None, None, None)

            first_prompt_id = active_prompt[0]
            landing_config = DEFAULT_LANDING_REGISTRATION_CONFIG.copy()
            if pack_config_row[0]:
                stored_config = orjson.loads(pack_config_row[0])
                landing_config.update(stored_config)
            landing_config = await strip_personal_subscription_default(landing_config)

            pack_owner_id = pack_config_row[1] if landing_config.get("billing_mode") == "user_pays" else None
            return (pack_id, first_prompt_id, False, landing_config, pack_owner_id, None)
    except Exception as e:
        logger.warning(f"Could not resolve pack OAuth context for pack {pack_id}: {e}")
        return (None, None, False, None, None, None)


async def handle_pack_for_existing_user(pack_id, user_id):
    """
    Handle pack access for an existing user during acquisition login.
    Returns redirect_url if user needs to purchase a paid pack, else None.
    """
    if not marketplace_public_landings_enabled() or not marketplace_checkout_enabled():
        return None

    resolved = await resolve_pack_oauth_context(pack_id)
    r_pack_id, _r_prompt_id, r_is_paid, _r_config, _r_owner_id, r_paid_url = resolved

    if r_pack_id is None:
        return None

    async with get_db_connection(readonly=True) as conn:
        has_access = await user_has_pack_entitlement_access(
            conn,
            user_id=user_id,
            pack_id=pack_id,
        )

    if has_access:
        return None

    if r_is_paid:
        return r_paid_url

    try:
        async with get_db_connection() as conn:
            await grant_pack_entitlement(
                conn,
                user_id=user_id,
                pack_id=pack_id,
                source="oauth_acquisition",
                source_ref_type="oauth_pack",
                source_ref_id=f"{user_id}:{pack_id}",
                metadata={"provider": "google"},
            )
            await conn.commit()
        logger.info(f"Granted pack access via acquisition for existing user: user_id={user_id}, pack_id={pack_id}")

        try:
            async with get_db_connection() as ucr_conn:
                ucr_cursor = await ucr_conn.cursor()
                pack_cr = await ucr_cursor.execute(
                    "SELECT created_by_user_id FROM PACKS WHERE id = ?",
                    (pack_id,),
                )
                pack_cr_row = await pack_cr.fetchone()
                if pack_cr_row and pack_cr_row[0]:
                    from common import upsert_creator_relationship

                    await upsert_creator_relationship(
                        ucr_cursor,
                        user_id,
                        pack_cr_row[0],
                        "purchased_from",
                        "pack",
                        pack_id,
                    )
                    await ucr_conn.commit()
        except Exception as ucr_err:
            logger.warning(f"Could not record creator relationship for pack {pack_id}, user {user_id}: {ucr_err}")
    except Exception as pack_err:
        logger.error(f"Failed to grant pack access via acquisition for existing user: {pack_err}")

    return None


async def render_custom_domain_register(
    request: Request,
    *,
    captcha: dict,
    google_oauth_available: bool,
) -> HTMLResponse:
    public_id = request.state.public_id
    prompt = await get_prompt_for_registration(public_id)
    if not prompt:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Prompt not found")

    response = templates.TemplateResponse(
        "register_public.html",
        {
            "request": request,
            "target_role": "customer",
            "prompt": prompt,
            "login_url": "/login",
            "captcha": captcha,
            "google_oauth_available": google_oauth_available,
        },
    )
    response.headers["X-Robots-Tag"] = "noindex"
    return response


async def render_custom_domain_login(
    request: Request,
    *,
    login_handler,
) -> HTMLResponse:
    public_id = request.state.public_id
    prompt = await get_prompt_for_registration(public_id)
    if not prompt:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Prompt not found")

    response = await login_handler(
        request,
        prompt_context=prompt,
        login_url="/login",
        register_url="/register",
    )
    response.headers["X-Robots-Tag"] = "noindex"
    return response
