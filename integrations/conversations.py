import asyncio
import calendar
import re
import sqlite3
import unicodedata
from datetime import datetime
from typing import Any, Callable

import orjson

from database import (
    DB_MAX_RETRIES,
    DB_RETRY_DELAY_BASE,
    get_db_connection,
    is_lock_error,
)
from chat.services.conversations import create_conversation_core
from log_config import logger


def sanitize_chat_title(name: str | None, max_len: int = 25) -> str:
    """Strip control/format chars, collapse whitespace, and truncate."""
    cleaned = "".join(
        ch for ch in (name or "Untitled")
        if unicodedata.category(ch) not in ("Cc", "Cf")
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_len] if cleaned else "Untitled"


def escape_markdown(text: str) -> str:
    """Escape Markdown special characters for WhatsApp."""
    for ch in ("*", "_", "`", "[", "]"):
        text = text.replace(ch, f"\\{ch}")
    return text


async def can_use_platform(user_id: int, platform: str, cursor) -> tuple[bool, str, str]:
    """Check if a user can use a platform."""
    if platform == "telegram":
        await cursor.execute("SELECT telegram_chat_id FROM USERS WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
        if not row or not row[0]:
            return (
                False,
                "platform_not_linked",
                "Telegram is not linked to your account. Link it first from Telegram.",
            )
        config_key = "telegram_require_phone_verification"
    elif platform == "whatsapp":
        await cursor.execute("SELECT phone_number FROM USERS WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
        if not row or not row[0]:
            return (
                False,
                "no_phone_number",
                "No phone number on your account. Set it in your account settings first.",
            )
        config_key = "whatsapp_require_phone_verification"
    else:
        return (
            False,
            "invalid_platform",
            "Invalid platform. Use: whatsapp (wa) or telegram (tg).",
        )

    await cursor.execute("SELECT value FROM SYSTEM_CONFIG WHERE key = ?", (config_key,))
    cfg = await cursor.fetchone()
    if cfg and cfg[0] == "1":
        await cursor.execute("SELECT phone_verified FROM USERS WHERE id = ?", (user_id,))
        verified_row = await cursor.fetchone()
        if not verified_row or not verified_row[0]:
            return (
                False,
                "phone_verification_required",
                "Your phone number must be verified first. Check your account settings.",
            )

    return True, "", ""


async def _rollback_quietly(conn) -> None:
    try:
        await conn.rollback()
    except Exception:
        pass


async def _run_begin_immediate(
    operation_name: str,
    work_fn: Callable[[Any, Any], Any],
):
    last_lock_error = None

    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        wait_time = 0.0

        async with get_db_connection() as conn:
            transaction_started = False
            try:
                await conn.execute("BEGIN IMMEDIATE")
                transaction_started = True
                cursor = await conn.cursor()
                return await work_fn(conn, cursor)
            except sqlite3.OperationalError as exc:
                if transaction_started:
                    await _rollback_quietly(conn)
                if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                    wait_time = DB_RETRY_DELAY_BASE * (attempt + 1)
                    logger.warning(
                        "Lock detected in %s (retry %s/%s, wait %.2fs)",
                        operation_name,
                        attempt + 1,
                        DB_MAX_RETRIES,
                        wait_time,
                    )
                    last_lock_error = exc
                    retry_needed = True
                else:
                    raise
            except Exception:
                if transaction_started:
                    await _rollback_quietly(conn)
                raise

        if retry_needed:
            await asyncio.sleep(wait_time)
            continue
        break

    if last_lock_error:
        logger.error(
            "Failed %s after %s retries: %s",
            operation_name,
            DB_MAX_RETRIES,
            last_lock_error,
        )
        raise last_lock_error

    raise RuntimeError(f"{operation_name} failed without returning a result")


async def mutate_external_platforms(user_id: int, mutate_fn: Callable[[dict], Any]):
    """Atomically read-modify-write the external_platforms JSON."""

    async def _work(conn, cursor):
        await cursor.execute(
            "SELECT external_platforms FROM USER_DETAILS WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            await conn.rollback()
            raise ValueError("User details not found")

        platforms = orjson.loads(row[0]) if row[0] else {}
        result = mutate_fn(platforms)

        update_cursor = await cursor.execute(
            "UPDATE USER_DETAILS SET external_platforms = ? WHERE user_id = ?",
            (orjson.dumps(platforms).decode("utf-8"), user_id),
        )
        if update_cursor.rowcount == 0:
            await conn.rollback()
            raise ValueError("User details not found - UPDATE affected 0 rows")

        await conn.commit()
        return result

    return await _run_begin_immediate("mutate_external_platforms", _work)


async def ensure_platform_conversation(
    user_id: int,
    platform: str,
    current_user,
) -> tuple[dict, bool]:
    """Ensure a valid conversation exists for the requested platform."""
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            "SELECT external_platforms FROM USER_DETAILS WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        platforms = orjson.loads(row[0]) if row and row[0] else {}
        platform_data = platforms.get(platform) or {}
        if not isinstance(platform_data, dict):
            platform_data = {}

        conversation_id = platform_data.get("conversation_id")
        if conversation_id:
            await cursor.execute(
                "SELECT id FROM CONVERSATIONS WHERE id = ? AND user_id = ?",
                (conversation_id, user_id),
            )
            if await cursor.fetchone():
                return dict(platform_data), False

    async def _work(conn, cursor):
        await cursor.execute(
            "SELECT external_platforms FROM USER_DETAILS WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            await conn.rollback()
            raise ValueError("User details not found")

        platforms = orjson.loads(row[0]) if row[0] else {}
        platform_data = platforms.get(platform) or {}
        if not isinstance(platform_data, dict):
            platform_data = {}

        conversation_id = platform_data.get("conversation_id")
        if conversation_id:
            await cursor.execute(
                "SELECT id FROM CONVERSATIONS WHERE id = ? AND user_id = ?",
                (conversation_id, user_id),
            )
            if await cursor.fetchone():
                await conn.commit()
                return dict(platform_data), False

            if platform not in platforms or not isinstance(platforms.get(platform), dict):
                platforms[platform] = {}
            platforms[platform].pop("conversation_id", None)

        new_conv_id = await create_conversation_core(
            user_id,
            cursor,
            current_user,
            prompt_id=None,
        )

        if platform not in platforms or not isinstance(platforms.get(platform), dict):
            platforms[platform] = {}
        platforms[platform]["conversation_id"] = new_conv_id
        platforms[platform].setdefault("answer", "text")

        update_cursor = await cursor.execute(
            "UPDATE USER_DETAILS SET external_platforms = ? WHERE user_id = ?",
            (orjson.dumps(platforms).decode("utf-8"), user_id),
        )
        if update_cursor.rowcount == 0:
            await conn.rollback()
            raise ValueError("User details not found")

        await conn.commit()
        return dict(platforms[platform]), True

    return await _run_begin_immediate("ensure_platform_conversation", _work)


async def create_new_platform_conversation(
    user_id: int,
    platform: str,
    current_user,
) -> dict:
    """Create a new conversation and overwrite the platform binding atomically."""

    async def _work(conn, cursor):
        new_conv_id = await create_conversation_core(user_id, cursor, current_user)

        await cursor.execute(
            "SELECT external_platforms FROM USER_DETAILS WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            await conn.rollback()
            raise ValueError("User details not found")

        platforms = orjson.loads(row[0]) if row[0] else {}
        if platform not in platforms or not isinstance(platforms.get(platform), dict):
            platforms[platform] = {}
        platforms[platform]["conversation_id"] = new_conv_id
        platforms[platform].setdefault("answer", "text")

        update_cursor = await cursor.execute(
            "UPDATE USER_DETAILS SET external_platforms = ? WHERE user_id = ?",
            (orjson.dumps(platforms).decode("utf-8"), user_id),
        )
        if update_cursor.rowcount == 0:
            await conn.rollback()
            raise ValueError("User details not found - UPDATE affected 0 rows")

        await conn.commit()
        return dict(platforms[platform])

    return await _run_begin_immediate("create_new_platform_conversation", _work)


async def set_external_conversation(
    user_id: int,
    conv_id: int,
    target_platform: str,
    current_platform: str,
) -> dict:
    """Validate and assign a conversation to a platform in one transaction."""

    async def _work(conn, cursor):
        await cursor.execute(
            "SELECT user_id, chat_name, locked FROM CONVERSATIONS WHERE id = ?",
            (conv_id,),
        )
        conv_row = await cursor.fetchone()
        if not conv_row or conv_row[0] != user_id:
            await conn.rollback()
            return {
                "success": False,
                "error": "conversation_not_found",
                "message": "Conversation not found.",
            }

        conv_name = sanitize_chat_title(conv_row[1])
        if conv_row[2]:
            await conn.rollback()
            return {
                "success": False,
                "error": "conversation_locked",
                "message": f"Conversation #{conv_id} is locked. Use !new to start a fresh one.",
            }

        try:
            await cursor.execute(
                """
                SELECT 1
                FROM EXTERNAL_DEVICE_BINDINGS
                WHERE user_id = ?
                  AND conversation_id = ?
                LIMIT 1
                """,
                (user_id, conv_id),
            )
            if await cursor.fetchone():
                await conn.rollback()
                return {
                    "success": False,
                    "error": "external_devices_attached",
                    "message": "Remove external device access before assigning this conversation to WhatsApp or Telegram.",
                }
        except sqlite3.OperationalError as exc:
            if "EXTERNAL_DEVICE_BINDINGS" not in str(exc).upper():
                await conn.rollback()
                raise

        ok, err_code, err_msg = await can_use_platform(user_id, target_platform, cursor)
        if not ok:
            await conn.rollback()
            return {"success": False, "error": err_code, "message": err_msg}

        await cursor.execute(
            "SELECT external_platforms FROM USER_DETAILS WHERE user_id = ?",
            (user_id,),
        )
        ep_row = await cursor.fetchone()
        if not ep_row:
            await conn.rollback()
            raise ValueError("User details not found")

        platforms = orjson.loads(ep_row[0]) if ep_row[0] else {}

        target_data = platforms.get(target_platform)
        previous_conversation_id = (
            target_data.get("conversation_id")
            if isinstance(target_data, dict)
            else None
        )
        try:
            normalized_previous_conversation_id = int(previous_conversation_id)
        except (TypeError, ValueError):
            normalized_previous_conversation_id = None
        if target_platform not in platforms or not isinstance(platforms.get(target_platform), dict):
            platforms[target_platform] = {}
        platforms[target_platform]["conversation_id"] = conv_id

        update_cursor = await cursor.execute(
            "UPDATE USER_DETAILS SET external_platforms = ? WHERE user_id = ?",
            (orjson.dumps(platforms).decode("utf-8"), user_id),
        )
        if update_cursor.rowcount == 0:
            await conn.rollback()
            raise ValueError("User details not found - UPDATE affected 0 rows")

        await conn.commit()

        platform_label = "WhatsApp" if target_platform == "whatsapp" else "Telegram"
        affected_conversation_ids = [conv_id]
        if (
            normalized_previous_conversation_id is not None
            and normalized_previous_conversation_id > 0
            and normalized_previous_conversation_id != conv_id
        ):
            affected_conversation_ids.append(normalized_previous_conversation_id)

        if (
            normalized_previous_conversation_id
            and normalized_previous_conversation_id != conv_id
        ):
            message = (
                f'Moved {platform_label} to conversation #{conv_id} "{conv_name}".'
            )
        elif target_platform != current_platform:
            message = f'Assigned conversation #{conv_id} "{conv_name}" to {platform_label}.'
        else:
            message = f'Switched to conversation #{conv_id} "{conv_name}" on {platform_label}.'

        return {
            "success": True,
            "error": None,
            "message": message,
            "affected_conversation_ids": affected_conversation_ids,
        }

    return await _run_begin_immediate("set_external_conversation", _work)


async def get_chats_list(
    user_id: int,
    current_platform: str,
    conn,
    *,
    markdown: bool = True,
) -> str:
    """Return a formatted recent-conversation list for external platforms."""
    cursor = await conn.cursor()

    await cursor.execute(
        "SELECT external_platforms FROM USER_DETAILS WHERE user_id = ?",
        (user_id,),
    )
    ep_row = await cursor.fetchone()
    platforms = orjson.loads(ep_row[0]) if ep_row and ep_row[0] else {}
    wa_conv_id = (platforms.get("whatsapp") or {}).get("conversation_id")
    tg_conv_id = (platforms.get("telegram") or {}).get("conversation_id")

    await cursor.execute(
        """
        SELECT c.id, c.chat_name, c.last_activity, c.locked,
               (SELECT COUNT(*) FROM MESSAGES m WHERE m.conversation_id = c.id) AS msg_count
        FROM CONVERSATIONS c
        WHERE c.user_id = ?
        ORDER BY c.last_activity DESC, c.id DESC
        LIMIT 15
        """,
        (user_id,),
    )
    rows = list(await cursor.fetchall())

    if not rows:
        return "No conversations yet. Send a message to start one!"

    current_conv_id = wa_conv_id if current_platform == "whatsapp" else tg_conv_id
    result_ids = {row[0] for row in rows}
    if current_conv_id and current_conv_id not in result_ids:
        await cursor.execute(
            """
            SELECT c.id, c.chat_name, c.last_activity, c.locked,
                   (SELECT COUNT(*) FROM MESSAGES m WHERE m.conversation_id = c.id) AS msg_count
            FROM CONVERSATIONS c
            WHERE c.id = ? AND c.user_id = ?
            """,
            (current_conv_id, user_id),
        )
        active_row = await cursor.fetchone()
        if active_row:
            rows.append(active_row)

    lines = []
    for row in rows:
        conv_id, name, last_activity, locked, msg_count = row
        raw_name = sanitize_chat_title(name)
        display_name = escape_markdown(raw_name) if markdown else raw_name

        date_str = ""
        try:
            if last_activity and str(last_activity).strip():
                dt = datetime.fromisoformat(str(last_activity))
                date_str = f"{calendar.month_abbr[dt.month]} {dt.day:02d}"
        except (TypeError, ValueError):
            date_str = ""

        badges = []
        if conv_id == wa_conv_id:
            badges.append("WA")
        if conv_id == tg_conv_id:
            badges.append("TG")
        if locked:
            badges.append("LOCKED")
        badge_str = " ".join(f"[{badge}]" for badge in badges)

        pointer = "-> " if conv_id == current_conv_id else ""
        count_part = f"{int(msg_count or 0)} msgs"
        if date_str:
            count_part += f", {date_str}"

        line = f"{pointer}#{conv_id} - {display_name} ({count_part})"
        if badge_str:
            line += f" {badge_str}"
        lines.append(line)

    header = "*Your conversations:*" if markdown else "Your conversations:"
    footer = (
        "Use *!set <id>* to switch conversation."
        if markdown
        else "Use !set <id> to switch conversation."
    )
    return header + "\n\n" + "\n".join(lines) + "\n\n" + footer


async def is_platform_conversation(conversation_id: int, platform: str) -> bool:
    """Check if a conversation is assigned to an external platform."""
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            "SELECT external_platforms FROM USER_DETAILS "
            "WHERE user_id IN (SELECT user_id FROM conversations WHERE id = ?)",
            (conversation_id,),
        )
        result = await cursor.fetchone()
        if not result:
            return False

        external_platforms = orjson.loads(result[0]) if result[0] else {}
        platform_data = external_platforms.get(platform, {})
        platform_conversation_id = platform_data.get("conversation_id")
        if platform_conversation_id is None:
            return False
        try:
            return int(platform_conversation_id) == int(conversation_id)
        except (TypeError, ValueError):
            return str(platform_conversation_id) == str(conversation_id)


async def is_whatsapp_conversation(conversation_id: int) -> bool:
    """Check if a conversation is assigned to WhatsApp."""
    return await is_platform_conversation(conversation_id, "whatsapp")


async def is_telegram_conversation(conversation_id: int) -> bool:
    """Check if a conversation is assigned to Telegram."""
    return await is_platform_conversation(conversation_id, "telegram")


async def change_response_mode(
    user_id: int,
    new_mode: str,
    platform: str = "whatsapp",
    conn=None,
) -> str:
    """
    Change the default response mode for a user's external platform.

    This preserves the command-handler semantics used by WhatsApp, Telegram,
    and AI-side confirmation flows: update the platform JSON for the user and
    return the human-readable confirmation string.
    """
    if new_mode not in ("voice", "text"):
        return "Error changing mode: invalid mode"
    if platform not in ("whatsapp", "telegram"):
        return "Error changing mode: invalid platform"

    async def _write(db_conn):
        await db_conn.execute(
            """UPDATE USER_DETAILS SET external_platforms =
               json_set(COALESCE(NULLIF(external_platforms, ''), '{}'), ?, ?)
               WHERE user_id = ?""",
            (f"$.{platform}.answer", new_mode, user_id),
        )
        await db_conn.commit()

    try:
        if conn is not None:
            await _write(conn)
        else:
            async with get_db_connection() as owned_conn:
                await _write(owned_conn)
        return f"Changed to {'voice' if new_mode == 'voice' else 'text'} mode"
    except Exception as e:
        logger.error(f"Error in change_response_mode: {e}")
        return f"Error changing mode: {str(e)}"


async def change_conversation_response_mode(
    user_id: int,
    platform: str,
    conversation_id: int,
    mode: str,
):
    """Change response mode for an assigned external-platform conversation."""
    if platform not in ("whatsapp", "telegram"):
        return {
            "success": False,
            "error": "invalid_platform",
            "message": "Invalid platform. Use whatsapp or telegram.",
        }
    if mode not in ("voice", "text"):
        return {
            "success": False,
            "error": "invalid_mode",
            "message": "Invalid mode. Use voice or text.",
        }

    async def _work(conn, cursor):
        await cursor.execute(
            "SELECT user_id FROM CONVERSATIONS WHERE id = ?",
            (conversation_id,),
        )
        row = await cursor.fetchone()
        if not row or row[0] != user_id:
            await conn.rollback()
            return {
                "success": False,
                "error": "conversation_not_found",
                "message": "Conversation not found.",
            }

        await cursor.execute(
            "SELECT external_platforms FROM USER_DETAILS WHERE user_id = ?",
            (user_id,),
        )
        details = await cursor.fetchone()
        platforms = orjson.loads(details[0]) if details and details[0] else {}
        platform_data = platforms.get(platform, {})
        if not isinstance(platform_data, dict) or platform_data.get("conversation_id") != conversation_id:
            await conn.rollback()
            return {
                "success": False,
                "error": "conversation_not_assigned",
                "message": f"Conversation is not assigned to {platform}.",
            }

        platforms[platform]["answer"] = mode
        await cursor.execute(
            "UPDATE USER_DETAILS SET external_platforms = ? WHERE user_id = ?",
            (orjson.dumps(platforms).decode("utf-8"), user_id),
        )
        await conn.commit()
        return {
            "success": True,
            "error": None,
            "message": f"Response mode changed to {mode}",
            "mode": mode,
        }

    return await _run_begin_immediate("change_response_mode", _work)
