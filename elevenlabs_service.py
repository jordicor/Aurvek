"""Utility helpers to integrate ElevenLabs realtime call flows with Aurvek."""

import orjson
import os
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
import aiofiles

from common import custom_unescape, generate_user_hash, sanitize_name, users_directory
from database import get_db_connection, DB_MAX_RETRIES, DB_RETRY_DELAY_BASE, is_lock_error
from log_config import logger

# Import the load balancer to get valid API keys
try:
    from tools.tts_load_balancer import get_elevenlabs_key
except ImportError:
    # Fallback to common if load balancer not available
    from common import elevenlabs_key
    def get_elevenlabs_key():
        return elevenlabs_key

API_BASE_URL = os.getenv("ELEVENLABS_CONVAI_BASE", "https://api.elevenlabs.io/v1/convai")
USE_SIGNED_URL = os.getenv("ELEVENLABS_USE_SIGNED_URL", "true").lower() == "true"
CONTEXT_MESSAGE_LIMIT = int(os.getenv("ELEVENLABS_CONTEXT_LIMIT", "20"))  # Increased from 10 to 20
MAX_CONTEXT_CHARACTERS = int(os.getenv("ELEVENLABS_CONTEXT_MAX_CHARS", "8000"))  # Increased from 6000 to 8000
HTTP_TIMEOUT_SECONDS = float(os.getenv("ELEVENLABS_HTTP_TIMEOUT", "30"))


