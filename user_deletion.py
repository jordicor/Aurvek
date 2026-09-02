"""Coordinated user-account deletion service.

Single source of truth for erasing an Aurvek account, shared by the HTTP
endpoints (``app.py``) and the administrative CLI (``tools/delete_user_data.py``).

Flow, in order:

1. GUARDS: stale provider holds are reconciled first (skipped on dry runs), then
   active provider usage, admin accounts, financial/marketplace entanglement, and
   Atagia presence without a reachable bridge each block deletion and return a
   structured result. The active-usage guard is re-checked inside the local write
   transaction, where BEGIN IMMEDIATE serializes with reservation creation.
2. CAPTURE (no mutation): per-table counts, conversation ids, ElevenLabs remote
   conversation ids (purged post-commit), Atagia user id, and file-tree paths.
3. EXTERNAL PURGE FIRST: erase the user in Atagia before touching local rows, so a
   failure aborts with the references still intact for a retry.
4. LOCAL DELETION: one transaction with foreign keys ON, children before parents,
   reusing the per-conversation domain helper. Single-party wallet rows
   (TRANSACTIONS) are pseudonymized into FINANCIAL_ARCHIVE and then deleted here.
5. POST-COMMIT (best effort): session revocation, blob pruning, remote ElevenLabs
   conversation purge, and removal of both the modern hash tree and the legacy
   ``data/static/files/<user_id>/`` tree.

Fail-fast: the external Atagia purge is fail-closed (any failure aborts before
local mutations). Only post-commit cleanup (filesystem removal and remote
ElevenLabs purge) is best effort and, on failure, surfaces as
``completed_with_cleanup_errors`` rather than being swallowed.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from atagia_bridge import _aurvek_user_id, get_atagia_erasure_bridge
from billing.usage_reservations import reconcile_stale_usage_reservations
from chat.services.privacy import delete_conversation_rows
from common import PEPPER
from database import get_db_connection
from file_storage import prune_unreferenced_blobs
from integrations.elevenlabs.service import delete_remote_conversation
from log_config import logger
from prompts import get_user_directory
from rediscfg import add_revoked_user

ATAGIA_PROVIDER = "atagia"

_ACTIVE_USAGE_SUMMARY = (
    "Account cannot be deleted while provider usage is still in progress. "
    "Please try again shortly."
)


async def _await_task_uninterruptibly(task: "asyncio.Task"):
    """Delay repeated caller cancellation until a security finalizer is done."""
    delayed_cancel: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            delayed_cancel = exc
            continue
    task_error: BaseException | None = None
    result = None
    try:
        result = task.result()
    except BaseException as exc:
        task_error = exc
    if task_error is not None:
        raise task_error
    if delayed_cancel is not None:
        raise delayed_cancel
    return result


async def _run_finalizer_uninterruptibly(coro, *, name: str):
    task = asyncio.create_task(coro, name=name)
    return await _await_task_uninterruptibly(task)

# Same-user cascade in dependency order. MESSAGES, WATCHDOG_EVENTS, WATCHDOG_STATE,
# and CONVERSATIONS are intentionally absent: they are removed per conversation via
# delete_conversation_rows so memory-provider links and watchdog rows are cleared
# before the enforced FK on MESSAGES is hit. Each entry lists any extra tables a
# subquery depends on so a partial schema (test DBs, pre-migration prod) is skipped
# instead of raising.
_CASCADE_TABLES: list[tuple[str, str, list[str]]] = [
    ("CHAT_FOLDERS", "user_id = ?", []),
    ("SERVICE_USAGE", "user_id = ?", []),
    ("USAGE_DAILY", "user_id = ?", []),
    # Explicit even though its FKs declare ON DELETE CASCADE: deletion must not
    # silently depend on a cascade that a future schema rebuild could drop.
    ("ELEVENLABS_CALL_SESSIONS", "user_id = ?", []),
    # TRANSACTIONS is intentionally absent: single-party wallet rows are archived
    # into FINANCIAL_ARCHIVE (pseudonymized) and then deleted explicitly by
    # _archive_transactions inside the same local transaction.
    ("DISCOUNTS", "created_by_user_id = ?", []),
    ("PROMPT_PERMISSIONS", "user_id = ?", []),
    ("PROMPT_PURCHASES", "buyer_user_id = ?", []),
    ("FAVORITE_PROMPTS", "user_id = ?", []),
    ("PACK_PURCHASES", "buyer_user_id = ?", []),
    ("PACK_ACCESS", "user_id = ?", []),
    ("ENTITLEMENTS", "user_id = ?", []),
    ("PACKS", "created_by_user_id = ?", []),
    ("CREATOR_EARNINGS", "creator_id = ?", []),
    ("CREATOR_EARNINGS", "consumer_id = ?", []),
    ("CREATOR_EARNINGS", "referral_id = ?", []),
    ("CREATOR_PROFILES", "user_id = ?", []),
    ("USER_CREATOR_RELATIONSHIPS", "user_id = ?", []),
    ("USER_CREATOR_RELATIONSHIPS", "creator_id = ?", []),
    ("USER_CAPTIVE_DOMAINS", "user_id = ?", []),
    ("PROMPT_CUSTOM_DOMAINS", "activated_by_user_id = ?", []),
    ("USER_BRANDING", "user_id = ?", []),
    ("USER_ALTER_EGOS", "user_id = ?", []),
    ("WHATSAPP_LOG", "user_id = ?", []),
    ("TELEGRAM_LOG", "user_id = ?", []),
    ("PENDING_ENTITLEMENTS", "user_id = ?", []),
    ("MAGIC_LINKS", "user_id = ?", []),
    (
        "PROMPT_SECTION_CONFIGS",
        "prompt_id IN (SELECT id FROM PROMPTS WHERE created_by_user_id = ?)",
        ["PROMPTS"],
    ),
    (
        "PROMPT_AGENT_MAPPING",
        "prompt_id IN (SELECT id FROM PROMPTS WHERE created_by_user_id = ?)",
        ["PROMPTS"],
    ),
    ("PROMPTS", "created_by_user_id = ?", []),
]

# User-level tables cleared by a plain user_id predicate, guarded on existence.
_USER_LEVEL_TABLES: list[str] = [
    "MEMORY_PROVIDER_MESSAGE_LINKS",
    "MEMORY_PROVIDER_CONVERSATION_LINKS",
    "MEMORY_USER_PREFERENCES",
    "USER_ACTIVITY_SESSION_CONVERSATIONS",
    "USER_ACTIVITY_SESSIONS",
    "USER_WELLBEING_EVENTS",
    "USER_WELLBEING_PREFERENCES",
]


@dataclass
class DeletionResult:
    """Machine-readable outcome of a deletion attempt (no personal content)."""

    status: str
    user_id: int | None = None
    username: str | None = None
    reason_codes: list[str] = field(default_factory=list)
    summary: str = ""
    blocking_tables: list[dict[str, Any]] = field(default_factory=list)
    table_counts: dict[str, int] = field(default_factory=dict)
    conversation_ids: list[int] = field(default_factory=list)
    elevenlabs_session_ids: list[str] = field(default_factory=list)
    atagia_user_id: str | None = None
    atagia_link_count: int = 0
    atagia_report: dict[str, Any] | None = None
    archived_transaction_count: int = 0
    file_paths: list[str] = field(default_factory=list)
    cleanup_errors: list[dict[str, str]] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"

    @property
    def ok(self) -> bool:
        return self.status in ("completed", "completed_with_cleanup_errors", "dry_run")


@dataclass
class _SubscriptionDeletionLifecycle:
    """Held GPTSub lifecycle guard spanning purge through local DELETE commit."""

    lock: Any
    end_marker: Any
    reconcile: Any
    user_id: int
    purged: bool
    released: bool = False
    finalizer_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def abort(self) -> None:
        async def _abort_once() -> None:
            async with self.finalizer_lock:
                if self.released:
                    return
                try:
                    await self.end_marker(self.user_id)
                finally:
                    if self.lock.locked():
                        self.lock.release()
                    self.released = True

        await _run_finalizer_uninterruptibly(
            _abort_once(),
            name=f"gptsub-deletion-abort-{self.user_id}",
        )

    async def complete(self) -> None:
        async def _complete_once() -> None:
            async with self.finalizer_lock:
                if self.released:
                    return
                # Keep the deleted-user marker for this process lifetime. It blocks
                # a stale authenticated request that began before COMMIT from
                # starting a device flow for a row that no longer exists.
                if self.lock.locked():
                    self.lock.release()
                self.released = True
                if self.purged:
                    await self.reconcile(self.user_id)

        await _run_finalizer_uninterruptibly(
            _complete_once(),
            name=f"gptsub-deletion-complete-{self.user_id}",
        )


async def _begin_subscription_deletion_lifecycle(
    user_id: int,
) -> _SubscriptionDeletionLifecycle | None:
    """Invalidate pending links and strictly revoke any current saved grant."""
    try:
        from subscription_auth.linking import begin_user_deletion, end_user_deletion
        from subscription_auth.purge import (
            _purge_user_link_with_session_lock_held,
            _reconcile_after_purge,
        )
        from subscription_auth.token_store import _read_column, get_user_session_lock
    except ModuleNotFoundError as exc:
        if exc.name != "subscription_auth":
            raise
        # The public mirror intentionally omits the private package but may retain
        # the nullable schema column. Deletion is a safe no-op only when there is
        # positively no credential to revoke; a non-NULL orphan stays fail-closed.
        async with get_db_connection(readonly=True) as conn:
            columns_cursor = await conn.execute("PRAGMA table_info(USER_DETAILS)")
            columns = {row[1] for row in await columns_cursor.fetchall()}
            if "subscription_auth" not in columns:
                # A legacy public schema had nowhere to store this credential.
                return None
            cursor = await conn.execute(
                "SELECT subscription_auth FROM USER_DETAILS WHERE user_id = ?",
                (int(user_id),),
            )
            row = await cursor.fetchone()
        if row is not None and row[0] is not None:
            raise RuntimeError(
                "subscription credential exists but its private cleanup module is absent"
            )
        return None

    lock = get_user_session_lock(user_id)
    await lock.acquire()
    marker_started = False
    try:
        await begin_user_deletion(user_id)
        marker_started = True

        # Re-read under the lifecycle lock. The initial capture may have seen NULL
        # while a completed poll was waiting to persist; if poll won the lock, its
        # new credential is now visible and must be remotely revoked before DELETE.
        stored = await _read_column(user_id)
        purged = stored is not None
        if purged:
            await _purge_user_link_with_session_lock_held(
                user_id,
                revoke=True,
                strict_remote_revoke=True,
            )
        return _SubscriptionDeletionLifecycle(
            lock=lock,
            end_marker=end_user_deletion,
            reconcile=_reconcile_after_purge,
            user_id=user_id,
            purged=purged,
        )
    except BaseException:
        async def _release_failed_begin() -> None:
            try:
                if marker_started:
                    await end_user_deletion(user_id)
            finally:
                if lock.locked():
                    lock.release()

        await _run_finalizer_uninterruptibly(
            _release_failed_begin(),
            name=f"gptsub-deletion-begin-rollback-{user_id}",
        )
        raise


async def delete_user_account(
    *,
    user_id: int | None = None,
    username: str | None = None,
    dry_run: bool = False,
    bridge: Any | None = None,
) -> DeletionResult:
    """Delete one account end to end. Exactly one of user_id/username is required.

    Never raises for a missing user, blocked guard, or unreachable Atagia; those are
    reported as structured results with no mutation. An unexpected error during the
    local transaction is rolled back and re-raised.
    """
    if (user_id is None) == (username is None):
        raise ValueError("Provide exactly one of user_id or username")

    if bridge is None:
        # Erasure bridge: gated on Atagia data custody (atagia_enabled), NOT on
        # the active runtime memory provider -- deletion must still purge Atagia
        # data while the provider is switched off.
        bridge = get_atagia_erasure_bridge()

    if not dry_run:
        # Settle stale provider holds up front so a dead reservation cannot
        # false-block the active-usage guard below. Skipped on dry runs, which
        # must not mutate anything.
        await reconcile_stale_usage_reservations()

    # --- Resolve + guards + capture (read-only, no mutation) ---
    async with get_db_connection(readonly=True) as conn:
        conn.row_factory = aiosqlite.Row
        user_row = await _resolve_user(conn, user_id=user_id, username=username)
        if user_row is None:
            return DeletionResult(
                status="not_found",
                user_id=user_id,
                username=username,
                reason_codes=["not_found"],
                summary="User not found.",
            )

        uid = int(user_row["id"])
        uname = str(user_row["username"])
        atagia_uid = _aurvek_user_id(uid)

        result = DeletionResult(status="", user_id=uid, username=uname, atagia_user_id=atagia_uid)

        # Active-usage guard, checked before any capture or external purge: an
        # in-flight provider reservation must block deletion. Unlike the
        # review-requiring guards below this is transient (retry-shortly), so it
        # fails fast on its own. Re-checked inside the write transaction, where
        # BEGIN IMMEDIATE serializes with reservation creation.
        if await _has_active_reservation(conn, uid):
            result.status = "blocked"
            result.reason_codes = ["active_usage"]
            result.summary = _ACTIVE_USAGE_SUMMARY
            return result

        if await _has_active_phone_call(conn, uid):
            result.status = "blocked"
            result.reason_codes = ["active_phone_call"]
            result.summary = (
                "An active or unresolved phone call must be ended before account deletion."
            )
            return result

        is_admin, admin_summary = await _is_admin_account(conn, uid, user_row["role_id"])
        result.blocking_tables = await _financial_blocks(conn, uid)
        result.atagia_link_count = await _atagia_link_count(conn, uid)

        result.conversation_ids = await _int_ids(
            conn, "SELECT id FROM CONVERSATIONS WHERE user_id = ?", (uid,)
        )
        result.elevenlabs_session_ids = await _elevenlabs_session_ids(conn, uid)
        result.table_counts = await _capture_counts(conn, uid)
        subscription_schema_present = False
        if (
            await _table_exists(conn, "USER_DETAILS")
            and await _column_exists(conn, "USER_DETAILS", "subscription_auth")
        ):
            subscription_schema_present = True
        # Rows that WOULD be archived on execute (single-party wallet ledger).
        # Overwritten with the real archived count during the write transaction.
        if await _table_exists(conn, "TRANSACTIONS"):
            result.archived_transaction_count = await _count(
                conn, "TRANSACTIONS", "user_id = ?", (uid,)
            )
            if result.archived_transaction_count:
                if not PEPPER:
                    raise RuntimeError(
                        "PEPPER is required to pseudonymize archived financial rows"
                    )
                if not await _table_exists(conn, "FINANCIAL_ARCHIVE"):
                    raise RuntimeError(
                        "FINANCIAL_ARCHIVE table is missing; run "
                        "migration_financial_archive.py"
                    )
        held_blob_ids = await _int_ids(
            conn,
            "SELECT DISTINCT blob_id FROM FILE_ATTACHMENTS WHERE user_id = ?",
            (uid,),
        ) if await _table_exists(conn, "FILE_ATTACHMENTS") else []

    result.file_paths = [
        get_user_directory(uname),
        os.path.join("data", "static", "files", str(uid)),
    ]

    has_atagia = result.atagia_link_count > 0
    bridge_enabled = bool(await bridge.is_enabled())

    # --- Evaluate guards ---
    reasons: list[str] = []
    summaries: list[str] = []
    if is_admin:
        reasons.append("admin_account")
        summaries.append(admin_summary)
    if result.blocking_tables:
        reasons.append("financial_entanglement")
        detail = ", ".join(
            f"{block['reason']} ({block['count']})" for block in result.blocking_tables
        )
        summaries.append(
            "Financial or marketplace entanglement requires manual review: " + detail + "."
        )
    if has_atagia and not bridge_enabled:
        reasons.append("atagia_unpurgeable")
        summaries.append(
            "User has Atagia memory data but the Atagia bridge is not configured; "
            "external purge is mandatory and cannot be performed."
        )

    if reasons:
        result.status = "blocked"
        result.reason_codes = reasons
        result.summary = " ".join(summaries)
        return result

    if dry_run:
        result.status = "dry_run"
        result.summary = f"Dry run for user {uname}: no changes made."
        return result

    # --- External purge first (outside the local transaction) ---
    if bridge_enabled:
        try:
            result.atagia_report = await bridge.erase_user(uid)
        except Exception as exc:  # noqa: BLE001 - fail-closed, no local mutation
            result.status = "atagia_unreachable"
            result.reason_codes = ["atagia_unreachable"]
            result.summary = (
                "Atagia erase failed; no local changes were made so the deletion "
                f"can be retried: {exc}"
            )
            return result

    # Hold the GPTSub lifecycle across pending invalidation, strict remote logout,
    # local tombstone, and the final DELETE transaction. This closes both directions
    # of the poll race: poll-before-delete is observed/revoked; delete-before-poll
    # leaves a marker that prevents the completed OAuth grant from being saved.
    subscription_lifecycle = None
    if subscription_schema_present:
        try:
            subscription_lifecycle = await _begin_subscription_deletion_lifecycle(uid)
        except Exception as exc:  # fail closed; row/token remain available for retry
            logger.error(
                "Subscription credential purge failed for user_id=%s: %s", uid, exc
            )
            result.status = "subscription_purge_failed"
            result.reason_codes = ["subscription_purge_failed"]
            result.summary = (
                "The ChatGPT subscription connection could not be revoked; no local "
                "account rows were deleted so the operation can be retried."
            )
            return result

    # --- Local deletion (single transaction, FKs ON) ---
    deletion_committed = False
    try:
        async with get_db_connection() as conn:
            conn.row_factory = aiosqlite.Row
            try:
                await conn.execute("BEGIN IMMEDIATE")
                # Authoritative active-usage re-check: a reservation created after
                # the external purge is caught here. BEGIN IMMEDIATE keeps new ones
                # from being committed until the user deletion finishes.
                if await _has_active_reservation(conn, uid):
                    await conn.rollback()
                    result.status = "blocked"
                    result.reason_codes = ["active_usage"]
                    result.summary = _ACTIVE_USAGE_SUMMARY
                    return result
                if await _has_active_phone_call(conn, uid):
                    await conn.rollback()
                    result.status = "blocked"
                    result.reason_codes = ["active_phone_call"]
                    result.summary = (
                        "An active or unresolved phone call must be ended before "
                        "account deletion."
                    )
                    return result
                # Re-read conversation ids inside the write transaction: a
                # conversation created after capture must not strand the USERS FK.
                result.conversation_ids = await _int_ids(
                    conn, "SELECT id FROM CONVERSATIONS WHERE user_id = ?", (uid,)
                )
                result.archived_transaction_count = await _delete_local_rows(
                    conn, uid, result.conversation_ids
                )
                await conn.commit()
                deletion_committed = True
            except Exception:
                await conn.rollback()
                logger.exception(
                    "Local deletion transaction failed for user_id=%s", uid
                )
                raise
    finally:
        if subscription_lifecycle is not None:
            if deletion_committed:
                await subscription_lifecycle.complete()
            else:
                await subscription_lifecycle.abort()

    # --- Post-commit best-effort cleanup ---
    result.status = "completed"

    try:
        await add_revoked_user(uid)
    except Exception as exc:  # noqa: BLE001 - account already gone; log and continue
        logger.error("Could not add deleted user %s to the revocation cache: %s", uname, exc)

    if held_blob_ids:
        try:
            await prune_unreferenced_blobs(held_blob_ids)
        except Exception as exc:  # noqa: BLE001
            logger.error("Error pruning blobs for deleted user %s: %s", uname, exc)
            result.cleanup_errors.append(
                {
                    "kind": "blob_prune",
                    "target": ",".join(str(blob_id) for blob_id in held_blob_ids),
                    "error": str(exc),
                }
            )

    # Remote ElevenLabs conversation purge. Best effort so a third-party outage
    # never blocks account deletion, but every failure is recorded (never
    # silent). Skipped entirely when there are no captured session ids, so the
    # API-key requirement only applies when there is something to purge.
    for session_id in result.elevenlabs_session_ids:
        try:
            await delete_remote_conversation(session_id)
            logger.debug("Purged remote ElevenLabs conversation %s", session_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Could not purge remote ElevenLabs conversation %s for deleted user %s: %s",
                session_id,
                uname,
                exc,
            )
            result.cleanup_errors.append(
                {"kind": "elevenlabs_session", "target": session_id, "error": str(exc)}
            )

    for path in result.file_paths:
        if not os.path.exists(path):
            continue
        try:
            shutil.rmtree(path)
            logger.debug("Deleted user directory: %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Error deleting user directory %s: %s", path, exc)
            result.cleanup_errors.append(
                {"kind": "file", "target": path, "error": str(exc)}
            )

    if result.cleanup_errors:
        result.status = "completed_with_cleanup_errors"
        result.summary = (
            f"User {uname} deleted, but some cleanup steps (file removal, blob "
            "pruning, or remote ElevenLabs purge) failed and require a manual retry."
        )
    else:
        result.summary = f"User {uname} successfully deleted."
    return result


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #


async def _is_admin_account(
    conn: aiosqlite.Connection, user_id: int, role_id: Any
) -> tuple[bool, str]:
    cursor = await conn.execute(
        "SELECT role_name FROM USER_ROLES WHERE id = ?", (role_id,)
    )
    role_row = await cursor.fetchone()
    if role_row and role_row[0] == "admin":
        return True, "This is an admin account; admin accounts require a manual operation."
    if await _table_exists(conn, "ADMIN_AUDIT_LOG"):
        cursor = await conn.execute(
            "SELECT 1 FROM ADMIN_AUDIT_LOG WHERE admin_id = ? LIMIT 1", (user_id,)
        )
        if await cursor.fetchone():
            return True, (
                "This account acted as an admin (it has audit-log entries) and "
                "requires a manual operation."
            )
    return False, ""


async def _financial_blocks(
    conn: aiosqlite.Connection, user_id: int
) -> list[dict[str, Any]]:
    """Return blocking (table, reason, count) entries for financial/marketplace ties.

    Only CROSS-USER (two-party) money entanglement blocks deletion: a row that is
    simultaneously the counterparty's sales/earnings/entitlement record. Cleanly
    removing this user from such a row would require nullable identity columns (a
    schema rebuild), which is deferred until real marketplace traffic exists; until
    then these cases go to manual review.

    Single-party wallet rows (TRANSACTIONS.user_id = uid) do NOT block: they are
    the user's own ledger and are pseudonymized into FINANCIAL_ARCHIVE and deleted
    automatically inside the local transaction (see _archive_transactions).
    """
    checks: list[tuple[str, str, str, tuple[Any, ...], list[str]]] = [
        (
            "PROMPT_PURCHASES",
            "own prompt purchases",
            "buyer_user_id = ?",
            (user_id,),
            [],
        ),
        ("PACK_PURCHASES", "own pack purchases", "buyer_user_id = ?", (user_id,), []),
        (
            "CREATOR_EARNINGS",
            "own creator earnings",
            "creator_id = ? OR consumer_id = ? OR referral_id = ?",
            (user_id, user_id, user_id),
            [],
        ),
        (
            "PROMPT_PURCHASES",
            "other users' purchases of this user's prompts",
            "buyer_user_id <> ? AND prompt_id IN (SELECT id FROM PROMPTS WHERE created_by_user_id = ?)",
            (user_id, user_id),
            ["PROMPTS"],
        ),
        (
            "PACK_PURCHASES",
            "other users' purchases of this user's packs",
            "buyer_user_id <> ? AND pack_id IN (SELECT id FROM PACKS WHERE created_by_user_id = ?)",
            (user_id, user_id),
            ["PACKS"],
        ),
        (
            "ENTITLEMENTS",
            "other users' entitlements to this user's assets",
            "user_id <> ? AND ("
            "(asset_type = 'prompt' AND asset_id IN (SELECT id FROM PROMPTS WHERE created_by_user_id = ?)) "
            "OR (asset_type = 'pack' AND asset_id IN (SELECT id FROM PACKS WHERE created_by_user_id = ?)))",
            (user_id, user_id, user_id),
            ["PROMPTS", "PACKS"],
        ),
        (
            "PROMPT_PERMISSIONS",
            "other users granted access to this user's prompts",
            "user_id <> ? AND prompt_id IN (SELECT id FROM PROMPTS WHERE created_by_user_id = ?)",
            (user_id, user_id),
            ["PROMPTS"],
        ),
        (
            "USER_CREATOR_RELATIONSHIPS",
            "this user is a creator with existing customers",
            "creator_id = ?",
            (user_id,),
            [],
        ),
    ]

    blocks: list[dict[str, Any]] = []
    for table, reason, where, params, required in checks:
        if not await _table_exists(conn, table):
            continue
        if not await _all_exist(conn, required):
            continue
        count = await _count(conn, table, where, params)
        if count > 0:
            blocks.append({"table": table, "reason": reason, "count": count})
    return blocks


async def _has_active_reservation(conn: aiosqlite.Connection, user_id: int) -> bool:
    """True when an in-flight provider reservation holds this user's funds.

    Existence-guarded: partial schemas (test DBs) simply have no reservations.
    """
    if not await _table_exists(conn, "BILLING_USAGE_RESERVATIONS"):
        return False
    cursor = await conn.execute(
        "SELECT 1 FROM BILLING_USAGE_RESERVATIONS "
        "WHERE status = 'active' AND (user_id = ? OR billing_account_id = ?) "
        "LIMIT 1",
        (user_id, user_id),
    )
    return await cursor.fetchone() is not None


async def _has_active_phone_call(conn: aiosqlite.Connection, user_id: int) -> bool:
    """Fail closed before account cascades can orphan an active provider call."""

    if not await _table_exists(conn, "PHONE_CALLS"):
        return False
    cursor = await conn.execute(
        """
        SELECT 1 FROM PHONE_CALLS
        WHERE owner_user_id=? AND deleted_at IS NULL
          AND status NOT IN ('completed','busy','no_answer','machine','failed','canceled')
        LIMIT 1
        """,
        (int(user_id),),
    )
    return await cursor.fetchone() is not None


async def _atagia_link_count(conn: aiosqlite.Connection, user_id: int) -> int:
    total = 0
    for table in ("MEMORY_PROVIDER_MESSAGE_LINKS", "MEMORY_PROVIDER_CONVERSATION_LINKS"):
        if not await _table_exists(conn, table):
            continue
        cursor = await conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE user_id = ? AND provider = ?",
            (user_id, ATAGIA_PROVIDER),
        )
        total += int((await cursor.fetchone())[0])
    return total


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #


async def _capture_counts(conn: aiosqlite.Connection, user_id: int) -> dict[str, int]:
    specs: list[tuple[str, str, str]] = [
        ("conversations", "CONVERSATIONS", "user_id = ?"),
        (
            "messages",
            "MESSAGES",
            "conversation_id IN (SELECT id FROM CONVERSATIONS WHERE user_id = ?)",
        ),
        ("memory_message_links", "MEMORY_PROVIDER_MESSAGE_LINKS", "user_id = ?"),
        ("memory_conversation_links", "MEMORY_PROVIDER_CONVERSATION_LINKS", "user_id = ?"),
        ("activity_sessions", "USER_ACTIVITY_SESSIONS", "user_id = ?"),
        ("wellbeing_events", "USER_WELLBEING_EVENTS", "user_id = ?"),
        ("elevenlabs_sessions", "ELEVENLABS_CALL_SESSIONS", "user_id = ?"),
        (
            "content_reports",
            "CONTENT_REPORTS",
            "reporter_user_id = ? OR target_owner_user_id = ?",
        ),
        ("prompts", "PROMPTS", "created_by_user_id = ?"),
        ("packs", "PACKS", "created_by_user_id = ?"),
        ("transactions", "TRANSACTIONS", "user_id = ?"),
        ("chat_folders", "CHAT_FOLDERS", "user_id = ?"),
        ("magic_links", "MAGIC_LINKS", "user_id = ?"),
    ]
    counts: dict[str, int] = {}
    for label, table, where in specs:
        if not await _table_exists(conn, table):
            continue
        params = tuple([user_id] * where.count("?"))
        counts[label] = await _count(conn, table, where, params)
    return counts


async def _elevenlabs_session_ids(conn: aiosqlite.Connection, user_id: int) -> list[str]:
    ids: set[str] = set()
    if await _table_exists(conn, "ELEVENLABS_CALL_SESSIONS"):
        cursor = await conn.execute(
            "SELECT session_id FROM ELEVENLABS_CALL_SESSIONS WHERE user_id = ?",
            (user_id,),
        )
        ids.update(str(row[0]) for row in await cursor.fetchall() if row[0])
    if await _column_exists(conn, "CONVERSATIONS", "elevenlabs_session_id"):
        cursor = await conn.execute(
            "SELECT elevenlabs_session_id FROM CONVERSATIONS "
            "WHERE user_id = ? AND elevenlabs_session_id IS NOT NULL",
            (user_id,),
        )
        ids.update(str(row[0]) for row in await cursor.fetchall() if row[0])
    return sorted(ids)


# --------------------------------------------------------------------------- #
# Local deletion
# --------------------------------------------------------------------------- #

# Source columns read before archiving. Identity columns (user_id) are deliberately
# excluded, and every free-form value is converted to a keyed fingerprint before
# it enters FINANCIAL_ARCHIVE. Numeric ledger values and timestamps remain readable
# for financial reconciliation.
_TRANSACTION_SNAPSHOT_COLUMNS = (
    "id",
    "type",
    "amount",
    "balance_before",
    "balance_after",
    "description",
    "reference_id",
    "discount_code",
    "created_at",
)

_ARCHIVE_FINGERPRINT_PREFIX = "hmac-sha256:fa-v1:"
_ARCHIVE_FINGERPRINT_PATTERN = re.compile(
    rf"^{re.escape(_ARCHIVE_FINGERPRINT_PREFIX)}[0-9a-f]{{64}}$"
)
_ARCHIVE_KEY_DOMAIN = b"aurvek-financial-archive-key\0v1"
_ARCHIVE_VALUE_DOMAIN = b"aurvek-financial-archive\0v1\0"


def _is_archive_fingerprint(value: Any) -> bool:
    """Return whether ``value`` is a complete fingerprint emitted by this version."""
    return (
        isinstance(value, str)
        and _ARCHIVE_FINGERPRINT_PATTERN.fullmatch(value) is not None
    )


def _archive_field_fingerprint(
    field_name: str,
    value: Any,
    *,
    preserve_existing: bool = False,
) -> str | None:
    """Return a deterministic keyed fingerprint for an archive field value.

    The field name is bound into the HMAC so the same plaintext used in two
    different fields does not produce the same archive value. Operators who know
    an original payment reference can still reconcile it by recomputing the
    fingerprint, while the archive never stores that reference, discount code, or
    free-form description in plaintext.
    """
    if value is None:
        return None
    value_text = str(value)
    if preserve_existing and _is_archive_fingerprint(value_text):
        return value_text
    if not PEPPER:
        raise RuntimeError(
            "PEPPER is required to pseudonymize archived financial fields"
        )
    field_bytes = field_name.encode("utf-8")
    value_bytes = value_text.encode("utf-8")
    archive_key = hmac.new(
        PEPPER.encode("utf-8"),
        _ARCHIVE_KEY_DOMAIN,
        hashlib.sha256,
    ).digest()
    payload = b"".join(
        (
            _ARCHIVE_VALUE_DOMAIN,
            len(field_bytes).to_bytes(4, "big"),
            field_bytes,
            len(value_bytes).to_bytes(8, "big"),
            value_bytes,
        )
    )
    digest = hmac.new(
        archive_key,
        payload,
        hashlib.sha256,
    ).hexdigest()
    return f"{_ARCHIVE_FINGERPRINT_PREFIX}{digest}"


def _transaction_reference_fingerprint(transaction_type: Any, value: Any) -> str | None:
    """Fingerprint a transaction reference without double-HMACing paired transfers."""
    preserve_existing = str(transaction_type) in {
        "balance_transfer_in",
        "balance_transfer_out",
    }
    return _archive_field_fingerprint(
        "transaction-reference",
        value,
        preserve_existing=preserve_existing,
    )


def _deleted_user_ref(user_id: int) -> str:
    """Return a deterministic, keyed pseudonym for a deleted user's archive rows.

    Keyed HMAC over the same PEPPER the app uses for directory hashing:
    deterministic (all of one user's rows share a ref) yet not dictionary-
    enumerable. Fail fast if the pepper is missing/empty.
    """
    if not PEPPER:
        raise RuntimeError(
            "PEPPER is required to pseudonymize archived financial rows"
        )
    return hmac.new(
        PEPPER.encode(),
        f"deleted-user:{user_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


async def _archive_transactions(conn: aiosqlite.Connection, user_id: int) -> int:
    """Pseudonymize the user's TRANSACTIONS into FINANCIAL_ARCHIVE, then delete them.

    TRANSACTIONS are single-party wallet ledger rows (user_id only). They are not a
    counterparty's record, so they can be removed on account deletion -- but a
    pseudonymized, keyed snapshot is retained for financial/audit reconciliation.
    Returns the number of archived (and deleted) rows.
    """
    if not await _table_exists(conn, "TRANSACTIONS"):
        return 0
    columns = ", ".join(_TRANSACTION_SNAPSHOT_COLUMNS)
    cursor = await conn.execute(
        f"SELECT {columns} FROM TRANSACTIONS WHERE user_id = ?",
        (user_id,),
    )
    rows = await cursor.fetchall()
    if not rows:
        return 0
    if not await _table_exists(conn, "FINANCIAL_ARCHIVE"):
        # Hard dependency of the current deletion policy; fail fast rather than
        # silently plain-delete financial rows without an archive.
        raise RuntimeError(
            "FINANCIAL_ARCHIVE table is missing; run migration_financial_archive.py"
        )

    user_ref = _deleted_user_ref(user_id)
    for row in rows:
        reference_fingerprint = _transaction_reference_fingerprint(
            row["type"],
            row["reference_id"],
        )
        snapshot = {
            "archive_format_version": 2,
            "type": row["type"],
            "amount": row["amount"],
            "balance_before": row["balance_before"],
            "balance_after": row["balance_after"],
            "description_fingerprint": _archive_field_fingerprint(
                "transaction-description", row["description"]
            ),
            "reference_id_fingerprint": reference_fingerprint,
            "discount_code_fingerprint": _archive_field_fingerprint(
                "transaction-discount-code", row["discount_code"]
            ),
            "created_at": row["created_at"],
        }
        await conn.execute(
            "INSERT INTO FINANCIAL_ARCHIVE ("
            "deleted_user_ref, source_table, source_row_id, amount, currency, "
            "payment_reference, occurred_at, row_snapshot"
            ") VALUES (?, 'TRANSACTIONS', ?, ?, NULL, ?, ?, ?)",
            (
                user_ref,
                row["id"],
                row["amount"],
                reference_fingerprint,
                row["created_at"],
                json.dumps(snapshot),
            ),
        )
    await conn.execute("DELETE FROM TRANSACTIONS WHERE user_id = ?", (user_id,))
    return len(rows)


async def _redact_counterparty_balance_transfers(
    conn: aiosqlite.Connection, user_id: int
) -> int:
    """Remove the deleted account identity from surviving transfer ledger rows.

    Initial-balance transfers create one row per party with an exact shared
    reference. Older references and descriptions embedded account ids. Match only
    the counterpart of a transfer owned by the account being deleted, then replace
    its reference with the same keyed fingerprint used by FINANCIAL_ARCHIVE and its
    description with non-identifying ledger text. Exact reference equality avoids
    partial-id mistakes such as confusing user 8 with user 84.
    """
    if not await _table_exists(conn, "TRANSACTIONS"):
        return 0

    cursor = await conn.execute(
        "SELECT type, reference_id FROM TRANSACTIONS "
        "WHERE user_id = ? "
        "AND type IN ('balance_transfer_in', 'balance_transfer_out') "
        "AND reference_id IS NOT NULL",
        (user_id,),
    )
    transfer_rows = await cursor.fetchall()
    updated = 0
    counterpart_metadata = {
        "balance_transfer_in": (
            "balance_transfer_out",
            "Balance transfer sent",
        ),
        "balance_transfer_out": (
            "balance_transfer_in",
            "Balance transfer received",
        ),
    }
    for row in transfer_rows:
        source_type = str(row["type"])
        raw_reference = str(row["reference_id"])
        counterpart_type, safe_description = counterpart_metadata[source_type]
        safe_reference = _transaction_reference_fingerprint(
            source_type,
            raw_reference,
        )
        update_cursor = await conn.execute(
            "UPDATE TRANSACTIONS SET description = ?, reference_id = ? "
            "WHERE user_id <> ? AND type = ? AND reference_id = ?",
            (
                safe_description,
                safe_reference,
                user_id,
                counterpart_type,
                raw_reference,
            ),
        )
        updated += max(0, update_cursor.rowcount)
    return updated


async def _delete_local_rows(
    conn: aiosqlite.Connection, user_id: int, conversation_ids: list[int]
) -> int:
    """Delete all of the user's local rows. Returns the archived TRANSACTIONS count."""
    # A paired initial-balance transaction survives in the counterparty's ledger.
    # Redact it before archiving/deleting this user's half, inside the same atomic
    # transaction so either both privacy changes commit or neither does.
    await _redact_counterparty_balance_transfers(conn, user_id)

    # Archive single-party wallet rows (pseudonymized) before anything else, so a
    # missing pepper or archive table fails fast before any row is removed.
    archived_transaction_count = await _archive_transactions(conn, user_id)

    prompt_subq = "(SELECT id FROM PROMPTS WHERE created_by_user_id = ?)"
    pack_subq = "(SELECT id FROM PACKS WHERE created_by_user_id = ?)"
    ext_subq = f"(SELECT id FROM PROMPT_EXTENSIONS WHERE prompt_id IN {prompt_subq})"

    # Cross-user cleanup: other users' data that references this user's prompts/packs
    # (nullify their conversations, drop cross-user FK rows). Each is skipped if any
    # table it touches is absent.
    cross_user: list[tuple[list[str], str, tuple[Any, ...]]] = [
        (
            ["WELCOME_MESSAGES", "PROMPTS"],
            "DELETE FROM WELCOME_MESSAGES WHERE entity_type = 'prompt' "
            f"AND entity_id IN {prompt_subq}",
            (user_id,),
        ),
        (
            ["WELCOME_MESSAGES", "PACKS"],
            "DELETE FROM WELCOME_MESSAGES WHERE entity_type = 'pack' "
            f"AND entity_id IN {pack_subq}",
            (user_id,),
        ),
        (["WELCOME_MESSAGE_READS"], "DELETE FROM WELCOME_MESSAGE_READS WHERE user_id = ?", (user_id,)),
        (
            ["USER_DETAILS"],
            "UPDATE USER_DETAILS SET current_alter_ego_id = NULL WHERE user_id = ?",
            (user_id,),
        ),
        (
            ["CONVERSATIONS", "PROMPTS"],
            f"UPDATE CONVERSATIONS SET role_id = NULL WHERE role_id IN {prompt_subq}",
            (user_id,),
        ),
        (
            ["CONVERSATIONS", "PROMPT_EXTENSIONS", "PROMPTS"],
            f"UPDATE CONVERSATIONS SET active_extension_id = NULL WHERE active_extension_id IN {ext_subq}",
            (user_id,),
        ),
        (
            ["USER_DETAILS", "PROMPTS"],
            f"UPDATE USER_DETAILS SET current_prompt_id = NULL WHERE current_prompt_id IN {prompt_subq}",
            (user_id,),
        ),
        (["PROMPT_PERMISSIONS", "PROMPTS"], f"DELETE FROM PROMPT_PERMISSIONS WHERE prompt_id IN {prompt_subq}", (user_id,)),
        (["PROMPT_PURCHASES", "PROMPTS"], f"DELETE FROM PROMPT_PURCHASES WHERE prompt_id IN {prompt_subq}", (user_id,)),
        (["ENTITLEMENTS", "PROMPTS"], f"DELETE FROM ENTITLEMENTS WHERE asset_type = 'prompt' AND asset_id IN {prompt_subq}", (user_id,)),
        (["PACK_ITEMS", "PROMPTS"], f"DELETE FROM PACK_ITEMS WHERE prompt_id IN {prompt_subq}", (user_id,)),
        (["CREATOR_EARNINGS", "PROMPTS"], f"DELETE FROM CREATOR_EARNINGS WHERE prompt_id IN {prompt_subq}", (user_id,)),
        (["WATCHDOG_EVENTS", "PROMPTS"], f"DELETE FROM WATCHDOG_EVENTS WHERE prompt_id IN {prompt_subq}", (user_id,)),
        (["WATCHDOG_STATE", "PROMPTS"], f"DELETE FROM WATCHDOG_STATE WHERE prompt_id IN {prompt_subq}", (user_id,)),
        (["PENDING_REGISTRATIONS", "PROMPTS"], f"DELETE FROM PENDING_REGISTRATIONS WHERE prompt_id IN {prompt_subq}", (user_id,)),
        (["PENDING_ENTITLEMENTS", "PROMPTS"], f"DELETE FROM PENDING_ENTITLEMENTS WHERE prompt_id IN {prompt_subq}", (user_id,)),
        (["PACK_PURCHASES", "PACKS"], f"DELETE FROM PACK_PURCHASES WHERE pack_id IN {pack_subq}", (user_id,)),
        (["ENTITLEMENTS", "PACKS"], f"DELETE FROM ENTITLEMENTS WHERE asset_type = 'pack' AND asset_id IN {pack_subq}", (user_id,)),
        (["PENDING_REGISTRATIONS", "PACKS"], f"DELETE FROM PENDING_REGISTRATIONS WHERE pack_id IN {pack_subq}", (user_id,)),
        (["PENDING_ENTITLEMENTS", "PACKS"], f"DELETE FROM PENDING_ENTITLEMENTS WHERE pack_id IN {pack_subq}", (user_id,)),
    ]
    for required, sql, params in cross_user:
        if await _all_exist(conn, required):
            await conn.execute(sql, params)

    # Per-conversation deletion: memory links, watchdog rows, attachments, sync
    # watermark, messages, then the conversation itself, in FK-safe order.
    for conversation_id in conversation_ids:
        await delete_conversation_rows(
            conn,
            conversation_id=conversation_id,
            user_id=user_id,
        )

    # User-level provider links, memory preferences, wellbeing and activity rows.
    for table in _USER_LEVEL_TABLES:
        if await _table_exists(conn, table):
            await conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))

    if await _table_exists(conn, "CONTENT_REPORTS"):
        await conn.execute(
            "DELETE FROM CONTENT_REPORTS WHERE reporter_user_id = ? OR target_owner_user_id = ?",
            (user_id, user_id),
        )

    # Same-user cascade for the remaining owned data.
    for table, condition, required in _CASCADE_TABLES:
        if not await _table_exists(conn, table):
            continue
        if not await _all_exist(conn, required):
            continue
        await conn.execute(f"DELETE FROM {table} WHERE {condition}", (user_id,))

    # Nullify references that survive the account. admin_id is intentionally NOT
    # nullified: it is NOT NULL and admin accounts are blocked from deletion.
    nullifications: list[tuple[list[str], str]] = [
        (["ADMIN_AUDIT_LOG"], "UPDATE ADMIN_AUDIT_LOG SET target_user_id = NULL WHERE target_user_id = ?"),
        (["USER_DETAILS"], "UPDATE USER_DETAILS SET billing_account_id = NULL WHERE billing_account_id = ?"),
        (["LANDING_PAGE_ANALYTICS"], "UPDATE LANDING_PAGE_ANALYTICS SET converted_user_id = NULL WHERE converted_user_id = ?"),
        (["USER_DETAILS"], "UPDATE USER_DETAILS SET created_by = NULL WHERE created_by = ?"),
    ]
    for required, sql in nullifications:
        if await _all_exist(conn, required):
            await conn.execute(sql, (user_id,))

    await conn.execute("DELETE FROM USER_DETAILS WHERE user_id = ?", (user_id,))
    await conn.execute("DELETE FROM USERS WHERE id = ?", (user_id,))

    return archived_transaction_count


