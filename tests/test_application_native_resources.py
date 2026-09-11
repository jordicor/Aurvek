"""Native resource paths preserve application permission and never strip scope."""
import pytest
import pytest_asyncio
from fastapi import HTTPException

from chat.routes import branching, conversations, pages
from chat.schemas import BranchConversationRequest
from integrations.embed import identity
from tests.test_application_frames import application_frames
from tests.test_embed_native_creation import native_pilot


@pytest_asyncio.fixture
async def native_resources(application_frames, monkeypatch):
    from auth import get_user_by_id
    pilot, service, principal, first, coach, app = application_frames
    for module in (branching, conversations, pages):
        monkeypatch.setattr(module, "get_db_connection", pilot.connect)
    monkeypatch.setattr(identity, "get_embed_store", lambda: pilot.store)
    return pilot, service, principal, coach, await get_user_by_id(principal.user_id)


@pytest.mark.asyncio
async def test_native_branch_cannot_copy_scoped_history_or_start_media_work(native_resources, monkeypatch):
    pilot, service, principal, coach, user = native_resources

    async def no_branch_preparation(*args, **kwargs):
        pytest.fail("Application branch reached copy/write preparation")

    monkeypatch.setattr(branching, "ensure_conversation_privacy_schema", no_branch_preparation)
    async with pilot.connect() as connection:
        before = (await (await connection.execute("SELECT COUNT(*) FROM CONVERSATIONS")).fetchone())[0]
    with pytest.raises(HTTPException) as failure:
        await branching.branch_conversation(coach["conversation_id"], BranchConversationRequest(message_id=1), user)
    assert failure.value.status_code == 403
    assert failure.value.detail == "application_branch_unavailable"
    async with pilot.connect() as connection:
        after = (await (await connection.execute("SELECT COUNT(*) FROM CONVERSATIONS")).fetchone())[0]
    assert after == before


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [pages.get_conversation_details, conversations.get_last_message_id,
                                    conversations.conversation_status, conversations.get_web_search_status])
async def test_native_metadata_rechecks_context_access(native_resources, handler):
    pilot, service, principal, coach, user = native_resources
    async with pilot.connect() as connection:
        await connection.execute("UPDATE APPLICATION_PARTICIPANTS SET active=0 WHERE context_id=?", (coach["context_id"],))
        await connection.commit()
    with pytest.raises(HTTPException) as failure:
        await handler(coach["conversation_id"], current_user=user)
    assert failure.value.status_code == 404


@pytest.mark.asyncio
async def test_native_model_change_obeys_effective_capability(native_resources):
    pilot, service, principal, coach, user = native_resources

    class Request:
        async def json(self):
            return {"llm_id": 7}

    with pytest.raises(HTTPException) as failure:
        await pages.update_conversation_model(coach["conversation_id"], Request(), user)
    assert failure.value.status_code == 403
    async with pilot.connect() as connection:
        row = await (await connection.execute("SELECT llm_id FROM CONVERSATIONS WHERE id=?", (coach["conversation_id"],))).fetchone()
    assert row[0] == 8
