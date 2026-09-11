"""Backend source reads use real delegated scope and native SQLite/file storage."""
import base64
import hashlib
import time
from pathlib import Path

import pytest

import file_storage
from integrations.applications.models import OpenConversationRequest
from integrations.applications.sources import ApplicationSourceService
from integrations.embed.models import EmbedError
from tests.test_application_attachments import application_files, store_file
from tests.test_application_frames import application_frames
from tests.test_embed_native_creation import native_pilot, _native_login


async def login(pilot, user_id=1):
    from auth import get_user_by_id
    user = await get_user_by_id(user_id)
    user.session_expires_at = int(time.time()) + 3600
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    tx = await pilot.store.authorize_begin("first", pilot.configs["first"].redirect_uris[0], "s" * 32, challenge)
    code = await pilot.store.authorize_complete(tx, user, "verified-native-source-session")
    result = await pilot.store.exchange_code("first", code["code"], verifier, code["redirect_uri"])
    return await pilot.store.inspect_identity("first", result["delegated_credential"]), result["delegated_credential"]


async def enable(pilot, *capabilities):
    async with pilot.connect() as connection:
        for table, column, prefix in (("EMBED_APPS", "config_json", "$.capabilities."),
            ("APPLICATION_ASSISTANTS", "config_json", "$.capabilities."),
            ("EMBED_MEMBERSHIPS", "capabilities_json", "$."),
            ("APPLICATION_PARTICIPANTS", "capabilities_json", "$.")):
            for cap in capabilities:
                await connection.execute(f"UPDATE {table} SET {column}=json_set({column},?,json('true'))",
                                         (prefix + cap,))
        await connection.commit()


@pytest.mark.asyncio
async def test_sources_paginate_detect_edits_deletions_and_enforce_subject_context(application_frames, application_files):
    pilot, apps, principal, first, coach, _ = application_frames
    sources = ApplicationSourceService(pilot.store)
    cid = first["conversation_id"]
    async with pilot.connect() as connection:
        await connection.execute("DELETE FROM MESSAGES WHERE conversation_id=?", (cid,))
        await connection.executemany("INSERT INTO MESSAGES(conversation_id,user_id,message,type) VALUES(?,1,?,'user')",
                                     [(cid, "first"), (cid, "second"), (cid, "third")])
        await connection.commit()
    one = await sources.list_sources(principal, cid, "practice", limit=2)
    assert [item["content"] for item in one["items"]] == ["first", "second"]
    two = await sources.list_sources(principal, cid, "practice", limit=2, cursor=one["next_cursor"])
    assert [item["content"] for item in two["items"]] == ["third"] and two["next_cursor"] is None
    assert one == await sources.list_sources(principal, cid, "practice", limit=2)
    async with pilot.connect() as connection:
        await connection.execute("UPDATE MESSAGES SET message='corrected' WHERE id=?", (one["items"][0]["message_id"],))
        await connection.execute("DELETE FROM MESSAGES WHERE id=?", (one["items"][1]["message_id"],))
        await connection.commit()
    updated = await sources.list_sources(principal, cid, "practice", limit=2)
    assert updated["revision"] != one["revision"]
    assert [item["content"] for item in updated["items"]] == ["corrected", "third"]
    assert updated["items"][0]["revision"] != one["items"][0]["revision"]
    assert updated["items"][1]["revision"] == two["items"][0]["revision"]
    await apps.grant_context("first", principal.subject, "other")
    other = await apps.open_conversation(principal, OpenConversationRequest(operation_id="other-source-context", context_ref="other"))
    for target, context in ((cid, "other"), (other["conversation_id"], "practice")):
        with pytest.raises(EmbedError, match="not_found"):
            await sources.list_sources(principal, target, context)
    with pytest.raises(EmbedError, match="invalid_cursor"):
        await sources.list_sources(principal, coach["conversation_id"], "practice", cursor=one["next_cursor"])
    second = await _native_login(pilot, "second")
    with pytest.raises(EmbedError, match="not_found"):
        await sources.list_sources(second, cid, "practice")
    await pilot.store.provision_membership("first", 2)
    other_subject, _ = await login(pilot, 2)
    with pytest.raises(EmbedError, match="not_found"):
        await sources.list_sources(other_subject, cid, "practice")


