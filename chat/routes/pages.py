import orjson
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from auth import get_current_user
from captcha_service import get_captcha_config
from common import GOOGLE_CLIENT_ID, _get_marketplace_template_flags, templates
from database import get_db_connection
from log_config import logger
from models import User
from prompts import can_user_access_prompt

from chat.services.page_context import handle_get_request

router = APIRouter()


@router.get("/chat", response_class=HTMLResponse)
async def chat(request: Request, current_user: User | None = Depends(get_current_user)):
    logger.debug("Access attempt to /chat. Current user: %s", current_user)
    if current_user is None:
        logger.info("User not authenticated. Redirecting to /login")
        return RedirectResponse(url="/login")

    try:
        async with get_db_connection() as conn:
            return await handle_get_request(request, None, current_user, conn)
    except Exception as exc:
        logger.error("Error handling chat request: %s", exc)
        return templates.TemplateResponse(
            "/error.html",
            {
                "request": request,
                "error_message": "An unexpected error occurred. Please try again later.",
                "marketplace": _get_marketplace_template_flags(),
            },
            status_code=500,
        )


@router.post("/chat")
async def chat_post(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "captcha": get_captcha_config(),
                "google_oauth_available": bool(GOOGLE_CLIENT_ID),
            },
        )

    data = await request.json()
    form_type = data.get("form_type")
    prompt_id = data.get("prompt_id")
    type_of_model = data.get("type_of_model")

    logger.info(
        "[DEBUG] /chat POST - User: %s, form_type: %s, prompt_id: %s, type_of_model: %s",
        current_user.username,
        form_type,
        prompt_id,
        type_of_model,
    )

    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            "SELECT llm_id, current_prompt_id FROM USER_DETAILS WHERE user_id = ?",
            (current_user.id,),
        )
        before_values = await cursor.fetchone()
        logger.info(
            "[DEBUG] Values BEFORE: llm_id=%s, current_prompt_id=%s",
            before_values[0] if before_values else "NULL",
            before_values[1] if before_values else "NULL",
        )

        if form_type == "prompt":
            if not await can_user_access_prompt(current_user, prompt_id, cursor):
                await conn.close()
                raise HTTPException(status_code=403, detail="Access denied to this prompt")
            logger.info("[DEBUG] Updating current_prompt_id to %s for user_id %s", prompt_id, current_user.id)
            await cursor.execute(
                "UPDATE USER_DETAILS SET current_prompt_id = ? WHERE user_id = ?",
                (prompt_id, current_user.id),
            )
        elif form_type == "llm":
            logger.info("[DEBUG] Updating llm_id to %s for user_id %s", type_of_model, current_user.id)
            # Validate the target model exists and is enabled. This path previously
            # wrote llm_id with no validation at all (pre-existing gap, fixed here).
            await cursor.execute(
                "SELECT COALESCE(enabled, 1), machine, model FROM LLM WHERE id = ?",
                (type_of_model,),
            )
            llm_row = await cursor.fetchone()
            if not llm_row:
                await conn.close()
                raise HTTPException(status_code=404, detail="LLM model not found")
            if not bool(llm_row[0]):
                await conn.close()
                raise HTTPException(status_code=400, detail="This LLM model is disabled")
            # GPTSub (ChatGPT subscription) models need an active per-user link.
            if llm_row[1] == "GPTSub":
                try:
                    from subscription_auth import gptsub_allowed
                except ImportError:
                    gptsub_allowed = None
                if gptsub_allowed is None or not await gptsub_allowed(
                    current_user, model=llm_row[2]
                ):
                    await conn.close()
                    raise HTTPException(
                        status_code=403,
                        detail="This model requires connecting your ChatGPT subscription",
                    )
            await cursor.execute(
                "UPDATE USER_DETAILS SET llm_id = ? WHERE user_id = ?",
                (type_of_model, current_user.id),
            )

        await conn.commit()
        await cursor.execute(
            "SELECT llm_id, current_prompt_id FROM USER_DETAILS WHERE user_id = ?",
            (current_user.id,),
        )
        after_values = await cursor.fetchone()
        logger.info(
            "[DEBUG] Values AFTER: llm_id=%s, current_prompt_id=%s",
            after_values[0] if after_values else "NULL",
            after_values[1] if after_values else "NULL",
        )
        await conn.close()

    logger.info("[DEBUG] /chat POST completed successfully")
    return JSONResponse(content={"success": True})


