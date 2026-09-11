"""Single-user, loopback-only integration demo. No business workflow or new chat client.

Run from the Aurvek checkout after configuring the demo app; see README.md here.
In a real consumer, derive external_user_id from its authenticated server session.
"""
from contextlib import asynccontextmanager
import asyncio
import ipaddress
import os
from pathlib import Path
import secrets
import time

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from integrations.applications.client import AurvekClient, AurvekError

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app):
    app.state.app_id = os.environ["AURVEK_DEMO_APP_ID"]
    app.state.parent = os.environ["AURVEK_DEMO_PARENT_ORIGIN"]
    app.state.embed = os.environ["AURVEK_DEMO_EMBED_ORIGIN"]
    app.state.member = os.environ.get("AURVEK_DEMO_MEMBER", "demo-member")
    app.state.context_ref = os.environ.get("AURVEK_DEMO_CONTEXT", "practice")
    app.state.session, app.state.profile = None, None
    app.state.lock = asyncio.Lock()
    async with AurvekClient(os.environ["AURVEK_DEMO_ISSUER"], app.state.app_id,
                            os.environ["AURVEK_CLIENT_SECRET"],
                            loopback_url=os.environ.get("AURVEK_DEMO_LOOPBACK_URL")) as client:
        app.state.client = client
        yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def local_demo_only(request, call_next):
    if not request.client or not ipaddress.ip_address(request.client.host).is_loopback:
        return JSONResponse({"error": "local_demo_only"}, status_code=403)
    if request.method == "POST" and request.url.path not in {"/lookup", "/lookup-alternate"}:
        if request.headers.get("origin") != app.state.parent:
            return JSONResponse({"error": "invalid_origin"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    # The bootstrap form must send its parent Origin. no-referrer makes that
    # Origin opaque (null) on cross-origin POST navigation in browsers.
    response.headers["Referrer-Policy"] = "strict-origin"
    return response


@app.exception_handler(AurvekError)
async def aurvek_error(request, exc):
    if exc.status_code == 401:
        app.state.session = None
    return JSONResponse({"error": exc.code}, status_code=exc.status_code)


async def identity():
    async with app.state.lock:
        session = app.state.session
        if session and session["expires_at"] > time.time() + 60:
            return session["delegated_credential"]
        client, member = app.state.client, app.state.member
        try:
            profile = await client.account_status(member)
        except AurvekError as exc:
            if exc.status_code != 404:
                raise
            profile = await client.provision(member, "demo-provision-0001", display_name="Demo member")
        session = await client.account_session(member)
        await client.request("channels/context", {"external_user_id": member,
            "context_ref": app.state.context_ref})
        app.state.profile, app.state.session = profile, session
        return session["delegated_credential"]


@app.get("/")
async def home():
    return FileResponse(HERE / "index.html")


@app.get("/aurvek-applications.js")
async def sdk():
    return FileResponse(ROOT / "data/static/js/aurvek-applications.js", media_type="text/javascript")


@app.post("/session")
async def session():
    credential = await identity()
    opened = await app.state.client.open_conversation(credential, secrets.token_urlsafe(24),
        context_ref=app.state.context_ref)
    bootstrap = await app.state.client.bootstrap(credential, opened["conversation_id"],
        parent_origin=app.state.parent, embed_origin=app.state.embed,
        frame_instance_id=secrets.token_urlsafe(24), ui_language="es")
    return {"appId": app.state.app_id, "contextId": opened["context_id"],
        "conversationId": opened["conversation_id"], "assistantId": opened["assistant_id"],
        "bootstrap": bootstrap, "profile": app.state.profile}


class Conversation(BaseModel):
    conversation_id: int = Field(gt=0)


class BootstrapConversation(Conversation):
    assistant_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")


@app.post("/bootstrap")
async def fresh_bootstrap(body: BootstrapConversation):
    credential = await identity()
    # Reauthorize this exact destination in the demo's server-owned context.
    # A fallback must not silently select another conversation or assistant.
    opened = await app.state.client.open_conversation(credential, secrets.token_urlsafe(24),
        context_ref=app.state.context_ref, assistant_id=body.assistant_id,
        conversation_id=body.conversation_id)
    bootstrap = await app.state.client.bootstrap(credential, opened["conversation_id"],
        parent_origin=app.state.parent, embed_origin=app.state.embed,
        frame_instance_id=secrets.token_urlsafe(24), ui_language="es")
    return {"appId": app.state.app_id, "contextId": opened["context_id"], "bootstrap": bootstrap}


class SourceQuery(Conversation):
    kind: str = "messages"
    cursor: str | None = None


@app.post("/sources")
async def sources(body: SourceQuery):
    return await app.state.client.sources(await identity(), body.conversation_id,
        context_ref=app.state.context_ref, kind=body.kind, cursor=body.cursor, limit=20)


class ToolQuery(Conversation):
    operation_id: str
    topic: str = Field(default="practice", max_length=100)


@app.post("/remote-query")
async def remote_query(body: ToolQuery):
    return await app.state.client.invoke_tool(await identity(), body.conversation_id,
        "app_http_demo_lookup", {"topic": body.topic}, body.operation_id, context_ref=app.state.context_ref)


class Profile(BaseModel):
    operation_id: str
    expected_version: int
    display_name: str = Field(max_length=120)
    pending_email: str | None = None


@app.post("/profile")
async def profile(body: Profile):
    result = await app.state.client.update_profile(await identity(), app.state.member,
        body.operation_id, body.expected_version, display_name=body.display_name, pending_email=body.pending_email)
    app.state.profile = result
    return result


@app.post("/lookup")
@app.post("/lookup-alternate")
async def lookup(request: Request):
    # Fixed, non-sensitive demo data; the listener itself is loopback-only.
    data = await request.json()
    return {"topic": data.get("arguments", {}).get("topic"),
        "exercise": "Describe a familiar place in three sentences.",
        "source": "alternate" if request.url.path.endswith("alternate") else "demo"}