# --------------------------------------------------------------------------- #
# Small DB helpers
# --------------------------------------------------------------------------- #


async def _resolve_user(
    conn: aiosqlite.Connection, *, user_id: int | None, username: str | None
) -> aiosqlite.Row | None:
    if user_id is not None:
        cursor = await conn.execute(
            "SELECT id, username, role_id FROM USERS WHERE id = ?", (int(user_id),)
        )
    else:
        cursor = await conn.execute(
            "SELECT id, username, role_id FROM USERS WHERE LOWER(username) = ?",
            (username.strip().lower(),),
        )
    return await cursor.fetchone()


async def _count(
    conn: aiosqlite.Connection, table: str, where: str, params: tuple[Any, ...]
) -> int:
    cursor = await conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params)
    return int((await cursor.fetchone())[0])


async def _int_ids(
    conn: aiosqlite.Connection, sql: str, params: tuple[Any, ...]
) -> list[int]:
    cursor = await conn.execute(sql, params)
    return [int(row[0]) for row in await cursor.fetchall()]


async def _table_exists(conn: aiosqlite.Connection, table_name: str) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? COLLATE NOCASE",
        (table_name,),
    )
    return await cursor.fetchone() is not None


async def _all_exist(conn: aiosqlite.Connection, tables: list[str]) -> bool:
    for table in tables:
        if not await _table_exists(conn, table):
            return False
    return True


async def _column_exists(
    conn: aiosqlite.Connection, table_name: str, column_name: str
) -> bool:
    if not await _table_exists(conn, table_name):
        return False
    cursor = await conn.execute(f"PRAGMA table_info({table_name})")
    return any(str(row[1]) == column_name for row in await cursor.fetchall())
