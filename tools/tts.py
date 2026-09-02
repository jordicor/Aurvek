import html
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import re
import orjson
import os
import time
import hashlib
import base64
import io
import inspect
from pydub import AudioSegment
import asyncio
import aiohttp
from fastapi import WebSocket
import aiofiles
from dataclasses import dataclass
from typing import Any, Protocol
import common as _common
from database import get_db_connection

# Own libraries
from models import User, ConnectionManager
from tools.tts_load_balancer import get_elevenlabs_key
from tools.tts_config import get_tts_profile, TTSProfile, format_to_pydub
from tools.voice_sync import mark_voice_deprecated
from ai_runtime.voice_resolution import (
    CanonicalVoice,
    CanonicalVoiceResolutionError,
    resolve_catalog_voice,
    resolve_default_voice,
    resolve_prompt_voice,
)
from common import (
    Cost,
    cache_directory,
    cost_tts,
    has_sufficient_balance,
    openai_key,
    refund_tts,
)
from log_config import logger

manager = ConnectionManager()
# Retained as a compatibility surface for callers/tests that used to inspect
# the legacy global engine. Canonical voices, not this value, select providers.
tts_engine = _common.tts_engine

# WebSocket TTS constants
ELEVENLABS_WS_ENDPOINT = "wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input"
ELEVENLABS_WS_CONNECT_TIMEOUT = 10   # seconds
ELEVENLABS_WS_FIRST_FRAME_TIMEOUT = 60  # seconds
ELEVENLABS_WS_RECEIVE_TIMEOUT = 30   # seconds

# --- Cache cleanup constants and state ---
CACHE_MAX_AGE_DAYS = 30
CACHE_CLEANUP_MIN_INTERVAL = 3600      # 1 hour — cooldown between cleanups
CACHE_CLEANUP_MAX_INTERVAL = 86400     # 24 hours — force cleanup after this
CACHE_CLEANUP_OPS_THRESHOLD = 500      # trigger cleanup every N TTS operations

_last_cleanup_ts: float = 0.0
_ops_since_cleanup: int = 0
_cleanup_running: bool = False


class TTSBillingError(RuntimeError):
    """An injected provider billing boundary could not be completed."""


class TTSBillingInsufficientBalance(TTSBillingError):
    """The injected payer cannot cover a TTS provider request."""


class TTSBillingAdapter(Protocol):
    async def cache_hit(
        self, *, provider: str, characters: int, cache_key: str
    ) -> None: ...

    async def reserve(self, *, provider: str, characters: int) -> Any: ...

    async def provider_started(self, token: Any) -> bool: ...

    async def settle(self, token: Any) -> None: ...

    async def failed(
        self, token: Any, *, provider_started: bool, reason: str
    ) -> None: ...


def process_plain_text(text):
    #logger.debug(f"entra en process_plain_text: {text}")
    # Decode HTML entities
    text = html.unescape(text)

    # Remove HTML
    soup = BeautifulSoup(text, 'html.parser')

    # Replace images
    for img in soup.find_all('img'):
        img.replace_with("[IMAGE]")

    # Replace code
    for code in soup.find_all('code'):
        code.replace_with("[CODE]")

    # Replace links
    for a in soup.find_all('a'):
        href = a.get('href', '')
        domain = urlparse(href).netloc
        a.replace_with(f"[LINK, {domain}]" if domain else "[LINK]")
    
    # Get the clean text
    clean_text = soup.get_text()

    # Process Markdown
    # Replace Markdown links including the domain
    def replace_markdown_link(match):
        url = match.group(2)
        domain = urlparse(url).netloc
        return f"[LINK, {domain}]" if domain else "[LINK]"
    
    clean_text = re.sub(r'\[([^\]]+)\]\(([^\)]+)\)', replace_markdown_link, clean_text)

    # Replace Markdown images
    clean_text = re.sub(r'!\[([^\]]*)\]\([^\)]+\)', '[IMAGE]', clean_text)

    # Replace plain URLs in the text
    def replace_plain_url(match):
        url = match.group(0)
        domain = urlparse(url).netloc
        return f"[LINK, {domain}]" if domain else "[LINK]"
    
    clean_text = re.sub(r'https?://\S+', replace_plain_url, clean_text)
    
    return clean_text


