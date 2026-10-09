import json

import pytest

from ai_runtime import provider_health


@pytest.fixture
def claude_status(monkeypatch):
    payload = {
        "status": {"indicator": "minor", "description": "Partially Degraded Service"},
        "components": [
            {"id": "api", "name": "Claude API (api.anthropic.com)", "status": "operational"},
            {"id": "cowork", "name": "Claude Cowork", "status": "degraded_performance"},
        ],
        "incidents": [{"name": "Cowork on Windows", "components": [{"id": "cowork"}]}],
    }

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def read(self):
            return json.dumps(payload).encode()

        def raise_for_status(self):
            pass

    class Session(Response):
        def get(self, _url):
            return Response()

    monkeypatch.setattr(provider_health.aiohttp, "ClientSession", lambda **_kwargs: Session())
    provider_health.reset_provider_health_state()
    yield payload
    provider_health.reset_provider_health_state()


@pytest.mark.asyncio
async def test_claude_api_ignores_unrelated_cowork_incident(claude_status):
    result = await provider_health._fetch_official_status("anthropic")
    assert result["indicator"] == "none"
    assert result["incident"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status, indicator", [
    ("degraded_performance", "minor"),
    ("partial_outage", "major"),
    ("major_outage", "critical"),
    ("under_maintenance", "minor"),
])
async def test_claude_api_reports_its_own_service_status(claude_status, status, indicator):
    claude_status["components"][0]["status"] = status
    claude_status["incidents"].append({"name": "API errors", "components": [{"id": "api"}]})
    result = await provider_health._fetch_official_status("anthropic")
    assert result["indicator"] == indicator
    assert result["incident"] == "API errors"


@pytest.mark.asyncio
async def test_claude_recovery_clears_warning_while_cowork_remains_degraded(claude_status):
    claude_status["components"][0]["status"] = "partial_outage"
    await provider_health._run_official_check("anthropic")
    assert provider_health.get_provider_health("anthropic")["surface"] is True

    claude_status["components"][0]["status"] = "operational"
    await provider_health._run_official_check("anthropic")
    health = provider_health.get_provider_health("anthropic")
    assert health["status"] == "operational"
    assert health["surface"] is False


@pytest.mark.asyncio
async def test_missing_api_component_is_not_treated_as_healthy(claude_status):
    claude_status["components"].pop(0)
    with pytest.raises(ValueError, match="Claude API"):
        await provider_health._fetch_official_status("anthropic")
