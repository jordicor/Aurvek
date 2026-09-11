"""Coherent private source capture uses real SQLite and delegated ownership."""
import base64

import pytest

from integrations.applications import sources as source_module
from integrations.applications.sources import ApplicationSourceService
from integrations.embed.models import EmbedError
from tests.test_application_sources import (
    application_files, application_frames, native_pilot, login,
)


async def replace_messages(pilot, conversation_id, values):
    async with pilot.connect() as connection:
        await connection.execute("DELETE FROM MESSAGES WHERE conversation_id=?", (conversation_id,))
        await connection.executemany(
            "INSERT INTO MESSAGES(conversation_id,user_id,message,type,date) VALUES(?,1,?,?,?)",
            [(conversation_id, content, role, "2026-09-11 04:00:00") for content, role in values],
        )
        await connection.commit()


@pytest.mark.asyncio
async def test_snapshot_keeps_roles_order_and_detects_all_message_mutations(application_frames, application_files):
    pilot, _, principal, first, _, _ = application_frames
    cid = first["conversation_id"]
    await replace_messages(pilot, cid, [("question?", "bot"), ("source answer", "user")])
    service = ApplicationSourceService(pilot.store)
    original = await service.conversation_snapshot(principal, cid, "practice")
    assert original["snapshot_version"] == "aurvek.conversation_snapshot.v1"
    assert original["complete"] is True and original["message_count"] == 2
    assert [(row["content"], row["role"]) for row in original["items"]] == [
        ("question?", "bot"), ("source answer", "user"),
    ]
    assert all(row["created_at"] == "2026-09-11 04:00:00" for row in original["items"])
    revision_only = await service.conversation_snapshot(principal, cid, "practice", mode="revision")
    assert revision_only["revision"] == original["revision"]
    assert "items" not in revision_only
    previous = original
    for statement, values in [
        ("UPDATE MESSAGES SET message='edited' WHERE id=?", (original["items"][1]["message_id"],)),
        ("INSERT INTO MESSAGES(conversation_id,user_id,message,type) VALUES(?,1,'new','user')", (cid,)),
        ("DELETE FROM MESSAGES WHERE id=?", (original["items"][0]["message_id"],)),
    ]:
        async with pilot.connect() as connection:
            await connection.execute(statement, values)
            await connection.commit()
        current = await service.conversation_snapshot(principal, cid, "practice")
        assert current["revision"] != previous["revision"]
        previous = current
    assert previous["message_count"] == original["message_count"]


@pytest.mark.asyncio
async def test_snapshot_keeps_one_database_revision_across_internal_batches(
    application_frames, application_files, monkeypatch,
):
    pilot, _, principal, first, _, _ = application_frames
    cid = first["conversation_id"]
    async with pilot.connect() as connection:
        await connection.execute("PRAGMA journal_mode=WAL")
    await replace_messages(pilot, cid, [(f"source-{index}", "user") for index in range(401)])
    service = ApplicationSourceService(pilot.store)
    project = service._messages
    writes = 0

    async def project_with_concurrent_write(connection, scope, rows):
        nonlocal writes
        if writes == 0:
            async with pilot.connect() as writer:
                await writer.execute(
                    "UPDATE MESSAGES SET message='concurrent edit' WHERE conversation_id=? AND message='source-400'",
                    (cid,),
                )
                await writer.execute(
                    "INSERT INTO MESSAGES(conversation_id,user_id,message,type) VALUES(?,1,'concurrent append','user')",
                    (cid,),
                )
                await writer.commit()
            writes += 1
        return await project(connection, scope, rows)

    monkeypatch.setattr(service, "_messages", project_with_concurrent_write)
    snapshot = await service.conversation_snapshot(principal, cid, "practice")
    assert snapshot["message_count"] == 401
    assert snapshot["items"][-1]["content"] == "source-400"
    refreshed = await service.conversation_snapshot(principal, cid, "practice")
    assert refreshed["message_count"] == 402
    assert refreshed["items"][-2]["content"] == "concurrent edit"
    assert refreshed["revision"] != snapshot["revision"]


@pytest.mark.asyncio
async def test_snapshot_refuses_oversize_instead_of_truncating(application_frames, application_files, monkeypatch):
    pilot, _, principal, first, _, _ = application_frames
    cid = first["conversation_id"]
    await replace_messages(pilot, cid, [("source-one", "user"), ("source-two", "bot")])
    service = ApplicationSourceService(pilot.store)
    monkeypatch.setattr(source_module, "SNAPSHOT_MAX_MESSAGES", 1)
    with pytest.raises(EmbedError, match="source_snapshot_too_large"):
        await service.conversation_snapshot(principal, cid, "practice")
    monkeypatch.setattr(source_module, "SNAPSHOT_MAX_MESSAGES", 5000)
    monkeypatch.setattr(source_module, "SNAPSHOT_MAX_BYTES", 3)
    with pytest.raises(EmbedError, match="source_snapshot_too_large"):
        await service.conversation_snapshot(principal, cid, "practice", mode="revision")


@pytest.mark.asyncio
async def test_snapshot_requires_live_subject_and_context(application_frames, application_files):
    pilot, _, principal, first, _, _ = application_frames
    service = ApplicationSourceService(pilot.store)
    cid = first["conversation_id"]
    with pytest.raises(EmbedError, match="not_found"):
        await service.conversation_snapshot(principal, cid, "other")
    await pilot.store.provision_membership("first", 2)
    other_subject, _ = await login(pilot, 2)
    with pytest.raises(EmbedError, match="not_found"):
        await service.conversation_snapshot(other_subject, cid, "practice")
    async with pilot.connect() as connection:
        await connection.execute("UPDATE APPLICATION_CONTEXTS SET active=0 WHERE context_id=?", (first["context_id"],))
        await connection.commit()
    with pytest.raises(EmbedError, match="not_found"):
        await service.conversation_snapshot(principal, cid, "practice", mode="revision")


@pytest.mark.asyncio
async def test_snapshot_route_requires_credentials_and_is_not_cacheable(application_frames, application_files, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from integrations.applications import sources_api

    pilot, _, _, first, _, _ = application_frames
    monkeypatch.setattr(sources_api, "get_embed_store", lambda: pilot.store)
    _, credential = await login(pilot)
    app = FastAPI()
    app.include_router(sources_api.router)
    body = {"delegated_credential": credential, "conversation_id": first["conversation_id"], "context_ref": "practice"}
    headers = {"Authorization": "Basic " + base64.b64encode(("first:secret" + "a" * 40).encode()).decode()}
    async with AsyncClient(transport=ASGITransport(app, client=("127.0.0.1", 3000)), base_url="http://identity.example") as client:
        denied = await client.post("/api/applications/v1/conversation-snapshot", json=body)
        assert denied.status_code == 401
        response = await client.post("/api/applications/v1/conversation-snapshot", json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        invalid = await client.post("/api/applications/v1/conversation-snapshot", json={**body, "mode": "truncate"}, headers=headers)
        assert invalid.status_code == 422
        assert credential not in invalid.text