async def insert_tts_break(text, min_length=50, max_length=90, look_ahead=30):
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    
    # Define allowed characters (letters, digits, whitespace, punctuation, and accented characters)
    #allowed_characters = string.ascii_letters + string.digits + string.whitespace + string.punctuation + "áéíóúÁÉÍÓÚüÜñÑ"
    
    # Remove emojis and other unwanted characters
    #pattern = f"[^{re.escape(allowed_characters)}]"
    #text = re.sub(pattern, "", text)

    # Check if text length is less than or equal to max_length
    if len(text) <= max_length:
        return [text]

    chunks = []
    
    while len(text) > max_length:
        # Find last space or punctuation mark between min_length and max_length
        match = re.search(r".*[,;!?.)\]]\s|.*\s", text[min_length:max_length])

        if match:
            split_pos = match.end() + min_length

            # Look ahead to find a better split point
            look_ahead_match = re.search(r"[,;!?.)\]]", text[split_pos:split_pos+look_ahead])
            if look_ahead_match:
                split_pos = look_ahead_match.end() + split_pos

            chunk = text[:split_pos].rstrip()
            if len(chunk) >= min_length:
                chunks.append(chunk)
                text = text[split_pos:].lstrip()
            else:
                # If chunk is too short, find next split point
                next_match = re.search(r".*[,;!?.)\]]\s|.*\s", text[split_pos:])
                if next_match:
                    next_split_pos = next_match.end() + split_pos
                    chunks.append(text[:next_split_pos].rstrip())
                    text = text[next_split_pos:].lstrip()
                else:
                    # If no more split points, add all remaining text
                    chunks.append(text)
                    text = ""
        else:
            # If no suitable split point found, cut at max_length
            chunks.append(text[:max_length])
            text = text[max_length:]

    if text:
        # Handle remaining text
        if len(text) < min_length and chunks:
            chunks[-1] += " " + text
        else:
            chunks.append(text)

    return chunks

def process_text_for_tts(message):
    try:
        # Try to parse message as JSON
        message_data = orjson.loads(message)
        
        # Check if message_data is a list/dict that we can iterate over
        if isinstance(message_data, (list, dict)):
            processed_parts = []
            # If it's a dict, wrap it in a list to process it
            items = message_data if isinstance(message_data, list) else [message_data]
            
            for item in items:
                if isinstance(item, dict):
                    if item.get('type') == 'text':
                        processed_parts.append(process_plain_text(item['text']))
                    elif item.get('type') == 'image_url':
                        processed_parts.append("IMAGE")
                    elif item.get('type') == 'audio_url':
                        processed_parts.append("AUDIO")
                    # Add more types as needed
                    # elif item['type'] == 'pdf_url':
                    #     processed_parts.append("PDF")
                    # elif item['type'] == 'doc_url':
                    #     processed_parts.append("DOCUMENT")
            
            return ' '.join(processed_parts)
        else:
            # If message_data is not iterable, treat it as plain text
            return process_plain_text(str(message_data))

    except orjson.JSONDecodeError:
        # If not JSON, treat as plain text
        return process_plain_text(message)
    except Exception as e:
        # Log the error and return the message as plain text
        logger.error(f"Error processing text for TTS: {str(e)}")
        return process_plain_text(str(message))


@dataclass(frozen=True)
class PreviewVoice:
    """Explicit compatibility path for an uncatalogued admin preview voice."""

    voice_code: str
    provider: str


async def get_voice_from_prompt(prompt_id: int) -> CanonicalVoice:
    return await resolve_prompt_voice(prompt_id)


async def get_voice_code_from_prompt(prompt_id: int) -> str:
    """Compatibility wrapper for callers that only display the provider ID."""
    return (await get_voice_from_prompt(prompt_id)).voice_code


async def get_voice_from_conversation(
    conversation_id: int, current_user: User
) -> CanonicalVoice:
    async with get_db_connection(readonly=True) as conn:
        # First, check if the user has access to the conversation
        async with conn.execute('''
            SELECT c.id
            FROM CONVERSATIONS c
            WHERE c.id = ? AND c.user_id = ?
        ''', (conversation_id, current_user.id)) as cursor:
            conversation = await cursor.fetchone()

        if not conversation:
            raise ValueError("User does not have access to this conversation or conversation does not exist")

        # Resolve the prompt through the provider-neutral canonical resolver.
        async with conn.execute('''
            SELECT c.role_id
            FROM CONVERSATIONS c
            WHERE c.id = ?
        ''', (conversation_id,)) as cursor:
            prompt_row = await cursor.fetchone()

        if not prompt_row or prompt_row["role_id"] is None:
            raise ValueError("Conversation has no prompt configured")
        return await resolve_prompt_voice(prompt_row["role_id"], conn=conn)


async def get_voice_code_from_conversation(conversation_id: int, current_user: User) -> str:
    """Compatibility wrapper for callers that only display the provider ID."""
    return (await get_voice_from_conversation(conversation_id, current_user)).voice_code


