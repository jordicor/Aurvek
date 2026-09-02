from __future__ import annotations

import hashlib

import orjson
import pytest

import file_storage
from chat.services.privacy import delete_conversation_rows, ensure_conversation_privacy_schema


async def _seed_user_conversation(conn, *, user_id: int = 1, conversation_id: int = 1) -> None:
    await conn.execute(
        "INSERT INTO USERS (id, username) VALUES (?, ?)",
        (user_id, f"user{user_id}"),
    )
    await conn.execute(
        "INSERT INTO CONVERSATIONS (id, user_id, role_id) VALUES (?, ?, 1)",
        (conversation_id, user_id),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_same_text_bytes_reuse_blob_but_keep_per_attachment_filename(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "file_blobs")
    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=1, conversation_id=1)
        await conn.execute(
            "INSERT INTO CONVERSATIONS (id, user_id, role_id) VALUES (2, 1, 1)"
        )
        await conn.commit()

    first = await file_storage.create_pending_text_attachment(
        user_id=1,
        conversation_id=1,
        text_content="same bytes",
        filename="first-name.txt",
    )
    second = await file_storage.create_pending_text_attachment(
        user_id=1,
        conversation_id=2,
        text_content="same bytes",
        filename="second-name.txt",
    )

    assert first.public_id != second.public_id
    assert first.blob_id == second.blob_id

    async with mock_db() as conn:
        blob_count = await (await conn.execute("SELECT COUNT(*) FROM FILE_BLOBS")).fetchone()
        rows = await (await conn.execute(
            "SELECT original_filename FROM FILE_ATTACHMENTS ORDER BY id"
        )).fetchall()

    assert blob_count[0] == 1
    assert [row[0] for row in rows] == ["first-name.txt", "second-name.txt"]


@pytest.mark.asyncio
async def test_finalize_activates_only_matching_pending_attachment(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "file_blobs")
    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=3, conversation_id=7)

    pending = await file_storage.create_pending_text_attachment(
        user_id=3,
        conversation_id=7,
        text_content="hello",
        filename="hello.txt",
    )
    message_json = orjson.dumps([pending.block]).decode("utf-8")

    async with mock_db() as conn:
        await conn.execute(
            """
            INSERT INTO MESSAGES (id, conversation_id, user_id, message, type, date)
            VALUES (70, 7, 3, ?, 'user', '2026-05-06 10:00:00')
            """,
            (message_json,),
        )
        await file_storage.finalize_message_attachments(
            conn,
            message_id=70,
            conversation_id=7,
            user_id=3,
            message_json=message_json,
        )
        await conn.commit()

    async with mock_db() as conn:
        row = await (await conn.execute(
            "SELECT status, message_id FROM FILE_ATTACHMENTS WHERE public_id = ?",
            (pending.public_id,),
        )).fetchone()

    assert row["status"] == "active"
    assert row["message_id"] == 70


@pytest.mark.asyncio
async def test_pending_attachment_bytes_are_scoped_to_owner_and_conversation(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "file_blobs")
    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=11, conversation_id=21)
        await _seed_user_conversation(conn, user_id=12, conversation_id=22)

    pending = await file_storage.create_pending_text_attachment(
        user_id=11,
        conversation_id=21,
        text_content="pending text",
        filename="pending.txt",
    )

    result = await file_storage.read_pending_attachment_bytes(
        pending.public_id,
        user_id=11,
        conversation_id=21,
    )
    wrong_user = await file_storage.read_pending_attachment_bytes(
        pending.public_id,
        user_id=12,
        conversation_id=21,
    )
    wrong_conversation = await file_storage.read_pending_attachment_bytes(
        pending.public_id,
        user_id=11,
        conversation_id=22,
    )

    assert result is not None
    data, attachment = result
    assert data == b"pending text"
    assert wrong_user is None
    assert wrong_conversation is None

    block = file_storage.attachment_record_to_block(attachment, data=data)
    assert block["type"] == "text_file"
    assert block["text_file"]["attachment_ref"] == pending.public_id
    assert block["text_file"]["filename"] == "pending.txt"


@pytest.mark.asyncio
async def test_pending_pdf_block_includes_retry_file_hash(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "file_blobs")
    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=8, conversation_id=12)

    pdf_data = b"%PDF-1.4\n%tiny test pdf bytes\n%%EOF\n"
    pending = await file_storage.create_pending_pdf_attachment(
        user_id=8,
        conversation_id=12,
        data=pdf_data,
        filename="retry.pdf",
        page_count=1,
    )

    assert pending.block["document_url"]["file_hash"] == hashlib.sha1(pdf_data).hexdigest()