@pytest.mark.asyncio
async def test_private_file_refs_keep_bytes_and_recheck_deletion_and_capability(application_files, application_frames):
    pilot, first, coach, user = application_files
    principal = application_frames[2]
    sources = ApplicationSourceService(pilot.store)
    pending = await store_file(application_files, b"original source\n", "source.txt", "text/plain")
    cid = first["conversation_id"]
    listing = await sources.list_sources(principal, cid, "practice", kind="attachments")
    assert len(listing["items"]) == 1
    item = listing["items"][0]
    assert item["source_ref"] == "attachment:" + pending.public_id
    assert "path" not in item and "url" not in item
    response = await sources.source_content(principal, cid, "practice", item["source_ref"])
    assert Path(response.path).read_bytes() == b"original source\n"
    messages = await sources.list_sources(principal, cid, "practice")
    exported = next(row for row in messages["items"] if row["message_id"] == item["message_id"])
    assert exported["content"][0]["text_file"]["source_ref"] == item["source_ref"]
    assert "url" not in exported["content"][0]["text_file"]
    with pytest.raises(EmbedError, match="not_found"):
        await sources.source_content(principal, coach["conversation_id"], "practice", item["source_ref"])
    async with pilot.connect() as connection:
        await connection.execute("UPDATE APPLICATION_PARTICIPANTS SET capabilities_json=json_set(capabilities_json,'$.attachments',json('false'))")
        await connection.commit()
    with pytest.raises(EmbedError, match="not_found"):
        await sources.source_content(principal, cid, "practice", item["source_ref"])
    hidden = await sources.list_sources(principal, cid, "practice")
    exported = next(row for row in hidden["items"] if row["message_id"] == item["message_id"])
    assert exported["files"] == [] and exported["content"][0]["text_file"] == {"unavailable": True}
    await enable(pilot, "attachments")
    async with pilot.connect() as connection:
        await file_storage.delete_attachment_and_rewrite_message(connection, public_id=pending.public_id, user_id=user.id)
        await connection.commit()
    deleted = await sources.list_sources(principal, cid, "practice", kind="attachments")
    assert deleted["items"] == [] and deleted["revision"] != listing["revision"]
    with pytest.raises(EmbedError, match="not_found"):
        await sources.source_content(principal, cid, "practice", item["source_ref"])


@pytest.mark.asyncio
async def test_generated_media_reference_observes_replacement_and_original_web_audio(application_files, application_frames, monkeypatch, tmp_path):
    import save_images
    from chat.services import generated_media
    from integrations.applications import web_audio
    from tests.test_application_generated_media import image_bytes, media_environment
    from tests.test_application_browser_voice import recording
    from types import SimpleNamespace

    pilot, first, coach, user = application_files
    principal = application_frames[2]
    cid = first["conversation_id"]
    await enable(pilot, "image_generation", "voice", "stt", "tts")
    media_environment(application_files, monkeypatch, tmp_path)
    monkeypatch.setattr(web_audio, "get_db_connection", pilot.connect)
    urls = await save_images.save_image_locally(None, image_bytes(), user, cid, "test.png", "bot")
    sources = ApplicationSourceService(pilot.store)
    listing = await sources.list_sources(principal, cid, "practice", kind="generated_media")
    assert listing["items"]
    media_id = generated_media.parse_generated_media_url(urls[2])[1]
    item = next(row for row in listing["items"] if row["source_ref"] == f"generated:{media_id}")
    response = await sources.source_content(principal, cid, "practice", item["source_ref"])
    from PIL import Image
    with Image.open(response.path) as decoded:
        assert decoded.size == (32, 32) and decoded.convert("RGB").getpixel((0, 0))[2] > 240
    Path(response.path).write_bytes(b"replacement")
    changed = await sources.list_sources(principal, cid, "practice", kind="generated_media")
    assert changed["revision"] != listing["revision"]
    with pytest.raises(EmbedError, match="not_found"):
        await sources.source_content(principal, coach["conversation_id"], "practice", item["source_ref"])
    async with pilot.connect() as connection:
        message = await connection.execute("INSERT INTO MESSAGES(conversation_id,user_id,message,type) VALUES(?,1,'Retained speech','user')", (cid,))
        mid = message.lastrowid
        await connection.commit()
    voice = web_audio.VoiceRecording(cid, user.id)
    await voice.retain_input(recording())
    async with pilot.connect() as connection:
        await voice.commit(SimpleNamespace(user_message_id=mid, assistant_message_id=None), connection)
        await connection.commit()
    page = await sources.list_sources(principal, cid, "practice")
    original = next(row for row in page["items"] if row["message_id"] == mid)["files"][0]
    audio = await sources.source_content(principal, cid, "practice", original["source_ref"])
    assert Path(audio.path).read_bytes() == recording()
    assert audio.media_type == "audio/wav"


@pytest.mark.asyncio
async def test_handoff_notes_keep_provenance_without_exporting_other_assistant_messages(application_frames):
    from integrations.applications.handoff import ApplicationHandoffService
    from integrations.applications.models import HandoffRequest
    pilot, _, principal, first, coach, _ = application_frames
    await ApplicationHandoffService(pilot.store).handoff(principal, HandoffRequest(
        operation_id="source-handoff-01", source_conversation_id=first["conversation_id"],
        assistant_id="coach", note="Discuss the saved draft."))
    sources = ApplicationSourceService(pilot.store)
    page = await sources.list_sources(principal, coach["conversation_id"], "practice", kind="handoffs")
    assert len(page["items"]) == 1
    note = page["items"][0]
    assert note["note"] == "Discuss the saved draft."
    assert note["source_conversation_id"] == first["conversation_id"] and note["note_origin"]
    assert "messages" not in note


