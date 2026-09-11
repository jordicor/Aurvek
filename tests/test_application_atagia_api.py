"""ASGI contract tests; provider runtime is fake and performs no external I/O."""
from contextlib import asynccontextmanager
from dataclasses import dataclass
import sys
from types import ModuleType, SimpleNamespace

from fastapi import FastAPI, Header, HTTPException, Request
import httpx
import pytest

from integrations.applications.memory import MemoryNamespace
from memory.atagia_application_api import create_app


@dataclass
class FakeAuthContext:
    service_mode: bool
    claimed_user_id: str | None


@pytest.fixture
def atagia_api(monkeypatch):
    """Stand in for optional Atagia imports, preserving its published auth contract.

    The app factory/auth dependencies are injected through their original import
    locations; tests exercise the real Aurvek route, facade and retrieval helper.
    """
    events = []
    runtime = SimpleNamespace(settings=SimpleNamespace(service_mode=True, service_api_key="server-secret"))

    class Connection:
        async def close(self):
            events.append(("close_connection",))

    async def open_connection():
        events.append(("open_connection",))
        return Connection()

    runtime.open_connection = open_connection

    def get_runtime(request):
        return request.app.state.runtime

    def get_auth_context(request: Request, authorization: str | None = Header(default=None),
                         x_atagia_user_id: str | None = Header(default=None, alias="X-Atagia-User-Id")):
        settings = get_runtime(request).settings
        if not settings.service_mode:
            return FakeAuthContext(False, None)
        if authorization != "Bearer " + settings.service_api_key or not x_atagia_user_id:
            raise HTTPException(401, "Invalid service authentication")
        return FakeAuthContext(True, x_atagia_user_id)

    def ensure_user_access(user_id, auth):
        events.append(("authorize", user_id))
        if auth.claimed_user_id != user_id:
            raise HTTPException(403, "Authenticated user does not match the requested user_id")

    def native_factory(settings=None):
        events.append(("native_factory", settings))

        @asynccontextmanager
        async def lifespan(app):
            events.append(("native_start",))
            yield
            events.append(("native_stop",))

        app = FastAPI(lifespan=lifespan)
        app.state.runtime = runtime

        @app.get("/native-existing-route")
        async def existing():
            return {"native": True}

        return app

    class Sidecar:
        def __init__(self, provider_runtime):
            assert provider_runtime is runtime

        async def ensure_user_exists(self, connection, user):
            events.append(("ensure_user", user))

        async def ensure_conversation(self, connection, *, user_id, conversation_id,
                                      workspace_id, assistant_mode_id, **kwargs):
            events.append(("ensure_conversation", {"user_id": user_id, "conversation_id": conversation_id,
                "workspace_id": workspace_id, "assistant_mode_id": assistant_mode_id, **kwargs}))
            return {"id": conversation_id}

        async def get_context(self, *args, **kwargs):
            pytest.fail("Ingesting context operation must never be called")

        async def ingest_message(self, *args, **kwargs):
            pytest.fail("A memory query must never be ingested")

    class ContextCache:
        def __init__(self, provider_runtime):
            assert provider_runtime is runtime

        async def resolve_with_connection(self, connection, **kwargs):
            events.append(("retrieve", kwargs))
            return SimpleNamespace(composed_context=SimpleNamespace(contract_block="contract",
                workspace_block="workspace", memory_block="preferred short answers", state_block="state",
                recent_window="private other assistant transcript must be excluded"),
                memory_summaries=[{"id": "scoped-memory", "text": "preferred short answers"}])

    modules = {"atagia": {}, "atagia.api": {}, "atagia.services": {},
        "atagia.app": {"create_app": native_factory},
        "atagia.api.dependencies": {"get_auth_context": get_auth_context, "ensure_user_access": ensure_user_access,
                                    "get_runtime": get_runtime},
        "atagia.services.sidecar_service": {"SidecarService": Sidecar},
        "atagia.services.context_cache_service": {"ContextCacheService": ContextCache}}
    for name, attributes in modules.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    settings = object()
    app = create_app(settings)
    namespace = MemoryNamespace("first-app", "context", "member:subject", "private", "guide")
    body = {"namespace": namespace.as_dict(), "conversation_id": 8, "message": "What did I prefer?",
            "mode": "personal_assistant", "platform_id": "aurvek"}
    headers = {"Authorization": "Bearer server-secret", "X-Atagia-User-Id": namespace.provider_user_id}
    return SimpleNamespace(app=app, runtime=runtime, events=events, settings=settings,
                           namespace=namespace, body=body, headers=headers)


