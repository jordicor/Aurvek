from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass
from time import monotonic
from typing import Iterable
from uuid import uuid4

import orjson
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from auth import get_user_by_id
from ai_runtime.messages import process_save_message
from billing.usage_reservations import serialize_user_billing_response
from chat.services.file_inputs import validate_and_compress_image
from chat.services.conversations import create_conversation_core
from common import MAX_IMAGE_UPLOAD_SIZE, decrypt_api_key
from database import get_db_connection
from integrations.telephony.channel_context import capture_non_phone_channel_turn
from integrations.devices.schemas import (
    BASIC_CAPABILITIES,
    DEVICE_RESPONSE_MODES,
    DEVICE_TOKEN_PREFIX,
    MAX_DEVICE_MESSAGE_CHARS,
)
from log_config import logger
from rediscfg import increment_metric, increment_user_activity, redis_client
from wellbeing_service import get_active_pause


SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")
ICON_CLASS_RE = re.compile(r"^(?:fa[bsr]?|fas|far|fab) fa-[a-z0-9-]+$")
ACTION_BLOCK_RE = re.compile(
    r"(?:\[AURVEK_ACTIONS\](.*?)\[/AURVEK_ACTIONS\]|```aurvek-actions\s*(.*?)```)",
    re.IGNORECASE | re.DOTALL,
)
SAFE_SNAPSHOT_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_DEVICE_SNAPSHOTS = 4
MAX_DEVICE_ACTIONS = 5
MAX_ACTION_TEXT_CHARS = 1000
MAX_DEVICE_MESSAGE_BODY_BYTES = int(
    os.getenv(
        "MAX_DEVICE_MESSAGE_BODY_SIZE",
        str(((MAX_IMAGE_UPLOAD_SIZE * MAX_DEVICE_SNAPSHOTS * 4 + 2) // 3) + (512 * 1024)),
    )
)
DEVICE_PROCESSING_EVENT_STALE_SECONDS = int(
    os.getenv("DEVICE_PROCESSING_EVENT_STALE_SECONDS", "900")
)
DEVICE_EVENT_RETENTION_DAYS = int(os.getenv("DEVICE_EVENT_RETENTION_DAYS", "90"))
DEVICE_EVENT_PRUNE_INTERVAL_SECONDS = int(
    os.getenv("DEVICE_EVENT_PRUNE_INTERVAL_SECONDS", "86400")
)


class DeviceValidationError(ValueError):
    """Raised for user-correctable device admin validation errors."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "validation_error",
        status_code: int = 400,
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class DeviceRuntimeError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class DevicesAdminOverview:
    total_devices: int
    enabled_devices: int
    seen_recently: int
    group_count: int
    messages_today: int
    recent_events: list[dict]


@dataclass(frozen=True)
class DeviceTokenResult:
    token: str
    device_id: int
    conversation_id: int | None = None


@dataclass(frozen=True)
class AuthenticatedDevice:
    id: int
    user_id: int
    slug: str
    display_name: str
    device_type: str
    enabled: bool
    capabilities: dict
    metadata: dict


@dataclass(frozen=True)
class DeviceBindingResult:
    status: str
    conversation_id: int | None
    source: str | None = None
    conversation_name: str | None = None
    group_name: str | None = None
    groups: list[str] | None = None


_device_turn_locks: dict[int, asyncio.Lock] = {}
_device_turn_locks_guard = asyncio.Lock()
_last_device_event_prune_monotonic = 0.0


async def acquire_device_turn_lock(conversation_id: int) -> asyncio.Lock | None:
    # Production currently runs one worker; this in-memory lock will not protect
    # multi-worker deployments. It keeps the first device runtime version honest.
    async with _device_turn_locks_guard:
        lock = _device_turn_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            _device_turn_locks[conversation_id] = lock
        if lock.locked():
            return None
        await lock.acquire()
        return lock


async def release_device_turn_lock(conversation_id: int, lock: asyncio.Lock | None) -> None:
    if lock is None:
        return
    async with _device_turn_locks_guard:
        current = _device_turn_locks.get(conversation_id)
        if lock.locked():
            lock.release()
        if current is lock and not lock.locked():
            _device_turn_locks.pop(conversation_id, None)


def _clean_text(value: str | None, *, max_len: int, field: str) -> str:
    cleaned = "" if value is None else str(value).strip()
    if len(cleaned) > max_len:
        raise DeviceValidationError(f"{field} is too long")
    return cleaned


def _clean_filename(value: str | None, *, fallback: str) -> str:
    filename = os.path.basename(str(value or "").strip())[:120]
    filename = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip(".-")
    return filename or fallback


def validate_slug(value: str | None, *, field: str = "Slug") -> str:
    slug = _clean_text(value, max_len=80, field=field).lower()
    if not slug or not SLUG_RE.fullmatch(slug):
        raise DeviceValidationError(
            f"{field} must use lowercase letters, numbers, and hyphens."
        )
    return slug


def _validate_icon_class(value: str | None) -> str | None:
    icon_class = _clean_text(value, max_len=80, field="Icon class")
    if not icon_class:
        return None
    if not ICON_CLASS_RE.fullmatch(icon_class):
        raise DeviceValidationError("Icon class must be a Font Awesome class.")
    return icon_class


def capabilities_json(selected: Iterable[str] | None) -> str:
    selected_set = {str(item).strip() for item in selected or []}
    payload = {name: name in selected_set for name in BASIC_CAPABILITIES}
    return orjson.dumps(payload).decode("utf-8")


def parse_capabilities(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = orjson.loads(value)
    except orjson.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_object(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = orjson.loads(value)
    except orjson.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def generate_device_token() -> str:
    return DEVICE_TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_device_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_prefix(token: str) -> str:
    return token[:16]


def extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise DeviceRuntimeError("unauthorized", "Invalid device token", 401)
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise DeviceRuntimeError("unauthorized", "Invalid device token", 401)
    token = token.strip()
    if not token.startswith(DEVICE_TOKEN_PREFIX):
        raise DeviceRuntimeError("unauthorized", "Invalid device token", 401)
    return token


async def authenticate_device_token(token: str) -> AuthenticatedDevice:
    prefix = token_prefix(token)
    candidate_hash = hash_device_token(token)
    async with get_db_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT d.id, d.user_id, d.slug, d.display_name, d.device_type, d.enabled,
                   d.capabilities_json, d.metadata_json, d.token_hash,
                   COALESCE(u.is_enabled, 1) AS owner_enabled
            FROM EXTERNAL_DEVICES d
            JOIN USERS u ON u.id = d.user_id
            WHERE token_prefix = ?
              AND COALESCE(json_extract(d.metadata_json, '$.deleted'), 0) = 0
            """,
            (prefix,),
        )
        rows = await cursor.fetchall()

        matched = None
        for row in rows:
            if secrets.compare_digest(str(row["token_hash"]), candidate_hash):
                matched = row
                break

        if matched is None:
            raise DeviceRuntimeError("unauthorized", "Invalid device token", 401)
        if not bool(matched["enabled"]):
            raise DeviceRuntimeError("device_disabled", "Device is disabled", 403)
        if not bool(matched["owner_enabled"]):
            raise DeviceRuntimeError("owner_disabled", "Device owner is disabled", 403)

        await conn.execute(
            """
            UPDATE EXTERNAL_DEVICES
            SET last_seen_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (matched["id"],),
        )
        await conn.commit()

    return AuthenticatedDevice(
        id=int(matched["id"]),
        user_id=int(matched["user_id"]),
        slug=matched["slug"],
        display_name=matched["display_name"],
        device_type=matched["device_type"],
        enabled=bool(matched["enabled"]),
        capabilities=parse_capabilities(matched["capabilities_json"]),
        metadata=_json_object(matched["metadata_json"]),
    )


async def _fetch_owner_user(owner_user_id: int):
    owner = await get_user_by_id(owner_user_id)
    if owner is None:
        raise DeviceValidationError("Owner user not found")
    if not owner.is_enabled:
        raise DeviceValidationError("Owner user is disabled")
    return owner


async def _validate_conversation_owner(
    cursor,
    conversation_id: int,
    user_id: int,
    *,
    allow_classic_platform: bool = True,
) -> None:
    await cursor.execute(
        "SELECT id FROM CONVERSATIONS WHERE id = ? AND user_id = ?",
        (conversation_id, user_id),
    )
    if not await cursor.fetchone():
        raise DeviceValidationError(
            "Conversation not found for selected owner",
            code="conversation_not_found",
            status_code=404,
        )
    if not allow_classic_platform:
        classic_platform = await _classic_platform_for_conversation(
            cursor,
            conversation_id=conversation_id,
            user_id=user_id,
        )
        if classic_platform:
            raise DeviceValidationError(
                "External devices cannot be attached to WhatsApp or Telegram conversations",
                code="classic_platform_conflict",
                status_code=409,
            )


async def _classic_platform_for_conversation(
    cursor,
    *,
    conversation_id: int,
    user_id: int,
) -> str | None:
    await cursor.execute(
        """
        SELECT
            CASE
                WHEN CAST(json_extract(external_platforms, '$.whatsapp.conversation_id') AS TEXT) = CAST(? AS TEXT)
                    THEN 'whatsapp'
                WHEN CAST(json_extract(external_platforms, '$.telegram.conversation_id') AS TEXT) = CAST(? AS TEXT)
                    THEN 'telegram'
                ELSE NULL
            END AS external_platform
        FROM USER_DETAILS
        WHERE user_id = ?
        """,
        (conversation_id, conversation_id, user_id),
    )
    row = await cursor.fetchone()
    return row["external_platform"] if row and row["external_platform"] else None


def _empty_conversation_bindings() -> dict:
    return {
        "effective_count": 0,
        "effective_devices": [],
        "assigned_devices": [],
        "assigned_groups": [],
        "tooltip": "",
    }


def _finalize_conversation_binding_summary(summary: dict) -> dict:
    effective_devices = sorted(
        summary["effective_devices"],
        key=lambda item: ((item.get("display_name") or "").lower(), int(item.get("id") or 0)),
    )
    assigned_devices = sorted(
        summary["assigned_devices"],
        key=lambda item: ((item.get("display_name") or "").lower(), int(item.get("id") or 0)),
    )
    assigned_groups = sorted(
        summary["assigned_groups"],
        key=lambda item: ((item.get("name") or "").lower(), int(item.get("id") or 0)),
    )
    tooltip_parts = []
    for device in effective_devices:
        label = device.get("display_name") or device.get("slug") or "Device"
        if device.get("source") == "group" and device.get("group_name"):
            label = f"{label} via {device['group_name']}"
        tooltip_parts.append(label)
    return {
        "effective_count": len(effective_devices),
        "effective_devices": effective_devices,
        "assigned_devices": assigned_devices,
        "assigned_groups": assigned_groups,
        "tooltip": ", ".join(tooltip_parts),
    }


def _parse_id_list(values, field: str) -> list[int]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise DeviceValidationError(f"{field} must be a list")
    parsed = []
    for value in values:
        try:
            item_id = int(value)
        except (TypeError, ValueError):
            raise DeviceValidationError(f"{field} must contain numbers")
        if item_id <= 0:
            raise DeviceValidationError(f"{field} must contain positive IDs")
        parsed.append(item_id)
    return sorted(set(parsed))


async def _validate_groups_owner(cursor, group_ids: list[int], user_id: int) -> None:
    if not group_ids:
        return
    placeholders = ",".join("?" for _ in group_ids)
    await cursor.execute(
        f"""
        SELECT COUNT(*)
        FROM EXTERNAL_DEVICE_GROUPS
        WHERE user_id = ?
          AND id IN ({placeholders})
          AND COALESCE(json_extract(metadata_json, '$.deleted'), 0) = 0
        """,
        [user_id, *group_ids],
    )
    row = await cursor.fetchone()
    if int(row[0] or 0) != len(set(group_ids)):
        raise DeviceValidationError(
            "One or more groups do not belong to the owner",
            code="target_forbidden",
            status_code=403,
        )


async def _validate_device_group_same_owner(cursor, device_id: int, group_id: int) -> int:
    await cursor.execute(
        """
        SELECT d.user_id
        FROM EXTERNAL_DEVICES d
        JOIN EXTERNAL_DEVICE_GROUPS g ON g.id = ?
        WHERE d.id = ? AND g.user_id = d.user_id
          AND COALESCE(json_extract(d.metadata_json, '$.deleted'), 0) = 0
          AND COALESCE(json_extract(g.metadata_json, '$.deleted'), 0) = 0
        """,
        (group_id, device_id),
    )
    row = await cursor.fetchone()
    if not row:
        raise DeviceValidationError(
            "Device and group must belong to the same user",
            code="target_forbidden",
            status_code=403,
        )
    return int(row[0])


async def _target_owner(cursor, target_type: str, target_id: int) -> int:
    if target_type == "device":
        await cursor.execute(
            """
            SELECT user_id
            FROM EXTERNAL_DEVICES
            WHERE id = ?
              AND COALESCE(json_extract(metadata_json, '$.deleted'), 0) = 0
            """,
            (target_id,),
        )
    elif target_type == "group":
        await cursor.execute(
            """
            SELECT user_id
            FROM EXTERNAL_DEVICE_GROUPS
            WHERE id = ?
              AND COALESCE(json_extract(metadata_json, '$.deleted'), 0) = 0
            """,
            (target_id,),
        )
    else:
        raise DeviceValidationError("Invalid binding target")
    row = await cursor.fetchone()
    if not row:
        raise DeviceValidationError(
            "Binding target not found",
            code="binding_target_not_found",
            status_code=404,
        )
    return int(row[0])


async def resolve_device_binding(conn, device_id: int) -> dict:
    """Resolve direct/group routing for admin display and runtime reuse."""
    cursor = await conn.cursor()
    await cursor.execute(
        """
        SELECT b.conversation_id, c.chat_name
        FROM EXTERNAL_DEVICE_BINDINGS b
        JOIN EXTERNAL_DEVICES d ON d.id = b.target_id
        JOIN CONVERSATIONS c ON c.id = b.conversation_id
        WHERE b.target_type = 'device'
          AND b.target_id = ?
          AND b.user_id = d.user_id
          AND c.user_id = d.user_id
          AND COALESCE(json_extract(d.metadata_json, '$.deleted'), 0) = 0
        """,
        (device_id,),
    )
    direct = await cursor.fetchone()
    if direct:
        return {
            "status": "bound",
            "source": "device",
            "conversation_id": direct[0],
            "conversation_name": direct[1] or "New Chat",
        }

    await cursor.execute(
        """
        SELECT b.conversation_id, c.chat_name, g.id AS group_id, g.name AS group_name,
               m.is_primary_route_group, m.routing_priority
        FROM EXTERNAL_DEVICE_GROUP_MEMBERS m
        JOIN EXTERNAL_DEVICE_GROUPS g ON g.id = m.group_id
        JOIN EXTERNAL_DEVICE_BINDINGS b ON b.target_type = 'group' AND b.target_id = g.id
        JOIN EXTERNAL_DEVICES d ON d.id = m.device_id
        JOIN CONVERSATIONS c ON c.id = b.conversation_id
        WHERE m.device_id = ?
          AND g.user_id = d.user_id
          AND b.user_id = d.user_id
          AND c.user_id = d.user_id
          AND COALESCE(json_extract(d.metadata_json, '$.deleted'), 0) = 0
          AND COALESCE(json_extract(g.metadata_json, '$.deleted'), 0) = 0
        ORDER BY m.is_primary_route_group DESC, m.routing_priority ASC, g.id ASC
        """,
        (device_id,),
    )
    rows = await cursor.fetchall()
    if not rows:
        return {"status": "setup_required", "source": None, "conversation_id": None}

    best = rows[0]
    tied = [
        row
        for row in rows
        if int(row["is_primary_route_group"] or 0) == int(best["is_primary_route_group"] or 0)
        and int(row["routing_priority"] or 100) == int(best["routing_priority"] or 100)
    ]
    if len(tied) > 1:
        return {
            "status": "routing_conflict",
            "source": "group",
            "conversation_id": None,
            "groups": [row["group_name"] for row in tied],
        }

    return {
        "status": "bound",
        "source": "group",
        "conversation_id": best["conversation_id"],
        "conversation_name": best["chat_name"] or "New Chat",
        "group_id": best["group_id"],
        "group_name": best["group_name"],
    }


async def get_device_groups(device_id: int) -> list[dict]:
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """
            SELECT g.id, g.slug, g.name, m.is_primary_route_group, m.routing_priority
            FROM EXTERNAL_DEVICE_GROUP_MEMBERS m
            JOIN EXTERNAL_DEVICE_GROUPS g ON g.id = m.group_id
            JOIN EXTERNAL_DEVICES d ON d.id = m.device_id
            WHERE m.device_id = ?
              AND g.user_id = d.user_id
              AND COALESCE(json_extract(g.metadata_json, '$.deleted'), 0) = 0
              AND COALESCE(json_extract(d.metadata_json, '$.deleted'), 0) = 0
            ORDER BY m.is_primary_route_group DESC, m.routing_priority ASC, g.name COLLATE NOCASE ASC
            """,
            (device_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def device_me_payload(device: AuthenticatedDevice) -> dict:
    groups = await get_device_groups(device.id)
    async with get_db_connection(readonly=True) as conn:
        binding = await resolve_device_binding(conn, device.id)
    conversation_id = binding.get("conversation_id") if binding.get("status") == "bound" else None
    return {
        "slug": device.slug,
        "display_name": device.display_name,
        "device_type": device.device_type,
        "enabled": device.enabled,
        "groups": [group["slug"] for group in groups],
        "conversation_id": conversation_id,
        "capabilities": device.capabilities,
    }


async def resolve_runtime_binding(device: AuthenticatedDevice) -> DeviceBindingResult:
    async with get_db_connection(readonly=True) as conn:
        binding = await resolve_device_binding(conn, device.id)
    status = binding.get("status")
    if status == "bound":
        return DeviceBindingResult(
            status="bound",
            source=binding.get("source"),
            conversation_id=int(binding["conversation_id"]),
            conversation_name=binding.get("conversation_name"),
            group_name=binding.get("group_name"),
        )
    if status == "routing_conflict":
        return DeviceBindingResult(
            status="routing_conflict",
            conversation_id=None,
            source=binding.get("source"),
            groups=binding.get("groups") or [],
        )
    return DeviceBindingResult(status="setup_required", conversation_id=None)


def _safe_details(details: dict | None) -> str:
    try:
        return orjson.dumps(details or {}).decode("utf-8")
    except TypeError:
        return "{}"


def _load_details(value: str | None) -> dict:
    return _json_object(value)


def _processing_stale_modifier() -> str:
    seconds = max(1, int(DEVICE_PROCESSING_EVENT_STALE_SECONDS))
    return f"-{seconds} seconds"


async def completed_incoming_message_event(
    *,
    device_id: int,
    external_message_id: str,
) -> dict | None:
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """
            SELECT id, conversation_id, status, details_json
            FROM EXTERNAL_DEVICE_EVENTS
            WHERE device_id = ? AND external_message_id = ?
            """,
            (device_id, external_message_id),
        )
        row = await cursor.fetchone()

    if not row or row["status"] != "completed":
        return None
    details = _load_details(row["details_json"])
    return {
        "event_id": int(row["id"]),
        "conversation_id": row["conversation_id"],
        "reply": details.get("reply", ""),
        "actions": details.get("actions", []),
    }


async def reserve_incoming_message_event(
    *,
    device_id: int,
    conversation_id: int,
    external_message_id: str,
    metadata: dict | None,
    text: str,
) -> dict:
    details = {
        "metadata": metadata if isinstance(metadata, dict) else {},
        "text_chars": len(text),
    }
    async with get_db_connection() as conn:
        try:
            cursor = await conn.execute(
                """
                INSERT INTO EXTERNAL_DEVICE_EVENTS
                    (device_id, conversation_id, external_message_id, direction,
                     event_type, status, details_json)
                VALUES (?, ?, ?, 'in', 'message', 'processing', ?)
                """,
                (device_id, conversation_id, external_message_id, _safe_details(details)),
            )
            await conn.commit()
            return {"state": "reserved", "event_id": int(cursor.lastrowid)}
        except sqlite3.IntegrityError:
            await conn.rollback()

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """
            SELECT id, conversation_id, status, details_json, created_at
            FROM EXTERNAL_DEVICE_EVENTS
            WHERE device_id = ? AND external_message_id = ?
            """,
            (device_id, external_message_id),
        )
        row = await cursor.fetchone()

    if not row:
        raise DeviceRuntimeError("idempotency_error", "Could not inspect duplicate message", 409)

    details = _load_details(row["details_json"])
    if row["status"] == "completed":
        return {
            "state": "duplicate_completed",
            "event_id": int(row["id"]),
            "conversation_id": row["conversation_id"],
            "reply": details.get("reply", ""),
            "actions": details.get("actions", []),
        }
    if row["status"] == "processing":
        stale_modifier = _processing_stale_modifier()
        async with get_db_connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE EXTERNAL_DEVICE_EVENTS
                SET conversation_id = ?,
                    details_json = ?,
                    latency_ms = NULL,
                    created_at = CURRENT_TIMESTAMP
                WHERE id = ?
                  AND status = 'processing'
                  AND created_at <= datetime('now', ?)
                """,
                (conversation_id, _safe_details(details), int(row["id"]), stale_modifier),
            )
            await conn.commit()
        if cursor.rowcount:
            logger.warning(
                "[devices] Reclaimed stale processing event id=%s for device_id=%s message_id=%s",
                row["id"],
                device_id,
                external_message_id,
            )
            return {
                "state": "reserved",
                "event_id": int(row["id"]),
                "reclaimed": True,
            }
        return {
            "state": "duplicate_processing",
            "event_id": int(row["id"]),
            "conversation_id": row["conversation_id"],
        }
    return {
        "state": "duplicate_failed",
        "event_id": int(row["id"]),
        "conversation_id": row["conversation_id"],
    }


async def complete_incoming_message_event(
    *,
    event_id: int,
    reply: str,
    actions: list[dict],
    latency_ms: int,
) -> None:
    details = {"reply": reply, "actions": actions}
    async with get_db_connection() as conn:
        await conn.execute(
            """
            UPDATE EXTERNAL_DEVICE_EVENTS
            SET status = 'completed', details_json = ?, latency_ms = ?
            WHERE id = ?
            """,
            (_safe_details(details), latency_ms, event_id),
        )
        await conn.commit()


async def fail_incoming_message_event(
    *,
    event_id: int,
    code: str,
    message: str,
    latency_ms: int | None = None,
) -> None:
    details = {"error": code, "message": message}
    async with get_db_connection() as conn:
        await conn.execute(
            """
            UPDATE EXTERNAL_DEVICE_EVENTS
            SET status = 'failed', details_json = ?, latency_ms = ?
            WHERE id = ?
            """,
            (_safe_details(details), latency_ms, event_id),
        )
        await conn.commit()


async def release_retryable_incoming_message_event(*, event_id: int) -> None:
    """Remove a transient reservation so the same external id can retry."""
    async with get_db_connection() as conn:
        await conn.execute(
            "DELETE FROM EXTERNAL_DEVICE_EVENTS WHERE id = ? AND status = 'processing'",
            (event_id,),
        )
        await conn.commit()


async def record_outgoing_reply_event(
    *,
    device_id: int,
    conversation_id: int,
    external_message_id: str,
    reply: str,
    actions: list[dict],
    latency_ms: int,
) -> None:
    async with get_db_connection() as conn:
        await conn.execute(
            """
            INSERT INTO EXTERNAL_DEVICE_EVENTS
                (device_id, conversation_id, external_message_id, direction,
                 event_type, status, details_json, latency_ms)
            VALUES (?, ?, NULL, 'out', 'reply', 'completed', ?, ?)
            """,
            (
                device_id,
                conversation_id,
                _safe_details(
                    {
                        "reply": reply,
                        "actions": actions,
                        "request_message_id": external_message_id,
                    }
                ),
                latency_ms,
            ),
        )
        await conn.commit()


async def sanitize_last_device_reply_in_db(
    *,
    conversation_id: int,
    raw_reply: str,
    clean_reply: str,
) -> None:
    if not raw_reply or raw_reply == clean_reply:
        return
    try:
        async with get_db_connection() as conn:
            await conn.execute(
                """
                UPDATE MESSAGES
                SET message = ?
                WHERE id = (
                    SELECT id
                    FROM MESSAGES
                    WHERE conversation_id = ?
                      AND type = 'bot'
                    ORDER BY id DESC
                    LIMIT 1
                )
                  AND message = ?
                """,
                (clean_reply, conversation_id, raw_reply),
            )
            await conn.commit()
    except Exception:
        logger.warning(
            "[devices] Failed to sanitize stored action block for conversation_id=%s",
            conversation_id,
            exc_info=True,
        )


async def record_system_device_event(
    *,
    device_id: int,
    conversation_id: int | None,
    event_type: str,
    status: str,
    details: dict | None = None,
    latency_ms: int | None = None,
) -> None:
    async with get_db_connection() as conn:
        await conn.execute(
            """
            INSERT INTO EXTERNAL_DEVICE_EVENTS
                (device_id, conversation_id, external_message_id, direction,
                 event_type, status, details_json, latency_ms)
            VALUES (?, ?, NULL, 'system', ?, ?, ?, ?)
            """,
            (
                device_id,
                conversation_id,
                event_type,
                status,
                _safe_details(details),
                latency_ms,
            ),
        )
        await conn.commit()


async def prune_old_device_events(*, force: bool = False) -> int:
    global _last_device_event_prune_monotonic
    retention_days = int(DEVICE_EVENT_RETENTION_DAYS)
    if retention_days <= 0:
        return 0

    now = monotonic()
    if (
        not force
        and _last_device_event_prune_monotonic
        and now - _last_device_event_prune_monotonic < DEVICE_EVENT_PRUNE_INTERVAL_SECONDS
    ):
        return 0

    async with get_db_connection() as conn:
        cursor = await conn.execute(
            """
            DELETE FROM EXTERNAL_DEVICE_EVENTS
            WHERE status != 'processing'
              AND created_at < datetime('now', ?)
            """,
            (f"-{retention_days} days",),
        )
        await conn.commit()
        deleted = int(cursor.rowcount or 0)
    _last_device_event_prune_monotonic = now
    return deleted


def device_context_header(device: AuthenticatedDevice, groups: list[dict]) -> str:
    group_slugs = ", ".join(group["slug"] for group in groups) or "none"
    capabilities = ", ".join(
        name for name, enabled in sorted(device.capabilities.items()) if enabled
    ) or "none"
    action_types = ", ".join(allowed_device_action_types(device.capabilities))
    action_note = (
        "; allowed action block: [AURVEK_ACTIONS] JSON array with types "
        f"{action_types} [/AURVEK_ACTIONS]"
        if action_types
        else ""
    )
    return (
        f"[External device: {device.display_name}; slug: {device.slug}; "
        f"groups: {group_slugs}; capabilities: {capabilities}{action_note}]"
    )


def allowed_device_action_types(capabilities: dict) -> list[str]:
    actions = []
    if capabilities.get("speak"):
        actions.append("speak")
    if capabilities.get("snapshot"):
        actions.append("snapshot")
    return actions


def _normalize_snapshot_payloads(snapshot_payload, snapshots_payload) -> list:
    payloads = []
    if snapshot_payload is not None:
        payloads.append(snapshot_payload)
    if snapshots_payload is not None:
        if not isinstance(snapshots_payload, list):
            raise DeviceValidationError("snapshots must be a list")
        payloads.extend(snapshots_payload)
    if len(payloads) > MAX_DEVICE_SNAPSHOTS:
        raise DeviceValidationError(f"Maximum {MAX_DEVICE_SNAPSHOTS} snapshots per message")
    return payloads


def _snapshot_data_value(snapshot) -> str:
    if isinstance(snapshot, str):
        return snapshot
    if isinstance(snapshot, dict):
        return (
            snapshot.get("data_base64")
            or snapshot.get("image_base64")
            or snapshot.get("data")
            or ""
        )
    return ""


def _base64_body_and_mime(value: str) -> tuple[str, str | None]:
    data = str(value or "").strip()
    mime_type = None
    if data.startswith("data:"):
        header, sep, body = data.partition(",")
        if not sep or ";base64" not in header.lower():
            raise DeviceValidationError("Snapshot data URL must be base64 encoded")
        mime_type = header[5:].split(";", 1)[0].strip().lower() or None
        data = body.strip()
    return data, mime_type


def prevalidate_device_snapshots(
    *,
    capabilities: dict,
    snapshot_payload=None,
    snapshots_payload=None,
) -> list:
    payloads = _normalize_snapshot_payloads(snapshot_payload, snapshots_payload)
    if payloads and not capabilities.get("snapshot"):
        raise DeviceValidationError("Device is not allowed to send snapshots")

    max_encoded_len = ((MAX_IMAGE_UPLOAD_SIZE + 2) // 3) * 4 + 4096
    for snapshot in payloads:
        if not isinstance(snapshot, (str, dict)):
            raise DeviceValidationError("Snapshot must be an object or base64 string")
        data_value = _snapshot_data_value(snapshot)
        if not data_value:
            raise DeviceValidationError("Snapshot data is required")
        body, data_url_mime = _base64_body_and_mime(data_value)
        if not body:
            raise DeviceValidationError("Snapshot data is empty")
        if len(body) > max_encoded_len:
            raise DeviceValidationError("Snapshot image is too large")
        if data_url_mime and data_url_mime not in SAFE_SNAPSHOT_MIME_TYPES:
            raise DeviceValidationError("Snapshot mime type is not supported")
        if isinstance(snapshot, dict):
            content_type = (
                data_url_mime
                or snapshot.get("mime_type")
                or snapshot.get("content_type")
                or "image/jpeg"
            )
            content_type = str(content_type or "").split(";", 1)[0].strip().lower()
            if content_type not in SAFE_SNAPSHOT_MIME_TYPES:
                raise DeviceValidationError("Snapshot mime type is not supported")
    return payloads


def _decode_snapshot_data(value: str) -> tuple[bytes, str | None]:
    data, mime_type = _base64_body_and_mime(value)
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DeviceValidationError("Snapshot data must be valid base64") from exc
    return decoded, mime_type


def _decode_device_snapshot(snapshot, index: int) -> dict:
    if isinstance(snapshot, str):
        raw_data, data_url_mime = _decode_snapshot_data(snapshot)
        content_type = data_url_mime or "image/jpeg"
        filename = f"snapshot-{index}.jpg"
    elif isinstance(snapshot, dict):
        data_value = (
            snapshot.get("data_base64")
            or snapshot.get("image_base64")
            or snapshot.get("data")
        )
        if not data_value:
            raise DeviceValidationError("Snapshot data is required")
        raw_data, data_url_mime = _decode_snapshot_data(data_value)
        content_type = (
            data_url_mime
            or snapshot.get("mime_type")
            or snapshot.get("content_type")
            or "image/jpeg"
        )
        filename = _clean_filename(
            snapshot.get("filename"),
            fallback=f"snapshot-{index}.jpg",
        )
    else:
        raise DeviceValidationError("Snapshot must be an object or base64 string")

    content_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if content_type not in SAFE_SNAPSHOT_MIME_TYPES:
        raise DeviceValidationError("Snapshot mime type is not supported")
    if not raw_data:
        raise DeviceValidationError("Snapshot data is empty")
    if len(raw_data) > MAX_IMAGE_UPLOAD_SIZE:
        raise DeviceValidationError("Snapshot image is too large")

    return {
        "data": raw_data,
        "content_type": content_type,
        "filename": filename,
    }


async def decode_device_snapshots(snapshot_payloads: list) -> list[dict]:
    files = []
    for index, snapshot in enumerate(snapshot_payloads):
        file_item = _decode_device_snapshot(snapshot, index + 1)
        try:
            image_data, image_media_type, _w, _h, _actual_format, _was_compressed = await asyncio.to_thread(
                validate_and_compress_image,
                file_item["data"],
                file_item["filename"],
            )
        except ValueError as exc:
            raise DeviceValidationError(str(exc)) from exc
        files.append(
            {
                "data": image_data,
                "content_type": image_media_type,
                "filename": file_item["filename"],
            }
        )
    return files


def extract_structured_actions(reply: str, capabilities: dict) -> tuple[str, list[dict]]:
    allowed_types = set(allowed_device_action_types(capabilities))
    if not reply:
        return reply, []

    action_blocks: list[str] = []

    def _strip_action_block(match) -> str:
        action_blocks.append(match.group(1) or match.group(2) or "")
        return ""

    clean_reply = ACTION_BLOCK_RE.sub(_strip_action_block, reply).strip()
    if not allowed_types:
        return clean_reply, []
    actions: list[dict] = []
    for block in action_blocks:
        try:
            parsed = orjson.loads(block.strip())
        except orjson.JSONDecodeError:
            logger.debug("[devices] Ignoring malformed action block")
            continue
        if isinstance(parsed, dict):
            parsed = parsed.get("actions", [])
        if not isinstance(parsed, list):
            continue
        for item in parsed:
            if len(actions) >= MAX_DEVICE_ACTIONS:
                break
            if not isinstance(item, dict):
                continue
            action_type = str(item.get("type") or "").strip().lower()
            if action_type not in allowed_types:
                continue
            if action_type == "speak":
                text = str(item.get("text") or "").strip()[:MAX_ACTION_TEXT_CHARS]
                if text:
                    actions.append({"type": "speak", "text": text})
            elif action_type == "snapshot":
                action = {"type": "snapshot"}
                reason = str(item.get("reason") or "").strip()[:200]
                if reason:
                    action["reason"] = reason
                actions.append(action)
    return clean_reply, actions


async def _streaming_response_to_reply(
    response: StreamingResponse,
) -> tuple[str, str | None]:
    reply_parts: list[str] = []
    errors: list[str] = []
    terminal: str | None = None
    durable_message_ids = False
    buffer = ""

    async for chunk in response.body_iterator:
        chunk_str = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
        buffer += chunk_str
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                data = orjson.loads(payload)
            except orjson.JSONDecodeError:
                logger.debug("[devices] Could not parse SSE payload from runtime")
                continue
            runtime_terminal = data.get("terminal")
            if runtime_terminal in {
                "queued_for_active_phone",
                "stale_channel_turn",
            }:
                terminal = str(runtime_terminal)
                continue
            if data.get("persistence_error") is True:
                terminal = "persistence_error"
                continue
            if "error" in data:
                errors.append(str(data.get("message") or data.get("error") or "Runtime error"))
                continue
            message_ids = data.get("message_ids")
            if isinstance(message_ids, dict):
                durable_message_ids = bool(
                    message_ids.get("user") and message_ids.get("bot")
                )
            content = data.get("content")
            if isinstance(content, str):
                reply_parts.append(content)

    if errors:
        raise DeviceRuntimeError("runtime_error", errors[-1], 502)
    if terminal is None and not durable_message_ids:
        terminal = "persistence_error"
    if terminal is not None:
        return "", terminal
    return "".join(reply_parts).strip(), terminal


async def _json_response_error(response: JSONResponse) -> DeviceRuntimeError:
    try:
        payload = orjson.loads(response.body)
    except orjson.JSONDecodeError:
        payload = {}
    message = payload.get("message") or payload.get("error") or "Runtime error"
    return DeviceRuntimeError("runtime_error", str(message), response.status_code)


async def _assert_device_rate_limits(device: AuthenticatedDevice) -> None:
    if not await check_device_rate_limit(
        f"device:{device.id}",
        action="external_device_ai_call",
        limit=30,
        window_minutes=1,
    ):
        raise DeviceRuntimeError(
            "rate_limited",
            "Too many device requests. Limit: 30 per minute.",
            429,
        )
    if not await check_device_rate_limit(
        f"user:{device.user_id}",
        action="external_device_user_ai_call",
        limit=120,
        window_minutes=1,
    ):
        raise DeviceRuntimeError(
            "rate_limited",
            "Too many user device requests. Limit: 120 per minute.",
            429,
        )


async def check_device_rate_limit(
    subject: str,
    *,
    action: str,
    limit: int,
    window_minutes: int,
) -> bool:
    try:
        import time

        now = time.time()
        window_start = now - (window_minutes * 60)
        key = f"rate_limit:{action}:{subject}"
        await redis_client.zremrangebyscore(key, 0, window_start)
        current_count = await redis_client.zcard(key)
        if current_count >= limit:
            logger.warning("Rate limit exceeded for %s %s: %s/%s", action, subject, current_count, limit)
            return False
        await redis_client.zadd(key, {f"{now:.6f}:{uuid4().hex}": now})
        await redis_client.expire(key, window_minutes * 60 + 60)
        return True
    except Exception as exc:
        logger.error("[devices] Error checking rate limit for %s %s: %s", action, subject, exc)
        return True


async def load_user_api_keys(user_id: int) -> dict | None:
    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT user_api_keys FROM USER_DETAILS WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
        if row and row[0]:
            keys_json = decrypt_api_key(row[0])
            if keys_json:
                parsed = orjson.loads(keys_json)
                return parsed if isinstance(parsed, dict) else None
    except Exception as exc:
        logger.warning("[devices] Failed to load user API keys for owner %s: %s", user_id, exc)
    return None


async def handle_device_text_message(
    *,
    request: Request,
    device: AuthenticatedDevice,
    message_id: str,
    text: str,
    metadata: dict | None,
    snapshot=None,
    snapshots=None,
) -> dict:
    try:
        message_id = _clean_text(message_id, max_len=160, field="message_id")
        text = _clean_text(text, max_len=MAX_DEVICE_MESSAGE_CHARS, field="text")
        snapshot_payloads = prevalidate_device_snapshots(
            capabilities=device.capabilities,
            snapshot_payload=snapshot,
            snapshots_payload=snapshots,
        )
    except DeviceValidationError as exc:
        raise DeviceRuntimeError("invalid_request", str(exc), 400) from exc
    if not message_id:
        raise DeviceRuntimeError("invalid_request", "message_id is required", 400)
    if not text:
        raise DeviceRuntimeError("invalid_request", "text is required", 400)
    if metadata is not None and not isinstance(metadata, dict):
        raise DeviceRuntimeError("invalid_request", "metadata must be an object", 400)

    binding = await resolve_runtime_binding(device)
    if binding.status == "setup_required":
        await record_system_device_event(
            device_id=device.id,
            conversation_id=None,
            event_type="routing",
            status="setup_required",
            details={"request_message_id": message_id},
        )
        raise DeviceRuntimeError("setup_required", "Device has no conversation binding", 409)
    if binding.status == "routing_conflict":
        await record_system_device_event(
            device_id=device.id,
            conversation_id=None,
            event_type="routing",
            status="routing_conflict",
            details={
                "request_message_id": message_id,
                "groups": binding.groups or [],
            },
        )
        raise DeviceRuntimeError("routing_conflict", "Device group routing is ambiguous", 409)
    conversation_id = int(binding.conversation_id)

    duplicate_completed = await completed_incoming_message_event(
        device_id=device.id,
        external_message_id=message_id,
    )
    if duplicate_completed:
        return {
            "success": True,
            "conversation_id": duplicate_completed["conversation_id"],
            "reply": duplicate_completed.get("reply") or "",
            "actions": duplicate_completed.get("actions") or [],
            "duplicate": True,
        }

    started = monotonic()
    turn_lock = await acquire_device_turn_lock(conversation_id)
    if turn_lock is None:
        await record_system_device_event(
            device_id=device.id,
            conversation_id=conversation_id,
            event_type="busy",
            status="busy",
            details={"request_message_id": message_id},
        )
        raise DeviceRuntimeError("busy", "Conversation is processing another turn", 409)
    event_id: int | None = None
    event_completed = False
    try:
        try:
            await _assert_device_rate_limits(device)
        except DeviceRuntimeError as exc:
            await record_system_device_event(
                device_id=device.id,
                conversation_id=conversation_id,
                event_type="rate_limit",
                status=exc.code,
                details={"request_message_id": message_id, "message": exc.message},
                latency_ms=int((monotonic() - started) * 1000),
            )
            raise

        active_pause = await get_active_pause(device.user_id)
        if active_pause:
            await record_system_device_event(
                device_id=device.id,
                conversation_id=conversation_id,
                event_type="wellbeing",
                status="wellbeing_pause_active",
                details={"request_message_id": message_id},
                latency_ms=int((monotonic() - started) * 1000),
            )
            raise DeviceRuntimeError(
                "wellbeing_pause_active",
                active_pause.get("message") or "A break pause is required before continuing.",
                429,
            )

        try:
            snapshot_files = await decode_device_snapshots(snapshot_payloads)
        except DeviceValidationError as exc:
            raise DeviceRuntimeError("invalid_request", str(exc), 400) from exc

        reservation = await reserve_incoming_message_event(
            device_id=device.id,
            conversation_id=conversation_id,
            external_message_id=message_id,
            metadata=metadata,
            text=text,
        )
        if reservation["state"] == "duplicate_completed":
            return {
                "success": True,
                "conversation_id": reservation["conversation_id"],
                "reply": reservation.get("reply") or "",
                "actions": reservation.get("actions") or [],
                "duplicate": True,
            }
        if reservation["state"] == "duplicate_processing":
            raise DeviceRuntimeError("busy", "Message is already processing", 409)
        if reservation["state"] == "duplicate_failed":
            raise DeviceRuntimeError("duplicate_failed", "Message id was already used by a failed turn", 409)
        event_id = int(reservation["event_id"])

        owner = await get_user_by_id(device.user_id)
        if owner is None or not owner.is_enabled:
            raise DeviceRuntimeError("owner_disabled", "Device owner is disabled", 403)

        groups = await get_device_groups(device.id)
        prefixed_text = f"{device_context_header(device, groups)}\n{text}"
        foreground_turn = await capture_non_phone_channel_turn(
            conversation_id=conversation_id,
            channel="device",
            connection_factory=get_db_connection,
        )
        response = await serialize_user_billing_response(
            owner.id,
            process_save_message(
                request=request,
                conversation_id=conversation_id,
                current_user=owner,
                text_plain=prefixed_text,
                files=snapshot_files,
                full_response=False,
                is_whatsapp=False,
                thinking_budget_tokens=None,
                user_api_keys=await load_user_api_keys(device.user_id),
                prevalidated=True,
                strip_device_action_blocks=True,
                channel_context=foreground_turn.context,
            ),
        )

        if isinstance(response, JSONResponse):
            raise await _json_response_error(response)
        if not isinstance(response, StreamingResponse):
            raise DeviceRuntimeError("runtime_error", "Unexpected runtime response", 502)

        raw_reply, runtime_terminal = await _streaming_response_to_reply(response)
        latency_ms = int((monotonic() - started) * 1000)
        if runtime_terminal == "queued_for_active_phone":
            await complete_incoming_message_event(
                event_id=event_id,
                reply="",
                actions=[],
                latency_ms=latency_ms,
            )
            event_completed = True
            return {
                "success": True,
                "conversation_id": conversation_id,
                "reply": "",
                "actions": [],
                "terminal": runtime_terminal,
            }
        if runtime_terminal in {"stale_channel_turn", "persistence_error"}:
            await release_retryable_incoming_message_event(event_id=event_id)
            event_id = None
            raise DeviceRuntimeError(
                "foreground_changed"
                if runtime_terminal == "stale_channel_turn"
                else "persistence_error",
                "The inbound turn was not persisted; retry with the same message id",
                503,
            )

        reply, actions = extract_structured_actions(raw_reply, device.capabilities)
        await sanitize_last_device_reply_in_db(
            conversation_id=conversation_id,
            raw_reply=raw_reply,
            clean_reply=reply,
        )
        await complete_incoming_message_event(
            event_id=event_id,
            reply=reply,
            actions=actions,
            latency_ms=latency_ms,
        )
        event_completed = True
        try:
            await record_outgoing_reply_event(
                device_id=device.id,
                conversation_id=conversation_id,
                external_message_id=message_id,
                reply=reply,
                actions=actions,
                latency_ms=latency_ms,
            )
        except Exception:
            logger.exception(
                "[devices] Failed to record outgoing reply event for device_id=%s message_id=%s",
                device.id,
                message_id,
            )
        try:
            await increment_metric("ai_requests_total")
            await increment_user_activity(device.user_id)
        except Exception:
            logger.exception(
                "[devices] Failed to record post-completion metrics for device_id=%s message_id=%s",
                device.id,
                message_id,
            )
        return {
            "success": True,
            "conversation_id": conversation_id,
            "reply": reply,
            "actions": actions,
        }
    except DeviceRuntimeError as exc:
        if event_id is not None and not event_completed:
            await fail_incoming_message_event(
                event_id=event_id,
                code=exc.code,
                message=exc.message,
                latency_ms=int((monotonic() - started) * 1000),
            )
        raise
    except Exception as exc:
        logger.exception("[devices] Runtime message failed for device_id=%s", device.id)
        if event_id is not None and not event_completed:
            await fail_incoming_message_event(
                event_id=event_id,
                code="runtime_exception",
                message=type(exc).__name__,
                latency_ms=int((monotonic() - started) * 1000),
            )
        raise DeviceRuntimeError("runtime_error", "Device message failed", 502) from exc
    finally:
        await release_device_turn_lock(conversation_id, turn_lock)


async def get_admin_overview() -> DevicesAdminOverview:
    """Return real aggregate data for the Devices admin page."""
    try:
        await prune_old_device_events()
    except Exception as exc:
        logger.warning("[devices] Failed to prune old external device events: %s", exc)

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """
            SELECT COUNT(*)
            FROM EXTERNAL_DEVICES
            WHERE COALESCE(json_extract(metadata_json, '$.deleted'), 0) = 0
            """
        )
        total_devices = int((await cursor.fetchone())[0] or 0)

        cursor = await conn.execute(
            """
            SELECT COUNT(*)
            FROM EXTERNAL_DEVICES
            WHERE enabled = 1
              AND COALESCE(json_extract(metadata_json, '$.deleted'), 0) = 0
            """
        )
        enabled_devices = int((await cursor.fetchone())[0] or 0)

        cursor = await conn.execute(
            """
            SELECT COUNT(*)
            FROM EXTERNAL_DEVICES
            WHERE last_seen_at >= datetime('now', '-24 hours')
              AND COALESCE(json_extract(metadata_json, '$.deleted'), 0) = 0
            """
        )
        seen_recently = int((await cursor.fetchone())[0] or 0)

        cursor = await conn.execute(
            """
            SELECT COUNT(*)
            FROM EXTERNAL_DEVICE_GROUPS
            WHERE COALESCE(json_extract(metadata_json, '$.deleted'), 0) = 0
            """
        )
        group_count = int((await cursor.fetchone())[0] or 0)

        cursor = await conn.execute(
            """
            SELECT COUNT(*)
            FROM EXTERNAL_DEVICE_EVENTS e
            JOIN EXTERNAL_DEVICES d ON d.id = e.device_id
            WHERE e.direction = 'in'
              AND e.event_type = 'message'
              AND e.created_at >= date('now')
              AND COALESCE(json_extract(d.metadata_json, '$.deleted'), 0) = 0
            """
        )
        messages_today = int((await cursor.fetchone())[0] or 0)

        cursor = await conn.execute(
            """
            SELECT e.id, e.direction, e.event_type, e.status, e.created_at,
                   e.external_message_id, e.latency_ms, e.conversation_id,
                   d.display_name AS device_name, d.slug AS device_slug
            FROM EXTERNAL_DEVICE_EVENTS e
            JOIN EXTERNAL_DEVICES d ON d.id = e.device_id
            WHERE COALESCE(json_extract(d.metadata_json, '$.deleted'), 0) = 0
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT 10
            """
        )
        recent_events = [dict(row) for row in await cursor.fetchall()]

    return DevicesAdminOverview(
        total_devices=total_devices,
        enabled_devices=enabled_devices,
        seen_recently=seen_recently,
        group_count=group_count,
        messages_today=messages_today,
        recent_events=recent_events,
    )


async def get_admin_page_data() -> dict:
    overview = await get_admin_overview()
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """
            SELECT u.id, u.username
            FROM USERS u
            WHERE COALESCE(u.is_enabled, 1) = 1
            ORDER BY u.username COLLATE NOCASE ASC
            """
        )
        users = [dict(row) for row in await cursor.fetchall()]

        cursor = await conn.execute(
            """
            SELECT c.id, c.user_id, u.username, COALESCE(c.chat_name, 'New Chat') AS chat_name,
                   c.last_activity
            FROM CONVERSATIONS c
            JOIN USERS u ON u.id = c.user_id
            ORDER BY u.username COLLATE NOCASE ASC, c.last_activity DESC, c.id DESC
            LIMIT 500
            """
        )
        conversations = [dict(row) for row in await cursor.fetchall()]

        cursor = await conn.execute(
            """
            SELECT g.*, u.username,
                   COUNT(DISTINCT d.id) AS member_count,
                   GROUP_CONCAT(d.display_name, ', ') AS member_names,
                   b.conversation_id,
                   COALESCE(c.chat_name, 'New Chat') AS conversation_name
            FROM EXTERNAL_DEVICE_GROUPS g
            JOIN USERS u ON u.id = g.user_id
            LEFT JOIN EXTERNAL_DEVICE_GROUP_MEMBERS m ON m.group_id = g.id
            LEFT JOIN EXTERNAL_DEVICES d ON d.id = m.device_id
                AND COALESCE(json_extract(d.metadata_json, '$.deleted'), 0) = 0
            LEFT JOIN EXTERNAL_DEVICE_BINDINGS b ON b.target_type = 'group' AND b.target_id = g.id
            LEFT JOIN CONVERSATIONS c ON c.id = b.conversation_id
            WHERE COALESCE(json_extract(g.metadata_json, '$.deleted'), 0) = 0
            GROUP BY g.id
            ORDER BY u.username COLLATE NOCASE ASC, g.name COLLATE NOCASE ASC
            """
        )
        groups = [dict(row) for row in await cursor.fetchall()]

        cursor = await conn.execute(
            """
            SELECT d.*, u.username,
                   GROUP_CONCAT(g.name, ', ') AS group_names,
                   b.conversation_id AS direct_conversation_id,
                   COALESCE(c.chat_name, 'New Chat') AS direct_conversation_name
            FROM EXTERNAL_DEVICES d
            JOIN USERS u ON u.id = d.user_id
            LEFT JOIN EXTERNAL_DEVICE_GROUP_MEMBERS m ON m.device_id = d.id
            LEFT JOIN EXTERNAL_DEVICE_GROUPS g ON g.id = m.group_id
                AND COALESCE(json_extract(g.metadata_json, '$.deleted'), 0) = 0
            LEFT JOIN EXTERNAL_DEVICE_BINDINGS b ON b.target_type = 'device' AND b.target_id = d.id
            LEFT JOIN CONVERSATIONS c ON c.id = b.conversation_id
            WHERE COALESCE(json_extract(d.metadata_json, '$.deleted'), 0) = 0
            GROUP BY d.id
            ORDER BY u.username COLLATE NOCASE ASC, d.display_name COLLATE NOCASE ASC
            """
        )
        devices = []
        for row in await cursor.fetchall():
            device = dict(row)
            device["capabilities"] = parse_capabilities(device.get("capabilities_json"))
            device["effective_binding"] = await resolve_device_binding(conn, int(device["id"]))
            member_cursor = await conn.execute(
                """
                SELECT g.id, g.name, g.slug, m.is_primary_route_group, m.routing_priority
                FROM EXTERNAL_DEVICE_GROUP_MEMBERS m
                JOIN EXTERNAL_DEVICE_GROUPS g ON g.id = m.group_id
                WHERE m.device_id = ?
                  AND COALESCE(json_extract(g.metadata_json, '$.deleted'), 0) = 0
                ORDER BY g.name COLLATE NOCASE ASC
                """,
                (device["id"],),
            )
            device["memberships"] = [dict(member) for member in await member_cursor.fetchall()]
            devices.append(device)

    return {
        "stats": {
            "total_devices": overview.total_devices,
            "enabled_devices": overview.enabled_devices,
            "seen_recently": overview.seen_recently,
            "group_count": overview.group_count,
            "messages_today": overview.messages_today,
        },
        "recent_events": overview.recent_events,
        "users": users,
        "conversations": conversations,
        "groups": groups,
        "devices": devices,
        "basic_capabilities": BASIC_CAPABILITIES,
    }