@pytest.mark.asyncio
async def test_retained_phone_turn_uses_native_range_and_rechecks_removal(application_frames, application_files, tmp_path, monkeypatch):
    import io
    import wave
    from functools import partial
    from integrations.telephony import recording_storage
    pilot, _, principal, first, coach, _ = application_frames
    cid = first["conversation_id"]
    await enable(pilot, "voice", "stt", "tts")
    call_id = "call-source-test"
    root = tmp_path / "phone-recordings"
    path = recording_storage.private_call_directory(call_id, root=root) / "assistant.mulaw"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff" * 800)
    monkeypatch.setattr(recording_storage, "resolve_private_recording_path",
        partial(recording_storage.resolve_private_recording_path, root=root))
    async with pilot.connect() as connection:
        contact = await connection.execute("INSERT INTO PHONE_CONTACTS(owner_user_id,display_name,e164,timezone_name) VALUES(1,'Source test','+12025550101','UTC')")
        number = await connection.execute("INSERT INTO TELEPHONY_NUMBERS(provider_number_sid,e164) VALUES('PN-source-test','+12025550102')")
        await connection.execute("""INSERT INTO PHONE_CALLS(id,owner_user_id,conversation_id,contact_id,telephony_number_id,
            direction,from_e164,to_e164,dispatch_token,binding_snapshot_json,config_snapshot_json,status,recording_enabled)
            VALUES(?,1,?,?,?,'outbound','+12025550102','+12025550101','source-test','{}','{}','completed',1)""",
            (call_id, cid, contact.lastrowid, number.lastrowid))
        message = await connection.execute("INSERT INTO MESSAGES(conversation_id,user_id,message,type) VALUES(?,1,'Phone assistant reply','bot')", (cid,))
        mid = message.lastrowid
        await connection.execute("INSERT INTO PHONE_CALL_MESSAGE_LINKS(call_id,message_id,participant,origin_channel) VALUES(?,?,'assistant','phone')", (call_id, mid))
        await connection.execute("INSERT INTO PHONE_CALL_MESSAGE_AUDIO_RANGES(message_id,call_id,start_byte,end_byte) VALUES(?,?,80,240)", (mid, call_id))
        await connection.execute("INSERT INTO PHONE_RECORDINGS(call_id,status,assistant_path) VALUES(?,'available',?)", (call_id, str(path)))
        await connection.commit()
    sources = ApplicationSourceService(pilot.store)
    listing = await sources.list_sources(principal, cid, "practice")
    row = next(item for item in listing["items"] if item["message_id"] == mid)
    reference = row["files"][0]["source_ref"]
    assert reference == f"phone_audio:{mid}"
    response = await sources.source_content(principal, cid, "practice", reference)
    body = b"".join([chunk async for chunk in response.body_iterator])
    with wave.open(io.BytesIO(body)) as audio:
        assert audio.getnframes() == 160 and audio.getframerate() == 8000
    await response.background()
    with pytest.raises(EmbedError, match="not_found"):
        await sources.source_content(principal, coach["conversation_id"], "practice", reference)
    async with pilot.connect() as connection:
        await connection.execute("UPDATE PHONE_RECORDINGS SET status='deleted' WHERE call_id=?", (call_id,))
        await connection.commit()
    with pytest.raises(EmbedError, match="not_found"):
        await sources.source_content(principal, cid, "practice", reference)
    refreshed = await sources.list_sources(principal, cid, "practice")
    row = next(item for item in refreshed["items"] if item["message_id"] == mid)
    assert row["files"] == [] and refreshed["revision"] != listing["revision"]


@pytest.mark.asyncio
async def test_backend_routes_require_both_credentials_and_context(application_frames, application_files, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from integrations.applications import sources_api
    pilot, _, principal, first, _, _ = application_frames
    monkeypatch.setattr(sources_api, "get_embed_store", lambda: pilot.store)
    from integrations.embed import api as embed_api
    monkeypatch.setattr(embed_api, "get_embed_store", lambda: pilot.store)
    _, credential = await login(pilot)
    app = FastAPI()
    app.include_router(sources_api.router)
    async with AsyncClient(transport=ASGITransport(app, client=("127.0.0.1", 3000)), base_url="http://identity.example") as client:
        body = {"delegated_credential": credential, "conversation_id": first["conversation_id"], "context_ref": "practice"}
        denied = await client.post("/api/applications/v1/conversation-sources", json=body)
        assert denied.status_code == 401
        headers = {"Authorization": "Basic " + base64.b64encode(("first:secret" + "a" * 40).encode()).decode()}
        response = await client.post("/api/applications/v1/conversation-sources", json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        response = await client.post("/api/applications/v1/conversation-sources", json={**body, "context_ref": "other"}, headers=headers)
        assert response.status_code == 404
        response = await client.post("/api/applications/v1/conversation-sources", json={**body, "delegated_credential": "invalid" * 10}, headers=headers)
        assert response.status_code == 401