@pytest.mark.asyncio
async def test_pending_audio_uses_detected_mime_and_persists_audio_kind(
    mock_db, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "file_blobs")
    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=31, conversation_id=41)

    audio_data = b"OggS\x00voice-note-bytes"
    pending = await file_storage.create_pending_audio_attachment(
        user_id=31,
        conversation_id=41,
        data=audio_data,
        filename="../untrusted-name.exe",
        mime_detected="audio/ogg",
        declared_mime="application/octet-stream",
    )

    async with mock_db() as conn:
        row = await (
            await conn.execute(
                """
                SELECT fb.kind, fb.mime_detected, fb.storage_key,
                       fa.attachment_type, fa.original_filename,
                       fa.declared_mime, fa.status
                FROM FILE_ATTACHMENTS AS fa
                JOIN FILE_BLOBS AS fb ON fb.id = fa.blob_id
                WHERE fa.public_id = ?
                """,
                (pending.public_id,),
            )
        ).fetchone()

    assert pending.kind == "audio"
    assert pending.size_bytes == len(audio_data)
    assert row["kind"] == "audio"
    assert row["attachment_type"] == "audio"
    assert row["mime_detected"] == "audio/ogg"
    assert row["declared_mime"] == "application/octet-stream"
    assert row["original_filename"] == "untrusted-name.exe"
    assert not row["storage_key"].lower().endswith(".exe")
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_finalize_pending_audio_is_idempotent_only_for_same_message(
    mock_db, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "file_blobs")
    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=32, conversation_id=42)
        await conn.executemany(
            """
            INSERT INTO MESSAGES (id, conversation_id, user_id, message, type, date)
            VALUES (?, 42, 32, 'voice note', 'user', '2026-09-02 10:00:00')
            """,
            ((420,), (421,)),
        )
        await conn.commit()

    pending = await file_storage.create_pending_audio_attachment(
        user_id=32,
        conversation_id=42,
        data=b"OggS\x00idempotent-audio",
        filename="voice.ogg",
        mime_detected="audio/ogg",
    )

    async with mock_db() as conn:
        await file_storage.finalize_pending_attachment(
            conn,
            pending.public_id,
            message_id=420,
            conversation_id=42,
            user_id=32,
            require_kind="audio",
        )
        # A webhook retry for the same persisted message must be a no-op success.
        await file_storage.finalize_pending_attachment(
            conn,
            pending.public_id,
            message_id=420,
            conversation_id=42,
            user_id=32,
            require_kind="audio",
        )
        with pytest.raises(ValueError):
            await file_storage.finalize_pending_attachment(
                conn,
                pending.public_id,
                message_id=421,
                conversation_id=42,
                user_id=32,
                require_kind="audio",
            )
        await conn.commit()

    async with mock_db() as conn:
        row = await (
            await conn.execute(
                "SELECT status, message_id FROM FILE_ATTACHMENTS WHERE public_id = ?",
                (pending.public_id,),
            )
        ).fetchone()

    assert (row["status"], row["message_id"]) == ("active", 420)


@pytest.mark.asyncio
async def test_finalize_pending_attachment_rejects_scope_and_kind_mismatches(
    mock_db, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "file_blobs")
    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=33, conversation_id=43)
        await _seed_user_conversation(conn, user_id=34, conversation_id=44)
        await conn.execute(
            """
            INSERT INTO MESSAGES (id, conversation_id, user_id, message, type, date)
            VALUES (430, 43, 33, 'voice note', 'user', '2026-09-02 10:00:00')
            """
        )
        await conn.commit()

    pending = await file_storage.create_pending_audio_attachment(
        user_id=33,
        conversation_id=43,
        data=b"OggS\x00scoped-audio",
        filename="voice.ogg",
        mime_detected="audio/ogg",
    )

    async with mock_db() as conn:
        for mismatch in (
            {"user_id": 34, "conversation_id": 43, "require_kind": "audio"},
            {"user_id": 33, "conversation_id": 44, "require_kind": "audio"},
            {"user_id": 33, "conversation_id": 43, "require_kind": "image"},
        ):
            with pytest.raises(ValueError):
                await file_storage.finalize_pending_attachment(
                    conn,
                    pending.public_id,
                    message_id=430,
                    **mismatch,
                )

        row = await (
            await conn.execute(
                "SELECT status, message_id FROM FILE_ATTACHMENTS WHERE public_id = ?",
                (pending.public_id,),
            )
        ).fetchone()

    assert (row["status"], row["message_id"]) == ("pending", None)


