"""Per-user storage quota accounting and enforcement.

Two accounting sources feed one quota:

- Uploads: computed live from the content-addressed blob store. A user's usage
  is the SUM of DISTINCT blobs referenced by their FILE_ATTACHMENTS rows, so the
  same file re-uploaded by the same user counts once, while each distinct user
  holding it is charged fully.
- Generated media: recorded in the GENERATED_MEDIA_FILES ledger, one row per
  file on disk, summed 1:1 with the filesystem.

Every public function takes an already-open aiosqlite connection so callers can
compose it into their existing transactions -- this module never opens, commits,
or rolls back a connection of its own. Bytes are the canonical unit everywhere;
GB in this codebase means binary GB (1 GB = 1024**3 bytes). A quota of 0 means
unlimited at either the per-user or the global-default level.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Iterable

import aiosqlite


# 30 GB binary (30 * 1024**3). Seeded into SYSTEM_CONFIG by
# migration_storage_quotas.py; used as the fallback when the key is missing.
DEFAULT_QUOTA_BYTES = 32212254720
MAX_QUOTA_BYTES = (1 << 63) - 1

# Mirrors the CHECK constraint on GENERATED_MEDIA_FILES.kind.
ALLOWED_GENERATED_KINDS = ("image", "video", "pdf", "mp3", "wav")

_BYTES_PER_GB = 1024 ** 3


def format_gb(num_bytes: int) -> str:
    """Format a byte count as binary GB with one decimal, e.g. "29.8 GB"."""
    return f"{num_bytes / _BYTES_PER_GB:.1f} GB"


# --- User-facing messages (exact wording, shared by every surface) ----------
# One builder per surface so web, platform (Telegram/WhatsApp), generation and
# export all report identical wording. Callers holding a StorageQuotaExceededError
# can rebuild any flavor from its usage_bytes / quota_bytes.

def web_upload_quota_message(usage_bytes: int, quota_bytes: int) -> str:
    return (
        f"Storage quota exceeded: you are using {format_gb(usage_bytes)} of "
        f"{format_gb(quota_bytes)}. Delete some files to free up space."
    )


def platform_reply_quota_message(usage_bytes: int, quota_bytes: int, *, translator=None) -> str:
    from chat.services.localization import chat_translator
    translator = chat_translator(translator)
    return translator.t(
        "channel_notices.storage_full",
        usage=translator.format_number(usage_bytes / _BYTES_PER_GB, 1, 1) + " GB",
        quota=translator.format_number(quota_bytes / _BYTES_PER_GB, 1, 1) + " GB",
    )


def generation_quota_message(usage_bytes: int, quota_bytes: int) -> str:
    return (
        f"Storage quota exceeded ({format_gb(usage_bytes)} of "
        f"{format_gb(quota_bytes)} used). Free up space to generate or export "
        f"new content."
    )


class StorageQuotaExceededError(Exception):
    """Raised when a storage-consuming operation would exceed the user's quota.

    Carries the raw numbers (usage_bytes, quota_bytes) and a preformatted human
    message so callers can surface identical wording or rebuild another flavor.
    """

    def __init__(self, usage_bytes: int, quota_bytes: int, message: str) -> None:
        super().__init__(message)
        self.usage_bytes = usage_bytes
        self.quota_bytes = quota_bytes
        self.message = message

    @classmethod
    def web_upload(cls, usage_bytes: int, quota_bytes: int) -> "StorageQuotaExceededError":
        return cls(usage_bytes, quota_bytes, web_upload_quota_message(usage_bytes, quota_bytes))

    @classmethod
    def platform_reply(cls, usage_bytes: int, quota_bytes: int) -> "StorageQuotaExceededError":
        return cls(usage_bytes, quota_bytes, platform_reply_quota_message(usage_bytes, quota_bytes))

    @classmethod
    def generation(cls, usage_bytes: int, quota_bytes: int) -> "StorageQuotaExceededError":
        return cls(usage_bytes, quota_bytes, generation_quota_message(usage_bytes, quota_bytes))


_QUOTA_MESSAGE_BUILDERS = {
    "web_upload": web_upload_quota_message,
    "platform": platform_reply_quota_message,
    "generation": generation_quota_message,
}


def normalize_rel_path(path: str) -> str:
    """Canonicalize a media path to the ledger's rel_path form.

    Accepts an absolute path, a path relative to data/ (which may start with
    "users/"), or an already-canonical rel_path. Returns a forward-slash string
    relative to data/users/ with no leading slash and no "users/" prefix, e.g.
    "abc/de/<hash>/files/000/0001/img/bot/<sha1>_fullsize.webp".

    This is the ONE shared normalizer: every write hook, delete hook and the
    reconcile tool must go through it, or the UNIQUE upsert and rel_path-matched
    deletes silently miss. Purely mechanical string work (handles Windows
    backslashes). Raises ValueError when the input clearly is not under the
    users tree -- fail fast.
    """
    if path is None:
        raise ValueError("rel_path is required")
    normalized = str(path).replace("\\", "/")
    parts = [segment for segment in normalized.split("/") if segment not in ("", ".")]
    if ".." in parts:
        raise ValueError(f"Path traversal segment in rel_path: {path!r}")

    is_absolute = normalized.startswith("/") or bool(
        re.match(r"^[A-Za-z]:/", normalized)
    )

    # The users directory boundary is the last "users" segment: hash prefixes and
    # the SHA1 user_hash are hex, and the subdir names (files/img/video/...) never
    # equal "users", so this is unambiguous.
    users_index = -1
    for index, segment in enumerate(parts):
        if segment == "users" and (
            not is_absolute or (index > 0 and parts[index - 1] == "data")
        ):
            users_index = index

    if users_index >= 0:
        canonical_parts = parts[users_index + 1:]
    else:
        # No "users" anchor: either an already-canonical rel_path, or a path that
        # points elsewhere. A "data" segment with no following "users" is clearly
        # outside the users tree (e.g. data/file_blobs/...).
        if is_absolute or "data" in parts:
            raise ValueError(f"Path is not under data/users/: {path!r}")
        canonical_parts = parts

    if not canonical_parts:
        raise ValueError(f"Empty rel_path after normalization: {path!r}")
    return "/".join(canonical_parts)


async def get_default_quota_bytes(conn: aiosqlite.Connection) -> int:
    """Global default quota from SYSTEM_CONFIG.

    Missing key falls back to DEFAULT_QUOTA_BYTES. A present-but-unparseable or
    negative value raises (fail fast: a corrupt config must never silently
    become "unlimited").
    """
    cursor = await conn.execute(
        "SELECT value FROM SYSTEM_CONFIG WHERE key = ?",
        ("storage_quota_default_bytes",),
    )
    row = await cursor.fetchone()
    if row is None:
        return DEFAULT_QUOTA_BYTES
    raw = row[0]
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"Corrupt SYSTEM_CONFIG storage_quota_default_bytes: {raw!r}")
    if value < 0 or value > MAX_QUOTA_BYTES:
        raise ValueError(f"Out-of-range SYSTEM_CONFIG storage_quota_default_bytes: {value}")
    return value


async def get_effective_quota_bytes(conn: aiosqlite.Connection, user_id: int) -> int:
    """Per-user override if set (NOT NULL), else the global default.

    0 = unlimited at either level; a NULL override means "use the default".
    """
    cursor = await conn.execute(
        "SELECT storage_quota_bytes FROM USER_DETAILS WHERE user_id = ?",
        (user_id,),
    )
    row = await cursor.fetchone()
    if row is not None and row[0] is not None:
        value = int(row[0])
        if value < 0 or value > MAX_QUOTA_BYTES:
            raise ValueError(f"Out-of-range user storage quota: {value}")
        return value
    return await get_default_quota_bytes(conn)


async def get_uploads_usage_bytes(conn: aiosqlite.Connection, user_id: int) -> int:
    """Live, dedupe-aware upload usage: SUM of DISTINCT blobs the user references."""
    # FILE_BLOB_VARIANTS (upload thumbnails) are deliberately NOT summed here:
    # they are platform-derived overhead on uploads, not user storage. This is a
    # deliberate asymmetry with generated media (see get_generated_usage_bytes).
    try:
        cursor = await conn.execute(
            """
            SELECT COALESCE(SUM(fb.size_bytes), 0)
            FROM FILE_BLOBS fb
            WHERE fb.id IN (
                SELECT DISTINCT fa.blob_id
                FROM FILE_ATTACHMENTS fa
                WHERE fa.user_id = ? AND fa.status IN ('pending', 'active')
            )
            """,
            (user_id,),
        )
    except sqlite3.OperationalError as exc:
        # The FILE_* tables are runtime-created by file_storage on first upload;
        # a DB where nobody has ever uploaded simply has no such table, which is
        # exactly zero upload usage.
        if "no such table" in str(exc):
            return 0
        raise
    row = await cursor.fetchone()
    return int(row[0])


async def get_generated_usage_bytes(conn: aiosqlite.Connection, user_id: int) -> int:
    """Generated-media usage from the ledger, summed 1:1 with the filesystem."""
    # Generated-media thumbnails DO count (an image save is 2 rows, thumbnail +
    # fullsize): they are part of the generated artifact, and keeping the ledger
    # == filesystem makes reconciliation trivial. Deliberate asymmetry with
    # uploads, where variants are excluded (see get_uploads_usage_bytes).
    cursor = await conn.execute(
        "SELECT COALESCE(SUM(size_bytes), 0) FROM GENERATED_MEDIA_FILES WHERE user_id = ?",
        (user_id,),
    )
    row = await cursor.fetchone()
    return int(row[0])


async def get_total_usage_bytes(conn: aiosqlite.Connection, user_id: int) -> int:
    """Uploads + generated usage in bytes."""
    uploads = await get_uploads_usage_bytes(conn, user_id)
    generated = await get_generated_usage_bytes(conn, user_id)
    return uploads + generated


async def ensure_upload_fits(
    conn: aiosqlite.Connection,
    user_id: int,
    blob_id: int,
    size_bytes: int,
) -> None:
    """Dedupe-aware hard gate for a single blob ingest.

    Runs the two checks the caller needs BEFORE inserting the attachment; the
    commit/prune/raise choreography around a rejection is the caller's job. This
    function only reads -- it must run inside the caller's already-open
    transaction and must never open, commit or roll back one.
    """
    size_bytes = int(size_bytes)
    if size_bytes < 0 or size_bytes > MAX_QUOTA_BYTES:
        raise ValueError(f"Invalid upload size: {size_bytes}")

    # (1) Free re-link: if the user already references this blob (pending or
    # active), the incremental cost is 0 -> allowed even at/over quota. This is
    # why the gate lives at blob-ingest level, after dedupe: re-linking content
    # you already own must never be rejected.
    cursor = await conn.execute(
        """
        SELECT 1 FROM FILE_ATTACHMENTS
        WHERE user_id = ? AND blob_id = ? AND status IN ('pending', 'active')
        LIMIT 1
        """,
        (user_id, blob_id),
    )
    if await cursor.fetchone() is not None:
        return
    # (2) Unlimited quota -> no check.
    quota_bytes = await get_effective_quota_bytes(conn, user_id)
    if quota_bytes == 0:
        return
    # (3) The blob's stored size must fit on top of current usage.
    usage_bytes = await get_total_usage_bytes(conn, user_id)
    if usage_bytes + size_bytes > quota_bytes:
        raise StorageQuotaExceededError.web_upload(usage_bytes, quota_bytes)


async def ensure_generation_headroom(
    conn: aiosqlite.Connection,
    user_id: int,
    *,
    message_flavor: str = "generation",
) -> None:
    """Soft pre-check for generation / export / branching.

    An operation whose result size is unknowable up front may START only while
    the user is strictly under quota; once started, its result always saves.
    Raises StorageQuotaExceededError unless the quota is unlimited or usage is
    below it. message_flavor selects the wording ("generation" | "platform" |
    "web_upload") so the caller can match the surface.
    """
    builder = _QUOTA_MESSAGE_BUILDERS.get(message_flavor)
    if builder is None:
        raise ValueError(f"Unknown quota message flavor: {message_flavor!r}")
    quota_bytes = await get_effective_quota_bytes(conn, user_id)
    if quota_bytes == 0:
        return
    usage_bytes = await get_total_usage_bytes(conn, user_id)
    if usage_bytes >= quota_bytes:
        raise StorageQuotaExceededError(usage_bytes, quota_bytes, builder(usage_bytes, quota_bytes))


async def ensure_known_growth_fits(
    conn: aiosqlite.Connection,
    user_id: int,
    size_bytes: int,
    *,
    message_flavor: str = "generation",
) -> None:
    """Hard gate for an operation whose incremental disk growth is known."""
    builder = _QUOTA_MESSAGE_BUILDERS.get(message_flavor)
    if builder is None:
        raise ValueError(f"Unknown quota message flavor: {message_flavor!r}")
    size_bytes = int(size_bytes)
    if size_bytes < 0 or size_bytes > MAX_QUOTA_BYTES:
        raise ValueError(f"Invalid storage growth: {size_bytes}")

    quota_bytes = await get_effective_quota_bytes(conn, user_id)
    if quota_bytes == 0:
        return
    usage_bytes = await get_total_usage_bytes(conn, user_id)
    if usage_bytes + size_bytes > quota_bytes:
        raise StorageQuotaExceededError(
            usage_bytes,
            quota_bytes,
            builder(usage_bytes, quota_bytes),
        )


async def record_generated_file(
    conn: aiosqlite.Connection,
    conversation_id: int,
    kind: str,
    rel_path: str,
    size_bytes: int,
) -> None:
    """Upsert one generated-media file into the ledger.

    Resolves the owner from CONVERSATIONS.user_id, normalizes rel_path through
    the shared normalizer, and upserts on rel_path so the SHA1-name overwrite of
    a regenerated identical file updates its size instead of duplicating a row.
    Runs inside the caller's transaction; does not commit.
    """
    if kind not in ALLOWED_GENERATED_KINDS:
        raise ValueError(f"Unknown generated media kind: {kind!r}")
    size_bytes = int(size_bytes)
    if size_bytes < 0 or size_bytes > MAX_QUOTA_BYTES:
        raise ValueError(f"Invalid generated media size: {size_bytes}")
    cursor = await conn.execute(
        "SELECT user_id FROM CONVERSATIONS WHERE id = ?",
        (conversation_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise ValueError(
            f"Conversation {conversation_id} not found; cannot ledger generated media"
        )
    user_id = int(row[0])
    canonical = normalize_rel_path(rel_path)
    await conn.execute(
        """
        INSERT INTO GENERATED_MEDIA_FILES (user_id, conversation_id, kind, rel_path, size_bytes)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(rel_path) DO UPDATE SET size_bytes = excluded.size_bytes
        """,
        (user_id, conversation_id, kind, canonical, size_bytes),
    )


async def delete_generated_file_rows(
    conn: aiosqlite.Connection,
    rel_paths: Iterable[str],
) -> int:
    """Delete ledger rows for the given files (matched by canonical rel_path).

    Normalizes each path through the shared normalizer, deletes the matching
    rows, and returns the number deleted. Runs inside the caller's transaction;
    does not commit.
    """
    canonical = [normalize_rel_path(path) for path in rel_paths if path]
    if not canonical:
        return 0
    placeholders = ",".join("?" for _ in canonical)
    cursor = await conn.execute(
        f"DELETE FROM GENERATED_MEDIA_FILES WHERE rel_path IN ({placeholders})",
        canonical,
    )
    return int(cursor.rowcount or 0)
