from typing import Any

import jwt
from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from auth import get_current_user, unauthenticated_response
from auth_constants import SESSION_COOKIE_NAME
from common import SECRET_KEY, decode_jwt_cached, verify_token_expiration
from log_config import logger
from models import User
from rediscfg import check_rate_limit, get_rate_limit_status
from ai_runtime.context.warmup import (
    _build_chat_warmup_snapshot,
    _build_warmup_cache_key_from_state,
    _load_warmup_conversation_state,
    _sanitize_warmup_payload,
    _warmup_mode_from_model_ids,
)

from chat.services.warmup import (
    get_or_prepare as warmup_get_or_prepare,
    get_ttl_seconds as get_warmup_ttl_seconds,
    mark_error as mark_warmup_error,
    mark_skipped as mark_warmup_skipped,
)

router = APIRouter()


@router.post("/api/conversations/{conversation_id}/warmup")
async def warmup_conversation_context(
    request: Request,
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    payload: Any = Body(default={}),
):
    if current_user is None:
        return unauthenticated_response()

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return unauthenticated_response()

    try:
        jwt_payload = decode_jwt_cached(token, SECRET_KEY)
        if not verify_token_expiration(jwt_payload):
            return unauthenticated_response()
    except jwt.PyJWTError:
        return unauthenticated_response()

    activity_payload, payload_error = _sanitize_warmup_payload(payload)
    if payload_error:
        return JSONResponse(content={"success": False, "message": payload_error}, status_code=400)

    if not await check_rate_limit(current_user.id, action="chat_warmup", limit=30, window_minutes=1):
        mark_warmup_skipped()
        rate_status = await get_rate_limit_status(
            current_user.id,
            action="chat_warmup",
            limit=30,
            window_minutes=1,
        )
        return JSONResponse(
            content={
                "success": False,
                "status": "skipped",
                "reason": "rate_limited",
                "rate_limit": rate_status,
            },
            status_code=429,
        )

    state = await _load_warmup_conversation_state(conversation_id, current_user.id)
    if not state:
        mark_warmup_skipped()
        return JSONResponse(
            content={"success": False, "status": "skipped", "message": "Conversation not found."},
            status_code=404,
        )

    if state.get("locked"):
        mark_warmup_skipped()
        return JSONResponse(
            content={"success": False, "status": "skipped", "message": "Conversation is locked."},
            status_code=403,
        )

    multi_ai_model_ids = activity_payload["multi_ai_model_ids"]
    mode = _warmup_mode_from_model_ids(multi_ai_model_ids)
    cache_key = _build_warmup_cache_key_from_state(
        state,
        current_user.id,
        conversation_id,
        mode=mode,
        multi_ai_model_ids=multi_ai_model_ids,
    )

    try:
        snapshot, status = await warmup_get_or_prepare(
            cache_key,
            lambda: _build_chat_warmup_snapshot(
                conversation_id,
                current_user,
                state,
                cache_key,
                activity_payload,
            ),
        )
    except Exception:
        mark_warmup_error()
        logger.warning(
            "[warmup] Failed to prepare context for conversation_id=%s",
            conversation_id,
            exc_info=True,
        )
        return JSONResponse(
            content={
                "success": False,
                "status": "skipped",
                "reason": "prepare_failed",
            },
            status_code=200,
        )

    return JSONResponse(
        content={
            "success": True,
            "status": status,
            "ttl_seconds": get_warmup_ttl_seconds(),
            "conversation_id": conversation_id,
            "last_message_id": state.get("last_message_id") or 0,
            "context_count": (snapshot or {}).get("context_count", 0),
            "mode": mode,
        }
    )
