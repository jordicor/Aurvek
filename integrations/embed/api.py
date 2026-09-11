"""Private backend contract plus a native-login authorization entry point."""
from __future__ import annotations

import base64
import binascii
import html
import ipaddress
import re
import sqlite3
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRoute

from .identity import get_embed_store
from .models import (AppConfig, BootstrapRequest, CONTRACT_VERSION, DelegatedRequest,
                     EmbedError, EnsureInterviewRequest, ExchangeCodeRequest,
                     InterviewRequest, UpdateBriefRequest)


_EMBED_CHAT_DOCUMENT = re.compile(
    r"^/embed/[a-z][a-z0-9_-]{0,63}/[A-Za-z0-9_-]{16,128}/chat$"
)


def embed_document_error_response(request: Request, status_code: int, *, language: str | None = None):
    """Render only embed browser-document failures without exposing protocol codes."""
    if request.url.path != "/embed/bootstrap" and not _EMBED_CHAT_DOCUMENT.fullmatch(request.url.path):
        return None
    from i18n import Translator, negotiate_language
    selected = language or negotiate_language(None, request.headers.get("accept-language", ""))
    translator = Translator(selected)
    message_key = "embed.expired" if status_code == 401 else "embed.error"
    document = (
        "<!doctype html><html lang=\"" + html.escape(translator.language, quote=True) + "\">"
        "<head><meta charset=\"utf-8\"><meta name=\"viewport\" "
        "content=\"width=device-width,initial-scale=1\"><title>"
        + html.escape(translator.t("chat_ui.title.chat")) + "</title></head>"
        "<body><main><p>" + html.escape(translator.t(message_key)) + "</p></main></body></html>"
    )
    return HTMLResponse(document, status_code=status_code, headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    })


async def read_bounded_body(request: Request, max_bytes: int = 32768) -> bytes:
    """Bound chunked bodies before FastAPI/form parsing can allocate them."""
    try:
        length = int(request.headers.get("content-length", "0"))
    except ValueError:
        raise EmbedError("invalid_request", 400) from None
    if length < 0 or length > max_bytes:
        raise EmbedError("invalid_request", 413)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise EmbedError("invalid_request", 413)
        body.extend(chunk)
    request._body = bytes(body)
    return request._body


class PrivateRoute(APIRoute):
    contract_version = CONTRACT_VERSION

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_handler(request: Request):
            try:
                if request.method == "POST":
                    await read_bounded_body(request)
                response = await handler(request)
            except EmbedError as error:
                principal = getattr(request.state, "embed_principal", None)
                response = embed_document_error_response(
                    request, error.status_code,
                    language=getattr(principal, "ui_language", None),
                )
                if response is None:
                    response = JSONResponse({"contract_version": self.contract_version, "error": error.code},
                                            status_code=error.status_code)
            except HTTPException as error:
                principal = getattr(request.state, "embed_principal", None)
                response = embed_document_error_response(
                    request, error.status_code,
                    language=getattr(principal, "ui_language", None),
                )
                if response is None:
                    raise
            except RequestValidationError:
                # Never echo an invalid credential or brief in validation details.
                response = JSONResponse({"contract_version": self.contract_version, "error": "invalid_request"}, status_code=422)
            except sqlite3.OperationalError:
                response = JSONResponse({"contract_version": self.contract_version, "error": "service_unavailable"}, status_code=503)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response
        return private_handler


router = APIRouter(route_class=PrivateRoute)


async def backend_client(request: Request) -> AppConfig:
    """Loopback transport AND registered backend authentication are required."""
    try:
        peer = ipaddress.ip_address(request.client.host if request.client else "")
    except ValueError:
        raise EmbedError("unauthenticated") from None
    if not peer.is_loopback:
        raise EmbedError("unauthenticated")
    return await authenticate_backend_client(request)


async def authenticate_backend_client(request: Request) -> AppConfig:
    """Registered client authentication, independent of the ingress policy."""
    header = request.headers.get("authorization", "")
    if len(header) > 1024 or not header.startswith("Basic "):
        raise EmbedError("unauthenticated")
    try:
        username, secret = base64.b64decode(header[6:], validate=True).decode("ascii").split(":", 1)
    except (ValueError, UnicodeError, binascii.Error):
        raise EmbedError("unauthenticated") from None
    config = await get_embed_store().authenticate_client(username, secret)
    # Caller pins the canonical public Host; inbound forwarded headers never set it.
    if request.headers.get("host", "").lower() != urlsplit(config.issuer).netloc:
        raise EmbedError("unauthenticated")
    return config


