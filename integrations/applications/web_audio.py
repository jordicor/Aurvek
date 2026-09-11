"""Scoped web dictation, message playback and private original recordings.

STT/TTS and storage remain native services. These routes supply the application
principal and funding which the native global media endpoints do not carry.
"""
from __future__ import annotations

import asyncio
import io
import json
from urllib.parse import urlencode

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from database import get_db_connection
from integrations.embed.context import get_embed_principal
from integrations.embed.models import EmbedError
from .runtime import authorize_application_read

router = APIRouter()
MAX_DICTATION_BYTES = 20 * 1024 * 1024
MAX_DICTATION_SECONDS = 300


async def audio_scope(connection, conversation_id, user_id, capability, *, mutation=False):
    scope = await authorize_application_read(connection, conversation_id, user_id)
    if scope is None or not scope.capabilities.get(capability):
        raise EmbedError("application_capability_denied", 403)
    cursor = await connection.execute(
        "SELECT locked,is_incognito FROM CONVERSATIONS WHERE id=? AND user_id=?",
        (conversation_id, user_id))
    row = await cursor.fetchone()
    if not row or row["is_incognito"] or (mutation and row["locked"]):
        raise EmbedError("conversation_unavailable", 403)
    return scope


async def _audio_request(request, conversation_id, capability, *, mutation=False):
    from auth import get_current_user
    from common import READONLY_MODE
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(401, "unauthenticated")
    if mutation:
        if READONLY_MODE:
            raise HTTPException(503, "read_only")
        from request_security import validate_mutation_request
        if validate_mutation_request(request) is not None:
            raise HTTPException(403, "unauthenticated")
    principal = get_embed_principal(request)
    if principal is not None and principal.conversation_id != conversation_id:
        raise HTTPException(404, "not_found")
    async with get_db_connection(readonly=True) as connection:
        try:
            scope = await audio_scope(connection, conversation_id, user.id, capability, mutation=mutation)
        except EmbedError as exc:
            raise HTTPException(exc.status_code, exc.code) from exc
    return user, scope, principal


@router.post("/api/conversations/{conversation_id}/voice/transcribe")
async def transcribe_dictation(conversation_id: int, request: Request, audio: UploadFile = File(...)):
    from chat.routes.voice_io import _decode_audio_duration
    from integrations.media import transcribe_external_audio_detailed
    from .browser_voice import _voice_operation
    user, scope, principal = await _audio_request(request, conversation_id, "stt", mutation=True)
    content = await audio.read(MAX_DICTATION_BYTES + 1)
    if not content or len(content) > MAX_DICTATION_BYTES:
        raise HTTPException(413, "audio_too_large")
    audio_format = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4",
        "audio/wav": "wav", "audio/x-wav": "wav"}.get((audio.content_type or "").split(";", 1)[0])
    if audio_format is None:
        raise HTTPException(400, "invalid_audio")
    try:
        # MediaRecorder WebM often has no duration metadata. Native decoding
        # derives samples rather than trusting the browser's reported duration.
        duration = await asyncio.to_thread(_decode_audio_duration, content, audio_format=audio_format)
    except Exception as exc:
        raise HTTPException(400, "invalid_audio") from exc
    if not 0 < duration <= MAX_DICTATION_SECONDS:
        raise HTTPException(400, "audio_duration_invalid")
    try:
        async with _voice_operation(scope, principal):
            result = await transcribe_external_audio_detailed(user_id=user.id, audio_content=content,
                duration_seconds=duration, user_agent=request.headers.get("user-agent"))
    except EmbedError as exc:
        raise HTTPException(exc.status_code, exc.code) from exc
    # Dictation remains editable text; no claim of a retained/sent voice note.
    return {"prompt": result.text}


class _PlaybackAccess:
    def __init__(self, request, conversation_id, user):
        self.request, self.conversation_id, self.user = request, conversation_id, user

    async def check_access(self):
        await _audio_request(self.request, self.conversation_id, "tts", mutation=True)


