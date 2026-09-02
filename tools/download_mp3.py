# tools/download_mp3.py

import os
import sys
import logging
from datetime import datetime, timezone
from pydub import AudioSegment
from io import BytesIO
from dotenv import load_dotenv

# Import necessary functions from tts.py
from tools.tts import (
    get_tts_generator_for_voice,
    insert_tts_break,
    process_text_for_tts,
)
from tools.tts_config import get_tts_profile, format_to_pydub
from ai_runtime.voice_resolution import (
    CanonicalVoice,
    CanonicalVoiceResolutionError,
    resolve_catalog_voice,
    resolve_default_voice,
    resolve_prompt_voice,
)
from common import Cost, generate_user_hash, has_sufficient_balance, cost_tts, refund_tts
from database import get_db_connection
from storage_quota import record_generated_file

# Logging Configuration
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# Load Environment Variables
load_dotenv()

DB_NAME = os.getenv("DATABASE")
if not DB_NAME:
    logger.error("DATABASE is not defined in .env file")
    sys.exit(1)

# Global Variables
BASE_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'users')


async def _resolve_export_voices(conversation, conn) -> tuple[CanonicalVoice, CanonicalVoice]:
    """Resolve both speakers without falling back to the global TTS engine."""
    bot_voice = await resolve_prompt_voice(conversation["role_id"], conn=conn)
    user_voice_code = str(conversation["user_voice_code"] or "").strip()
    if user_voice_code:
        user_voice = await resolve_catalog_voice(user_voice_code, conn=conn)
        if user_voice is None:
            raise CanonicalVoiceResolutionError(
                "user_voice_not_catalogued",
                "The account voice selected for MP3 export is not in the voice catalogue.",
            )
    else:
        user_voice = await resolve_default_voice(conn=conn)
    return bot_voice, user_voice


async def _refund_mp3_charges(user_id: int, charges: dict[str, int]) -> bool:
    all_refunded = True
    for provider, characters in charges.items():
        if not await refund_tts(user_id, characters, provider=provider):
            all_refunded = False
            logger.critical(
                "REFUND FAILED user_id=%s provider=%s chars=%d -- manual review needed",
                user_id,
                provider,
                characters,
            )
    return all_refunded


async def _charge_mp3_providers(
    user_id: int,
    characters_by_provider: dict[str, int],
) -> dict[str, int] | None:
    """Reserve all provider charges, compensating earlier reservations on failure."""
    total_cost = 0.0
    for provider, characters in characters_by_provider.items():
        rate, service_id = Cost.get_tts_service(provider)
        if service_id is None:
            logger.error("MP3 TTS billing is not configured for provider=%s", provider)
            return None
        total_cost += rate * characters

    if not await has_sufficient_balance(user_id, total_cost):
        return None

    charged: dict[str, int] = {}
    for provider, characters in characters_by_provider.items():
        if not await cost_tts(user_id, characters, provider=provider):
            await _refund_mp3_charges(user_id, charged)
            return None
        charged[provider] = characters
    return charged

