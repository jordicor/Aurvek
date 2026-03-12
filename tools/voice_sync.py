# voice_sync.py
# Premade voice sync and default voice resolution for the auto-repair system.

import time
import aiohttp

from database import get_db_connection
from tools.tts_load_balancer import get_elevenlabs_key
from log_config import logger

HARDCODED_FALLBACK_VOICE = "nMPrFLO7QElx9wTR0JGo"
ELEVENLABS_TTS_SERVICE_ID = 1
SYNC_INTERVAL_SECONDS = 7 * 24 * 3600  # 7 days

_last_sync_timestamp: float = 0.0


async def sync_premade_voices() -> int:
    """Fetch premade voices from ElevenLabs API and sync them into the VOICES table.

    - Inserts new premade voices that don't exist in DB.
    - Updates names if they changed.
    - If no voice has is_default=1, sets the first premade voice as default.
    - Paginates using next_page_token.

    Returns the number of DB changes made.
    """
    global _last_sync_timestamp

    api_key = get_elevenlabs_key()
    if not api_key:
        logger.error("voice_sync: No ElevenLabs API key available, cannot sync")
        return 0

    headers = {"xi-api-key": api_key}
    all_premade: list[dict] = []
    next_cursor: str | None = None

    async with aiohttp.ClientSession() as session:
        while True:
            url = "https://api.elevenlabs.io/v2/voices?category=premade&page_size=100"
            if next_cursor:
                url += f"&next_page_token={next_cursor}"

            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(f"voice_sync: ElevenLabs API returned {resp.status}: {body}")
                        break
                    data = await resp.json()
            except Exception as e:
                logger.error(f"voice_sync: Failed to fetch premade voices: {e}")
                break

            voices = data.get("voices", [])
            for v in voices:
                voice_id = v.get("voice_id")
                name = v.get("name", "Unknown")
                if voice_id:
                    all_premade.append({"voice_code": voice_id, "name": name})

            next_cursor = data.get("next_page_token")
            if not next_cursor:
                break

    if not all_premade:
        logger.warning("voice_sync: No premade voices returned from API")
        return 0

    logger.info(f"voice_sync: Fetched {len(all_premade)} premade voices from ElevenLabs")

    changes = 0
    async with get_db_connection(readonly=False) as conn:
        # Build a lookup of existing ElevenLabs voices
        async with conn.execute(
            "SELECT id, name, voice_code FROM VOICES WHERE tts_service = ?",
            (ELEVENLABS_TTS_SERVICE_ID,),
        ) as cursor:
            rows = await cursor.fetchall()

        existing: dict[str, dict] = {}
        for row in rows:
            existing[row["voice_code"]] = {"id": row["id"], "name": row["name"]}

        for pv in all_premade:
            vc = pv["voice_code"]
            name = pv["name"]

            if vc in existing:
                # Update name if changed
                if existing[vc]["name"] != name:
                    await conn.execute(
                        "UPDATE VOICES SET name = ? WHERE id = ?",
                        (name, existing[vc]["id"]),
                    )
                    logger.info(f"voice_sync: Updated voice name '{existing[vc]['name']}' -> '{name}' (code={vc})")
                    changes += 1

                # Un-deprecate if it was deprecated
                await conn.execute(
                    "UPDATE VOICES SET deprecated = 0 WHERE id = ? AND deprecated = 1",
                    (existing[vc]["id"],),
                )
            else:
                # Insert new premade voice
                await conn.execute(
                    "INSERT INTO VOICES (name, voice_code, tts_service, is_default, deprecated) VALUES (?, ?, ?, 0, 0)",
                    (name, vc, ELEVENLABS_TTS_SERVICE_ID),
                )
                logger.info(f"voice_sync: Inserted new premade voice '{name}' (code={vc})")
                changes += 1

        # Ensure at least one default voice exists
        async with conn.execute(
            "SELECT id FROM VOICES WHERE is_default = 1 AND deprecated = 0 LIMIT 1"
        ) as cursor:
            default_row = await cursor.fetchone()

        if not default_row:
            # Set first premade voice as default
            first_code = all_premade[0]["voice_code"]
            await conn.execute(
                "UPDATE VOICES SET is_default = 1 WHERE voice_code = ?",
                (first_code,),
            )
            logger.info(f"voice_sync: Set default voice to '{all_premade[0]['name']}' (code={first_code})")
            changes += 1

        await conn.commit()

    _last_sync_timestamp = time.monotonic()
    logger.info(f"voice_sync: Sync complete, {changes} changes")
    return changes


async def get_default_voice_code() -> str:
    """Return the default voice code, triggering a sync if needed.

    Resolution order:
    1. DB query for is_default=1 AND deprecated=0
    2. If not found, run sync_premade_voices() then retry
    3. Hardcoded fallback as absolute last resort
    """
    async with get_db_connection(readonly=True) as conn:
        async with conn.execute(
            "SELECT voice_code FROM VOICES WHERE is_default = 1 AND deprecated = 0 LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            return row["voice_code"]

    # No default found -- try syncing
    logger.warning("voice_sync: No default voice in DB, triggering premade sync")
    await sync_premade_voices()

    async with get_db_connection(readonly=True) as conn:
        async with conn.execute(
            "SELECT voice_code FROM VOICES WHERE is_default = 1 AND deprecated = 0 LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            return row["voice_code"]

    # Still nothing -- hardcoded fallback
    logger.error("voice_sync: No default voice even after sync, using hardcoded fallback")
    return HARDCODED_FALLBACK_VOICE


async def mark_voice_deprecated(voice_code: str) -> None:
    """Mark a voice as deprecated and detach it from all prompts that use it."""
    async with get_db_connection(readonly=False) as conn:
        # Get the voice id
        async with conn.execute(
            "SELECT id FROM VOICES WHERE voice_code = ?", (voice_code,)
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            logger.warning(f"voice_sync: Cannot deprecate unknown voice_code={voice_code}")
            return

        voice_id = row["id"]

        await conn.execute(
            "UPDATE VOICES SET deprecated = 1 WHERE id = ?", (voice_id,)
        )

        # Detach from prompts so they fall back to default on next TTS call
        await conn.execute(
            "UPDATE PROMPTS SET voice_id = NULL WHERE voice_id = ?", (voice_id,)
        )

        await conn.commit()

    logger.warning(
        f"voice_sync: Deprecated voice_code={voice_code} (id={voice_id}) and detached from prompts"
    )


def should_trigger_background_sync() -> bool:
    """Check if enough time has passed since the last sync."""
    if _last_sync_timestamp == 0.0:
        return True
    return (time.monotonic() - _last_sync_timestamp) >= SYNC_INTERVAL_SECONDS
