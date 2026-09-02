"""Read-only telephone provenance for the normal paginated chat history."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from log_config import logger


_CALL_TERMINAL_STATUSES = frozenset(
    {"completed", "busy", "no_answer", "machine", "failed", "canceled"}
)
_PURGE_STATUSES = frozenset(
    {"scheduled", "running", "needs_attention", "completed"}
)


@dataclass(slots=True)
class PhoneHistoryPage:
    """Telephone metadata for exactly one existing MESSAGES page."""

    message_metadata: dict[int, dict[str, Any]] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    markers: list[dict[str, Any]] = field(default_factory=list)

    def public_payload(self) -> dict[str, Any]:
        return {"calls": self.calls, "markers": self.markers}


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row)


def _timeline(row: dict[str, Any]) -> list[dict[str, str]]:
    events = (
        ("created", row.get("created_at")),
        ("initiated", row.get("initiated_at")),
        ("ringing", row.get("ringing_at")),
        ("answered", row.get("answered_at")),
        ("ended", row.get("ended_at")),
    )
    return [
        {"event": event, "at": str(timestamp)}
        for event, timestamp in events
        if timestamp
    ]


def _call_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "direction": row.get("direction"),
        "status": row.get("status"),
        "answered_by": row.get("answered_by"),
        "initiated_at": row.get("initiated_at"),
        "ringing_at": row.get("ringing_at"),
        "answered_at": row.get("answered_at"),
        "ended_at": row.get("ended_at"),
        "duration_seconds": row.get("duration_seconds"),
        "termination_reason": row.get("termination_reason"),
        "error_code": row.get("last_error_code"),
        "estimated_cost": row.get("estimated_cost"),
        "final_cost": row.get("final_cost"),
        "currency": row.get("currency"),
        "recording_enabled": bool(row.get("recording_enabled")),
        "amd_enabled": bool(row.get("amd_enabled")),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "timeline": _timeline(row),
        "cost_components": [],
        "provenance_summary": {
            "total_messages": 0,
            "phone_messages": 0,
            "interrupted_messages": 0,
            "played_ms": 0,
            "delivery_states": {},
            "participants": {},
            "origin_channels": {},
            "turn_ids": [],
        },
        "recording": {
            "present": False,
            "status": None,
            "audio_available": False,
        },
        "purge": None,
        "audio": None,
    }


def _safe_purge_payload(row: Any) -> dict[str, Any] | None:
    status = str(row["status"] or "")
    if status not in _PURGE_STATUSES:
        return None
    scope = str(row["purge_scope"] or "")
    if scope not in {"call", "recording"}:
        return None
    try:
        attempt = max(0, int(row["attempt_count"] or 0))
    except (TypeError, ValueError):
        attempt = 0
    return {
        "status": status,
        "scope": scope,
        "attempt": attempt,
        # Raw worker errors can contain private paths or provider identifiers.
        "error": (
            "Deletion requires administrator attention."
            if status == "needs_attention" and row["last_error"]
            else None
        ),
    }


def _message_payload(row: dict[str, Any]) -> dict[str, Any]:
    origin_channel = str(row["origin_channel"])
    linked_at = row.get("link_created_at")
    return {
        "phone_call_id": str(row["call_id"]),
        "channel": origin_channel,
        "participant": row.get("participant"),
        "provenance": {
            "phone_call_id": str(row["call_id"]),
            "channel": "phone",
            "origin_channel": origin_channel,
            "participant": row.get("participant"),
            "turn_id": row.get("turn_id"),
        },
        "interrupted": bool(row.get("interrupted")),
        "played_ms": row.get("played_ms"),
        "delivery_state": row.get("delivery_state"),
        "phone_timestamps": {
            "linked_at": linked_at,
            "call_started_at": row.get("call_started_at"),
            "call_answered_at": row.get("call_answered_at"),
            "call_ended_at": row.get("call_ended_at"),
        },
    }


async def load_phone_history_page(
    connection_factory,
    *,
    conversation_id: int,
    owner_user_id: int,
    message_ids: list[int],
    newest_page: bool,
    allow_admin: bool = False,
) -> PhoneHistoryPage:
    """Load canonical call links and virtual boundaries for one message page.

    The existing message cursor remains authoritative. Calls without any linked
    message are assigned to exactly one deterministic page by their first
    chronological message anchor, or to the newest page when no later message
    exists. This keeps refreshes and older-page loads idempotent.
    """

    result = PhoneHistoryPage()
    normalized_ids = sorted({int(value) for value in message_ids if int(value) > 0})
    scope_sql = "" if allow_admin else " AND c.owner_user_id=?"
    scope_params: tuple[Any, ...] = () if allow_admin else (int(owner_user_id),)

    try:
        async with connection_factory(readonly=True) as conn:
            link_rows: list[dict[str, Any]] = []
            if normalized_ids:
                placeholders = ",".join("?" for _ in normalized_ids)
                cursor = await conn.execute(
                    f"""
                    SELECT l.call_id,l.message_id,l.participant,l.turn_id,
                           l.origin_channel,l.interrupted,l.played_ms,
                           l.delivery_state,l.created_at AS link_created_at,
                           COALESCE(c.initiated_at,c.created_at) AS call_started_at,
                           c.answered_at AS call_answered_at,c.ended_at AS call_ended_at
                    FROM PHONE_CALL_MESSAGE_LINKS l
                    JOIN PHONE_CALLS c ON c.id=l.call_id
                    WHERE c.conversation_id=? AND c.deleted_at IS NULL
                      AND l.message_id IN ({placeholders}){scope_sql}
                    ORDER BY l.message_id,l.id
                    """,
                    (int(conversation_id), *normalized_ids, *scope_params),
                )
                link_rows = [_row_dict(row) for row in await cursor.fetchall()]
                for row in link_rows:
                    result.message_metadata.setdefault(
                        int(row["message_id"]),
                        _message_payload(row),
                    )

            boundaries: list[dict[str, Any]] = []
            linked_candidate_ids = sorted(
                {str(row["call_id"]) for row in link_rows}
            )
            if linked_candidate_ids:
                linked_placeholders = ",".join("?" for _ in linked_candidate_ids)
                cursor = await conn.execute(
                    f"""
                    SELECT c.id,
                           MIN(l.message_id) AS first_message_id,
                           MAX(l.message_id) AS last_message_id,
                           COUNT(l.id) AS link_count,
                           SUM(CASE WHEN l.origin_channel='phone' THEN 1 ELSE 0 END)
                               AS transcript_link_count,
                           COALESCE(c.initiated_at,c.created_at) AS started_at,
                           c.ended_at AS finished_at,c.status AS call_status,
                           NULL AS no_link_start_anchor_message_id,
                           NULL AS no_link_end_anchor_message_id
                    FROM PHONE_CALLS c
                    JOIN PHONE_CALL_MESSAGE_LINKS l ON l.call_id=c.id
                    WHERE c.id IN ({linked_placeholders})
                      AND c.conversation_id=? AND c.deleted_at IS NULL{scope_sql}
                    GROUP BY c.id
                    """,
                    (
                        *linked_candidate_ids,
                        int(conversation_id),
                        *scope_params,
                    ),
                )
                boundaries.extend(
                    _row_dict(row) for row in await cursor.fetchall()
                )

            unlinked_candidate_ids: list[str] = []
            temporal_sql = "0=1"
            temporal_params: list[Any] = []
            if normalized_ids:
                page_placeholders = ",".join("?" for _ in normalized_ids)
                cursor = await conn.execute(
                    f"""
                    SELECT id,date FROM MESSAGES
                    WHERE conversation_id=? AND id IN ({page_placeholders})
                    ORDER BY julianday(date),id
                    """,
                    (int(conversation_id), *normalized_ids),
                )
                page_messages = [_row_dict(row) for row in await cursor.fetchall()]
                if page_messages:
                    first_page_message = page_messages[0]
                    last_page_message = page_messages[-1]
                    cursor = await conn.execute(
                        """
                        SELECT date FROM MESSAGES
                        WHERE conversation_id=? AND (
                            julianday(date) < julianday(?)
                            OR (julianday(date)=julianday(?) AND id < ?)
                        )
                        ORDER BY julianday(date) DESC,id DESC LIMIT 1
                        """,
                        (
                            int(conversation_id),
                            first_page_message["date"],
                            first_page_message["date"],
                            int(first_page_message["id"]),
                        ),
                    )
                    previous_row = await cursor.fetchone()
                    cursor = await conn.execute(
                        """
                        SELECT date FROM MESSAGES WHERE conversation_id=?
                        ORDER BY julianday(date) DESC,id DESC LIMIT 1
                        """,
                        (int(conversation_id),),
                    )
                    latest_row = await cursor.fetchone()

                    def event_window(expression: str) -> tuple[str, list[Any]]:
                        clauses = [f"julianday({expression}) <= julianday(?)"]
                        params: list[Any] = [last_page_message["date"]]
                        if previous_row is not None:
                            clauses.append(f"julianday({expression}) >= julianday(?)")
                            params.append(previous_row["date"])
                        anchored = "(" + " AND ".join(clauses) + ")"
                        if newest_page and latest_row is not None:
                            anchored += (
                                f" OR julianday({expression}) > julianday(?)"
                                f" OR julianday({expression}) IS NULL"
                            )
                            params.append(latest_row["date"])
                        return f"({anchored})", params

                    start_window, start_params = event_window(
                        "COALESCE(c.initiated_at,c.created_at)"
                    )
                    end_window, end_params = event_window("c.ended_at")
                    temporal_sql = f"""
                        {start_window}
                        OR (
                            c.ended_at IS NOT NULL
                            AND c.status IN (
                                'completed','busy','no_answer','machine',
                                'failed','canceled'
                            )
                            AND {end_window}
                        )
                    """
                    temporal_params = [*start_params, *end_params]
            elif newest_page:
                # With no MESSAGES rows every no-link call belongs to the only
                # virtual history page, so there is no narrower time interval.
                temporal_sql = "1=1"

            cursor = await conn.execute(
                f"""
                SELECT c.id FROM PHONE_CALLS c
                WHERE c.conversation_id=? AND c.deleted_at IS NULL{scope_sql}
                  AND NOT EXISTS (
                      SELECT 1 FROM PHONE_CALL_MESSAGE_LINKS l
                      WHERE l.call_id=c.id
                  )
                  AND ({temporal_sql})
                """,
                (
                    int(conversation_id),
                    *scope_params,
                    *temporal_params,
                ),
            )
            unlinked_candidate_ids = [str(row["id"]) for row in await cursor.fetchall()]

            if unlinked_candidate_ids:
                unlinked_placeholders = ",".join(
                    "?" for _ in unlinked_candidate_ids
                )
                cursor = await conn.execute(
                    f"""
                    SELECT c.id,NULL AS first_message_id,NULL AS last_message_id,
                           0 AS link_count,0 AS transcript_link_count,
                           COALESCE(c.initiated_at,c.created_at) AS started_at,
                           c.ended_at AS finished_at,c.status AS call_status,
                           (
                               SELECT m.id FROM MESSAGES m
                               WHERE m.conversation_id=c.conversation_id
                                 AND julianday(m.date) >= julianday(
                                     COALESCE(c.initiated_at,c.created_at)
                                 )
                               ORDER BY julianday(m.date),m.id LIMIT 1
                           ) AS no_link_start_anchor_message_id,
                           (
                               SELECT m.id FROM MESSAGES m
                               WHERE m.conversation_id=c.conversation_id
                                 AND julianday(m.date) >= julianday(c.ended_at)
                               ORDER BY julianday(m.date),m.id LIMIT 1
                           ) AS no_link_end_anchor_message_id
                    FROM PHONE_CALLS c
                    WHERE c.id IN ({unlinked_placeholders})
                      AND c.conversation_id=? AND c.deleted_at IS NULL{scope_sql}
                    """,
                    (
                        *unlinked_candidate_ids,
                        int(conversation_id),
                        *scope_params,
                    ),
                )
                boundaries.extend(
                    _row_dict(row) for row in await cursor.fetchall()
                )
            boundaries.sort(
                key=lambda row: (row.get("started_at") or "", str(row["id"]))
            )

            page_ids = set(normalized_ids)
            relevant_call_ids = {str(row["call_id"]) for row in link_rows}
            markers: list[dict[str, Any]] = []
            for boundary in boundaries:
                call_id = str(boundary["id"])
                first_id = boundary.get("first_message_id")
                last_id = boundary.get("last_message_id")
                transcript_present = bool(boundary.get("transcript_link_count"))
                has_real_end = bool(boundary.get("finished_at")) and (
                    str(boundary.get("call_status")) in _CALL_TERMINAL_STATUSES
                )

                if first_id is None:
                    raw_start_anchor = boundary.get(
                        "no_link_start_anchor_message_id"
                    )
                    raw_end_anchor = boundary.get("no_link_end_anchor_message_id")
                    start_anchor = (
                        int(raw_start_anchor) if raw_start_anchor is not None else None
                    )
                    end_anchor = (
                        int(raw_end_anchor) if raw_end_anchor is not None else None
                    )
                    start_belongs = (
                        start_anchor in page_ids
                        if start_anchor is not None
                        else newest_page
                    )
                    end_belongs = has_real_end and (
                        end_anchor in page_ids
                        if end_anchor is not None
                        else newest_page
                    )
                    if not start_belongs and not end_belongs:
                        continue
                    relevant_call_ids.add(call_id)
                    if start_belongs:
                        markers.append(
                            {
                                "id": f"phone-call:{call_id}:start",
                                "phone_call_id": call_id,
                                "kind": "start",
                                "anchor_message_id": start_anchor,
                                "placement": "before",
                                "occurred_at": boundary.get("started_at"),
                                "transcript_present": transcript_present,
                            }
                        )
                    if end_belongs:
                        markers.append(
                            {
                                "id": f"phone-call:{call_id}:end",
                                "phone_call_id": call_id,
                                "kind": "end",
                                "anchor_message_id": end_anchor,
                                "placement": "before",
                                "occurred_at": boundary.get("finished_at"),
                                "transcript_present": transcript_present,
                            }
                        )
                    continue

                if int(first_id) not in page_ids and int(last_id) not in page_ids:
                    continue

                relevant_call_ids.add(call_id)
                if first_id is None or int(first_id) in page_ids:
                    markers.append(
                        {
                            "id": f"phone-call:{call_id}:start",
                            "phone_call_id": call_id,
                            "kind": "start",
                            "anchor_message_id": (
                                int(first_id) if first_id is not None else None
                            ),
                            "placement": "before",
                            "occurred_at": boundary.get("started_at"),
                            "transcript_present": transcript_present,
                        }
                    )
                if has_real_end and (last_id is None or int(last_id) in page_ids):
                    markers.append(
                        {
                            "id": f"phone-call:{call_id}:end",
                            "phone_call_id": call_id,
                            "kind": "end",
                            "anchor_message_id": (
                                int(last_id) if last_id is not None else None
                            ),
                            "placement": "after",
                            "occurred_at": boundary.get("finished_at"),
                            "transcript_present": transcript_present,
                        }
                    )

            result.markers = sorted(
                markers,
                key=lambda marker: (
                    marker.get("anchor_message_id") is None,
                    marker.get("anchor_message_id") or 0,
                    marker.get("occurred_at") or "",
                    0 if marker["kind"] == "start" else 1,
                    marker["id"],
                ),
            )

            if not relevant_call_ids:
                return result

            call_ids = sorted(relevant_call_ids)
            placeholders = ",".join("?" for _ in call_ids)
            cursor = await conn.execute(
                f"""
                SELECT c.id,c.owner_user_id,c.direction,c.status,c.answered_by,c.initiated_at,
                       c.ringing_at,c.answered_at,c.ended_at,c.duration_seconds,
                       c.termination_reason,c.estimated_cost,c.final_cost,c.currency,
                       c.recording_enabled,c.amd_enabled,c.created_at,c.updated_at,
                       j.last_error_code
                FROM PHONE_CALLS c
                LEFT JOIN PHONE_CALL_JOBS j ON j.id=c.job_id
                WHERE c.id IN ({placeholders}) AND c.conversation_id=?
                  AND c.deleted_at IS NULL{scope_sql}
                ORDER BY julianday(COALESCE(c.initiated_at,c.created_at)),c.id
                """,
                (*call_ids, int(conversation_id), *scope_params),
            )
            calls: dict[str, dict[str, Any]] = {}
            audio_authorized_call_ids: set[str] = set()
            for raw_row in await cursor.fetchall():
                row = _row_dict(raw_row)
                call_id = str(row["id"])
                calls[call_id] = _call_payload(row)
                if int(row["owner_user_id"]) == int(owner_user_id):
                    audio_authorized_call_ids.add(call_id)

            cursor = await conn.execute(
                f"""
                SELECT call_id,participant,turn_id,origin_channel,interrupted,
                       played_ms,delivery_state
                FROM PHONE_CALL_MESSAGE_LINKS
                WHERE call_id IN ({placeholders})
                ORDER BY call_id,message_id,id
                """,
                tuple(call_ids),
            )
            for row in await cursor.fetchall():
                call = calls.get(str(row["call_id"]))
                if call is None:
                    continue
                summary = call["provenance_summary"]
                summary["total_messages"] += 1
                if row["origin_channel"] == "phone":
                    summary["phone_messages"] += 1
                if bool(row["interrupted"]):
                    summary["interrupted_messages"] += 1
                if row["played_ms"] is not None:
                    summary["played_ms"] += int(row["played_ms"])
                for summary_key, value in (
                    ("delivery_states", row["delivery_state"]),
                    ("participants", row["participant"]),
                    ("origin_channels", row["origin_channel"]),
                ):
                    normalized_value = str(value or "unknown")
                    counts = summary[summary_key]
                    counts[normalized_value] = counts.get(normalized_value, 0) + 1
                turn_id = str(row["turn_id"] or "").strip()
                if turn_id and turn_id not in summary["turn_ids"]:
                    summary["turn_ids"].append(turn_id)

            cursor = await conn.execute(
                f"""
                SELECT call_id,provider,component_type,quantity,unit,
                       provider_cost,customer_charge,currency,state,occurred_at
                FROM PHONE_CALL_COST_COMPONENTS
                WHERE call_id IN ({placeholders})
                ORDER BY call_id,occurred_at,id
                """,
                tuple(call_ids),
            )
            for row in await cursor.fetchall():
                call = calls.get(str(row["call_id"]))
                if call is None:
                    continue
                component = {
                    key: row[key]
                    for key in (
                        "provider",
                        "component_type",
                        "quantity",
                        "unit",
                        "customer_charge",
                        "currency",
                        "state",
                        "occurred_at",
                    )
                }
                if allow_admin:
                    component["provider_cost"] = row["provider_cost"]
                call["cost_components"].append(component)

            cursor = await conn.execute(
                f"""
                SELECT call_id,status,participant_path,assistant_path,mixed_path
                FROM PHONE_RECORDINGS
                WHERE call_id IN ({placeholders})
                ORDER BY call_id,id DESC
                """,
                tuple(call_ids),
            )
            for row in await cursor.fetchall():
                call_id = str(row["call_id"])
                call = calls.get(call_id)
                if call is None:
                    continue
                recording = call["recording"]
                if not recording["present"]:
                    recording["present"] = True
                    recording["status"] = str(row["status"] or "unknown")
                if (
                    call_id not in audio_authorized_call_ids
                    or row["status"] != "available"
                    or call["audio"] is not None
                ):
                    continue
                tracks = []
                for public_name, column in (
                    ("mixed", "mixed_path"),
                    ("participant", "participant_path"),
                    ("assistant", "assistant_path"),
                ):
                    if row[column]:
                        tracks.append(
                            {
                                "track": public_name,
                                "url": (
                                    f"/api/phone-calls/{quote(call_id, safe='')}"
                                    f"/recording?track={public_name}"
                                ),
                            }
                        )
                if tracks:
                    call["audio"] = {"tracks": tracks}
                    recording["audio_available"] = True

            # PHONE_DATA_PURGE_JOBS was introduced after telephone history.
            # Probe its public columns so older databases retain call history.
            cursor = await conn.execute("PRAGMA table_info(PHONE_DATA_PURGE_JOBS)")
            purge_columns = {str(row["name"]) for row in await cursor.fetchall()}
            required_purge_columns = {
                "id",
                "owner_user_id_snapshot",
                "conversation_id_snapshot",
                "call_id_snapshot",
                "purge_scope",
                "status",
                "attempt_count",
                "last_error",
                "created_at",
            }
            if required_purge_columns.issubset(purge_columns):
                cursor = await conn.execute(
                    f"""
                    SELECT id,call_id_snapshot,purge_scope,status,attempt_count,
                           last_error,created_at
                    FROM PHONE_DATA_PURGE_JOBS
                    WHERE call_id_snapshot IN ({placeholders})
                      AND owner_user_id_snapshot=?
                      AND conversation_id_snapshot=?
                    ORDER BY call_id_snapshot,created_at DESC,id DESC
                    """,
                    (*call_ids, int(owner_user_id), int(conversation_id)),
                )
                for row in await cursor.fetchall():
                    call = calls.get(str(row["call_id_snapshot"] or ""))
                    if call is None or call["purge"] is not None:
                        continue
                    call["purge"] = _safe_purge_payload(row)

            result.calls = [calls[call_id] for call_id in call_ids if call_id in calls]
            return result
    except (sqlite3.OperationalError, TypeError, ValueError):
        # Older/test databases without the telephony migration retain the exact
        # historical message contract instead of breaking chat reads.
        logger.debug(
            "Telephone history is unavailable for conversation %s",
            conversation_id,
            exc_info=True,
        )
        return PhoneHistoryPage()


__all__ = ["PhoneHistoryPage", "load_phone_history_page"]
