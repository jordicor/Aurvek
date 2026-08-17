import asyncio
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

import orjson
from fastapi.responses import JSONResponse

from common import (
    MAX_API_IMAGE_SIZE_MB,
    MAX_PDF_SIZE_MB,
    MAX_RAW_UPLOAD_SIZE_MB,
    MAX_TEXT_FILE_SIZE_MB,
)
from database import get_db_connection
from file_storage import (
    create_pending_image_attachment,
    create_pending_pdf_attachment,
    create_pending_text_attachment,
    discard_pending_attachments_for_user,
    read_pending_attachment_bytes,
)
from log_config import logger
from models import User
from save_pdfs import validate_pdf

from chat.services.file_inputs import (
    decode_text_file,
    is_text_file,
    validate_and_compress_image,
)


ATTACHMENT_UPLOAD_CHUNK_SIZE_MB = max(1, int(os.getenv("ATTACHMENT_UPLOAD_CHUNK_SIZE_MB", "2")))
ATTACHMENT_UPLOAD_CHUNK_SIZE_BYTES = ATTACHMENT_UPLOAD_CHUNK_SIZE_MB * 1024 * 1024
ATTACHMENT_UPLOAD_LEGACY_CHUNK_SIZE_BYTES = 4 * 1024 * 1024
# Fixed (NOT env-overridable) chunk-size bounds; must stay in lockstep with the
# frontend's UPLOAD_MIN_CHUNK / UPLOAD_MAX_CHUNK to avoid server/client desync.
ATTACHMENT_UPLOAD_MIN_CHUNK_SIZE_BYTES = 256 * 1024
ATTACHMENT_UPLOAD_MAX_CHUNK_SIZE_BYTES = 8 * 1024 * 1024
ATTACHMENT_UPLOAD_MAX_CHUNKS = max(1, int(os.getenv("ATTACHMENT_UPLOAD_MAX_CHUNKS", "128")))
ATTACHMENT_UPLOAD_TTL_SECONDS = max(60, int(os.getenv("ATTACHMENT_UPLOAD_TTL_SECONDS", str(2 * 60 * 60))))
ATTACHMENT_UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
_ATTACHMENT_UPLOAD_CHUNK_ROOT_RAW = Path(os.getenv("ATTACHMENT_UPLOAD_CHUNK_ROOT", "data/upload_chunks"))
ATTACHMENT_UPLOAD_CHUNK_ROOT = (
    _ATTACHMENT_UPLOAD_CHUNK_ROOT_RAW
    if _ATTACHMENT_UPLOAD_CHUNK_ROOT_RAW.is_absolute()
    else Path(__file__).resolve().parents[2] / _ATTACHMENT_UPLOAD_CHUNK_ROOT_RAW
)


def _assert_chunk_bounds_consistent(max_type_cap_bytes: int, min_chunk_bytes: int, max_chunks: int) -> None:
    """Fail fast at import if the smallest allowed chunk size cannot cover the
    largest allowed upload within ATTACHMENT_UPLOAD_MAX_CHUNKS. Integer ceil, no math import."""
    needed = (max_type_cap_bytes + min_chunk_bytes - 1) // min_chunk_bytes
    if needed > max_chunks:
        raise RuntimeError(
            "Attachment upload bounds are inconsistent: a "
            f"{max_type_cap_bytes // (1024 * 1024)}MB upload at the minimum "
            f"{min_chunk_bytes // 1024}KB chunk size needs {needed} chunks, "
            f"exceeding ATTACHMENT_UPLOAD_MAX_CHUNKS={max_chunks}. "
            "Raise ATTACHMENT_UPLOAD_MAX_CHUNKS or lower the per-type size caps."
        )


_assert_chunk_bounds_consistent(
    max(MAX_PDF_SIZE_MB, MAX_RAW_UPLOAD_SIZE_MB, MAX_TEXT_FILE_SIZE_MB) * 1024 * 1024,
    ATTACHMENT_UPLOAD_MIN_CHUNK_SIZE_BYTES,
    ATTACHMENT_UPLOAD_MAX_CHUNKS,
)


def json_error(message: str, status_code: int = 400, **extra):
    payload = {"success": False, "message": message}
    payload.update(extra)
    return JSONResponse(content=payload, status_code=status_code)


def parse_attachment_refs_value(value: str | list[str] | None) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        refs = value
    else:
        try:
            refs = orjson.loads(value)
        except orjson.JSONDecodeError as exc:
            raise ValueError("Invalid attachment_refs JSON") from exc
    if not isinstance(refs, list):
        raise ValueError("attachment_refs must be a JSON array")
    cleaned: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        if not isinstance(ref, str) or not ref.startswith("att_") or len(ref) > 128:
            raise ValueError("Invalid attachment reference")
        if ref in seen:
            continue
        seen.add(ref)
        cleaned.append(ref)
    return cleaned


