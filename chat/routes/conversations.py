import os
from typing import Optional

import orjson
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from auth import get_current_user, unauthenticated_response
from database import get_db_connection
from log_config import logger
from models import User

from chat.schemas import NewConversationRequest
from chat.services.conversations import create_conversation_core
from chat.services.deletion import (
    close_incognito_conversation_for_user,
    delete_owned_conversation,
)
from chat.services.locks import conversation_write_lock
from chat.services.privacy import (
    ensure_conversation_privacy_schema,
    get_conversation_privacy,
    mark_conversation_incognito,
)
from chat.services.stop_signals import stop_signals
from integrations.devices.service import get_conversation_binding_summaries

router = APIRouter()


async def is_admin(user_id):
    async with get_db_connection(readonly=True) as conn:
        query = """
        SELECT u.role_id, r.role_name
        FROM USERS u
        JOIN USER_ROLES r ON u.role_id = r.id
        WHERE u.id = ?
        """
        try:
            async with conn.execute(query, (user_id,)) as cursor:
                result = await cursor.fetchone()
                return bool(result and result[1].lower() == "admin")
        except Exception as exc:
            logger.error("Error verifying if user is admin: %s", exc)
            return False


def _conversation_not_found() -> HTTPException:
    """Use one response for missing and inaccessible conversation metadata."""
    return HTTPException(status_code=404, detail="Conversation not found")


