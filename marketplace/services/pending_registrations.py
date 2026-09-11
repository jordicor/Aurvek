from datetime import datetime
from typing import Optional

from database import get_db_connection
from i18n import normalize_language
from log_config import logger


async def create_pending_registration(
    email: str,
    username: str,
    password_hash: bytes,
    token: str,
    target_role: str,
    prompt_id: Optional[int],
    expires_at: datetime,
    pack_id: Optional[int] = None,
    ui_language: Optional[str] = None,
) -> bool:
    """Capture the native interface language once, including cross-browser verification."""
    language = "en" if ui_language is None else normalize_language(ui_language)
    if language is None:
        raise ValueError("Unsupported interface language")
    try:
        async with get_db_connection() as conn:
            await conn.execute(
                "DELETE FROM PENDING_REGISTRATIONS WHERE email = ?",
                (email,),
            )

            await conn.execute(
                """
                INSERT INTO PENDING_REGISTRATIONS
                (email, username, password_hash, token, target_role, prompt_id, pack_id, expires_at, ui_language)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (email, username, password_hash, token, target_role, prompt_id, pack_id, expires_at, language),
            )
            await conn.commit()
            return True
    except Exception as e:
        logger.error(f"Error creating pending registration: {e}")
        return False


async def get_pending_registration(token: str) -> Optional[dict]:
    """Get pending registration by token."""
    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT id, email, username, password_hash, target_role, prompt_id, expires_at, pack_id, ui_language
                FROM PENDING_REGISTRATIONS
                WHERE token = ?
                """,
                (token,),
            )
            result = await cursor.fetchone()

        if not result:
            return None

        return {
            "id": result[0],
            "email": result[1],
            "username": result[2],
            "password_hash": result[3],
            "target_role": result[4],
            "prompt_id": result[5],
            "expires_at": datetime.fromisoformat(result[6]) if isinstance(result[6], str) else result[6],
            "pack_id": result[7],
            "ui_language": normalize_language(result[8]) or "en",
        }
    except Exception as e:
        logger.error(f"Error getting pending registration: {e}")
        return None


async def delete_pending_registration(token: str) -> bool:
    """Delete a pending registration by token."""
    try:
        async with get_db_connection() as conn:
            await conn.execute(
                "DELETE FROM PENDING_REGISTRATIONS WHERE token = ?",
                (token,),
            )
            await conn.commit()
            return True
    except Exception as e:
        logger.error(f"Error deleting pending registration: {e}")
        return False


async def cleanup_expired_registrations() -> int:
    """Delete expired pending registrations. Returns count of deleted rows."""
    try:
        async with get_db_connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM PENDING_REGISTRATIONS WHERE expires_at < ?",
                (datetime.now(),),
            )
            await conn.commit()
            return cursor.rowcount
    except Exception as e:
        logger.error(f"Error cleaning up expired registrations: {e}")
        return 0


async def get_user_by_email_record(email: str) -> Optional[dict]:
    """Check if a user with this email already exists."""
    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT id, username, ui_language FROM USERS WHERE email = ?",
                (email,),
            )
            result = await cursor.fetchone()
            if result:
                return {"id": result[0], "username": result[1], "ui_language": result[2]}
            return None
    except Exception as e:
        logger.error(f"Error checking user by email: {e}")
        return None
