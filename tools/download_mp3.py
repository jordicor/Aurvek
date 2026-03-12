# tools/download_mp3.py

import os
import sys
import logging
import asyncio
import aiosqlite
from datetime import datetime
import hashlib
from pydub import AudioSegment
from io import BytesIO
from dotenv import load_dotenv

# Import necessary functions from tts.py
from tools.tts import process_text_for_tts, insert_tts_break, get_tts_generator
from common import Cost, generate_user_hash, has_sufficient_balance, cost_tts, cache_directory, users_directory, elevenlabs_key, openai_key, tts_engine, get_balance, deduct_balance, load_service_costs

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

async def generate_and_save_mp3(conversation_id: int, user_id: int, is_admin: bool):
    logger.debug(f"Starting MP3 generation for conversation_id: {conversation_id}")
    async with aiosqlite.connect(f"file:db/{DB_NAME}?mode=ro", uri=True) as conn:
        conn.row_factory = aiosqlite.Row

        # Verify permissions and conversation existence
        query_convo = """
            SELECT c.id, u.username, llm.machine, llm.model, p.name AS prompt_name, v.voice_code
            FROM conversations c
            JOIN users u ON c.user_id = u.id
            LEFT JOIN llm ON c.llm_id = llm.id
            LEFT JOIN prompts p ON c.role_id = p.id
            LEFT JOIN voices v ON p.voice_id = v.id
            WHERE c.id = ? AND (c.user_id = ? OR ?)
        """
        async with conn.execute(query_convo, (conversation_id, user_id, is_admin)) as cursor:
            conversation = await cursor.fetchone()
            if not conversation:
                logger.warning(f"Unauthorized access or conversation not found for conversation_id: {conversation_id}")
                return

        # Get messages
        query_messages = """
            SELECT id, date, message, type FROM messages
            WHERE conversation_id = ?
            ORDER BY id ASC, date ASC
        """
        async with conn.execute(query_messages, (conversation_id,)) as cursor:
            messages = await cursor.fetchall()

    # Generate MP3
    audio_segments = []
    bot_voice_id = conversation['voice_code']
    user_voice_id = "nMPrFLO7QElx9wTR0JGo"  # Default voice for user

    for message in messages:
        text = process_text_for_tts(message['message'])
        chunks = await insert_tts_break(text)
        voice_id = bot_voice_id if message['type'] == 'bot' else user_voice_id

        audio_generator = get_tts_generator(tts_engine, voice_id, chunks)
        audio_input_format = 'ogg' if tts_engine == 'elevenlabs' else 'mp3'
        async for audio_chunk in audio_generator:
            audio_segment = AudioSegment.from_file(BytesIO(audio_chunk), format=audio_input_format)
            audio_segments.append(audio_segment)

    if audio_segments:
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

        timestamp = datetime.utcnow().strftime("%Y_%m_%d_%H_%M_%S")
        prompt_name_safe = ''.join(c for c in conversation["prompt_name"] if c.isalnum() or c in (' ', '_')).rstrip().replace(' ', '_')
        mp3_filename = f"{prompt_name_safe}_{timestamp}.mp3"
        mp3_file_path = os.path.join(mp3_convo_folder, mp3_filename)

        try:
            combined_audio.export(mp3_file_path, format="mp3")
            logger.debug(f"MP3 saved successfully at {mp3_file_path} for conversation_id: {conversation_id}")
        except Exception as e:
            logger.error(f"Error saving MP3: {e}")

