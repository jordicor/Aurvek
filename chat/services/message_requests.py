import jwt
from jwt import PyJWTError as JWTError
from fastapi import Request
from fastapi.responses import JSONResponse

from auth_constants import SESSION_COOKIE_NAME
from common import SECRET_KEY, decode_jwt_cached, verify_token_expiration
from log_config import logger
from models import User
from rediscfg import (
    check_rate_limit,
    get_rate_limit_status,
    increment_metric,
    increment_user_activity,
)
from wellbeing_service import get_active_pause


async def validate_message_request(
    request: Request,
    current_user: User,
    is_whatsapp: bool = False,
):
    """Validate auth/session/rate limits for message endpoints."""
    if current_user is None:
        return JSONResponse(content={"redirect": "/login"}, status_code=401)

    if not is_whatsapp:
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if not token:
            logger.debug("no token!")
            return JSONResponse(content={"redirect": "/login"}, status_code=401)

        try:
            payload = decode_jwt_cached(token, SECRET_KEY)
            logger.info("payload: %s", payload)

            if not verify_token_expiration(payload):
                logger.debug("token expired")
                return JSONResponse(content={"redirect": "/login"}, status_code=401)

        except (JWTError, jwt.PyJWTError):
            return JSONResponse(content={"redirect": "/login"}, status_code=401)

    active_pause = await get_active_pause(current_user.id)
    if active_pause:
        pause_reason = active_pause.get("reason") or "pause_active"
        return JSONResponse(
            content={
                "error": "wellbeing_pause_active" if pause_reason == "pause_active" else "wellbeing_pause_required",
                "message": active_pause.get("message") or "A break pause is required before continuing.",
                "pause_until": active_pause.get("pause_until"),
                "session_id": active_pause.get("session_id"),
                "reason": pause_reason,
            },
            status_code=429,
        )

    if not await check_rate_limit(current_user.id, action="ai_call", limit=120, window_minutes=1):
        rate_status = await get_rate_limit_status(current_user.id, action="ai_call", limit=120, window_minutes=1)
        logger.warning("Rate limit exceeded for user %s", current_user.id)
        return JSONResponse(
            content={
                "error": "Rate limit exceeded",
                "message": f"Too many AI requests. Limit: {rate_status['limit']} per minute. Current: {rate_status['current']}",
                "rate_limit": rate_status,
            },
            status_code=429,
        )

    await increment_metric("ai_requests_total")
    await increment_user_activity(current_user.id)
    return None
