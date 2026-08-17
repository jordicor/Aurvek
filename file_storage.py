"""Content-addressed storage for user chat attachments.

The physical bytes live under data/file_blobs and are deduplicated by SHA-256.
Each upload/use creates a FILE_ATTACHMENTS row with a public opaque id that can
be stored in message JSON and exposed through authenticated endpoints.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import mimetypes
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import aiosqlite
import orjson
from PIL import Image as PilImage

import database
from log_config import logger
from storage_quota import StorageQuotaExceededError, ensure_upload_fits


PROJECT_ROOT = Path(__file__).resolve().parent


def _project_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


FILE_BLOB_ROOT = _project_path(os.getenv("FILE_BLOB_ROOT", "data/file_blobs"))
PUBLIC_ID_PREFIX = "att_"
THUMB_VARIANT = "thumb_256"
ALLOWED_VARIANTS = {None, "", "fullsize", THUMB_VARIANT}

_FILE_STORAGE_SCHEMA_VERSION = 1
_file_storage_schema_ready: set[tuple[str, int]] = set()
_file_storage_schema_lock = asyncio.Lock()
_DEFAULT_DATABASE_CONNECTION_FACTORY = database.get_db_connection


@dataclass(frozen=True)
class PendingAttachment:
    public_id: str
    block: dict[str, Any]
    blob_id: int
    kind: str
    original_filename: str
    size_bytes: int


def attachment_content_url(public_id: str, *, variant: str | None = None) -> str:
    if variant:
        return f"/api/attachments/{public_id}/content?variant={variant}"
    return f"/api/attachments/{public_id}/content"


def attachment_download_url(public_id: str) -> str:
    return f"/api/attachments/{public_id}/download"


def extract_attachment_refs_from_message(message_json: str | list | dict | None) -> list[str]:
    refs: list[str] = []
    payload = _parse_message_json(message_json)
    for block in _iter_message_blocks(payload):
        ref = _block_attachment_ref(block)
        if ref:
            refs.append(ref)
    return refs


async def ensure_file_storage_schema(conn: aiosqlite.Connection | None = None) -> None:
    if conn is None:
        configured_key = _configured_database_key()
        if configured_key is not None and configured_key in _file_storage_schema_ready:
            return
        async with database.get_db_connection() as owned_conn:
            await _ensure_file_storage_schema_once(owned_conn)
        return
    await _ensure_file_storage_schema_once(conn)


async def _resolved_database_identity(conn: aiosqlite.Connection) -> str:
    marked_identity = getattr(conn, "_aurvek_database_identity", None)
    if marked_identity:
        return str(marked_identity)
    cursor = await conn.execute("PRAGMA database_list")
    try:
        rows = await cursor.fetchall()
    finally:
        await cursor.close()
    for _, name, filename in rows:
        if name == "main":
            if filename:
                return str(Path(filename).resolve())
            return f":memory:{id(conn)}"
    return f":connection:{id(conn)}"


def _configured_database_key() -> tuple[str, int] | None:
    """Return the native factory's DB key, or None for patched/custom factories."""
    if database.get_db_connection is not _DEFAULT_DATABASE_CONNECTION_FACTORY:
        return None
    return (database.get_database_identity(), _FILE_STORAGE_SCHEMA_VERSION)


async def _ensure_file_storage_schema_once(conn: aiosqlite.Connection) -> None:
    key = (await _resolved_database_identity(conn), _FILE_STORAGE_SCHEMA_VERSION)
    if key in _file_storage_schema_ready:
        return

    async with _file_storage_schema_lock:
        if key in _file_storage_schema_ready:
            return
        had_transaction = conn.in_transaction
        await _ensure_schema_on_connection(conn)
        if had_transaction:
            # Do not commit upload/branch transactions merely to initialize
            # schema. Startup performs the durable initialization and marks it.
            return
        await conn.commit()
        _file_storage_schema_ready.add(key)


def reset_file_storage_schema_guard() -> None:
    """Reset the once-guard for fixtures that switch or recreate databases."""
    _file_storage_schema_ready.clear()


async def _enforce_upload_quota(
    conn: aiosqlite.Connection,
    *,
    user_id: int,
    blob_id: int,
    size_bytes: int,
) -> StorageQuotaExceededError | None:
    """Run the dedupe-aware upload quota gate inside the caller's transaction.

    Returns None when the ingest fits and the caller may insert the attachment.
    On a quota rejection it COMMITS the open transaction -- persisting the
    just-created, momentarily zero-referenced FILE_BLOBS row -- and returns the
    error. The blob's physical file was written non-transactionally by
    _get_or_create_blob, so rolling back here would strand that file with no row
    left to prune it. Commit now, then prune after the transaction closes
    (_drop_rejected_upload_blob): that is the only orphan-free order.
    """
    try:
        await ensure_upload_fits(conn, user_id, blob_id, size_bytes)
    except StorageQuotaExceededError as exc:
        await conn.commit()
        return exc
    return None


async def _drop_rejected_upload_blob(
    blob_id: int, quota_error: StorageQuotaExceededError
) -> None:
    """Prune a rejected upload's zero-referenced blob, then re-raise.

    Must run only AFTER the ingest transaction has committed and released its
    connection: prune_unreferenced_blobs opens its own connection with
    BEGIN IMMEDIATE and would block against a still-open writer. It deletes the
    row + file + variants iff nothing else references the blob (no-op otherwise).
    """
    await prune_unreferenced_blobs([blob_id])
    raise quota_error


