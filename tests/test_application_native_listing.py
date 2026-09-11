"""Native page metadata obeys live application scope, including old bindings."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from integrations.applications.listing import blocked_application_conversation_ids
from integrations.applications.models import OpenConversationRequest
from integrations.applications.service import ApplicationService
from tests.test_embed_native_creation import native_pilot, _native_login


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["member", "context"])
async def test_native_page_excludes_revoked_metadata_and_requested_selection(native_pilot, monkeypatch, revocation):
    from chat.services import page_context
    from fastapi import HTTPException
    import llm_catalog

    service = ApplicationService(native_pilot.store)
    identity = await _native_login(native_pilot)
    app_chat = await service.open_conversation(identity, OpenConversationRequest(operation_id="listing-app-open"))
    app_id = app_chat["conversation_id"]
    async with native_pilot.connect() as connection:
        await connection.execute("ALTER TABLE CONVERSATIONS ADD COLUMN is_incognito INTEGER DEFAULT 0")
        await connection.execute("ALTER TABLE CONVERSATIONS ADD COLUMN hidden_from_history INTEGER DEFAULT 0")
        await connection.execute("INSERT INTO CHAT_FOLDERS(id,user_id,name) VALUES(1,1,'My folder')")
        await connection.execute("UPDATE CONVERSATIONS SET chat_name='Application private title',last_activity='2099-01-01',folder_id=1 WHERE id=?", (app_id,))
        await connection.execute("INSERT INTO CONVERSATIONS(user_id,role_id,llm_id,chat_name,last_activity) VALUES(1,99,8,'Native title','2020-01-01')")
        native_id = (await (await connection.execute("SELECT MAX(id) FROM CONVERSATIONS")).fetchone())[0]
        await connection.commit()

    async def blocked(connection, user_id, *, viewer_user_id=None):
        return await blocked_application_conversation_ids(connection, user_id, viewer_user_id=viewer_user_id, service=service)

    monkeypatch.setattr(page_context, "blocked_application_conversation_ids", blocked)
    for name, result in {
        "ensure_conversation_privacy_schema": None,
        "purge_stale_incognito_conversations_for_user": None,
        "get_subscription_auth_enabled": False,
        "get_user_accessible_prompts": [],
        "get_user_api_key_mode": "platform",
        "user_requires_own_keys": False,
        "user_has_valid_api_keys": False,
        "get_conversation_channel_summaries": {},
        "get_conversation_binding_summaries": {},
    }.items():
        monkeypatch.setattr(page_context, name, AsyncMock(return_value=result))
    monkeypatch.setattr(llm_catalog, "get_selector_llms", AsyncMock(return_value=[]))
    monkeypatch.setattr(page_context, "get_signed_bot_avatar_urls", lambda *_: {})
    monkeypatch.setattr(page_context, "_get_marketplace_template_flags", lambda: {})
    monkeypatch.setattr(page_context, "ensure_csrf_token", lambda _: "test-csrf")
    monkeypatch.setattr(page_context, "templates", SimpleNamespace(TemplateResponse=lambda _, context: context))
    user = await native_pilot.store._load_user(1)

    async with native_pilot.connect() as connection:
        # A permitted app conversation still selects its normal page metadata.
        permitted = await page_context.handle_get_request(SimpleNamespace(query_params={"conversation_id": str(app_id)}), None, user, connection)
        assert permitted["conversation_id"] == app_id
        assert permitted["conversation_count"] == 2
        assert permitted["chat_folders"][0]["conversation_count"] == 1
        assert {row["id"] for row in permitted["initial_conversations"]} == {app_id, native_id}
        assert await blocked_application_conversation_ids(connection, 1, viewer_user_id=2, service=service) == [app_id]
        if revocation == "member":
            await connection.execute("UPDATE EMBED_MEMBERSHIPS SET active=0 WHERE app_id=? AND subject=?", (identity.app_id, identity.subject))
        else:
            await connection.execute("UPDATE APPLICATION_CONTEXTS SET active=0 WHERE context_id=?", (app_chat["context_id"],))
        await connection.commit()
        denied = await page_context.handle_get_request(SimpleNamespace(query_params={}), None, user, connection)
        assert denied["conversation_id"] == native_id
        assert denied["current_model_type"] == 8
        assert denied["conversation_count"] == 1
        assert denied["chat_folders"][0]["conversation_count"] == 0
        assert [row["chat_name"] for row in denied["initial_conversations"]] == ["Native title"]
        assert "Application private title" not in str(denied)
        await connection.execute("UPDATE CONVERSATIONS SET folder_id=NULL WHERE id=?", (app_id,))
        await connection.commit()
        normal = await page_context.handle_get_request(SimpleNamespace(query_params={}), None, user, connection)
        assert [row["id"] for row in normal["initial_conversations"]] == [native_id]
        monkeypatch.setattr(page_context, "get_conversation_channel_summaries", AsyncMock(return_value={
            app_id: {"external_channels": ["telegram"], "phone_binding": None},
        }))
        pinned = await page_context.handle_get_request(SimpleNamespace(query_params={}), None, user, connection)
        assert [row["id"] for row in pinned["initial_conversations"]] == [native_id]
        with pytest.raises(HTTPException) as error:
            await page_context.handle_get_request(SimpleNamespace(query_params={"conversation_id": str(app_id)}), None, user, connection)
        assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_ordinary_native_schema_needs_no_application_identity(tmp_path):
    async with aiosqlite.connect(tmp_path / "native.sqlite") as connection:
        await connection.execute("CREATE TABLE CONVERSATIONS(id INTEGER PRIMARY KEY,user_id INTEGER)")
        await connection.execute("INSERT INTO CONVERSATIONS VALUES(1,1)")
        service = SimpleNamespace(authorize_runtime_conversation=AsyncMock(side_effect=AssertionError("Native chat must not resolve app identity")))
        assert await blocked_application_conversation_ids(connection, 1, service=service) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["embed_interviews", "application_conversations"])
async def test_partial_or_unmigrated_binding_fails_closed(tmp_path, table):
    async with aiosqlite.connect(tmp_path / "partial.sqlite") as connection:
        connection.row_factory = aiosqlite.Row
        await connection.execute("CREATE TABLE CONVERSATIONS(id INTEGER PRIMARY KEY,user_id INTEGER)")
        await connection.execute("INSERT INTO CONVERSATIONS VALUES(1,1),(2,1),(3,2)")
        await connection.execute(f"CREATE TABLE {table}(conversation_id INTEGER)")
        await connection.execute(f"INSERT INTO {table} VALUES(1),(3)")
        from integrations.embed.store import EmbedStore
        service = ApplicationService(EmbedStore())
        # Lowercase registry names are valid SQLite identifiers, never a bypass.
        assert await blocked_application_conversation_ids(connection, 1, service=service) == [1]
