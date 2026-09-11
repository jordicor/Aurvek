"""Exercise the real ASGI host boundary independently of providers/production DB."""
from dataclasses import replace
from contextlib import asynccontextmanager
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import aiosqlite
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from starlette.datastructures import FormData
from starlette.websockets import WebSocketDisconnect

import request_security
from integrations.embed.context import get_embed_user
from integrations.embed.middleware import EmbedHostMiddleware
from integrations.embed.models import AppConfig, EMBED_COOKIE_NAME, EmbedError, EmbedPrincipal
from marketplace.middleware import custom_domains

HOST = "chat.product.example"
ORIGIN = f"https://{HOST}"
COOKIE = "c" * 48
FRAME_A = "frame_project_a_12345"
FRAME_B = "frame_project_b_12345"
CSRF = "s" * 48


class ScopedStore:
    def __init__(self):
        self.app = AppConfig(
            app_id="product", display_name="Product", issuer="https://aurvek.example",
            embed_origins=[ORIGIN], parent_origins=["https://product.example"],
            redirect_uris=["https://product.example/callback"], prompt_id=10,
            capabilities={"text": True, "attachments": True}, enabled=True,
        )
        first = EmbedPrincipal(
            app_id="product", issuer="https://aurvek.example", subject="subject-a",
            user_id=1, prompt_id=10, delegated_session_id="delegated-a",
            session_version=3, membership_version=1, expires_at=9999999999,
            conversation_id=101, external_project_id="project-a", parent_origin="https://product.example",
            embed_origin=ORIGIN, frame_instance_id=FRAME_A, csrf_token=CSRF,
            capabilities={"text": True, "attachments": True},
        )
        self.frames = {FRAME_A: first, FRAME_B: replace(first, conversation_id=102,
                       external_project_id="project-b", frame_instance_id=FRAME_B)}
        self.active = True
        self.authorized = []

    async def app_for_host(self, authority):
        return self.app if authority == HOST else None

    async def resolve_frame(self, cookie, host, app_id, frame_id):
        if not self.active or cookie != COOKIE or host != HOST or app_id != self.app.app_id:
            raise EmbedError("unauthenticated")
        frame = self.frames.get(frame_id)
        if frame is None:
            raise EmbedError("not_found", 404)
        return frame

    async def authorize_conversation(self, principal, conversation_id, external_project_id=None):
        if conversation_id != principal.conversation_id or external_project_id != principal.external_project_id:
            raise EmbedError("not_found", 404)
        self.authorized.append((principal.app_id, principal.subject, external_project_id, conversation_id))
        return {"conversation_id": conversation_id, "external_project_id": external_project_id}