class ElevenLabsService:
    """Service layer that centralises ElevenLabs agent resolution and transcript handling."""

    def __init__(self) -> None:
        self.api_base_url = API_BASE_URL.rstrip("/")
        self.http_timeout = HTTP_TIMEOUT_SECONDS
        self.context_message_limit = CONTEXT_MESSAGE_LIMIT

    @staticmethod
    def is_configured() -> bool:
        api_key = get_elevenlabs_key()
        return bool(api_key)

    async def get_configuration(
        self,
        conversation_id: int,
        current_user_id: int,
        is_admin: bool,
    ) -> Optional[Dict[str, Any]]:
        async with get_db_connection(readonly=True) as conn:
            conversation = await self._load_conversation(conn, conversation_id)
            if not conversation:
                return None
            if not is_admin and conversation["user_id"] != current_user_id:
                return None

            agent_info = await self._resolve_agent(conn, conversation.get("role_id"))
            if not agent_info:
                logger.warning(
                    "No ElevenLabs agent configured for conversation %s (prompt_id=%s)",
                    conversation_id,
                    conversation.get("role_id"),
                )
                return None

            signed_url = await self._fetch_signed_url(agent_info["agent_id"]) if USE_SIGNED_URL else None
            recent_messages = await self._fetch_recent_messages(conn, conversation_id)

            # Watchdog: read pending hint (readonly, do NOT consume)
            watchdog_steering_hint = None
            watchdog_hint_eval_id = None
            prompt_id = conversation.get("role_id")
            if prompt_id is not None:
                cursor = await conn.execute(
                    """SELECT pending_hint, last_evaluated_message_id
                       FROM WATCHDOG_STATE
                       WHERE conversation_id = ? AND prompt_id = ?
                       AND pending_hint IS NOT NULL""",
                    (conversation_id, prompt_id),
                )
                ws_row = await cursor.fetchone()
                if ws_row and ws_row["pending_hint"]:
                    watchdog_steering_hint = ws_row["pending_hint"]
                    watchdog_hint_eval_id = ws_row["last_evaluated_message_id"]

        context_text = self._build_context_string(recent_messages)
        # The prompt from database will be sent as a dynamic variable
        # ElevenLabs agent template should have {{personality_template}} placeholder
        prompt_text = conversation.get("prompt_text") or ""

        # Debug logging
        logger.info("[ElevenLabs] Building configuration for conversation %s", conversation_id)
        logger.info("[ElevenLabs] User ID: %s", current_user_id)
        logger.info("[ElevenLabs] Recent messages count: %s", len(recent_messages))
        logger.info("[ElevenLabs] Context text length: %s", len(context_text))
        logger.info("[ElevenLabs] Context preview (first 500 chars): %s", context_text[:500] if context_text else "EMPTY")
        logger.info("[ElevenLabs] Prompt text length: %s", len(prompt_text))
        logger.info("[ElevenLabs] Prompt name: %s", conversation.get("prompt_name"))
        logger.info("[ElevenLabs] Prompt preview (first 300 chars): %s", prompt_text[:300] if prompt_text else "EMPTY")

        prompt_voice_code = conversation.get("prompt_voice_code")
        voice_id = agent_info.get("voice_id") or prompt_voice_code

        logger.info(
            "[ElevenLabs] Resolved voice_id for conversation %s (agent=%s, prompt=%s, final=%s)",
            conversation_id,
            agent_info.get("voice_id") or "NONE",
            prompt_voice_code or "NONE",
            voice_id or "NONE",
        )

        result = {
            "conversation_id": conversation_id,
            "conversation_name": conversation.get("chat_name"),
            "prompt_id": conversation.get("role_id"),
            "prompt_name": conversation.get("prompt_name"),
            "prompt_text": prompt_text,
            "agent_id": agent_info["agent_id"],
            "agent_name": agent_info.get("agent_name"),
            "voice_id": voice_id,
            "signed_url": signed_url,
            "context": context_text,
            "recent_messages": recent_messages,
            "status": conversation.get("elevenlabs_status"),
            "session_id": conversation.get("elevenlabs_session_id"),
            "user_id": current_user_id,
        }

        # Watchdog: include steering hint and CAS token if available
        if watchdog_steering_hint:
            result["watchdog_steering_hint"] = watchdog_steering_hint
            result["watchdog_hint_eval_id"] = watchdog_hint_eval_id

        return result

    async def validate_conversation_access(
        self,
        conversation_id: int,
        current_user_id: int,
        is_admin: bool,
    ) -> Optional[Dict[str, Any]]:
        async with get_db_connection(readonly=True) as conn:
            conversation = await self._load_conversation(conn, conversation_id)
        if not conversation:
            return None
        if not is_admin and conversation["user_id"] != current_user_id:
            return None
        return conversation

    async def mark_session_started(self, conversation_id: int, session_id: str) -> None:
        async with get_db_connection() as conn:
            await conn.execute(
                """
                UPDATE CONVERSATIONS
                SET elevenlabs_session_id = ?, elevenlabs_status = 'active'
                WHERE id = ?
                """,
                (session_id, conversation_id),
            )
            await conn.commit()
        logger.info(
            "[ElevenLabs] Session %s marked as active for conversation %s",
            session_id,
            conversation_id,
        )

    async def mark_session_status(
        self,
        conversation_id: int,
        session_id: str,
        status: str,
    ) -> None:
        async with get_db_connection() as conn:
            await conn.execute(
                """
                UPDATE CONVERSATIONS
                SET elevenlabs_session_id = ?, elevenlabs_status = ?
                WHERE id = ?
                """,
                (session_id, status, conversation_id),
            )
            await conn.commit()
        logger.info(
            "[ElevenLabs] Session %s updated to status '%s' for conversation %s",
            session_id,
            status,
            conversation_id,
        )

    async def check_conversation_status(self, session_id: str) -> Optional[str]:
        """Check the status of a conversation before fetching transcript."""
        if not session_id:
            return None

        url = f"{self.api_base_url}/conversations/{session_id}"
        api_key = get_elevenlabs_key()
        if not api_key:
            logger.error("[ElevenLabs] No valid API key available")
            return None
        headers = {"xi-api-key": api_key, "Accept": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=self.http_timeout) as client:
                response = await client.get(url, headers=headers)

                if response.status_code == 404:
                    logger.warning("[ElevenLabs] Conversation %s not found", session_id)
                    return None

                if response.status_code != 200:
                    logger.error("[ElevenLabs] Failed to check status: %s - %s", response.status_code, response.text)
                    return None

                data = response.json()
                status = data.get("status", "unknown").lower()

                logger.info("[ElevenLabs] Conversation %s status: %s", session_id, status)
                return status

        except Exception as exc:
            logger.error("[ElevenLabs] Error checking conversation status: %s", exc)
            return None

    async def fetch_full_transcript(self, session_id: str) -> List[Dict[str, Any]]:
        if not session_id:
            return []

        url = f"{self.api_base_url}/conversations/{session_id}"
        api_key = get_elevenlabs_key()
        if not api_key:
            logger.error("[ElevenLabs] No valid API key available")
            return None
        headers = {"xi-api-key": api_key, "Accept": "application/json"}

        logger.info("[ElevenLabs] Fetching transcript from URL: %s", url)
        logger.info("[ElevenLabs] Session ID: %s", session_id)
        logger.info("[ElevenLabs] API Base URL: %s", self.api_base_url)
        logger.info("[ElevenLabs] Using API key: %s...%s", api_key[:10] if api_key else "NONE", api_key[-4:] if api_key else "")

        async with httpx.AsyncClient(timeout=self.http_timeout) as client:
            response = await client.get(url, headers=headers)

            if response.status_code == 404:
                logger.error("[ElevenLabs] Conversation not found (404). This could mean:")
                logger.error("  1. The conversation ID doesn't exist")
                logger.error("  2. The API key doesn't have access to this conversation")
                logger.error("  3. The conversation belongs to a different agent")
                logger.error("[ElevenLabs] Full URL attempted: %s", url)
                logger.error("[ElevenLabs] Response: %s", response.text)
            elif response.status_code != 200:
                logger.error("[ElevenLabs] API error response: %s - %s", response.status_code, response.text)

            response.raise_for_status()
            payload = response.json()

        transcript = payload.get("transcript") or []
        if isinstance(transcript, list):
            return [turn for turn in transcript if isinstance(turn, dict)]
        return []

    async def save_transcript_to_db(
        self,
        conversation_id: int,
        session_id: str,
        user_id: int,
        transcript: List[Dict[str, Any]],
    ) -> tuple:
        """Save transcript to DB. Returns (saved_count, last_user_message_id, last_bot_message_id)."""
        if not transcript:
            await self.mark_session_status(conversation_id, session_id, "completed")
            return (0, None, None)

        base_time = datetime.now(timezone.utc)
        last_lock_error: Optional[Exception] = None

        for attempt in range(DB_MAX_RETRIES):
            retry_needed = False
            wait_time = 0.0
            saved_messages = 0
            last_user_message_id = None
            last_bot_message_id = None

            async with get_db_connection() as conn:
                transaction_started = False
                try:
                    await conn.execute("BEGIN IMMEDIATE")
                    transaction_started = True

                    for index, turn in enumerate(transcript):
                        message_text = self._extract_transcript_text(turn)
                        if not message_text:
                            continue

                        if len(message_text) > MAX_CONTEXT_CHARACTERS:
                            message_text = message_text[:MAX_CONTEXT_CHARACTERS]

                        message_role = self._map_transcript_role(turn.get("role"))
                        timestamp = (base_time + timedelta(seconds=index)).strftime("%Y-%m-%d %H:%M:%S.%f")

                        cursor = await conn.execute(
                            """
                            INSERT INTO MESSAGES (conversation_id, user_id, message, type, date)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (conversation_id, user_id, message_text, message_role, timestamp),
                        )
                        inserted_id = cursor.lastrowid
                        if message_role == "user":
                            last_user_message_id = inserted_id
                        else:
                            last_bot_message_id = inserted_id
                        saved_messages += 1

                    await conn.execute(
                        """
                        UPDATE CONVERSATIONS
                        SET elevenlabs_session_id = ?, elevenlabs_status = 'completed'
                        WHERE id = ?
                        """,
                        (session_id, conversation_id),
                    )
                    # Update conversation last_activity for sort ordering
                    await conn.execute("UPDATE CONVERSATIONS SET last_activity = CURRENT_TIMESTAMP WHERE id = ?", (conversation_id,))
                    await conn.commit()

                    logger.info(
                        "[ElevenLabs] Stored %s transcript turns for conversation %s",
                        saved_messages,
                        conversation_id,
                    )
                    return (saved_messages, last_user_message_id, last_bot_message_id)

                except sqlite3.OperationalError as exc:
                    if transaction_started:
                        try:
                            await conn.rollback()
                        except Exception:
                            pass
                    if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                        wait_time = DB_RETRY_DELAY_BASE * (attempt + 1)
                        logger.warning(
                            "[ElevenLabs] Database locked storing transcript for conversation %s "
                            "(attempt %s/%s). Retrying in %.2fs",
                            conversation_id,
                            attempt + 1,
                            DB_MAX_RETRIES,
                            wait_time,
                        )
                        last_lock_error = exc
                        retry_needed = True
                    else:
                        logger.exception(
                            "[ElevenLabs] Operational error persisting transcript for conversation %s: %s",
                            conversation_id,
                            exc,
                        )
                        raise
                except Exception as exc:
                    if transaction_started:
                        try:
                            await conn.rollback()
                        except Exception:
                            pass
                    logger.exception(
                        "[ElevenLabs] Failed to persist transcript for conversation %s: %s",
                        conversation_id,
                        exc,
                    )
                    raise

            if retry_needed:
                await asyncio.sleep(wait_time)
                continue
            break

        if last_lock_error:
            logger.error(
                "[ElevenLabs] Failed to persist transcript for conversation %s after %s attempts: %s",
                conversation_id,
                DB_MAX_RETRIES,
                last_lock_error,
            )
            raise last_lock_error

        raise RuntimeError(
            f"[ElevenLabs] Failed to store transcript for conversation {conversation_id} due to unknown error"
        )

    async def download_conversation_audio(
        self,
        conversation_id: int,
        session_id: str,
        user_id: int,
    ) -> Optional[str]:
        session_id = (session_id or '').strip()
        if not session_id:
            logger.warning('[ElevenLabs] Cannot download audio without a session id for conversation %s', conversation_id)
            return None

        api_key = get_elevenlabs_key()
        if not api_key:
            logger.error('[ElevenLabs] No valid API key available for audio download')
            return None

        async with get_db_connection(readonly=True) as conn:
            conversation = await self._load_conversation(conn, conversation_id)

        if not conversation:
            logger.warning('[ElevenLabs] Conversation %s not found while preparing audio download', conversation_id)
            return None

        if conversation.get('user_id') != user_id:
            logger.warning('[ElevenLabs] Conversation %s does not belong to user %s', conversation_id, user_id)
            return None

        username = conversation.get('user_username')
        if not username:
            logger.warning('[ElevenLabs] Username not available for conversation %s', conversation_id)
            return None

        hash_prefix1, hash_prefix2, user_hash = generate_user_hash(username)
        conversation_id_str = f"{conversation_id:07d}"
        wav_directory = os.path.join(
            users_directory,
            hash_prefix1,
            hash_prefix2,
            user_hash,
            'files',
            conversation_id_str[:3],
            conversation_id_str[3:],
            'wav',
        )
        os.makedirs(wav_directory, exist_ok=True)

        short_session = session_id.replace('-', '')[:8] or 'session'
        base_name_source = conversation.get('chat_name') or conversation.get('prompt_name') or f'conversation_{conversation_id}'
        safe_base_name = sanitize_name(base_name_source)
        if not safe_base_name:
            safe_base_name = f'conversation_{conversation_id}'
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        filename = f"{safe_base_name}_{short_session}_{timestamp}.wav"
        file_path = os.path.join(wav_directory, filename)

        try:
            existing_files = [name for name in os.listdir(wav_directory) if name.endswith('.wav') and short_session in name]
        except FileNotFoundError:
            existing_files = []
        if existing_files:
            logger.info('[ElevenLabs] Audio for session %s already saved as %s', session_id, existing_files[0])
            return os.path.join(wav_directory, existing_files[0])

        audio_url = f"{self.api_base_url}/conversations/{session_id}/audio"
        headers = {'xi-api-key': api_key, 'Accept': 'audio/wav'}
        params = {'format': 'wav'}
        bytes_written = 0

        try:
            async with httpx.AsyncClient(timeout=self.http_timeout) as client:
                async with client.stream('GET', audio_url, headers=headers, params=params) as response:
                    if response.status_code == 404:
                        logger.warning('[ElevenLabs] Audio stream not available for session %s', session_id)
                        return None
                    response.raise_for_status()

                    async with aiofiles.open(file_path, 'wb') as file_obj:
                        async for chunk in response.aiter_bytes():
                            if not chunk:
                                continue
                            await file_obj.write(chunk)
                            bytes_written += len(chunk)
        except httpx.HTTPStatusError as exc:
            logger.error('[ElevenLabs] Failed to download audio for session %s: %s', session_id, exc)
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    logger.warning('[ElevenLabs] Could not remove partial audio file at %s', file_path)
            return None
        except httpx.HTTPError as exc:
            logger.error('[ElevenLabs] HTTP error while downloading audio for session %s: %s', session_id, exc)
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    logger.warning('[ElevenLabs] Could not remove partial audio file at %s', file_path)
            return None
        except Exception as exc:
            logger.exception('[ElevenLabs] Unexpected error while saving audio for conversation %s: %s', conversation_id, exc)
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    logger.warning('[ElevenLabs] Could not remove partial audio file at %s', file_path)
            return None

        if bytes_written == 0:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    logger.warning('[ElevenLabs] Could not remove empty audio file at %s', file_path)
            logger.warning('[ElevenLabs] Empty audio response for session %s', session_id)
            return None

        relative_path = os.path.relpath(file_path, start=users_directory)
        logger.info('[ElevenLabs] Saved conversation audio to %s (%d bytes)', relative_path, bytes_written)
        return file_path


    async def _load_conversation(
        self,
        conn,
        conversation_id: int,
    ) -> Optional[Dict[str, Any]]:
        cursor = await conn.execute(
            """
            SELECT
                c.id,
                c.user_id,
                c.role_id,
                c.chat_name,
                c.locked,
                c.elevenlabs_session_id,
                c.elevenlabs_status,
                p.prompt AS prompt_text,
                p.name AS prompt_name,
                p.description AS prompt_description,
                p.voice_id AS prompt_voice_fk,
                v.voice_code AS prompt_voice_code,
                u.username AS user_username
            FROM CONVERSATIONS c
            JOIN USERS u ON u.id = c.user_id
            LEFT JOIN PROMPTS p ON p.id = c.role_id
            LEFT JOIN VOICES v ON v.id = p.voice_id
            WHERE c.id = ?
            """,
            (conversation_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def _resolve_agent(
        self,
        conn,
        prompt_id: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        if prompt_id is not None:
            cursor = await conn.execute(
                """
                SELECT m.agent_id, m.voice_id, a.agent_name
                FROM PROMPT_AGENT_MAPPING m
                LEFT JOIN ELEVENLABS_AGENTS a ON a.agent_id = m.agent_id
                WHERE m.prompt_id = ?
                """,
                (prompt_id,),
            )
            mapping_row = await cursor.fetchone()
            if mapping_row:
                return {
                    "agent_id": mapping_row["agent_id"],
                    "voice_id": mapping_row["voice_id"],
                    "agent_name": mapping_row["agent_name"],
                }

        cursor = await conn.execute(
            """
            SELECT agent_id, agent_name
            FROM ELEVENLABS_AGENTS
            WHERE is_default = 1
            ORDER BY id ASC
            LIMIT 1
            """,
        )
        default_row = await cursor.fetchone()
        if default_row:
            return {
                "agent_id": default_row["agent_id"],
                "agent_name": default_row["agent_name"],
                "voice_id": None,
            }
        return None

    async def _fetch_signed_url(
        self, agent_id: str,
    ) -> Optional[str]:
        if not agent_id or not USE_SIGNED_URL:
            return None

        url = f"{self.api_base_url}/conversation/get-signed-url"
        api_key = get_elevenlabs_key()
        if not api_key:
            logger.error("[ElevenLabs] No valid API key available for signed URL")
            return None
        headers = {"xi-api-key": api_key, "Accept": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=self.http_timeout) as client:
                response = await client.get(url, headers=headers, params={"agent_id": agent_id})
                response.raise_for_status()
                payload = response.json()
                return payload.get("signed_url") or payload.get("url")
        except httpx.HTTPError as exc:
            logger.warning("[ElevenLabs] Unable to fetch signed URL for agent %s: %s", agent_id, exc)
        except Exception as exc:
            logger.warning("[ElevenLabs] Unexpected error fetching signed URL for agent %s: %s", agent_id, exc)
        return None


    async def _fetch_recent_messages(
        self,
        conn,
        conversation_id: int,
    ) -> List[Dict[str, str]]:
        cursor = await conn.execute(
            """
            SELECT message, type
            FROM MESSAGES
            WHERE conversation_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (conversation_id, self.context_message_limit),
        )
        rows = await cursor.fetchall()
        if not rows:
            return []

        messages: List[Dict[str, str]] = []
        for row in reversed(rows):
            text = self._render_message_text(row["message"])
            if not text:
                continue
            role = "assistant" if (row["type"] or "").lower() == "bot" else "user"
            messages.append({"role": role, "text": text})
        return messages

    @staticmethod
    def _render_message_text(raw_message: str) -> str:
        if not raw_message:
            return ""
        raw_message = raw_message.strip()
        if not raw_message:
            return ""

        try:
            parsed = orjson.loads(raw_message)
        except (orjson.JSONDecodeError, TypeError):
            return custom_unescape(raw_message)

        if isinstance(parsed, list):
            parts = []
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                kind = (item.get("type") or "").lower()
                if kind == "text":
                    parts.append(item.get("text", ""))
                elif kind == "image_url":
                    parts.append("[image]")
                elif kind == "video_url":
                    parts.append("[video]")
                elif "text" in item:
                    parts.append(str(item.get("text")))
            return custom_unescape(" ".join(part for part in parts if part).strip())

        if isinstance(parsed, dict):
            if "text" in parsed:
                return custom_unescape(str(parsed.get("text", "")))
            if "content" in parsed:
                return custom_unescape(str(parsed.get("content", "")))

        return custom_unescape(str(parsed))

    @staticmethod
    def _build_context_string(recent_messages: List[Dict[str, str]]) -> str:
        if not recent_messages:
            logger.info("[ElevenLabs] No recent messages for context")
            return ""

        # Format context exactly like ConvAI does
        lines = []
        for msg in recent_messages:
            if not msg.get("text"):
                continue
            # Use lowercase roles exactly as ConvAI does
            role = "user" if msg["role"] == "user" else "assistant"
            text = msg["text"].strip().replace('\n', ' ')  # ConvAI replaces newlines with spaces
            lines.append(f"{role}: {text}")

        if not lines:
            logger.info("[ElevenLabs] No valid messages for context after filtering")
            return ""

        # Join messages with newlines, exactly like ConvAI
        context = "\n".join(lines)

        # Add the exact same system message that ConvAI uses
        context += "\nuser: (system: user starts call to continue conversation)"

        logger.info("[ElevenLabs] Context built with %d message turns", len(lines))
        logger.info("[ElevenLabs] Context sample: %s", context[:200] + "..." if len(context) > 200 else context)

        if len(context) > MAX_CONTEXT_CHARACTERS:
            # If too long, try to keep the most recent messages (like ConvAI does)
            truncated = context[-MAX_CONTEXT_CHARACTERS:]
            logger.info("[ElevenLabs] Context truncated from %d to %d chars", len(context), len(truncated))
            return truncated

        return context

    @staticmethod
    def _extract_transcript_text(turn: Dict[str, Any]) -> str:
        if not turn:
            return ""
        message = turn.get("message")
        if isinstance(message, dict):
            message = message.get("text") or message.get("content")
        if message is None:
            message = turn.get("text")
        if message is None and "segments" in turn:
            segments = turn.get("segments")
            if isinstance(segments, list):
                combined = " ".join(str(seg.get("text", "")) for seg in segments if isinstance(seg, dict))
                message = combined
        if message is None:
            return ""
        return custom_unescape(str(message).strip())

    @staticmethod
    def _map_transcript_role(role: Optional[str]) -> str:
        if not role:
            return "bot"
        role_lower = role.lower()
        if role_lower in {"user", "customer", "client", "speaker", "human", "caller"}:
            return "user"
        return "bot"


service = ElevenLabsService()
