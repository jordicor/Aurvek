import math
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from admin_audit import log_admin_action
from auth import get_current_user, unauthenticated_response
from captcha_service import get_captcha_config
from common import GOOGLE_CLIENT_ID, get_template_context, templates
from database import get_db_connection
from log_config import logger
from i18n import Translator, get_translator
from models import User

from chat.routes.conversations import is_admin
from chat.services.deletion import delete_conversation_folder, delete_conversation_recursively
from chat.services.locks import conversation_write_lock
from chat.services.privacy import ensure_conversation_privacy_schema
from chat.services.stop_signals import stop_signals

router = APIRouter()
static_directory = Path("data/static")


@router.get("/admin/chat", response_class=HTMLResponse)
async def admin_conversations(request: Request, current_user: User = Depends(get_current_user)):
    translator = get_translator(request, current_user)
    if current_user is None:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "captcha": get_captcha_config(),
                "google_oauth_available": bool(GOOGLE_CLIENT_ID),
            },
        )
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail=translator.t('chat_errors.access_denied'))
    await ensure_conversation_privacy_schema()

    f_search = request.query_params.get("search", "").strip()
    f_user_exact = request.query_params.get("user_exact", "").strip()
    f_prompt_id = request.query_params.get("prompt_id", "").strip()
    f_locked = request.query_params.get("locked", "").strip()
    f_conversation_id = request.query_params.get("conversation_id", "").strip()
    f_date_from = request.query_params.get("date_from", "").strip()
    f_date_to = request.query_params.get("date_to", "").strip()
    f_min_tokens = request.query_params.get("min_tokens", "").strip()

    allowed_per_page = [25, 50, 100]
    try:
        per_page = int(request.query_params.get("per_page", "25"))
    except (ValueError, TypeError):
        per_page = 25
    if per_page not in allowed_per_page:
        per_page = 25

    try:
        page = int(request.query_params.get("page", "1"))
    except (ValueError, TypeError):
        page = 1
    if page < 1:
        page = 1

    allowed_sort_columns = {
        "id": "c.id",
        "chat_name": "c.chat_name",
        "username": "u.username",
        "prompt_name": "p.name",
        "last_activity": "c.last_activity",
        "start_date": "c.start_date",
        "message_count": "message_count",
        "total_tokens": "total_tokens",
        "locked": "c.locked",
    }
    allowed_sort_dirs = {"asc", "desc"}

    sort_by = request.query_params.get("sort_by", "last_activity").strip()
    sort_dir = request.query_params.get("sort_dir", "desc").strip().lower()
    if sort_by not in allowed_sort_columns:
        sort_by = "last_activity"
    if sort_dir not in allowed_sort_dirs:
        sort_dir = "desc"

    sort_column = allowed_sort_columns[sort_by]
    conditions = ["COALESCE(c.hidden_from_history, 0) = 0"]
    params = []

    if f_user_exact:
        conditions.append("u.username = ?")
        params.append(f_user_exact)
    elif f_search:
        escaped = f_search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append("(u.username LIKE ? ESCAPE '\\' OR c.chat_name LIKE ? ESCAPE '\\')")
        params.append(f"%{escaped}%")
        params.append(f"%{escaped}%")

    if f_prompt_id.isdigit():
        conditions.append("c.role_id = ?")
        params.append(int(f_prompt_id))

    if f_locked == "locked":
        conditions.append("c.locked = 1")
    elif f_locked == "unlocked":
        conditions.append("(c.locked = 0 OR c.locked IS NULL)")

    if f_conversation_id.isdigit():
        conditions.append("c.id = ?")
        params.append(int(f_conversation_id))

    if f_date_from:
        conditions.append("c.start_date >= ?")
        params.append(f_date_from)

    if f_date_to:
        conditions.append("c.start_date <= ?")
        params.append(f_date_to + " 23:59:59")

    needs_messages_in_where = False
    min_tokens_val = 0
    if f_min_tokens.isdigit() and int(f_min_tokens) > 0:
        needs_messages_in_where = True
        min_tokens_val = int(f_min_tokens)

    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params_tuple = tuple(params)
    needs_messages_join = sort_by in ("message_count", "total_tokens") or needs_messages_in_where

    async with get_db_connection(readonly=True) as conn:
        if needs_messages_in_where:
            count_sql = f"""SELECT COUNT(*) FROM CONVERSATIONS c
                JOIN USERS u ON c.user_id = u.id
                LEFT JOIN (
                    SELECT conversation_id, SUM(input_tokens_used + output_tokens_used) AS total_tokens
                    FROM MESSAGES GROUP BY conversation_id
                ) ms ON ms.conversation_id = c.id
                {where_clause}"""
            extra_cond = " AND " if conditions else " WHERE "
            count_sql += f"{extra_cond}COALESCE(ms.total_tokens, 0) >= ?"
            count_params = params_tuple + (min_tokens_val,)
        else:
            count_sql = f"SELECT COUNT(*) FROM CONVERSATIONS c JOIN USERS u ON c.user_id = u.id{where_clause}"
            count_params = params_tuple

        cursor = await conn.execute(count_sql, count_params)
        total_conversations = (await cursor.fetchone())[0]
        total_pages = max(1, math.ceil(total_conversations / per_page))
        if page > total_pages:
            page = total_pages
        offset = (page - 1) * per_page

        data_sql = f"""SELECT
            c.id, c.chat_name, c.locked, c.locked_reason,
            c.start_date, c.last_activity,
            u.username, u.id AS user_id,
            p.name AS prompt_name, p.id AS prompt_id,
            l.machine AS llm_machine, l.model AS llm_model,
            COALESCE(ms.message_count, 0) AS message_count,
            COALESCE(ms.total_tokens, 0) AS total_tokens
        FROM CONVERSATIONS c
        JOIN USERS u ON c.user_id = u.id
        LEFT JOIN PROMPTS p ON c.role_id = p.id
        LEFT JOIN LLM l ON c.llm_id = l.id
        LEFT JOIN (
            SELECT conversation_id,
                   COUNT(*) AS message_count,
                   SUM(input_tokens_used + output_tokens_used) AS total_tokens
            FROM MESSAGES
            GROUP BY conversation_id
        ) ms ON ms.conversation_id = c.id
        {where_clause}"""

        data_params = list(params_tuple)
        if needs_messages_in_where:
            extra_cond = " AND " if conditions else " WHERE "
            data_sql += f"{extra_cond}COALESCE(ms.total_tokens, 0) >= ?"
            data_params.append(min_tokens_val)

        data_sql += f" ORDER BY {sort_column} {sort_dir} LIMIT ? OFFSET ?"
        data_params.extend([per_page, offset])
        cursor = await conn.execute(data_sql, tuple(data_params))
        conversations = [dict(row) for row in await cursor.fetchall()]

        locked_sql = f"SELECT COUNT(*) FROM CONVERSATIONS c JOIN USERS u ON c.user_id = u.id{where_clause}"
        locked_cond = " AND " if conditions else " WHERE "
        cursor = await conn.execute(f"{locked_sql}{locked_cond}c.locked = 1", params_tuple)
        locked_count = (await cursor.fetchone())[0]

        cursor = await conn.execute("SELECT id, name FROM PROMPTS ORDER BY name ASC")
        prompts = [dict(row) for row in await cursor.fetchall()]

    showing_start = offset + 1 if total_conversations > 0 else 0
    showing_end = offset + len(conversations)
    context = await get_template_context(request, current_user)
    context.update({
        "conversations": conversations,
        "prompts": prompts,
        "stats": {"locked": locked_count, "total": total_conversations},
        "filters": {
            "search": f_search or "",
            "user_exact": f_user_exact or "",
            "prompt_id": int(f_prompt_id) if f_prompt_id.isdigit() else None,
            "locked": f_locked or "",
            "conversation_id": f_conversation_id or "",
            "date_from": f_date_from or "",
            "date_to": f_date_to or "",
            "min_tokens": f_min_tokens or "",
        },
        "sort_by": sort_by,
        "sort_dir": sort_dir,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "showing_start": showing_start,
        "showing_end": showing_end,
    })
    return templates.TemplateResponse("admin_chat.html", context)


