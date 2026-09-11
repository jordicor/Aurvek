"""Browser entry points. Transition tickets are accepted only in a POST body."""

import time
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from integrations.embed.identity import get_embed_store
from integrations.embed.page_context import render_embed_chat
from integrations.embed.api import PrivateRoute, read_bounded_body
from integrations.applications.models import EntryPreferenceRequest, HandoffRequest
from integrations.embed.models import EmbedError
from i18n import LANGUAGES, Translator
from integrations.applications.profile_pages import router as profile_router

router = APIRouter(route_class=PrivateRoute)
router.include_router(profile_router)
COOKIE_NAME = "__Host-aurvek_embed"


async def _principal(request: Request, app_id: str, frame_instance_id: str):
    return await get_embed_store().resolve_frame(
        request.cookies.get(COOKIE_NAME, ""),
        request.headers.get("host", ""),
        app_id,
        frame_instance_id,
    )


def _private_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.post("/embed/bootstrap")
async def bootstrap_embed(request: Request):
    if request.url.query:
        raise HTTPException(status_code=400, detail="invalid_request")
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=400, detail="invalid_request")
    body = await read_bounded_body(request, max_bytes=4096)
    try:
        form = parse_qs(body.decode("ascii"), strict_parsing=True, max_num_fields=1)
        tickets = form.get("ticket", [])
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid_request") from None
    ticket = tickets[0] if len(tickets) == 1 else None
    if not isinstance(ticket, str) or not ticket or len(ticket) > 512 or set(form) != {"ticket"}:
        raise HTTPException(status_code=400, detail="invalid_request")
    cookie, principal = await get_embed_store().consume_bootstrap(
        ticket,
        request.headers.get("host", ""),
        request.headers.get("origin", ""),
    )
    response = RedirectResponse(
        f"/embed/{principal.app_id}/{principal.frame_instance_id}/chat", status_code=303
    )
    response.set_cookie(
        COOKIE_NAME,
        cookie,
        max_age=max(0, int(principal.expires_at - time.time())),
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return _private_headers(response)


@router.get("/embed/{app_id}/{frame_instance_id}/chat")
async def embed_chat(request: Request, app_id: str, frame_instance_id: str):
    if request.url.query:
        raise HTTPException(status_code=400, detail="invalid_request")
    principal = await _principal(request, app_id, frame_instance_id)
    app = await get_embed_store().get_app(principal.app_id)
    return _private_headers(await render_embed_chat(request, principal, app))


@router.get("/embed/{app_id}/{frame_instance_id}/session")
async def embed_session(request: Request, app_id: str, frame_instance_id: str,
                        ui_language: str | None = None):
    principal = await _principal(request, app_id, frame_instance_id)
    result = {
        "expired": False,
        "expires_in": max(0, int(principal.expires_at - time.time())),
        "capabilities": principal.capabilities,
    }
    if ui_language is not None:
        if ui_language not in LANGUAGES:
            raise EmbedError("invalid_request", 400)
        result["i18n"] = Translator(ui_language).browser_payload((
            "navigation", "chat", "chat_widgets", "chat_ui", "chat_errors",
            "embed", "application",
        ))
    return _private_headers(JSONResponse(result))


@router.get("/embed/{app_id}/{frame_instance_id}/activity")
async def embed_activity(request: Request, app_id: str, frame_instance_id: str):
    from integrations.embed.activity import get_activity

    principal = await _principal(request, app_id, frame_instance_id)
    return _private_headers(JSONResponse(await get_activity(principal)))


@router.post("/embed/{app_id}/{frame_instance_id}/activity/stop")
async def stop_embed_activity(request: Request, app_id: str, frame_instance_id: str):
    from integrations.embed.activity import request_stop

    principal = await _principal(request, app_id, frame_instance_id)
    return _private_headers(JSONResponse(await request_stop(principal)))


@router.get("/embed/{app_id}/{frame_instance_id}/application/state")
async def application_state(request: Request, app_id: str, frame_instance_id: str):
    from integrations.applications.handoff import ApplicationHandoffService
    principal = await _principal(request, app_id, frame_instance_id)
    return await ApplicationHandoffService(get_embed_store()).state(
        principal, principal.conversation_id, frame_instance_id=frame_instance_id)


@router.post("/embed/{app_id}/{frame_instance_id}/application/handoff")
async def application_handoff(request: Request, app_id: str, frame_instance_id: str, body: HandoffRequest):
    from integrations.applications.handoff import ApplicationHandoffService
    principal = await _principal(request, app_id, frame_instance_id)
    if body.source_conversation_id != principal.conversation_id:
        raise EmbedError("frame_destination_changed", 409)
    return await ApplicationHandoffService(get_embed_store()).handoff(
        principal, body, frame_instance_id=frame_instance_id, note_origin="user")


@router.post("/embed/{app_id}/{frame_instance_id}/application/entry")
async def application_entry(request: Request, app_id: str, frame_instance_id: str, body: EntryPreferenceRequest):
    from integrations.applications.handoff import ApplicationHandoffService
    from integrations.embed.store import _one
    principal = await _principal(request, app_id, frame_instance_id)
    if principal.application is None:
        raise EmbedError("not_found", 404)
    store = get_embed_store()
    async with store.connection(readonly=True) as connection:
        participant = await _one(connection, "SELECT context_ref FROM APPLICATION_PARTICIPANTS WHERE app_id=? AND subject=? AND context_id=?",
            (principal.app_id, principal.subject, principal.application.context_id))
    if not participant or (body.context_ref is not None and body.context_ref != participant["context_ref"]):
        raise EmbedError("not_found", 404)
    body = body.model_copy(update={"context_ref": participant["context_ref"]})
    return await ApplicationHandoffService(store).set_entry_preference(principal, body)
