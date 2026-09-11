# tools/download_mp3.py

import asyncio
import logging
from i18n import Translator
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO

from dotenv import load_dotenv
from pydub import AudioSegment

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


@dataclass(frozen=True, slots=True)
class _OriginalAudioSource:
    kind: str
    message_id: int
    attachment_ref: str | None = None
    duration_seconds: float | None = None
    call_id: str | None = None
    covered_message_ids: tuple[int, ...] = ()


@dataclass(slots=True)
class _PreparedMessageAudio:
    original_segment: AudioSegment | None
    voice: CanonicalVoice | None
    chunks: list[str]


_MIME_TO_PYDUB_FORMAT = {
    "application/ogg": "ogg",
    "audio/aac": "aac",
    "audio/flac": "flac",
    "audio/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/wav": "wav",
    "audio/wave": "wav",
    "audio/webm": "webm",
    "audio/x-wav": "wav",
}
_PHONE_PCMU_BYTES_PER_SECOND = 8_000
_MAX_REUSED_ORIGINAL_MESSAGE_SECONDS = 15 * 60
_MAX_REUSED_ORIGINAL_TOTAL_SECONDS = 30 * 60
_ATTACHMENT_DECODE_SAMPLE_RATE = 16_000
_ATTACHMENT_DECODE_SAMPLE_WIDTH = 2
_ATTACHMENT_DECODE_CHANNELS = 1
_ATTACHMENT_DECODE_TIMEOUT_SECONDS = 120
_ATTACHMENT_DECODE_MAX_SECONDS = _MAX_REUSED_ORIGINAL_MESSAGE_SECONDS + 1
_MAX_ATTACHMENT_DECODED_BYTES = (
    _ATTACHMENT_DECODE_MAX_SECONDS
    * _ATTACHMENT_DECODE_SAMPLE_RATE
    * _ATTACHMENT_DECODE_SAMPLE_WIDTH
    * _ATTACHMENT_DECODE_CHANNELS
)


async def _discover_original_audio_sources(
    conn,
    conversation_id: int,
) -> dict[int, _OriginalAudioSource]:
    """Find durable per-message originals without resolving private paths.

    The actual phone path and attachment are owner-scoped again immediately
    before reading. Older installations without the optional provenance
    tables simply fall back to TTS.
    """
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('PHONE_CALL_MESSAGE_AUDIO_RANGES','MESSAGE_VOICE_NOTES','FILE_ATTACHMENTS')"
    )
    available = {str(row[0]) for row in await cursor.fetchall()}
    sources: dict[int, _OriginalAudioSource] = {}

    if "PHONE_CALL_MESSAGE_AUDIO_RANGES" in available:
        cursor = await conn.execute(
            """
            SELECT a.message_id, a.start_byte, a.end_byte
            FROM PHONE_CALL_MESSAGE_AUDIO_RANGES a
            JOIN MESSAGES m ON m.id = a.message_id
            WHERE m.conversation_id = ?
            """,
            (conversation_id,),
        )
        for row in await cursor.fetchall():
            message_id = int(row[0])
            sources[message_id] = _OriginalAudioSource(
                kind="phone",
                message_id=message_id,
                duration_seconds=(int(row[2]) - int(row[1]))
                / _PHONE_PCMU_BYTES_PER_SECOND,
            )

    if "MESSAGE_VOICE_NOTES" in available:
        cursor = await conn.execute(
            """
            SELECT v.message_id, v.audio_attachment_ref, v.duration_seconds
            FROM MESSAGE_VOICE_NOTES v
            JOIN MESSAGES m ON m.id = v.message_id
            WHERE m.conversation_id = ?
              AND v.audio_attachment_ref IS NOT NULL
            """,
            (conversation_id,),
        )
        for row in await cursor.fetchall():
            message_id = int(row[0])
            sources.setdefault(
                message_id,
                _OriginalAudioSource(
                    kind="attachment",
                    message_id=message_id,
                    attachment_ref=str(row[1]),
                    duration_seconds=(
                        float(row[2]) if row[2] is not None else None
                    ),
                ),
            )

    if "FILE_ATTACHMENTS" in available:
        cursor = await conn.execute(
            """SELECT a.message_id,a.public_id FROM FILE_ATTACHMENTS a
            JOIN MESSAGES m ON m.id=a.message_id AND m.conversation_id=a.conversation_id
                AND m.user_id=a.user_id
            WHERE a.conversation_id=? AND a.attachment_type='audio' AND a.status='active'
            ORDER BY a.id""", (conversation_id,))
        for row in await cursor.fetchall():
            sources.setdefault(int(row[0]), _OriginalAudioSource(
                kind="attachment", message_id=int(row[0]), attachment_ref=str(row[1])))

    return sources