@pytest.mark.asyncio
async def test_inherits_native_routes_lifecycle_and_existing_runtime_without_ingestion(atagia_api):
    env = atagia_api
    async with env.app.router.lifespan_context(env.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://provider") as client:
            assert (await client.get("/native-existing-route")).json() == {"native": True}
            response = await client.post("/v1/context/read-only", json=env.body, headers=env.headers)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"system_prompt": "contract\n\nworkspace\n\npreferred short answers\n\nstate",
        "memories": [{"id": "scoped-memory", "text": "preferred short answers"}],
        "partial": True, "namespace_key": env.namespace.key}
    assert env.events.count(("native_factory", env.settings)) == 1
    assert env.events.count(("native_start",)) == env.events.count(("native_stop",)) == 1
    assert env.events.count(("open_connection",)) == env.events.count(("close_connection",)) == 3
    conversation = next(event[1] for event in env.events if event[0] == "ensure_conversation")
    assert conversation == {"user_id": env.namespace.provider_user_id,
        "conversation_id": env.namespace.provider_conversation_id(8),
        "workspace_id": None, "assistant_mode_id": None,
        "user_persona_id": env.namespace.persona_id, "character_id": env.namespace.character_id,
        "platform_id": "aurvek", "mode": "personal_assistant", "incognito": False}
    retrieval = next(event[1] for event in env.events if event[0] == "retrieve")
    assert retrieval["user_id"] == env.namespace.provider_user_id
    assert retrieval["conversation_id"] == env.namespace.provider_conversation_id(8)
    assert retrieval["message_text"] == env.body["message"]
    assert "private other assistant" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"X-Atagia-User-Id": "forged"},
    {"Authorization": "Bearer bad", "X-Atagia-User-Id": "forged"}, {"Authorization": "Bearer server-secret"}])
async def test_missing_or_forged_authentication_never_calls_runtime(atagia_api, headers):
    env = atagia_api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://provider") as client:
        response = await client.post("/v1/context/read-only", json=env.body, headers=headers)
    assert response.status_code == 401
    assert not any(event[0] in {"retrieve", "open_connection"} for event in env.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("app_id", "other-app"), ("context_id", "other-context"),
                                       ("subject", "member:other"), ("assistant_id", "other-assistant")])
async def test_valid_service_key_cannot_relabel_authenticated_namespace(atagia_api, field, value):
    env = atagia_api
    env.body["namespace"][field] = value
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://provider") as client:
        response = await client.post("/v1/context/read-only", json=env.body, headers=env.headers)
    assert response.status_code == 403
    assert not any(event[0] in {"retrieve", "open_connection"} for event in env.events)


@pytest.mark.asyncio
async def test_insecure_library_mode_does_not_open_new_route(atagia_api):
    env = atagia_api
    env.runtime.settings.service_mode = False
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://provider") as client:
        response = await client.post("/v1/context/read-only", json=env.body, headers=env.headers)
        assert (await client.get("/native-existing-route")).status_code == 200
    assert response.status_code == 401
    assert not any(event[0] == "open_connection" for event in env.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"user_id": "arbitrary-provider-user"}, {"conversation_id": "8"},
    {"message": "x" * 131073}, {"namespace": {"app_id": "missing-other-fields"}}])
async def test_body_cannot_supply_arbitrary_provider_ids_or_unbounded_query(atagia_api, changes):
    env = atagia_api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://provider") as client:
        response = await client.post("/v1/context/read-only", json={**env.body, **changes}, headers=env.headers)
    assert response.status_code == 422
    assert not any(event[0] == "open_connection" for event in env.events)


@pytest.mark.asyncio
async def test_retrieval_failure_is_sanitized_and_does_not_fall_back_to_ingestion(atagia_api, monkeypatch):
    env = atagia_api

    async def failed(*args, **kwargs):
        raise RuntimeError("Sensitive internal provider state")

    monkeypatch.setattr("atagia_bridge.read_application_atagia_context", failed)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://provider") as client:
        response = await client.post("/v1/context/read-only", json=env.body, headers=env.headers)
    assert response.status_code == 503 and "Sensitive" not in response.text
    assert not any(event[0] == "open_connection" for event in env.events)
