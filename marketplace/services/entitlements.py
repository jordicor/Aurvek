"""Generic asset entitlement helpers for marketplace use access."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


ASSET_TYPE_PROMPT = "prompt"
ASSET_TYPE_PACK = "pack"
SUPPORTED_ASSET_TYPES = {ASSET_TYPE_PROMPT, ASSET_TYPE_PACK}

STATUS_ACTIVE = "active"
STATUS_PENDING = "pending"
STATUS_EXPIRED = "expired"
STATUS_REVOKED = "revoked"
STATUS_REFUNDED = "refunded"
STATUS_SUSPENDED = "suspended"
INACTIVE_STATUSES = {STATUS_EXPIRED, STATUS_REVOKED, STATUS_REFUNDED, STATUS_SUSPENDED}


class EntitlementError(ValueError):
    """Raised when an entitlement operation receives invalid input."""


@dataclass(frozen=True)
class EntitlementGrantResult:
    entitlement_id: int
    created: bool
    status: str


def utc_now_db() -> str:
    """Return a UTC timestamp in SQLite-friendly text format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def active_entitlement_condition(alias: str = "e") -> str:
    """SQL predicate for an entitlement that is usable at query time."""
    return (
        f"{alias}.status = 'active' "
        f"AND ({alias}.starts_at IS NULL OR julianday({alias}.starts_at) <= julianday('now')) "
        f"AND ({alias}.expires_at IS NULL OR julianday({alias}.expires_at) > julianday('now'))"
    )


def _validate_asset_type(asset_type: str) -> None:
    if asset_type not in SUPPORTED_ASSET_TYPES:
        raise EntitlementError(f"Unsupported entitlement asset_type: {asset_type}")