async def resolve_playback_voice(
    voice_code: str | None,
    *,
    allow_uncatalogued_preview: bool = False,
    preview_provider: str | None = None,
) -> CanonicalVoice | PreviewVoice:
    """Resolve provider for user/sample playback without guessing across providers."""
    if not voice_code:
        return await resolve_default_voice()
    catalog_voice = await resolve_catalog_voice(voice_code)
    if catalog_voice is not None:
        if preview_provider and str(preview_provider).strip().lower() != catalog_voice.provider:
            raise CanonicalVoiceResolutionError(
                "preview_voice_provider_mismatch",
                "The requested preview provider does not match the catalogued voice provider.",
            )
        return catalog_voice
    if allow_uncatalogued_preview:
        provider = str(preview_provider or "").strip().lower()
        if not provider:
            raise CanonicalVoiceResolutionError(
                "preview_voice_provider_required",
                "An explicit provider is required for an uncatalogued admin preview voice.",
            )
        if provider not in {"elevenlabs", "openai"}:
            raise CanonicalVoiceResolutionError(
                "preview_voice_provider_unknown",
                "The explicit preview voice provider is unsupported.",
            )
        return PreviewVoice(voice_code=str(voice_code).strip(), provider=provider)
    raise CanonicalVoiceResolutionError(
        "voice_not_catalogued",
        "The selected voice is not present in the voice catalogue.",
    )


def get_tts_generator(
    engine: str,
    voice_id: str,
    chunks: list,
    profile: TTSProfile | None = None,
    resolved_voice: CanonicalVoice | PreviewVoice | None = None,
    elevenlabs_key: str | None = None,
):
    logger.info("entra en get_tts_generator")
    if engine == 'elevenlabs':
        logger.info("devuelve elevenlabs")
        if profile:
            return _elevenlabs_http_generator(
                voice_id, chunks,
                output_format=profile.output_format,
                model_id=profile.model_id,
                stability=profile.stability,
                similarity_boost=profile.similarity_boost,
                resolved_voice=resolved_voice,
                elevenlabs_key=elevenlabs_key,
            )
        return _elevenlabs_http_generator(
            voice_id,
            chunks,
            resolved_voice=resolved_voice,
            elevenlabs_key=elevenlabs_key,
        )
    elif engine == 'openai':
        return openai_generator(voice_id, chunks)
    else:
        raise ValueError(f"Unsupported TTS engine: {engine}")


def get_tts_generator_for_voice(
    voice: CanonicalVoice | PreviewVoice,
    chunks: list,
    profile: TTSProfile | None = None,
    elevenlabs_key: str | None = None,
):
    """Select the generator from the resolved voice, never from global config."""
    if voice.provider != "elevenlabs":
        return get_tts_generator(
            voice.provider,
            voice.voice_code,
            chunks,
            profile=profile,
            resolved_voice=voice,
        )
    return get_tts_generator(
        voice.provider,
        voice.voice_code,
        chunks,
        profile=profile,
        resolved_voice=voice,
        elevenlabs_key=elevenlabs_key,
    )


async def send_cached_audio(websocket: WebSocket, full_path_opus: str):
    logger.debug(f"Sending cached audio: {full_path_opus}")
    with open(full_path_opus, "rb") as f:
        while chunk := f.read(16384):
            await manager.send_bytes(websocket, chunk)


def get_file_path(hash_digest):
    file_path = os.path.join(str(cache_directory), hash_digest[:2], hash_digest[2:4], hash_digest[4:6], hash_digest[6:8])
    file_name_opus = f"{hash_digest}.opus"
    full_path_opus = os.path.join(file_path, file_name_opus)
    return file_path, full_path_opus


def get_tts_cache_digest(
    text: str,
    voice: CanonicalVoice | PreviewVoice,
    profile: TTSProfile,
) -> str:
    """Return the single cache identity shared by generation and lookup."""
    profile_identity = orjson.dumps(
        {
            "chunk_schedule": list(profile.chunk_schedule),
            "model_id": profile.model_id,
            "output_format": profile.output_format,
            "similarity_boost": profile.similarity_boost,
            "stability": profile.stability,
            "ws_enabled": profile.ws_enabled,
        },
        option=orjson.OPT_SORT_KEYS,
    ).decode("utf-8")
    hash_input = "_".join(
        (text, voice.provider, voice.voice_code, profile_identity)
    )
    return hashlib.sha256(hash_input.encode("utf-8")).hexdigest()


_ELEVENLABS_MISSING_VOICE_CODES = {
    "invalid_voice_id",
    "voice_does_not_exist",
    "voice_not_found",
}


