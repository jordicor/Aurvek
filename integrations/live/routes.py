"""Native browser and device connections; provider credentials never leave Aurvek."""

import asyncio
import contextlib
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from database import get_db_connection
from integrations.applications.browser_voice import _http_request
from integrations.embed.context import get_embed_principal, get_embed_user, is_embed_request
from integrations.embed.models import EmbedError
from log_config import logger
from .configuration import configuration
from .session import LiveMediaSession

router = APIRouter()
_active_conversations = set()


@router.get("/sdk/native-live-worklet.js", include_in_schema=False)
async def native_live_worklet():
    # Keep AudioWorklet loading on the page origin; /static/ may redirect to a CDN.
    return FileResponse(
        Path(__file__).resolve().parents[2] / "data/static/js/chat/native-live.js",
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=3600, must-revalidate",
                 "X-Content-Type-Options": "nosniff"},
    )


def public_configuration(settings):
    return {"model_id": settings["model"]["id"], "camera": settings["vision"]}


def error_message(code):
    unavailable = "Voice calls aren't available for this assistant right now."
    return {
        "live_backend_unavailable": unavailable,
        "live_model_unavailable": unavailable,
        "live_model_incompatible": unavailable,
        "live_voice_unavailable": unavailable,
        "live_configuration_changed": "The call settings changed. Close this window and try again.",
        "live_camera_unavailable": "Camera sharing isn't available for this call.",
        "live_credentials_unavailable": unavailable,
        "application_capability_denied": "Voice or camera calls aren't available here.",
        "conversation_unavailable": "You can't start a call in this conversation. Open another conversation and try again.",
        "live_session_already_active": "You already have a call open in this conversation. End that call first.",
        "conversation_busy": "This conversation is busy. Wait for it to finish and try again.",
        "unauthenticated": "Sign in again to start a call.",
    }.get(code, "The call couldn't continue. Please try again.")


@router.get("/api/conversations/{conversation_id}/live/config")
async def live_configuration(request: Request, conversation_id: int):
    from auth import get_current_user
    user = await get_embed_user(request) if is_embed_request(request) else await get_current_user(request)
    if user is None or not user.is_enabled:
        raise HTTPException(401, "Authentication required")
    try:
        async with get_db_connection(readonly=True) as connection:
            return public_configuration(await configuration(connection, conversation_id, user.id))
    except EmbedError as exc:
        logger.warning("Native voice configuration unavailable: %s", exc.code)
        raise HTTPException(exc.status_code, error_message(exc.code)) from None


async def _serve(websocket, **kwargs):
    conversation_id = kwargs["conversation_id"]
    if conversation_id in _active_conversations:
        raise EmbedError("live_session_already_active", 409)
    _active_conversations.add(conversation_id)
    try:
        await LiveMediaSession(websocket, **kwargs).run()
    finally:
        _active_conversations.discard(conversation_id)


async def _finish(websocket, exc=None):
    if exc is not None:
        # Do not expose provider responses, credentials, or recorded media.
        code = exc.code if isinstance(exc, EmbedError) else "live_session_failed"
        logger.warning("Native voice closed: %s (%s)", code, type(exc).__name__)
        with contextlib.suppress(Exception):
            await websocket.send_json({"event": "error", "code": code, "message": error_message(code)})
    with contextlib.suppress(Exception):
        await websocket.close()


async def _hello(websocket):
    async with asyncio.timeout(10):
        raw = await websocket.receive_text()
    if len(raw) > 4096:
        raise ValueError("Live configuration is too large")
    import json
    hello = json.loads(raw)
    if not isinstance(hello, dict) or hello.get("event") != "start":
        raise ValueError("Expected live session start")
    model = hello.get("model_id")
    if model is not None and (isinstance(model, bool) or not isinstance(model, int) or model <= 0):
        raise ValueError("Invalid live model")
    if not isinstance(hello.get("camera", False), bool):
        raise ValueError("Invalid camera setting")
    return hello


@router.websocket("/ws/conversations/{conversation_id}/live")
async def browser_live(websocket: WebSocket, conversation_id: int):
    from auth import get_current_user_from_websocket
    from common import READONLY_MODE
    from request_security import validate_mutation_request

    async def identity():
        principal = get_embed_principal(websocket)
        if principal is not None:
            from integrations.embed.identity import get_embed_store
            principal = await get_embed_store().revalidate_principal(principal)
            if principal.conversation_id != conversation_id:
                raise EmbedError("frame_destination_changed", 409)
            websocket.scope["state"]["embed_principal"] = principal
            user = await get_embed_user(websocket)
        else:
            user = await get_current_user_from_websocket(websocket)
        if user is None or READONLY_MODE:
            raise EmbedError("unauthenticated", 401)
        return user, principal
    try:
        user, _ = await identity()
        await websocket.accept()
        hello = await _hello(websocket)
        token = hello.get("csrf_token")
        if not isinstance(token, str) or not 32 <= len(token) <= 256 or not token.isascii():
            raise EmbedError("unauthenticated", 401)
        request = _http_request(websocket, token)
        if validate_mutation_request(request) is not None:
            raise EmbedError("unauthenticated", 401)
        await _serve(websocket, conversation_id=conversation_id, user=user, request=request,
                     check_identity=identity, model_id=hello.get("model_id"), camera=hello.get("camera", False))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        await _finish(websocket, exc)
        return
    await _finish(websocket)


@router.websocket("/ws/devices/live")
async def device_live(websocket: WebSocket):
    from common import READONLY_MODE
    from auth import get_user_by_id
    from integrations.devices.service import authenticate_device_token, extract_bearer_token, resolve_runtime_binding
    try:
        if READONLY_MODE:
            raise EmbedError("read_only", 403)
        token = extract_bearer_token(websocket.headers.get("authorization"))
        device = await authenticate_device_token(token)
        binding = await resolve_runtime_binding(device)
        if binding.status != "bound":
            raise EmbedError("device_binding_required", 409)
        conversation_id = binding.conversation_id
        await websocket.accept()
        hello = await _hello(websocket)
        camera = hello.get("camera", False)

        async def identity():
            current = await authenticate_device_token(token)
            current_binding = await resolve_runtime_binding(current)
            if current.id != device.id or current.user_id != device.user_id or current_binding.conversation_id != conversation_id:
                raise EmbedError("device_binding_changed", 409)
            if (not all(current.capabilities.get(key) for key in ("listen", "speak"))
                    or camera and not current.capabilities.get("snapshot")):
                raise EmbedError("device_capability_denied", 403)
            user = await get_user_by_id(current.user_id)
            return user, None
        user, _ = await identity()
        await _serve(websocket, conversation_id=conversation_id, user=user, request=None,
                     check_identity=identity, channel="device", model_id=hello.get("model_id"), camera=camera)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        await _finish(websocket, exc)
        return
    await _finish(websocket)