@pytest.fixture
def surface(monkeypatch):
    monkeypatch.setenv("UVICORN_WORKERS", "1")
    monkeypatch.setattr(custom_domains, "_primary_domains", {"aurvek.example"})
    store = ScopedStore()
    app = FastAPI()

    @app.api_route("/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
    async def observed(request: Request, path: str):
        principal = getattr(request.state, "embed_principal", None)
        response = JSONResponse({
            "cookies": request.cookies,
            "conversation": principal.conversation_id if principal else None,
            "csrf": request_security.ensure_csrf_token(request) if principal else None,
            "ui_language": principal.ui_language if principal else None,
        })
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "object-src 'none'; frame-ancestors 'self'"
        response.set_cookie("native_session", "native-secret")
        return response

    app.add_middleware(custom_domains.CustomDomainMiddleware)
    app.add_middleware(EmbedHostMiddleware, store=store)
    with TestClient(app, base_url=ORIGIN) as client:
        client.cookies.set(EMBED_COOKIE_NAME, COOKIE)
        client.cookies.set("native_session", "native-authentication-cookie")
        yield client, store


def scoped_headers(frame=FRAME_A, **extra):
    return {"X-Aurvek-Embed-Frame": frame, **extra}


@pytest.mark.parametrize("kind", ["pdf", "mp3"])
def test_export_routes_require_capability_csrf_and_matching_frame(surface, kind):
    client, store = surface
    path = f"/api/conversations/101/exports/{kind}"
    headers = scoped_headers(Origin=ORIGIN, **{"X-GPTSub-CSRF": CSRF})
    assert client.post(path, headers=headers).status_code == 403
    store.frames[FRAME_A] = replace(store.frames[FRAME_A],
        capabilities={**store.frames[FRAME_A].capabilities, "export_" + kind: True})
    assert client.post(path, headers=scoped_headers(Origin=ORIGIN)).status_code == 403
    assert client.post(path, headers=headers).status_code == 200
    job = "j" * 32
    assert client.get(path + "/" + job + "/content", headers=scoped_headers()).status_code == 200
    other = path.replace("/101/", "/102/")
    assert client.get(other + "/" + job, headers=scoped_headers()).status_code == 404
    assert client.delete(path + "/" + job, headers=headers).status_code == 404


def test_registered_host_reuses_chat_with_distinct_cookie_and_scoped_framing(surface):
    client, store = surface
    response = client.get("/api/conversations/101/messages", headers=scoped_headers())
    assert response.status_code == 200
    assert response.json() == {"cookies": {EMBED_COOKIE_NAME: COOKIE}, "conversation": 101,
                               "csrf": CSRF, "ui_language": "en"}
    assert store.authorized == [("product", "subject-a", "project-a", 101)]
    assert "set-cookie" not in response.headers
    assert "x-frame-options" not in response.headers
    assert "frame-ancestors https://product.example" in response.headers["content-security-policy"]
    assert "frame-ancestors 'self'" not in response.headers["content-security-policy"]
    assert "object-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "no-store" in response.headers["cache-control"]


def test_native_stop_marks_embed_activity_only_after_csrf_and_scope_checks(surface, monkeypatch):
    from integrations.embed import activity
    client, store = surface
    active = activity.ActiveGeneration(101, 1, "delegated-a")
    monkeypatch.setattr(activity, "_active", {101: active})
    path = "/api/conversations/101/stop"
    assert client.post(path, headers=scoped_headers()).status_code == 403
    assert not active.ending
    response = client.post(path, headers=scoped_headers(**{
        "Origin": ORIGIN, request_security.CSRF_HEADER: CSRF,
    }))
    assert response.status_code == 200
    assert active.ending


@pytest.mark.parametrize("path,method", [
    ("/admin/chat", "GET"), ("/api/admin/conversations", "GET"), ("/login", "GET"),
    ("/api/conversations", "GET"), ("/api/conversations/new", "POST"),
    ("/api/conversations/101/branch", "POST"),
    ("/api/messages/search?conversation_id=101", "GET"), ("/get-image/private.png", "GET"),
    ("/get-audio/private.mp3", "GET"), ("/auth-file?path=secret", "GET"),
    ("/api/user/memory-preferences", "GET"), ("/api/user-credentials", "GET"),
    ("/static/files/private.pdf", "GET"), ("/static/sec/private.key", "GET"),
    ("/api/embed/v1/exchange-code", "POST"), ("/embed/bootstrap", "GET"),
])
def test_embed_host_denies_unsupported_routes_even_with_native_cookie(surface, path, method):
    client, store = surface
    response = client.request(method, path, headers=scoped_headers())
    assert response.status_code == 404
    assert not store.authorized


def test_two_tabs_keep_their_durable_conversation_scope(surface):
    client, _ = surface
    assert client.get("/api/conversations/101/messages", headers=scoped_headers()).status_code == 200
    assert client.get("/api/conversations/102/messages", headers=scoped_headers(FRAME_B)).status_code == 200
    assert client.get("/api/conversations/101/messages", headers=scoped_headers()).json()["conversation"] == 101
    assert client.get("/api/conversations/102/messages", headers=scoped_headers()).status_code == 404
    assert client.get("/api/conversations/999/messages", headers=scoped_headers()).status_code == 404


@pytest.mark.parametrize("language", ["en", "es", "ja", "fr", "pt", "it", "de"])
def test_authenticated_frame_header_sets_only_request_locale(surface, language):
    client, store = surface
    response = client.get("/api/conversations/101/messages", headers=scoped_headers(**{
        "X-Aurvek-UI-Language": language,
    }))
    assert response.status_code == 200
    assert response.json()["ui_language"] == language
    assert store.frames[FRAME_A].ui_language == "en"
    assert store.frames[FRAME_B].ui_language == "en"


def test_invalid_frame_locale_is_rejected_after_auth_without_changing_identity(surface):
    client, store = surface
    response = client.get("/api/conversations/101/messages", headers=scoped_headers(**{
        "X-Aurvek-UI-Language": "es-MX",
    }))
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert store.authorized == [("product", "subject-a", "project-a", 101)]
    assert store.frames[FRAME_A].ui_language == "en"


def test_native_host_ignores_embed_locale_header(surface):
    client, _ = surface
    response = client.get("https://aurvek.example/admin/chat", headers={
        "X-Aurvek-UI-Language": "ja",
    })
    assert response.status_code == 200
    assert response.json()["ui_language"] is None


@pytest.mark.parametrize("extra", [
    {"X-Aurvek-Embed-Project": "project-b"}, {"X-Aurvek-Embed-Conversation": "102"},
])
def test_explicit_scope_mismatch_is_rejected(surface, extra):
    client, _ = surface
    assert client.get("/api/conversations/101/messages", headers=scoped_headers(**extra)).status_code == 404


def test_missing_frame_and_changed_account_do_not_fall_back_to_native(surface):
    client, store = surface
    assert client.get("/api/conversations/101/messages").status_code == 401
    store.active = False
    assert client.get("/api/conversations/101/messages", headers=scoped_headers()).status_code == 401
    expired = client.get(f"/embed/product/{FRAME_A}/chat", headers={"Accept-Language": "de"})
    assert expired.status_code == 401
    assert expired.headers["content-type"].startswith("text/html")
    assert '<html lang="de">' in expired.text
    assert "Ihr Zugriff ist abgelaufen" in expired.text
    assert "unauthenticated" not in expired.text and "Aurvek" not in expired.text


def test_chat_failure_uses_verified_frame_language_before_header_negotiation(surface, monkeypatch):
    client, store = surface
    store.frames[FRAME_A] = replace(store.frames[FRAME_A], ui_language="ja")
    monkeypatch.setattr(
        store, "authorize_conversation",
        AsyncMock(side_effect=EmbedError("membership_inactive", 403)),
    )
    response = client.get(f"/embed/product/{FRAME_A}/chat", headers={"Accept-Language": "de"})
    assert response.status_code == 403
    assert '<html lang="ja">' in response.text
    assert "操作を完了できませんでした" in response.text
    assert "membership_inactive" not in response.text and "Aurvek" not in response.text


def test_frame_page_binds_exact_app(surface):
    client, _ = surface
    assert client.get(f"/embed/product/{FRAME_A}/chat").status_code == 200
    assert client.get(f"/embed/other_app/{FRAME_A}/chat").status_code == 404


@pytest.mark.parametrize("origin,token,status", [
    (ORIGIN, CSRF, 200), ("https://aurvek.example", CSRF, 403),
    ("https://product.example", CSRF, 403), (ORIGIN, "wrong", 403), (None, CSRF, 403),
])
def test_embed_mutation_uses_registered_origin_and_own_csrf(surface, monkeypatch, origin, token, status):
    monkeypatch.setattr(request_security, "PRIMARY_APP_DOMAIN", "aurvek.example")
    client, _ = surface
    headers = scoped_headers(**{request_security.CSRF_HEADER: token})
    if origin:
        headers["Origin"] = origin
    response = client.post("/api/conversations/101/stop", headers=headers)
    assert response.status_code == status


def test_forwarded_headers_do_not_authorize_embed_mutation(surface):
    client, _ = surface
    response = client.post("/api/conversations/101/stop", headers=scoped_headers(**{
        request_security.CSRF_HEADER: CSRF, "Origin": "https://attacker.example",
        "X-Forwarded-Host": "attacker.example", "X-Forwarded-Proto": "https",
    }))
    assert response.status_code == 403


def test_native_host_does_not_require_embed_registry_or_change_framing(surface, monkeypatch):
    client, store = surface
    monkeypatch.setattr(store, "app_for_host", AsyncMock(side_effect=RuntimeError("offline")))
    response = client.get("https://aurvek.example/admin/chat")
    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "DENY"
    assert "native_session=" in response.headers["set-cookie"]
    store.app_for_host.assert_not_awaited()


def test_embed_registry_and_worker_mismatch_fail_closed(surface, monkeypatch):
    client, store = surface
    monkeypatch.setenv("UVICORN_WORKERS", "2")
    worker = client.get(f"/embed/product/{FRAME_A}/chat", headers={"Accept-Language": "es"})
    assert worker.status_code == 503 and worker.headers["content-type"].startswith("text/html")
    assert "No se ha podido completar la operación" in worker.text
    assert "service_unavailable" not in worker.text
    monkeypatch.setenv("UVICORN_WORKERS", "1")
    monkeypatch.setattr(store, "app_for_host", AsyncMock(side_effect=RuntimeError("offline")))
    registry = client.get(f"/embed/product/{FRAME_A}/chat", headers={"Accept-Language": "fr"})
    assert registry.status_code == 503 and registry.headers["content-type"].startswith("text/html")
    assert "L’opération n’a pas pu être effectuée" in registry.text
    assert "service_unavailable" not in registry.text


def test_embed_session_inspection_failure_is_typed_and_does_not_serve_history(surface, monkeypatch):
    client, store = surface
    monkeypatch.setattr(store, "resolve_frame", AsyncMock(side_effect=RuntimeError("offline")))
    response = client.get("/api/conversations/101/messages", headers=scoped_headers())
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"
    assert not store.authorized
    document = client.get(f"/embed/product/{FRAME_A}/chat", headers={"Accept-Language": "it"})
    assert document.status_code == 503 and document.headers["content-type"].startswith("text/html")
    assert "Non è stato possibile completare l’operazione" in document.text
    assert "service_unavailable" not in document.text


def test_generation_scope_covers_stream_finalization(monkeypatch):
    from integrations.embed import activity
    monkeypatch.setenv("UVICORN_WORKERS", "1")
    monkeypatch.setattr(custom_domains, "_primary_domains", {"aurvek.example"})
    monkeypatch.setattr(activity, "_revalidate", AsyncMock())
    store = ScopedStore()
    states = []
    app = FastAPI()

    @app.post("/api/conversations/{conversation_id}/messages")
    async def generate(conversation_id: int):
        async def body():
            states.append(await activity.activity_state(conversation_id))
            yield b"data: first\n\n"
            states.append(await activity.activity_state(conversation_id))
            yield b"data: done\n\n"
            states.append(await activity.activity_state(conversation_id))
        return StreamingResponse(body(), media_type="text/event-stream")

    app.add_middleware(custom_domains.CustomDomainMiddleware)
    app.add_middleware(EmbedHostMiddleware, store=store)
    with TestClient(app, base_url=ORIGIN) as client:
        client.cookies.set(EMBED_COOKIE_NAME, COOKIE)
        response = client.post("/api/conversations/101/messages", headers=scoped_headers(**{
            "Origin": ORIGIN, request_security.CSRF_HEADER: CSRF,
        }))
    assert response.status_code == 200 and "data: done" in response.text
    assert states == ["generating", "generating", "generating"]
    assert 101 not in activity._active


def test_disabled_host_and_unverified_voice_are_closed(surface):
    client, store = surface
    response = client.get("/api/conversations/101/elevenlabs/config", headers=scoped_headers())
    assert response.status_code == 403
    store.app.enabled = False
    assert client.get("/static/js/chat/chat.js").status_code == 404


def test_legacy_websocket_is_not_exposed_on_embed_host(surface):
    client, _ = surface
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("/ws"):
            pass
    assert error.value.code == 4403


def test_unknown_host_cannot_reach_chat_or_socket_through_forwarded_host(surface):
    client, _ = surface
    response = client.get("https://unregistered.example/api/conversations/101/messages", headers={
        **scoped_headers(), "X-Forwarded-Host": HOST,
    })
    assert response.status_code == 404
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("wss://unregistered.example/ws"):
            pass
    assert error.value.code == 4403


def test_private_attachment_resolves_resource_conversation_before_serving(surface, monkeypatch, tmp_path):
    import database
    client, store = surface
    db_path = tmp_path / "attachments.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE FILE_ATTACHMENTS (public_id TEXT, user_id INTEGER, conversation_id INTEGER)")
        connection.executemany("INSERT INTO FILE_ATTACHMENTS VALUES (?,?,?)", [
            ("file-a", 1, 101), ("file-b", 1, 102), ("file-c", 2, 101),
        ])

    @asynccontextmanager
    async def connection_factory(readonly=False):
        async with aiosqlite.connect(db_path) as connection:
            yield connection

    monkeypatch.setattr(database, "get_db_connection", connection_factory)
    assert client.get(f"/api/attachments/file-b/content?embed_frame={FRAME_A}").status_code == 404
    assert client.get(f"/api/attachments/file-c/content?embed_frame={FRAME_A}").status_code == 404
    assert client.get(f"/api/attachments/missing/content?embed_frame={FRAME_A}").status_code == 404
    assert client.get(f"/api/attachments/file-a/download?embed_frame={FRAME_A}").status_code == 200
    assert store.authorized[-1] == ("product", "subject-a", "project-a", 101)


