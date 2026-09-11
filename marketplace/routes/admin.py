"""Admin routes for marketplace runtime controls."""

from __future__ import annotations

import json
from typing import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from i18n import get_translator
from marketplace.services.admin_localization import localized_config, FLAG_KEYS, CONFIG_ERRORS
from auth import get_current_user, unauthenticated_response
from captcha_service import get_captcha_config
from common import GOOGLE_CLIENT_ID, get_template_context, templates
from database import get_db_connection
from log_config import logger
from models import User
from marketplace.config import (
    MARKETPLACE_FLAG_DEFINITIONS,
    MarketplaceConfigError,
    marketplace_config_has_env_override,
    marketplace_config_value_to_text,
    normalize_marketplace_config_updates,
    update_marketplace_config_values,
)
from marketplace.runtime import (
    load_marketplace_config_from_db,
    system_config_columns,
    upsert_system_config_value,
)

LogAdminAction = Callable[..., Awaitable[None]]


def create_router(log_admin_action: LogAdminAction) -> APIRouter:
    router = APIRouter()

    @router.get("/admin/marketplace", response_class=HTMLResponse)
    async def get_admin_marketplace_page(request: Request, current_user: User = Depends(get_current_user)):
        """Admin page for marketplace kill-switch controls."""
        translator = get_translator(request, current_user)
        if current_user is None:
            return templates.TemplateResponse(
                "login.html",
                {
                    "request": request,
                    "captcha": get_captcha_config(),
                    "google_oauth_available": bool(GOOGLE_CLIENT_ID),
                },
            )

        if not await current_user.is_admin:
            raise HTTPException(status_code=403, detail=translator.t("marketplace_admin.config.admin_required"))

        await load_marketplace_config_from_db()
        context = await get_template_context(request, current_user)
        context["marketplace_admin_config"] = localized_config(translator)
        return templates.TemplateResponse("marketplace/admin_marketplace.html", context)

    @router.get("/api/admin/marketplace-config")
    async def get_admin_marketplace_config(request: Request, current_user: User = Depends(get_current_user)):
        """Return marketplace kill-switch state for the admin dashboard."""
        translator = get_translator(request, current_user)
        if current_user is None:
            return unauthenticated_response()

        if not await current_user.is_admin:
            return JSONResponse(content={"success": False, "message": translator.t("marketplace_admin.config.admin_required")}, status_code=403)

        await load_marketplace_config_from_db()
        return JSONResponse(content={"success": True, "config": localized_config(translator)})

    @router.post("/api/admin/marketplace-config")
    async def update_admin_marketplace_config(
        data: dict,
        request: Request,
        current_user: User = Depends(get_current_user),
    ):
        """Persist marketplace kill-switch state from the admin dashboard."""
        translator = get_translator(request, current_user)
        if current_user is None:
            return unauthenticated_response()

        if not await current_user.is_admin:
            return JSONResponse(content={"success": False, "message": translator.t("marketplace_admin.config.admin_required")}, status_code=403)

        try:
            updates = normalize_marketplace_config_updates(data)
        except MarketplaceConfigError as exc:
            return JSONResponse(content={"success": False, "message": translator.t(CONFIG_ERRORS[exc.code])}, status_code=400)

        env_locked = [
            flag
            for flag in MARKETPLACE_FLAG_DEFINITIONS
            if flag.key in updates and marketplace_config_has_env_override(flag.key)
        ]
        if env_locked:
            return JSONResponse(
                content={
                    "success": False,
                    "message": translator.t("marketplace_admin.config.locked"),
                    "locked_flags": [
                        {"key": flag.key, "env_var": flag.env_var, "label": translator.t("marketplace_admin.flags." + FLAG_KEYS[flag.key] + ".label")}
                        for flag in env_locked
                    ],
                    "config": localized_config(translator),
                },
                status_code=409,
            )

        descriptions = {flag.key: flag.description for flag in MARKETPLACE_FLAG_DEFINITIONS}
        async with get_db_connection() as conn:
            columns = await system_config_columns(conn)
            for key, value in updates.items():
                await upsert_system_config_value(
                    conn,
                    columns,
                    key,
                    marketplace_config_value_to_text(value),
                    descriptions.get(key, "Marketplace runtime control."),
                )
            await conn.commit()

        update_marketplace_config_values(updates)
        await log_admin_action(
            admin_id=current_user.id,
            action_type="marketplace_config_update",
            request=request,
            target_resource_type="system_config",
            details=json.dumps(
                {key: marketplace_config_value_to_text(value) for key, value in sorted(updates.items())},
                sort_keys=True,
            ),
        )

        logger.info("Marketplace config updated by admin %s: %s", current_user.username, updates)
        return JSONResponse(
            content={
                "success": True,
                "message": translator.t("marketplace_admin.ui.marketplace_controls_saved"),
                "config": localized_config(translator),
            }
        )

    return router