@router.get("/api/admin/conversations")
async def get_all_conversations(request: Request, current_user: User = Depends(get_current_user)):
    translator = get_translator(request, current_user)
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request})
    if not await current_user.is_admin:
        return JSONResponse(content={"error": translator.t('chat_errors.access_denied')}, status_code=403)

    await log_admin_action(
        admin_id=current_user.id,
        action_type="list_all_conversations",
        request=request,
        target_resource_type="conversations",
        details="Admin listed all user conversations",
    )

    await ensure_conversation_privacy_schema()
    async with get_db_connection(readonly=True) as conn:
        query = """
            SELECT c.id, u.username, c.start_date, u.id as user_id,
                COALESCE(SUM(m.input_tokens_used + m.output_tokens_used), 0) as total_tokens_used,
                c.last_activity
            FROM conversations c
            JOIN users u ON c.user_id = u.id
            LEFT JOIN messages m ON c.id = m.conversation_id
            WHERE COALESCE(c.hidden_from_history, 0) = 0
            GROUP BY c.id
            ORDER BY c.last_activity DESC
        """
        conversations = await conn.execute_fetchall(query)
    return JSONResponse(content=[
        {
            "id": conv[0],
            "username": conv[1],
            "start_date": conv[2],
            "user_id": conv[3],
            "total_tokens_used": conv[4],
            "last_activity": conv[5],
        }
        for conv in conversations
    ])


