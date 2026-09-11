"""Expected authenticated backend 404s must not ban the shared loopback service."""
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from integrations.applications import api
from integrations.applications.accounts import ApplicationAccountService
from integrations.embed import api as embed_api
from middleware import security
from tests.test_embed_native_creation import native_pilot


@pytest.mark.asyncio
async def test_proven_backend_domain_denials_skip_scoring_but_unknown_and_invalid_requests_do_not(native_pilot, monkeypatch):
    tracker = security.InMemorySecurityBackend()
    reputation = Mock(return_value=None)
    monkeypatch.setattr(security, "_tracker", tracker)
    monkeypatch.setattr(security, "reputation_manager", SimpleNamespace(
        check_reputation_ban=lambda _ip: None, record_request=reputation))
    monkeypatch.setattr(security, "nginx_blocklist_manager", SimpleNamespace(maybe_reload=AsyncMock()))
    monkeypatch.setattr(security.SecurityConfig, "get_admin_ips", classmethod(lambda cls: set()))
    monkeypatch.setattr(security.SecurityConfig, "NORMAL_THRESHOLD", (2, 3))
    for module in (api, embed_api):
        monkeypatch.setattr(module, "get_embed_store", lambda: native_pilot.store)
    monkeypatch.setattr(api, "account_service", lambda: ApplicationAccountService(native_pilot.store))
    app = FastAPI()
    app.include_router(api.router)
    app.add_middleware(security.SecurityMiddleware)
    good = "Basic " + base64.b64encode(("first:secret" + "a" * 40).encode()).decode()
    bad = "Basic " + base64.b64encode(("first:wrong" + "a" * 40).encode()).decode()
    path = "/api/applications/v1/accounts/status"
    body = {"external_user_id": "missing-domain-account"}
    async with AsyncClient(transport=ASGITransport(app, client=("127.0.0.1", 3000)), base_url="http://identity.example") as client:
        for _ in range(3):
            response = await client.post(path, json=body, headers={"Authorization": good})
            assert response.status_code == 404 and response.json()["error"] == "not_found"
        assert await tracker.get_404_count("127.0.0.1", 3) == 0
        assert not await tracker.is_blocked("127.0.0.1")
        reputation.assert_not_called()

        response = await client.post(path, json=body, headers={"Authorization": bad})
        assert response.status_code == 401
        assert reputation.call_args.args[1] == 401
        # Valid credentials alone, or a forged header, cannot authenticate an
        # unmatched route because the application dependency never runs there.
        for _ in range(2):
            response = await client.post("/api/applications/v1/unknown-resource", json=body,
                headers={"Authorization": good, "X-Aurvek-Application-Backend-Authenticated": "true"})
            assert response.status_code == 404
        assert await tracker.get_404_count("127.0.0.1", 3) == 2
        assert reputation.call_count == 3 and await tracker.is_blocked("127.0.0.1")
        response = await client.post(path, json=body, headers={"Authorization": good})
        assert response.status_code == 403

        await tracker.unblock_ip("127.0.0.1")
        response = await client.get("/.git/config", headers={"Authorization": good})
        assert response.status_code == 403
        assert reputation.call_args.kwargs["is_pattern_hit"] is True

    # Correct credentials with disallowed remote ingress remain anonymous too.
    reputation.reset_mock()
    async with AsyncClient(transport=ASGITransport(app, client=("198.51.100.25", 3000)), base_url="http://identity.example") as client:
        response = await client.post(path, json=body, headers={"Authorization": good})
        assert response.status_code == 401
        assert reputation.call_args.args[1] == 401