async def _discover_requester_original_audio_sources(
    conn,
    *,
    conversation_id: int,
    owner_user_id: int,
    requester_user_id: int,
) -> dict[int, _OriginalAudioSource]:
    """Do not turn cross-owner admin access into private-audio access."""
    if int(owner_user_id) != int(requester_user_id):
        return {}
    return await _discover_original_audio_sources(conn, conversation_id)


async def _discover_phone_recording_blocks(
    conn, *, conversation_id: int, requester_user_id: int, messages,
) -> dict[int, _OriginalAudioSource]:
    """Use each complete call once when its messages form one export block.

    A call transferred between conversations, or interleaved with written
    messages, keeps the per-message path. Importing its entire recording there
    would include another conversation or reorder intervening messages.
    """
    required = {"PHONE_CALLS", "PHONE_RECORDINGS", "PHONE_CALL_MESSAGE_LINKS"}
    tables = await (await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('PHONE_CALLS','PHONE_RECORDINGS','PHONE_CALL_MESSAGE_LINKS')"
    )).fetchall()
    if {str(row[0]) for row in tables} != required:
        return {}
    rows = await (await conn.execute(
        """
        SELECT c.id,l.message_id,r.duration_seconds
        FROM PHONE_CALLS c
        JOIN PHONE_RECORDINGS r ON r.call_id=c.id
        JOIN PHONE_CALL_MESSAGE_LINKS l ON l.call_id=c.id
        JOIN MESSAGES m ON m.id=l.message_id
        WHERE c.conversation_id=? AND c.owner_user_id=?
          AND c.active_conversation_id IS NULL
          AND c.ended_at IS NOT NULL AND c.deleted_at IS NULL
          AND r.status='available' AND r.local_deleted_at IS NULL
          AND r.mixed_path IS NOT NULL AND r.duration_seconds>0
          AND r.id=(SELECT MAX(latest.id) FROM PHONE_RECORDINGS latest WHERE latest.call_id=c.id)
          AND l.origin_channel='phone' AND m.conversation_id=c.conversation_id
          AND m.user_id=c.owner_user_id
          AND NOT EXISTS (
              SELECT 1 FROM PHONE_CALL_MESSAGE_LINKS other
              JOIN MESSAGES outside ON outside.id=other.message_id
              WHERE other.call_id=c.id AND outside.conversation_id<>c.conversation_id
          )
        ORDER BY m.id
        """,
        (conversation_id, requester_user_id),
    )).fetchall()
    calls: dict[str, tuple[float, list[int]]] = {}
    for call_id, message_id, duration in rows:
        calls.setdefault(str(call_id), (float(duration), []))[1].append(int(message_id))
    positions = {int(message["id"]): index for index, message in enumerate(messages)}
    blocks = {}
    for call_id, (duration, ids) in calls.items():
        if (not all(mid in positions for mid in ids)
                or positions[ids[-1]] - positions[ids[0]] + 1 != len(ids)):
            continue
        blocks[ids[0]] = _OriginalAudioSource(
            kind="phone_call", message_id=ids[0], duration_seconds=duration,
            call_id=call_id, covered_message_ids=tuple(ids),
        )
    return blocks


