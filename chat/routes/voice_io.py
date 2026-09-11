import asyncio
import io
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import jwt
import orjson
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect, Form
from fastapi.responses import FileResponse, JSONResponse, Response
from pydub import AudioSegment

from auth import (
    get_current_user,
    get_current_user_from_websocket,
    get_user_by_username,
    unauthenticated_response,
)
from captcha_service import get_captcha_config
from clients import stt_engine, stt_fallback_enabled
from common import (
    GOOGLE_CLIENT_ID,
    READONLY_MODE,
    SECRET_KEY,
    cache_directory,
    decode_jwt_cached,
    templates,
    validate_path_within_directory,
)
from database import get_db_connection
from integrations.media import (
    finalize_failed_stt_attempt,
    reserve_stt_attempt,
    settle_stt_attempt,
    transcribe_with_deepgram as transcribe_media_with_deepgram,
    transcribe_with_elevenlabs as transcribe_media_with_elevenlabs,
)
from storage_quota import StorageQuotaExceededError, ensure_generation_headroom
from log_config import logger
from models import ConnectionManager, User
from rediscfg import redis_client
from tasks import generate_mp3_task, generate_pdf_task
from i18n import Translator, get_translator
from tools.tts import (
    get_file_path,
    get_tts_cache_digest,
    get_voice_from_conversation,
    handle_tts_request,
    process_text_for_tts,
    resolve_playback_voice,
)
from tools.tts_config import get_tts_profile
from user_languages import load_user_preferred_languages

from chat.services.localization import chat_text, chat_error

router = APIRouter()
manager = ConnectionManager()
DEFAULT_STT_LANGUAGE = "es"


class _LocalizedSTTError(HTTPException):
    """An STT error whose detail is already safe for the native chat UI."""


def _localized_stt_error(exc: HTTPException, translator, key: str) -> _LocalizedSTTError:
    return _LocalizedSTTError(
        status_code=exc.status_code,
        detail=chat_text(translator, key),
        headers=exc.headers,
    )


async def _load_primary_stt_language(user_id: int | None, application=None) -> str | None:
    """Return the user's primary language, or None for provider autodetection."""
    if user_id is None:
        return None
    async with get_db_connection(readonly=True) as conn:
        if application is not None:
            from integrations.applications.profile import load_profile
            preferred_languages = (await load_profile(conn, application.app_id, application.subject)).preferred_languages
        else:
            preferred_languages = await load_user_preferred_languages(conn, user_id)
    return preferred_languages[0] if preferred_languages else None


async def require_conversation_access(conversation_id: int, current_user: User) -> int:
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT user_id FROM CONVERSATIONS WHERE id = ?",
            (conversation_id,),
        )
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=chat_text(current_user, "conversation_not_found"))

    owner_id = int(row["user_id"])
    if owner_id != int(current_user.id) and not await current_user.is_admin:
        raise HTTPException(status_code=403, detail=chat_text(current_user, "access_denied"))
    return owner_id