@router.get("/api/admin/users/autocomplete")
async def admin_users_autocomplete(q: str = "", current_user: User = Depends(get_current_user)):
    translator = Translator(getattr(current_user, "ui_language", None))
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail=translator.t('chat_errors.access_denied'))
    if len(q) < 2:
        return []
    async with get_db_connection(readonly=True) as db:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = await db.execute_fetchall(
            "SELECT username FROM USERS WHERE username LIKE ? ESCAPE '\\' LIMIT 10",
            (f"%{escaped}%",),
        )
        return [row["username"] for row in rows]


@router.post("/api/conversations/{conversation_id}/lock")
async def toggle_conversation_lock(
    conversation_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    translator = get_translator(request, current_user)
    if current_user is None:
        return unauthenticated_response()
    if not await is_admin(current_user.id):
        return JSONResponse(content={"error": translator.t('management_operations_errors.admin_required')}, status_code=403)

    try:
        data = await request.json()
        lock = data.get("lock", True)
    except Exception:
        return JSONResponse(content={"error": translator.t('chat_errors.invalid_request')}, status_code=400)

    async with conversation_write_lock(conversation_id):
        async with get_db_connection() as conn:
            cursor = await conn.cursor()
            await cursor.execute("SELECT id FROM conversations WHERE id = ?", (conversation_id,))
            result = await cursor.fetchone()
            if not result:
                return JSONResponse(content={"error": translator.t('chat_errors.conversation_not_found')}, status_code=404)

        if lock:
            from tools.watchdog import _finalize_conversation_lock
            await _finalize_conversation_lock(conversation_id, "ADMIN_MANUAL", insert_system_message=False)
            stop_signals[conversation_id] = True
            try:
                from rediscfg import redis_client as _redis
                await _redis.set(f"gransabio:stop:{conversation_id}", "1", ex=300)
            except Exception:
                pass
            await log_admin_action(
                admin_id=current_user.id,
                action_type="lock_conversation",
                target_resource_id=conversation_id,
                details="Manual admin lock",
            )
        else:
            async with get_db_connection() as conn:
                await conn.execute(
                    "UPDATE conversations SET locked = FALSE, locked_reason = NULL WHERE id = ?",
                    (conversation_id,),
                )
                await conn.execute(
                    """UPDATE WATCHDOG_STATE
                       SET pending_hint = NULL, hint_severity = NULL,
                           consecutive_hint_count = 0, pending_hint_event_type = NULL
                       WHERE conversation_id = ?""",
                    (conversation_id,),
                )
                await conn.commit()
            stop_signals.pop(conversation_id, None)
            try:
                from rediscfg import redis_client as _redis
                await _redis.delete(f"gransabio:stop:{conversation_id}")
            except Exception:
                pass
            await log_admin_action(
                admin_id=current_user.id,
                action_type="unlock_conversation",
                target_resource_id=conversation_id,
                details="Manual admin unlock",
            )

    logger.info(
        "[toggle_conversation_lock] Conversation %s %s by admin %s",
        conversation_id,
        "locked" if lock else "unlocked",
        current_user.username,
    )
    return JSONResponse(content={"success": True, "locked": lock}, status_code=200)


@router.post("/admin/api/conversations/bulk_lock")
async def bulk_lock_conversations(request: Request, current_user: User = Depends(get_current_user)):
    translator = get_translator(request, current_user)
    if current_user is None:
        return unauthenticated_response()
    if not await is_admin(current_user.id):
        return JSONResponse(content={"error": translator.t('management_operations_errors.admin_required')}, status_code=403)

    try:
        data = await request.json()
        conversation_ids = data.get("conversation_ids", [])
        lock = data.get("lock", True)
    except Exception:
        return JSONResponse(content={"error": translator.t('chat_errors.invalid_request')}, status_code=400)

    if not conversation_ids or not isinstance(conversation_ids, list):
        return JSONResponse(content={"error": translator.t('management_operations_errors.conversation_ids_required')}, status_code=400)

    processed = 0
    for conv_id in conversation_ids:
        try:
            conv_id = int(conv_id)
        except (ValueError, TypeError):
            continue

        if lock:
            from tools.watchdog import _finalize_conversation_lock
            await _finalize_conversation_lock(conv_id, "ADMIN_MANUAL", insert_system_message=False)
            stop_signals[conv_id] = True
            try:
                from rediscfg import redis_client as _redis
                await _redis.set(f"gransabio:stop:{conv_id}", "1", ex=300)
            except Exception:
                pass
        else:
            async with get_db_connection() as conn:
                await conn.execute(
                    "UPDATE conversations SET locked = FALSE, locked_reason = NULL WHERE id = ?",
                    (conv_id,),
                )
                await conn.execute(
                    """UPDATE WATCHDOG_STATE
                       SET pending_hint = NULL, hint_severity = NULL,
                           consecutive_hint_count = 0, pending_hint_event_type = NULL
                       WHERE conversation_id = ?""",
                    (conv_id,),
                )
                await conn.commit()
            stop_signals.pop(conv_id, None)
            try:
                from rediscfg import redis_client as _redis
                await _redis.delete(f"gransabio:stop:{conv_id}")
            except Exception:
                pass
        processed += 1

    action_type = "bulk_lock_conversations" if lock else "bulk_unlock_conversations"
    await log_admin_action(
        admin_id=current_user.id,
        action_type=action_type,
        details=f"{'Locked' if lock else 'Unlocked'} {processed} conversations",
    )
    return JSONResponse(content={"success": True, "processed": processed})


@router.delete("/admin/api/conversations/{conversation_id}")
async def delete_conversation_absolute(conversation_id: int, current_user: User = Depends(get_current_user)):
    translator = Translator(getattr(current_user, "ui_language", None))
    if current_user is None:
        return unauthenticated_response()
    if await is_admin(current_user.id):
        user_id = await delete_conversation_recursively(conversation_id)
        if user_id:
            success = await delete_conversation_folder(static_directory, user_id, conversation_id)
            if success:
                return JSONResponse(content={"message": translator.t('management_operations_errors.conversation_deleted')})
            return JSONResponse(content={"message": translator.t('management_operations_errors.conversation_folder_failed')}, status_code=500)
        return JSONResponse(content={"message": translator.t('chat_errors.conversation_not_found')}, status_code=404)
    return unauthenticated_response()


@router.post("/admin/api/conversations/bulk_delete")
async def delete_multiple_conversations(request: Request, current_user: User = Depends(get_current_user)):
    translator = get_translator(request, current_user)
    if current_user is None:
        return unauthenticated_response()
    if not await is_admin(current_user.id):
        return unauthenticated_response()

    body = await request.json()
    conversation_ids = body.get("conversation_ids")
    if not conversation_ids:
        return JSONResponse(content={"error": translator.t('management_operations_errors.conversation_ids_required')}, status_code=400)

    failed_conversations = []
    for conversation_id in conversation_ids:
        user_id = await delete_conversation_recursively(conversation_id)
        if user_id:
            success = await delete_conversation_folder(static_directory, user_id, conversation_id)
            if not success:
                failed_conversations.append(conversation_id)

    if failed_conversations:
        return JSONResponse(
            content={
                "message": translator.t('management_operations_errors.conversations_folder_failed'),
                "failed_conversations": failed_conversations,
            },
            status_code=500,
        )
    return JSONResponse(content={"message": translator.t('management_operations_errors.conversations_deleted')})