@router.get("/api/conversations")
async def get_conversations(
    request: Request,
    current_user: User = Depends(get_current_user),
    user_id: Optional[int] = None,
    before_activity: Optional[str] = Query(None),
    before_id: Optional[int] = Query(None),
    limit: int = Query(25, ge=1, le=50),
    folder_id: Optional[int] = None,
):
    if (before_activity is None) != (before_id is None):
        return JSONResponse(content={"error": "before_activity and before_id must both be provided or both omitted"}, status_code=400)

    if current_user is None:
        return unauthenticated_response()

    if user_id is None:
        return JSONResponse(content={"error": "user_id is required"}, status_code=400)

    if current_user.id != user_id and not await is_admin(current_user.id):
        return JSONResponse(content={"error": "Access denied"}, status_code=403)

    await ensure_conversation_privacy_schema()
    async with get_db_connection(readonly=True) as conn:
        async with conn.cursor() as cursor:
            folder_condition = ""
            folder_params = []
            if folder_id is not None:
                folder_condition = " AND c.folder_id = ?"
                folder_params.append(folder_id)
            else:
                folder_condition = " AND (c.folder_id IS NULL OR c.folder_id = 0)"

            external_conversations = []
            external_exclude = ""
            external_exclude_params = []

            await cursor.execute(
                """
                SELECT json_extract(u.external_platforms, '$.whatsapp.conversation_id') as whatsapp_conv_id,
                       json_extract(u.external_platforms, '$.telegram.conversation_id') as telegram_conv_id
                FROM user_details u
                WHERE u.user_id = ?
                """,
                [user_id],
            )
            ext_id_row = await cursor.fetchone()

            external_ids = []
            if ext_id_row:
                for platform, key in [("whatsapp", "whatsapp_conv_id"), ("telegram", "telegram_conv_id")]:
                    conv_id = ext_id_row[key]
                    if conv_id is not None:
                        external_ids.append((platform, conv_id))

            if external_ids:
                placeholders = ",".join(["?" for _ in external_ids])
                external_exclude = f" AND c.id NOT IN ({placeholders})"
                external_exclude_params = [eid for _, eid in external_ids]

                if before_activity is None:
                    for platform, conv_id in external_ids:
                        ext_query = f"""
                            SELECT c.id, c.user_id, c.start_date, c.chat_name, ? as external_platform,
                                   c.locked, l.model as llm_model, COALESCE(p.disable_web_search, 0) as web_search_disabled,
                                   COALESCE(p.force_web_search, 0) as web_search_forced,
                                   p.forced_llm_id, p.hide_llm_name, p.allowed_llms,
                                   COALESCE(p.is_paid, 0) as is_paid,
                                   c.last_activity, c.llm_id, l.machine, c.role_id
                            FROM conversations c
                            JOIN llm l ON c.llm_id = l.id
                            LEFT JOIN prompts p ON c.role_id = p.id
                            WHERE c.id = ? AND c.user_id = ?{folder_condition}
                              AND COALESCE(c.hidden_from_history, 0) = 0
                        """
                        ext_params = [platform, conv_id, user_id] + folder_params
                        await cursor.execute(ext_query, ext_params)
                        ext_conv = await cursor.fetchone()
                        if ext_conv:
                            external_conversations.append(ext_conv)

            normal_limit = max(0, limit - len(external_conversations))
            query = f"""
                SELECT c.id, c.user_id, c.start_date, c.chat_name,
                       NULL as external_platform,
                       c.locked, l.model as llm_model, COALESCE(p.disable_web_search, 0) as web_search_disabled,
                       COALESCE(p.force_web_search, 0) as web_search_forced,
                       p.forced_llm_id, p.hide_llm_name, p.allowed_llms,
                       COALESCE(p.is_paid, 0) as is_paid,
                       c.last_activity, c.llm_id, l.machine, c.role_id
                FROM conversations c
                JOIN llm l ON c.llm_id = l.id
                LEFT JOIN prompts p ON c.role_id = p.id
                WHERE c.user_id = ?{folder_condition}{external_exclude}
                  AND COALESCE(c.hidden_from_history, 0) = 0
            """
            params = [user_id] + folder_params + external_exclude_params

            if before_activity is not None:
                query += " AND (c.last_activity < ? OR (c.last_activity = ? AND c.id < ?))"
                params.extend([before_activity, before_activity, before_id])

            query += " ORDER BY c.last_activity DESC, c.id DESC LIMIT ?"
            params.append(normal_limit)

            await cursor.execute(query, params)
            conversations = await cursor.fetchall()
            all_conversations = list(external_conversations) + list(conversations)
            binding_summaries = await get_conversation_binding_summaries(
                user_id,
                [conv[0] for conv in all_conversations if not conv[4]],
            )

            return JSONResponse(content=[
                {
                    "id": conv[0],
                    "user_id": conv[1],
                    "start_date": conv[2],
                    "chat_name": conv[3] if conv[3] else "New Chat",
                    "external_platform": conv[4],
                    "locked": bool(conv[5]) if conv[5] is not None else False,
                    "llm_model": conv[6],
                    "web_search_allowed": not bool(conv[7]),
                    "web_search_forced": bool(conv[8]),
                    "forced_llm_id": conv[9],
                    "hide_llm_name": bool(conv[10]) if conv[10] else False,
                    "allowed_llms": orjson.loads(conv[11]) if conv[11] else None,
                    "is_paid": bool(conv[12]),
                    "last_activity": conv[13],
                    "llm_id": conv[14],
                    "machine": conv[15],
                    "prompt_id": conv[16],
                    "external_bindings": (
                        None if conv[4] else binding_summaries.get(int(conv[0]))
                    ),
                }
                for conv in all_conversations
            ])