async def generate_and_save_mp3(conversation_id: int, user_id: int, is_admin: bool):
    logger.debug(f"Starting MP3 generation for conversation_id: {conversation_id}")
    async with get_db_connection(readonly=True) as conn:
        # Verify permissions and conversation existence
        query_convo = """
            SELECT c.id, c.role_id, u.username, llm.machine, llm.model,
                   p.name AS prompt_name, uv.voice_code AS user_voice_code
            FROM conversations c
            JOIN users u ON c.user_id = u.id
            LEFT JOIN USER_DETAILS ud ON ud.user_id = c.user_id
            LEFT JOIN VOICES uv ON uv.id = ud.voice_id
            LEFT JOIN llm ON c.llm_id = llm.id
            LEFT JOIN prompts p ON c.role_id = p.id
            WHERE c.id = ? AND (c.user_id = ? OR ?)
        """
        async with conn.execute(query_convo, (conversation_id, user_id, is_admin)) as cursor:
            conversation = await cursor.fetchone()
            if not conversation:
                logger.warning(f"Unauthorized access or conversation not found for conversation_id: {conversation_id}")
                return

        bot_voice, user_voice = await _resolve_export_voices(conversation, conn)

        # Get messages
        query_messages = """
            SELECT id, date, message, type FROM messages
            WHERE conversation_id = ?
            ORDER BY id ASC, date ASC
        """
        async with conn.execute(query_messages, (conversation_id,)) as cursor:
            messages = await cursor.fetchall()

    # Generate MP3
    # Load TTS profile for MP3 export context
    profile = await get_tts_profile("mp3")

    # Prepare per-message TTS chunks once so the charge can be sized before any
    # audio is generated. MP3 export regenerates TTS for every message, which has
    # a real per-character cost on the platform key, so the user must be billed
    # exactly like the WebSocket TTS path (fail-fast pre-check, then charge-first
    # / refund-on-failure). The subscription (text-only) option never frees this.
    prepared_messages = []  # list of (CanonicalVoice, chunks)
    characters_by_provider: dict[str, int] = {}
    for message in messages:
        text = process_text_for_tts(message['message'])
        chunks = await insert_tts_break(text)
        voice = bot_voice if message['type'] == 'bot' else user_voice
        prepared_messages.append((voice, chunks))
        if text:
            characters_by_provider[voice.provider] = (
                characters_by_provider.get(voice.provider, 0) + len(text)
            )

    if not any(characters_by_provider.values()):
        logger.warning("No text to synthesize for MP3 export of conversation_id: %s", conversation_id)
        return

    charged = await _charge_mp3_providers(user_id, characters_by_provider)
    if charged is None:
        logger.warning(
            "TTS reservation failed for MP3 export: user_id=%s conversation_id=%s",
            user_id, conversation_id,
        )
        return

    async def _refund_mp3_charge():
        await _refund_mp3_charges(user_id, charged)

    try:
        audio_segments = []
        for voice, chunks in prepared_messages:
            audio_generator = get_tts_generator_for_voice(voice, chunks, profile=profile)
            audio_input_format = (
                format_to_pydub(profile.output_format)
                if voice.provider == 'elevenlabs'
                else 'mp3'
            )
            async for audio_chunk in audio_generator:
                audio_segment = AudioSegment.from_file(BytesIO(audio_chunk), format=audio_input_format)
                audio_segments.append(audio_segment)

        if not audio_segments:
            logger.error("No audio segments generated for MP3 export of conversation_id: %s", conversation_id)
            await _refund_mp3_charge()
            return

        combined_audio = audio_segments[0]
        for segment in audio_segments[1:]:
            combined_audio += segment

        # Generate hash and file path
        username = conversation["username"]
        hash_prefixes = generate_user_hash(username)
        user_hash = hash_prefixes[2]
        prefix1 = f"{conversation_id:07d}"[:3]
        prefix2 = f"{conversation_id:07d}"[3:]
        mp3_convo_folder = os.path.join(BASE_DIR, hash_prefixes[0], hash_prefixes[1], user_hash, "files", prefix1, prefix2, "mp3")
        os.makedirs(mp3_convo_folder, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y_%m_%d_%H_%M_%S")
        prompt_name_safe = ''.join(c for c in conversation["prompt_name"] if c.isalnum() or c in (' ', '_')).rstrip().replace(' ', '_')
        mp3_filename = f"{prompt_name_safe}_{timestamp}.mp3"
        mp3_file_path = os.path.join(mp3_convo_folder, mp3_filename)

        try:
            combined_audio.export(mp3_file_path, format="mp3")
            logger.debug(f"MP3 saved successfully at {mp3_file_path} for conversation_id: {conversation_id}")
        except Exception as e:
            logger.error(f"Error saving MP3: {e}")
            await _refund_mp3_charge()
            return

        # Ledger the export so it counts against the owner's storage quota (one
        # row per file on disk). Fail fast: written first, ledgered immediately
        # after -- if the ledger insert (or the getsize sizing it) fails we delete
        # the file and re-raise so an unaccounted artifact never exists. BASE_DIR
        # carries a ".." segment, so the path is resolved before the ledger.
        try:
            mp3_size_bytes = os.path.getsize(mp3_file_path)
            async with get_db_connection() as conn:
                await record_generated_file(
                    conn, conversation_id, 'mp3', os.path.abspath(mp3_file_path), mp3_size_bytes
                )
                await conn.commit()
        except Exception:
            if os.path.exists(mp3_file_path):
                try:
                    os.remove(mp3_file_path)
                except OSError:
                    logger.warning("Could not remove unaccounted MP3 file at %s", mp3_file_path)
            raise
    except Exception:
        # Any failure after charging (generation error or the ledger re-raise
        # above) leaves the user with no MP3 -- refund so they are not billed for
        # audio they never received, then re-raise.
        await _refund_mp3_charge()
        raise
