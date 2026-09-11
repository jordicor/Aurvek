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
from chat.services.localization import chat_text


def unavailable_model_response(llm_id: int, model: str, current_user=None) -> JSONResponse:
    """Reject a retired model before a turn reaches inference or persistence."""
    return JSONResponse(
        content={
            "success": False,
            "error_code": "model_unavailable",
            "message": chat_text(current_user, "model_unavailable"),
            "llm_id": int(llm_id),
            "model": model,
            "llm_enabled": False,
        },
        status_code=409,
    )


async def validate_message_request(
    request: Request,
    current_user: User,
    is_whatsapp: bool = False,
):
    """Validate auth/session/rate limits for message endpoints."""
    if current_user is None:
        return JSONResponse(content={"redirect": "/login"}, status_code=401)

    from integrations.embed.context import get_embed_principal, is_embed_request
    if is_embed_request(request):
        principal = get_embed_principal(request)
        if principal is None or principal.user_id != current_user.id:
            return JSONResponse(content={"error": "unauthenticated"}, status_code=401)
        if is_whatsapp or not principal.capabilities.get("text", False):
            return JSONResponse(content={"error": "capability_disabled"}, status_code=403)
        from request_security import validate_mutation_request
        rejection = validate_mutation_request(request)
        if rejection is not None:
            return rejection
        # Optional native controls require the delegated capability. The shared
        # runtime still validates model restrictions and reasoning budgets.
        form = await request.form()
        if form.get("multi_ai_models") and not principal.capabilities.get("multi_ai", False):
            return JSONResponse(content={"error": "capability_disabled"}, status_code=403)
        if any(form.get(key) for key in (
            "reasoning_mode", "reasoning_budget_tokens", "thinking_budget_tokens",
        )) and not principal.capabilities.get("reasoning", False):
            return JSONResponse(content={"error": "capability_disabled"}, status_code=403)
        if not principal.capabilities.get("attachments", False):
            if form.get("attachment_refs") or any(getattr(item, "filename", "") for item in form.getlist("file")):
                return JSONResponse(content={"error": "capability_disabled"}, status_code=403)
    elif not is_whatsapp:
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if not token:
            logger.debug("no token!")
            return JSONResponse(content={"redirect": "/login"}, status_code=401)

        try:
            payload = decode_jwt_cached(token, SECRET_KEY)
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
                "message": chat_text(current_user, "wellbeing_pause_active" if pause_reason == "pause_active" else "wellbeing_pause_required"),
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
                "error_code": "rate_limit_exceeded",
                "message": chat_text(current_user, "rate_limit_exceeded"),
                "rate_limit": rate_status,
            },
            status_code=429,
        )

    await increment_metric("ai_requests_total")
    await increment_user_activity(current_user.id)
    return None