@pytest.mark.asyncio
async def test_missing_ready_blob_file_fails_fast(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "file_blobs")
    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=9, conversation_id=13)

    pending = await file_storage.create_pending_text_attachment(
        user_id=9,
        conversation_id=13,
        text_content="lost bytes",
        filename="lost.txt",
    )

    async with mock_db() as conn:
        row = await (await conn.execute(
            "SELECT storage_key FROM FILE_BLOBS WHERE id = ?",
            (pending.blob_id,),
        )).fetchone()

    file_storage._path_from_storage_key(row["storage_key"]).unlink()

    with pytest.raises(RuntimeError, match="missing storage file"):
        await file_storage.create_pending_text_attachment(
            user_id=9,
            conversation_id=13,
            text_content="lost bytes",
            filename="lost-again.txt",
        )


@pytest.mark.asyncio
async def test_delete_conversation_rows_removes_attachment_refs_not_blob(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "file_blobs")
    async with mock_db() as conn:
        await ensure_conversation_privacy_schema(conn)
        await _seed_user_conversation(conn, user_id=4, conversation_id=8)

    pending = await file_storage.create_pending_text_attachment(
        user_id=4,
        conversation_id=8,
        text_content="keep blob",
        filename="keep.txt",
    )
    message_json = orjson.dumps([pending.block]).decode("utf-8")

    async with mock_db() as conn:
        await conn.execute(
            """
            INSERT INTO MESSAGES (id, conversation_id, user_id, message, type, date)
            VALUES (80, 8, 4, ?, 'user', '2026-05-06 10:00:00')
            """,
            (message_json,),
        )
        await file_storage.finalize_message_attachments(
            conn,
            message_id=80,
            conversation_id=8,
            user_id=4,
            message_json=message_json,
        )
        await delete_conversation_rows(conn, conversation_id=8, user_id=4)
        await conn.commit()

    async with mock_db() as conn:
        attachment_count = await (await conn.execute("SELECT COUNT(*) FROM FILE_ATTACHMENTS")).fetchone()
        blob_count = await (await conn.execute("SELECT COUNT(*) FROM FILE_BLOBS")).fetchone()

    assert attachment_count[0] == 0
    assert blob_count[0] == 1


@pytest.mark.asyncio
async def test_discard_pending_attachments_prunes_unreferenced_blob(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "file_blobs")
    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=5, conversation_id=9)

    pending = await file_storage.create_pending_text_attachment(
        user_id=5,
        conversation_id=9,
        text_content="will be discarded",
        filename="discard.txt",
    )

    await file_storage.discard_pending_attachments([pending.public_id], "test")

    async with mock_db() as conn:
        attachment_count = await (await conn.execute("SELECT COUNT(*) FROM FILE_ATTACHMENTS")).fetchone()
        blob_count = await (await conn.execute("SELECT COUNT(*) FROM FILE_BLOBS")).fetchone()
        variant_count = await (await conn.execute("SELECT COUNT(*) FROM FILE_BLOB_VARIANTS")).fetchone()

    assert attachment_count[0] == 0
    assert blob_count[0] == 0
    assert variant_count[0] == 0


@pytest.mark.asyncio
async def test_discard_stale_pending_attachments_prunes_after_restart_gap(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "file_blobs")
    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=6, conversation_id=10)

    pending = await file_storage.create_pending_text_attachment(
        user_id=6,
        conversation_id=10,
        text_content="stale pending",
        filename="stale.txt",
    )
    second_pending = await file_storage.create_pending_text_attachment(
        user_id=6,
        conversation_id=10,
        text_content="stale pending",
        filename="stale-again.txt",
    )
    long_running_audio = await file_storage.create_pending_audio_attachment(
        user_id=6,
        conversation_id=10,
        data=b"long-running-audio",
        filename="voice-note.ogg",
        mime_detected="audio/ogg",
    )

    async with mock_db() as conn:
        await conn.execute(
            """
            UPDATE FILE_ATTACHMENTS
            SET created_at = datetime('now', '-180 minutes')
            WHERE public_id IN (?, ?, ?)
            """,
            (pending.public_id, second_pending.public_id, long_running_audio.public_id),
        )
        await conn.commit()

    pruned = await file_storage.discard_stale_pending_attachments(max_age_minutes=120)

    async with mock_db() as conn:
        attachment_count = await (await conn.execute("SELECT COUNT(*) FROM FILE_ATTACHMENTS")).fetchone()
        blob_count = await (await conn.execute("SELECT COUNT(*) FROM FILE_BLOBS")).fetchone()

    assert pruned == 2
    assert attachment_count[0] == 1
    assert blob_count[0] == 1