@pytest.mark.asyncio
async def test_embed_account_object_has_no_admin_bypass_or_persisted_preference_change(monkeypatch):
    import auth
    import rediscfg
    from models import User
    principal = ScopedStore().frames[FRAME_A]
    user = User(id=1, username="administrator", password=None, role_id=1,
                is_enabled=True, session_version=3, current_prompt_id=99,
                can_send_files=True, can_generate_images=True, is_admin=True)
    monkeypatch.setattr(auth, "get_user_by_id", AsyncMock(return_value=user))
    monkeypatch.setattr(rediscfg, "is_user_revoked", AsyncMock(return_value=False))
    request = Request({"type": "http", "state": {"embed_host": True, "embed_principal": principal}})
    loaded = await get_embed_user(request)
    assert loaded._is_admin is False
    assert await loaded.is_admin is False
    assert loaded.current_prompt_id == 10
    assert loaded.can_generate_images is False
    assert loaded.all_prompts_access is False
    user.session_version = 4
    assert await get_embed_user(request) is None
    user.session_version = 3
    user.is_enabled = False
    assert await get_embed_user(request) is None


def _message_request(principal):
    request = Request({"type": "http", "method": "POST", "scheme": "https", "path": "/api/conversations/101/messages",
                       "query_string": b"", "server": (HOST, 443),
                       "headers": [(b"host", HOST.encode()), (b"origin", ORIGIN.encode()),
                                   (request_security.CSRF_HEADER.lower().encode(), CSRF.encode())],
                       "state": {"embed_host": True, "embed_origin": ORIGIN, "embed_principal": principal}})
    request._form = FormData()
    return request


