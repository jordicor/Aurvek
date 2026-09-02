from typing import Any

import orjson
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from auth import get_current_user
from captcha_service import get_captcha_config
from clients import async_twilio
from common import (
    GOOGLE_CLIENT_ID,
    PRIMARY_APP_DOMAIN,
    get_template_context,
    templates,
    twilio_messaging_service_sid,
)
from database import get_db_connection
from integrations.whatsapp.service import get_phone_user_not_found, set_phone_user_not_found
from log_config import logger
from models import User
from request_security import (
    ensure_csrf_token,
    validate_mutation_request,
)


router = APIRouter()


async def _load_active_whatsapp_users(conn: Any) -> list[dict[str, Any]]:
    """Return configured WhatsApp users with activity keyed by user identity."""

    cursor = await conn.execute(
        """
        SELECT u.id, u.username, u.phone_number, ud.external_platforms,
               MAX(w.timestamp) AS last_message
        FROM USERS u
        JOIN USER_DETAILS ud ON u.id = ud.user_id
        LEFT JOIN WHATSAPP_LOG w ON w.user_id = u.id
        WHERE ud.external_platforms IS NOT NULL
          AND ud.external_platforms != ''
        GROUP BY u.id, u.username, u.phone_number, ud.external_platforms
        ORDER BY LOWER(u.username), u.id
        """
    )
    rows = await cursor.fetchall()
    whatsapp_users = []
    for row in rows:
        try:
            platforms = orjson.loads(row[3])
        except (orjson.JSONDecodeError, TypeError):
            continue
        if not isinstance(platforms, dict):
            continue
        whatsapp = platforms.get("whatsapp")
        if not isinstance(whatsapp, dict) or not whatsapp:
            continue

        phone = str(row[2] or "")
        phone_display = (
            phone[:4] + "***" + phone[-4:]
            if len(phone) > 8
            else phone
        )
        whatsapp_users.append(
            {
                "username": row[1],
                "phone_display": phone_display,
                "conversation_id": whatsapp.get("conversation_id", "N/A"),
                "answer_mode": whatsapp.get("answer", "text"),
                "last_message": row[4] or None,
            }
        )
    return whatsapp_users


# ============================================================================
# WhatsApp Admin Dashboard
# ============================================================================

@router.get("/admin/whatsapp", response_class=HTMLResponse)
async def admin_whatsapp(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    message = request.query_params.get("message")
    error = request.query_params.get("error")

    # Check Twilio configuration status
    twilio_configured = async_twilio is not None

    # Check Twilio webhook configuration
    webhook_status = None  # None = not checkable, dict otherwise
    if async_twilio and twilio_messaging_service_sid:
        expected_webhook_url = f"https://{PRIMARY_APP_DOMAIN}/whatsapp"
        try:
            ms_data = await async_twilio.get_messaging_service(twilio_messaging_service_sid)
            current_webhook_url = ms_data.get("inbound_request_url", "")
            webhook_status = {
                "current_url": current_webhook_url,
                "expected_url": expected_webhook_url,
                "match": current_webhook_url == expected_webhook_url,
                "service_name": ms_data.get("friendly_name", "Unknown"),
            }
        except Exception as e:
            logger.error(f"Failed to fetch Twilio Messaging Service config: {e}")
            webhook_status = {"error": str(e)}

    # Get configurable messages from SYSTEM_CONFIG
    unknown_user_message = get_phone_user_not_found()
    welcome_message = ""
    require_phone_verification = False
    retain_voice_notes = False
    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT key, value FROM SYSTEM_CONFIG WHERE key IN "
                "('whatsapp_unknown_user_message', 'whatsapp_welcome_message', "
                "'whatsapp_require_phone_verification', "
                "'whatsapp_retain_voice_notes')"
            )
            rows = await cursor.fetchall()
            for row in rows:
                if row[0] == 'whatsapp_unknown_user_message':
                    unknown_user_message = row[1] or get_phone_user_not_found()
                elif row[0] == 'whatsapp_welcome_message':
                    welcome_message = row[1] or ""
                elif row[0] == 'whatsapp_require_phone_verification':
                    require_phone_verification = row[1] == '1'
                elif row[0] == 'whatsapp_retain_voice_notes':
                    retain_voice_notes = str(row[1] or "").strip() == '1'
    except Exception as e:
        logger.error(f"Failed to load WhatsApp config from SYSTEM_CONFIG: {e}")

    # Get users with WhatsApp active
    whatsapp_users = []
    try:
        async with get_db_connection(readonly=True) as conn:
            whatsapp_users = await _load_active_whatsapp_users(conn)
    except Exception:
        logger.exception("Failed to load active WhatsApp users")

    # Get today's stats
    stats = {"messages_today": 0, "active_users_today": 0, "text_count": 0, "voice_count": 0}
    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM WHATSAPP_LOG WHERE timestamp >= date('now')"
            )
            row = await cursor.fetchone()
            stats["messages_today"] = row[0] if row else 0

            cursor = await conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM WHATSAPP_LOG WHERE timestamp >= date('now')"
            )
            row = await cursor.fetchone()
            stats["active_users_today"] = row[0] if row else 0

            cursor = await conn.execute(
                "SELECT response_mode, COUNT(*) FROM WHATSAPP_LOG WHERE timestamp >= date('now') AND direction = 'in' GROUP BY response_mode"
            )
            rows = await cursor.fetchall()
            for row in rows:
                if row[0] == 'text':
                    stats["text_count"] = row[1]
                elif row[0] == 'voice':
                    stats["voice_count"] = row[1]
    except Exception:
        pass  # WHATSAPP_LOG table might not exist

    context = await get_template_context(request, current_user)
    context.update({
        "message": message,
        "error": error,
        "twilio_configured": twilio_configured,
        "whatsapp_users": whatsapp_users,
        "unknown_user_message": unknown_user_message,
        "welcome_message": welcome_message,
        "require_phone_verification": require_phone_verification,
        "whatsapp_retain_voice_notes": retain_voice_notes,
        "stats": stats,
        "webhook_status": webhook_status,
        "admin_csrf_token": ensure_csrf_token(request),
    })
    return templates.TemplateResponse("admin_whatsapp.html", context)


