"""Marketplace ranking admin routes."""

from __future__ import annotations

import asyncio
import json
import math

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from auth import get_current_user, unauthenticated_response
from common import get_template_context, templates
from database import get_db_connection
from log_config import logger
from models import User
from i18n import get_translator
from ranking import (
    get_ranking_config,
    invalidate_ranking_config_cache,
    recalculate_ranking_scores,
)


router = APIRouter()


@router.get("/admin/ranking", response_class=HTMLResponse)
async def admin_ranking_page(request: Request, current_user: User = Depends(get_current_user)):
    """Admin page for configuring ranking weights and mode."""
    t = get_translator(request, current_user).t
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail=t("management_operations_errors.admin_required"))

    ranking_config = await get_ranking_config()
    context = await get_template_context(request, current_user)
    context["ranking_config"] = ranking_config
    return templates.TemplateResponse("admin_ranking.html", context)


@router.get("/api/admin/ranking-config")
async def api_get_ranking_config(request: Request, current_user: User = Depends(get_current_user)):
    """Get current ranking configuration."""
    t = get_translator(request, current_user).t
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": t("management_operations_errors.admin_required")})

    config = await get_ranking_config()
    return JSONResponse(content={"success": True, "config": config})


@router.put("/api/admin/ranking-config")
async def api_update_ranking_config(request: Request, current_user: User = Depends(get_current_user)):
    """Update ranking configuration."""
    t = get_translator(request, current_user).t
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": t("management_operations_errors.admin_required")})

    try:
        data = await request.json()
        if not isinstance(data, dict):
            return JSONResponse(status_code=400, content={"success": False, "message": t("admin_ranking.error.invalid_request")})

        if "mode" in data and data["mode"] not in ("piggyback", "scheduled"):
            return JSONResponse(status_code=400, content={"success": False, "message": t("admin_ranking.error.invalid_mode")})
        if "interval_hours" in data:
            try:
                interval = int(data["interval_hours"])
                if isinstance(data["interval_hours"], bool) or not 1 <= interval <= 168:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                return JSONResponse(status_code=400, content={"success": False, "message": t("admin_ranking.error.invalid_interval")})
        if "weights" in data:
            weights = data["weights"]
            if not isinstance(weights, dict):
                return JSONResponse(status_code=400, content={"success": False, "message": t("admin_ranking.error.invalid_request")})
            weight_labels = {"users_with_access", "purchases", "unique_chatters", "landing_conversions", "favorites", "has_landing_boost", "recency_max_bonus"}
            for key, value in weights.items():
                try:
                    numeric_value = float(value)
                    if isinstance(value, bool) or not math.isfinite(numeric_value) or not 0 <= numeric_value <= 1000:
                        raise ValueError
                except (TypeError, ValueError, OverflowError):
                    metric = t("admin_ranking.weight." + key) if key in weight_labels else key
                    return JSONResponse(status_code=400, content={"success": False, "message": t("admin_ranking.error.invalid_weight", metric=metric)})

        async with get_db_connection() as conn:
            cursor = await conn.cursor()

            if "mode" in data:
                mode = data["mode"]
                await cursor.execute(
                    "UPDATE SYSTEM_CONFIG SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = 'ranking_mode'",
                    (mode,),
                )

            if "interval_hours" in data:
                await cursor.execute(
                    "UPDATE SYSTEM_CONFIG SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = 'ranking_interval_hours'",
                    (str(interval),),
                )

            if "weights" in data:
                await cursor.execute(
                    "UPDATE SYSTEM_CONFIG SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = 'ranking_weights'",
                    (json.dumps(weights),),
                )

            await conn.commit()

        invalidate_ranking_config_cache()
        return JSONResponse(content={"success": True, "message": t("admin_ranking.notice.updated")})
    except Exception as exc:
        logger.error("Error updating ranking config: %s", exc)
        return JSONResponse(status_code=500, content={"success": False, "message": t("admin_ranking.error.save")})


@router.post("/api/admin/ranking-recalculate")
async def api_ranking_recalculate(request: Request, current_user: User = Depends(get_current_user)):
    """Trigger manual ranking recalculation."""
    t = get_translator(request, current_user).t
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": t("management_operations_errors.admin_required")})

    asyncio.create_task(recalculate_ranking_scores())
    return JSONResponse(content={"success": True, "message": t("admin_ranking.recalculation_started")})
