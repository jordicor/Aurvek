from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from auth import get_current_user, unauthenticated_response
from database import get_db_connection
from log_config import logger
from models import User
from chat.services.privacy import ensure_conversation_privacy_schema

from chat.services.localization import chat_text, chat_error

router = APIRouter()


@router.get("/api/chat-folders")
async def get_chat_folders(current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    try:
        await ensure_conversation_privacy_schema()
        async with get_db_connection(readonly=True) as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT cf.id, cf.name, cf.color, cf.created_at, cf.updated_at,
                           COUNT(c.id) as conversation_count
                    FROM CHAT_FOLDERS cf
                    LEFT JOIN CONVERSATIONS c
                      ON cf.id = c.folder_id
                     AND COALESCE(c.hidden_from_history, 0) = 0
                    WHERE cf.user_id = ?
                    GROUP BY cf.id, cf.name, cf.color, cf.created_at, cf.updated_at
                    ORDER BY cf.created_at ASC
                    """,
                    (current_user.id,),
                )
                folders = await cursor.fetchall()
                return JSONResponse(content={
                    "folders": [
                        {
                            "id": folder[0],
                            "name": folder[1],
                            "color": folder[2],
                            "created_at": folder[3],
                            "updated_at": folder[4],
                            "conversation_count": folder[5],
                        }
                        for folder in folders
                    ]
                })
    except Exception as exc:
        logger.error("Error getting chat folders: %s", exc)
        return JSONResponse(content={"error": chat_text(current_user, "folders_load_failed")}, status_code=500)


@router.post("/api/chat-folders")
async def create_chat_folder(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    try:
        body = await request.json()
        name = body.get("name", "").strip()
        color = body.get("color", "#3B82F6")
        if not name:
            return JSONResponse(content={"error": chat_text(current_user, "folder_name_required")}, status_code=400)

        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT id FROM CHAT_FOLDERS WHERE user_id = ? AND name = ?",
                    (current_user.id, name),
                )
                if await cursor.fetchone():
                    return JSONResponse(content={"error": chat_text(current_user, "folder_name_exists")}, status_code=400)

                await cursor.execute(
                    """
                    INSERT INTO CHAT_FOLDERS (name, user_id, color, created_at, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (name, current_user.id, color),
                )
                await conn.commit()
                return JSONResponse(content={"message": chat_text(current_user, "folder_created")}, status_code=201)
    except Exception as exc:
        logger.error("Error creating chat folder: %s", exc)
        return JSONResponse(content={"error": chat_text(current_user, "folder_create_failed")}, status_code=500)


@router.put("/api/chat-folders/{folder_id}")
async def update_chat_folder(folder_id: int, request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    try:
        body = await request.json()
        name = body.get("name", "").strip()
        color = body.get("color", "#3B82F6")
        if not name:
            return JSONResponse(content={"error": chat_text(current_user, "folder_name_required")}, status_code=400)

        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT id FROM CHAT_FOLDERS WHERE id = ? AND user_id = ?",
                    (folder_id, current_user.id),
                )
                if not await cursor.fetchone():
                    return JSONResponse(content={"error": chat_text(current_user, "folder_not_found")}, status_code=404)

                await cursor.execute(
                    "SELECT id FROM CHAT_FOLDERS WHERE user_id = ? AND name = ? AND id != ?",
                    (current_user.id, name, folder_id),
                )
                if await cursor.fetchone():
                    return JSONResponse(content={"error": chat_text(current_user, "folder_name_exists")}, status_code=400)

                await cursor.execute(
                    """
                    UPDATE CHAT_FOLDERS
                    SET name = ?, color = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND user_id = ?
                    """,
                    (name, color, folder_id, current_user.id),
                )
                await conn.commit()
                return JSONResponse(content={"message": chat_text(current_user, "folder_updated")})
    except Exception as exc:
        logger.error("Error updating chat folder: %s", exc)
        return JSONResponse(content={"error": chat_text(current_user, "folder_update_failed")}, status_code=500)


@router.delete("/api/chat-folders/{folder_id}")
async def delete_chat_folder(folder_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT id, name FROM CHAT_FOLDERS WHERE id = ? AND user_id = ?",
                    (folder_id, current_user.id),
                )
                folder = await cursor.fetchone()
                if not folder:
                    return JSONResponse(content={"error": chat_text(current_user, "folder_not_found")}, status_code=404)

                await cursor.execute(
                    "UPDATE CONVERSATIONS SET folder_id = NULL WHERE folder_id = ?",
                    (folder_id,),
                )
                await cursor.execute(
                    "DELETE FROM CHAT_FOLDERS WHERE id = ? AND user_id = ?",
                    (folder_id, current_user.id),
                )
                await conn.commit()
                return JSONResponse(content={"message": chat_text(current_user, "folder_deleted", name=folder[1])})
    except Exception as exc:
        logger.error("Error deleting chat folder: %s", exc)
        return JSONResponse(content={"error": chat_text(current_user, "folder_delete_failed")}, status_code=500)


@router.post("/api/conversations/{conversation_id}/move-to-folder")
async def move_conversation_to_folder(
    conversation_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()

    try:
        body = await request.json()
        folder_id = body.get("folder_id")

        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT id FROM CONVERSATIONS WHERE id = ? AND user_id = ?",
                    (conversation_id, current_user.id),
                )
                if not await cursor.fetchone():
                    return JSONResponse(content={"error": chat_text(current_user, "conversation_not_found")}, status_code=404)

                if folder_id is not None:
                    await cursor.execute(
                        "SELECT id FROM CHAT_FOLDERS WHERE id = ? AND user_id = ?",
                        (folder_id, current_user.id),
                    )
                    if not await cursor.fetchone():
                        return JSONResponse(content={"error": chat_text(current_user, "folder_not_found")}, status_code=404)

                await cursor.execute(
                    "UPDATE CONVERSATIONS SET folder_id = ? WHERE id = ? AND user_id = ?",
                    (folder_id, conversation_id, current_user.id),
                )
                await conn.commit()
                message = chat_text(current_user, "folder_moved" if folder_id else "folder_removed")
                return JSONResponse(content={"message": message})
    except Exception as exc:
        logger.error("Error moving conversation to folder: %s", exc)
        return JSONResponse(content={"error": chat_text(current_user, "folder_move_failed")}, status_code=500)
