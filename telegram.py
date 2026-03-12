"""Telegram integration helpers, mirroring whatsapp.py."""

import orjson
from database import get_db_connection


async def is_telegram_conversation(conversation_id: int) -> bool:
    """Check if a conversation is assigned to Telegram."""
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            "SELECT external_platforms FROM USER_DETAILS "
            "WHERE user_id IN (SELECT user_id FROM conversations WHERE id = ?)",
            (conversation_id,),
        )
        result = await cursor.fetchone()
        if result:
            external_platforms = orjson.loads(result[0]) if result[0] else {}
            telegram_data = external_platforms.get("telegram", {})
            telegram_conv_id = telegram_data.get("conversation_id")
            if telegram_conv_id is None:
                return False
            try:
                return int(telegram_conv_id) == int(conversation_id)
            except (TypeError, ValueError):
                return str(telegram_conv_id) == str(conversation_id)
    return False