@pytest.mark.asyncio
async def test_embed_message_requires_no_native_cookie_and_retains_pause_rate_accounting(monkeypatch):
    from chat.services import message_requests
    monkeypatch.setattr(message_requests, "get_active_pause", AsyncMock(return_value=None))
    monkeypatch.setattr(message_requests, "check_rate_limit", AsyncMock(return_value=True))
    monkeypatch.setattr(message_requests, "increment_metric", AsyncMock())
    monkeypatch.setattr(message_requests, "increment_user_activity", AsyncMock())
    request = _message_request(ScopedStore().frames[FRAME_A])
    assert await message_requests.validate_message_request(request, SimpleNamespace(id=1)) is None
    message_requests.get_active_pause.assert_awaited_once_with(1)
    message_requests.check_rate_limit.assert_awaited_once_with(1, action="ai_call", limit=120, window_minutes=1)
    message_requests.increment_user_activity.assert_awaited_once_with(1)
    response = await message_requests.validate_message_request(request, SimpleNamespace(id=1), is_whatsapp=True)
    assert response.status_code == 403
    request._form = FormData({"multi_ai_models": "[1,2]"})
    response = await message_requests.validate_message_request(request, SimpleNamespace(id=1))
    assert response.status_code == 403
    for field in ("reasoning_mode", "reasoning_budget_tokens", "thinking_budget_tokens"):
        request._form = FormData({field: "99999"})
        response = await message_requests.validate_message_request(request, SimpleNamespace(id=1))
        assert response.status_code == 403

    from dataclasses import replace
    principal = request.state.embed_principal
    request.state.embed_principal = replace(
        principal, capabilities={**principal.capabilities, "multi_ai": True, "reasoning": True},
    )
    request._form = FormData({"multi_ai_models": "[1,2]", "reasoning_mode": "high"})
    assert await message_requests.validate_message_request(request, SimpleNamespace(id=1)) is None

@pytest.mark.parametrize("operation,method,capability", [
    ("model", "PATCH", "model_selection"), ("extension", "PATCH", "extensions"),
    ("voice/transcribe", "POST", "stt"), ("voice/tts", "POST", "tts"),
    ("voice/audio", "GET", "voice"), ("media/content", "GET", "text"),
])
def test_multimodal_routes_require_capability_and_exact_frame(surface, operation, method, capability):
    client, store = surface
    headers = scoped_headers(**{"Origin": ORIGIN, "X-GPTSub-CSRF": CSRF})
    path = f"/api/conversations/101/{operation}"
    store.frames[FRAME_A].capabilities[capability] = False
    assert client.request(method, path, headers=headers).status_code == 403
    store.frames[FRAME_A].capabilities[capability] = True
    assert client.request(method, path, headers=headers).status_code == 200
    assert client.request(method, path.replace('/101/', '/102/'), headers=headers).status_code == 404