async def create_pending_pdf_attachment(
    *,
    user_id: int,
    conversation_id: int,
    data: bytes,
    filename: str,
    page_count: int,
    declared_mime: str = "application/pdf",
) -> PendingAttachment:
    filename = _clean_filename(filename, "document.pdf")
    quota_error: StorageQuotaExceededError | None = None
    async with database.get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await ensure_file_storage_schema(conn)
            blob_id = await _get_or_create_blob(
                conn,
                data=data,
                kind="pdf",
                mime_detected="application/pdf",
                extension=".pdf",
                page_count=page_count,
            )
            quota_error = await _enforce_upload_quota(
                conn, user_id=user_id, blob_id=blob_id, size_bytes=len(data)
            )
            if quota_error is None:
                public_id = _new_public_id()
                await _insert_attachment(
                    conn,
                    public_id=public_id,
                    blob_id=blob_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    message_id=None,
                    attachment_type="pdf",
                    original_filename=filename,
                    display_name=filename,
                    declared_mime=declared_mime,
                    status="pending",
                )
                await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    if quota_error is not None:
        await _drop_rejected_upload_blob(blob_id, quota_error)

    block = {
        "type": "document_url",
        "document_url": {
            "attachment_ref": public_id,
            "url": attachment_download_url(public_id),
            "filename": filename,
            "pages": page_count,
            "file_hash": hashlib.sha1(data).hexdigest(),
        },
    }
    return PendingAttachment(public_id, block, blob_id, "pdf", filename, len(data))


async def create_pending_text_attachment(
    *,
    user_id: int,
    conversation_id: int,
    text_content: str,
    filename: str,
    declared_mime: str | None = None,
) -> PendingAttachment:
    filename = _clean_filename(filename, "document.txt")
    text_bytes = text_content.encode("utf-8")
    line_count = text_content.count("\n") + 1 if text_content else 0
    quota_error: StorageQuotaExceededError | None = None
    async with database.get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await ensure_file_storage_schema(conn)
            blob_id = await _get_or_create_blob(
                conn,
                data=text_bytes,
                kind="text",
                mime_detected="text/plain; charset=utf-8",
                extension=".txt",
                text_line_count=line_count,
            )
            quota_error = await _enforce_upload_quota(
                conn, user_id=user_id, blob_id=blob_id, size_bytes=len(text_bytes)
            )
            if quota_error is None:
                public_id = _new_public_id()
                await _insert_attachment(
                    conn,
                    public_id=public_id,
                    blob_id=blob_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    message_id=None,
                    attachment_type="text",
                    original_filename=filename,
                    display_name=filename,
                    declared_mime=declared_mime or "text/plain",
                    status="pending",
                )
                await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    if quota_error is not None:
        await _drop_rejected_upload_blob(blob_id, quota_error)

    block = {
        "type": "text_file",
        "text_file": {
            "attachment_ref": public_id,
            "url": attachment_download_url(public_id),
            "filename": filename,
            "lines": line_count,
        },
    }
    return PendingAttachment(public_id, block, blob_id, "text", filename, len(text_bytes))


