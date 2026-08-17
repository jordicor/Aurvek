import asyncio
import logging

import httpx
import orjson
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from auth import get_current_user, unauthenticated_response
from common import VOICE_CALL_MIN_BALANCE_TO_START, has_sufficient_balance
from database import get_db_connection
from integrations.elevenlabs.service import (
    ElevenLabsProviderSessionError,
    ElevenLabsSessionBindingError,
    service as elevenlabs_service,
)
from log_config import logger
from models import User
from tasks import download_elevenlabs_audio_task
from wellbeing_service import get_active_pause, record_activity as record_wellbeing_activity


router = APIRouter()


async def _is_admin_user(current_user: User) -> bool:
    return bool(await current_user.is_admin)


def _pause_response(active_pause: dict) -> JSONResponse:
    pause_reason = active_pause.get("reason") or "pause_active"
    return JSONResponse(
        content={
            "error": "wellbeing_pause_active" if pause_reason == "pause_active" else "wellbeing_pause_required",
            "message": active_pause.get("message") or "A break pause is required before continuing.",
            "pause_until": active_pause.get("pause_until"),
            "session_id": active_pause.get("session_id"),
            "reason": pause_reason,
        },
        status_code=429,
    )


def _binding_error_response(error_code: str) -> JSONResponse:
    return JSONResponse(
        content={
            "error": error_code,
            "message": "This voice session is not linked to this conversation.",
        },
        status_code=409,
    )


def _insufficient_balance_response() -> JSONResponse:
    # Realtime voice calls run on the platform ElevenLabs key and bill per minute.
    # Fail fast: block before the signed URL is minted (config path) or before the
    # session is bound (session path) so a zero-balance user cannot start a
    # real-cost call. The 402 status is the machine signal; the human message is
    # placed in both `error` and `message` because the config path surfaces
    # `error` and the session path surfaces `message`.
    human_message = (
        "You need a prepaid balance to start a voice call. Add credit and try again."
    )
    return JSONResponse(
        content={"error": human_message, "message": human_message},
        status_code=402,
    )


async def _mark_session_failed(
    conversation_id: int,
    session_id: str,
    user_id: int,
) -> None:
    try:
        await elevenlabs_service.mark_session_status(
            conversation_id,
            session_id,
            "failed",
            user_id,
        )
    except ElevenLabsSessionBindingError:
        logger.warning(
            "[ElevenLabs] Could not mark unbound session %s failed for conversation %s",
            session_id,
            conversation_id,
        )


