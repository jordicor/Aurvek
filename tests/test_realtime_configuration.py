"""Shared settings preserve choices and never dispatch an unconfigured model."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from integrations.embed.models import EmbedError
from integrations.live import configuration as live
from integrations.live import session as sessions
from integrations.realtime.configuration import resolve_selection
from tests.test_native_live_context import context_db


@pytest.mark.asyncio
async def test_phone_browser_and_device_share_selection_with_disabled_subscription_chat(context_db):
    await context_db.execute("INSERT INTO LLM VALUES(4,'GPTSub','gpt-5.6-luna',0,'{}')")
    await context_db.execute("UPDATE CONVERSATIONS SET llm_id=4")
    await context_db.execute("UPDATE PROMPTS SET realtime_backend_llm_id=1")
    choices = [await resolve_selection(context_db, prompt_id=10, conversation_llm_id=4, channel=channel)
               for channel in ("phone", "live", "device")]
    assert all(choice.matches(choices[0]) for choice in choices)
    assert choices[0].backend["id"] == 1 and choices[0].model["id"] == 2
    assert (await live.configuration(context_db, 1, 5))["voice"] == "Kore"
    assert (await (await context_db.execute("SELECT llm_id FROM CONVERSATIONS")).fetchone())[0] == 4
    await context_db.execute("UPDATE PROMPTS SET realtime_backend_llm_id=NULL")
    with pytest.raises(EmbedError, match="live_backend_unavailable"):
        await live.configuration(context_db, 1, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [None, 1, 4])
async def test_live_never_substitutes_gemini_for_an_incompatible_selection(context_db, selected):
    await context_db.execute("INSERT INTO LLM VALUES(4,'GPT','gpt-realtime-2.1-mini',1,'{}')")
    await context_db.execute("UPDATE PROMPTS SET realtime_llm_id=?", (selected,))
    with pytest.raises(EmbedError, match="live_model_incompatible"):
        await live.configuration(context_db, 1, 5)


@pytest.mark.asyncio
async def test_client_model_override_cannot_replace_the_owner_selection(context_db):
    for channel in ("live", "device"):
        with pytest.raises(EmbedError, match="live_configuration_changed"):
            await live.configuration(context_db, 1, 5, 3, channel=channel)


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", [None, 1, 2])
async def test_prompt_creation_validates_inherited_standard_and_gemini_settings(context_db, monkeypatch, model_id):
    import prompts

    @asynccontextmanager
    async def connection(**_kwargs):
        yield context_db

    monkeypatch.setattr(prompts, "get_db_connection", connection)
    result = await prompts._validate_realtime_ai_selection(realtime_llm_id=model_id,
        realtime_reasoning_mode="inherit", realtime_reasoning_budget_tokens=None,
        realtime_voice="Puck" if model_id == 2 else None, inherited_llm_id=None,
        realtime_backend_llm_id=1 if model_id == 2 else None)
    assert result == (model_id, None, "Puck" if model_id == 2 else None)


@pytest.mark.asyncio
async def test_session_revalidation_ignores_catalog_copy_but_fences_backend_settings(context_db, monkeypatch):
    @asynccontextmanager
    async def connection(**_kwargs):
        yield context_db

    monkeypatch.setattr(sessions, "get_db_connection", connection)
    await context_db.execute("ALTER TABLE LLM ADD COLUMN last_synced_at TEXT")
    await context_db.execute("ALTER TABLE LLM ADD COLUMN description TEXT")
    user = SimpleNamespace(id=5, is_enabled=True)
    session = sessions.LiveMediaSession(None, conversation_id=1, user=user, request=None,
        check_identity=AsyncMock(return_value=(user, None)))
    session.settings, _ = await session.check_access()
    await context_db.execute("UPDATE LLM SET last_synced_at='later',description='New catalog copy'")
    await session.check_access()
    await context_db.execute("UPDATE PROMPTS SET realtime_backend_llm_id=1")
    with pytest.raises(EmbedError, match="live_configuration_changed"):
        await session.check_access()


@pytest.mark.asyncio
async def test_settings_api_requires_owner_and_csrf_and_persists_gemini_backend(context_db, monkeypatch):
    from integrations.realtime import routes
    import request_security
    from request_security import CSRF_SESSION_KEY

    monkeypatch.setattr(request_security, "PRIMARY_APP_DOMAIN", "test")

    @asynccontextmanager
    async def connection(**_kwargs):
        yield context_db

    class Owner:
        id = 5
        is_enabled = True

        @property
        async def is_admin(self):
            return False

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_current_user] = lambda: Owner()

    @app.middleware("http")
    async def session_fixture(request, call_next):
        request.scope["session"] = {CSRF_SESSION_KEY: "s" * 32}
        return await call_next(request)

    manager = AsyncMock(return_value=False)
    monkeypatch.setattr(routes, "can_manage_prompt", manager)
    monkeypatch.setattr(routes, "get_db_connection", connection)
    payload = {"llm_id": 2, "backend_llm_id": 1, "realtime_voice": "Puck"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        assert (await client.get("/api/prompts/10/realtime-settings")).status_code == 403
        manager.return_value = True
        assert (await client.put("/api/prompts/10/realtime-settings", json=payload)).status_code == 403
        assert (await client.put("/api/prompts/10/realtime-settings", json=payload,
            headers={"Origin": "https://other.test", "X-GPTSub-CSRF": "s" * 32})).status_code == 403
        response = await client.put("/api/prompts/10/realtime-settings", json=payload,
                                    headers={"Origin": "https://test", "X-GPTSub-CSRF": "s" * 32})
        assert response.status_code == 200, response.text
        saved = (await client.get("/api/prompts/10/realtime-settings")).json()
        assert saved["realtime_voice"] == "Puck" and saved["realtime_backend_llm_id"] == 1
        payload["backend_llm_id"] = 3
        denied = await client.put("/api/prompts/10/realtime-settings", json=payload,
                                 headers={"Origin": "https://test", "X-GPTSub-CSRF": "s" * 32})
        assert denied.status_code == 422
        assert (await client.get("/api/prompts/10/realtime-settings")).json() == saved
