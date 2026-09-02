from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from auth import get_current_user, unauthenticated_response
from common import custom_unescape
from database import get_db_connection
from models import User

from chat.services.message_rendering import process_message
from integrations.messaging_voice_notes.service import load_message_channel_provenance

router = APIRouter()


@router.post("/api/conversations/{conversation_id}/bookmark")
async def bookmark_message(
    conversation_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if not current_user:
        return unauthenticated_response()

    data = await request.json()
    message_id = data.get("message_id")
    action = data.get("action")

    async with get_db_connection() as conn:
        async with conn.execute("SELECT user_id FROM conversations WHERE id = ?", (conversation_id,)) as cursor:
            conversation = await cursor.fetchone()

        if not conversation or conversation[0] != current_user.id:
            raise HTTPException(status_code=403, detail="You do not have permission to mark this conversation")

        is_bookmarked = 1 if action == "add" else 0
        await conn.execute(
            "UPDATE MESSAGES SET is_bookmarked = ? WHERE id = ? AND conversation_id = ?",
            (is_bookmarked, message_id, conversation_id),
        )
        await conn.commit()

    return JSONResponse(content={"success": True})


@router.get("/api/bookmarks")
async def get_bookmarked_messages(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT m.id, m.conversation_id, m.user_id, u.username, m.message, m.type,
                   strftime('%Y-%m-%d %H:%M:%S', m.date) as date_utc,
                   COALESCE(c.chat_name, 'Chat ' || m.conversation_id) as chat_name
            FROM MESSAGES m
            JOIN USERS u ON m.user_id = u.id
            LEFT JOIN CONVERSATIONS c ON m.conversation_id = c.id
            WHERE m.user_id = ? AND m.is_bookmarked = 1
              AND COALESCE(c.hidden_from_history, 0) = 0
            ORDER BY m.conversation_id DESC, m.date ASC
            """,
            (current_user.id,),
        )
        messages = await cursor.fetchall()
        channel_provenance = await load_message_channel_provenance(
            [int(message["id"]) for message in messages]
        )

        messages_list = []
        for msg in messages:
            processed_message = await process_message(
                custom_unescape(msg["message"]),
                request,
                current_user,
                media_owner_username=msg["username"],
            )
            message_data = {
                "id": msg["id"],
                "conversation_id": msg["conversation_id"],
                "user_id": msg["user_id"],
                "username": msg["username"],
                "message": processed_message,
                "type": msg["type"],
                "date": msg["date_utc"],
                "is_bookmarked": True,
                "chat_name": msg["chat_name"],
            }
            message_data.update(channel_provenance.get(int(msg["id"]), {}))
            messages_list.append(message_data)

    return JSONResponse(content=messages_list)