def normalize_upload_content_type(content_type: str | None, filename: str | None) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    lower_name = (filename or "").lower()
    if not ct or ct == "application/octet-stream":
        if lower_name.endswith(".pdf"):
            return "application/pdf"
        if is_text_file("text/plain", filename or ""):
            return "text/plain"
    return ct


def max_upload_bytes_for_attachment(content_type: str, filename: str) -> int:
    if content_type == "application/pdf":
        return MAX_PDF_SIZE_MB * 1024 * 1024
    if is_text_file(content_type, filename):
        return MAX_TEXT_FILE_SIZE_MB * 1024 * 1024
    if content_type.startswith("image/"):
        return MAX_RAW_UPLOAD_SIZE_MB * 1024 * 1024
    raise ValueError(f"Unsupported file type: {content_type or 'unknown'}")


def validate_chunk_upload_metadata(
    *,
    upload_id: str,
    chunk_index: int,
    total_chunks: int,
    filename: str,
    content_type: str,
    total_size: int,
    chunk_size: int,
) -> tuple[str, int]:
    if not ATTACHMENT_UPLOAD_ID_RE.match(upload_id or ""):
        raise ValueError("Invalid upload id")
    if not filename:
        raise ValueError("Filename is required")
    if chunk_index < 0:
        raise ValueError("Invalid chunk index")
    if total_chunks < 1 or total_chunks > ATTACHMENT_UPLOAD_MAX_CHUNKS:
        raise ValueError("Invalid chunk count")
    if chunk_index >= total_chunks:
        raise ValueError("Chunk index exceeds chunk count")
    if total_size < 0:
        raise ValueError("Invalid file size")
    if not (ATTACHMENT_UPLOAD_MIN_CHUNK_SIZE_BYTES <= chunk_size <= ATTACHMENT_UPLOAD_MAX_CHUNK_SIZE_BYTES):
        raise ValueError("Invalid chunk size")
    normalized_type = normalize_upload_content_type(content_type, filename)
    max_bytes = max_upload_bytes_for_attachment(normalized_type, filename)
    if total_size > max_bytes:
        raise ValueError(f"File '{filename}' exceeds the {max_bytes // (1024 * 1024)}MB size limit")
    expected_chunks = max(1, (total_size + chunk_size - 1) // chunk_size)
    if total_chunks != expected_chunks:
        raise ValueError("Chunk metadata does not match file size")
    return normalized_type, max_bytes


def attachment_upload_dir(user_id: int, conversation_id: int, upload_id: str) -> Path:
    root = ATTACHMENT_UPLOAD_CHUNK_ROOT.resolve()
    target = (root / str(user_id) / str(conversation_id) / upload_id).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("Invalid upload path") from exc
    return target


def prune_stale_attachment_upload_dirs(root: Path, ttl_seconds: int) -> int:
    if not root.exists():
        return 0
    cutoff = time.time() - ttl_seconds
    pruned = 0
    for upload_dir in root.glob("*/*/*"):
        try:
            if not upload_dir.is_dir():
                continue
            if upload_dir.stat().st_mtime < cutoff:
                shutil.rmtree(upload_dir, ignore_errors=True)
                pruned += 1
        except OSError:
            logger.debug("Could not prune stale attachment upload dir %s", upload_dir, exc_info=True)
    return pruned


async def delete_attachment_upload_dir(upload_dir: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, upload_dir, True)


async def prune_stale_attachment_upload_chunks() -> int:
    return await asyncio.to_thread(
        prune_stale_attachment_upload_dirs,
        ATTACHMENT_UPLOAD_CHUNK_ROOT,
        ATTACHMENT_UPLOAD_TTL_SECONDS,
    )


async def ensure_attachment_upload_allowed(conversation_id: int, current_user: User):
    if current_user is None:
        return json_error("Not authenticated", status_code=401, redirect="/login")
    if not current_user.can_send_files:
        return json_error("File uploads are not enabled for your account", status_code=403)
    async with get_db_connection(readonly=True) as conn:
        try:
            cursor = await conn.execute(
                """
                SELECT
                    c.locked,
                    c.user_id,
                    COALESCE(ud.allow_file_upload, 0) AS allow_file_upload,
                    COALESCE(ep.gransabio_enabled, 0) AS gransabio_enabled
                FROM CONVERSATIONS c
                LEFT JOIN USER_DETAILS ud ON ud.user_id = c.user_id
                LEFT JOIN PROMPTS ep ON ep.id = COALESCE(c.role_id, ud.current_prompt_id)
                WHERE c.id = ?
                """,
                (conversation_id,),
            )
        except sqlite3.OperationalError:
            try:
                cursor = await conn.execute(
                    """
                    SELECT
                        c.locked,
                        c.user_id,
                        COALESCE(ud.allow_file_upload, 0) AS allow_file_upload,
                        0 AS gransabio_enabled
                    FROM CONVERSATIONS c
                    LEFT JOIN USER_DETAILS ud ON ud.user_id = c.user_id
                    WHERE c.id = ?
                    """,
                    (conversation_id,),
                )
            except sqlite3.OperationalError:
                cursor = await conn.execute(
                    """
                    SELECT
                        c.locked,
                        c.user_id,
                        1 AS allow_file_upload,
                        0 AS gransabio_enabled
                    FROM CONVERSATIONS c
                    WHERE c.id = ?
                    """,
                    (conversation_id,),
                )
        row = await cursor.fetchone()
    if not row or int(row["user_id"]) != int(current_user.id):
        return json_error("Conversation not found.", status_code=404)
    if row["locked"]:
        return json_error("Conversation is locked.", status_code=403)
    if not bool(row["allow_file_upload"]):
        return json_error("File uploads are not enabled for your account", status_code=403)
    if bool(row["gransabio_enabled"]):
        return json_error("File attachments are not supported with GranSabio mode. Send text only.", status_code=400)
    return None


async def create_pending_attachment_from_upload(
    *,
    user_id: int,
    conversation_id: int,
    data: bytes,
    filename: str,
    content_type: str,
):
    normalized_type = normalize_upload_content_type(content_type, filename)
    max_bytes = max_upload_bytes_for_attachment(normalized_type, filename)
    if len(data) > max_bytes:
        raise ValueError(f"File '{filename}' exceeds the {max_bytes // (1024 * 1024)}MB size limit")

    if normalized_type == "application/pdf":
        page_count = await asyncio.to_thread(
            validate_pdf,
            data,
            enforce_page_limit=False,
        )
        return await create_pending_pdf_attachment(
            user_id=user_id,
            conversation_id=conversation_id,
            data=data,
            filename=filename,
            page_count=page_count,
            declared_mime=normalized_type,
        )

    if is_text_file(normalized_type, filename):
        text_content = await asyncio.to_thread(decode_text_file, data, filename)
        return await create_pending_text_attachment(
            user_id=user_id,
            conversation_id=conversation_id,
            text_content=text_content,
            filename=filename,
            declared_mime=normalized_type,
        )

    if normalized_type.startswith("image/"):
        image_data, image_media_type, width, height, _actual_format, _was_compressed = await asyncio.to_thread(
            validate_and_compress_image,
            data,
            filename,
        )
        if len(image_data) > MAX_API_IMAGE_SIZE_MB * 1024 * 1024:
            raise ValueError("Image is too large. Please use a smaller or lower-resolution image.")
        return await create_pending_image_attachment(
            user_id=user_id,
            conversation_id=conversation_id,
            data=image_data,
            filename=filename,
            mime_detected=image_media_type,
            declared_mime=normalized_type,
            width=width,
            height=height,
        )

    raise ValueError(f"Unsupported file type: {normalized_type or 'unknown'}")


def pending_attachment_upload_payload(pending) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": True,
        "attachment_ref": pending.public_id,
        "attachment_type": pending.kind,
        "filename": pending.original_filename,
        "size_bytes": pending.size_bytes,
        "block": pending.block,
    }
    if pending.kind == "pdf":
        payload["pages"] = pending.block.get("document_url", {}).get("pages")
    elif pending.kind == "text":
        payload["lines"] = pending.block.get("text_file", {}).get("lines")
    return payload


async def load_pending_attachment_files(
    *,
    attachment_refs: list[str],
    user_id: int,
    conversation_id: int,
) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for ref in attachment_refs:
        result = await read_pending_attachment_bytes(
            ref,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if not result:
            raise ValueError("Attachment upload expired or is not available. Please attach the file again.")
        data, attachment = result
        kind = str(attachment.get("attachment_type") or "")
        if kind == "pdf":
            content_type = "application/pdf"
        elif kind == "text":
            content_type = attachment.get("declared_mime") or "text/plain"
        elif kind == "image":
            content_type = attachment.get("mime_detected") or attachment.get("declared_mime") or "image/webp"
        else:
            raise ValueError("Unsupported attachment reference")
        files.append({
            "data": data,
            "content_type": str(content_type).lower(),
            "filename": attachment.get("original_filename") or attachment.get("display_name") or "attachment",
            "attachment_ref": ref,
        })
    return files