async def create_pending_image_attachment(
    *,
    user_id: int,
    conversation_id: int,
    data: bytes,
    filename: str,
    mime_detected: str,
    declared_mime: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> PendingAttachment:
    filename = _clean_filename(filename, "image")
    extension = _extension_for_mime(mime_detected, default=".webp")
    thumb = await asyncio.to_thread(_make_image_thumbnail, data, mime_detected)
    quota_error: StorageQuotaExceededError | None = None
    async with database.get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await ensure_file_storage_schema(conn)
            blob_id = await _get_or_create_blob(
                conn,
                data=data,
                kind="image",
                mime_detected=mime_detected or "application/octet-stream",
                extension=extension,
            )
            quota_error = await _enforce_upload_quota(
                conn, user_id=user_id, blob_id=blob_id, size_bytes=len(data)
            )
            if quota_error is None:
                if thumb is not None:
                    await _get_or_create_variant(
                        conn,
                        blob_id=blob_id,
                        variant=THUMB_VARIANT,
                        data=thumb,
                        mime_detected="image/webp",
                        extension=".webp",
                        width=min(width or 256, 256) if width else None,
                        height=min(height or 256, 256) if height else None,
                    )
                public_id = _new_public_id()
                await _insert_attachment(
                    conn,
                    public_id=public_id,
                    blob_id=blob_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    message_id=None,
                    attachment_type="image",
                    original_filename=filename,
                    display_name=filename,
                    declared_mime=declared_mime or mime_detected,
                    status="pending",
                )
                await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    if quota_error is not None:
        await _drop_rejected_upload_blob(blob_id, quota_error)

    block = {
        "type": "image_url",
        "image_url": {
            "attachment_ref": public_id,
            "url": attachment_content_url(public_id, variant=THUMB_VARIANT),
            "fullsize_url": attachment_content_url(public_id),
            "filename": filename,
        },
    }
    if width is not None:
        block["image_url"]["width"] = width
    if height is not None:
        block["image_url"]["height"] = height
    return PendingAttachment(public_id, block, blob_id, "image", filename, len(data))


async def finalize_message_attachments(
    conn: aiosqlite.Connection,
    *,
    message_id: int,
    conversation_id: int,
    user_id: int,
    message_json: str | list | dict,
) -> None:
    refs = extract_attachment_refs_from_message(message_json)
    if not refs:
        return
    await ensure_file_storage_schema(conn)
    placeholders = ",".join("?" for _ in refs)
    cursor = await conn.execute(
        f"""
        SELECT public_id
        FROM FILE_ATTACHMENTS
        WHERE public_id IN ({placeholders})
          AND user_id = ?
          AND conversation_id = ?
          AND status = 'pending'
        """,
        (*refs, user_id, conversation_id),
    )
    found = {str(row[0]) for row in await cursor.fetchall()}
    missing = set(refs) - found
    if missing:
        raise ValueError(f"Attachment refs are not pending for this message: {sorted(missing)}")
    await conn.execute(
        f"""
        UPDATE FILE_ATTACHMENTS
        SET message_id = ?, status = 'active'
        WHERE public_id IN ({placeholders})
          AND user_id = ?
          AND conversation_id = ?
          AND status = 'pending'
        """,
        (message_id, *refs, user_id, conversation_id),
    )


async def discard_pending_attachments(public_ids: Iterable[str] | None, reason: str = "") -> None:
    refs = [ref for ref in (public_ids or []) if ref]
    if not refs:
        return
    blob_ids: list[int] = []
    async with database.get_db_connection() as conn:
        await ensure_file_storage_schema(conn)
        placeholders = ",".join("?" for _ in refs)
        cursor = await conn.execute(
            f"SELECT DISTINCT blob_id FROM FILE_ATTACHMENTS WHERE status = 'pending' AND public_id IN ({placeholders})",
            refs,
        )
        blob_ids = [int(row[0]) for row in await cursor.fetchall()]
        await conn.execute(
            f"DELETE FROM FILE_ATTACHMENTS WHERE status = 'pending' AND public_id IN ({placeholders})",
            refs,
        )
        await conn.commit()
    if blob_ids:
        await prune_unreferenced_blobs(blob_ids)
    if reason:
        logger.info("Discarded %d pending attachment(s): %s", len(refs), reason)


async def discard_pending_attachments_for_user(
    public_ids: Iterable[str] | None,
    *,
    user_id: int,
    conversation_id: int,
    reason: str = "",
) -> int:
    refs = [ref for ref in (public_ids or []) if ref]
    if not refs:
        return 0
    blob_ids: list[int] = []
    discarded = 0
    async with database.get_db_connection() as conn:
        await ensure_file_storage_schema(conn)
        placeholders = ",".join("?" for _ in refs)
        params = (*refs, user_id, conversation_id)
        cursor = await conn.execute(
            f"""
            SELECT DISTINCT blob_id
            FROM FILE_ATTACHMENTS
            WHERE status = 'pending'
              AND public_id IN ({placeholders})
              AND user_id = ?
              AND conversation_id = ?
            """,
            params,
        )
        blob_ids = [int(row[0]) for row in await cursor.fetchall()]
        cursor = await conn.execute(
            f"""
            DELETE FROM FILE_ATTACHMENTS
            WHERE status = 'pending'
              AND public_id IN ({placeholders})
              AND user_id = ?
              AND conversation_id = ?
            """,
            params,
        )
        discarded = int(cursor.rowcount or 0)
        await conn.commit()
    if blob_ids:
        await prune_unreferenced_blobs(blob_ids)
    if reason and discarded:
        logger.info("Discarded %d pending attachment(s): %s", discarded, reason)
    return discarded


async def discard_stale_pending_attachments(max_age_minutes: int = 120) -> int:
    """Delete old pending attachment refs left behind by interrupted uploads."""
    blob_ids: list[int] = []
    discarded_count = 0
    async with database.get_db_connection() as conn:
        await ensure_file_storage_schema(conn)
        cursor = await conn.execute(
            """
            SELECT blob_id
            FROM FILE_ATTACHMENTS
            WHERE status = 'pending'
              AND created_at < datetime('now', ?)
            """,
            (f"-{int(max_age_minutes)} minutes",),
        )
        rows = await cursor.fetchall()
        discarded_count = len(rows)
        blob_ids = sorted({int(row[0]) for row in rows})
        await conn.execute(
            """
            DELETE FROM FILE_ATTACHMENTS
            WHERE status = 'pending'
              AND created_at < datetime('now', ?)
            """,
            (f"-{int(max_age_minutes)} minutes",),
        )
        await conn.commit()
    if blob_ids:
        await prune_unreferenced_blobs(blob_ids)
    return discarded_count


async def prune_unreferenced_blobs(blob_ids: Iterable[int] | None = None) -> int:
    """Delete zero-reference blobs and variants from DB, then remove their files."""
    ids = sorted({int(blob_id) for blob_id in (blob_ids or []) if blob_id})
    storage_keys: list[str] = []
    deleted = 0

    async with database.get_db_connection() as conn:
        await ensure_file_storage_schema(conn)
        await conn.commit()
        await conn.execute("BEGIN IMMEDIATE")
        try:
            if ids:
                placeholders = ",".join("?" for _ in ids)
                cursor = await conn.execute(
                    f"""
                    SELECT fb.id, fb.storage_key
                    FROM FILE_BLOBS fb
                    LEFT JOIN FILE_ATTACHMENTS fa ON fa.blob_id = fb.id
                    WHERE fb.id IN ({placeholders})
                    GROUP BY fb.id
                    HAVING COUNT(fa.id) = 0
                    """,
                    ids,
                )
            else:
                cursor = await conn.execute(
                    """
                    SELECT fb.id, fb.storage_key
                    FROM FILE_BLOBS fb
                    LEFT JOIN FILE_ATTACHMENTS fa ON fa.blob_id = fb.id
                    GROUP BY fb.id
                    HAVING COUNT(fa.id) = 0
                    """
                )
            rows = await cursor.fetchall()
            zero_ref_ids = [int(row["id"]) for row in rows]
            storage_keys.extend(str(row["storage_key"]) for row in rows)

            if zero_ref_ids:
                placeholders = ",".join("?" for _ in zero_ref_ids)
                variant_cursor = await conn.execute(
                    f"SELECT storage_key FROM FILE_BLOB_VARIANTS WHERE blob_id IN ({placeholders})",
                    zero_ref_ids,
                )
                storage_keys.extend(str(row["storage_key"]) for row in await variant_cursor.fetchall())
                await conn.execute(
                    f"DELETE FROM FILE_BLOB_VARIANTS WHERE blob_id IN ({placeholders})",
                    zero_ref_ids,
                )
                await conn.execute(
                    f"DELETE FROM FILE_BLOBS WHERE id IN ({placeholders})",
                    zero_ref_ids,
                )
                deleted = len(zero_ref_ids)

            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    for storage_key in storage_keys:
        try:
            path = _path_from_storage_key(storage_key)
            if path.exists():
                await asyncio.to_thread(path.unlink)
        except Exception:
            logger.warning("Could not delete unreferenced blob file %s", storage_key, exc_info=True)

    return deleted


async def resolve_attachment_for_user(
    conn: aiosqlite.Connection,
    *,
    public_id: str,
    user_id: int | None = None,
    conversation_id: int | None = None,
    message_id: int | None = None,
    require_kind: str | None = None,
    allow_admin: bool = False,
) -> dict[str, Any] | None:
    clauses = ["fa.public_id = ?", "fa.status = 'active'", "fb.status = 'ready'"]
    params: list[Any] = [public_id]
    if require_kind:
        clauses.append("fa.attachment_type = ?")
        params.append(require_kind)
    if not allow_admin:
        if user_id is None:
            return None
        clauses.append("fa.user_id = ?")
        params.append(user_id)
    if conversation_id is not None:
        clauses.append("fa.conversation_id = ?")
        params.append(conversation_id)
    if message_id is not None:
        clauses.append("fa.message_id = ?")
        params.append(message_id)

    cursor = await conn.execute(
        f"""
        SELECT fa.*, fb.sha256, fb.size_bytes AS blob_size_bytes, fb.kind,
               fb.mime_detected, fb.storage_key, fb.page_count, fb.text_line_count
        FROM FILE_ATTACHMENTS fa
        JOIN FILE_BLOBS fb ON fb.id = fa.blob_id
        WHERE {" AND ".join(clauses)}
        """,
        params,
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def resolve_pending_attachment_for_user(
    conn: aiosqlite.Connection,
    *,
    public_id: str,
    user_id: int,
    conversation_id: int,
    require_kind: str | None = None,
) -> dict[str, Any] | None:
    clauses = [
        "fa.public_id = ?",
        "fa.status = 'pending'",
        "fb.status = 'ready'",
        "fa.user_id = ?",
        "fa.conversation_id = ?",
    ]
    params: list[Any] = [public_id, user_id, conversation_id]
    if require_kind:
        clauses.append("fa.attachment_type = ?")
        params.append(require_kind)

    cursor = await conn.execute(
        f"""
        SELECT fa.*, fb.sha256, fb.size_bytes AS blob_size_bytes, fb.kind,
               fb.mime_detected, fb.storage_key, fb.page_count, fb.text_line_count
        FROM FILE_ATTACHMENTS fa
        JOIN FILE_BLOBS fb ON fb.id = fa.blob_id
        WHERE {" AND ".join(clauses)}
        """,
        params,
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def read_pending_attachment_bytes(
    public_id: str,
    *,
    user_id: int,
    conversation_id: int,
    require_kind: str | None = None,
) -> tuple[bytes, dict[str, Any]] | None:
    async with database.get_db_connection(readonly=True) as conn:
        attachment = await resolve_pending_attachment_for_user(
            conn,
            public_id=public_id,
            user_id=user_id,
            conversation_id=conversation_id,
            require_kind=require_kind,
        )
        if not attachment:
            return None
        path = await _attachment_storage_path(conn, attachment, variant=None)
    if not path.exists():
        return None
    data = await asyncio.to_thread(path.read_bytes)
    return data, attachment


def attachment_record_to_block(
    attachment: dict[str, Any],
    *,
    data: bytes | None = None,
) -> dict[str, Any]:
    public_id = str(attachment["public_id"])
    filename = str(
        attachment.get("display_name")
        or attachment.get("original_filename")
        or "attachment"
    )
    kind = str(attachment.get("attachment_type") or attachment.get("kind") or "")

    if kind == "pdf":
        doc_info: dict[str, Any] = {
            "attachment_ref": public_id,
            "url": attachment_download_url(public_id),
            "filename": filename,
            "pages": int(attachment.get("page_count") or 0),
        }
        if data is not None:
            doc_info["file_hash"] = hashlib.sha1(data).hexdigest()
        return {"type": "document_url", "document_url": doc_info}

    if kind == "text":
        return {
            "type": "text_file",
            "text_file": {
                "attachment_ref": public_id,
                "url": attachment_download_url(public_id),
                "filename": filename,
                "lines": int(attachment.get("text_line_count") or 0),
            },
        }

    if kind == "image":
        return {
            "type": "image_url",
            "image_url": {
                "attachment_ref": public_id,
                "url": attachment_content_url(public_id, variant=THUMB_VARIANT),
                "fullsize_url": attachment_content_url(public_id),
                "filename": filename,
            },
        }

    raise ValueError(f"Unsupported attachment kind: {kind}")


async def get_attachment_path_for_user(
    conn: aiosqlite.Connection,
    *,
    public_id: str,
    user_id: int | None,
    variant: str | None = None,
    require_kind: str | None = None,
    allow_admin: bool = False,
) -> tuple[Path, dict[str, Any]] | None:
    variant = _normalize_variant(variant)
    attachment = await resolve_attachment_for_user(
        conn,
        public_id=public_id,
        user_id=user_id,
        require_kind=require_kind,
        allow_admin=allow_admin,
    )
    if not attachment:
        return None
    path = await _attachment_storage_path(conn, attachment, variant=variant)
    if not path.exists():
        return None
    return path, attachment


async def read_attachment_bytes(
    public_id: str,
    *,
    user_id: int | None = None,
    conversation_id: int | None = None,
    message_id: int | None = None,
    require_kind: str | None = None,
    allow_admin: bool = False,
    variant: str | None = None,
) -> tuple[bytes, dict[str, Any]] | None:
    variant = _normalize_variant(variant)
    async with database.get_db_connection(readonly=True) as conn:
        attachment = await resolve_attachment_for_user(
            conn,
            public_id=public_id,
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            require_kind=require_kind,
            allow_admin=allow_admin,
        )
        if not attachment:
            return None
        path = await _attachment_storage_path(conn, attachment, variant=variant)
    if not path.exists():
        return None
    data = await asyncio.to_thread(path.read_bytes)
    return data, attachment


def read_attachment_bytes_sync(
    public_id: str,
    *,
    user_id: int | None = None,
    conversation_id: int | None = None,
    message_id: int | None = None,
    require_kind: str | None = None,
    allow_admin: bool = False,
    variant: str | None = None,
) -> tuple[bytes, dict[str, Any]] | None:
    variant = _normalize_variant(variant)
    with _sqlite_connection_sync() as conn:
        attachment = _resolve_attachment_sync(
            conn,
            public_id=public_id,
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            require_kind=require_kind,
            allow_admin=allow_admin,
        )
        if not attachment:
            return None
        path = _attachment_storage_path_sync(conn, attachment, variant=variant)
    if not path.exists():
        return None
    return path.read_bytes(), attachment


def get_attachment_local_path_sync(
    public_id: str,
    *,
    user_id: int | None = None,
    conversation_id: int | None = None,
    message_id: int | None = None,
    require_kind: str | None = None,
    allow_admin: bool = False,
    variant: str | None = None,
) -> Path | None:
    variant = _normalize_variant(variant)
    with _sqlite_connection_sync() as conn:
        attachment = _resolve_attachment_sync(
            conn,
            public_id=public_id,
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            require_kind=require_kind,
            allow_admin=allow_admin,
        )
        if not attachment:
            return None
        path = _attachment_storage_path_sync(conn, attachment, variant=variant)
    return path if path.exists() else None


def text_file_block_to_text(
    block: dict[str, Any],
    *,
    user_id: int | None = None,
    conversation_id: int | None = None,
    message_id: int | None = None,
    allow_admin: bool = False,
) -> str:
    text_info = block.get("text_file", {}) if isinstance(block, dict) else {}
    ref = text_info.get("attachment_ref")
    if not ref:
        return ""
    result = read_attachment_bytes_sync(
        ref,
        user_id=user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        require_kind="text",
        allow_admin=allow_admin,
    )
    if not result:
        return ""
    data, _ = result
    return data.decode("utf-8", errors="replace")


async def delete_attachments_for_conversation(
    conn: aiosqlite.Connection,
    *,
    conversation_id: int,
) -> None:
    await ensure_file_storage_schema(conn)
    await conn.execute(
        "DELETE FROM FILE_ATTACHMENTS WHERE conversation_id = ?",
        (conversation_id,),
    )


async def delete_attachment_and_rewrite_message(
    conn: aiosqlite.Connection,
    *,
    public_id: str,
    user_id: int,
    allow_admin: bool = False,
) -> bool:
    await ensure_file_storage_schema(conn)
    attachment = await resolve_attachment_for_user(
        conn,
        public_id=public_id,
        user_id=user_id,
        allow_admin=allow_admin,
    )
    if not attachment:
        return False
    message_id = attachment.get("message_id")
    if message_id is None:
        await conn.execute("DELETE FROM FILE_ATTACHMENTS WHERE public_id = ?", (public_id,))
        return True

    cursor = await conn.execute(
        "SELECT message FROM MESSAGES WHERE id = ?",
        (message_id,),
    )
    row = await cursor.fetchone()
    if not row:
        await conn.execute("DELETE FROM FILE_ATTACHMENTS WHERE public_id = ?", (public_id,))
        return True

    replacement = remove_attachment_ref_from_message(row[0], public_id)
    await conn.execute(
        "UPDATE MESSAGES SET message = ? WHERE id = ?",
        (replacement, message_id),
    )
    await conn.execute("DELETE FROM FILE_ATTACHMENTS WHERE public_id = ?", (public_id,))
    return True


async def clone_attachments_for_branch(
    conn: aiosqlite.Connection,
    *,
    old_message_id: int,
    new_message_id: int,
    new_conversation_id: int,
    user_id: int,
    message_json: str,
) -> str:
    payload = _parse_message_json(message_json)
    if not isinstance(payload, list):
        return message_json

    changed = False
    for index, block in enumerate(payload):
        ref = _block_attachment_ref(block)
        if not ref:
            continue
        source = await resolve_attachment_for_user(
            conn,
            public_id=ref,
            user_id=user_id,
            message_id=old_message_id,
        )
        if not source:
            continue
        new_ref = _new_public_id()
        await _insert_attachment(
            conn,
            public_id=new_ref,
            blob_id=int(source["blob_id"]),
            user_id=user_id,
            conversation_id=new_conversation_id,
            message_id=new_message_id,
            attachment_type=str(source["attachment_type"]),
            original_filename=str(source["original_filename"]),
            display_name=source.get("display_name"),
            declared_mime=source.get("declared_mime"),
            legacy_url=source.get("legacy_url"),
            legacy_block_index=index,
            status="active",
        )
        _set_block_attachment_ref(block, new_ref)
        changed = True

    if not changed:
        return message_json
    return orjson.dumps(payload).decode("utf-8")


def remove_attachment_ref_from_message(message_json: str | list | dict, public_id: str) -> str:
    payload = _parse_message_json(message_json)
    if isinstance(payload, list):
        remaining = [
            block for block in payload
            if not (isinstance(block, dict) and _block_attachment_ref(block) == public_id)
        ]
        if remaining:
            return orjson.dumps(remaining).decode("utf-8")
        return "[attachment deleted]"
    if isinstance(payload, dict) and _block_attachment_ref(payload) == public_id:
        return "[attachment deleted]"
    if isinstance(message_json, str):
        return message_json
    return orjson.dumps(payload).decode("utf-8")


async def create_active_attachment_from_bytes(
    conn: aiosqlite.Connection,
    *,
    data: bytes,
    kind: str,
    user_id: int,
    conversation_id: int,
    message_id: int,
    original_filename: str,
    mime_detected: str,
    declared_mime: str | None = None,
    page_count: int | None = None,
    text_line_count: int | None = None,
    legacy_url: str | None = None,
    legacy_path: str | None = None,
    legacy_block_index: int | None = None,
) -> tuple[str, int]:
    await ensure_file_storage_schema(conn)
    extension = _extension_for_mime(mime_detected, default=_default_extension_for_kind(kind))
    blob_id = await _get_or_create_blob(
        conn,
        data=data,
        kind=kind,
        mime_detected=mime_detected,
        extension=extension,
        page_count=page_count,
        text_line_count=text_line_count,
    )
    if kind == "image":
        thumb = await asyncio.to_thread(_make_image_thumbnail, data, mime_detected)
        if thumb is not None:
            await _get_or_create_variant(
                conn,
                blob_id=blob_id,
                variant=THUMB_VARIANT,
                data=thumb,
                mime_detected="image/webp",
                extension=".webp",
            )
    public_id = _new_public_id()
    await _insert_attachment(
        conn,
        public_id=public_id,
        blob_id=blob_id,
        user_id=user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        attachment_type=kind,
        original_filename=_clean_filename(original_filename, f"attachment{extension}"),
        display_name=_clean_filename(original_filename, f"attachment{extension}"),
        declared_mime=declared_mime or mime_detected,
        legacy_url=legacy_url,
        legacy_block_index=legacy_block_index,
        status="active",
    )
    if legacy_url:
        await _insert_legacy_cleanup_candidate(
            conn,
            legacy_url=legacy_url,
            legacy_path=legacy_path or "",
            data=data,
            kind=kind,
            migrated_blob_sha256=hashlib.sha256(data).hexdigest(),
            migrated_blob_size_bytes=len(data),
            source_message_id=message_id,
            source_attachment_public_id=public_id,
        )
    return public_id, blob_id


async def _ensure_schema_on_connection(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS FILE_BLOBS (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('image', 'pdf', 'text')),
            mime_detected TEXT NOT NULL,
            storage_key TEXT NOT NULL UNIQUE,
            page_count INTEGER,
            text_line_count INTEGER,
            text_extract_key TEXT,
            status TEXT NOT NULL DEFAULT 'ready' CHECK(status IN ('pending', 'ready', 'gc_pending', 'quarantined')),
            quarantine_reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS FILE_BLOB_VARIANTS (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            blob_id INTEGER NOT NULL,
            variant TEXT NOT NULL,
            storage_key TEXT NOT NULL UNIQUE,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            mime_detected TEXT NOT NULL,
            width INTEGER,
            height INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(blob_id) REFERENCES FILE_BLOBS(id) ON DELETE CASCADE,
            UNIQUE(blob_id, variant)
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS FILE_ATTACHMENTS (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT NOT NULL UNIQUE,
            blob_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            conversation_id INTEGER NOT NULL,
            message_id INTEGER,
            attachment_type TEXT NOT NULL CHECK(attachment_type IN ('image', 'pdf', 'text')),
            original_filename TEXT NOT NULL,
            display_name TEXT,
            declared_mime TEXT,
            legacy_url TEXT,
            legacy_block_index INTEGER,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'active')),
            upload_token TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(blob_id) REFERENCES FILE_BLOBS(id),
            FOREIGN KEY(user_id) REFERENCES USERS(id) ON DELETE CASCADE,
            FOREIGN KEY(conversation_id) REFERENCES CONVERSATIONS(id) ON DELETE CASCADE,
            FOREIGN KEY(message_id) REFERENCES MESSAGES(id) ON DELETE CASCADE,
            UNIQUE(message_id, legacy_block_index)
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS FILE_LEGACY_CLEANUP_CANDIDATES (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            legacy_url TEXT NOT NULL UNIQUE,
            legacy_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('image', 'pdf', 'text')),
            migrated_blob_sha256 TEXT NOT NULL,
            migrated_blob_size_bytes INTEGER NOT NULL,
            source_message_id INTEGER,
            source_attachment_public_id TEXT,
            cleanup_status TEXT NOT NULL DEFAULT 'pending' CHECK(cleanup_status IN ('pending', 'deleted', 'skipped')),
            cleanup_note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            cleaned_at TIMESTAMP
        )
        """
    )
    await conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_file_blobs_ready_identity
        ON FILE_BLOBS(sha256, size_bytes, kind)
        WHERE status IN ('pending', 'ready', 'gc_pending')
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_file_blobs_hash ON FILE_BLOBS(sha256, size_bytes, kind)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_file_attachments_user ON FILE_ATTACHMENTS(user_id, created_at DESC)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_file_attachments_conversation ON FILE_ATTACHMENTS(conversation_id, id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_file_attachments_message ON FILE_ATTACHMENTS(message_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_file_attachments_blob ON FILE_ATTACHMENTS(blob_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_file_attachments_user_blob ON FILE_ATTACHMENTS(user_id, blob_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_file_attachments_legacy_url ON FILE_ATTACHMENTS(legacy_url)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_file_attachments_pending ON FILE_ATTACHMENTS(status, created_at)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_legacy_cleanup_status ON FILE_LEGACY_CLEANUP_CANDIDATES(cleanup_status, created_at)")


async def _get_or_create_blob(
    conn: aiosqlite.Connection,
    *,
    data: bytes,
    kind: str,
    mime_detected: str,
    extension: str,
    page_count: int | None = None,
    text_line_count: int | None = None,
) -> int:
    sha256 = hashlib.sha256(data).hexdigest()
    size_bytes = len(data)
    for _ in range(5):
        cursor = await conn.execute(
            """
            SELECT id, storage_key, status
            FROM FILE_BLOBS
            WHERE sha256 = ? AND size_bytes = ? AND kind = ?
              AND status IN ('pending', 'ready', 'gc_pending')
            ORDER BY id ASC
            LIMIT 1
            """,
            (sha256, size_bytes, kind),
        )
        row = await cursor.fetchone()
        if row:
            status = row["status"]
            if status in {"ready", "gc_pending"}:
                path = _path_from_storage_key(row["storage_key"])
                if path.exists():
                    if status == "gc_pending":
                        await conn.execute(
                            "UPDATE FILE_BLOBS SET status = 'ready', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (int(row["id"]),),
                        )
                    return int(row["id"])
                await conn.execute(
                    "UPDATE FILE_BLOBS SET status = 'quarantined', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (int(row["id"]),),
                )
                raise RuntimeError(f"Ready blob {row['id']} is missing storage file: {path}")
            await asyncio.sleep(0.05)
            continue

        storage_key = _new_storage_key("sha256", sha256, extension)
        try:
            cursor = await conn.execute(
                """
                INSERT INTO FILE_BLOBS
                    (sha256, size_bytes, kind, mime_detected, storage_key,
                     page_count, text_line_count, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                RETURNING id
                """,
                (sha256, size_bytes, kind, mime_detected, storage_key, page_count, text_line_count),
            )
            blob_id = int((await cursor.fetchone())[0])
            await asyncio.to_thread(_write_file_atomic, _path_from_storage_key(storage_key), data)
            await conn.execute(
                "UPDATE FILE_BLOBS SET status = 'ready', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (blob_id,),
            )
            return blob_id
        except sqlite3.IntegrityError:
            await asyncio.sleep(0.05)
            continue
    raise RuntimeError("Could not create or reuse file blob")


async def _get_or_create_variant(
    conn: aiosqlite.Connection,
    *,
    blob_id: int,
    variant: str,
    data: bytes,
    mime_detected: str,
    extension: str,
    width: int | None = None,
    height: int | None = None,
) -> int:
    cursor = await conn.execute(
        "SELECT id, storage_key FROM FILE_BLOB_VARIANTS WHERE blob_id = ? AND variant = ?",
        (blob_id, variant),
    )
    row = await cursor.fetchone()
    if row:
        path = _path_from_storage_key(row["storage_key"])
        if not path.exists():
            raise RuntimeError(f"Blob variant {row['id']} is missing storage file: {path}")
        return int(row["id"])

    sha256 = hashlib.sha256(data).hexdigest()
    storage_key = _new_storage_key("variants", f"{sha256}_{variant}", extension)
    try:
        cursor = await conn.execute(
            """
            INSERT INTO FILE_BLOB_VARIANTS
                (blob_id, variant, storage_key, sha256, size_bytes, mime_detected, width, height)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (blob_id, variant, storage_key, sha256, len(data), mime_detected, width, height),
        )
        variant_id = int((await cursor.fetchone())[0])
        await asyncio.to_thread(_write_file_atomic, _path_from_storage_key(storage_key), data)
        return variant_id
    except sqlite3.IntegrityError:
        cursor = await conn.execute(
            "SELECT id, storage_key FROM FILE_BLOB_VARIANTS WHERE blob_id = ? AND variant = ?",
            (blob_id, variant),
        )
        row = await cursor.fetchone()
        if row:
            path = _path_from_storage_key(row["storage_key"])
            if not path.exists():
                raise RuntimeError(f"Blob variant {row['id']} is missing storage file: {path}")
            return int(row[0])
        raise


async def _insert_attachment(
    conn: aiosqlite.Connection,
    *,
    public_id: str,
    blob_id: int,
    user_id: int,
    conversation_id: int,
    message_id: int | None,
    attachment_type: str,
    original_filename: str,
    display_name: str | None,
    declared_mime: str | None,
    legacy_url: str | None = None,
    legacy_block_index: int | None = None,
    status: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO FILE_ATTACHMENTS
            (public_id, blob_id, user_id, conversation_id, message_id,
             attachment_type, original_filename, display_name, declared_mime,
             legacy_url, legacy_block_index, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            public_id,
            blob_id,
            user_id,
            conversation_id,
            message_id,
            attachment_type,
            original_filename,
            display_name,
            declared_mime,
            legacy_url,
            legacy_block_index,
            status,
        ),
    )


async def _insert_legacy_cleanup_candidate(
    conn: aiosqlite.Connection,
    *,
    legacy_url: str,
    legacy_path: str,
    data: bytes,
    kind: str,
    migrated_blob_sha256: str,
    migrated_blob_size_bytes: int,
    source_message_id: int,
    source_attachment_public_id: str,
) -> None:
    sha256 = hashlib.sha256(data).hexdigest()
    await conn.execute(
        """
        INSERT OR IGNORE INTO FILE_LEGACY_CLEANUP_CANDIDATES
            (legacy_url, legacy_path, sha256, size_bytes, kind,
             migrated_blob_sha256, migrated_blob_size_bytes,
             source_message_id, source_attachment_public_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            legacy_url,
            legacy_path,
            sha256,
            len(data),
            kind,
            migrated_blob_sha256,
            migrated_blob_size_bytes,
            source_message_id,
            source_attachment_public_id,
        ),
    )


async def _attachment_storage_path(
    conn: aiosqlite.Connection,
    attachment: dict[str, Any],
    *,
    variant: str | None,
) -> Path:
    if variant == THUMB_VARIANT:
        cursor = await conn.execute(
            "SELECT storage_key FROM FILE_BLOB_VARIANTS WHERE blob_id = ? AND variant = ?",
            (attachment["blob_id"], THUMB_VARIANT),
        )
        row = await cursor.fetchone()
        if row:
            return _path_from_storage_key(row["storage_key"])
    return _path_from_storage_key(str(attachment["storage_key"]))


def _attachment_storage_path_sync(
    conn: sqlite3.Connection,
    attachment: dict[str, Any],
    *,
    variant: str | None,
) -> Path:
    if variant == THUMB_VARIANT:
        row = conn.execute(
            "SELECT storage_key FROM FILE_BLOB_VARIANTS WHERE blob_id = ? AND variant = ?",
            (attachment["blob_id"], THUMB_VARIANT),
        ).fetchone()
        if row:
            return _path_from_storage_key(row["storage_key"])
    return _path_from_storage_key(str(attachment["storage_key"]))


def _resolve_attachment_sync(
    conn: sqlite3.Connection,
    *,
    public_id: str,
    user_id: int | None,
    conversation_id: int | None,
    message_id: int | None,
    require_kind: str | None,
    allow_admin: bool,
) -> dict[str, Any] | None:
    clauses = ["fa.public_id = ?", "fa.status = 'active'", "fb.status = 'ready'"]
    params: list[Any] = [public_id]
    if require_kind:
        clauses.append("fa.attachment_type = ?")
        params.append(require_kind)
    if not allow_admin:
        if user_id is None:
            return None
        clauses.append("fa.user_id = ?")
        params.append(user_id)
    if conversation_id is not None:
        clauses.append("fa.conversation_id = ?")
        params.append(conversation_id)
    if message_id is not None:
        clauses.append("fa.message_id = ?")
        params.append(message_id)
    row = conn.execute(
        f"""
        SELECT fa.*, fb.sha256, fb.size_bytes AS blob_size_bytes, fb.kind,
               fb.mime_detected, fb.storage_key, fb.page_count, fb.text_line_count
        FROM FILE_ATTACHMENTS fa
        JOIN FILE_BLOBS fb ON fb.id = fa.blob_id
        WHERE {" AND ".join(clauses)}
        """,
        params,
    ).fetchone()
    return dict(row) if row else None


def _sqlite_connection_sync() -> sqlite3.Connection:
    db_name = database.dbname
    if not db_name:
        raise RuntimeError("DATABASE is not configured")
    db_path = Path(db_name)
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / "db" / db_path
    conn = sqlite3.connect(
        f"{db_path.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=database.DEFAULT_DB_TIMEOUT,
    )
    conn.row_factory = sqlite3.Row
    for statement in database.PRAGMA_STATEMENTS_RO:
        conn.execute(statement)
    return conn


def _parse_message_json(message_json: str | list | dict | None) -> Any:
    if isinstance(message_json, (list, dict)):
        return message_json
    if isinstance(message_json, str):
        stripped = message_json.strip()
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                return orjson.loads(stripped)
            except orjson.JSONDecodeError:
                return message_json
    return message_json


def _iter_message_blocks(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, list):
        for block in payload:
            if isinstance(block, dict):
                yield block
    elif isinstance(payload, dict):
        yield payload


def _block_attachment_ref(block: dict[str, Any]) -> str | None:
    if block.get("type") == "image_url":
        return block.get("image_url", {}).get("attachment_ref")
    if block.get("type") == "document_url":
        return block.get("document_url", {}).get("attachment_ref")
    if block.get("type") == "text_file":
        return block.get("text_file", {}).get("attachment_ref")
    return None


def _set_block_attachment_ref(block: dict[str, Any], public_id: str) -> None:
    if block.get("type") == "image_url":
        image_info = block.setdefault("image_url", {})
        image_info["attachment_ref"] = public_id
        image_info["url"] = attachment_content_url(public_id, variant=THUMB_VARIANT)
        image_info["fullsize_url"] = attachment_content_url(public_id)
    elif block.get("type") == "document_url":
        doc_info = block.setdefault("document_url", {})
        doc_info["attachment_ref"] = public_id
        doc_info["url"] = attachment_download_url(public_id)
    elif block.get("type") == "text_file":
        text_info = block.setdefault("text_file", {})
        text_info["attachment_ref"] = public_id
        text_info["url"] = attachment_download_url(public_id)


def _normalize_variant(variant: str | None) -> str | None:
    if variant in (None, "", "fullsize"):
        return None
    if variant not in ALLOWED_VARIANTS:
        raise ValueError("Invalid attachment variant")
    return variant


def _new_public_id() -> str:
    return f"{PUBLIC_ID_PREFIX}{secrets.token_urlsafe(18)}"


def _clean_filename(filename: str | None, default: str) -> str:
    name = os.path.basename(filename or default).strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\r\n\t")
    if not name:
        name = default
    return name[:180]


def _extension_for_mime(mime_type: str | None, *, default: str) -> str:
    if not mime_type:
        return default
    if mime_type == "image/jpeg":
        return ".jpg"
    if mime_type == "text/plain; charset=utf-8":
        return ".txt"
    ext = mimetypes.guess_extension(mime_type.split(";", 1)[0].strip())
    if ext:
        return ext
    return default


def _default_extension_for_kind(kind: str) -> str:
    if kind == "pdf":
        return ".pdf"
    if kind == "text":
        return ".txt"
    if kind == "image":
        return ".webp"
    return ".bin"


def _new_storage_key(prefix: str, name: str, extension: str) -> str:
    safe_ext = extension if extension.startswith(".") else f".{extension}"
    first = name[:2]
    second = name[2:4]
    base = f"{prefix}/{first}/{second}/{name}{safe_ext}"
    candidate = base
    counter = 1
    while _path_from_storage_key(candidate).exists():
        stem = f"{name}_{counter}"
        candidate = f"{prefix}/{first}/{second}/{stem}{safe_ext}"
        counter += 1
    return candidate


def _path_from_storage_key(storage_key: str) -> Path:
    root = FILE_BLOB_ROOT.resolve()
    path = (root / storage_key).resolve()
    if root not in path.parents and path != root:
        raise ValueError("Invalid storage key")
    return path


def _write_file_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _make_image_thumbnail(data: bytes, mime_detected: str | None) -> bytes | None:
    try:
        image = PilImage.open(io.BytesIO(data))
        image.load()
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA") if image.mode in ("PA", "LA") else image.convert("RGB")
        image.thumbnail((256, 256), PilImage.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="WEBP", quality=85)
        return out.getvalue()
    except Exception as exc:
        logger.warning("Could not generate attachment thumbnail: %s", exc)
        return None


def image_block_to_provider_block(
    *,
    data: bytes,
    mime_type: str,
    machine: str,
    force_base64: bool = False,
) -> dict[str, Any] | None:
    if machine == "xAI" and mime_type == "image/webp":
        try:
            image = PilImage.open(io.BytesIO(data))
            if image.mode in ("RGBA", "P"):
                image = image.convert("RGB")
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=85)
            data = out.getvalue()
            mime_type = "image/jpeg"
        except Exception as exc:
            logger.warning("Could not convert attachment image for xAI: %s", exc)
            return None
    b64 = base64.b64encode(data).decode("utf-8")
    if machine == "Claude":
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime_type,
                "data": b64,
            },
        }
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{b64}"},
    }