def _metadata_to_json(metadata: Mapping[str, Any] | None) -> str | None:
    if not metadata:
        return None
    return json.dumps(dict(metadata), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


async def _fetchone(db, sql: str, params: tuple = ()):
    cursor = await db.execute(sql, params)
    return await cursor.fetchone()


async def _asset_exists(db, asset_type: str, asset_id: int) -> bool:
    _validate_asset_type(asset_type)
    if asset_type == ASSET_TYPE_PROMPT:
        row = await _fetchone(db, "SELECT 1 FROM PROMPTS WHERE id = ?", (asset_id,))
        return row is not None
    if asset_type == ASSET_TYPE_PACK:
        row = await _fetchone(db, "SELECT 1 FROM PACKS WHERE id = ?", (asset_id,))
        return row is not None
    return False


async def grant_entitlement(
    db,
    *,
    user_id: int,
    asset_type: str,
    asset_id: int,
    source: str,
    source_ref_type: str | None = None,
    source_ref_id: str | int | None = None,
    starts_at: str | None = None,
    expires_at: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    created_by_user_id: int | None = None,
    reactivate_inactive: bool = False,
    validate_asset: bool = True,
) -> EntitlementGrantResult:
    """Grant asset use access idempotently.

    The caller owns transactions and commits. Existing inactive purchase/source
    rows are not reactivated unless explicitly requested by admin/manual flows.
    """
    _validate_asset_type(asset_type)
    if user_id <= 0 or asset_id <= 0:
        raise EntitlementError("user_id and asset_id must be positive")
    if not source:
        raise EntitlementError("source is required")
    if validate_asset and not await _asset_exists(db, asset_type, asset_id):
        raise EntitlementError(f"{asset_type} {asset_id} does not exist")

    source_ref_id_text = str(source_ref_id) if source_ref_id is not None else None
    now = utc_now_db()
    starts_at = starts_at or now
    metadata_json = _metadata_to_json(metadata)

    existing = None
    if source_ref_type and source_ref_id_text:
        existing = await _fetchone(
            db,
            """SELECT id, status FROM ENTITLEMENTS
               WHERE user_id = ? AND asset_type = ? AND asset_id = ?
                 AND source_ref_type = ? AND source_ref_id = ?
               ORDER BY id DESC LIMIT 1""",
            (user_id, asset_type, asset_id, source_ref_type, source_ref_id_text),
        )
    else:
        existing = await _fetchone(
            db,
            """SELECT id, status FROM ENTITLEMENTS
               WHERE user_id = ? AND asset_type = ? AND asset_id = ?
                 AND source = ? AND source_ref_type IS NULL AND source_ref_id IS NULL
               ORDER BY id DESC LIMIT 1""",
            (user_id, asset_type, asset_id, source),
        )

    if existing:
        entitlement_id = int(existing["id"] if hasattr(existing, "keys") else existing[0])
        existing_status = existing["status"] if hasattr(existing, "keys") else existing[1]
        if existing_status != STATUS_ACTIVE and reactivate_inactive:
            await db.execute(
                """UPDATE ENTITLEMENTS
                   SET status = 'active', starts_at = ?, expires_at = ?,
                       metadata_json = COALESCE(?, metadata_json),
                       updated_at = ?, revoked_at = NULL, revoked_by_user_id = NULL
                   WHERE id = ?""",
                (starts_at, expires_at, metadata_json, now, entitlement_id),
            )
            return EntitlementGrantResult(entitlement_id, False, STATUS_ACTIVE)
        return EntitlementGrantResult(entitlement_id, False, existing_status)

    cursor = await db.execute(
        """INSERT INTO ENTITLEMENTS
           (user_id, asset_type, asset_id, source, source_ref_type, source_ref_id,
            starts_at, expires_at, status, metadata_json, created_at, updated_at,
            created_by_user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
        (
            user_id,
            asset_type,
            asset_id,
            source,
            source_ref_type,
            source_ref_id_text,
            starts_at,
            expires_at,
            metadata_json,
            now,
            now,
            created_by_user_id,
        ),
    )
    return EntitlementGrantResult(int(cursor.lastrowid), True, STATUS_ACTIVE)


async def grant_prompt_entitlement(db, *, user_id: int, prompt_id: int, source: str, **kwargs) -> EntitlementGrantResult:
    return await grant_entitlement(
        db,
        user_id=user_id,
        asset_type=ASSET_TYPE_PROMPT,
        asset_id=prompt_id,
        source=source,
        **kwargs,
    )


async def grant_pack_entitlement(db, *, user_id: int, pack_id: int, source: str, **kwargs) -> EntitlementGrantResult:
    return await grant_entitlement(
        db,
        user_id=user_id,
        asset_type=ASSET_TYPE_PACK,
        asset_id=pack_id,
        source=source,
        **kwargs,
    )


async def revoke_entitlement(
    db,
    *,
    user_id: int,
    asset_type: str,
    asset_id: int,
    status: str = STATUS_REVOKED,
    source_ref_type: str | None = None,
    source_ref_id: str | int | None = None,
    revoked_by_user_id: int | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> int:
    """Mark active entitlements inactive or finalize revoked ones as refunded.

    Merge event metadata into the existing evidence and retain the original
    revocation attribution. Returns affected rows; the caller owns the commit.
    """
    _validate_asset_type(asset_type)
    if status not in INACTIVE_STATUSES:
        raise EntitlementError(f"Invalid revoke status: {status}")

    now = utc_now_db()
    params: list[Any] = [status, now, now, revoked_by_user_id]
    metadata_json = _metadata_to_json(metadata)
    set_metadata = ""
    if metadata_json is not None:
        set_metadata = ", metadata_json = json_patch(COALESCE(metadata_json, '{}'), ?)"
        params.append(metadata_json)

    where = "user_id = ? AND asset_type = ? AND asset_id = ?"
    if status == STATUS_REFUNDED:
        where += " AND status IN ('active', 'revoked')"
    else:
        where += " AND status = 'active'"
    params.extend([user_id, asset_type, asset_id])
    if source_ref_type and source_ref_id is not None:
        where += " AND source_ref_type = ? AND source_ref_id = ?"
        params.extend([source_ref_type, str(source_ref_id)])

    cursor = await db.execute(
        f"""UPDATE ENTITLEMENTS
            SET status = ?, revoked_at = COALESCE(revoked_at, ?), updated_at = ?,
                revoked_by_user_id = COALESCE(?, revoked_by_user_id){set_metadata}
            WHERE {where}""",
        tuple(params),
    )
    return int(cursor.rowcount if cursor.rowcount is not None else 0)


async def refund_entitlement(db, *, user_id: int, asset_type: str, asset_id: int, **kwargs) -> int:
    return await revoke_entitlement(
        db,
        user_id=user_id,
        asset_type=asset_type,
        asset_id=asset_id,
        status=STATUS_REFUNDED,
        **kwargs,
    )


async def user_has_direct_entitlement(db, *, user_id: int, asset_type: str, asset_id: int) -> bool:
    _validate_asset_type(asset_type)
    if not await _asset_exists(db, asset_type, asset_id):
        return False
    row = await _fetchone(
        db,
        f"""SELECT 1 FROM ENTITLEMENTS e
            WHERE e.user_id = ? AND e.asset_type = ? AND e.asset_id = ?
              AND {active_entitlement_condition("e")}
            LIMIT 1""",
        (user_id, asset_type, asset_id),
    )
    return row is not None


async def user_has_prompt_access(db, *, user_id: int, prompt_id: int, include_owner_editor: bool = True) -> bool:
    """Check direct prompt use access, including inherited pack entitlements."""
    if include_owner_editor:
        row = await _fetchone(
            db,
            """SELECT 1 FROM PROMPT_PERMISSIONS
               WHERE prompt_id = ? AND user_id = ?
                 AND permission_level IN ('owner', 'edit')
               LIMIT 1""",
            (prompt_id, user_id),
        )
        if row is not None:
            return True

    if await user_has_direct_entitlement(
        db,
        user_id=user_id,
        asset_type=ASSET_TYPE_PROMPT,
        asset_id=prompt_id,
    ):
        return True

    row = await _fetchone(
        db,
        f"""SELECT 1
            FROM ENTITLEMENTS e
            JOIN PACK_ITEMS pi ON pi.pack_id = e.asset_id
            JOIN PROMPTS p ON p.id = pi.prompt_id
            WHERE e.user_id = ?
              AND e.asset_type = 'pack'
              AND pi.prompt_id = ?
              AND pi.is_active = 1
              AND (pi.disable_at IS NULL OR julianday(pi.disable_at) > julianday('now'))
              AND {active_entitlement_condition("e")}
            LIMIT 1""",
        (user_id, prompt_id),
    )
    return row is not None


async def user_has_pack_access(db, *, user_id: int, pack_id: int, include_pack_owner: bool = True) -> bool:
    """Check pack use access, keeping pack ownership as management auth."""
    if include_pack_owner:
        row = await _fetchone(db, "SELECT created_by_user_id FROM PACKS WHERE id = ?", (pack_id,))
        if row is None:
            return False
        owner_id = row["created_by_user_id"] if hasattr(row, "keys") else row[0]
        if owner_id == user_id:
            return True

    return await user_has_direct_entitlement(
        db,
        user_id=user_id,
        asset_type=ASSET_TYPE_PACK,
        asset_id=pack_id,
    )


async def get_user_entitled_pack_ids(db, user_id: int, *, limit: int | None = None) -> list[int]:
    limit_sql = " LIMIT ?" if limit is not None else ""
    params: list[Any] = [user_id]
    if limit is not None:
        params.append(limit)
    cursor = await db.execute(
        f"""SELECT e.asset_id
            FROM ENTITLEMENTS e
            JOIN PACKS p ON p.id = e.asset_id
            WHERE e.user_id = ? AND e.asset_type = 'pack'
              AND {active_entitlement_condition("e")}
            ORDER BY e.created_at DESC, e.id DESC{limit_sql}""",
        tuple(params),
    )
    rows = await cursor.fetchall()
    return [int(row[0]) for row in rows]


async def get_user_entitled_prompt_ids(db, user_id: int, *, include_pack_items: bool = True) -> list[int]:
    prompt_ids: set[int] = set()
    cursor = await db.execute(
        f"""SELECT e.asset_id
            FROM ENTITLEMENTS e
            JOIN PROMPTS p ON p.id = e.asset_id
            WHERE e.user_id = ? AND e.asset_type = 'prompt'
              AND {active_entitlement_condition("e")}""",
        (user_id,),
    )
    prompt_ids.update(int(row[0]) for row in await cursor.fetchall())

    if include_pack_items:
        cursor = await db.execute(
            f"""SELECT pi.prompt_id
                FROM ENTITLEMENTS e
                JOIN PACK_ITEMS pi ON pi.pack_id = e.asset_id
                JOIN PROMPTS p ON p.id = pi.prompt_id
                WHERE e.user_id = ? AND e.asset_type = 'pack'
                  AND pi.is_active = 1
                  AND (pi.disable_at IS NULL OR julianday(pi.disable_at) > julianday('now'))
                  AND {active_entitlement_condition("e")}""",
            (user_id,),
        )
        prompt_ids.update(int(row[0]) for row in await cursor.fetchall())

    return sorted(prompt_ids)