def _elevenlabs_error_indicates_missing_voice(error_body: str | bytes | None) -> bool:
    """Recognize only provider errors that explicitly identify a missing voice."""
    if isinstance(error_body, bytes):
        error_text = error_body.decode("utf-8", errors="replace")
    else:
        error_text = str(error_body or "")

    try:
        payload = orjson.loads(error_text)
    except orjson.JSONDecodeError:
        payload = None

    def contains_explicit_code(value) -> bool:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in {"code", "status", "type"}:
                    normalized = str(child or "").strip().lower().replace("-", "_")
                    if normalized in _ELEVENLABS_MISSING_VOICE_CODES:
                        return True
                if contains_explicit_code(child):
                    return True
        elif isinstance(value, list):
            return any(contains_explicit_code(child) for child in value)
        return False

    if contains_explicit_code(payload):
        return True

    normalized_text = " ".join(error_text.lower().replace("_", " ").split())
    return bool(
        re.search(
            r"\bvoice(?: id)?\b.{0,80}\b(?:does not exist|not found)\b",
            normalized_text,
        )
        or re.search(
            r"\b(?:invalid|unknown)\s+voice id\b",
            normalized_text,
        )
    )


def _invoke_elevenlabs_key_selector(voice_id: str) -> str | None:
    """Call the voice-aware selector while retaining zero-arg test injectors."""

    getter = get_elevenlabs_key
    try:
        signature = inspect.signature(getter)
    except (TypeError, ValueError):
        return getter(voice_id)
    try:
        signature.bind(voice_id)
    except TypeError:
        return getter()
    return getter(voice_id)


async def _select_elevenlabs_key(voice_id: str) -> str | None:
    return await asyncio.to_thread(_invoke_elevenlabs_key_selector, voice_id)


async def _elevenlabs_http_generator(
    voice_id: str,
    chunks: list,
    output_format: str = "opus_48000_128",
    model_id: str = "eleven_turbo_v2_5",
    stability: float = 0.45,
    similarity_boost: float = 0.89,
    resolved_voice: CanonicalVoice | PreviewVoice | None = None,
    elevenlabs_key: str | None = None,
):
    """Generate TTS via per-chunk HTTP POST requests.
    Used as primary path for existing TTS button, and as fallback for WS path.
    """
    logger.info("Entering elevenlabs HTTP generator (model=%s, format=%s)", model_id, output_format)
    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        f"/stream?output_format={output_format}"
    )

    async with aiohttp.ClientSession() as session:
        for i, chunk_text in enumerate(chunks):
            try:
                # The preflight key owns the first provider call. Later chunks
                # retain the existing load-balancing behavior, constrained to
                # keys that have verified access to this exact voice.
                selected_key = (
                    elevenlabs_key
                    if i == 0 and elevenlabs_key
                    else await _select_elevenlabs_key(voice_id)
                )
                if not selected_key:
                    raise ValueError("No valid Elevenlabs API key available")

                headers = {
                    "Content-Type": "application/json",
                    "xi-api-key": selected_key
                }

                previous_text = None if i == 0 else " ".join(chunks[:i])
                next_text = None if i == len(chunks) - 1 else " ".join(chunks[i + 1:])
                payload = {
                    "text": chunk_text,
                    "model_id": model_id,
                    "voice_settings": {
                        "stability": stability,
                        "similarity_boost": similarity_boost
                    }
                }
                if previous_text:
                    payload["previous_text"] = previous_text
                if next_text:
                    payload["next_text"] = next_text

                async with session.post(url, headers=headers, json=payload) as response:
                    if response.status != 200:
                        error_message = await response.text()
                        if _elevenlabs_error_indicates_missing_voice(error_message):
                            await mark_voice_deprecated(
                                resolved_voice or voice_id,
                                provider="elevenlabs",
                            )
                        raise ValueError(f"Elevenlabs API error: {response.status} - {error_message}")
                    yield await response.read()
            except ValueError as ve:
                logger.error(f"Error in elevenlabs HTTP generator: {str(ve)}")
                raise
            except Exception as e:
                logger.error(f"Unexpected error in elevenlabs HTTP generator: {str(e)}")
                raise ValueError(f"Unexpected error in TTS generation: {str(e)}")


