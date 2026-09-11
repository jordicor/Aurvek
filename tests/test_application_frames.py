"""Application destination authority through the real delegated frame transport."""
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from integrations.applications import api
from integrations.applications.models import AssistantConfig, OpenConversationRequest
from integrations.applications.service import ApplicationService
from integrations.embed import api as embed_api, pages
from integrations.embed.middleware import EmbedHostMiddleware
from integrations.embed.models import EmbedError, InterviewBrief
from tests.test_embed_native_creation import native_pilot, _native_login


@pytest_asyncio.fixture
async def application_frames(native_pilot, monkeypatch):
    from marketplace.middleware import custom_domains
    from marketplace.services.entitlements import grant_prompt_entitlement

    service = ApplicationService(native_pilot.store)
    await service.register_assistants("first", [
        AssistantConfig(assistant_id="default", display_name="Reception", prompt_id=10),
        AssistantConfig(assistant_id="coach", display_name="Coach", prompt_id=99),
    ], "default")
    async with native_pilot.connect() as connection:
        await grant_prompt_entitlement(connection, user_id=1, prompt_id=99, source="test_application")
        await connection.commit()
    principal = await _native_login(native_pilot)
    await service.grant_context("first", principal.subject, "practice")
    first = await service.open_conversation(principal, OpenConversationRequest(
        operation_id="frame-open-first", context_ref="practice", assistant_id="default"))
    coach = await service.open_conversation(principal, OpenConversationRequest(
        operation_id="frame-open-coach", context_ref="practice", assistant_id="coach"))
    for module in (api, embed_api, pages):
        monkeypatch.setattr(module, "get_embed_store", lambda: native_pilot.store)
    monkeypatch.setattr(custom_domains, "_primary_domains", {"identity.example"})
    application = FastAPI()
    application.include_router(api.router)
    application.include_router(pages.router)

    @application.get("/api/conversations/{conversation_id}/details")
    async def details(request: Request, conversation_id: int):
        resolved = request.state.embed_principal
        return {"conversation_id": resolved.conversation_id, "prompt_id": resolved.prompt_id,
                "assistant_id": resolved.application.assistant_id}

    application.add_middleware(EmbedHostMiddleware, store=native_pilot.store)
    return native_pilot, service, principal, first, coach, application


async def _ticket(pilot, principal, conversation_id, frame):
    return await pilot.store.issue_bootstrap(
        principal, None, conversation_id, "https://first.example", "https://chat.first.example",
        "es", frame, binding_kind="application")


@pytest.mark.asyncio
async def test_two_assistants_keep_independent_frames_with_rotated_cookie(application_frames):
    pilot, service, principal, first, coach, application = application_frames
    async with AsyncClient(transport=ASGITransport(application), base_url="https://chat.first.example") as client:
        for conversation, frame in [(first, "frame_reception_0001"), (coach, "frame_coach_00000001")]:
            ticket = await _ticket(pilot, principal, conversation["conversation_id"], frame)
            response = await client.post("/embed/bootstrap", data={"ticket": ticket["ticket"]},
                                         headers={"Origin": "https://first.example"})
            assert response.status_code == 303
        # The newest cookie authenticates the same delegated identity; each frame
        # still resolves and authorizes its own durable destination.
        for conversation, frame, prompt in [(first, "frame_reception_0001", 10), (coach, "frame_coach_00000001", 99)]:
            response = await client.get(f"/api/conversations/{conversation['conversation_id']}/details",
                                        headers={"X-Aurvek-Embed-Frame": frame})
            assert response.status_code == 200
            assert response.json()["prompt_id"] == prompt
        mismatch = await client.get(f"/api/conversations/{coach['conversation_id']}/details",
                                    headers={"X-Aurvek-Embed-Frame": "frame_reception_0001"})
        assert mismatch.status_code == 404
        async with pilot.connect() as connection:
            await connection.execute("UPDATE APPLICATION_PARTICIPANTS SET active=0 WHERE context_id=?",
                                     (coach["context_id"],))
            await connection.commit()
        denied = await client.get(f"/api/conversations/{coach['conversation_id']}/details",
                                  headers={"X-Aurvek-Embed-Frame": "frame_coach_00000001"})
        assert denied.status_code == 404


@pytest.mark.asyncio
async def test_destination_uses_its_prompt_even_when_legacy_entry_access_is_removed(application_frames):
    pilot, service, principal, first, coach, _ = application_frames
    async with pilot.connect() as connection:
        await connection.execute("UPDATE ENTITLEMENTS SET status='revoked' WHERE user_id=1 AND asset_type='prompt' AND asset_id=10")
        await connection.commit()
    # The legacy operation still checks its own prompt, while the other permitted
    # assistant can bootstrap with its own identity+destination authorization.
    with pytest.raises(EmbedError):
        await pilot.store.ensure_interview(principal, "legacy", "legacy-operation-0001", InterviewBrief(revision=1, language="es"))
    ticket = await _ticket(pilot, principal, coach["conversation_id"], "frame_coach_00000001")
    cookie, resolved = await pilot.store.consume_bootstrap(ticket["ticket"], "chat.first.example", "https://first.example")
    assert resolved.prompt_id == 99 and resolved.application.assistant_id == "coach"
    assert (await pilot.store.revalidate_principal(resolved)).prompt_id == 99
    async with pilot.connect() as connection:
        await connection.execute("UPDATE APPLICATION_CONTEXTS SET active=0 WHERE context_id=?", (coach["context_id"],))
        await connection.commit()
    with pytest.raises(EmbedError):
        await pilot.store.revalidate_principal(resolved)