@router.post("/admin/whatsapp", response_class=HTMLResponse)
async def admin_whatsapp_save(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    form = await request.form()
    csrf_rejection = validate_mutation_request(
        request,
        supplied_token=form.get("csrf_token"),
    )
    if csrf_rejection is not None:
        return csrf_rejection
    action = form.get("action")

    if action == "save_config":
        unknown_msg = form.get("unknown_user_message", "").strip()
        welcome_msg = form.get("welcome_message", "").strip()
        require_verification = "1" if form.get("require_phone_verification") else "0"
        retain_voice_notes = (
            "1" if form.get("whatsapp_retain_voice_notes") == "1" else "0"
        )

        try:
            async with get_db_connection() as conn:
                await conn.execute("""
                    INSERT INTO SYSTEM_CONFIG (key, value, description, updated_at)
                    VALUES ('whatsapp_unknown_user_message', ?, 'Message sent to unregistered WhatsApp users', CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """, (unknown_msg,))
                await conn.execute("""
                    INSERT INTO SYSTEM_CONFIG (key, value, description, updated_at)
                    VALUES ('whatsapp_welcome_message', ?, 'Welcome message for new WhatsApp conversations', CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """, (welcome_msg,))
                await conn.execute("""
                    INSERT INTO SYSTEM_CONFIG (key, value, description, updated_at)
                    VALUES ('whatsapp_require_phone_verification', ?, 'Require phone verification for WhatsApp', CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """, (require_verification,))
                await conn.execute("""
                    INSERT INTO SYSTEM_CONFIG (key, value, description, updated_at)
                    VALUES ('whatsapp_retain_voice_notes', ?, 'Retain original WhatsApp voice-note audio for future playback and processing', CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """, (retain_voice_notes,))
                await conn.commit()

            # Update the in-memory variable for unknown user message
            set_phone_user_not_found(unknown_msg)

            return RedirectResponse(url="/admin/whatsapp?message=Configuration saved successfully", status_code=303)
        except Exception as e:
            logger.error(f"Error saving WhatsApp config: {e}")
            return RedirectResponse(url="/admin/whatsapp?error=Failed to save configuration", status_code=303)

    if action == "fix_webhook":
        if not async_twilio or not twilio_messaging_service_sid:
            return RedirectResponse(url="/admin/whatsapp?error=Twilio not configured", status_code=303)
        expected_url = f"https://{PRIMARY_APP_DOMAIN}/whatsapp"
        try:
            await async_twilio.update_messaging_service(
                twilio_messaging_service_sid, inbound_request_url=expected_url
            )
            return RedirectResponse(
                url=f"/admin/whatsapp?message=Webhook URL updated to {expected_url}",
                status_code=303
            )
        except Exception as e:
            logger.error(f"Failed to update Twilio webhook URL: {e}")
            return RedirectResponse(
                url=f"/admin/whatsapp?error=Failed to update webhook: {e}",
                status_code=303
            )

    return RedirectResponse(url="/admin/whatsapp", status_code=303)