async def elevenlabs_ws_generator(
    voice_id: str,
    chunks: list,
    model_id: str = "eleven_turbo_v2_5",
    output_format: str = "mp3_44100_128",
    stability: float = 0.45,
    similarity_boost: float = 0.89,
    chunk_schedule: list | None = None,
    resolved_voice: CanonicalVoice | PreviewVoice | None = None,
    elevenlabs_key: str | None = None,
):
    """Generate TTS audio via ElevenLabs WebSocket API.

    Sends all text over a single WebSocket connection.
    Yields audio bytes as they arrive -- MP3 frames are independently
    decodable by the browser's AudioContext.decodeAudioData().

    Falls back to HTTP generator if WebSocket fails.
    """
    if not chunks:
        return

    selected_key = elevenlabs_key or await _select_elevenlabs_key(voice_id)
    if not selected_key:
        raise ValueError("No valid ElevenLabs API key available")

    full_text = " ".join(chunks)
    url = ELEVENLABS_WS_ENDPOINT.format(voice_id=voice_id)

    # --- Attempt WebSocket connection ---
    use_fallback = False
    fallback_reason = ""

    async with aiohttp.ClientSession() as session:
        try:
            ws = await asyncio.wait_for(
                session.ws_connect(
                    url,
                    params={
                        "model_id": model_id,
                        "output_format": output_format,
                    },
                    headers={"xi-api-key": selected_key},
                ),
                timeout=ELEVENLABS_WS_CONNECT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            use_fallback = True
            fallback_reason = "connection timed out"
        except aiohttp.WSServerHandshakeError as e:
            if e.status in (400, 401, 403, 404, 422):
                if _elevenlabs_error_indicates_missing_voice(e.message):
                    await mark_voice_deprecated(
                        resolved_voice or voice_id,
                        provider="elevenlabs",
                    )
                raise ValueError(
                    f"ElevenLabs WebSocket handshake failed: {e.status} {e.message}"
                )
            use_fallback = True
            fallback_reason = f"handshake error {e.status}"
        except Exception as e:
            use_fallback = True
            fallback_reason = str(e)

        if use_fallback:
            logger.warning(
                "ElevenLabs WebSocket %s, falling back to HTTP", fallback_reason
            )
        else:
            # --- WebSocket connected -- stream audio ---
            received_final = False
            try:
                # BOS
                await ws.send_json({
                    "text": " ",
                    "voice_settings": {
                        "stability": stability,
                        "similarity_boost": similarity_boost,
                    },
                    "generation_config": {
                        "chunk_length_schedule": chunk_schedule or [120, 160, 250, 290],
                    },
                })

                # Send full text + EOS
                await ws.send_json({"text": full_text + " "})
                await ws.send_json({"text": ""})

                # Receive audio frames
                is_first_frame = True
                while True:
                    timeout = (
                        ELEVENLABS_WS_FIRST_FRAME_TIMEOUT
                        if is_first_frame
                        else ELEVENLABS_WS_RECEIVE_TIMEOUT
                    )
                    try:
                        msg = await asyncio.wait_for(
                            ws.receive(), timeout=timeout
                        )
                    except asyncio.TimeoutError:
                        kind = "first frame" if is_first_frame else "frame"
                        logger.error(
                            "ElevenLabs WS receive timed out (%s) after %ds",
                            kind, timeout,
                        )
                        break

                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = orjson.loads(msg.data)

                        if "audio" in data and data["audio"]:
                            audio_bytes = base64.b64decode(data["audio"])
                            if audio_bytes:
                                is_first_frame = False
                                yield audio_bytes

                        if data.get("isFinal"):
                            received_final = True
                            break

                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                    ):
                        break
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error(
                            "ElevenLabs WebSocket error: %s", ws.exception()
                        )
                        break

                if not received_final:
                    raise ValueError(
                        "ElevenLabs WebSocket stream ended without completion "
                        "signal -- partial audio will NOT be cached"
                    )

            finally:
                if not ws.closed:
                    await ws.close()

            return  # WS succeeded, skip fallback

    # --- HTTP fallback (outside WS session) ---
    if use_fallback:
        async for chunk in _elevenlabs_http_generator(
            voice_id, chunks,
            output_format=output_format,
            model_id=model_id,
            stability=stability,
            similarity_boost=similarity_boost,
            resolved_voice=resolved_voice,
            elevenlabs_key=selected_key,
        ):
            yield chunk


async def openai_generator(voice_id: str, chunks: list):
    """Generate TTS audio using OpenAI API."""
    logger.info("Entering openai generator")
    url = "https://api.openai.com/v1/audio/speech"

    if not openai_key:
        raise ValueError("OpenAI API key not configured")

    headers = {
        "Authorization": f"Bearer {openai_key}",
        "Content-Type": "application/json"
    }

    async with aiohttp.ClientSession() as session:
        for chunk_text in chunks:
            try:
                payload = {
                    "model": "tts-1",
                    "input": chunk_text,
                    "voice": voice_id,
                    "response_format": "mp3"
                }

                async with session.post(url, headers=headers, json=payload) as response:
                    if response.status != 200:
                        error_message = await response.text()
                        raise ValueError(f"OpenAI TTS API error: {response.status} - {error_message}")
                    yield await response.read()
            except ValueError as ve:
                logger.error(f"Error in openai_generator: {str(ve)}")
                raise
            except Exception as e:
                logger.error(f"Unexpected error in openai_generator: {str(e)}")
                raise ValueError(f"Unexpected error in OpenAI TTS generation: {str(e)}")

