"""Marketplace analytics and creator earnings routes."""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from auth import get_current_user, unauthenticated_response
from captcha_service import get_captcha_config
from common import GOOGLE_CLIENT_ID, SECURE_COOKIES, get_template_context, templates
from database import get_db_connection
from marketplace.config import require_creator_tools_enabled, require_public_landings_enabled
from models import User
from rate_limiter import get_client_ip


router = APIRouter()


@router.get("/user/landing-analytics")
async def user_landing_analytics_page(request: Request, current_user: User = Depends(get_current_user)):
    """Render the landing page analytics dashboard."""
    require_creator_tools_enabled()

    if current_user is None:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "captcha": get_captcha_config(),
                "google_oauth_available": bool(GOOGLE_CLIENT_ID),
            },
        )

    if not await current_user.is_user and not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only users can access analytics")

    context = await get_template_context(request, current_user)
    return templates.TemplateResponse("user_landing_analytics.html", context)


@router.post("/api/analytics/track-visit")
async def track_landing_visit(request: Request):
    """Track a public prompt or pack landing-page visit."""
    require_public_landings_enabled()

    try:
        data = await request.json()
    except Exception:
        return JSONResponse(content={"error": "Invalid JSON"}, status_code=400)

    prompt_id = data.get("prompt_id")
    pack_id = data.get("pack_id")
    if prompt_id and pack_id:
        return JSONResponse(content={"error": "Cannot track both prompt_id and pack_id in a single visit"}, status_code=400)
    if not prompt_id and not pack_id:
        return JSONResponse(content={"error": "prompt_id or pack_id required"}, status_code=400)

    visitor_id = request.cookies.get("_aurvek_visitor")
    if not visitor_id:
        visitor_id = secrets.token_urlsafe(16)

    client_ip = get_client_ip(request)
    ip_hash = hashlib.sha256((client_ip + os.getenv("PEPPER", "aurvek")).encode()).hexdigest()[:16]
    page_path = data.get("page_path", "/")
    referrer = data.get("referrer", "")
    user_agent = request.headers.get("user-agent", "")[:500]

    async with get_db_connection() as conn:
        cursor = await conn.cursor()

        if pack_id:
            await cursor.execute(
                "SELECT 1 FROM PACKS WHERE id = ? AND status = 'published' AND is_public = 1",
                (pack_id,),
            )
            if not await cursor.fetchone():
                pack_id = None

        if prompt_id:
            await cursor.execute(
                "SELECT 1 FROM PROMPTS WHERE id = ? AND public = 1",
                (prompt_id,),
            )
            if not await cursor.fetchone():
                prompt_id = None

        if not pack_id and not prompt_id:
            return JSONResponse(content={"error": "Invalid or missing entity"}, status_code=400)

        if pack_id:
            await cursor.execute(
                """
                SELECT id FROM LANDING_PAGE_ANALYTICS
                WHERE pack_id = ? AND visitor_id = ?
                AND visit_timestamp > datetime('now', '-30 minutes')
                """,
                (pack_id, visitor_id),
            )
        else:
            await cursor.execute(
                """
                SELECT id FROM LANDING_PAGE_ANALYTICS
                WHERE prompt_id = ? AND visitor_id = ?
                AND visit_timestamp > datetime('now', '-30 minutes')
                """,
                (prompt_id, visitor_id),
            )

        if await cursor.fetchone():
            response = JSONResponse(content={"status": "already_tracked"})
        else:
            await cursor.execute(
                """
                INSERT INTO LANDING_PAGE_ANALYTICS
                (prompt_id, pack_id, visitor_id, page_path, referrer, user_agent, ip_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (prompt_id, pack_id, visitor_id, page_path, referrer, user_agent, ip_hash),
            )
            await conn.commit()
            response = JSONResponse(content={"status": "tracked"})

    if not request.cookies.get("_aurvek_visitor"):
        response.set_cookie(
            key="_aurvek_visitor",
            value=visitor_id,
            max_age=365 * 24 * 60 * 60,
            httponly=True,
            samesite="lax",
            secure=SECURE_COOKIES,
        )

    return response


@router.get("/api/user/landing-analytics")
async def get_landing_analytics(request: Request, current_user: User = Depends(get_current_user)):
    """Get landing page analytics summary for all prompts owned by the user."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_user and not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)

    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT
                p.id,
                p.name,
                COUNT(DISTINCT CASE WHEN a.visit_timestamp >= ? AND a.visit_timestamp < ? THEN a.visitor_id END) as today_visitors,
                COUNT(DISTINCT CASE WHEN a.visit_timestamp >= ? THEN a.visitor_id END) as week_visitors,
                COUNT(DISTINCT CASE WHEN a.visit_timestamp >= ? THEN a.visitor_id END) as month_visitors,
                COUNT(CASE WHEN a.visit_timestamp >= ? THEN 1 END) as month_visits,
                COUNT(CASE WHEN a.converted = 1 AND a.visit_timestamp >= ? THEN 1 END) as month_conversions
            FROM PROMPTS p
            LEFT JOIN LANDING_PAGE_ANALYTICS a ON p.id = a.prompt_id
            WHERE p.created_by_user_id = ?
            GROUP BY p.id, p.name
            ORDER BY month_visitors DESC
            """,
            (today, tomorrow, week_ago, month_ago, month_ago, month_ago, current_user.id),
        )

        prompts_data = []
        total_today = 0
        total_week = 0
        total_month = 0
        total_conversions = 0

        for row in await cursor.fetchall():
            prompt_id, name, today_v, week_v, month_v, month_visits, month_conv = row
            conversion_rate = (month_conv / month_visits * 100) if month_visits > 0 else 0

            prompts_data.append(
                {
                    "id": prompt_id,
                    "name": name,
                    "today_visitors": today_v or 0,
                    "week_visitors": week_v or 0,
                    "month_visitors": month_v or 0,
                    "month_visits": month_visits or 0,
                    "conversions": month_conv or 0,
                    "conversion_rate": round(conversion_rate, 1),
                }
            )

            total_today += today_v or 0
            total_week += week_v or 0
            total_month += month_v or 0
            total_conversions += month_conv or 0

        await cursor.execute(
            """
            SELECT a.referrer, COUNT(*) as count
            FROM LANDING_PAGE_ANALYTICS a
            JOIN PROMPTS p ON a.prompt_id = p.id
            WHERE p.created_by_user_id = ?
            AND a.visit_timestamp >= ?
            AND a.referrer IS NOT NULL AND a.referrer != ''
            GROUP BY a.referrer
            ORDER BY count DESC
            LIMIT 10
            """,
            (current_user.id, month_ago),
        )

        top_referrers = []
        for row in await cursor.fetchall():
            referrer = row[0]
            if len(referrer) > 50:
                referrer = referrer[:47] + "..."
            top_referrers.append({"referrer": referrer, "count": row[1]})

        await cursor.execute(
            """
            SELECT substr(a.visit_timestamp, 1, 10) as day, COUNT(*) as visits
            FROM LANDING_PAGE_ANALYTICS a
            JOIN PROMPTS p ON a.prompt_id = p.id
            WHERE p.created_by_user_id = ?
            AND a.visit_timestamp >= date('now', '-14 days')
            GROUP BY day
            ORDER BY day ASC
            """,
            (current_user.id,),
        )

        daily_visits = [{"date": row[0], "visits": row[1]} for row in await cursor.fetchall()]

    return JSONResponse(
        content={
            "summary": {
                "today_visitors": total_today,
                "week_visitors": total_week,
                "month_visitors": total_month,
                "total_conversions": total_conversions,
            },
            "prompts": prompts_data,
            "top_referrers": top_referrers,
            "daily_visits": daily_visits,
        }
    )


@router.get("/api/user/landing-analytics/{prompt_id}")
async def get_prompt_analytics(prompt_id: int, request: Request, current_user: User = Depends(get_current_user)):
    """Get detailed analytics for a specific prompt."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_user and not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)

    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()

        await cursor.execute(
            "SELECT name FROM PROMPTS WHERE id = ? AND created_by_user_id = ?",
            (prompt_id, current_user.id),
        )
        prompt = await cursor.fetchone()
        if not prompt:
            return JSONResponse(content={"error": "Prompt not found"}, status_code=404)

        prompt_name = prompt[0]
        await cursor.execute(
            """
            SELECT
                COUNT(*) as total_visits,
                COUNT(DISTINCT visitor_id) as unique_visitors,
                COUNT(CASE WHEN converted = 1 THEN 1 END) as conversions
            FROM LANDING_PAGE_ANALYTICS
            WHERE prompt_id = ? AND date(visit_timestamp) >= ?
            """,
            (prompt_id, month_ago),
        )
        stats = await cursor.fetchone()

        await cursor.execute(
            """
            SELECT
                date(visit_timestamp) as day,
                COUNT(*) as visits,
                COUNT(DISTINCT visitor_id) as visitors,
                COUNT(CASE WHEN converted = 1 THEN 1 END) as conversions
            FROM LANDING_PAGE_ANALYTICS
            WHERE prompt_id = ? AND date(visit_timestamp) >= date('now', '-30 days')
            GROUP BY day
            ORDER BY day DESC
            """,
            (prompt_id,),
        )
        daily_data = [
            {"date": row[0], "visits": row[1], "visitors": row[2], "conversions": row[3]}
            for row in await cursor.fetchall()
        ]

        await cursor.execute(
            """
            SELECT referrer, COUNT(*) as count
            FROM LANDING_PAGE_ANALYTICS
            WHERE prompt_id = ? AND date(visit_timestamp) >= ?
            AND referrer IS NOT NULL AND referrer != ''
            GROUP BY referrer
            ORDER BY count DESC
            LIMIT 15
            """,
            (prompt_id, month_ago),
        )
        referrers = [{"referrer": row[0], "count": row[1]} for row in await cursor.fetchall()]

    conversion_rate = (stats[2] / stats[0] * 100) if stats[0] > 0 else 0
    return JSONResponse(
        content={
            "prompt_id": prompt_id,
            "prompt_name": prompt_name,
            "stats": {
                "total_visits": stats[0] or 0,
                "unique_visitors": stats[1] or 0,
                "conversions": stats[2] or 0,
                "conversion_rate": round(conversion_rate, 1),
            },
            "daily": daily_data,
            "referrers": referrers,
        }
    )


@router.get("/api/user/pack-landing-analytics")
async def get_pack_landing_analytics(current_user: User = Depends(get_current_user)):
    """Get landing page analytics for packs owned by the current user."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_admin and not await current_user.is_user:
        raise HTTPException(status_code=403, detail="Not authorized")

    async with get_db_connection(readonly=True) as conn:
        if await current_user.is_admin:
            rows = await conn.execute(
                """
                SELECT p.id, p.name,
                    COUNT(DISTINCT CASE WHEN a.visit_timestamp > datetime('now', '-1 day') THEN a.visitor_id END) as today_visitors,
                    COUNT(DISTINCT CASE WHEN a.visit_timestamp > datetime('now', '-7 days') THEN a.visitor_id END) as week_visitors,
                    COUNT(DISTINCT CASE WHEN a.visit_timestamp > datetime('now', '-30 days') THEN a.visitor_id END) as month_visitors,
                    COUNT(CASE WHEN a.visit_timestamp > datetime('now', '-30 days') THEN a.id END) as month_visits,
                    SUM(CASE WHEN a.converted = 1 AND a.visit_timestamp > datetime('now', '-30 days') THEN 1 ELSE 0 END) as conversions
                FROM PACKS p
                LEFT JOIN LANDING_PAGE_ANALYTICS a ON a.pack_id = p.id
                GROUP BY p.id, p.name
                ORDER BY month_visitors DESC
                """
            )
        else:
            rows = await conn.execute(
                """
                SELECT p.id, p.name,
                    COUNT(DISTINCT CASE WHEN a.visit_timestamp > datetime('now', '-1 day') THEN a.visitor_id END) as today_visitors,
                    COUNT(DISTINCT CASE WHEN a.visit_timestamp > datetime('now', '-7 days') THEN a.visitor_id END) as week_visitors,
                    COUNT(DISTINCT CASE WHEN a.visit_timestamp > datetime('now', '-30 days') THEN a.visitor_id END) as month_visitors,
                    COUNT(CASE WHEN a.visit_timestamp > datetime('now', '-30 days') THEN a.id END) as month_visits,
                    SUM(CASE WHEN a.converted = 1 AND a.visit_timestamp > datetime('now', '-30 days') THEN 1 ELSE 0 END) as conversions
                FROM PACKS p
                LEFT JOIN LANDING_PAGE_ANALYTICS a ON a.pack_id = p.id
                WHERE p.created_by_user_id = ?
                GROUP BY p.id, p.name
                ORDER BY month_visitors DESC
                """,
                (current_user.id,),
            )

        packs = []
        for row in await rows.fetchall():
            month_visits = row[5] or 0
            conversions = row[6] or 0
            packs.append(
                {
                    "id": row[0],
                    "name": row[1],
                    "today_visitors": row[2] or 0,
                    "week_visitors": row[3] or 0,
                    "month_visitors": row[4] or 0,
                    "month_visits": month_visits,
                    "conversions": conversions,
                    "conversion_rate": round((conversions / month_visits * 100) if month_visits > 0 else 0, 1),
                }
            )

        return {"packs": packs}


@router.get("/api/user/pack-landing-analytics/{pack_id}")
async def get_pack_analytics_detail(pack_id: int, current_user: User = Depends(get_current_user)):
    """Get detailed analytics for a specific pack landing page."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_admin and not await current_user.is_user:
        raise HTTPException(status_code=403, detail="Not authorized")

    async with get_db_connection(readonly=True) as conn:
        if not await current_user.is_admin:
            pack = await conn.execute("SELECT created_by_user_id FROM PACKS WHERE id = ?", (pack_id,))
            pack_row = await pack.fetchone()
            if not pack_row or pack_row[0] != current_user.id:
                raise HTTPException(status_code=404, detail="Pack not found")

        pack_info = await conn.execute("SELECT name FROM PACKS WHERE id = ?", (pack_id,))
        pack_name_row = await pack_info.fetchone()
        if not pack_name_row:
            raise HTTPException(status_code=404, detail="Pack not found")

        stats = await (
            await conn.execute(
                """
                SELECT
                    COUNT(id) as total_visits,
                    COUNT(DISTINCT visitor_id) as unique_visitors,
                    SUM(CASE WHEN converted = 1 THEN 1 ELSE 0 END) as conversions
                FROM LANDING_PAGE_ANALYTICS WHERE pack_id = ?
                """,
                (pack_id,),
            )
        ).fetchone()

        total = stats[0] or 0
        unique = stats[1] or 0
        convs = stats[2] or 0

        daily_rows = await (
            await conn.execute(
                """
                SELECT DATE(visit_timestamp) as date,
                    COUNT(id) as visits,
                    COUNT(DISTINCT visitor_id) as visitors,
                    SUM(CASE WHEN converted = 1 THEN 1 ELSE 0 END) as conversions
                FROM LANDING_PAGE_ANALYTICS
                WHERE pack_id = ? AND visit_timestamp > datetime('now', '-30 days')
                GROUP BY DATE(visit_timestamp)
                ORDER BY date DESC
                """,
                (pack_id,),
            )
        ).fetchall()

        ref_rows = await (
            await conn.execute(
                """
                SELECT COALESCE(referrer, 'direct') as referrer, COUNT(*) as count
                FROM LANDING_PAGE_ANALYTICS
                WHERE pack_id = ? AND visit_timestamp > datetime('now', '-30 days')
                GROUP BY referrer
                ORDER BY count DESC
                LIMIT 10
                """,
                (pack_id,),
            )
        ).fetchall()

        return {
            "pack_id": pack_id,
            "pack_name": pack_name_row[0],
            "stats": {
                "total_visits": total,
                "unique_visitors": unique,
                "conversions": convs,
                "conversion_rate": round((convs / total * 100) if total > 0 else 0, 1),
            },
            "daily": [
                {"date": r[0], "visits": r[1], "visitors": r[2], "conversions": r[3]}
                for r in daily_rows
            ],
            "referrers": [{"referrer": r[0], "count": r[1]} for r in ref_rows],
        }


@router.get("/my-earnings")
async def my_earnings_page(request: Request, current_user: User = Depends(get_current_user)):
    """Render the creator earnings dashboard page."""
    require_creator_tools_enabled()

    if current_user is None:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "captcha": get_captcha_config(),
                "google_oauth_available": bool(GOOGLE_CLIENT_ID),
            },
        )

    if not await current_user.is_user and not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only creators can access earnings dashboard")

    context = await get_template_context(request, current_user)
    return templates.TemplateResponse("creator_earnings.html", context)


