"""Owner-scoped self-service APIs for retained messaging voice notes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from auth import get_current_user
from database import get_db_connection
from log_config import logger
from models import User
from request_security import validate_mutation_request

from .service import (
    ActiveRetranscriptionError,
    create_retranscription_revision,
    decide_retranscription_revision,
    get_retranscription_revision,
    get_voice_note_state,
    list_comparison_llms,
)


router = APIRouter()


def _dispatch_retranscription_revision(revision_id: int) -> None:
    """Publish an idempotently claimed worker message."""
    from tasks import retranscribe_voice_note_task

    retranscribe_voice_note_task.send(int(revision_id))


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RetranscribePayload(_Payload):
    stt_engine: Literal["configured", "deepgram", "elevenlabs"] = "configured"
    comparison_llm_id: int | None = Field(default=None, gt=0)


class RevisionDecisionPayload(_Payload):
    decision: Literal["accept", "reject"]


def _require_user(current_user: User | None) -> User:
    if current_user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return current_user


def _validated_mutation(
    request: Request, current_user: User | None
) -> User | JSONResponse:
    user = _require_user(current_user)
    rejection = validate_mutation_request(request)
    return rejection if rejection is not None else user


@router.get("/api/messaging-voice-notes/comparison-models")
async def comparison_models(
    current_user: User = Depends(get_current_user),
):
    _require_user(current_user)
    models = await list_comparison_llms()
    return {
        "models": [
            {
                "id": model["id"],
                "machine": model["machine"],
                "model": model["model"],
                "display_name": model["display_name"],
                "input_token_cost": model["input_token_cost"],
                "output_token_cost": model["output_token_cost"],
            }
            for model in models
        ]
    }


@router.get("/api/messaging-voice-notes/{message_id}")
async def voice_note_state(
    message_id: int,
    current_user: User = Depends(get_current_user),
):
    user = _require_user(current_user)
    state = await get_voice_note_state(message_id, owner_user_id=user.id)
    if state is None:
        raise HTTPException(status_code=404, detail="Voice note not found")
    if str(state.get("latest_revision_status") or "") == "queued":
        try:
            _dispatch_retranscription_revision(int(state["latest_revision_id"]))
        except Exception:
            logger.warning(
                "Could not redispatch queued voice-note revision %s",
                state.get("latest_revision_id"),
                exc_info=True,
            )
    return JSONResponse(
        content={"voice_note": state},
        headers={"Cache-Control": "private, no-store, max-age=0"},
    )


@router.get("/api/messaging-voice-notes/revisions/{revision_id}")
async def retranscription_revision(
    revision_id: int,
    current_user: User = Depends(get_current_user),
):
    user = _require_user(current_user)
    revision = await get_retranscription_revision(
        revision_id,
        owner_user_id=user.id,
    )
    if revision is None:
        raise HTTPException(status_code=404, detail="Revision not found")
    return JSONResponse(
        content={"revision": revision},
        headers={"Cache-Control": "private, no-store, max-age=0"},
    )


@router.post("/api/messaging-voice-notes/{message_id}/retranscribe", response_model=None)
async def retranscribe_voice_note(
    message_id: int,
    request: Request,
    payload: RetranscribePayload,
    current_user: User = Depends(get_current_user),
):
    checked = _validated_mutation(request, current_user)
    if isinstance(checked, JSONResponse):
        return checked
    try:
        revision_id = await create_retranscription_revision(
            message_id=message_id,
            owner_user_id=checked.id,
            stt_engine=payload.stt_engine,
            comparison_llm_id=payload.comparison_llm_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ActiveRetranscriptionError as exc:
        try:
            _dispatch_retranscription_revision(exc.revision_id)
        except Exception:
            logger.warning(
                "Could not redispatch active voice-note revision %s",
                exc.revision_id,
                exc_info=True,
            )
        return JSONResponse(
            status_code=409,
            content={"detail": str(exc), "revision_id": exc.revision_id},
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        _dispatch_retranscription_revision(revision_id)
    except Exception as exc:
        async with get_db_connection() as conn:
            await conn.execute(
                """
                UPDATE MESSAGE_TRANSCRIPTION_REVISIONS
                SET status='failed', error_message=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='queued'
                """,
                (f"Could not enqueue retranscription: {exc}"[:2000], revision_id),
            )
            await conn.commit()
        raise HTTPException(
            status_code=503,
            detail="Could not enqueue retranscription",
        ) from exc

    return JSONResponse(
        status_code=202,
        content={"accepted": True, "revision_id": revision_id},
    )


@router.post("/api/messaging-voice-notes/revisions/{revision_id}/decision", response_model=None)
async def decide_retranscription(
    revision_id: int,
    request: Request,
    payload: RevisionDecisionPayload,
    current_user: User = Depends(get_current_user),
):
    checked = _validated_mutation(request, current_user)
    if isinstance(checked, JSONResponse):
        return checked
    try:
        return await decide_retranscription_revision(
            revision_id=revision_id,
            owner_user_id=checked.id,
            decision=payload.decision,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