async def require_native_export(conversation_id: int, current_user: User) -> int:
    owner_id = await require_conversation_access(conversation_id, current_user)
    # Application exports require a scoped POST with funding and private result
    # delivery. Their legacy global GET routes cannot supply that context.
    from integrations.applications.runtime import authorize_application_read
    from integrations.embed.models import EmbedError
    try:
        async with get_db_connection(readonly=True) as conn:
            application = await authorize_application_read(conn, conversation_id, owner_id)
    except EmbedError as exc:
        raise HTTPException(exc.status_code, exc.code) from exc
    if application is not None:
        raise HTTPException(403, "application_export_route_required")
    return owner_id


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        current_user = await get_current_user_from_websocket(websocket)
        if current_user is None:
            await websocket.close(code=4401, reason="Session expired")
            return

        if READONLY_MODE:
            await websocket.close(code=1013, reason="Read-only mode active")
            return

        while True:
            message = await websocket.receive_text()
            current_user = await get_current_user_from_websocket(websocket)
            if current_user is None:
                await websocket.close(code=4401, reason="Session expired")
                return
            try:
                data = orjson.loads(message)
            except orjson.JSONDecodeError:
                await manager.send_json(
                    websocket,
                    {"action": "error", "error": chat_text(current_user, "invalid_json")},
                )
                continue
            if not isinstance(data, dict):
                await manager.send_json(
                    websocket,
                    {"action": "error", "error": chat_text(current_user, "invalid_json")},
                )
                continue
            action = data.get("action")

            if action == "start_tts":
                if manager.active_connections[websocket]["task"]:
                    manager.active_connections[websocket]["task"].cancel()
                task = asyncio.create_task(
                    handle_tts_request(websocket, data, current_user, tts_context="webchat")
                )
                manager.active_connections[websocket]["task"] = task

            elif action == "start_tts_ws":
                if manager.active_connections[websocket]["task"]:
                    manager.active_connections[websocket]["task"].cancel()
                task = asyncio.create_task(
                    handle_tts_request(
                        websocket,
                        data,
                        current_user,
                        ws_mode=True,
                        tts_context="webchat",
                    )
                )
                manager.active_connections[websocket]["task"] = task

            elif action == "stop":
                if manager.active_connections[websocket]["task"]:
                    manager.active_connections[websocket]["task"].cancel()
                await manager.send_json(websocket, {"action": "stopped"})

    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)


@router.post("/api/get-tts-audio")
async def get_tts_audio_endpoint(request: Request, current_user: User = Depends(get_current_user)):
    data = await request.json()
    text = data.get("text")
    conversation_id = data.get("conversationId")
    author = data.get("author", "bot")

    if conversation_id is None:
        return JSONResponse(status_code=400, content={"error": chat_text(current_user, "conversation_id_required")})

    try:
        if author == "user":
            voice = await resolve_playback_voice(current_user.voice_code)
        elif author == "bot":
            voice = await get_voice_from_conversation(conversation_id, current_user)
        else:
            voice = await resolve_playback_voice(None)

        text_processed = process_text_for_tts(text)
        profile = await get_tts_profile("webchat")
        hash_digest = get_tts_cache_digest(text_processed, voice, profile)
        _, full_path_opus = get_file_path(hash_digest)

        if os.path.exists(full_path_opus):
            return FileResponse(full_path_opus, media_type="audio/ogg")
        return Response(status_code=204)
    except ValueError as exc:
        logger.warning("TTS audio lookup failed: %s", exc)
        return Response(status_code=204)
    except Exception as exc:
        logger.error("Error in get_tts_audio_endpoint: %s", exc)
        return JSONResponse(status_code=500, content={"error": chat_text(current_user, "request_failed")})


@router.get("/download-pdf/{conversation_id}")
async def initiate_download_pdf(
    conversation_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        logger.warning("User not authenticated attempted to access /download-pdf")
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "captcha": get_captcha_config(),
                "google_oauth_available": bool(GOOGLE_CLIENT_ID),
            },
        )

    try:
        is_user_admin = await current_user.is_admin
    except Exception as exc:
        logger.error("Error verifying if user is admin: %s", exc)
        raise HTTPException(status_code=500, detail=chat_text(current_user, "permission_check_failed"))

    owner_id = await require_native_export(conversation_id, current_user)

    # Storage-quota soft pre-check: exports charge the conversation OWNER's
    # quota (an admin may export another user's conversation, but the file lands
    # under and is ledgered against the owner). Runs before enqueueing the task.
    try:
        async with get_db_connection(readonly=True) as conn:
            await ensure_generation_headroom(conn, owner_id)
    except StorageQuotaExceededError as exc:
        raise HTTPException(status_code=413, detail=chat_text(current_user, "storage_quota_exceeded"))

    lock_key = f"pdf_lock:{conversation_id}"
    try:
        lock_acquired = await redis_client.set(lock_key, "locked", nx=True, ex=300)
        if not lock_acquired:
            return JSONResponse(content={"message": chat_text(current_user, "pdf_generation_busy")})

        generate_pdf_task.send(conversation_id=conversation_id, user_id=current_user.id, is_admin=is_user_admin,
                               ui_language=get_translator(request, current_user).language)
        return JSONResponse(content={"message": chat_text(current_user, "pdf_generation_started")})
    except Exception as exc:
        logger.error("Error trying to generate PDF for conversation_id %s: %s", conversation_id, exc)
        raise HTTPException(status_code=500, detail=chat_text(current_user, "request_failed"))


