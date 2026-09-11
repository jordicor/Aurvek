from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request
from i18n import template_context

from integrations.embed import page_context, pages
from integrations.embed.models import EmbedError
from integrations.embed.models import EmbedPrincipal
from integrations.embed.api import PrivateRoute


@pytest.fixture
def client(monkeypatch):
    principal = SimpleNamespace(
        app_id="sample", frame_instance_id="frame_instance_123", expires_at=9999999999
    )
    store = SimpleNamespace(consume_bootstrap=AsyncMock(return_value=("opaque-cookie", principal)))
    monkeypatch.setattr(pages, "get_embed_store", lambda: store)
    app = FastAPI()
    app.include_router(pages.router)

    @app.exception_handler(EmbedError)
    async def embed_error(request, error):
        return JSONResponse({"error": error.code}, status_code=error.status_code)

    return TestClient(app, base_url="https://chat.example", follow_redirects=False), store


def test_bootstrap_consumes_body_with_actual_host_origin_and_sets_distinct_cookie(client):
    browser, store = client
    response = browser.post(
        "/embed/bootstrap", data={"ticket": "opaque-ticket"},
        headers={"Origin": "https://product.example"},
    )
    assert response.status_code == 303
    store.consume_bootstrap.assert_awaited_once_with("opaque-ticket", "chat.example", "https://product.example")
    assert response.headers["location"] == "/embed/sample/frame_instance_123/chat"
    assert "opaque-ticket" not in response.headers["location"]
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-aurvek_embed=")
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert "Domain=" not in cookie
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize("path,body,content_type", [
    ("/embed/bootstrap?ticket=bad", "ticket=ok", "application/x-www-form-urlencoded"),
    ("/embed/bootstrap", "ticket=first&ticket=second", "application/x-www-form-urlencoded"),
    ("/embed/bootstrap", "ticket=first&other=value", "application/x-www-form-urlencoded"),
    ("/embed/bootstrap", "ticket=" + "x" * 5000, "application/x-www-form-urlencoded"),
    ("/embed/bootstrap", '{"ticket":"first"}', "application/json"),
])
def test_bootstrap_rejects_url_credentials_duplicate_fields_and_oversized_body(client, path, body, content_type):
    browser, store = client
    response = browser.post(path, content=body, headers={"Content-Type": content_type})
    assert response.status_code == (413 if len(body) > 4096 else 400)
    store.consume_bootstrap.assert_not_awaited()
    assert "set-cookie" not in response.headers


def test_invalid_bootstrap_document_is_localized_without_protocol_codes(client):
    browser, _ = client
    response = browser.post(
        "/embed/bootstrap", content="not-a-form", headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept-Language": "es",
        },
    )
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert '<html lang="es">' in response.text
    assert "No se ha podido completar la operación" in response.text
    assert "invalid_request" not in response.text and "Aurvek" not in response.text


def test_routed_chat_failure_uses_verified_request_principal_language():
    app = FastAPI()
    router = APIRouter(route_class=PrivateRoute)

    @router.get("/embed/{app_id}/{frame_instance_id}/chat")
    async def failed_chat(request: Request, app_id: str, frame_instance_id: str):
        request.state.embed_principal = SimpleNamespace(ui_language="ja")
        raise EmbedError("service_unavailable", 503)

    app.include_router(router)
    with TestClient(app, base_url="https://chat.example") as browser:
        response = browser.get(
            "/embed/sample/frame_instance_123/chat", headers={"Accept-Language": "de"}
        )
    assert response.status_code == 503
    assert '<html lang="ja">' in response.text
    assert "操作を完了できませんでした" in response.text
    assert "service_unavailable" not in response.text and "Aurvek" not in response.text


def test_bootstrap_get_never_establishes_identity(client):
    browser, store = client
    assert browser.get("/embed/bootstrap?user_id=1&conversation_id=12").status_code == 405
    store.consume_bootstrap.assert_not_awaited()


@pytest.mark.parametrize("language", ["en", "es", "ja", "fr", "pt", "it", "de"])
def test_session_can_return_one_authenticated_language_payload(monkeypatch, language):
    principal = EmbedPrincipal(
        app_id="sample", issuer="https://identity.example", subject="subject", user_id=7,
        prompt_id=10, delegated_session_id="delegated", session_version=1,
        membership_version=1, expires_at=9999999999, capabilities={"text": True},
        conversation_id=12, external_project_id="project", parent_origin="https://product.example",
        embed_origin="https://chat.example", frame_instance_id="frame_instance_123",
    )
    store = SimpleNamespace(resolve_frame=AsyncMock(return_value=principal))
    monkeypatch.setattr(pages, "get_embed_store", lambda: store)
    app = FastAPI()
    app.include_router(pages.router)
    with TestClient(app, base_url="https://chat.example") as browser:
        browser.cookies.set("__Host-aurvek_embed", "opaque-cookie")
        response = browser.get(f"/embed/sample/frame_instance_123/session?ui_language={language}")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"expired", "expires_in", "capabilities", "i18n"}
    assert body["i18n"]["language"] == language
    assert set(body["i18n"]["resources"]) == ({"en"} if language == "en" else {"en", language})
    assert set(body["i18n"]["resources"]["en"]) == {
        "common", "navigation", "chat", "chat_widgets", "chat_ui", "chat_errors",
        "embed", "application",
    }