def _attachment_audio_format(metadata: dict) -> str | None:
    mime = str(
        metadata.get("mime_detected") or metadata.get("declared_mime") or ""
    ).split(";", 1)[0].strip().lower()
    if mime in _MIME_TO_PYDUB_FORMAT:
        return _MIME_TO_PYDUB_FORMAT[mime]

    suffix = os.path.splitext(str(metadata.get("storage_key") or ""))[1].lower()
    return {
        ".aac": "aac",
        ".flac": "flac",
        ".m4a": "mp4",
        ".mp3": "mp3",
        ".mp4": "mp4",
        ".oga": "ogg",
        ".ogg": "ogg",
        ".opus": "ogg",
        ".wav": "wav",
        ".webm": "webm",
    }.get(suffix)


def _decode_bounded_attachment_audio(
    data: bytes,
    audio_format: str | None,
) -> AudioSegment:
    """Decode untrusted compressed audio into bounded, normalized PCM."""
    command = [
        AudioSegment.converter,
        "-v",
        "error",
        "-nostdin",
        "-max_alloc",
        str(64 * 1024 * 1024),
        "-threads",
        "1",
    ]
    if audio_format:
        command.extend(("-f", audio_format))
    command.extend(
        (
            "-i",
            "pipe:0",
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-t",
            str(_ATTACHMENT_DECODE_MAX_SECONDS),
            "-ac",
            str(_ATTACHMENT_DECODE_CHANNELS),
            "-ar",
            str(_ATTACHMENT_DECODE_SAMPLE_RATE),
            "-acodec",
            "pcm_s16le",
            "-f",
            "s16le",
            "pipe:1",
        )
    )
    result = subprocess.run(
        command,
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=_ATTACHMENT_DECODE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError("bounded attachment audio decode failed")
    if len(result.stdout) > _MAX_ATTACHMENT_DECODED_BYTES:
        raise RuntimeError("bounded attachment audio decode exceeded its limit")
    return AudioSegment(
        data=result.stdout,
        sample_width=_ATTACHMENT_DECODE_SAMPLE_WIDTH,
        frame_rate=_ATTACHMENT_DECODE_SAMPLE_RATE,
        channels=_ATTACHMENT_DECODE_CHANNELS,
    )


async def _decode_original_audio(
    source: _OriginalAudioSource,
    *,
    requester_user_id: int,
    conversation_id: int,
) -> AudioSegment | None:
    """Load one owner-scoped original, returning None for a safe TTS fallback."""
    try:
        if source.kind == "attachment":
            from file_storage import read_attachment_bytes

            if not source.attachment_ref:
                return None
            result = await read_attachment_bytes(
                source.attachment_ref,
                user_id=requester_user_id,
                conversation_id=conversation_id,
                message_id=source.message_id,
                require_kind="audio",
            )
            if result is None:
                return None
            data, metadata = result
            audio_format = _attachment_audio_format(metadata)
            return await asyncio.to_thread(
                _decode_bounded_attachment_audio,
                data,
                audio_format,
            )

        if source.kind == "phone_call":
            from integrations.telephony.purge import PhoneDataPurgeRepository
            from integrations.telephony.recording_storage import resolve_private_recording_path

            audio = await PhoneDataPurgeRepository().get_owned_recording(
                owner_user_id=requester_user_id, call_id=source.call_id, track="mixed",
            )
            if int(audio["conversation_id"]) != int(conversation_id):
                return None
            path = resolve_private_recording_path(str(audio["call_id"]), str(audio["path"]))
            # The saved mix already preserves simultaneous voices, pauses,
            # greetings and notices. Decode it once; never concatenate the
            # overlapping message windows from this call on success.
            return await asyncio.to_thread(
                lambda: _decode_bounded_attachment_audio(path.read_bytes(), "mp3")
            )

        if source.kind == "phone":
            from integrations.telephony.message_audio import (
                PhoneMessageAudioRange,
                open_pcmu_range_as_wav_chunks,
            )
            from integrations.telephony.purge import PhoneDataPurgeRepository
            from integrations.telephony.recording_storage import (
                resolve_private_recording_path,
            )

            audio = await PhoneDataPurgeRepository().get_owned_message_audio(
                owner_user_id=requester_user_id,
                message_id=source.message_id,
            )
            if int(audio["conversation_id"]) != int(conversation_id):
                return None
            path = resolve_private_recording_path(
                str(audio["call_id"]),
                str(audio["path"]),
            )
            audio_range = PhoneMessageAudioRange(
                start_byte=int(audio["start_byte"]),
                end_byte=int(audio["end_byte"]),
            )
            chunks = open_pcmu_range_as_wav_chunks(path, audio_range)
            try:
                wav_data = await asyncio.to_thread(lambda: b"".join(chunks))
            finally:
                chunks.close()
            return await asyncio.to_thread(
                AudioSegment.from_file,
                BytesIO(wav_data),
                format="wav",
            )
    except Exception as exc:
        # Retention is best-effort and may race with a privacy deletion. Do not
        # expose private paths in logs; a missing/corrupt original falls to TTS.
        logger.warning(
            "Original %s audio unavailable for message_id=%s (%s); using TTS",
            source.kind,
            source.message_id,
            type(exc).__name__,
        )
    return None


async def _prepare_message_audio(
    messages,
    original_sources: dict[int, _OriginalAudioSource],
    *,
    requester_user_id: int,
    conversation_id: int,
    bot_voice: CanonicalVoice | None,
    user_voice: CanonicalVoice | None,
    voice_resolver: Callable[
        [], Awaitable[tuple[CanonicalVoice, CanonicalVoice]]
    ] | None = None,
    phone_recordings: dict[int, _OriginalAudioSource] | None = None,
) -> tuple[list[_PreparedMessageAudio], dict[str, int]]:
    prepared: list[_PreparedMessageAudio] = []
    characters_by_provider: dict[str, int] = {}
    reused_original_seconds = 0.0
    covered_messages: set[int] = set()

    for message in messages:
        message_id = int(message["id"])
        if message_id in covered_messages:
            continue
        recording = (phone_recordings or {}).get(message_id)
        if (recording is not None and recording.duration_seconds is not None
                and 0 < recording.duration_seconds <= _MAX_REUSED_ORIGINAL_MESSAGE_SECONDS
                and reused_original_seconds + recording.duration_seconds <= _MAX_REUSED_ORIGINAL_TOTAL_SECONDS):
            original = await _decode_original_audio(
                recording, requester_user_id=requester_user_id,
                conversation_id=conversation_id,
            )
            if original is not None:
                seconds = len(original) / 1000
                if (0 < seconds <= _MAX_REUSED_ORIGINAL_MESSAGE_SECONDS
                        and reused_original_seconds + seconds <= _MAX_REUSED_ORIGINAL_TOTAL_SECONDS):
                    prepared.append(_PreparedMessageAudio(original, None, []))
                    reused_original_seconds += seconds
                    covered_messages.update(recording.covered_message_ids)
                    continue
            # Missing/deleted/corrupt mix: keep all messages and let their
            # individual originals or normal TTS fallback handle them below.
        source = original_sources.get(message_id)
        source_duration = source.duration_seconds if source is not None else None
        within_original_budget = bool(
            source is not None
            and (source_duration is None and source.kind == "attachment"
                 or source_duration is not None and 0 < source_duration <= _MAX_REUSED_ORIGINAL_MESSAGE_SECONDS)
            and reused_original_seconds + (source_duration or 0) < _MAX_REUSED_ORIGINAL_TOTAL_SECONDS
        )
        if source is not None and within_original_budget:
            original = await _decode_original_audio(
                source,
                requester_user_id=requester_user_id,
                conversation_id=conversation_id,
            )
            if original is not None:
                actual_seconds = len(original) / 1_000
                if (
                    0 < actual_seconds <= _MAX_REUSED_ORIGINAL_MESSAGE_SECONDS
                    and reused_original_seconds + actual_seconds
                    <= _MAX_REUSED_ORIGINAL_TOTAL_SECONDS
                ):
                    reused_original_seconds += actual_seconds
                    prepared.append(
                        _PreparedMessageAudio(
                            original_segment=original,
                            voice=None,
                            chunks=[],
                        )
                    )
                    continue
                logger.warning(
                    "Original audio exceeded the MP3 reuse budget for "
                    "message_id=%s; using TTS",
                    message_id,
                )
        elif source is not None:
            logger.info(
                "Original audio is outside the MP3 reuse budget for "
                "message_id=%s; using TTS",
                message_id,
            )

        text = process_text_for_tts(message["message"])
        if not text:
            continue
        if bot_voice is None or user_voice is None:
            if voice_resolver is None:
                raise RuntimeError("MP3 fallback voices were not resolved")
            bot_voice, user_voice = await voice_resolver()
        chunks = await insert_tts_break(text)
        voice = bot_voice if message["type"] == "bot" else user_voice
        prepared.append(
            _PreparedMessageAudio(
                original_segment=None,
                voice=voice,
                chunks=chunks,
            )
        )
        characters_by_provider[voice.provider] = (
            characters_by_provider.get(voice.provider, 0) + len(text)
        )

    return prepared, characters_by_provider


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
    # FastAPI initializes these process-local service ids in its lifespan, but
    # Dramatiq is a separate process and never runs that lifespan. Lazily load
    # them in the worker before the first export that actually needs TTS.
    needs_initialization = False
    for provider in characters_by_provider:
        _, service_id = Cost.get_tts_service(provider)
        if service_id is None:
            needs_initialization = True
            break
    if needs_initialization:
        await Cost.initialize()

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

async def _billed_audio_chunks(generator, chunks, provider, billing_adapter):
    """Bill each native HTTP chunk, keeping its existing session and voice context.

    Both native generators yield one complete response per input chunk. Stop or
    revocation is checked before the next request; an in-flight response finishes
    and settles before cancellation, without dispatching the remaining chunks.
    """
    try:
        for text in chunks:
            token, started = None, False
            try:
                token = await billing_adapter.reserve(provider=provider, characters=len(text))
                started = await billing_adapter.provider_started(token)
                if not started:
                    raise RuntimeError("MP3 provider reservation was not claimed")
                audio = await anext(generator)
                if not audio:
                    raise RuntimeError("MP3 provider returned no audio")
                await billing_adapter.settle(token)
                yield audio
            except BaseException:
                if token is not None:
                    await billing_adapter.failed(token, provider_started=started, reason="mp3_export_failed")
                raise
    finally:
        await generator.aclose()


async def generate_and_save_mp3(conversation_id: int, user_id: int, is_admin: bool, ui_language: str = "en",
                                *, billing_adapter=None, check_access=None):
    logger.debug(f"Starting MP3 generation for conversation_id: {conversation_id}")
    if check_access is not None:
        await check_access()
    async with get_db_connection(readonly=True) as conn:
        # Verify permissions and conversation existence
        query_convo = """
            SELECT c.id, c.user_id AS owner_user_id, c.role_id, u.username,
                   llm.machine, llm.model,
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

        # Get messages
        query_messages = """
            SELECT id, date, message, type FROM messages
            WHERE conversation_id = ?
            ORDER BY id ASC, date ASC
        """
        async with conn.execute(query_messages, (conversation_id,)) as cursor:
            messages = await cursor.fetchall()

        # Per-message originals remain strictly owner-scoped. Admin access to
        # another user's conversation does not grant private retained audio.
        original_sources = await _discover_requester_original_audio_sources(
            conn,
            conversation_id=conversation_id,
            owner_user_id=int(conversation["owner_user_id"]),
            requester_user_id=user_id,
        )
        phone_recordings = await _discover_phone_recording_blocks(
            conn, conversation_id=conversation_id, requester_user_id=user_id,
            messages=messages,
        ) if int(conversation["owner_user_id"]) == int(user_id) else {}

    async def resolve_export_voices():
        async with get_db_connection(readonly=True) as conn:
            return await _resolve_export_voices(conversation, conn)

    # Decode retained originals first, then size and reserve only the TTS work
    # needed for the remaining messages. This keeps the conversation order and
    # avoids charging again for audio that already exists.
    prepared_messages, characters_by_provider = await _prepare_message_audio(
        messages,
        original_sources,
        requester_user_id=user_id,
        conversation_id=conversation_id,
        bot_voice=None,
        user_voice=None,
        voice_resolver=resolve_export_voices,
        phone_recordings=phone_recordings,
    )

    if not prepared_messages:
        logger.warning("No audio to export for conversation_id: %s", conversation_id)
        return

    original_count = sum(
        item.original_segment is not None for item in prepared_messages
    )
    tts_count = len(prepared_messages) - original_count
    logger.info(
        "Prepared MP3 export conversation_id=%s originals=%d "
        "tts_messages=%d tts_characters=%d",
        conversation_id,
        original_count,
        tts_count,
        sum(characters_by_provider.values()),
    )

    charged: dict[str, int] = {}
    profile = None
    if characters_by_provider:
        profile = await get_tts_profile("mp3")
        if billing_adapter is None:
            reservation = await _charge_mp3_providers(user_id, characters_by_provider)
            if reservation is None:
                logger.warning("TTS reservation failed for MP3 export: user_id=%s conversation_id=%s",
                               user_id, conversation_id)
                return
            charged = reservation

    async def _refund_mp3_charge():
        await _refund_mp3_charges(user_id, charged)

    try:
        audio_segments = []
        for prepared_message in prepared_messages:
            if check_access is not None:
                await check_access()
            if prepared_message.original_segment is not None:
                audio_segments.append(prepared_message.original_segment)
                continue

            voice = prepared_message.voice
            if voice is None or profile is None:
                raise RuntimeError("MP3 TTS message was not fully prepared")
            audio_generator = get_tts_generator_for_voice(
                voice,
                prepared_message.chunks,
                profile=profile,
            )
            audio_input_format = (
                format_to_pydub(profile.output_format)
                if voice.provider == 'elevenlabs'
                else 'mp3'
            )
            if billing_adapter is not None:
                audio_generator = _billed_audio_chunks(audio_generator, prepared_message.chunks,
                    voice.provider, billing_adapter)
            try:
                async for audio_chunk in audio_generator:
                    audio_segment = AudioSegment.from_file(BytesIO(audio_chunk), format=audio_input_format)
                    audio_segments.append(audio_segment)
            finally:
                await audio_generator.aclose()

        if not audio_segments:
            logger.error(
                "No audio segments generated for MP3 export of conversation_id: %s",
                conversation_id,
            )
            await _refund_mp3_charge()
            return

        combined_audio = audio_segments[0]
        for segment in audio_segments[1:]:
            combined_audio += segment

        # Generate hash and file path
        if check_access is not None:
            await check_access()
        username = conversation["username"]
        hash_prefixes = generate_user_hash(username)
        user_hash = hash_prefixes[2]
        prefix1 = f"{conversation_id:07d}"[:3]
        prefix2 = f"{conversation_id:07d}"[3:]
        mp3_convo_folder = os.path.join(
            BASE_DIR,
            hash_prefixes[0],
            hash_prefixes[1],
            user_hash,
            "files",
            prefix1,
            prefix2,
            "mp3",
        )
        os.makedirs(mp3_convo_folder, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y_%m_%d_%H_%M_%S")
        prompt_name = str(
            conversation["prompt_name"] or Translator(ui_language).t("exports.conversation_filename", id=conversation_id)
        )
        prompt_name_safe = ''.join(
            c for c in prompt_name if c.isalnum() or c in (' ', '_')
        ).rstrip().replace(' ', '_')
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
        return os.path.abspath(mp3_file_path)
    except Exception:
        # Any failure after charging (generation error or the ledger re-raise
        # above) leaves the user with no MP3 -- refund so they are not billed for
        # audio they never received, then re-raise.
        await _refund_mp3_charge()
        raise