@router.get("/api/conversations/{conversation_id}/elevenlabs/config")
async def get_elevenlabs_config(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()

    if not elevenlabs_service.is_configured():
        return JSONResponse(content={"error": "ElevenLabs integration is disabled"}, status_code=503)

    is_admin_user = await _is_admin_user(current_user)
    conversation = await elevenlabs_service.validate_conversation_access(
        conversation_id,
        current_user.id,
        is_admin_user,
    )
    if not conversation:
        return JSONResponse(content={"error": "Conversation not found"}, status_code=404)
    if conversation.get("locked"):
        return JSONResponse(content={"error": "This conversation is locked"}, status_code=403)
    if conversation.get("is_incognito"):
        return JSONResponse(
            content={"error": "Voice calls are not available in incognito chats"},
            status_code=403,
        )

    active_pause = await get_active_pause(current_user.id)
    if active_pause:
        return _pause_response(active_pause)

    # Fail-fast cost guard: voice calls are billed per minute on the platform
    # ElevenLabs key. Block before the signed URL is minted so a user without
    # prepaid balance cannot start a real-cost call. The subscription (text-only)
    # option never frees this -- paid media always requires real balance.
    if not await has_sufficient_balance(current_user.id, VOICE_CALL_MIN_BALANCE_TO_START):
        return _insufficient_balance_response()

    config = await elevenlabs_service.get_configuration(conversation_id, current_user.id, is_admin_user)
    if not config:
        return JSONResponse(content={"error": "No ElevenLabs agent configured for this conversation"}, status_code=409)

    return JSONResponse(content=config)


@router.post("/api/conversations/{conversation_id}/elevenlabs/session")
async def start_elevenlabs_session(
    conversation_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()

    if not elevenlabs_service.is_configured():
        return JSONResponse(content={"error": "ElevenLabs integration is disabled"}, status_code=503)

    payload = await request.json()
    raw_session_id = payload.get("session_id")
    session_id = raw_session_id.strip() if isinstance(raw_session_id, str) else ""

    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    is_admin_user = await _is_admin_user(current_user)
    conversation = await elevenlabs_service.validate_conversation_access(
        conversation_id,
        current_user.id,
        is_admin_user,
    )
    if not conversation:
        return JSONResponse(content={"error": "Conversation not found"}, status_code=404)
    if conversation.get("locked"):
        return JSONResponse(content={"error": "This conversation is locked"}, status_code=403)
    if conversation.get("is_incognito"):
        return JSONResponse(
            content={"error": "Voice calls are not available in incognito chats"},
            status_code=403,
        )

    active_pause = await get_active_pause(current_user.id)
    if active_pause:
        return _pause_response(active_pause)

    # Fail-fast cost guard (defense in depth): the client reports the session as
    # started here, so re-check balance to reject a call whose signed URL was
    # obtained while the user still had balance that has since been depleted. On
    # 402 the frontend tears the session down immediately (handleConnected).
    if not await has_sufficient_balance(current_user.id, VOICE_CALL_MIN_BALANCE_TO_START):
        return _insufficient_balance_response()

    existing_status = (conversation.get("elevenlabs_status") or "").lower()
    try:
        newly_bound = await elevenlabs_service.register_session(
            conversation_id,
            session_id,
            conversation["user_id"],
        )
    except ElevenLabsProviderSessionError:
        logger.warning(
            "[ElevenLabs] Provider metadata rejected for conversation %s",
            conversation_id,
        )
        return _binding_error_response("voice_session_validation_failed")
    except ElevenLabsSessionBindingError:
        logger.warning(
            "[ElevenLabs] Session binding rejected for conversation %s",
            conversation_id,
        )
        return _binding_error_response("voice_session_binding_conflict")
    except httpx.HTTPError:
        logger.warning(
            "[ElevenLabs] Provider error validating session %s",
            session_id,
            exc_info=True,
        )
        return JSONResponse(
            content={"error": "Unable to validate the ElevenLabs session"},
            status_code=502,
        )

    if newly_bound:
        try:
            await record_wellbeing_activity(
                user_id=current_user.id,
                conversation_id=conversation_id,
                activity_type="voice_call_started",
                metadata={"elevenlabs_session_id": session_id},
            )
        except Exception:
            logger.warning(
                "[wellbeing] Failed to record ElevenLabs session start for conversation_id=%s",
                conversation_id,
                exc_info=True,
            )

    watchdog_hint_eval_id = payload.get("watchdog_hint_eval_id")
    if newly_bound and watchdog_hint_eval_id is not None:
        prompt_id = conversation.get("role_id")
        if prompt_id is not None:
            try:
                async with get_db_connection() as wconn:
                    await wconn.execute(
                        """UPDATE WATCHDOG_STATE SET pending_hint = NULL, hint_severity = NULL
                           WHERE conversation_id = ? AND prompt_id = ? AND last_evaluated_message_id = ?""",
                        (conversation_id, prompt_id, watchdog_hint_eval_id),
                    )
                    await wconn.commit()
            except Exception:
                logging.getLogger("watchdog").warning(
                    "Failed to consume hint via CAS for conv=%d in /elevenlabs/session",
                    conversation_id,
                    exc_info=True,
                )

    return JSONResponse(
        content={
            "status": "active",
            "session_id": session_id,
            "previous_status": existing_status or None,
        }
    )


@router.post("/api/conversations/{conversation_id}/elevenlabs/complete")
async def complete_elevenlabs_session(
    conversation_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()

    if not elevenlabs_service.is_configured():
        return JSONResponse(content={"error": "ElevenLabs integration is disabled"}, status_code=503)

    payload = await request.json()
    raw_session_id = payload.get("session_id")
    requested_session_id = (
        raw_session_id.strip() if isinstance(raw_session_id, str) else ""
    )

    is_admin_user = await _is_admin_user(current_user)
    conversation = await elevenlabs_service.validate_conversation_access(
        conversation_id,
        current_user.id,
        is_admin_user,
    )
    if not conversation:
        return JSONResponse(content={"error": "Conversation not found"}, status_code=404)
    if conversation.get("locked"):
        return JSONResponse(content={"error": "This conversation is locked"}, status_code=403)

    try:
        binding = await elevenlabs_service.get_bound_session(
            conversation_id,
            requested_session_id,
            conversation["user_id"],
        )
    except ElevenLabsSessionBindingError:
        return _binding_error_response("voice_session_binding_mismatch")

    session_id = binding["session_id"]
    if binding.get("transcript_saved_at"):
        return JSONResponse(content={"messages_saved": 0, "status": "already_completed"})

    max_retries = 5
    retry_delay = 2.0

    for attempt in range(max_retries):
        status = await elevenlabs_service.check_conversation_status(session_id)

        if status is None:
            logger.error("[ElevenLabs] Unable to check provider session %s", session_id)
            return JSONResponse(
                content={
                    "error": "Unable to check ElevenLabs session",
                    "detail": "Try again later.",
                },
                status_code=502,
            )

        finished_statuses = {
            "done",
            "completed",
            "ended",
            "finished",
            "disconnected",
            "terminated",
        }
        active_statuses = {
            "initiated",
            "in-progress",
            "processing",
            "active",
            "in_progress",
            "ongoing",
            "started",
            "connected",
        }

        if status in finished_statuses:
            logger.info("[ElevenLabs] Conversation %s is ready for transcript fetch (status: %s)", session_id, status)
            break
        if status in active_statuses:
            logger.info(
                "[ElevenLabs] Conversation %s still active (status: %s), waiting... (attempt %d/%d)",
                session_id,
                status,
                attempt + 1,
                max_retries,
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
            else:
                logger.warning("[ElevenLabs] Conversation %s still active after %d retries", session_id, max_retries)
                return JSONResponse(
                    content={
                        "error": "Conversation still active",
                        "detail": f"Status: {status}. Try again later.",
                    },
                    status_code=425,
                )
        elif status == "failed":
            await _mark_session_failed(
                conversation_id,
                session_id,
                conversation["user_id"],
            )
            return JSONResponse(
                content={
                    "error": "The ElevenLabs session failed",
                    "status": "failed",
                },
                status_code=409,
            )
        else:
            logger.warning("[ElevenLabs] Unknown conversation status: %s", status)
            return JSONResponse(
                content={
                    "error": "ElevenLabs session is not ready",
                    "detail": f"Status: {status}. Try again later.",
                },
                status_code=425,
            )

    try:
        transcript = await elevenlabs_service.fetch_full_transcript(session_id)
    except httpx.HTTPStatusError as exc:
        logger.error("[ElevenLabs] API error while fetching transcript for session %s: %s", session_id, exc)
        return JSONResponse(
            content={
                "error": "Failed to fetch ElevenLabs transcript",
                "detail": "The provider did not return a transcript.",
            },
            status_code=502,
        )
    except httpx.HTTPError as exc:
        logger.error("[ElevenLabs] HTTP error while fetching transcript for session %s: %s", session_id, exc)
        return JSONResponse(
            content={
                "error": "Failed to fetch ElevenLabs transcript",
                "detail": "The provider could not be reached.",
            },
            status_code=502,
        )

    try:
        saved, last_user_id, last_bot_id, already_saved = await elevenlabs_service.save_transcript_to_db(
            conversation_id,
            session_id,
            conversation["user_id"],
            transcript,
        )
    except ElevenLabsSessionBindingError:
        return _binding_error_response("voice_session_binding_mismatch")
    except Exception as exc:
        logger.exception("[ElevenLabs] Failed to persist transcript for conversation %s", conversation_id)
        await _mark_session_failed(
            conversation_id,
            session_id,
            conversation["user_id"],
        )
        raise HTTPException(status_code=500, detail="Failed to store ElevenLabs transcript") from exc

    if saved == 0 and not already_saved:
        return JSONResponse(
            content={
                "error": "ElevenLabs transcript is not ready",
                "detail": "No transcript turns are available yet. Try again later.",
            },
            status_code=425,
        )

    if session_id and not already_saved:
        try:
            download_elevenlabs_audio_task.send(conversation_id, session_id, conversation["user_id"])
            logger.info("[ElevenLabs] Enqueued audio download for conversation %s (session %s)", conversation_id, session_id)
        except Exception as enqueue_exc:
            logger.warning("[ElevenLabs] Could not enqueue audio download for conversation %s: %s", conversation_id, enqueue_exc)

    prompt_id = conversation.get("role_id")
    if last_user_id and last_bot_id and prompt_id:
        try:
            async with get_db_connection(readonly=True) as wconn:
                cursor = await wconn.execute("SELECT watchdog_config FROM PROMPTS WHERE id = ?", (prompt_id,))
                row = await cursor.fetchone()
                watchdog_config = None
                if row and row["watchdog_config"]:
                    try:
                        watchdog_config = orjson.loads(row["watchdog_config"])
                    except Exception:
                        watchdog_config = None
            post_watchdog_config = None
            if isinstance(watchdog_config, dict):
                post_watchdog_config = (
                    watchdog_config.get("post_watchdog")
                    if isinstance(watchdog_config.get("post_watchdog"), dict)
                    else watchdog_config
                )
            if post_watchdog_config and post_watchdog_config.get("enabled"):
                from tools.watchdog import watchdog_evaluate_task

                watchdog_evaluate_task.send(conversation_id, last_user_id, last_bot_id, prompt_id, True)
                logger.info(
                    "[ElevenLabs] Enqueued watchdog evaluation for voice conv=%d (skip_frequency=True)",
                    conversation_id,
                )
        except Exception:
            logging.getLogger("watchdog").error(
                "Failed to enqueue watchdog for voice conv=%d",
                conversation_id,
                exc_info=True,
            )

    return JSONResponse(
        content={
            "messages_saved": saved,
            "status": "already_completed" if already_saved else "completed",
        }
    )


@router.post("/api/conversations/{conversation_id}/elevenlabs/stop")
async def stop_elevenlabs_session(
    conversation_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()

    if not elevenlabs_service.is_configured():
        return JSONResponse(content={"error": "ElevenLabs integration is disabled"}, status_code=503)

    payload = await request.json()
    raw_session_id = payload.get("session_id")
    requested_session_id = (
        raw_session_id.strip() if isinstance(raw_session_id, str) else ""
    )
    raw_status = payload.get("status")
    status = raw_status.strip().lower() if isinstance(raw_status, str) else "failed"

    if status != "failed":
        raise HTTPException(
            status_code=400,
            detail="Only failed sessions may be updated through this endpoint",
        )

    is_admin_user = await _is_admin_user(current_user)
    conversation = await elevenlabs_service.validate_conversation_access(
        conversation_id,
        current_user.id,
        is_admin_user,
    )
    if not conversation:
        return JSONResponse(content={"error": "Conversation not found"}, status_code=404)
    if conversation.get("locked"):
        return JSONResponse(content={"error": "This conversation is locked"}, status_code=403)

    try:
        binding = await elevenlabs_service.get_bound_session(
            conversation_id,
            requested_session_id,
            conversation["user_id"],
        )
        session_id = binding["session_id"]
        await elevenlabs_service.mark_session_status(
            conversation_id,
            session_id,
            status,
            conversation["user_id"],
        )
    except ElevenLabsSessionBindingError:
        return _binding_error_response("voice_session_binding_mismatch")
    return JSONResponse(content={"status": status, "session_id": session_id})