def _maybe_cleanup_cache():
    """Check if cache cleanup should run. Non-blocking -- launches background task if needed."""
    global _last_cleanup_ts, _ops_since_cleanup, _cleanup_running

    _ops_since_cleanup += 1
    now = time.time()
    time_since = now - _last_cleanup_ts

    if time_since < CACHE_CLEANUP_MIN_INTERVAL:
        return

    if _cleanup_running:
        return

    if time_since >= CACHE_CLEANUP_MAX_INTERVAL or _ops_since_cleanup >= CACHE_CLEANUP_OPS_THRESHOLD:
        _cleanup_running = True
        _last_cleanup_ts = now
        _ops_since_cleanup = 0
        try:
            asyncio.create_task(_do_cache_cleanup())
        except RuntimeError:
            _cleanup_running = False  # event loop closing, reset flag


async def _do_cache_cleanup():
    """Delete cached TTS files older than CACHE_MAX_AGE_DAYS. Runs in executor to avoid blocking event loop."""
    global _cleanup_running
    try:
        deleted = await asyncio.get_running_loop().run_in_executor(None, _cleanup_sync)
        logger.info("Cache cleanup: deleted %d files older than %d days", deleted, CACHE_MAX_AGE_DAYS)
    except Exception as e:
        logger.error("Cache cleanup failed: %s", e)
    finally:
        _cleanup_running = False


def _cleanup_sync() -> int:
    """Synchronous cache cleanup. Intended to run in a thread executor."""
    cutoff = time.time() - (CACHE_MAX_AGE_DAYS * 86400)
    deleted = 0
    cache_dir = str(cache_directory.resolve())

    for root, dirs, files in os.walk(cache_dir, topdown=False):
        for f in files:
            path = os.path.join(root, f)
            try:
                if os.path.islink(path):
                    continue
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    deleted += 1
            except OSError:
                pass

        if root != cache_dir:
            try:
                os.rmdir(root)
            except OSError:
                pass

    return deleted


def _decode_and_export_audio_chunks(
    audio_chunks: tuple[bytes, ...],
    audio_input_format: str,
) -> bytes:
    """Decode, combine, and encode generated chunks outside the event loop."""
    audio_segments = [
        AudioSegment.from_file(io.BytesIO(chunk), format=audio_input_format)
        for chunk in audio_chunks
    ]
    combined_audio = audio_segments[0]
    for segment in audio_segments[1:]:
        combined_audio += segment

    encoded_audio = io.BytesIO()
    combined_audio.export(encoded_audio, format="ogg", codec="libopus")
    return encoded_audio.getvalue()


