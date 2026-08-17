from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import quote

import orjson
import pytest
from fastapi import WebSocketDisconnect

import file_storage
from chat.routes import voice_io
from chat.services import attachment_uploads, deletion, message_rendering
from chat.services.stop_signals import stop_signals
from chat.services.warmup import normalize_model_ids


class DummyUser:
    def __init__(self, user_id: int = 7, username: str = "alice", admin: bool = False):
        self.id = user_id
        self.username = username
        self._admin = admin

    @property
    async def is_admin(self):
        return self._admin


def _legacy_pdf_path(username: str, filename: str) -> str:
    prefix1, prefix2, user_hash = message_rendering.generate_user_hash(username)
    return (
        f"users/{prefix1}/{prefix2}/{user_hash}/files/000/0042/"
        f"pdf/uploads/{filename}"
    )


@pytest.mark.asyncio
async def test_legacy_unicode_pdf_url_gets_cloudflare_signature(monkeypatch) -> None:
    base_url = "https://fstcdn.example.com/"
    document_path = _legacy_pdf_path(
        "alice",
        "0123456789abcdef0123456789abcdef01234567_Informe análisis_日本語.pdf",
    )
    signing_calls = []

    def fake_sign(path, *, expiration_seconds):
        signing_calls.append((path, expiration_seconds))
        return f"{base_url}{quote(path, safe='/')}?expires=123&signature=signed"

    monkeypatch.setattr(message_rendering, "CLOUDFLARE_BASE_URL", base_url)
    monkeypatch.setattr(message_rendering, "CLOUDFLARE_FOR_IMAGES", True)
    monkeypatch.setattr(
        message_rendering,
        "generate_signed_url_cloudflare",
        fake_sign,
    )

    message = orjson.dumps([{
        "type": "document_url",
        "document_url": {
            "url": f"{base_url}{document_path}",
            "filename": "Informe análisis_日本語.pdf",
        },
    }]).decode()
    rendered = await message_rendering.process_message(
        message,
        object(),
        DummyUser(),
        can_admin_view=False,
    )
    document = orjson.loads(rendered)[0]["document_url"]

    assert signing_calls == [(document_path, 3600)]
    assert document["url"] == (
        f"{base_url}{quote(document_path, safe='/')}"
        "?expires=123&signature=signed"
    )


@pytest.mark.asyncio
async def test_legacy_encoded_unicode_pdf_url_gets_owner_token(monkeypatch) -> None:
    base_url = "https://fstcdn.example.com/"
    document_path = _legacy_pdf_path(
        "alice",
        "fedcba9876543210fedcba9876543210fedcba98_Análisis clínico_東京.pdf",
    )
    token_users = []

    async def fake_token(user):
        token_users.append(user.username)
        return "owner-token"

    monkeypatch.setattr(message_rendering, "CLOUDFLARE_BASE_URL", base_url)
    monkeypatch.setattr(message_rendering, "CLOUDFLARE_FOR_IMAGES", False)
    monkeypatch.setattr(message_rendering, "get_or_generate_img_token", fake_token)

    encoded_path = quote(document_path, safe="/")
    message = orjson.dumps([{
        "type": "document_url",
        "document_url": {"url": f"{base_url}{encoded_path}"},
    }]).decode()
    rendered = await message_rendering.process_message(
        message,
        object(),
        DummyUser(),
        can_admin_view=False,
    )
    document = orjson.loads(rendered)[0]["document_url"]

    assert token_users == ["alice"]
    assert document["url"] == f"{base_url}{encoded_path}?token=owner-token"


@pytest.mark.asyncio
async def test_legacy_pdf_keeps_authorized_urls_and_rejects_other_owner(
    monkeypatch,
) -> None:
    base_url = "https://fstcdn.example.com/"
    own_path = quote(
        _legacy_pdf_path("alice", f"{'a' * 40}_Informe privado.pdf"),
        safe="/",
    )
    other_path = quote(
        _legacy_pdf_path("bob", f"{'b' * 40}_Otro usuario.pdf"),
        safe="/",
    )
    existing_token_url = f"{base_url}{own_path}?token=existing-token"
    existing_signature_url = (
        f"{base_url}{own_path}?expires=123&signature=existing-signature"
    )
    other_owner_url = f"{base_url}{other_path}"

    def unexpected_sign(*_args, **_kwargs):
        raise AssertionError("authorized or cross-owner URLs must not be signed")

    monkeypatch.setattr(message_rendering, "CLOUDFLARE_BASE_URL", base_url)
    monkeypatch.setattr(message_rendering, "CLOUDFLARE_FOR_IMAGES", True)
    monkeypatch.setattr(
        message_rendering,
        "generate_signed_url_cloudflare",
        unexpected_sign,
    )

    message = orjson.dumps([
        {"type": "document_url", "document_url": {"url": existing_token_url}},
        {"type": "document_url", "document_url": {"url": existing_signature_url}},
        {"type": "document_url", "document_url": {"url": other_owner_url}},
    ]).decode()
    rendered = await message_rendering.process_message(
        message,
        object(),
        DummyUser(),
        can_admin_view=False,
    )
    urls = [block["document_url"]["url"] for block in orjson.loads(rendered)]

    assert urls == [existing_token_url, existing_signature_url, other_owner_url]