@router.post("/api/conversations/{conversation_id}/voice/tts")
async def render_message_audio(conversation_id: int, request: Request):
    from ai_runtime.reasoning_tags import strip_tagged_thinking_prefix
    from common import custom_unescape
    from integrations.messaging_voice_notes.service import extract_message_text
    from tools.tts import handle_tts_request
    from .browser_voice import VoiceTTSBilling, _voice_operation
    user, scope, principal = await _audio_request(request, conversation_id, "tts", mutation=True)
    data = await request.json()
    message_id = data.get("message_id") if isinstance(data, dict) else None
    if type(message_id) is not int or message_id <= 0:
        raise HTTPException(400, "invalid_message")
    async with get_db_connection(readonly=True) as connection:
        cursor = await connection.execute(
            "SELECT message,type FROM MESSAGES WHERE id=? AND conversation_id=? AND user_id=?",
            (message_id, conversation_id, user.id))
        message = await cursor.fetchone()
    if message is None or message["type"] not in {"bot", "user"}:
        raise HTTPException(404, "not_found")
    stored_text = custom_unescape(message["message"])
    try:
        parsed = json.loads(stored_text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and parsed.get("multi_ai"):
        responses = parsed.get("responses") or []
        llm_id = data.get("llm_id")
        if llm_id is not None and type(llm_id) is not int:
            raise HTTPException(400, "invalid_message")
        selected = next((item for item in responses if isinstance(item, dict)
            and (llm_id is None or item.get("llm_id") == llm_id)), None)
        if selected is None or selected.get("error"):
            raise HTTPException(404, "not_found")
        stored_text = selected.get("content")
    text = extract_message_text(stored_text)
    if message["type"] == "bot":
        text = strip_tagged_thinking_prefix(text) or ""
    if not text.strip():
        raise HTTPException(400, "message_has_no_text")
    try:
        async with _voice_operation(scope, principal):
            path, error = await handle_tts_request(None,
                {"text": text, "author": message["type"], "conversationId": conversation_id}, user,
                tts_context="webchat", billing_adapter=VoiceTTSBilling(_PlaybackAccess(request, conversation_id, user)))
    except EmbedError as exc:
        raise HTTPException(exc.status_code, exc.code) from exc
    if error or not path:
        raise HTTPException(503, "audio_unavailable")
    return FileResponse(path, media_type="audio/ogg", headers={"Cache-Control": "private, no-store"})


async def load_original_recordings(connection, scope, message_ids):
    """One batch for the current history page, using its authorized scope."""
    if scope is None or not scope.capabilities.get("voice") or not message_ids:
        return {}
    placeholders = ",".join("?" for _ in message_ids)
    cursor = await connection.execute(
        f"""SELECT a.public_id,a.message_id,b.mime_detected,m.type FROM FILE_ATTACHMENTS a
        JOIN FILE_BLOBS b ON b.id=a.blob_id JOIN MESSAGES m ON m.id=a.message_id
        WHERE a.conversation_id=? AND a.user_id=? AND m.conversation_id=a.conversation_id
          AND a.attachment_type='audio' AND a.status='active' AND b.status='ready'
          AND a.message_id IN ({placeholders}) ORDER BY a.id""",
        (scope.conversation_id, scope.user_id, *message_ids))
    return {int(row["message_id"]): {
        "original_audio_url": f"/api/conversations/{scope.conversation_id}/voice/audio?"
            + urlencode({"attachment_ref": row["public_id"]}),
        "original_audio_mime": row["mime_detected"],
    } for row in await cursor.fetchall()
        if scope.capabilities.get("stt" if row["type"] == "user" else "tts")}


@router.get("/api/conversations/{conversation_id}/voice/audio")
async def read_recording(conversation_id: int, request: Request, attachment_ref: str):
    from file_storage import get_attachment_path_for_user
    user, scope, _ = await _audio_request(request, conversation_id, "voice")
    async with get_db_connection(readonly=True) as connection:
        # Check both the conversation and message before resolving any disk path.
        cursor = await connection.execute(
            """SELECT m.type FROM FILE_ATTACHMENTS a JOIN MESSAGES m ON m.id=a.message_id
            WHERE a.public_id=? AND a.conversation_id=? AND a.user_id=?
              AND m.conversation_id=a.conversation_id AND a.status='active'""",
            (attachment_ref, conversation_id, user.id))
        row = await cursor.fetchone()
        if not row or not scope.capabilities.get("stt" if row["type"] == "user" else "tts"):
            raise HTTPException(404, "not_found")
        result = await get_attachment_path_for_user(connection, public_id=attachment_ref,
            user_id=user.id, require_kind="audio")
    if not result:
        raise HTTPException(404, "not_found")
    path, metadata = result
    return FileResponse(path, media_type=metadata["mime_detected"],
        headers={"Cache-Control": "private, no-store"})


class VoiceRecording:
    """Originals for one canonical turn, never for the entire handoff session."""
    def __init__(self, conversation_id, user_id):
        self.conversation_id, self.user_id = conversation_id, user_id
        self.input_ref = None
        self.reply_ref = None
        self.played_segments = []
        self.retention_failed = False

    async def _retain(self, audio, filename, mime):
        from file_storage import create_pending_audio_attachment
        from log_config import logger
        try:
            pending = await create_pending_audio_attachment(user_id=self.user_id,
                conversation_id=self.conversation_id, data=audio, filename=filename, mime_detected=mime)
            return pending.public_id
        except Exception as exc:
            # Storage failure does not erase a paid transcript or interrupt the
            # call. The caller receives an explicit retention notice below.
            self.retention_failed = True
            logger.warning("Browser voice recording unavailable (%s)", type(exc).__name__)

    async def retain_input(self, audio):
        self.input_ref = await self._retain(audio, "browser-voice-input.wav", "audio/wav")

    async def retain_reply(self):
        if self.reply_ref or not self.played_segments:
            return
        if len(self.played_segments) == 1:
            self.reply_ref = await self._retain(self.played_segments.pop(),
                "browser-voice-reply.ogg", "audio/ogg")
            return
        from pydub import AudioSegment
        def combine():
            audio = AudioSegment.empty()
            for segment in self.played_segments:
                audio += AudioSegment.from_file(io.BytesIO(segment), format="ogg")
            output = io.BytesIO()
            audio.export(output, format="wav")
            return output.getvalue()
        try:
            self.reply_ref = await self._retain(await asyncio.to_thread(combine),
                "browser-voice-reply.wav", "audio/wav")
        except Exception as exc:
            from log_config import logger
            self.retention_failed = True
            logger.warning("Browser voice recording assembly failed (%s)", type(exc).__name__)
        finally:
            self.played_segments.clear()

    async def commit(self, commit, connection):
        from file_storage import finalize_pending_attachment
        for reference, message_id in ((self.input_ref, commit.user_message_id),
                                      (self.reply_ref, commit.assistant_message_id)):
            if reference and message_id:
                await finalize_pending_attachment(connection, public_id=reference, message_id=message_id,
                    conversation_id=self.conversation_id, user_id=self.user_id, require_kind="audio")

    async def discard_pending(self):
        from file_storage import discard_pending_attachments
        references = [reference for reference in (self.input_ref, self.reply_ref) if reference]
        if references:
            await discard_pending_attachments(references, "browser_voice_uncommitted")
