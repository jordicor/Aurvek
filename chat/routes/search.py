from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from auth import get_current_user, unauthenticated_response
from database import get_db_connection
from models import User

from chat.services.privacy import ensure_conversation_privacy_schema
from chat.services.search import build_fts_query, execute_search
from chat.services.localization import chat_translator

router = APIRouter()


@router.get("/api/messages/search")
async def search_messages(
    request: Request,
    current_user: User = Depends(get_current_user),
    q: str = Query(..., min_length=3),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    conversation_id: int = Query(None),
):
    if current_user is None:
        return unauthenticated_response()

    fts_query = build_fts_query(q)
    if not fts_query:
        return JSONResponse(content={"query": q, "has_more": False, "next_offset": 0, "items": []})

    await ensure_conversation_privacy_schema()
    async with get_db_connection(readonly=True) as conn:
        items = await execute_search(conn, current_user.id, fts_query, limit + 1, offset, conversation_id)

    has_more = len(items) > limit
    if has_more:
        items = items[:limit]
    translator = chat_translator(current_user)
    for item in items:
        item["chat_name"] = item["chat_name"] or translator.t("chat.chat_id", id=item["conversation_id"])

    return JSONResponse(content={
        "query": q,
        "has_more": has_more,
        "next_offset": offset + limit if has_more else offset + len(items),
        "items": items,
    })