@router.get("/api/my-earnings")
async def get_my_earnings(request: Request, current_user: User = Depends(get_current_user)):
    """Get creator earnings data for the dashboard."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_user and not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT pending_earnings FROM USER_DETAILS WHERE user_id = ?",
            (current_user.id,),
        )
        result = await cursor.fetchone()
        pending_earnings = float(result[0] or 0) if result else 0

        cursor = await conn.execute(
            "SELECT COALESCE(SUM(net_earnings), 0) FROM CREATOR_EARNINGS WHERE creator_id = ?",
            (current_user.id,),
        )
        result = await cursor.fetchone()
        total_earned = float(result[0] or 0)

        cursor = await conn.execute(
            """
            SELECT COALESCE(SUM(net_earnings), 0) FROM CREATOR_EARNINGS
            WHERE creator_id = ? AND created_at >= date('now', 'start of month')
            """,
            (current_user.id,),
        )
        result = await cursor.fetchone()
        this_month = float(result[0] or 0)

        cursor = await conn.execute(
            """
            SELECT
                ce.prompt_id,
                p.name as prompt_name,
                p.is_paid,
                COUNT(DISTINCT ce.consumer_id) as unique_users,
                SUM(ce.tokens_consumed) as total_tokens,
                SUM(ce.net_earnings) as total_earned,
                p.purchase_price
            FROM CREATOR_EARNINGS ce
            JOIN PROMPTS p ON ce.prompt_id = p.id
            WHERE ce.creator_id = ?
            GROUP BY ce.prompt_id
            ORDER BY total_earned DESC
            LIMIT 20
            """,
            (current_user.id,),
        )
        rows = await cursor.fetchall()
        by_prompt = [
            {
                "prompt_id": row[0],
                "prompt_name": row[1],
                "is_paid": bool(row[2]),
                "unique_users": row[3],
                "total_tokens": row[4],
                "total_earned": float(row[5] or 0),
                "purchase_price": float(row[6]) if row[6] is not None else None,
            }
            for row in rows
        ]

        cursor = await conn.execute(
            """
            SELECT
                ce.id,
                p.name as prompt_name,
                ce.net_earnings,
                ce.created_at
            FROM CREATOR_EARNINGS ce
            JOIN PROMPTS p ON ce.prompt_id = p.id
            WHERE ce.creator_id = ?
            ORDER BY ce.created_at DESC
            LIMIT 10
            """,
            (current_user.id,),
        )
        rows = await cursor.fetchall()
        recent = [
            {
                "id": row[0],
                "prompt_name": row[1],
                "net_earnings": float(row[2] or 0),
                "created_at": row[3],
            }
            for row in rows
        ]

    return JSONResponse(
        content={
            "total_earned": total_earned,
            "this_month": this_month,
            "pending_earnings": pending_earnings,
            "by_prompt": by_prompt,
            "recent": recent,
        }
    )
