import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest


class JsonRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


@pytest.fixture()
def routes_module(monkeypatch):
    task = SimpleNamespace(send=Mock())
    tasks_stub = ModuleType("tasks")
    tasks_stub.download_elevenlabs_audio_task = task
    monkeypatch.setitem(sys.modules, "tasks", tasks_stub)

    module_path = (
        Path(__file__).resolve().parents[1]
        / "integrations"
        / "elevenlabs"
        / "routes.py"
    )
    module_name = "_test_elevenlabs_routes"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    try:
        yield module, task
    finally:
        sys.modules.pop(module_name, None)


def _configure_access(monkeypatch, routes, conversation):
    monkeypatch.setattr(routes.elevenlabs_service, "is_configured", lambda: True)
    monkeypatch.setattr(routes, "_is_admin_user", AsyncMock(return_value=False))
    monkeypatch.setattr(
        routes.elevenlabs_service,
        "validate_conversation_access",
        AsyncMock(return_value=conversation),
    )


@pytest.mark.asyncio
async def test_incognito_gate_runs_before_config_or_session_creation(
    routes_module,
    monkeypatch,
):
    routes, _task = routes_module
    conversation = {
        "id": 100,
        "user_id": 1,
        "locked": 0,
        "is_incognito": 1,
    }
    _configure_access(monkeypatch, routes, conversation)
    get_configuration = AsyncMock()
    register_session = AsyncMock()
    monkeypatch.setattr(
        routes.elevenlabs_service,
        "get_configuration",
        get_configuration,
    )
    monkeypatch.setattr(
        routes.elevenlabs_service,
        "register_session",
        register_session,
    )
    user = SimpleNamespace(id=1)

    config_response = await routes.get_elevenlabs_config(100, user)
    session_response = await routes.start_elevenlabs_session(
        100,
        JsonRequest({"session_id": "provider-session"}),
        user,
    )

    assert config_response.status_code == 403
    assert session_response.status_code == 403
    assert "incognito" in json.loads(config_response.body)["error"]
    get_configuration.assert_not_awaited()
    register_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_availability_returns_canonical_visible_gate_without_starting_session(
    routes_module,
    monkeypatch,
):
    routes, _task = routes_module
    availability = {
        "available": False,
        "error_code": "elevenlabs_webrtc_voice_incompatible",
        "reason": "ElevenLabs cannot reproduce the canonical openai voice exactly.",
        "provider": "openai",
        "voice": "alloy",
    }
    get_availability = AsyncMock(return_value=availability)
    monkeypatch.setattr(routes, "_is_admin_user", AsyncMock(return_value=False))
    monkeypatch.setattr(
        routes.elevenlabs_service,
        "get_webrtc_availability",
        get_availability,
    )
    get_configuration = AsyncMock()
    register_session = AsyncMock()
    monkeypatch.setattr(routes.elevenlabs_service, "get_configuration", get_configuration)
    monkeypatch.setattr(routes.elevenlabs_service, "register_session", register_session)

    response = await routes.get_elevenlabs_availability(100, SimpleNamespace(id=1))
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload == availability
    get_availability.assert_awaited_once_with(100, 1, False)
    get_configuration.assert_not_awaited()
    register_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_transcript_response_is_retryable_and_does_not_enqueue_audio(
    routes_module,
    monkeypatch,
):
    routes, task = routes_module
    conversation = {
        "id": 100,
        "user_id": 1,
        "locked": 0,
        "is_incognito": 0,
        "role_id": 10,
    }
    _configure_access(monkeypatch, routes, conversation)
    monkeypatch.setattr(
        routes.elevenlabs_service,
        "get_bound_session",
        AsyncMock(
            return_value={
                "session_id": "provider-session",
                "transcript_saved_at": None,
            }
        ),
    )
    monkeypatch.setattr(
        routes.elevenlabs_service,
        "check_conversation_status",
        AsyncMock(return_value="done"),
    )
    monkeypatch.setattr(
        routes.elevenlabs_service,
        "fetch_full_transcript",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        routes.elevenlabs_service,
        "save_transcript_to_db",
        AsyncMock(return_value=(0, None, None, False)),
    )

    response = await routes.complete_elevenlabs_session(
        100,
        JsonRequest({"session_id": "provider-session"}),
        SimpleNamespace(id=1),
    )

    assert response.status_code == 425
    assert "not ready" in json.loads(response.body)["error"]
    task.send.assert_not_called()


@pytest.mark.asyncio
async def test_transient_transcript_fetch_error_does_not_fail_binding(
    routes_module,
    monkeypatch,
):
    routes, _task = routes_module
    conversation = {
        "id": 100,
        "user_id": 1,
        "locked": 0,
        "is_incognito": 0,
    }
    _configure_access(monkeypatch, routes, conversation)
    monkeypatch.setattr(
        routes.elevenlabs_service,
        "get_bound_session",
        AsyncMock(
            return_value={
                "session_id": "provider-session",
                "transcript_saved_at": None,
            }
        ),
    )
    monkeypatch.setattr(
        routes.elevenlabs_service,
        "check_conversation_status",
        AsyncMock(return_value="done"),
    )
    request = httpx.Request("GET", "https://api.elevenlabs.test/conversation")
    monkeypatch.setattr(
        routes.elevenlabs_service,
        "fetch_full_transcript",
        AsyncMock(side_effect=httpx.ConnectError("offline", request=request)),
    )
    mark_failed = AsyncMock()
    monkeypatch.setattr(routes, "_mark_session_failed", mark_failed)

    response = await routes.complete_elevenlabs_session(
        100,
        JsonRequest({"session_id": "provider-session"}),
        SimpleNamespace(id=1),
    )

    assert response.status_code == 502
    mark_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_failed_status_is_definitive_not_retryable_502(
    routes_module,
    monkeypatch,
):
    routes, _task = routes_module
    conversation = {
        "id": 100,
        "user_id": 1,
        "locked": 0,
        "is_incognito": 0,
    }
    _configure_access(monkeypatch, routes, conversation)
    monkeypatch.setattr(
        routes.elevenlabs_service,
        "get_bound_session",
        AsyncMock(
            return_value={
                "session_id": "provider-session",
                "transcript_saved_at": None,
            }
        ),
    )
    monkeypatch.setattr(
        routes.elevenlabs_service,
        "check_conversation_status",
        AsyncMock(return_value="failed"),
    )
    mark_failed = AsyncMock()
    monkeypatch.setattr(routes, "_mark_session_failed", mark_failed)

    response = await routes.complete_elevenlabs_session(
        100,
        JsonRequest({"session_id": "provider-session"}),
        SimpleNamespace(id=1),
    )

    assert response.status_code == 409
    assert json.loads(response.body)["status"] == "failed"
    mark_failed.assert_awaited_once_with(100, "provider-session", 1)