async def handle_tts_request(
    websocket: WebSocket,
    data: dict,
    current_user: User,
    is_whatsapp=False,
    sample_voice_id=None,
    ws_mode=False,
    tts_context: str = "webchat",
    resolved_sample_voice: CanonicalVoice | PreviewVoice | None = None,
    resolved_voice_override: CanonicalVoice | PreviewVoice | None = None,
    tts_profile_override: TTSProfile | None = None,
    billing_adapter: TTSBillingAdapter | None = None,
):
    _cancelled = False
    billing_token: Any | None = None
    billing_provider_started = False
    try:
        text = data.get('text', '')
        author = data.get('author', 'bot')
        conversationId = data.get('conversationId', None)
        voice = None

        logger.debug(f"Received: text='{text}', author='{author}', conversationId={conversationId}, is_whatsapp={is_whatsapp}, sample_voice_id={sample_voice_id}")

        if conversationId is None and not sample_voice_id:
            return None, "Conversation ID not found or not available"

        if text:
            text = process_text_for_tts(text)

        try:
            if not sample_voice_id:
                characters_used = len(text)

                if characters_used == 0:
                    return None, "No text to synthesize."

                if resolved_voice_override is not None:
                    if author != 'bot':
                        raise ValueError(
                            "A canonical voice snapshot can only render bot audio."
                        )
                    voice = resolved_voice_override
                elif author == 'user':
                    voice = await resolve_playback_voice(current_user.voice_code)
                elif author == 'bot':
                    voice = await get_voice_from_conversation(conversationId, current_user)
                else:
                    voice = await resolve_default_voice()
                    logger.debug(f"Warning: Unknown author '{author}'. Using default voice.")

                logger.debug("voice_id=%s provider=%s", voice.voice_code, voice.provider)

                if billing_adapter is None:
                    tts_rate, tts_service_id = Cost.get_tts_service(voice.provider)
                    if tts_service_id is None:
                        raise ValueError(
                            f"TTS billing is not configured for provider {voice.provider}."
                        )
                    tts_cost = characters_used * tts_rate
                    if not await has_sufficient_balance(current_user.id, tts_cost):
                        if is_whatsapp:
                            return None, 'insufficient-balance'
                        if websocket:
                            await manager.send_json(websocket, {'action': 'insufficient-balance'})
                        return None, None

            else:
                if resolved_voice_override is not None:
                    raise ValueError(
                        "Sample voice and canonical voice snapshot cannot be combined."
                    )
                voice = resolved_sample_voice or await resolve_playback_voice(sample_voice_id)

        except ValueError as e:
            if is_whatsapp:
                return None, str(e)
            if websocket:
                await manager.send_json(websocket, {'action': 'error', 'message': str(e)})
            return None, None

        logger.debug("Before hash_digest, voice_id=%s provider=%s", voice.voice_code, voice.provider)

        # Load TTS profile for this context
        if tts_profile_override is not None and not isinstance(
            tts_profile_override,
            TTSProfile,
        ):
            raise ValueError("tts_profile_override must be a TTSProfile")
        profile = tts_profile_override or await get_tts_profile(tts_context)

        # ws_mode override: admin can only DOWNGRADE WS->HTTP, never upgrade HTTP->WS
        if tts_context == "webchat" and ws_mode and not profile.ws_enabled:
            ws_mode = False

        # The WebSocket transport is provider-specific and must never receive
        # an OpenAI (or other provider) voice identifier.
        if ws_mode and voice.provider != "elevenlabs":
            ws_mode = False

        # Runtime guard: if format is non-MP3, force HTTP (browser can't decode non-MP3 WS frames)
        if ws_mode and not profile.output_format.startswith("mp3"):
            logger.warning("WS mode requested but format %s is not MP3, forcing HTTP", profile.output_format)
            ws_mode = False

        hash_digest = get_tts_cache_digest(text, voice, profile)
        file_path, full_path_opus = get_file_path(hash_digest)

        logger.debug(f"full_path_opus: {full_path_opus}")

        # --- Cache hit ---
        if os.path.exists(full_path_opus):
            try:
                os.utime(full_path_opus, None)  # touch mtime to keep alive for cache cleanup
            except OSError:
                pass
            logger.debug(f"Cached audio found at: {full_path_opus}")
            try:
                if billing_adapter is not None and not sample_voice_id:
                    await billing_adapter.cache_hit(
                        provider=voice.provider,
                        characters=characters_used,
                        cache_key=hash_digest,
                    )
                if is_whatsapp or not websocket:
                    return full_path_opus, None
                else:
                    await send_cached_audio(websocket, full_path_opus)
                    return full_path_opus, None
            except FileNotFoundError:
                logger.debug("Cache file vanished during serve, regenerating: %s", full_path_opus)

        # --- Cache miss (or vanished file) — generate ---
        os.makedirs(file_path, exist_ok=True)

        if not sample_voice_id:
            chunks = await insert_tts_break(text)
            if not chunks:
                error_message = "No audio content could be generated."
                logger.error(error_message)
                return None, error_message
        else:
            chunks = [text]

        logger.debug(f"Number of text chunks: {len(chunks)}")

        preselected_elevenlabs_key = None
        if voice.provider == "elevenlabs":
            try:
                preselected_elevenlabs_key = await _select_elevenlabs_key(
                    voice.voice_code
                )
            except Exception as exc:
                logger.error(
                    "ElevenLabs key selection failed before billing (%s)",
                    type(exc).__name__,
                )
            if not preselected_elevenlabs_key:
                error_message = (
                    "No valid ElevenLabs API key is available for the selected voice."
                )
                logger.error(error_message)
                return None, error_message

        try:
            # Charge before generation — atomic transaction
            if not sample_voice_id:
                if billing_adapter is not None:
                    try:
                        billing_token = await billing_adapter.reserve(
                            provider=voice.provider,
                            characters=characters_used,
                        )
                    except TTSBillingInsufficientBalance:
                        if websocket and not is_whatsapp:
                            await manager.send_json(
                                websocket, {'action': 'insufficient-balance'}
                            )
                        return None, 'insufficient-balance'
                else:
                    charged = await cost_tts(
                        current_user.id,
                        characters_used,
                        provider=voice.provider,
                    )
                    if not charged:
                        if is_whatsapp:
                            return None, 'insufficient-balance'
                        if websocket:
                            await manager.send_json(websocket, {'action': 'insufficient-balance'})
                        return None, 'insufficient-balance'

            # --- Generator selection ---
            if ws_mode:
                audio_generator = elevenlabs_ws_generator(
                    voice.voice_code, chunks,
                    model_id=profile.model_id,
                    output_format=profile.output_format,
                    stability=profile.stability,
                    similarity_boost=profile.similarity_boost,
                    chunk_schedule=profile.chunk_schedule,
                    resolved_voice=voice,
                    elevenlabs_key=preselected_elevenlabs_key,
                )
                audio_input_format = format_to_pydub(profile.output_format)
            else:
                if voice.provider == "elevenlabs":
                    audio_generator = get_tts_generator_for_voice(
                        voice,
                        chunks,
                        profile=profile,
                        elevenlabs_key=preselected_elevenlabs_key,
                    )
                else:
                    audio_generator = get_tts_generator_for_voice(
                        voice,
                        chunks,
                        profile=profile,
                    )
                audio_input_format = format_to_pydub(profile.output_format) if voice.provider == 'elevenlabs' else 'mp3'
            audio_chunks = []

            if billing_token is not None:
                claimed = await billing_adapter.provider_started(billing_token)
                if not claimed:
                    # Another renderer won the durable component claim.  It
                    # exclusively owns settlement/failure; this loser must not
                    # call the provider or refund the winner's reservation.
                    billing_token = None
                    raise TTSBillingError(
                        "TTS provider work was already claimed"
                    )
                billing_provider_started = True

            async for audio_chunk in audio_generator:
                if websocket and not is_whatsapp:
                    await manager.send_bytes(websocket, audio_chunk)
                audio_chunks.append(audio_chunk)

            if audio_chunks:
                if billing_token is not None:
                    await billing_adapter.settle(billing_token)
                    billing_token = None
                encoded_audio = await asyncio.to_thread(
                    _decode_and_export_audio_chunks,
                    tuple(audio_chunks),
                    audio_input_format,
                )
                async with aiofiles.open(full_path_opus, "wb") as audio_file:
                    await audio_file.write(encoded_audio)
                logger.debug(f"Audio opus cached at: {full_path_opus}")

                if is_whatsapp or not websocket:
                    return full_path_opus, None
                else:
                    await manager.send_json(websocket, {'action': 'finished'})
                    return full_path_opus, None
            else:
                error_message = "No audio segments were generated."
                logger.error(error_message)
                if billing_token is not None:
                    await billing_adapter.failed(
                        billing_token,
                        provider_started=billing_provider_started,
                        reason="provider_returned_no_audio",
                    )
                    billing_token = None
                elif not sample_voice_id and billing_adapter is None:
                    if not await refund_tts(
                        current_user.id,
                        characters_used,
                        provider=voice.provider,
                    ):
                        logger.critical("REFUND FAILED user_id=%s chars=%d -- manual review needed",
                                        current_user.id, characters_used)
                return None, error_message

        except asyncio.CancelledError:
            if billing_token is not None:
                cleanup = asyncio.create_task(
                    billing_adapter.failed(
                        billing_token,
                        provider_started=billing_provider_started,
                        reason="tts_generation_cancelled",
                    )
                )
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                billing_token = None
            logger.debug("TTS cancelled by user for conversation %s", conversationId)
            raise
        except ValueError as ve:
            error_message = f"TTS generation failed: {str(ve)}"
            logger.error(error_message)
            if billing_token is not None:
                await billing_adapter.failed(
                    billing_token,
                    provider_started=billing_provider_started,
                    reason=f"tts_value_error:{type(ve).__name__}",
                )
                billing_token = None
            elif not sample_voice_id and billing_adapter is None:
                if not await refund_tts(
                    current_user.id,
                    characters_used,
                    provider=voice.provider,
                ):
                    logger.critical("REFUND FAILED user_id=%s chars=%d -- manual review needed",
                                    current_user.id, characters_used)
            return None, error_message
        except Exception as e:
            error_message = f"Unexpected error in TTS generation: {str(e)}"
            logger.error(error_message)
            if billing_token is not None:
                await billing_adapter.failed(
                    billing_token,
                    provider_started=billing_provider_started,
                    reason=f"tts_generation_error:{type(e).__name__}",
                )
                billing_token = None
            elif not sample_voice_id and billing_adapter is None:
                if not await refund_tts(
                    current_user.id,
                    characters_used,
                    provider=voice.provider,
                ):
                    logger.critical("REFUND FAILED user_id=%s chars=%d -- manual review needed",
                                    current_user.id, characters_used)
            return None, error_message

    except asyncio.CancelledError:
        _cancelled = True
        raise
    except Exception as e:
        error_message = f"Error in handle_tts_request: {str(e)}"
        logger.error(error_message)
        return None, error_message
    finally:
        if not _cancelled:
            _maybe_cleanup_cache()

