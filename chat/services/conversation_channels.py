"""Read-only channel projection for conversation list cards.

The legacy chat payload exposes one ``external_platform`` value even though a
conversation can be used by WhatsApp, Telegram and telephone at the same time.
This module builds the additive plural projection without moving telephone
state into ``USER_DETAILS.external_platforms``.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import asynccontextmanager
import inspect
import sqlite3
from typing import Any

import orjson

from database import get_db_connection


EXTERNAL_CHANNEL_ORDER = ("whatsapp", "telegram", "phone")
MESSAGING_CHANNELS = frozenset({"whatsapp", "telegram"})


def _positive_id(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def empty_channel_summary() -> dict[str, Any]:
    return {"external_channels": [], "phone_binding": None}


def channel_summary_with_legacy(
    summary: dict[str, Any] | None, legacy_platform: Any = None
) -> dict[str, Any]:
    if summary is not None:
        return summary
    fallback = empty_channel_summary()
    if legacy_platform in MESSAGING_CHANNELS:
        fallback["external_channels"] = [legacy_platform]
    return fallback


def legacy_external_platform(summary: dict[str, Any] | None) -> str | None:
    channels = (summary or {}).get("external_channels") or []
    for channel in EXTERNAL_CHANNEL_ORDER:
        if channel in MESSAGING_CHANNELS and channel in channels:
            return channel
    return None


def has_messaging_channel(summary: dict[str, Any] | None) -> bool:
    channels = (summary or {}).get("external_channels") or []
    return any(channel in MESSAGING_CHANNELS for channel in channels)


@asynccontextmanager
async def _read_connection(existing=None):
    if existing is not None:
        yield existing
        return
    async with get_db_connection(readonly=True) as conn:
        yield conn


async def get_conversation_channel_summaries(
    user_id: int,
    conversation_ids: Iterable[int] | None = None,
    *,
    conn=None,
) -> dict[int, dict[str, Any]]:
    """Return active messaging/telephone assignments in a constant query count."""

    requested_ids = None
    if conversation_ids is not None:
        requested_ids = sorted(
            {
                parsed
                for value in conversation_ids
                if (parsed := _positive_id(value)) is not None
            }
        )
        if not requested_ids:
            return {}

    summaries: dict[int, dict[str, Any]] = {}

    def include(conversation_id: int | None) -> bool:
        return conversation_id is not None and (
            requested_ids is None or conversation_id in requested_ids
        )

    def ensure(conversation_id: int) -> dict[str, Any]:
        return summaries.setdefault(conversation_id, empty_channel_summary())

    async with _read_connection(conn) as active_conn:
        cursor_result = active_conn.cursor()
        cursor = await cursor_result if inspect.isawaitable(cursor_result) else cursor_result
        await cursor.execute(
            "SELECT external_platforms FROM USER_DETAILS WHERE user_id = ?",
            (int(user_id),),
        )
        row = await cursor.fetchone()
        try:
            platforms = orjson.loads(row[0]) if row and row[0] else {}
        except (orjson.JSONDecodeError, TypeError):
            platforms = {}
        if not isinstance(platforms, dict):
            platforms = {}

        for channel in ("whatsapp", "telegram"):
            platform_data = platforms.get(channel)
            conversation_id = _positive_id(
                platform_data.get("conversation_id")
                if isinstance(platform_data, dict)
                else None
            )
            if include(conversation_id):
                ensure(conversation_id)["external_channels"].append(channel)

        params: list[Any] = [int(user_id)]
        id_filter = ""
        if requested_ids is not None:
            placeholders = ",".join("?" for _ in requested_ids)
            id_filter = f" AND b.conversation_id IN ({placeholders})"
            params.extend(requested_ids)
        try:
            await cursor.execute(
                f"""
                SELECT b.id, b.conversation_id, b.contact_id,
                       b.allow_inbound, b.allow_outbound, c.display_name
                FROM PHONE_CONVERSATION_BINDINGS b
                JOIN PHONE_CONTACTS c
                  ON c.id = b.contact_id
                 AND c.owner_user_id = b.owner_user_id
                 AND c.active = 1
                JOIN USERS u ON u.id = b.owner_user_id
                JOIN USER_ROLES r ON r.id = u.role_id
                WHERE b.owner_user_id = ?
                  AND b.active = 1{id_filter}
                  AND COALESCE(u.is_enabled, 0) = 1
                  AND c.e164 = u.phone_number
                  AND (
                    lower(r.role_name) = 'admin'
                    OR COALESCE(u.phone_verified, 0) = 1
                  )
                ORDER BY b.conversation_id
                """,
                params,
            )
            phone_rows = await cursor.fetchall()
        except sqlite3.OperationalError as exc:
            if "PHONE_" not in str(exc).upper():
                raise
            phone_rows = []

        for phone_row in phone_rows:
            try:
                conversation_id = _positive_id(phone_row["conversation_id"])
            except (KeyError, TypeError, IndexError):
                continue
            if not include(conversation_id):
                continue
            summary = ensure(conversation_id)
            if "phone" not in summary["external_channels"]:
                summary["external_channels"].append("phone")
            summary["phone_binding"] = {
                "id": int(phone_row["id"]),
                "contact_id": int(phone_row["contact_id"]),
                "display_name": str(phone_row["display_name"] or "Phone contact"),
                "allow_inbound": bool(phone_row["allow_inbound"]),
                "allow_outbound": bool(phone_row["allow_outbound"]),
            }

    order = {channel: index for index, channel in enumerate(EXTERNAL_CHANNEL_ORDER)}
    for summary in summaries.values():
        summary["external_channels"] = sorted(
            set(summary["external_channels"]), key=lambda channel: order[channel]
        )
    return summaries
