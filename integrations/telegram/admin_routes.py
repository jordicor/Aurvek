import orjson
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from auth import get_current_user
from captcha_service import get_captcha_config
from clients import async_telegram
from common import (
    GOOGLE_CLIENT_ID,
    PRIMARY_APP_DOMAIN,
    TELEGRAM_WEBHOOK_SECRET,
    get_template_context,
    templates,
)
from database import get_db_connection
from log_config import logger
from models import User
from request_security import (
    ensure_csrf_token,
    validate_mutation_request,
)


router = APIRouter()


# ============================================================================
# Telegram Admin
# ============================================================================

@router.get("/admin/telegram", response_class=HTMLResponse)
async def admin_telegram(request: Request, current_user: User = Depends(get_current_user)):
    """Admin dashboard for Telegram configuration."""
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    message = request.query_params.get("message")
    error = request.query_params.get("error")

    bot_info = None
    webhook_info = None
    expected_webhook_url = f"https://{PRIMARY_APP_DOMAIN}/telegram"
    if async_telegram:
        try:
            bot_info = await async_telegram.get_me()
            raw_wh = await async_telegram.get_webhook_info()
            current_url = raw_wh.get("url", "")
            webhook_info = {
                "current_url": current_url,
                "expected_url": expected_webhook_url,
                "match": current_url == expected_webhook_url,
            }
        except Exception as e:
            logger.error(f"Failed to get Telegram bot info: {e}")
            webhook_info = {"error": str(e)}

    # Get configurable messages from SYSTEM_CONFIG
    unknown_user_message = ""
    welcome_message = ""
    require_phone_verification = False
    retain_voice_notes = False
    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT key, value FROM SYSTEM_CONFIG WHERE key LIKE 'telegram_%'"
            )
            rows = await cursor.fetchall()
            config = {row[0]: row[1] for row in rows}
            unknown_user_message = config.get('telegram_unknown_user_message', '')
            welcome_message = config.get('telegram_welcome_message', '')
            require_phone_verification = config.get('telegram_require_phone_verification', '0') == '1'
            retain_voice_notes = (
                str(config.get('telegram_retain_voice_notes', '') or '').strip()
                == '1'
            )
    except Exception as e:
        logger.error(f"Failed to load Telegram config from SYSTEM_CONFIG: {e}")

    # Get active Telegram users
    telegram_users = []
    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute('''
                SELECT u.username, u.telegram_chat_id, ud.external_platforms
                FROM USERS u
                JOIN USER_DETAILS ud ON u.id = ud.user_id
                WHERE u.telegram_chat_id IS NOT NULL
            ''')
            rows = await cursor.fetchall()
            for row in rows:
                try:
                    platforms = orjson.loads(row[2]) if row[2] else {}
                    tg = platforms.get('telegram')
                    if tg:
                        chat_id_str = str(row[1]) if row[1] else ""
                        if len(chat_id_str) > 5:
                            chat_id_display = chat_id_str[:3] + "***" + chat_id_str[-2:]
                        else:
                            chat_id_display = chat_id_str

                        last_msg_cursor = await conn.execute(
                            "SELECT MAX(timestamp) FROM TELEGRAM_LOG WHERE chat_id = ?",
                            (row[1],)
                        )
                        last_msg_row = await last_msg_cursor.fetchone()
                        last_message = last_msg_row[0] if last_msg_row and last_msg_row[0] else None

                        telegram_users.append({
                            "username": row[0],
                            "chat_id_display": chat_id_display,
                            "conversation_id": tg.get("conversation_id", "N/A"),
                            "answer_mode": tg.get("answer", "text"),
                            "last_message": last_message
                        })
                except (orjson.JSONDecodeError, TypeError):
                    continue
    except Exception:
        pass

    # Get today's stats
    stats = {"messages_today": 0, "active_users_today": 0, "text_count": 0, "voice_count": 0}
    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM TELEGRAM_LOG WHERE timestamp >= date('now')"
            )
            row = await cursor.fetchone()
            stats["messages_today"] = row[0] if row else 0

            cursor = await conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM TELEGRAM_LOG WHERE timestamp >= date('now')"
            )
            row = await cursor.fetchone()
            stats["active_users_today"] = row[0] if row else 0

            cursor = await conn.execute(
                "SELECT response_mode, COUNT(*) FROM TELEGRAM_LOG WHERE timestamp >= date('now') AND direction = 'in' GROUP BY response_mode"
            )
            rows = await cursor.fetchall()
            for row in rows:
                if row[0] == 'text':
                    stats["text_count"] = row[1]
                elif row[0] == 'voice':
                    stats["voice_count"] = row[1]
    except Exception:
        pass

    context = await get_template_context(request, current_user)
    context.update({
        "message": message,
        "error": error,
        "telegram_configured": async_telegram is not None,
        "bot_info": bot_info,
        "webhook_info": webhook_info,
        "expected_webhook_url": expected_webhook_url,
        "telegram_users": telegram_users,
        "unknown_user_message": unknown_user_message,
        "welcome_message": welcome_message,
        "require_phone_verification": require_phone_verification,
        "telegram_retain_voice_notes": retain_voice_notes,
        "stats": stats,
        "admin_csrf_token": ensure_csrf_token(request),
    })
    return templates.TemplateResponse("admin_telegram.html", context)