@router.post("/api/conversations/new")
async def start_new_conversation(
    request: NewConversationRequest = NewConversationRequest(),
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()

    logger.info(
        "[NEW] CREATING NEW CONVERSATION - User: %s, folder_id: %s, prompt_id: %s, incognito: %s",
        current_user.username,
        request.folder_id,
        request.prompt_id,
        request.incognito,
    )

    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        await ensure_conversation_privacy_schema(conn)

        if request.folder_id is not None and not request.incognito:
            await cursor.execute(
                "SELECT id FROM CHAT_FOLDERS WHERE id = ? AND user_id = ?",
                (request.folder_id, current_user.id),
            )
            if not await cursor.fetchone():
                raise HTTPException(status_code=400, detail="Invalid folder_id or folder does not belong to user")

        try:
            conversation_id = await create_conversation_core(
                current_user.id,
                cursor,
                current_user,
                prompt_id=request.prompt_id,
                llm_id=request.llm_id,
                folder_id=None if request.incognito else request.folder_id,
                strict_prompt_access=True,
            )
        except PermissionError:
            raise HTTPException(status_code=403, detail="Access denied to this prompt")
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        if request.incognito:
            await mark_conversation_incognito(
                conn,
                conversation_id=conversation_id,
                user_id=current_user.id,
                incognito=True,
            )

        await cursor.execute(
            """
            SELECT c.last_activity, c.llm_id, c.role_id, c.active_extension_id,
                   l.machine, l.model,
                   p.name, p.forced_llm_id, p.allowed_llms, p.hide_llm_name,
                   COALESCE(p.extensions_enabled, 0), COALESCE(p.is_paid, 0),
                   COALESCE(p.disable_web_search, 0), COALESCE(p.force_web_search, 0),
                   COALESCE(p.extensions_free_selection, 1)
            FROM CONVERSATIONS c
            LEFT JOIN LLM l ON l.id = c.llm_id
            LEFT JOIN PROMPTS p ON p.id = c.role_id
            WHERE c.id = ? AND c.user_id = ?
            """,
            (conversation_id, current_user.id),
        )
        conversation_row = await cursor.fetchone()
        if not conversation_row:
            raise HTTPException(status_code=500, detail="Failed to load new conversation")

        conversation_last_activity = conversation_row[0]
        llm_id = conversation_row[1]
        prompt_id = conversation_row[2]
        active_extension_id = conversation_row[3]
        machine = conversation_row[4]
        llm_model = conversation_row[5]
        prompt_name = conversation_row[6]
        forced_llm_id_value = conversation_row[7]
        allowed_llms_value = conversation_row[8]
        hide_llm_name_value = conversation_row[9]
        extensions_enabled_value = bool(conversation_row[10])
        is_paid_value = bool(conversation_row[11])
        disable_web_search_value = bool(conversation_row[12])
        force_web_search_value = bool(conversation_row[13])
        extensions_free_selection = bool(conversation_row[14])

        active_extension_data = None
        extensions_list = []
        if extensions_enabled_value and prompt_id:
            if active_extension_id:
                await cursor.execute(
                    "SELECT id, name, slug, description FROM PROMPT_EXTENSIONS WHERE id = ?",
                    (active_extension_id,),
                )
                ext_row = await cursor.fetchone()
                if ext_row:
                    active_extension_data = {
                        "id": ext_row[0],
                        "name": ext_row[1],
                        "slug": ext_row[2],
                        "description": ext_row[3] or "",
                    }

            await cursor.execute(
                """
                SELECT id, name, slug, description
                FROM PROMPT_EXTENSIONS
                WHERE prompt_id = ?
                ORDER BY display_order
                """,
                (prompt_id,),
            )
            ext_rows = await cursor.fetchall()
            extensions_list = [
                {"id": row[0], "name": row[1], "slug": row[2], "description": row[3] or ""}
                for row in ext_rows
            ]

        await conn.commit()

        response_data = {
            "id": conversation_id,
            "name": "New Chat",
            "machine": machine,
            "prompt_name": prompt_name,
            "locked": False,
            "llm_id": llm_id,
            "llm_model": llm_model,
            "prompt_id": prompt_id,
            "forced_llm_id": forced_llm_id_value,
            "hide_llm_name": bool(hide_llm_name_value) if hide_llm_name_value else False,
            "allowed_llms": orjson.loads(allowed_llms_value) if allowed_llms_value else None,
            "extensions_enabled": extensions_enabled_value,
            "is_paid": is_paid_value,
            "web_search_allowed": not disable_web_search_value,
            "web_search_forced": force_web_search_value,
            "last_activity": conversation_last_activity,
            "is_incognito": bool(request.incognito),
            "hidden_from_history": bool(request.incognito),
            "purge_on_close": bool(request.incognito),
        }
        if extensions_enabled_value:
            response_data["active_extension"] = active_extension_data
            response_data["extensions"] = extensions_list
            response_data["extensions_free_selection"] = extensions_free_selection

        return JSONResponse(content=response_data, status_code=201)


@router.patch("/api/conversations/{conversation_id}/extension")
async def update_conversation_extension(
    conversation_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    data = await request.json()
    extension_id = data.get("extension_id")

    async with conversation_write_lock(conversation_id):
        async with get_db_connection() as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                """
                SELECT c.id, c.role_id, c.active_extension_id,
                       p.extensions_enabled, p.extensions_free_selection
                FROM CONVERSATIONS c
                LEFT JOIN PROMPTS p ON c.role_id = p.id
                WHERE c.id = ? AND c.user_id = ?
                """,
                (conversation_id, current_user.id),
            )
            conv = await cursor.fetchone()
            if not conv:
                raise HTTPException(status_code=404, detail="Conversation not found")
            if not conv["extensions_enabled"]:
                raise HTTPException(status_code=400, detail="Extensions are not enabled for this prompt")

            prompt_id = conv["role_id"]
            if extension_id is None:
                await cursor.execute(
                    "UPDATE CONVERSATIONS SET active_extension_id = NULL WHERE id = ?",
                    (conversation_id,),
                )
                await conn.commit()
                return JSONResponse(content={"success": True, "extension": None})

            await cursor.execute(
                "SELECT id, name, slug, description, display_order FROM PROMPT_EXTENSIONS WHERE id = ? AND prompt_id = ?",
                (extension_id, prompt_id),
            )
            target_ext = await cursor.fetchone()
            if not target_ext:
                raise HTTPException(status_code=404, detail="Extension not found for this prompt")

            if not conv["extensions_free_selection"] and conv["active_extension_id"] is not None:
                await cursor.execute(
                    "SELECT display_order FROM PROMPT_EXTENSIONS WHERE id = ?",
                    (conv["active_extension_id"],),
                )
                current_ext = await cursor.fetchone()
                if current_ext:
                    current_order = current_ext["display_order"]
                    target_order = target_ext["display_order"]
                    if abs(target_order - current_order) > 1:
                        raise HTTPException(status_code=400, detail="Sequential mode: can only move one level at a time")

            await cursor.execute(
                "UPDATE CONVERSATIONS SET active_extension_id = ? WHERE id = ?",
                (extension_id, conversation_id),
            )
            await conn.commit()

            return JSONResponse(content={
                "success": True,
                "extension": {
                    "id": target_ext["id"],
                    "name": target_ext["name"],
                    "slug": target_ext["slug"],
                    "description": target_ext["description"] or "",
                },
            })


@router.post("/api/conversations/{conversation_id}/stop")
async def stop_message(conversation_id: int, current_user: User = Depends(get_current_user)):
    if not current_user:
        raise HTTPException(status_code=401, detail="User not authenticated")

    async with get_db_connection() as conn:
        async with conn.execute("SELECT user_id FROM conversations WHERE id = ?", (conversation_id,)) as cursor:
            conversation = await cursor.fetchone()

    if not conversation or conversation[0] != current_user.id:
        raise HTTPException(status_code=403, detail="You do not have permission to stop this conversation")

    stop_signals[conversation_id] = True

    try:
        from rediscfg import redis_client as _redis
        await _redis.set(f"gransabio:stop:{conversation_id}", "1", ex=300)
        session_id = await _redis.get(f"gransabio:session:{conversation_id}")
        if session_id:
            import httpx
            from gransabio_config import get_gransabio_config
            cfg = await get_gransabio_config()
            gs_url = cfg.get("gransabio_url", "")
            if gs_url:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(f"{gs_url}/stop/{session_id}")
    except Exception:
        pass

    return {"success": True, "message": "Stop signal sent."}


@router.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    result = await delete_owned_conversation(current_user, conversation_id)
    if not result.get("success"):
        return JSONResponse(content={"error": result["error"]}, status_code=result["status_code"])
    return JSONResponse(content={"success": True}, status_code=200)


@router.post("/api/conversations/{conversation_id}/incognito/close")
async def close_incognito_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()

    privacy = await get_conversation_privacy(conversation_id, user_id=current_user.id)
    if privacy is None:
        return JSONResponse(content={"success": True, "already_closed": True})
    if not bool(privacy.get("is_incognito")):
        return JSONResponse(
            content={"success": False, "error": "Conversation is not incognito"},
            status_code=400,
        )

    try:
        result = await close_incognito_conversation_for_user(current_user, privacy)
    except ValueError as exc:
        return JSONResponse(content={"success": False, "error": str(exc)}, status_code=400)
    return JSONResponse(content=result)


@router.post("/api/conversations/{conversation_id}/rename")
async def rename_conversation(
    conversation_id: int,
    new_name: str = Body(..., embed=True),
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    new_name = new_name[:256]
    async with get_db_connection() as conn:
        async with conn.execute("SELECT user_id FROM conversations WHERE id = ?", (conversation_id,)) as cursor:
            result = await cursor.fetchone()
            if not result or result[0] != current_user.id:
                raise HTTPException(status_code=403, detail="Not authorized to rename this conversation")

        await conn.execute(
            "UPDATE conversations SET chat_name = ? WHERE id = ?",
            (new_name, conversation_id),
        )
        await conn.commit()

    return {"success": True}


@router.get("/api/conversations/{conversation_id}/last_message_id")
async def get_last_message_id(conversation_id: int, current_user: User = Depends(get_current_user)):
    logger.info("enters get_last_message_id")
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    admin_access = await is_admin(current_user.id)
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """
            SELECT (
                SELECT m.id
                FROM MESSAGES m
                WHERE m.conversation_id = c.id
                ORDER BY m.date DESC, m.id DESC
                LIMIT 1
            ) AS message_id
            FROM CONVERSATIONS c
            WHERE c.id = ? AND (c.user_id = ? OR ? = 1)
            """,
            (conversation_id, current_user.id, int(admin_access)),
        )
        result = await cursor.fetchone()

    if not result:
        raise _conversation_not_found()
    return {"message_id": result[0]}


@router.get("/api/conversations/{conversation_id}/status")
async def conversation_status(conversation_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    admin_access = await is_admin(current_user.id)
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT id FROM CONVERSATIONS
            WHERE id = ? AND (user_id = ? OR ? = 1)
            """,
            (conversation_id, current_user.id, int(admin_access)),
        )
        conversation = await cursor.fetchone()

    if not conversation:
        raise _conversation_not_found()
    return JSONResponse(content={"isActive": True}, status_code=200)


@router.get("/api/conversations/{conversation_id}/web-search-status")
async def get_web_search_status(conversation_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    perplexity_available = bool(os.getenv("PERPLEXITY_API_KEY"))
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """
            SELECT
                COALESCE(p.disable_web_search, 0) as prompt_disabled,
                COALESCE(p.force_web_search, 0) as prompt_forced,
                COALESCE(ud.web_search_enabled, 1) as user_enabled,
                COALESCE(ud.web_search_mode, 'native') as web_search_mode
            FROM CONVERSATIONS c
            LEFT JOIN PROMPTS p ON c.role_id = p.id
            LEFT JOIN USER_DETAILS ud ON ud.user_id = c.user_id
            WHERE c.id = ? AND c.user_id = ?
            """,
            (conversation_id, current_user.id),
        )
        row = await cursor.fetchone()
    if row:
        prompt_disabled, prompt_forced, user_enabled, web_search_mode = row
        return JSONResponse(content={
            "allowed_by_prompt": not bool(prompt_disabled),
            "web_search_forced": bool(prompt_forced),
            "user_enabled": bool(user_enabled),
            "web_search_mode": web_search_mode,
            "perplexity_available": perplexity_available,
        })
    return JSONResponse(content={
        "allowed_by_prompt": True,
        "web_search_forced": False,
        "user_enabled": True,
        "web_search_mode": "native",
        "perplexity_available": perplexity_available,
    })


@router.post("/api/user/web-search-toggle")
async def toggle_web_search(current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    async with get_db_connection() as conn:
        cursor = await conn.execute(
            "SELECT COALESCE(web_search_enabled, 1) FROM USER_DETAILS WHERE user_id = ?",
            (current_user.id,),
        )
        row = await cursor.fetchone()
        current_value = row[0] if row else 1
        new_value = 0 if current_value else 1
        await conn.execute(
            "UPDATE USER_DETAILS SET web_search_enabled = ? WHERE user_id = ?",
            (new_value, current_user.id),
        )
        await conn.commit()
        return JSONResponse(content={"web_search_enabled": bool(new_value)})


@router.post("/api/user/web-search-mode")
async def set_web_search_mode(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    try:
        data = await request.json()
    except Exception:
        return JSONResponse(content={"error": "Invalid request body"}, status_code=400)

    mode = data.get("mode")
    if mode not in ("native", "perplexity"):
        return JSONResponse(content={"error": "Invalid mode. Must be 'native' or 'perplexity'"}, status_code=400)

    async with get_db_connection() as conn:
        await conn.execute(
            "UPDATE USER_DETAILS SET web_search_mode = ? WHERE user_id = ?",
            (mode, current_user.id),
        )
        await conn.commit()
        return JSONResponse(content={"web_search_mode": mode})


@router.get("/api/user/web-search-settings")
async def get_web_search_settings(current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    perplexity_available = bool(os.getenv("PERPLEXITY_API_KEY"))
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """
            SELECT
                COALESCE(web_search_mode, 'native') as web_search_mode,
                COALESCE(web_search_enabled, 1) as web_search_enabled,
                COALESCE(web_search_show_sources, 1) as web_search_show_sources,
                COALESCE(web_search_inline_citations, 0) as web_search_inline_citations
            FROM USER_DETAILS WHERE user_id = ?
            """,
            (current_user.id,),
        )
        row = await cursor.fetchone()

    if row:
        return JSONResponse(content={
            "web_search_mode": row[0],
            "web_search_enabled": bool(row[1]),
            "web_search_show_sources": bool(row[2]),
            "web_search_inline_citations": bool(row[3]),
            "perplexity_available": perplexity_available,
        })
    return JSONResponse(content={
        "web_search_mode": "native",
        "web_search_enabled": True,
        "web_search_show_sources": True,
        "web_search_inline_citations": False,
        "perplexity_available": perplexity_available,
    })


@router.post("/api/user/web-search-settings")
async def update_web_search_settings(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    try:
        data = await request.json()
    except Exception:
        return JSONResponse(content={"error": "Invalid request body"}, status_code=400)

    updates = []
    params = []

    mode = data.get("web_search_mode")
    if mode is not None:
        if mode not in ("native", "perplexity"):
            return JSONResponse(content={"error": "Invalid mode. Must be 'native' or 'perplexity'"}, status_code=400)
        updates.append("web_search_mode = ?")
        params.append(mode)

    show_sources = data.get("web_search_show_sources")
    if show_sources is not None:
        updates.append("web_search_show_sources = ?")
        params.append(1 if show_sources else 0)

    inline_citations = data.get("web_search_inline_citations")
    if inline_citations is not None:
        updates.append("web_search_inline_citations = ?")
        params.append(1 if inline_citations else 0)

    if not updates:
        return JSONResponse(content={"error": "No valid fields to update"}, status_code=400)

    params.append(current_user.id)
    async with get_db_connection() as conn:
        await conn.execute(
            f"UPDATE USER_DETAILS SET {', '.join(updates)} WHERE user_id = ?",
            tuple(params),
        )
        await conn.commit()

    return JSONResponse(content={"updated": True})
