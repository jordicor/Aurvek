from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from integrations.messaging_voice_notes import routes


@pytest.fixture
def voice_notes_client(monkeypatch: pytest.MonkeyPatch):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_current_user] = lambda: SimpleNamespace(id=17)
    monkeypatch.setattr(routes, "validate_mutation_request", lambda _request: None)

    with TestClient(app) as client:
        yield client


def test_owner_scoped_reads_hide_foreign_message_and_revision(
    voice_notes_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_state = AsyncMock(return_value=None)
    get_revision = AsyncMock(return_value=None)
    monkeypatch.setattr(routes, "get_voice_note_state", get_state)
    monkeypatch.setattr(routes, "get_retranscription_revision", get_revision)

    message_response = voice_notes_client.get("/api/messaging-voice-notes/9001")
    revision_response = voice_notes_client.get(
        "/api/messaging-voice-notes/revisions/9002"
    )

    assert message_response.status_code == 404
    assert message_response.json() == {"detail": "Voice note not found"}
    assert revision_response.status_code == 404
    assert revision_response.json() == {"detail": "Revision not found"}
    get_state.assert_awaited_once_with(9001, owner_user_id=17)
    get_revision.assert_awaited_once_with(9002, owner_user_id=17)


def test_voice_note_state_redispatches_a_queued_revision(
    voice_notes_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_state = AsyncMock(
        return_value={
            "message_id": 55,
            "latest_revision_id": 321,
            "latest_revision_status": "queued",
        }
    )
    dispatched: list[int] = []
    monkeypatch.setattr(routes, "get_voice_note_state", get_state)
    monkeypatch.setattr(
        routes,
        "_dispatch_retranscription_revision",
        lambda revision_id: dispatched.append(revision_id),
    )

    response = voice_notes_client.get("/api/messaging-voice-notes/55")

    assert response.status_code == 200
    assert dispatched == [321]


def test_owner_scoped_retranscription_hides_foreign_message(
    voice_notes_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_revision = AsyncMock(side_effect=LookupError("Voice note not found"))
    monkeypatch.setattr(routes, "create_retranscription_revision", create_revision)

    response = voice_notes_client.post(
        "/api/messaging-voice-notes/9001/retranscribe",
        json={"stt_engine": "configured"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Voice note not found"}
    create_revision.assert_awaited_once_with(
        message_id=9001,
        owner_user_id=17,
        stt_engine="configured",
        comparison_llm_id=None,
    )


def test_owner_scoped_decision_hides_foreign_revision(
    voice_notes_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decide_revision = AsyncMock(side_effect=LookupError("Revision not found"))
    monkeypatch.setattr(routes, "decide_retranscription_revision", decide_revision)

    response = voice_notes_client.post(
        "/api/messaging-voice-notes/revisions/9002/decision",
        json={"decision": "accept"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Revision not found"}
    decide_revision.assert_awaited_once_with(
        revision_id=9002,
        owner_user_id=17,
        decision="accept",
    )


def test_active_retranscription_conflict_returns_existing_revision_id(
    voice_notes_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_revision = AsyncMock(
        side_effect=routes.ActiveRetranscriptionError(revision_id=321)
    )
    monkeypatch.setattr(routes, "create_retranscription_revision", create_revision)
    dispatched: list[int] = []
    monkeypatch.setattr(
        routes,
        "_dispatch_retranscription_revision",
        lambda revision_id: dispatched.append(revision_id),
    )

    response = voice_notes_client.post(
        "/api/messaging-voice-notes/55/retranscribe",
        json={"stt_engine": "deepgram", "comparison_llm_id": 8},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "A retranscription is already running",
        "revision_id": 321,
    }
    create_revision.assert_awaited_once_with(
        message_id=55,
        owner_user_id=17,
        stt_engine="deepgram",
        comparison_llm_id=8,
    )
    assert dispatched == [321]