@pytest.mark.asyncio
async def test_application_api_preserves_backend_transport_and_redacts_invalid_authority(application_frames):
    import base64
    pilot, service, principal, first, coach, application = application_frames
    headers = {"Authorization": "Basic " + base64.b64encode(("first:" + "secret" + "a" * 40).encode()).decode()}
    async with AsyncClient(transport=ASGITransport(application, client=("127.0.0.1", 9000)),
                           base_url="https://identity.example") as client:
        invalid = await client.post("/api/applications/v1/open-conversation", headers=headers, json={
            "delegated_credential": "synthetic-secret-value",
            "conversation": {"operation_id": "new-open-0001", "payer_user_id": 123},
        })
        assert invalid.status_code == 422
        assert invalid.json() == {"contract_version": "aurvek_applications.v1", "error": "invalid_request"}
        assert "synthetic-secret-value" not in invalid.text
    async with AsyncClient(transport=ASGITransport(application, client=("203.0.113.9", 9000)),
                           base_url="https://identity.example") as remote:
        denied = await remote.post("/api/applications/v1/inspect-access", headers=headers,
                                    json={"delegated_credential": "synthetic-secret-value"})
        assert denied.status_code == 401  # Remote backend support belongs to F2.


@pytest.mark.asyncio
async def test_authenticated_api_opens_resumes_and_bootstraps_native_destination(application_frames):
    import base64
    import hashlib
    import time
    from auth import get_user_by_id

    pilot, service, principal, first, coach, application = application_frames
    user = await get_user_by_id(principal.user_id)
    user.session_expires_at = int(time.time()) + 3600
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    callback = pilot.configs["first"].redirect_uris[0]
    tx = await pilot.store.authorize_begin("first", callback, "s" * 32, challenge)
    code = await pilot.store.authorize_complete(tx, user, "synthetic-native-proof")
    headers = {"Authorization": "Basic " + base64.b64encode(("first:" + "secret" + "a" * 40).encode()).decode()}
    async with AsyncClient(transport=ASGITransport(application, client=("127.0.0.1", 9000)),
                           base_url="https://identity.example", headers=headers) as client:
        exchanged = await client.post("/api/applications/v1/exchange-code", json={
            "code": code["code"], "code_verifier": verifier, "redirect_uri": callback})
        assert exchanged.status_code == 200
        credential = exchanged.json()["delegated_credential"]
        payload = {"delegated_credential": credential, "conversation": {
            "operation_id": "api-open-000001", "context_ref": "practice", "assistant_id": "coach"}}
        opened = await client.post("/api/applications/v1/open-conversation", json=payload)
        assert opened.status_code == 200
        assert opened.json()["conversation_id"] == coach["conversation_id"]
        repeated = await client.post("/api/applications/v1/open-conversation", json=payload)
        assert repeated.status_code == 200 and repeated.json()["replayed"]
        bootstrapped = await client.post("/api/applications/v1/issue-embed-bootstrap", json={
            "delegated_credential": credential, "conversation_id": coach["conversation_id"],
            "parent_origin": "https://first.example", "embed_origin": "https://chat.first.example",
            "frame_instance_id": "api_frame_0000000001", "ui_language": "es"})
        assert bootstrapped.status_code == 200
        _, resolved = await pilot.store.consume_bootstrap(bootstrapped.json()["ticket"],
                                                         "chat.first.example", "https://first.example")
        assert resolved.application.assistant_id == "coach" and resolved.prompt_id == 99


@pytest.mark.asyncio
async def test_v1_frames_apply_adopted_context_revocation(application_frames):
    pilot, service, principal, _, _, _ = application_frames
    legacy = await pilot.store.ensure_interview(principal, "legacy-guard", "legacy-operation-0001",
                                                 InterviewBrief(revision=1, language="es"))
    ticket = await pilot.store.issue_bootstrap(principal, "legacy-guard", legacy["conversation_id"],
        "https://first.example", "https://chat.first.example", "es", "legacy_frame_0000001")
    cookie, resolved = await pilot.store.consume_bootstrap(ticket["ticket"], "chat.first.example", "https://first.example")
    assert resolved.binding_kind == "embed" and resolved.application is not None
    async with pilot.connect() as connection:
        await connection.execute("UPDATE APPLICATION_CONTEXTS SET active=0 WHERE context_id=?", (resolved.application.context_id,))
        await connection.commit()
    with pytest.raises(EmbedError):
        await pilot.store.resolve_frame(cookie, "chat.first.example", "first", "legacy_frame_0000001")
    with pytest.raises(EmbedError):
        await pilot.store.revalidate_principal(resolved)