@router.get("/download-mp3/{conversation_id}")
async def initiate_download_mp3(
    conversation_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        logger.warning("User not authenticated attempted to access /download-mp3")
        return unauthenticated_response()

    try:
        is_user_admin = await current_user.is_admin
    except Exception as exc:
        logger.error("Error verifying if user is admin: %s", exc)
        raise HTTPException(status_code=500, detail=chat_text(current_user, "permission_check_failed"))

    owner_id = await require_native_export(conversation_id, current_user)

    # Storage-quota soft pre-check: exports charge the conversation OWNER's
    # quota (an admin may export another user's conversation, but the file lands
    # under and is ledgered against the owner). Runs before enqueueing the task.
    try:
        async with get_db_connection(readonly=True) as conn:
            await ensure_generation_headroom(conn, owner_id)
    except StorageQuotaExceededError as exc:
        raise HTTPException(status_code=413, detail=chat_text(current_user, "storage_quota_exceeded"))

    lock_key = f"mp3_lock:{conversation_id}:{current_user.id}"
    try:
        lock_acquired = await redis_client.set(lock_key, "locked", nx=True, ex=300)
        if not lock_acquired:
            return JSONResponse(content={"message": chat_text(current_user, "mp3_generation_busy")})

        generate_mp3_task.send(conversation_id=conversation_id, user_id=current_user.id, is_admin=is_user_admin,
                               ui_language=get_translator(request, current_user).language)
        logger.info("MP3 generation task queued for conversation_id: %s", conversation_id)
        return JSONResponse(content={"message": chat_text(current_user, "mp3_generation_started")})
    except Exception as exc:
        logger.error("Error trying to generate MP3 for conversation_id %s: %s", conversation_id, exc)
        raise HTTPException(status_code=500, detail=chat_text(current_user, "request_failed"))


@router.get("/serve-mp3/{conversation_id}")
async def serve_mp3(conversation_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    await require_native_export(conversation_id, current_user)

    try:
        from common import generate_user_hash, users_directory

        hash_prefix1, hash_prefix2, user_hash = generate_user_hash(current_user.username)
        conv_str = f"{conversation_id:07d}"
        mp3_dir = Path(users_directory) / hash_prefix1 / hash_prefix2 / user_hash / "files" / conv_str[:3] / conv_str[3:] / "mp3"
        if not mp3_dir.exists():
            return JSONResponse(content={"error": chat_text(current_user, "mp3_not_found")}, status_code=404)
        mp3_files = sorted(mp3_dir.glob("*.mp3"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not mp3_files:
            return JSONResponse(content={"error": chat_text(current_user, "mp3_not_found")}, status_code=404)
        return FileResponse(mp3_files[0], media_type="audio/mpeg", filename=Translator(getattr(current_user, "ui_language", None)).t(
            "exports.conversation_filename", id=conversation_id) + ".mp3")
    except Exception as exc:
        logger.error("Error serving MP3: %s", exc)
        return JSONResponse(content={"error": chat_text(current_user, "audio_unavailable")}, status_code=500)


def get_browser(user_agent: str):
    logger.debug("User_agent: %s", user_agent)
    if "Firefox" in user_agent:
        return "firefox"
    if "Safari" in user_agent and "Chrome" not in user_agent:
        return "safari"
    if "Edg" in user_agent:
        return "edge"
    if "Chrome" in user_agent:
        return "chrome"
    return "other"


async def transcribe_with_elevenlabs(
    audio_content: bytes = None,
    language_code: str | None = None,
):
    return await transcribe_media_with_elevenlabs(
        audio_content=audio_content,
        language_code=language_code,
    )


async def transcribe_with_deepgram(
    audio_content: bytes = None,
    user_agent: str = None,
    language_code: str | None = None,
):
    return await transcribe_media_with_deepgram(
        audio_content=audio_content,
        user_agent=user_agent,
        language_code=language_code,
    )


def _decode_audio_duration(
    content: bytes,
    *,
    audio_format: str,
    codec: str | None = None,
) -> float:
    """Decode an uploaded recording and return its duration off the event loop."""
    options = {"format": audio_format}
    if codec is not None:
        options["codec"] = codec
    decoded_audio = AudioSegment.from_file(io.BytesIO(content), **options)
    return decoded_audio.duration_seconds


async def transcribe(request: Request, audio: UploadFile = File(None), user_id: int = None, *, ui_language: str = "en"):
    from i18n import Translator
    translator = Translator(ui_language)
    try:
        audio_duration = 0
        content = None

        if audio:
            content = await audio.read()
            user_agent = request.headers.get("user-agent")
            browser = get_browser(user_agent)

            if browser == "firefox":
                logger.info("Using OggOpus for Firefox")
                audio_format = "ogg"
                codec = "opus"
            elif browser == "chrome" or browser == "edge":
                logger.info("Using WebMOpus for Chrome and Edge")
                audio_format = "webm"
                codec = "opus"
            elif browser == "safari":
                logger.info("Using MP4 for Safari")
                audio_format = "mp4"
                codec = None
            else:
                raise _LocalizedSTTError(status_code=400, detail=chat_text(translator, "browser_unsupported"))
            audio_duration = await asyncio.to_thread(
                _decode_audio_duration,
                content,
                audio_format=audio_format,
                codec=codec,
            )
        else:
            raise _LocalizedSTTError(status_code=400, detail=chat_text(translator, "audio_required"))

        if audio_duration <= 0:
            raise _LocalizedSTTError(status_code=400, detail=chat_text(translator, "audio_required"))

        duration_min = audio_duration / 60
        primary_engine = (
            "deepgram"
            if str(stt_engine).strip().lower() == "deepgram"
            else "elevenlabs"
        )
        user_agent = request.headers.get("user-agent")
        language_code = await _load_primary_stt_language(user_id, getattr(request.state, "application_context", None))

        async def transcribe_with_engine(engine: str):
            if engine == "elevenlabs":
                return await transcribe_with_elevenlabs(
                    audio_content=content,
                    language_code=language_code,
                )
            return await transcribe_with_deepgram(
                audio_content=content,
                user_agent=user_agent,
                language_code=language_code,
            )

        try:
            primary_reservation_id = await reserve_stt_attempt(
                user_id=user_id,
                engine=primary_engine,
                configured_engine=primary_engine,
                duration_min=duration_min,
                context=primary_engine,
            )
        except HTTPException as exc:
            key = "insufficient_balance" if exc.status_code == 402 else "billing_unavailable"
            raise _localized_stt_error(exc, translator, key) from exc
        try:
            prompt = await transcribe_with_engine(primary_engine)
        except BaseException as primary_error:
            try:
                await finalize_failed_stt_attempt(
                    primary_reservation_id,
                    primary_error,
                    context=f"failed {primary_engine}",
                )
            except HTTPException as exc:
                raise _localized_stt_error(exc, translator, "billing_unavailable") from exc
            if (
                not isinstance(primary_error, Exception)
                or not stt_fallback_enabled
                or primary_engine == "elevenlabs"
            ):
                raise

            logger.warning(
                "Primary STT engine (%s) failed: %s",
                primary_engine,
                primary_error,
            )
            fallback_engine = "elevenlabs"
            try:
                fallback_reservation_id = await reserve_stt_attempt(
                    user_id=user_id,
                    engine=fallback_engine,
                    configured_engine=primary_engine,
                    duration_min=duration_min,
                    context=f"fallback {fallback_engine}",
                )
            except HTTPException as exc:
                key = "insufficient_balance" if exc.status_code == 402 else "billing_unavailable"
                raise _localized_stt_error(exc, translator, key) from exc
            try:
                prompt = await transcribe_with_engine(fallback_engine)
            except BaseException as fallback_error:
                try:
                    await finalize_failed_stt_attempt(
                        fallback_reservation_id,
                        fallback_error,
                        context=f"failed fallback {fallback_engine}",
                    )
                except HTTPException as exc:
                    raise _localized_stt_error(exc, translator, "billing_unavailable") from exc
                if not isinstance(fallback_error, Exception):
                    raise
                logger.error(
                    "Both STT engines failed. Primary: %s, Fallback: %s",
                    primary_error,
                    fallback_error,
                )
                raise primary_error from fallback_error

            try:
                await settle_stt_attempt(
                    fallback_reservation_id,
                    context=f"fallback {fallback_engine}",
                )
            except HTTPException as exc:
                raise _localized_stt_error(exc, translator, "billing_unavailable") from exc
            logger.info("Fallback to %s successful", fallback_engine)
            return prompt

        try:
            await settle_stt_attempt(
                primary_reservation_id,
                context=primary_engine,
            )
        except HTTPException as exc:
            raise _localized_stt_error(exc, translator, "billing_unavailable") from exc
        return prompt
    except _LocalizedSTTError:
        raise
    except HTTPException as exc:
        raise _localized_stt_error(exc, translator, "provider_unavailable") from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=chat_text(translator, "provider_unavailable"))
    except Exception as exc:
        logger.error("Error: %s", exc)
        raise HTTPException(status_code=500, detail=chat_text(translator, "provider_unavailable"))


@router.post("/api/transcribe-web")
async def transcribe_web(
    request: Request,
    audio: UploadFile = File(None),
    conversation_id: str = Form(...),
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()

    try:
        await require_conversation_access(int(conversation_id), current_user)

        from chat.services.localization import chat_translator
        prompt = await transcribe(request, audio, current_user.id, ui_language=chat_translator(current_user).language)
        return JSONResponse(content={"prompt": prompt}, status_code=200)
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse(content={"error": chat_text(current_user, "provider_unavailable")}, status_code=500)


@router.get("/get-audio/{path:path}")
async def get_audio(path: str, token: str):
    current_user = None
    try:
        payload = decode_jwt_cached(token, SECRET_KEY)
        username = payload.get("username")
        if not username:
            raise HTTPException(status_code=401, detail=chat_text(current_user, "token_invalid"))

        current_user = await get_user_by_username(username)
        if not current_user:
            raise HTTPException(status_code=401, detail=chat_text(current_user, "token_invalid"))

        exp = payload.get("exp")
        if not exp:
            raise HTTPException(status_code=401, detail=chat_text(current_user, "token_invalid"))

        cache_base = Path(cache_directory)
        validated_path = validate_path_within_directory(path, cache_base)
    except jwt.ExpiredSignatureError:
        response = JSONResponse(status_code=401, content={"detail": chat_text(current_user, "token_expired")})
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail=chat_text(current_user, "token_invalid"))

    if validated_path.exists():
        current_time = datetime.now(timezone.utc)
        expiration_time = datetime.fromtimestamp(exp, timezone.utc)
        time_until_expiration = expiration_time - current_time

        if time_until_expiration.total_seconds() <= 0:
            response = JSONResponse(status_code=401, content={"detail": chat_text(current_user, "token_expired")})
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response

        audio_path_str = str(validated_path)
        if audio_path_str.endswith(".ogg") or audio_path_str.endswith(".opus"):
            media_type = "audio/ogg"
        elif audio_path_str.endswith(".mp3"):
            media_type = "audio/mpeg"
        else:
            raise HTTPException(status_code=415, detail=chat_text(current_user, "media_type_unsupported"))

        response = FileResponse(str(validated_path), media_type=media_type)
        response.headers["Cache-Control"] = f"public, max-age={int(time_until_expiration.total_seconds())}"
        response.headers["Expires"] = expiration_time.strftime("%a, %d %b %Y %H:%M:%S GMT")
        return response

    raise HTTPException(status_code=404, detail=chat_text(current_user, "file_not_found"))