def test_session_without_language_preserves_contract_and_invalid_is_rejected(monkeypatch):
    principal = EmbedPrincipal(
        app_id="sample", issuer="https://identity.example", subject="subject", user_id=7,
        prompt_id=10, delegated_session_id="delegated", session_version=1,
        membership_version=1, expires_at=9999999999, capabilities={"text": True},
        conversation_id=12, external_project_id="project", parent_origin="https://product.example",
        embed_origin="https://chat.example", frame_instance_id="frame_instance_123",
    )
    store = SimpleNamespace(resolve_frame=AsyncMock(return_value=principal))
    monkeypatch.setattr(pages, "get_embed_store", lambda: store)
    app = FastAPI()
    app.include_router(pages.router)
    with TestClient(app, base_url="https://chat.example") as browser:
        ordinary = browser.get("/embed/sample/frame_instance_123/session")
        invalid = browser.get("/embed/sample/frame_instance_123/session?ui_language=xx")
    assert set(ordinary.json()) == {"expired", "expires_in", "capabilities"}
    assert invalid.status_code == 400 and invalid.json()["error"] == "invalid_request"
    assert store.resolve_frame.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("application_options", [False, True])
async def test_embed_render_uses_only_bound_conversation_and_keeps_native_preferences(monkeypatch, application_options):
    connection = await aiosqlite.connect(":memory:")
    connection.row_factory = aiosqlite.Row
    await connection.executescript("""
        CREATE TABLE USERS(id INTEGER, username TEXT);
        CREATE TABLE USER_DETAILS(user_id INTEGER, balance REAL, current_prompt_id INTEGER, llm_id INTEGER, theme TEXT,
            allow_file_upload INTEGER DEFAULT 1, allow_image_generation INTEGER DEFAULT 1, web_search_enabled INTEGER DEFAULT 1);
        CREATE TABLE PROMPTS(id INTEGER, name TEXT, description TEXT, is_paid INTEGER,
            forced_llm_id INTEGER, forced_reasoning_json TEXT, allowed_llms TEXT,
            hide_llm_name INTEGER DEFAULT 0, disable_web_search INTEGER DEFAULT 0, force_web_search INTEGER DEFAULT 0,
            extensions_enabled INTEGER DEFAULT 0, extensions_free_selection INTEGER DEFAULT 1, gransabio_enabled INTEGER DEFAULT 0);
        CREATE TABLE CONVERSATIONS(id INTEGER, user_id INTEGER, role_id INTEGER, llm_id INTEGER, chat_name TEXT, start_date TEXT, locked INTEGER,
            active_extension_id INTEGER);
        CREATE TABLE LLM(id INTEGER, machine TEXT, model TEXT, vision INTEGER, capabilities_json TEXT, enabled INTEGER DEFAULT 1, display_name TEXT);
        CREATE TABLE APPLICATION_ASSISTANTS(app_id TEXT, assistant_id TEXT, config_json TEXT);
        CREATE TABLE PROMPT_EXTENSIONS(id INTEGER, prompt_id INTEGER, name TEXT, slug TEXT, description TEXT, display_order INTEGER);
        INSERT INTO USERS VALUES(7, 'test account');
        INSERT INTO USER_DETAILS(user_id,balance,current_prompt_id,llm_id,theme) VALUES(7, 1.0, 99, 100, 'terminal');
        INSERT INTO PROMPTS(id,name,description,is_paid) VALUES(10, 'Interview', 'Shared prompt description', 0);
        INSERT INTO CONVERSATIONS VALUES(12, 7, 10, 22, 'My interview', '2026-09-06', 0, 1);
        INSERT INTO CONVERSATIONS VALUES(13, 7, 10, 22, 'Other product private title', '2026-09-06', 0, NULL);
        INSERT INTO LLM(id,machine,model,vision,capabilities_json) VALUES(22, 'OpenAI', 'test-model', 1, '{}');
        INSERT INTO LLM(id,machine,model,vision,capabilities_json) VALUES(23, 'Claude', 'second-model', 1, '{}');
        INSERT INTO LLM(id,machine,model,vision,capabilities_json) VALUES(24, 'GPT', 'disallowed-model', 1, '{}');
        INSERT INTO APPLICATION_ASSISTANTS VALUES('sample','coach','{"display_name":"Coach"}');
        INSERT INTO PROMPT_EXTENSIONS VALUES(1,10,'Level one','one','First level',1);
    """)

    @asynccontextmanager
    async def connect():
        yield connection

    monkeypatch.setattr(page_context, "get_db_connection", connect)
    monkeypatch.setattr(page_context, "user_requires_own_keys", AsyncMock(return_value=False))
    monkeypatch.setattr(page_context, "user_has_valid_api_keys", AsyncMock(return_value=False))
    monkeypatch.setattr(page_context, "get_user_api_key_mode", AsyncMock(return_value="system_only"))
    monkeypatch.setattr(page_context, "get_subscription_auth_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(page_context, "templates", SimpleNamespace(TemplateResponse=lambda name, context: (name, context)))
    principal = SimpleNamespace(
        app_id="sample", subject="subject", user_id=7, prompt_id=10, conversation_id=12,
        frame_instance_id="frame_instance_123", external_project_id="project-1", parent_origin="https://product.example",
        ui_language="ja", csrf_token="csrf", expires_at=9999999999, capabilities={"text": True, "voice": False},
    )
    if application_options:
        principal.application = SimpleNamespace(assistant_id="coach", context_id="project")
        principal.capabilities.update({name: True for name in (
            "model_selection", "reasoning", "extensions", "multi_ai", "attachments", "image_generation", "tools", "web_search",
        )})
        await connection.execute("UPDATE PROMPTS SET allowed_llms='[22,23]',extensions_enabled=1 WHERE id=10")
    request = Request({"type": "http", "headers": [(b"accept-language", b"de")]})
    template, context = await page_context.render_embed_chat(
        request, principal, SimpleNamespace(display_name="Sample", theme="katarishoji")
    )
    assert request.state.i18n.language == "ja"
    assert request.state.i18n.browser_payload()["language"] == "ja"
    assert template == "/chat/chat.html"
    assert [row["id"] for row in context["initial_conversations"]] == [12]
    assert {row["id"] for row in context["llm_models"]} == ({22, 23} if application_options else {22})
    controls = context["embed_config"]["controls"]
    assert controls["model_selection"] is application_options
    assert controls["reasoning"] is application_options
    assert controls["multi_ai"] is application_options
    assert context["can_send_files"] is application_options
    assert context["can_generate_images"] is application_options
    if application_options:
        assert context["initial_conversations"][0]["active_extension"]["id"] == 1
        assert context["initial_conversations"][0]["allowed_llms"] == [22, 23]
        assert context["have_vision"]
    assert not context["is_admin"] and not context["is_user"]
    assert context["chat_folders"] == []
    cursor = await connection.execute("SELECT current_prompt_id, llm_id, theme FROM USER_DETAILS")
    assert tuple(await cursor.fetchone()) == (99, 100, "terminal")
    env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    env.globals.update(get_static_url=lambda value: value, get_static_theme_hashes=lambda: {})
    context.update(template_context(request))
    html = env.get_template("chat/chat.html").render(context)
    assert 'lang="ja"' in html and 'data-presentation="embedded"' in html
    assert "chat-katarishoji.css" in html
    assert 'src="/static/js/chat/chat.js"' in html
    assert 'src="/static/js/themeManager.js"' not in html
    assert 'src="/static/js/theme-loader.js"' not in html
    assert 'src="/sdk/elevenlabs-client.js"' not in html
    assert "Other product private title" not in html
    if application_options:
        await connection.execute("UPDATE PROMPTS SET forced_llm_id=22,forced_reasoning_json='{}' WHERE id=10")
        _, forced_context = await page_context.render_embed_chat(
            request, principal, SimpleNamespace(display_name="Sample", theme="katarishoji"),
        )
        assert [row["id"] for row in forced_context["llm_models"]] == [22]
        forced_controls = forced_context["embed_config"]["controls"]
        assert not any(forced_controls[key] for key in ("model_selection", "reasoning", "multi_ai"))

        principal.capabilities["gransabio"] = True
        await connection.execute("UPDATE PROMPTS SET gransabio_enabled=1,forced_llm_id=NULL,forced_reasoning_json=NULL WHERE id=10")
        _, gs_context = await page_context.render_embed_chat(
            request, principal, SimpleNamespace(display_name="Sample", theme="katarishoji"),
        )
        gs_controls = gs_context["embed_config"]["controls"]
        assert gs_controls["gransabio"]
        assert not any(gs_controls[key] for key in ("model_selection", "reasoning", "multi_ai", "attachments", "image_generation"))
    await connection.close()
