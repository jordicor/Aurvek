"""Default-deny host boundary for the shared embedded chat surface.

    Install OUTSIDE CustomDomainMiddleware and security/session middlewares.
    Browser traffic remains separate from the authenticated backend contract.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import replace
from urllib.parse import urlsplit

from starlette.datastructures import Headers, QueryParams
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from .identity import get_embed_store
from .models import EMBED_COOKIE_NAME, EmbedError
from i18n import LANGUAGES

logger = logging.getLogger(__name__)

_FRAME_PAGE = re.compile(r"^/embed/([a-z][a-z0-9_-]{0,63})/([A-Za-z0-9_-]{16,128})/(chat|session|activity|activity/stop|application/state|application/handoff|application/entry)$")
_PROFILE_PAGE = re.compile(r"^/embed/([a-z][a-z0-9_-]{0,63})/([A-Za-z0-9_-]{16,128})/profile(?:/(state|save|language|phone|channel))?$")
_CONVERSATION = re.compile(r"^/api/conversations/([1-9][0-9]*)/([a-z_-]+(?:/[a-z_-]+)?)$")
_EXPORT = re.compile(r"^/api/conversations/([1-9][0-9]*)/exports/(pdf|mp3)(?:/([A-Za-z0-9_-]{32})(?:/(stop|content))?)?$")
_VOICE_SOCKET = re.compile(r"^/ws/conversations/([1-9][0-9]*)/voice$")
_ATTACHMENT = re.compile(r"^/api/attachments/([A-Za-z0-9_-]{1,128})(?:/(content|download))?$")
_STATIC = re.compile(r"^/static/(?:css|js|font|fonts|img|images)/[A-Za-z0-9_./@-]+$")
_READ_OPERATIONS = {"messages", "details", "last_message_id", "status", "provider-health", "web-search-status"}
_CONTROL_OPERATIONS = {"rename", "rollback", "bookmark"}
_ATTACHMENT_OPERATIONS = {"attachments/chunk", "attachments/complete", "attachments/status", "attachments/discard"}
_VOICE_READ = {"elevenlabs/config", "elevenlabs/availability", "voice/availability"}
_VOICE_WRITE = {"elevenlabs/session", "elevenlabs/complete", "elevenlabs/stop"}


def _route_kind(path: str, method: str) -> tuple[str, str | None] | None:
    """This allowlist intentionally excludes unscoped search, media and settings."""
    if any(part in {".", ".."} for part in path.split("/")) or "\\" in path:
        return None
    if _STATIC.fullmatch(path) and method in {"GET", "HEAD"}:
        return "static", None
    if path == "/embed/bootstrap" and method == "POST":
        return "bootstrap", None
    if path == "/embed/profile/bootstrap" and method == "POST":
        return "bootstrap", None
    profile = _PROFILE_PAGE.fullmatch(path)
    if profile and ((profile[3] in {None, 'state'} and method == 'GET') or (profile[3] in {'save', 'language', 'phone', 'channel'} and method == 'POST')):
        return 'profile', None
    match = _FRAME_PAGE.fullmatch(path)
    if match and ((match[3] in {"chat", "session", "activity", "application/state"} and method in {"GET", "HEAD"})
                  or (match[3] in {"activity/stop", "application/handoff", "application/entry"} and method == "POST")):
        return "frame", None
    export = _EXPORT.fullmatch(path)
    if export:
        if ((export[3] is None and method == "POST")
                or (export[3] and export[4] in {None, "content"} and method == "GET")
                or (export[3] and export[4] == "stop" and method == "POST")):
            return "conversation", "export_" + export[2]
        return None
    match = _CONVERSATION.fullmatch(path)
    if match:
        operation = match[2]
        if operation in _READ_OPERATIONS and method in {"GET", "HEAD"}:
            return "conversation", None
        if operation == "media/content" and method in {"GET", "HEAD"}:
            return "conversation", "text"
        if operation in {"model", "extension"} and method == "PATCH":
            return "conversation", "model_selection" if operation == "model" else "extensions"
        if operation == "voice/audio" and method in {"GET", "HEAD"}:
            return "conversation", "voice"
        if operation in {"voice/transcribe", "voice/tts"} and method == "POST":
            return "conversation", "stt" if operation.endswith("/transcribe") else "tts"
        if operation == "messages" and method == "POST":
            return "generation", "text"
        if operation == "stop" and method == "POST":
            return "stop", None
        if operation in _CONTROL_OPERATIONS and method == "POST":
            return "conversation", "conversation_controls"
        if operation in _ATTACHMENT_OPERATIONS:
            expected = {"GET", "HEAD"} if operation.endswith("/status") else {"POST"}
            if method in expected:
                return "conversation", "attachments"
        if ((operation in _VOICE_READ and method in {"GET", "HEAD"})
                or (operation in _VOICE_WRITE and method == "POST")):
            return "conversation", "voice"
    match = _ATTACHMENT.fullmatch(path)
    if match and ((match[2] and method in {"GET", "HEAD"})
                  or (not match[2] and method == "DELETE")):
        return "attachment", "attachments"
    if path in {"/sdk/elevenlabs-client.js", "/sdk/elevenlabs-client.js.map"} and method in {"GET", "HEAD"}:
        return "sdk", "voice"
    return None


class EmbedHostMiddleware:
    def __init__(self, app, *, store=None):
        self.app = app
        self.store = store

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        # Forwarded host/origin headers are never used to classify this boundary.
        authority = headers.get("host", "")
        if len(headers.getlist("host")) != 1:
            return await self._reject(scope, receive, send, "not_found", 404)
        try:
            parsed_host = urlsplit(f"https://{authority}")
            if (not authority or not parsed_host.hostname or parsed_host.username
                    or parsed_host.password or parsed_host.path or parsed_host.query
                    or parsed_host.fragment or any(ord(ch) <= 32 for ch in authority)):
                raise ValueError("Invalid authority")
            _ = parsed_host.port
        except ValueError:
            return await self._reject(scope, receive, send, "not_found", 404)
        from marketplace.middleware.custom_domains import is_primary_domain
        if is_primary_domain(parsed_host.hostname):
            # App registration prohibits native/identity hosts. Their availability
            # and authentication do not depend on the embed registry.
            return await self.app(scope, receive, send)
        store = self.store or get_embed_store()
        try:
            app_config = await store.app_for_host(authority)
        except Exception:
            logger.warning("Embed host registry unavailable", exc_info=False)
            return await self._reject(scope, receive, send, "service_unavailable", 503)
        if app_config is None:
            if scope["type"] == "websocket":
                return await self._reject(scope, receive, send, "not_found", 404)
            return await self.app(scope, receive, send)

        state = scope.setdefault("state", {})
        state["embed_host"] = True
        state["embed_app"] = app_config
        origin = next((value for value in app_config.embed_origins
                       if urlsplit(value).netloc == authority), None)
        if origin is None or not app_config.enabled:
            return await self._reject(scope, receive, send, "not_found", 404)
        state["embed_origin"] = origin
        scoped_send = self._response_sender(send, app_config)
        if os.getenv("UVICORN_WORKERS", "1").strip() != "1":
            return await self._reject(scope, receive, scoped_send, "service_unavailable", 503)
        voice_socket_match = _VOICE_SOCKET.fullmatch(scope["path"]) if scope["type"] == "websocket" else None
        is_voice_socket = voice_socket_match is not None
        if scope["type"] == "websocket" and not is_voice_socket:
            return await self._reject(scope, receive, scoped_send, "capability_disabled", 403)
        route = ("conversation", "voice") if is_voice_socket else _route_kind(scope["path"], scope["method"].upper())
        if route is None:
            return await self._reject(scope, receive, scoped_send, "not_found", 404)
        kind, capability = route

        # Embed requests cannot use or mint the native authentication/session
        # cookies. The distinct opaque host-only cookie is the sole credential.
        from starlette.requests import HTTPConnection
        request = HTTPConnection(scope)
        cookie = request.cookies.get(EMBED_COOKIE_NAME, "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", cookie):
            cookie = ""
        scope["headers"] = [(key, value) for key, value in scope["headers"]
                            if key.lower() != b"cookie"]
        if cookie:
            scope["headers"].append((b"cookie", f"{EMBED_COOKIE_NAME}={cookie}".encode("latin-1")))
        if kind in {"static", "bootstrap"}:
            return await self.app(scope, receive, scoped_send)
        dispatched = False
        principal = None
        try:
            if kind == 'profile':
                from integrations.applications.profile_frames import ProfileFrames
                match = _PROFILE_PAGE.fullmatch(scope['path'])
                if match[1] != app_config.app_id:
                    raise EmbedError('not_found', 404)
                principal = await ProfileFrames(store).resolve(cookie, authority, match[1], match[2])
                state['embed_principal'] = principal
                if scope['method'] == 'POST':
                    from request_security import validate_mutation_request
                    rejection = validate_mutation_request(Request(scope))
                    if rejection is not None:
                        return await rejection(scope, receive, scoped_send)
                dispatched = True
                return await self.app(scope, receive, scoped_send)
            frame_id = self._frame_id(scope, headers)
            principal = await store.resolve_frame(cookie, authority, app_config.app_id, frame_id)
            self._check_explicit_scope(scope, headers, principal)
            match = voice_socket_match or _EXPORT.fullmatch(scope["path"]) or _CONVERSATION.fullmatch(scope["path"])
            conversation_id = int(match[1]) if match else principal.conversation_id
            if kind == "attachment":
                conversation_id = await self._attachment_conversation(scope["path"], principal)
            if not conversation_id or conversation_id != principal.conversation_id:
                raise EmbedError("not_found", 404)
            binding = await store.authorize_conversation(
                principal, conversation_id, principal.external_project_id
            )
            if capability and not principal.capabilities.get(capability, False):
                raise EmbedError("capability_disabled", 403)
            requested_languages = [value for value in (
                headers.get("x-aurvek-ui-language"),
                QueryParams(scope.get("query_string", b"")).get("ui_language")
                if is_voice_socket else None,
            ) if value is not None]
            if requested_languages:
                if len(set(requested_languages)) != 1 or requested_languages[0] not in LANGUAGES:
                    raise EmbedError("invalid_request", 400)
                principal = replace(principal, ui_language=requested_languages[0])
            state["embed_principal"] = principal
            state["embed_binding"] = binding
            state["application_context"] = principal.application
            if principal.application is not None and "/elevenlabs/" in scope["path"]:
                raise EmbedError("hosted_voice_unavailable_for_application", 403)
            # The voice endpoint checks Origin and the session CSRF token in its
            # first message before microphone processing or provider work.
            if not is_voice_socket and scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
                from request_security import validate_mutation_request
                rejection = validate_mutation_request(Request(scope))
                if rejection is not None:
                    return await rejection(scope, receive, scoped_send)
            if kind == "generation":
                from .activity import track_embed_generation
                async with track_embed_generation(principal):
                    dispatched = True
                    return await self.app(scope, receive, scoped_send)
            if kind == "stop":
                from .activity import mark_embed_stop
                mark_embed_stop(principal)
            dispatched = True
            return await self.app(scope, receive, scoped_send)
        except EmbedError as exc:
            return await self._reject(
                scope, receive, scoped_send, exc.code, exc.status_code,
                language=getattr(principal, "ui_language", None),
            )
        except HTTPException as exc:
            return await self._reject(
                scope, receive, scoped_send, str(exc.detail), exc.status_code,
                language=getattr(principal, "ui_language", None),
            )
        except Exception:
            if dispatched:
                raise
            logger.warning("Embed request authorization unavailable", exc_info=False)
            return await self._reject(
                scope, receive, scoped_send, "service_unavailable", 503,
                language=getattr(principal, "ui_language", None),
            )

    @staticmethod
    def _frame_id(scope, headers):
        match = _FRAME_PAGE.fullmatch(scope["path"])
        values = [headers.get("x-aurvek-embed-frame"), QueryParams(scope.get("query_string", b"")).get("embed_frame")]
        if match:
            values.append(match[2])
        supplied = [value for value in values if value]
        if not supplied or len(set(supplied)) != 1:
            raise EmbedError("unauthenticated", 401)
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", supplied[0]):
            raise EmbedError("unauthenticated", 401)
        return supplied[0]

    @staticmethod
    def _check_explicit_scope(scope, headers, principal):
        match = _FRAME_PAGE.fullmatch(scope["path"])
        if match and match[1] != principal.app_id:
            raise EmbedError("not_found", 404)
        query = QueryParams(scope.get("query_string", b""))
        for header, parameter, expected in (
            ("x-aurvek-embed-project", "embed_project", principal.external_project_id),
            ("x-aurvek-embed-conversation", "embed_conversation", str(principal.conversation_id)),
        ):
            if any(value is not None and value != expected
                   for value in (headers.get(header), query.get(parameter))):
                raise EmbedError("not_found", 404)

    @staticmethod
    async def _attachment_conversation(path, principal):
        from database import get_db_connection
        public_id = _ATTACHMENT.fullmatch(path)[1]
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT conversation_id FROM FILE_ATTACHMENTS WHERE public_id = ? AND user_id = ?",
                (public_id, principal.user_id),
            )
            row = await cursor.fetchone()
        if not row:
            raise EmbedError("not_found", 404)
        return int(row[0])

    @staticmethod
    def _response_sender(send, app_config):
        async def scoped_send(message):
            if message["type"] == "http.response.start":
                clean = []
                for key, value in message.get("headers", []):
                    name = key.lower()
                    if name in {b"x-frame-options", b"cache-control", b"referrer-policy"}:
                        continue
                    if name == b"set-cookie":
                        cookie_name = value.split(b"=", 1)[0].strip().decode("latin-1")
                        if cookie_name != EMBED_COOKIE_NAME:
                            continue
                    if name == b"content-security-policy":
                        value = b"; ".join(directive.strip() for directive in value.split(b";")
                                           if directive.strip() and not directive.strip().lower().startswith(b"frame-ancestors"))
                        if not value:
                            continue
                    clean.append((key, value))
                clean.extend([
                    (b"content-security-policy", ("frame-ancestors " + " ".join(app_config.parent_origins)).encode("ascii")),
                    (b"cache-control", b"private, no-store"),
                    (b"referrer-policy", b"no-referrer"),
                ])
                message["headers"] = clean
            await send(message)
        return scoped_send

    @staticmethod
    async def _reject(scope, receive, send, code, status, *, language=None):
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 4403})
        if _FRAME_PAGE.fullmatch(scope["path"]) and scope["path"].endswith("/chat"):
            from .api import embed_document_error_response
            principal = scope.get("state", {}).get("embed_principal")
            response = embed_document_error_response(
                Request(scope), status,
                language=language or getattr(principal, "ui_language", None),
            )
            return await response(scope, receive, send)
        response = JSONResponse({"error": code, "contract_version": "aurvek_embed.v1"}, status_code=status,
                                headers={"Cache-Control": "no-store"})
        return await response(scope, receive, send)