@router.get("/api/conversations/{conversation_id}/details")
async def get_conversation_details(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT c.llm_id, c.role_id
            FROM CONVERSATIONS c
            WHERE c.id = ? AND c.user_id = ?
            """,
            (conversation_id, current_user.id),
        )

        conversation_data = await cursor.fetchone()
        if not conversation_data:
            raise HTTPException(status_code=404, detail="Conversation not found")

        llm_id, prompt_id = conversation_data
        await cursor.execute(
            """
            SELECT
                (SELECT l.model FROM LLM l WHERE l.id = ?) AS model,
                (SELECT p.name FROM PROMPTS p WHERE p.id = ?) AS prompt_name,
                (SELECT p.forced_llm_id FROM PROMPTS p WHERE p.id = ?) AS forced_llm_id,
                (SELECT p.hide_llm_name FROM PROMPTS p WHERE p.id = ?) AS hide_llm_name,
                (SELECT p.allowed_llms FROM PROMPTS p WHERE p.id = ?) AS allowed_llms,
                (SELECT COALESCE(p.is_paid, 0) FROM PROMPTS p WHERE p.id = ?) AS is_paid
            """,
            (llm_id, prompt_id, prompt_id, prompt_id, prompt_id, prompt_id),
        )

        result = await cursor.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="LLM or Prompt not found")

        model, prompt_name, forced_llm_id, hide_llm_name, allowed_llms, is_paid = result
        await conn.close()

    return JSONResponse(
        content={
            "llm_id": llm_id,
            "model": model,
            "prompt_id": prompt_id,
            "prompt_name": prompt_name,
            "forced_llm_id": forced_llm_id,
            "hide_llm_name": bool(hide_llm_name) if hide_llm_name else False,
            "allowed_llms": orjson.loads(allowed_llms) if allowed_llms else None,
            "is_paid": bool(is_paid),
        }
    )


@router.patch("/api/conversations/{conversation_id}/model")
async def update_conversation_model(
    conversation_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        data = await request.json()
        new_llm_id = data.get("llm_id")
        only_if_empty = data.get("only_if_empty") is True
        if not new_llm_id:
            raise HTTPException(status_code=400, detail="llm_id is required")

        async with get_db_connection() as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                """
                SELECT id, role_id, llm_id FROM CONVERSATIONS
                WHERE id = ? AND user_id = ?
                """,
                (conversation_id, current_user.id),
            )

            conv_data = await cursor.fetchone()
            if not conv_data:
                raise HTTPException(status_code=404, detail="Conversation not found")

            prompt_id = conv_data[1]
            current_llm_id = conv_data[2]
            if only_if_empty:
                await cursor.execute(
                    "SELECT 1 FROM MESSAGES WHERE conversation_id = ? LIMIT 1",
                    (conversation_id,),
                )
                if await cursor.fetchone():
                    return JSONResponse(content={"success": True, "updated": False})

            if prompt_id:
                await cursor.execute(
                    """
                    SELECT forced_llm_id, name, allowed_llms FROM PROMPTS WHERE id = ?
                    """,
                    (prompt_id,),
                )
                prompt_data = await cursor.fetchone()
                if prompt_data and prompt_data[0]:
                    forced_llm_id = prompt_data[0]
                    prompt_name = prompt_data[1]
                    if int(new_llm_id) != forced_llm_id:
                        if only_if_empty:
                            return JSONResponse(
                                content={"success": True, "updated": False}
                            )
                        raise HTTPException(
                            status_code=403,
                            detail=f"This prompt '{prompt_name}' requires a specific AI model and cannot be changed",
                        )

                if prompt_data and not prompt_data[0] and prompt_data[2]:
                    allowed_ids = orjson.loads(prompt_data[2])
                    prompt_name = prompt_data[1]
                    if int(new_llm_id) not in allowed_ids:
                        if only_if_empty:
                            return JSONResponse(
                                content={"success": True, "updated": False}
                            )
                        raise HTTPException(
                            status_code=403,
                            detail=f"This prompt '{prompt_name}' only allows specific AI models",
                        )

            await cursor.execute(
                """
                SELECT id, machine, model, COALESCE(enabled, 1)
                FROM LLM
                WHERE id = ?
                """,
                (new_llm_id,),
            )
            llm_data = await cursor.fetchone()
            if not llm_data:
                raise HTTPException(status_code=404, detail="LLM model not found")
            if not bool(llm_data[3]) and (
                llm_data[1] == "GPTSub"
                or int(new_llm_id) != int(current_llm_id or 0)
            ):
                raise HTTPException(status_code=400, detail="This LLM model is disabled")
            # GPTSub (ChatGPT subscription) models need an active per-user link.
            # Even a no-op selection is denied after unlink: preserving an inert
            # personal model would make subsequent sends fail unexpectedly.
            if llm_data[1] == "GPTSub":
                try:
                    from subscription_auth import gptsub_allowed
                except ImportError:
                    gptsub_allowed = None
                if gptsub_allowed is None or not await gptsub_allowed(
                    current_user, model=llm_data[2]
                ):
                    raise HTTPException(
                        status_code=403,
                        detail="This model requires connecting your ChatGPT subscription",
                    )

            if only_if_empty:
                await cursor.execute(
                    """
                    UPDATE CONVERSATIONS
                    SET llm_id = ?
                    WHERE id = ? AND user_id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM MESSAGES
                          WHERE conversation_id = CONVERSATIONS.id
                      )
                    """,
                    (new_llm_id, conversation_id, current_user.id),
                )
            else:
                await cursor.execute(
                    """
                    UPDATE CONVERSATIONS
                    SET llm_id = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (new_llm_id, conversation_id, current_user.id),
                )
            updated = cursor.rowcount > 0

            await conn.commit()
            await conn.close()

            return JSONResponse(
                content={
                    "success": True,
                    "updated": updated,
                    "llm_id": llm_data[0],
                    "model": llm_data[2],
                }
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error updating conversation model: %s", exc)
        raise HTTPException(status_code=500, detail="Internal server error")