def test_warmup_model_ids_are_capped_before_normalization() -> None:
    assert normalize_model_ids(range(1, 30)) == tuple(range(1, 17))
    assert normalize_model_ids(iter(range(1, 30))) == tuple(range(1, 17))


@pytest.mark.asyncio
async def test_text_attachment_decode_runs_in_worker_thread(monkeypatch) -> None:
    calls = []

    def fake_decode(data, filename):
        calls.append(("decode", data, filename))
        return "decoded text"

    async def fake_to_thread(function, *args, **kwargs):
        calls.append(("to_thread", function))
        return function(*args, **kwargs)

    async def fake_create_pending_text_attachment(**kwargs):
        return kwargs

    monkeypatch.setattr(attachment_uploads, "decode_text_file", fake_decode)
    monkeypatch.setattr(attachment_uploads.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        attachment_uploads,
        "create_pending_text_attachment",
        fake_create_pending_text_attachment,
    )

    result = await attachment_uploads.create_pending_attachment_from_upload(
        user_id=7,
        conversation_id=9,
        data=b"hello",
        filename="notes.txt",
        content_type="text/plain",
    )

    assert calls == [
        ("to_thread", fake_decode),
        ("decode", b"hello", "notes.txt"),
    ]
    assert result["text_content"] == "decoded text"


@pytest.mark.asyncio
async def test_voice_websocket_reports_bad_json_and_always_disconnects(monkeypatch) -> None:
    class FakeWebSocket:
        def __init__(self):
            self.receive_count = 0

        async def receive_text(self):
            self.receive_count += 1
            if self.receive_count == 1:
                return "not-json"
            raise WebSocketDisconnect()

        async def close(self, **_kwargs):
            return None

    class FakeManager:
        def __init__(self):
            self.sent = []
            self.disconnect_count = 0

        async def connect(self, _websocket):
            return None

        async def send_json(self, _websocket, payload):
            self.sent.append(payload)

        def disconnect(self, _websocket):
            self.disconnect_count += 1

    fake_manager = FakeManager()

    async def authenticated(_websocket):
        return DummyUser()

    monkeypatch.setattr(voice_io, "manager", fake_manager)
    monkeypatch.setattr(voice_io, "get_current_user_from_websocket", authenticated)
    monkeypatch.setattr(voice_io, "READONLY_MODE", False)

    await voice_io.websocket_endpoint(FakeWebSocket())

    assert fake_manager.sent == [
        {"action": "error", "error": "Invalid JSON payload"}
    ]
    assert fake_manager.disconnect_count == 1


@pytest.mark.asyncio
async def test_owned_delete_sets_stop_signal_inside_write_lock(mock_db, monkeypatch) -> None:
    events = []

    @asynccontextmanager
    async def tracked_lock(conversation_id):
        events.append(("lock_enter", conversation_id))
        yield
        events.append(("lock_exit", conversation_id))

    async def no_memory_purge(**_kwargs):
        return set()

    async def delete_rows(conn, *, conversation_id, **_kwargs):
        events.append(("delete", stop_signals.get(conversation_id)))
        await conn.execute("DELETE FROM CONVERSATIONS WHERE id = ?", (conversation_id,))

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(deletion, "conversation_write_lock", tracked_lock)
    monkeypatch.setattr(deletion, "purge_linked_memory_providers_best_effort", no_memory_purge)
    monkeypatch.setattr(deletion, "delete_conversation_rows", delete_rows)
    monkeypatch.setattr(deletion, "prune_unreferenced_blobs", no_op)
    monkeypatch.setattr(deletion, "delete_conversation_files_for_user", no_op)

    async with mock_db() as conn:
        await conn.execute("INSERT INTO USERS (id, username) VALUES (7, 'alice')")
        await conn.execute(
            "INSERT INTO CONVERSATIONS (id, user_id, role_id) VALUES (91, 7, 1)"
        )
        await conn.commit()

    stop_signals.pop(91, None)
    result = await deletion.delete_owned_conversation(DummyUser(), 91)

    assert result["success"] is True
    assert events == [("lock_enter", 91), ("delete", True), ("lock_exit", 91)]
    assert stop_signals[91] is True
    stop_signals.pop(91, None)