async def create_device(
    *,
    owner_user_id: int,
    display_name: str,
    slug: str,
    device_type: str,
    notes: str,
    capability_names: Iterable[str],
    group_ids: Iterable[int],
    conversation_id: int | None,
) -> DeviceTokenResult:
    owner = await _fetch_owner_user(owner_user_id)
    slug = validate_slug(slug)
    display_name = _clean_text(display_name, max_len=120, field="Display name")
    if not display_name:
        raise DeviceValidationError("Display name is required")
    device_type = validate_slug(device_type or "custom", field="Device type")
    notes = _clean_text(notes, max_len=2000, field="Notes")
    group_id_list = []
    for group_id in group_ids:
        try:
            parsed_group_id = int(group_id)
        except (TypeError, ValueError):
            raise DeviceValidationError("Group must be a number")
        if parsed_group_id > 0:
            group_id_list.append(parsed_group_id)
    group_id_list = sorted(set(group_id_list))
    token = generate_device_token()

    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.cursor()
        try:
            await _validate_groups_owner(cursor, group_id_list, owner_user_id)
            effective_conversation_id = conversation_id
            if effective_conversation_id is not None:
                await _validate_conversation_owner(
                    cursor,
                    effective_conversation_id,
                    owner_user_id,
                    allow_classic_platform=False,
                )
            else:
                effective_conversation_id = await create_conversation_core(
                    owner_user_id,
                    cursor,
                    owner,
                    prompt_id=None,
                )
                await cursor.execute(
                    "UPDATE CONVERSATIONS SET chat_name = ? WHERE id = ? AND user_id = ?",
                    (display_name, effective_conversation_id, owner_user_id),
                )

            insert_cursor = await cursor.execute(
                """
                INSERT INTO EXTERNAL_DEVICES
                    (user_id, slug, display_name, device_type, notes,
                     capabilities_json, token_hash, token_prefix)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_user_id,
                    slug,
                    display_name,
                    device_type,
                    notes or None,
                    capabilities_json(capability_names),
                    hash_device_token(token),
                    token_prefix(token),
                ),
            )
            device_id = int(insert_cursor.lastrowid)

            for group_id in group_id_list:
                await cursor.execute(
                    """
                    INSERT OR IGNORE INTO EXTERNAL_DEVICE_GROUP_MEMBERS
                        (device_id, group_id)
                    VALUES (?, ?)
                    """,
                    (device_id, group_id),
                )

            await cursor.execute(
                """
                INSERT INTO EXTERNAL_DEVICE_BINDINGS
                    (user_id, target_type, target_id, conversation_id, response_mode)
                VALUES (?, 'device', ?, ?, 'text')
                """,
                (owner_user_id, device_id, effective_conversation_id),
            )

            await conn.commit()
            return DeviceTokenResult(
                token=token,
                device_id=device_id,
                conversation_id=effective_conversation_id,
            )
        except sqlite3.IntegrityError as exc:
            await conn.rollback()
            raise DeviceValidationError("Device slug already exists for this user") from exc
        except Exception:
            await conn.rollback()
            raise


async def update_device(
    *,
    device_id: int,
    display_name: str,
    slug: str,
    device_type: str,
    notes: str,
    capability_names: Iterable[str],
    icon_class: str | None = None,
) -> None:
    slug = validate_slug(slug)
    display_name = _clean_text(display_name, max_len=120, field="Display name")
    if not display_name:
        raise DeviceValidationError("Display name is required")
    device_type = validate_slug(device_type or "custom", field="Device type")
    notes = _clean_text(notes, max_len=2000, field="Notes")
    icon_class = _validate_icon_class(icon_class)

    async with get_db_connection() as conn:
        try:
            cursor = await conn.execute(
                """
                UPDATE EXTERNAL_DEVICES
                SET slug = ?, display_name = ?, device_type = ?, notes = ?,
                    capabilities_json = ?, icon_class = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                  AND COALESCE(json_extract(metadata_json, '$.deleted'), 0) = 0
                """,
                (
                    slug,
                    display_name,
                    device_type,
                    notes or None,
                    capabilities_json(capability_names),
                    icon_class,
                    device_id,
                ),
            )
            if cursor.rowcount == 0:
                raise DeviceValidationError("Device not found")
            await conn.commit()
        except sqlite3.IntegrityError as exc:
            await conn.rollback()
            raise DeviceValidationError("Device slug already exists for this user") from exc


async def set_device_enabled(device_id: int, enabled: bool) -> None:
    async with get_db_connection() as conn:
        cursor = await conn.execute(
            """
            UPDATE EXTERNAL_DEVICES
            SET enabled = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND COALESCE(json_extract(metadata_json, '$.deleted'), 0) = 0
            """,
            (1 if enabled else 0, device_id),
        )
        if cursor.rowcount == 0:
            raise DeviceValidationError("Device not found")
        await conn.commit()


async def soft_delete_device(device_id: int) -> None:
    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await conn.execute(
                """
                SELECT slug, display_name, metadata_json
                FROM EXTERNAL_DEVICES
                WHERE id = ?
                  AND COALESCE(json_extract(metadata_json, '$.deleted'), 0) = 0
                """,
                (device_id,),
            )
            row = await cursor.fetchone()
            if not row:
                raise DeviceValidationError("Device not found")

            deleted_slug = f"deleted-{device_id}-{row['slug']}"[:80].rstrip("-")
            deleted_name = f"{row['display_name']} (deleted)"[:120]
            metadata = _json_object(row["metadata_json"])
            metadata["deleted"] = True
            await conn.execute(
                """
                UPDATE EXTERNAL_DEVICES
                SET slug = ?, display_name = ?, enabled = 0,
                    token_hash = 'deleted', token_prefix = 'deleted',
                    metadata_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (deleted_slug, deleted_name, orjson.dumps(metadata).decode("utf-8"), device_id),
            )
            await conn.execute(
                "DELETE FROM EXTERNAL_DEVICE_GROUP_MEMBERS WHERE device_id = ?",
                (device_id,),
            )
            await conn.execute(
                """
                DELETE FROM EXTERNAL_DEVICE_BINDINGS
                WHERE target_type = 'device' AND target_id = ?
                """,
                (device_id,),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def rotate_device_token(device_id: int) -> DeviceTokenResult:
    token = generate_device_token()
    async with get_db_connection() as conn:
        cursor = await conn.execute(
            """
            UPDATE EXTERNAL_DEVICES
            SET token_hash = ?, token_prefix = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND COALESCE(json_extract(metadata_json, '$.deleted'), 0) = 0
            """,
            (hash_device_token(token), token_prefix(token), device_id),
        )
        if cursor.rowcount == 0:
            raise DeviceValidationError("Device not found")
        await conn.commit()
    return DeviceTokenResult(token=token, device_id=device_id)


async def create_group(
    *,
    owner_user_id: int,
    name: str,
    slug: str,
    notes: str,
    icon_class: str | None = None,
) -> int:
    await _fetch_owner_user(owner_user_id)
    name = _clean_text(name, max_len=120, field="Group name")
    if not name:
        raise DeviceValidationError("Group name is required")
    slug = validate_slug(slug)
    notes = _clean_text(notes, max_len=2000, field="Notes")
    icon_class = _validate_icon_class(icon_class)

    async with get_db_connection() as conn:
        try:
            cursor = await conn.execute(
                """
                INSERT INTO EXTERNAL_DEVICE_GROUPS
                    (user_id, slug, name, notes, icon_class)
                VALUES (?, ?, ?, ?, ?)
                """,
                (owner_user_id, slug, name, notes or None, icon_class),
            )
            await conn.commit()
            return int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            await conn.rollback()
            raise DeviceValidationError("Group slug already exists for this user") from exc


async def update_group(
    *,
    group_id: int,
    name: str,
    slug: str,
    notes: str,
    icon_class: str | None = None,
) -> None:
    name = _clean_text(name, max_len=120, field="Group name")
    if not name:
        raise DeviceValidationError("Group name is required")
    slug = validate_slug(slug)
    notes = _clean_text(notes, max_len=2000, field="Notes")
    icon_class = _validate_icon_class(icon_class)

    async with get_db_connection() as conn:
        try:
            cursor = await conn.execute(
                """
                UPDATE EXTERNAL_DEVICE_GROUPS
                SET slug = ?, name = ?, notes = ?, icon_class = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                  AND COALESCE(json_extract(metadata_json, '$.deleted'), 0) = 0
                """,
                (slug, name, notes or None, icon_class, group_id),
            )
            if cursor.rowcount == 0:
                raise DeviceValidationError("Group not found")
            await conn.commit()
        except sqlite3.IntegrityError as exc:
            await conn.rollback()
            raise DeviceValidationError("Group slug already exists for this user") from exc


async def soft_delete_group(group_id: int) -> None:
    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await conn.execute(
                """
                SELECT slug, name, metadata_json
                FROM EXTERNAL_DEVICE_GROUPS
                WHERE id = ?
                  AND COALESCE(json_extract(metadata_json, '$.deleted'), 0) = 0
                """,
                (group_id,),
            )
            row = await cursor.fetchone()
            if not row:
                raise DeviceValidationError("Group not found")

            deleted_slug = f"deleted-{group_id}-{row['slug']}"[:80].rstrip("-")
            deleted_name = f"{row['name']} (deleted)"[:120]
            metadata = _json_object(row["metadata_json"])
            metadata["deleted"] = True
            await conn.execute(
                """
                UPDATE EXTERNAL_DEVICE_GROUPS
                SET slug = ?, name = ?, metadata_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (deleted_slug, deleted_name, orjson.dumps(metadata).decode("utf-8"), group_id),
            )
            await conn.execute(
                "DELETE FROM EXTERNAL_DEVICE_GROUP_MEMBERS WHERE group_id = ?",
                (group_id,),
            )
            await conn.execute(
                """
                DELETE FROM EXTERNAL_DEVICE_BINDINGS
                WHERE target_type = 'group' AND target_id = ?
                """,
                (group_id,),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def add_device_to_group(
    *,
    device_id: int,
    group_id: int,
    is_primary_route_group: bool = False,
    routing_priority: int = 100,
) -> None:
    routing_priority = max(0, min(int(routing_priority), 10000))
    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.cursor()
        try:
            await _validate_device_group_same_owner(cursor, device_id, group_id)
            if is_primary_route_group:
                await cursor.execute(
                    """
                    UPDATE EXTERNAL_DEVICE_GROUP_MEMBERS
                    SET is_primary_route_group = 0
                    WHERE device_id = ?
                    """,
                    (device_id,),
                )
            await cursor.execute(
                """
                INSERT INTO EXTERNAL_DEVICE_GROUP_MEMBERS
                    (device_id, group_id, is_primary_route_group, routing_priority)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(device_id, group_id) DO UPDATE SET
                    is_primary_route_group = excluded.is_primary_route_group,
                    routing_priority = excluded.routing_priority
                """,
                (device_id, group_id, 1 if is_primary_route_group else 0, routing_priority),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def remove_device_from_group(*, device_id: int, group_id: int) -> None:
    async with get_db_connection() as conn:
        cursor = await conn.execute(
            """
            DELETE FROM EXTERNAL_DEVICE_GROUP_MEMBERS
            WHERE device_id = ? AND group_id = ?
            """,
            (device_id, group_id),
        )
        if cursor.rowcount == 0:
            raise DeviceValidationError("Membership not found")
        await conn.commit()


async def set_binding(
    *,
    target_type: str,
    target_id: int,
    conversation_id: int,
    response_mode: str = "text",
) -> None:
    if response_mode not in DEVICE_RESPONSE_MODES:
        raise DeviceValidationError("Invalid response mode")

    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.cursor()
        try:
            user_id = await _target_owner(cursor, target_type, target_id)
            await _validate_conversation_owner(
                cursor,
                conversation_id,
                user_id,
                allow_classic_platform=False,
            )
            await cursor.execute(
                """
                INSERT INTO EXTERNAL_DEVICE_BINDINGS
                    (user_id, target_type, target_id, conversation_id, response_mode)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(target_type, target_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    conversation_id = excluded.conversation_id,
                    response_mode = excluded.response_mode,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, target_type, target_id, conversation_id, response_mode),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def clear_binding(*, target_type: str, target_id: int) -> None:
    async with get_db_connection() as conn:
        cursor = await conn.execute(
            """
            DELETE FROM EXTERNAL_DEVICE_BINDINGS
            WHERE target_type = ? AND target_id = ?
            """,
            (target_type, target_id),
        )
        if cursor.rowcount == 0:
            raise DeviceValidationError("Binding not found")
        await conn.commit()


async def get_conversation_binding_summaries(
    user_id: int,
    conversation_ids: Iterable[int],
) -> dict[int, dict]:
    ids = sorted({int(conversation_id) for conversation_id in conversation_ids if conversation_id})
    if not ids:
        return {}

    summaries = {conversation_id: _empty_conversation_bindings() for conversation_id in ids}
    placeholders = ",".join("?" for _ in ids)

    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                f"""
                SELECT b.conversation_id, d.id, d.slug, d.display_name, d.device_type,
                       d.icon_class, d.enabled, b.response_mode
                FROM EXTERNAL_DEVICE_BINDINGS b
                JOIN EXTERNAL_DEVICES d ON d.id = b.target_id
                WHERE b.user_id = ?
                  AND b.target_type = 'device'
                  AND b.conversation_id IN ({placeholders})
                  AND d.user_id = b.user_id
                  AND COALESCE(json_extract(d.metadata_json, '$.deleted'), 0) = 0
                ORDER BY d.display_name COLLATE NOCASE ASC
                """,
                [user_id, *ids],
            )
            for row in await cursor.fetchall():
                conversation_id = int(row["conversation_id"])
                if conversation_id not in summaries:
                    continue
                summaries[conversation_id]["assigned_devices"].append(
                    {
                        "id": int(row["id"]),
                        "slug": row["slug"],
                        "display_name": row["display_name"],
                        "device_type": row["device_type"],
                        "icon_class": row["icon_class"],
                        "enabled": bool(row["enabled"]),
                        "response_mode": row["response_mode"] or "text",
                    }
                )

            cursor = await conn.execute(
                f"""
                SELECT b.conversation_id, g.id, g.slug, g.name, g.icon_class,
                       b.response_mode,
                       COUNT(DISTINCT d.id) AS member_count
                FROM EXTERNAL_DEVICE_BINDINGS b
                JOIN EXTERNAL_DEVICE_GROUPS g ON g.id = b.target_id
                LEFT JOIN EXTERNAL_DEVICE_GROUP_MEMBERS m ON m.group_id = g.id
                LEFT JOIN EXTERNAL_DEVICES d ON d.id = m.device_id
                    AND d.user_id = g.user_id
                    AND COALESCE(json_extract(d.metadata_json, '$.deleted'), 0) = 0
                WHERE b.user_id = ?
                  AND b.target_type = 'group'
                  AND b.conversation_id IN ({placeholders})
                  AND g.user_id = b.user_id
                  AND COALESCE(json_extract(g.metadata_json, '$.deleted'), 0) = 0
                GROUP BY b.conversation_id, g.id
                ORDER BY g.name COLLATE NOCASE ASC
                """,
                [user_id, *ids],
            )
            for row in await cursor.fetchall():
                conversation_id = int(row["conversation_id"])
                if conversation_id not in summaries:
                    continue
                summaries[conversation_id]["assigned_groups"].append(
                    {
                        "id": int(row["id"]),
                        "slug": row["slug"],
                        "name": row["name"],
                        "icon_class": row["icon_class"],
                        "member_count": int(row["member_count"] or 0),
                        "response_mode": row["response_mode"] or "text",
                    }
                )

            cursor = await conn.execute(
                """
                SELECT id, slug, display_name, device_type, icon_class, enabled
                FROM EXTERNAL_DEVICES
                WHERE user_id = ?
                  AND enabled = 1
                  AND COALESCE(json_extract(metadata_json, '$.deleted'), 0) = 0
                ORDER BY display_name COLLATE NOCASE ASC
                """,
                (user_id,),
            )
            for row in await cursor.fetchall():
                binding = await resolve_device_binding(conn, int(row["id"]))
                if binding.get("status") != "bound":
                    continue
                conversation_id = int(binding["conversation_id"])
                if conversation_id not in summaries:
                    continue
                summaries[conversation_id]["effective_devices"].append(
                    {
                        "id": int(row["id"]),
                        "slug": row["slug"],
                        "display_name": row["display_name"],
                        "device_type": row["device_type"],
                        "icon_class": row["icon_class"],
                        "enabled": bool(row["enabled"]),
                        "source": binding.get("source"),
                        "group_id": binding.get("group_id"),
                        "group_name": binding.get("group_name"),
                    }
                )
    except sqlite3.OperationalError as exc:
        if "EXTERNAL_DEVICE" not in str(exc).upper():
            raise
        logger.warning("[devices] External device tables unavailable while loading chat summaries")

    return {
        conversation_id: _finalize_conversation_binding_summary(summary)
        for conversation_id, summary in summaries.items()
    }


async def conversation_has_external_device_bindings(
    *,
    user_id: int,
    conversation_id: int,
) -> bool:
    summaries = await get_conversation_binding_summaries(user_id, [conversation_id])
    summary = summaries.get(conversation_id) or _empty_conversation_bindings()
    return bool(
        summary.get("effective_count")
        or summary.get("assigned_devices")
        or summary.get("assigned_groups")
    )


async def _collect_user_device_conversation_ids(conn, user_id: int) -> set[int]:
    conversation_ids: set[int] = set()
    cursor = await conn.execute(
        """
        SELECT DISTINCT conversation_id
        FROM EXTERNAL_DEVICE_BINDINGS
        WHERE user_id = ?
        """,
        (user_id,),
    )
    conversation_ids.update(
        int(row["conversation_id"])
        for row in await cursor.fetchall()
        if row["conversation_id"]
    )

    cursor = await conn.execute(
        """
        SELECT id
        FROM EXTERNAL_DEVICES
        WHERE user_id = ?
          AND enabled = 1
          AND COALESCE(json_extract(metadata_json, '$.deleted'), 0) = 0
        """,
        (user_id,),
    )
    for row in await cursor.fetchall():
        binding = await resolve_device_binding(conn, int(row["id"]))
        if binding.get("status") == "bound" and binding.get("conversation_id"):
            conversation_ids.add(int(binding["conversation_id"]))
    return conversation_ids


async def get_conversation_external_bindings(
    *,
    user_id: int,
    conversation_id: int,
) -> dict:
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await _validate_conversation_owner(cursor, conversation_id, user_id)
        classic_platform = await _classic_platform_for_conversation(
            cursor,
            conversation_id=conversation_id,
            user_id=user_id,
        )
        summaries = await get_conversation_binding_summaries(user_id, [conversation_id])
        summary = summaries.get(conversation_id, _empty_conversation_bindings())

        await cursor.execute(
            """
            SELECT d.id, d.slug, d.display_name, d.device_type, d.icon_class,
                   d.enabled, b.conversation_id AS bound_conversation_id,
                   COALESCE(c.chat_name, 'New Chat') AS bound_conversation_name
            FROM EXTERNAL_DEVICES d
            LEFT JOIN EXTERNAL_DEVICE_BINDINGS b
              ON b.target_type = 'device'
             AND b.target_id = d.id
             AND b.user_id = d.user_id
            LEFT JOIN CONVERSATIONS c
              ON c.id = b.conversation_id
             AND c.user_id = d.user_id
            WHERE d.user_id = ?
              AND COALESCE(json_extract(d.metadata_json, '$.deleted'), 0) = 0
            ORDER BY d.display_name COLLATE NOCASE ASC
            """,
            (user_id,),
        )
        devices = []
        for row in await cursor.fetchall():
            devices.append(
                {
                    "id": int(row["id"]),
                    "slug": row["slug"],
                    "display_name": row["display_name"],
                    "device_type": row["device_type"],
                    "icon_class": row["icon_class"],
                    "enabled": bool(row["enabled"]),
                    "assigned": row["bound_conversation_id"] == conversation_id,
                    "bound_conversation_id": row["bound_conversation_id"],
                    "bound_conversation_name": row["bound_conversation_name"],
                }
            )

        await cursor.execute(
            """
            SELECT g.id, g.slug, g.name, g.icon_class,
                   COUNT(DISTINCT d.id) AS member_count,
                   b.conversation_id AS bound_conversation_id,
                   COALESCE(c.chat_name, 'New Chat') AS bound_conversation_name
            FROM EXTERNAL_DEVICE_GROUPS g
            LEFT JOIN EXTERNAL_DEVICE_GROUP_MEMBERS m ON m.group_id = g.id
            LEFT JOIN EXTERNAL_DEVICES d ON d.id = m.device_id
                AND d.user_id = g.user_id
                AND COALESCE(json_extract(d.metadata_json, '$.deleted'), 0) = 0
            LEFT JOIN EXTERNAL_DEVICE_BINDINGS b
              ON b.target_type = 'group'
             AND b.target_id = g.id
             AND b.user_id = g.user_id
            LEFT JOIN CONVERSATIONS c
              ON c.id = b.conversation_id
             AND c.user_id = g.user_id
            WHERE g.user_id = ?
              AND COALESCE(json_extract(g.metadata_json, '$.deleted'), 0) = 0
            GROUP BY g.id
            ORDER BY g.name COLLATE NOCASE ASC
            """,
            (user_id,),
        )
        groups = []
        for row in await cursor.fetchall():
            groups.append(
                {
                    "id": int(row["id"]),
                    "slug": row["slug"],
                    "name": row["name"],
                    "icon_class": row["icon_class"],
                    "member_count": int(row["member_count"] or 0),
                    "assigned": row["bound_conversation_id"] == conversation_id,
                    "bound_conversation_id": row["bound_conversation_id"],
                    "bound_conversation_name": row["bound_conversation_name"],
                }
            )

    return {
        "conversation_id": conversation_id,
        "external_platform": classic_platform,
        "assignable": classic_platform is None,
        "external_bindings": summary,
        "devices": devices,
        "groups": groups,
    }


async def update_conversation_external_bindings(
    *,
    user_id: int,
    conversation_id: int,
    device_ids,
    group_ids,
) -> dict:
    selected_device_ids = _parse_id_list(device_ids, "device_ids")
    selected_group_ids = _parse_id_list(group_ids, "group_ids")

    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.cursor()
        affected_conversation_ids = {conversation_id}
        try:
            await _validate_conversation_owner(
                cursor,
                conversation_id,
                user_id,
                allow_classic_platform=False,
            )

            if selected_device_ids:
                placeholders = ",".join("?" for _ in selected_device_ids)
                await cursor.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM EXTERNAL_DEVICES
                    WHERE user_id = ?
                      AND id IN ({placeholders})
                      AND COALESCE(json_extract(metadata_json, '$.deleted'), 0) = 0
                    """,
                    [user_id, *selected_device_ids],
                )
                row = await cursor.fetchone()
                if int(row[0] or 0) != len(selected_device_ids):
                    raise DeviceValidationError(
                        "One or more devices do not belong to the owner",
                        code="target_forbidden",
                        status_code=403,
                    )

            await _validate_groups_owner(cursor, selected_group_ids, user_id)
            affected_conversation_ids.update(
                await _collect_user_device_conversation_ids(conn, user_id)
            )

            affected_clauses = ["conversation_id = ?"]
            affected_params = [conversation_id]
            if selected_device_ids:
                placeholders = ",".join("?" for _ in selected_device_ids)
                affected_clauses.append(
                    f"(target_type = 'device' AND target_id IN ({placeholders}))"
                )
                affected_params.extend(selected_device_ids)
            if selected_group_ids:
                placeholders = ",".join("?" for _ in selected_group_ids)
                affected_clauses.append(
                    f"(target_type = 'group' AND target_id IN ({placeholders}))"
                )
                affected_params.extend(selected_group_ids)
            await cursor.execute(
                f"""
                SELECT DISTINCT conversation_id
                FROM EXTERNAL_DEVICE_BINDINGS
                WHERE user_id = ?
                  AND ({' OR '.join(affected_clauses)})
                """,
                [user_id, *affected_params],
            )
            affected_conversation_ids.update(
                int(row["conversation_id"])
                for row in await cursor.fetchall()
                if row["conversation_id"]
            )

            if selected_device_ids:
                placeholders = ",".join("?" for _ in selected_device_ids)
                await cursor.execute(
                    f"""
                    DELETE FROM EXTERNAL_DEVICE_BINDINGS
                    WHERE user_id = ?
                      AND target_type = 'device'
                      AND conversation_id = ?
                      AND target_id NOT IN ({placeholders})
                    """,
                    [user_id, conversation_id, *selected_device_ids],
                )
            else:
                await cursor.execute(
                    """
                    DELETE FROM EXTERNAL_DEVICE_BINDINGS
                    WHERE user_id = ?
                      AND target_type = 'device'
                      AND conversation_id = ?
                    """,
                    (user_id, conversation_id),
                )

            if selected_group_ids:
                placeholders = ",".join("?" for _ in selected_group_ids)
                await cursor.execute(
                    f"""
                    DELETE FROM EXTERNAL_DEVICE_BINDINGS
                    WHERE user_id = ?
                      AND target_type = 'group'
                      AND conversation_id = ?
                      AND target_id NOT IN ({placeholders})
                    """,
                    [user_id, conversation_id, *selected_group_ids],
                )
            else:
                await cursor.execute(
                    """
                    DELETE FROM EXTERNAL_DEVICE_BINDINGS
                    WHERE user_id = ?
                      AND target_type = 'group'
                      AND conversation_id = ?
                    """,
                    (user_id, conversation_id),
                )

            for device_id in selected_device_ids:
                await cursor.execute(
                    """
                    INSERT INTO EXTERNAL_DEVICE_BINDINGS
                        (user_id, target_type, target_id, conversation_id, response_mode)
                    VALUES (?, 'device', ?, ?, 'text')
                    ON CONFLICT(target_type, target_id) DO UPDATE SET
                        user_id = excluded.user_id,
                        conversation_id = excluded.conversation_id,
                        response_mode = excluded.response_mode,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (user_id, device_id, conversation_id),
                )

            for group_id in selected_group_ids:
                await cursor.execute(
                    """
                    INSERT INTO EXTERNAL_DEVICE_BINDINGS
                        (user_id, target_type, target_id, conversation_id, response_mode)
                    VALUES (?, 'group', ?, ?, 'text')
                    ON CONFLICT(target_type, target_id) DO UPDATE SET
                        user_id = excluded.user_id,
                        conversation_id = excluded.conversation_id,
                        response_mode = excluded.response_mode,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (user_id, group_id, conversation_id),
                )

            affected_conversation_ids.update(
                await _collect_user_device_conversation_ids(conn, user_id)
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    result = await get_conversation_external_bindings(
        user_id=user_id,
        conversation_id=conversation_id,
    )
    result["affected_conversations"] = await get_conversation_binding_summaries(
        user_id,
        affected_conversation_ids,
    )
    return result