@router.post("/admin/telegram", response_class=HTMLResponse)
async def admin_telegram_save(request: Request, current_user: User = Depends(get_current_user)):
    """Save Telegram admin configuration."""
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    if async_telegram is None:
        return RedirectResponse(url="/admin/telegram?error=Telegram bot not configured. Set TELEGRAM_BOT_TOKEN in .env", status_code=303)

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
            "1" if form.get("telegram_retain_voice_notes") == "1" else "0"
        )

        try:
            async with get_db_connection() as conn:
                await conn.execute("""
                    INSERT INTO SYSTEM_CONFIG (key, value, description, updated_at)
                    VALUES ('telegram_unknown_user_message', ?, 'Message sent to unregistered Telegram users', CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """, (unknown_msg,))
                await conn.execute("""
                    INSERT INTO SYSTEM_CONFIG (key, value, description, updated_at)
                    VALUES ('telegram_welcome_message', ?, 'Welcome message for new Telegram conversations', CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """, (welcome_msg,))
                await conn.execute("""
                    INSERT INTO SYSTEM_CONFIG (key, value, description, updated_at)
                    VALUES ('telegram_require_phone_verification', ?, 'Require phone verification for Telegram', CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """, (require_verification,))
                await conn.execute("""
                    INSERT INTO SYSTEM_CONFIG (key, value, description, updated_at)
                    VALUES ('telegram_retain_voice_notes', ?, 'Retain original Telegram voice-note audio for future playback and processing', CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """, (retain_voice_notes,))
                await conn.commit()

            return RedirectResponse(url="/admin/telegram?message=Configuration saved successfully", status_code=303)
        except Exception as e:
            logger.error(f"Error saving Telegram config: {e}")
            return RedirectResponse(url="/admin/telegram?error=Failed to save configuration", status_code=303)

    if action == "fix_webhook":
        webhook_url = f"https://{PRIMARY_APP_DOMAIN}/telegram"
        try:
            await async_telegram.set_webhook(webhook_url, TELEGRAM_WEBHOOK_SECRET)
            return RedirectResponse(
                url=f"/admin/telegram?message=Webhook URL updated to {webhook_url}",
                status_code=303
            )
        except Exception as e:
            logger.error(f"Failed to update Telegram webhook: {e}")
            return RedirectResponse(
                url=f"/admin/telegram?error=Failed to update webhook: {e}",
                status_code=303
            )

    return RedirectResponse(url="/admin/telegram", status_code=303)
