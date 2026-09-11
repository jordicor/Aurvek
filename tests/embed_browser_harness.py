"""Isolated layout fixture. Never sends a turn or calls an AI provider.

Run from the repository root: python -m tests.embed_browser_harness
"""
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("SECURITY_REDIS_MODE", "off")
os.environ.setdefault("USE_EMAIL_SERVICE", "false")

import aiosqlite
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

from integrations.embed import page_context
from integrations.embed.models import EmbedPrincipal
from i18n import LANGUAGES, Translator

PORT = 18793
ORIGIN = f"http://127.0.0.1:{PORT}"


@asynccontextmanager
async def lifespan(app):
    connection = await aiosqlite.connect(":memory:")
    connection.row_factory = aiosqlite.Row
    await connection.executescript("""
        CREATE TABLE USERS(id INTEGER, username TEXT);
        CREATE TABLE USER_DETAILS(user_id INTEGER, balance REAL,
            allow_file_upload INTEGER DEFAULT 0, allow_image_generation INTEGER DEFAULT 0,
            web_search_enabled INTEGER DEFAULT 0);
        CREATE TABLE PROMPTS(id INTEGER, name TEXT, description TEXT, is_paid INTEGER,
            forced_llm_id INTEGER, forced_reasoning_json TEXT, allowed_llms TEXT,
            hide_llm_name INTEGER DEFAULT 0, disable_web_search INTEGER DEFAULT 1, force_web_search INTEGER DEFAULT 0,
            extensions_enabled INTEGER DEFAULT 0, extensions_free_selection INTEGER DEFAULT 1,
            gransabio_enabled INTEGER DEFAULT 0);
        CREATE TABLE CONVERSATIONS(id INTEGER, user_id INTEGER, role_id INTEGER, llm_id INTEGER, chat_name TEXT, start_date TEXT, locked INTEGER);
        CREATE TABLE LLM(id INTEGER, machine TEXT, model TEXT, vision INTEGER, capabilities_json TEXT);
        INSERT INTO USERS VALUES(7, 'Layout fixture');
        INSERT INTO USER_DETAILS(user_id,balance) VALUES(7, 1.0);
        INSERT INTO PROMPTS(id,name,description,is_paid) VALUES(10, 'Interview', '', 0);
        INSERT INTO CONVERSATIONS VALUES(12, 7, 10, 22, 'Interview', '2026-09-06', 0);
        INSERT INTO LLM VALUES(22, 'OpenAI', 'layout-fixture', 0, '{}');
    """)

    @asynccontextmanager
    async def connect():
        yield connection

    async def false(*args):
        return False

    async def mode(*args):
        return "system_only"

    env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    env.globals.update(get_static_url=lambda value: value, get_static_theme_hashes=lambda: {})
    page_context.get_db_connection = connect
    page_context.user_requires_own_keys = false
    page_context.user_has_valid_api_keys = false
    page_context.get_user_api_key_mode = mode
    from i18n import template_context
    page_context.templates = SimpleNamespace(TemplateResponse=lambda name, context: HTMLResponse(
        env.get_template(name.lstrip("/")).render({**context, **template_context(context["request"])})))
    yield
    await connection.close()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="data/static"))


@app.get("/qa/{language}", response_class=HTMLResponse)
async def parent(language: str):
    if language not in LANGUAGES:
        return HTMLResponse("Invalid fixture language", status_code=404)
    return f'''<!doctype html><html lang="{language}"><meta name="viewport" content="width=device-width, initial-scale=1"><style>html,body,iframe{{margin:0;width:100%;height:100%;border:0}}body{{height:100dvh}}</style><iframe title="Shared chat layout fixture" src="/embed/sample/frame_instance_{language}/chat"></iframe><script>window.fixtureEvents=[];addEventListener('message',e=>{{if(e.origin===location.origin&&e.source===document.querySelector('iframe').contentWindow)window.fixtureEvents.push(e.data)}})</script></html>'''


@app.get("/embed/sample/{frame}/chat")
async def chat(request: Request, frame: str):
    principal = EmbedPrincipal(
        app_id="sample", issuer="https://identity.example", subject="layout-fixture",
        user_id=7, prompt_id=10, delegated_session_id="fixture-only", session_version=1,
        membership_version=1, expires_at=9999999999, capabilities={"text": True, "voice": False, "attachments": False},
        conversation_id=12, external_project_id="layout-fixture", parent_origin=ORIGIN,
        embed_origin=ORIGIN, frame_instance_id=frame, ui_language=frame[-2:], csrf_token="fixture-csrf",
    )
    return await page_context.render_embed_chat(request, principal, SimpleNamespace(display_name="Interview", theme="katarishoji"))


@app.get("/embed/sample/{frame}/session")
async def session(frame: str, ui_language: str | None = None):
    result = {"expired": False, "expires_in": 7200}
    if ui_language is not None:
        if ui_language not in LANGUAGES:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        result["i18n"] = Translator(ui_language).browser_payload((
            "navigation", "chat", "chat_widgets", "chat_ui", "chat_errors", "embed", "application"))
    return result


@app.get("/embed/sample/{frame}/activity")
async def activity(frame: str):
    return {"activity": "idle", "closed": True}


@app.get("/api/conversations/12/messages")
async def history():
    return {"messages": [{"id": 1, "conversation_id": 12, "type": "user", "message": "Texto de prueba para comprobar el historial y los controles del chat.", "date": "2026-09-06 00:00:00", "is_bookmarked": False}], "has_more": False, "conversation_info": {"prompt_name": "Interview", "prompt_description": "", "is_paid": False}}


@app.post("/{path:path}")
async def deny_turn(path: str):
    return JSONResponse({"error": "layout_fixture_does_not_send"}, status_code=403)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
