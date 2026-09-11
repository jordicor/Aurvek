"""Application files use native storage, provider content and live resource scope."""

import base64
import io
from pathlib import Path

import fitz
import orjson
import pytest
import pytest_asyncio
from fastapi import HTTPException
from PIL import Image

import file_storage
from ai_runtime.attachments.media import hydrate_image_for_context
from ai_runtime.attachments.pdf import hydrate_pdf_for_context
from ai_runtime.attachments.text_files import text_file_block_to_text_for_context
from chat.routes import attachments
from chat.services import attachment_uploads
from integrations.embed import identity
from tests.test_application_frames import application_frames
from tests.test_embed_native_creation import native_pilot


@pytest_asyncio.fixture
async def application_files(application_frames, monkeypatch, tmp_path):
    from auth import get_user_by_id

    pilot, service, principal, first, coach, _ = application_frames
    for module in (attachments, attachment_uploads):
        monkeypatch.setattr(module, "get_db_connection", pilot.connect)
    monkeypatch.setattr(identity, "get_embed_store", lambda: pilot.store)
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "blobs")
    async with pilot.connect() as conn:
        await file_storage.ensure_file_storage_schema(conn)
        for table, column, path in (
            ("EMBED_APPS", "config_json", "$.capabilities.attachments"),
            ("APPLICATION_ASSISTANTS", "config_json", "$.capabilities.attachments"),
            ("EMBED_MEMBERSHIPS", "capabilities_json", "$.attachments"),
            ("APPLICATION_PARTICIPANTS", "capabilities_json", "$.attachments"),
        ):
            await conn.execute(
                f"UPDATE {table} SET {column}=json_set({column}, ?, json('true')) WHERE app_id='first'",
                (path,),
            )
        await conn.execute("UPDATE USER_DETAILS SET allow_file_upload=1 WHERE user_id=1")
        await conn.commit()
    return pilot, first, coach, await get_user_by_id(principal.user_id)


async def store_file(fixture, data, filename, content_type):
    pilot, first, _, user = fixture
    conversation_id = first["conversation_id"]
    assert await attachment_uploads.ensure_attachment_upload_allowed(conversation_id, user) is None
    pending = await attachment_uploads.create_pending_attachment_from_upload(
        user_id=user.id, conversation_id=conversation_id, data=data,
        filename=filename, content_type=content_type,
    )
    message = orjson.dumps([pending.block]).decode()
    async with pilot.connect() as conn:
        cursor = await conn.execute(
            "INSERT INTO MESSAGES(conversation_id,user_id,message,type) VALUES(?,?,?,'user')",
            (conversation_id, user.id, message),
        )
        await file_storage.finalize_message_attachments(
            conn, message_id=cursor.lastrowid, conversation_id=conversation_id,
            user_id=user.id, message_json=message,
        )
        await conn.commit()
    return pending


@pytest.mark.asyncio
async def test_native_document_and_image_content_reaches_provider_with_conversation_scope(application_files):
    _, first, coach, user = application_files
    conversation_id = first["conversation_id"]
    text = await store_file(application_files, b"The project code is violet-73.", "notes.txt", "text/plain")
    assert "violet-73" in await text_file_block_to_text_for_context(text.block, user, conversation_id)
    assert "violet-73" not in await text_file_block_to_text_for_context(text.block, user, coach["conversation_id"])

    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "The reference animal is an otter.")
        pdf_data = document.tobytes()
    pdf = await store_file(application_files, pdf_data, "evidence.pdf", "application/pdf")
    payload = await hydrate_pdf_for_context(pdf.block, "Claude", user, conversation_id)
    assert base64.b64decode(payload["source"]["data"]) == pdf_data
    extracted = await hydrate_pdf_for_context(pdf.block, "O1", user, conversation_id)
    assert "otter" in extracted["text"]
    assert await hydrate_pdf_for_context(pdf.block, "Claude", user, coach["conversation_id"]) is None

    buffer = io.BytesIO()
    Image.new("RGB", (24, 24), color="red").save(buffer, format="PNG")
    picture = await store_file(application_files, buffer.getvalue(), "square.png", "image/png")
    image = await hydrate_image_for_context(picture.block, "Claude", user, conversation_id=conversation_id)
    with Image.open(io.BytesIO(base64.b64decode(image["source"]["data"]))) as decoded:
        assert decoded.size == (24, 24)
        assert decoded.convert("RGB").getpixel((12, 12))[0] > 240
    assert await hydrate_image_for_context(picture.block, "Claude", user, conversation_id=coach["conversation_id"]) is None

    response = await attachments.attachment_download(text.public_id, user)
    assert Path(response.path).read_bytes() == b"The project code is violet-73."
    assert (await attachments.delete_attachment(text.public_id, user)).status_code == 200
    assert "violet-73" not in await text_file_block_to_text_for_context(text.block, user, conversation_id)


@pytest.mark.asyncio
async def test_native_attachment_routes_recheck_application_access_and_upload_capability(application_files):
    pilot, first, _, user = application_files
    conversation_id = first["conversation_id"]
    pending = await store_file(application_files, b"Private evidence.", "notes.txt", "text/plain")
    async with pilot.connect() as conn:
        await conn.execute("UPDATE APPLICATION_PARTICIPANTS SET capabilities_json=json_set(capabilities_json,'$.attachments',json('false'))")
        await conn.commit()
    denied = await attachment_uploads.ensure_attachment_upload_allowed(conversation_id, user)
    assert denied.status_code == 403
    assert orjson.loads(denied.body)["error_code"] == "application_capability_denied"

    async with pilot.connect() as conn:
        await conn.execute("UPDATE APPLICATION_PARTICIPANTS SET active=0")
        await conn.commit()
    assert (await attachment_uploads.ensure_attachment_upload_allowed(conversation_id, user)).status_code == 404
    discard = await attachments.discard_uploaded_attachments(conversation_id, user, orjson.dumps([pending.public_id]).decode())
    assert discard.status_code == 404
    for handler in (attachments.attachment_download, attachments.delete_attachment):
        with pytest.raises(HTTPException) as rejected:
            await handler(pending.public_id, user)
        assert rejected.value.status_code == 404
    async with pilot.connect() as conn:
        assert await file_storage.resolve_attachment_for_user(conn, public_id=pending.public_id, user_id=user.id)