@router.get("/embed/authorize")
async def authorize(request: Request, app_id: str, redirect_uri: str, state: str,
                    code_challenge: str, code_challenge_method: str = "S256", ui_language: str = "en"):
    from rate_limiter import RateLimitConfig, check_rate_limits
    if check_rate_limits(request, ip_limit=RateLimitConfig.OAUTH_BY_IP, action_name="embed_authorize"):
        raise EmbedError("limit_reached", 429)
    store = get_embed_store()
    config = await store.get_app(app_id)
    if request.headers.get("host", "").lower() != urlsplit(config.issuer).netloc or code_challenge_method != "S256":
        raise EmbedError("invalid_request", 400)
    tx = await store.authorize_begin(app_id, redirect_uri, state, code_challenge, ui_language)
    pending = request.session.get("embed_authorizations", [])
    request.session["embed_authorizations"] = [value for value in pending if isinstance(value, str)][-3:] + [tx]
    return RedirectResponse("/embed/authorize/complete?" + urlencode({"transaction": tx}), status_code=303)


@router.get("/embed/authorize/complete")
async def authorize_complete(request: Request, transaction: str):
    if transaction not in request.session.get("embed_authorizations", []) or len(transaction) > 128:
        raise EmbedError("unauthenticated")
    config = await get_embed_store().authorization_app(transaction)
    if request.headers.get("host", "").lower() != urlsplit(config.issuer).netloc:
        raise EmbedError("unauthenticated")
    from auth import get_current_user
    from auth_constants import SESSION_COOKIE_NAME
    user = await get_current_user(request)
    if not user:
        internal_next = "/embed/authorize/complete?" + urlencode({"transaction": transaction})
        return RedirectResponse("/login?" + urlencode({"next": internal_next}), status_code=303)
    result = await get_embed_store().authorize_complete(transaction, user, request.cookies.get(SESSION_COOKIE_NAME, ""))
    request.session["embed_authorizations"] = [value for value in request.session.get("embed_authorizations", []) if value != transaction]
    return RedirectResponse(result["redirect_uri"] + "?" + urlencode({"code": result["code"], "state": result["state"]}), status_code=303)


@router.post("/api/embed/v1/exchange-code")
async def exchange_code(body: ExchangeCodeRequest, app: AppConfig = Depends(backend_client)):
    return await get_embed_store().exchange_code(app.app_id, body.code, body.code_verifier, body.redirect_uri)


@router.post("/api/embed/v1/inspect-access")
async def inspect_access(body: DelegatedRequest, app: AppConfig = Depends(backend_client)):
    store = get_embed_store()
    principal = await store.inspect_access(app.app_id, body.delegated_credential)
    return store.access_response(principal)


@router.post("/api/embed/v1/ensure-interview")
async def ensure_interview(body: EnsureInterviewRequest, app: AppConfig = Depends(backend_client)):
    store = get_embed_store()
    principal = await store.inspect_access(app.app_id, body.delegated_credential)
    return await store.ensure_interview(principal, body.external_project_id, body.idempotency_key, body.brief)


@router.post("/api/embed/v1/get-interview-state")
async def get_interview_state(body: InterviewRequest, app: AppConfig = Depends(backend_client)):
    store = get_embed_store()
    principal = await store.inspect_access(app.app_id, body.delegated_credential)
    state = await store.interview_state(principal, body.external_project_id)
    from .activity import activity_state
    state["activity"] = await activity_state(int(state["conversation_id"]))
    return state


@router.post("/api/embed/v1/update-interview-brief")
async def update_interview_brief(body: UpdateBriefRequest, app: AppConfig = Depends(backend_client)):
    store = get_embed_store()
    principal = await store.inspect_access(app.app_id, body.delegated_credential)
    return await store.update_brief(principal, body.external_project_id, body.conversation_id,
                                   body.expected_revision, body.brief)


@router.post("/api/embed/v1/issue-embed-bootstrap")
async def issue_embed_bootstrap(body: BootstrapRequest, app: AppConfig = Depends(backend_client)):
    store = get_embed_store()
    principal = await store.inspect_access(app.app_id, body.delegated_credential)
    return await store.issue_bootstrap(principal, body.external_project_id, body.conversation_id,
                                      body.parent_origin, body.embed_origin, body.ui_language, body.frame_instance_id)


@router.post("/api/embed/v1/end-app-session")
async def end_app_session(body: DelegatedRequest, app: AppConfig = Depends(backend_client)):
    from .activity import close_delegated_session
    result = await close_delegated_session(get_embed_store(), app.app_id, body.delegated_credential)
    return {"contract_version": CONTRACT_VERSION, **result}