@pytest.mark.asyncio
async def test_attachment_page_batch_revalidates_each_block_context(monkeypatch) -> None:
    records = [
        {
            "public_id": "att_valid",
            "user_id": 7,
            "conversation_id": 9,
            "message_id": 101,
            "attachment_type": "text",
            "original_filename": "visible.txt",
            "text_line_count": 4,
        },
        {
            "public_id": "att_other_message",
            "user_id": 7,
            "conversation_id": 9,
            "message_id": 102,
            "attachment_type": "text",
            "original_filename": "other-message-secret.txt",
        },
        {
            "public_id": "att_other_owner",
            "user_id": 8,
            "conversation_id": 9,
            "message_id": 101,
            "attachment_type": "text",
            "original_filename": "other-owner-secret.txt",
        },
        {
            "public_id": "att_other_conversation",
            "user_id": 7,
            "conversation_id": 10,
            "message_id": 101,
            "attachment_type": "text",
            "original_filename": "other-conversation-secret.txt",
        },
        {
            "public_id": "att_wrong_type",
            "user_id": 7,
            "conversation_id": 9,
            "message_id": 101,
            "attachment_type": "text",
            "original_filename": "wrong-type-secret.txt",
        },
    ]
    connection_count = 0
    executed = []

    class FakeCursor:
        async def fetchall(self):
            return records

    class FakeConnection:
        async def execute(self, query, params):
            executed.append((query, params))
            return FakeCursor()

    @asynccontextmanager
    async def fake_connection(*, readonly=False):
        nonlocal connection_count
        assert readonly is True
        connection_count += 1
        yield FakeConnection()

    monkeypatch.setattr(message_rendering, "get_db_connection", fake_connection)

    blocks = [
        {
            "type": "text_file",
            "text_file": {"attachment_ref": public_id, "url": "placeholder"},
        }
        for public_id in (
            "att_valid",
            "att_other_message",
            "att_other_owner",
            "att_other_conversation",
        )
    ]
    blocks.append({
        "type": "document_url",
        "document_url": {"attachment_ref": "att_wrong_type", "url": "placeholder"},
    })
    message = orjson.dumps(blocks).decode()

    attachment_records = await message_rendering.preload_attachment_records_for_messages(
        [(101, message)],
        user_id=7,
        conversation_id=9,
    )
    rendered = await message_rendering.process_message(
        message,
        object(),
        DummyUser(),
        conversation_id=9,
        message_id=101,
        attachment_records=attachment_records,
        can_admin_view=False,
    )
    rendered_blocks = orjson.loads(rendered)

    assert connection_count == 1
    assert len(executed) == 1
    assert "fa.public_id IN" in executed[0][0]
    assert rendered_blocks[0]["text_file"] == {
        "attachment_ref": "att_valid",
        "url": "/api/attachments/att_valid/download",
        "filename": "visible.txt",
        "lines": 4,
    }
    for block in rendered_blocks[1:]:
        info = block.get("text_file") or block.get("document_url")
        assert info["url"] == "placeholder"
        assert "filename" not in info


@pytest.mark.asyncio
async def test_attachment_page_batch_query_runs_against_sqlite(mock_db, monkeypatch) -> None:
    message = orjson.dumps([{
        "type": "text_file",
        "text_file": {"attachment_ref": "att_sqlite"},
    }]).decode()

    async with mock_db() as conn:
        await file_storage.ensure_file_storage_schema(conn)
        await conn.execute("INSERT INTO USERS (id, username) VALUES (17, 'sqlite-user')")
        await conn.execute(
            "INSERT INTO CONVERSATIONS (id, user_id, role_id) VALUES (190, 17, 1)"
        )
        await conn.execute(
            """
            INSERT INTO MESSAGES (id, conversation_id, user_id, message, type)
            VALUES (191, 190, 17, ?, 'user')
            """,
            (message,),
        )
        blob_cursor = await conn.execute(
            """
            INSERT INTO FILE_BLOBS
                (sha256, size_bytes, kind, mime_detected, storage_key, text_line_count, status)
            VALUES ('abc', 3, 'text', 'text/plain', 'text/test/abc.txt', 1, 'ready')
            RETURNING id
            """
        )
        blob_id = (await blob_cursor.fetchone())[0]
        await conn.execute(
            """
            INSERT INTO FILE_ATTACHMENTS
                (public_id, blob_id, user_id, conversation_id, message_id,
                 attachment_type, original_filename, status)
            VALUES ('att_sqlite', ?, 17, 190, 191, 'text', 'sqlite.txt', 'active')
            """,
            (blob_id,),
        )
        await conn.commit()

    monkeypatch.setattr(message_rendering, "get_db_connection", mock_db)
    records = await message_rendering.preload_attachment_records_for_messages(
        [(191, message)],
        user_id=17,
        conversation_id=190,
    )

    assert records["att_sqlite"]["message_id"] == 191
    assert records["att_sqlite"]["original_filename"] == "sqlite.txt"
