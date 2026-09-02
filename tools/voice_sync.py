# voice_sync.py
# Premade voice sync and default voice resolution for the auto-repair system.

import time
import aiohttp

from database import get_db_connection
from tools.tts_load_balancer import get_elevenlabs_key
from ai_runtime.voice_resolution import CanonicalVoice, resolve_default_voice
from log_config import logger

ELEVENLABS_TTS_SERVICE_ID = 1
SYNC_INTERVAL_SECONDS = 7 * 24 * 3600  # 7 days

_last_sync_timestamp: float = 0.0


async def sync_premade_voices() -> int:
    """Fetch premade voices from ElevenLabs API and sync them into the VOICES table.

    - Inserts new premade voices that don't exist in DB.
    - Updates names if they changed.
    - Paginates using next_page_token.

    Default selection is intentionally not changed here.  It is an explicit
    administrator decision shared by every voice channel.

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

        await conn.commit()

    _last_sync_timestamp = time.monotonic()
    logger.info(f"voice_sync: Sync complete, {changes} changes")
    return changes


async def get_default_voice_code() -> str:
    """Return the single canonical default voice code, or fail visibly."""
    return (await resolve_default_voice()).voice_code


async def mark_voice_deprecated(
    voice: CanonicalVoice | str,
    *,
    provider: str | None = None,
) -> None:
    """Deprecate one provider-qualified voice without crossing catalogues."""
    voice_code = voice.voice_code if isinstance(voice, CanonicalVoice) else str(voice)
    provider_key = voice.provider if isinstance(voice, CanonicalVoice) else provider
    voice_id = voice.id if isinstance(voice, CanonicalVoice) else None
    if provider_key != "elevenlabs":
        logger.warning(
            "voice_sync: Refusing to deprecate non-ElevenLabs voice_code=%s provider=%s",
            voice_code,
            provider_key,
        )
        return

    async with get_db_connection(readonly=False) as conn:
        if voice_id is not None:
            query = """
                SELECT v.id
                FROM VOICES v
                JOIN SERVICES s ON s.id = v.tts_service
                WHERE v.id = ? AND v.voice_code = ?
                  AND LOWER(s.name) LIKE '%elevenlabs%'
            """
            params = (voice_id, voice_code)
        else:
            query = """
                SELECT v.id
                FROM VOICES v
                JOIN SERVICES s ON s.id = v.tts_service
                WHERE v.voice_code = ?
                  AND LOWER(s.name) LIKE '%elevenlabs%'
            """
            params = (voice_code,)

        async with conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            logger.warning(
                "voice_sync: Cannot deprecate unknown ElevenLabs voice_code=%s",
                voice_code,
            )
            return

        await conn.execute(
            f"UPDATE VOICES SET deprecated = 1 WHERE id IN ({','.join('?' for _ in rows)})",
            tuple(row["id"] for row in rows),
        )

        await conn.commit()

    logger.warning(
        f"voice_sync: Deprecated ElevenLabs voice_code={voice_code}; "
        "prompts remain attached so configuration fails visibly instead of changing voice"
    )


def should_trigger_background_sync() -> bool:
    """Check if enough time has passed since the last sync."""
    if _last_sync_timestamp == 0.0:
        return True
    return (time.monotonic() - _last_sync_timestamp) >= SYNC_INTERVAL_SECONDS
