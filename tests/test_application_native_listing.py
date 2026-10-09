"""Native page metadata obeys live application scope, including old bindings."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio

from integrations.applications.listing import blocked_application_conversation_ids
from integrations.applications.models import OpenConversationRequest
from integrations.applications.service import ApplicationService
from tests.test_embed_native_creation import native_pilot, _native_login


@pytest_asyncio.fixture
async def native_listing(native_pilot, monkeypatch):
    from chat.routes import search
    from chat.services.privacy import ensure_conversation_privacy_schema
    from integrations.embed import identity

    service = ApplicationService(native_pilot.store)
    first = await _native_login(native_pilot)
    second = await _native_login(native_pilot, "second")
    revoked = await service.open_conversation(first, OpenConversationRequest(operation_id="listing-revoked"))
    allowed = await service.open_conversation(second, OpenConversationRequest(operation_id="listing-allowed"))
    monkeypatch.setattr(identity, "get_embed_store", lambda: native_pilot.store)
    monkeypatch.setattr(search, "get_db_connection", native_pilot.connect)

    async with native_pilot.connect() as connection:
        await ensure_conversation_privacy_schema(connection)
        await connection.execute("INSERT INTO CHAT_FOLDERS(id,user_id,name) VALUES(1,1,'Revoked only'),(2,1,'Mixed'),(3,1,'Empty')")
        await connection.execute("UPDATE CONVERSATIONS SET folder_id=1 WHERE id=?", (revoked["conversation_id"],))
        await connection.execute("UPDATE CONVERSATIONS SET folder_id=2 WHERE id=?", (allowed["conversation_id"],))
        native = await connection.execute(
            "INSERT INTO CONVERSATIONS(user_id,role_id,llm_id,chat_name,folder_id) VALUES(1,99,8,'Native chat',2)"
        )
        hidden = await connection.execute(
            "INSERT INTO CONVERSATIONS(user_id,role_id,llm_id,chat_name,hidden_from_history) VALUES(1,99,8,'Incognito',1)"
        )
        conversation_ids = [native.lastrowid, native.lastrowid, allowed["conversation_id"],
                            revoked["conversation_id"], revoked["conversation_id"], revoked["conversation_id"], hidden.lastrowid]
        for conversation_id in conversation_ids:
            await connection.execute(
                "INSERT INTO MESSAGES(conversation_id,user_id,message,type,is_bookmarked) VALUES(?,1,'searchable common phrase','user',1)",
                (conversation_id,),
            )
        await connection.execute("CREATE VIRTUAL TABLE MESSAGES_FTS USING fts5(search_text, tokenize='unicode61 remove_diacritics 2')")
        await connection.execute("INSERT INTO MESSAGES_FTS(rowid,search_text) SELECT id,message FROM MESSAGES")
        await connection.commit()

    return SimpleNamespace(pilot=native_pilot, first=first, revoked=revoked, allowed=allowed,
                           native_id=native.lastrowid, hidden_id=hidden.lastrowid,
                           user=await native_pilot.store._load_user(1))


@pytest.mark.asyncio
async def test_search_filters_revocation_before_pagination_and_conversation_scope(native_listing):
    import json
    from chat.routes.search import search_messages

    env = native_listing

    async def search(offset=0, conversation_id=None):
        response = await search_messages(None, env.user, q="searchable", limit=1,
                                         offset=offset, conversation_id=conversation_id)
        assert response.status_code == 200
        return json.loads(response.body)

    assert (await search(conversation_id=env.revoked["conversation_id"]))["items"]
    async with env.pilot.connect() as connection:
        await connection.execute("UPDATE EMBED_MEMBERSHIPS SET active=0 WHERE app_id=? AND subject=?",
                                 (env.first.app_id, env.first.subject))
        await connection.commit()

    pages = [await search(offset=offset) for offset in range(3)]
    assert [page["has_more"] for page in pages] == [True, True, False]
    assert [page["next_offset"] for page in pages] == [1, 2, 3]
    visible = [item["conversation_id"] for page in pages for item in page["items"]]
    assert visible == [env.allowed["conversation_id"], env.native_id, env.native_id]
    assert not (await search(offset=3))["items"]
    denied = await search(conversation_id=env.revoked["conversation_id"])
    assert denied["items"] == [] and denied["has_more"] is False
    assert (await search(conversation_id=env.allowed["conversation_id"]))["items"]
    assert (await search(conversation_id=env.native_id))["items"]
    assert (await search(conversation_id=env.hidden_id))["items"] == []


@pytest.mark.asyncio
async def test_bookmarks_folders_and_media_recheck_revoked_context(native_listing, monkeypatch, tmp_path):
    import hashlib
    import json
    from pathlib import Path
    from fastapi import HTTPException
    from chat.routes import bookmarks, folders, media
    from chat.services import message_rendering
    from file_storage import ensure_file_storage_schema
    from integrations.messaging_voice_notes import service as voice_notes

    env = native_listing
    for module in (bookmarks, folders, media, voice_notes):
        monkeypatch.setattr(module, "get_db_connection", env.pilot.connect)
    user_path = tmp_path / "data" / "users" / "aa" / "bb" / "pilot"
    monkeypatch.setattr(media, "get_user_directory", lambda _: str(user_path))
    monkeypatch.setattr(media, "get_or_generate_img_token", AsyncMock(return_value="fixture-token"))
    monkeypatch.setattr(media, "get_template_context", AsyncMock(return_value={}))
    monkeypatch.setattr(media, "templates", SimpleNamespace(TemplateResponse=lambda _, context: context))
    monkeypatch.setattr(message_rendering, "CLOUDFLARE_BASE_URL", "https://cdn.example/")
    ids = [env.native_id, env.allowed["conversation_id"], env.revoked["conversation_id"], env.hidden_id]
    exports = {}

    async with env.pilot.connect() as connection:
        await ensure_file_storage_schema(connection)
        for conversation_id in ids:
            row = await (await connection.execute("SELECT MIN(id) FROM MESSAGES WHERE conversation_id=?", (conversation_id,))).fetchone()
            message_id = row[0]
            await connection.execute(
                "INSERT INTO MESSAGES(conversation_id,user_id,message,type) VALUES(?,1,?,'user')",
                (conversation_id, json.dumps([{"type": "image_url", "image_url": {"url": f"https://media.example/{conversation_id}.png"}}])),
            )
            for kind, mime in [("image", "image/png"), ("pdf", "application/pdf")]:
                blob = await connection.execute(
                    "INSERT INTO FILE_BLOBS(sha256,size_bytes,kind,mime_detected,storage_key,page_count) VALUES(?,10,?,?,?,1)",
                    (hashlib.sha256(f"{conversation_id}-{kind}".encode()).hexdigest(), kind, mime, f"{conversation_id}/{kind}"),
                )
                await connection.execute(
                    """INSERT INTO FILE_ATTACHMENTS(public_id,blob_id,user_id,conversation_id,message_id,
                         attachment_type,original_filename,status) VALUES(?,?,1,?,?,?,?,'active')""",
                    (f"att_{conversation_id}_{kind}", blob.lastrowid, conversation_id, message_id, kind,
                     f"attachment-{conversation_id}.{kind}"),
                )
            for kind in ("pdf", "mp3"):
                directory = user_path / "files" / f"{conversation_id:07d}"[:3] / f"{conversation_id:07d}"[3:] / kind
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / f"export-{conversation_id}.{kind}"
                path.write_bytes(b"fixture export")
                exports[conversation_id, kind] = path
        await connection.commit()

    nested = exports[env.native_id, "pdf"].parent / "archive" / "older.pdf"
    nested.parent.mkdir()
    nested.write_bytes(b"older fixture export")

    async def snapshot():
        marked = json.loads((await bookmarks.get_bookmarked_messages(None, env.user)).body)
        folder_rows = json.loads((await folders.get_chat_folders(env.user)).body)["folders"]
        gallery = await media.media_gallery(None, env.user)
        assert "images" in gallery
        pdfs = json.loads((await media.get_pdfs(None, env.user)).body)["pdfs"]
        mp3s = json.loads((await media.get_mp3s(None, env.user)).body)["mp3s"]
        files = json.loads((await media.list_files(None, env.user)).body)
        return marked, folder_rows, gallery["images"], pdfs, mp3s, files

    before = await snapshot()
    assert {row["conversation_id"] for row in before[0]} == set(ids[:3])
    assert {row["id"]: row["conversation_count"] for row in before[1]} == {1: 1, 2: 2, 3: 0}
    assert len(before[2]) == 6 and len(before[3]) == 6 and len(before[4]) == 3

    allowed_message = next(row["id"] for row in before[0] if row["conversation_id"] == env.allowed["conversation_id"])
    request = SimpleNamespace(json=AsyncMock(return_value={"message_id": allowed_message, "action": "remove"}))
    assert (await bookmarks.bookmark_message(env.allowed["conversation_id"], request, env.user)).status_code == 200
    async with env.pilot.connect() as connection:
        assert (await (await connection.execute("SELECT is_bookmarked FROM MESSAGES WHERE id=?", (allowed_message,))).fetchone())[0] == 0
        await connection.execute("UPDATE MESSAGES SET is_bookmarked=1 WHERE id=?", (allowed_message,))
        await connection.execute("UPDATE APPLICATION_CONTEXTS SET active=0 WHERE context_id=?", (env.revoked["context_id"],))
        await connection.commit()

    marked, folder_rows, images, pdfs, mp3s, files = await snapshot()
    visible_ids = {env.native_id, env.allowed["conversation_id"]}
    assert {row["conversation_id"] for row in marked} == visible_ids
    assert {row["id"]: row["conversation_count"] for row in folder_rows} == {1: 0, 2: 2, 3: 0}
    assert {row.get("attachment_ref") for row in images if row.get("attachment_ref")} == {f"att_{cid}_image" for cid in visible_ids}
    assert {row["url"] for row in images if not row.get("attachment_ref")} == {f"https://media.example/{cid}.png" for cid in visible_ids}
    assert {row["name"] for row in pdfs} == {f"{prefix}-{cid}.pdf" for cid in visible_ids for prefix in ("attachment", "export")}
    assert {row["name"] for row in mp3s} == {f"export-{cid}.mp3" for cid in visible_ids}
    assert {Path(path).name for path in files["pdf_files"]} == {"older.pdf", *(f"export-{cid}.pdf" for cid in visible_ids)}
    assert {Path(path).name for path in files["mp3_files"]} == {f"export-{cid}.mp3" for cid in visible_ids}

    revoked_message = next(row["id"] for row in before[0] if row["conversation_id"] == env.revoked["conversation_id"])
    request.json = AsyncMock(return_value={"message_id": revoked_message, "action": "remove"})
    with pytest.raises(HTTPException) as denied:
        await bookmarks.bookmark_message(env.revoked["conversation_id"], request, env.user)
    assert denied.value.status_code == 404
    async with env.pilot.connect() as connection:
        assert (await (await connection.execute("SELECT is_bookmarked FROM MESSAGES WHERE id=?", (revoked_message,))).fetchone())[0] == 1

    # Existing personal export downloads keep their current access policy.
    for kind, download in [("pdf", media.download_pdf), ("mp3", media.download_mp3)]:
        response = await download(str(exports[env.revoked["conversation_id"], kind]), env.user)
        assert response.status_code == 200
        assert Path(response.path).is_file()


@pytest.mark.asyncio
async def test_search_literal_terms_and_quoted_phrases_use_real_fts5(native_listing):
    import json
    from chat.routes.search import search_messages

    env = native_listing
    cases = [
        ("hello-world?", "hello world", "hello separated world"),
        ("email@example.com", "email example com", "email other example com"),
        ('"visa interview"', "visa interview", "visa helpful interview"),
        ('co"operate', "co operate", "co separately operate"),
        ('"he said ""hello"""', 'he said "hello"', "he said distant hello"),
        ("AND OR NOT NEAR", "AND OR NOT NEAR", "AND OR NOT elsewhere"),
        ("nul\x00token", "nultoken", "nul token"),
        ('"unclosed', "unclosed", "unrelated"),
    ]
    expected = []
    async with env.pilot.connect() as connection:
        for query, target, distractor in cases:
            for message in (target, distractor):
                inserted = await connection.execute(
                    "INSERT INTO MESSAGES(conversation_id,user_id,message,type) VALUES(?,1,?,'user')",
                    (env.native_id, message),
                )
                await connection.execute("INSERT INTO MESSAGES_FTS(rowid,search_text) VALUES(?,?)", (inserted.lastrowid, message))
                if message == target:
                    expected.append((query, inserted.lastrowid))
        await connection.commit()

    for query, message_id in expected:
        response = await search_messages(None, env.user, q=query, limit=30, offset=0, conversation_id=env.native_id)
        assert response.status_code == 200
        assert [item["message_id"] for item in json.loads(response.body)["items"]] == [message_id], query
    for query in ('""', "\x00\x00\x00"):
        response = await search_messages(None, env.user, q=query, limit=30, offset=0, conversation_id=env.native_id)
        assert json.loads(response.body)["items"] == []


@pytest.mark.asyncio
async def test_search_database_failures_remain_internal_and_privacy_is_required(native_listing):
    import sqlite3
    from chat.routes.search import search_messages
    from chat.services.search import build_fts_query, execute_search

    env = native_listing
    async with env.pilot.connect() as connection:
        await connection.execute("DROP TABLE MESSAGES_FTS")
        await connection.commit()
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        await search_messages(None, env.user, q="searchable", limit=30, offset=0, conversation_id=None)

    async with env.pilot.connect() as connection:
        await connection.execute("CREATE VIRTUAL TABLE MESSAGES_FTS USING fts5(search_text, tokenize='unicode61 remove_diacritics 2')")
        await connection.execute("INSERT INTO MESSAGES_FTS(rowid,search_text) SELECT id,message FROM MESSAGES")
        await connection.execute("DROP INDEX idx_conversations_history_visible")
        await connection.execute("ALTER TABLE CONVERSATIONS DROP COLUMN hidden_from_history")
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            await execute_search(connection, env.user.id, build_fts_query("searchable"), 30, 0)


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
