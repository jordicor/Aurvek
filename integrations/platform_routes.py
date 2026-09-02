import orjson
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from auth import get_current_user
from chat.services.privacy import ensure_conversation_privacy_schema
from database import get_db_connection
from integrations.conversations import (
    change_conversation_response_mode,
    mutate_external_platforms,
    set_external_conversation,
)
from integrations.devices.service import (
    conversation_has_external_device_bindings,
    get_conversation_binding_summaries,
)
from integrations.platforms import validate_platform
from chat.services.conversation_channels import (
    channel_summary_with_legacy,
    get_conversation_channel_summaries,
    has_messaging_channel,
    legacy_external_platform,
)
from models import User


router = APIRouter()


def _conversation_ids_match(left, right) -> bool:
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return False


@router.post("/api/conversations/{conversation_id}/external-platform")
async def update_external_platform(
    conversation_id: int,
    data: dict,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    platform = data.get("platform")
    action = data.get("action")

    if action not in ["add", "remove"]:
        raise HTTPException(status_code=400, detail="Invalid action")

    if action == "add" and not validate_platform(platform):
        raise HTTPException(status_code=400, detail="Invalid platform")

    visible_limit = min(max(1, int(data.get("visible_count", 10))), 50)
    platform_conversation = None
    affected_conversation_ids = {conversation_id}

    if action == "add":
        if await conversation_has_external_device_bindings(
            user_id=current_user.id,
            conversation_id=conversation_id,
        ):
            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "error": "external_devices_attached",
                    "message": "Remove external device access before assigning this conversation to WhatsApp or Telegram.",
                },
            )
        result = await set_external_conversation(
            current_user.id,
            conversation_id,
            platform,
            platform,
        )
        if not result["success"]:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": result["error"],
                    "message": result["message"],
                },
            )
        affected_conversation_ids.update(result.get("affected_conversation_ids") or [])
    else:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT user_id FROM conversations WHERE id = ?",
                (conversation_id,),
            )
            row = await cursor.fetchone()
            if not row or row[0] != current_user.id:
                return JSONResponse(
                    status_code=403,
                    content={
                        "success": False,
                        "error": "conversation_not_found",
                        "message": "Conversation not found.",
                    },
                )

        def _web_remove(platforms):
            if platform == "all":
                for platform_name in list(platforms.keys()):
                    if (
                        isinstance(platforms.get(platform_name), dict)
                        and _conversation_ids_match(
                            platforms[platform_name].get("conversation_id"),
                            conversation_id,
                        )
                    ):
                        platforms[platform_name].pop("conversation_id", None)
            elif (
                platform in platforms
                and isinstance(platforms.get(platform), dict)
                and _conversation_ids_match(
                    platforms[platform].get("conversation_id"), conversation_id
                )
            ):
                platforms[platform].pop("conversation_id", None)

        await mutate_external_platforms(current_user.id, _web_remove)

    await ensure_conversation_privacy_schema()
    async with get_db_connection(readonly=True) as conn:
        channel_summaries = await get_conversation_channel_summaries(
            current_user.id, conn=conn
        )
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT c.id, c.user_id, c.start_date, c.chat_name,
                   CASE
                     WHEN json_extract(u.external_platforms, '$.whatsapp.conversation_id') = c.id THEN 'whatsapp'
                     WHEN json_extract(u.external_platforms, '$.telegram.conversation_id') = c.id THEN 'telegram'
                     ELSE NULL
                   END as external_platform,
                   c.locked, l.model AS llm_model,
                   COALESCE(p.disable_web_search, 0) AS web_search_disabled,
                   COALESCE(p.force_web_search, 0) AS web_search_forced,
                   p.forced_llm_id, p.hide_llm_name, p.allowed_llms,
                   COALESCE(p.is_paid, 0) AS is_paid,
                   c.last_activity, c.llm_id, l.machine, c.role_id, c.folder_id
            FROM conversations c
            JOIN user_details u ON c.user_id = u.user_id
            LEFT JOIN LLM l ON l.id = c.llm_id
            LEFT JOIN prompts p ON p.id = c.role_id
            WHERE c.user_id = ?
            ORDER BY c.last_activity DESC, c.id DESC
            LIMIT ?
            """,
            (current_user.id, visible_limit),
        )
        visible_conversations = await cursor.fetchall()

        if action == "add":
            await cursor.execute(
                """
                SELECT c.id, c.user_id, c.start_date, c.chat_name, ? as external_platform,
                       c.locked, l.model AS llm_model,
                       COALESCE(p.disable_web_search, 0) AS web_search_disabled,
                       COALESCE(p.force_web_search, 0) AS web_search_forced,
                       p.forced_llm_id, p.hide_llm_name, p.allowed_llms,
                       COALESCE(p.is_paid, 0) AS is_paid,
                       c.last_activity, c.llm_id, l.machine, c.role_id, c.folder_id
                FROM conversations c
                LEFT JOIN LLM l ON l.id = c.llm_id
                LEFT JOIN prompts p ON p.id = c.role_id
                WHERE c.id = ? AND c.user_id = ?
                """,
                (platform, conversation_id, current_user.id),
            )
            platform_conversation = await cursor.fetchone()

        visible_ids = {int(conv[0]) for conv in visible_conversations}
        missing_affected_ids = sorted(
            int(value)
            for value in affected_conversation_ids
            if int(value) not in visible_ids
        )
        extra_affected_conversations = []
        if missing_affected_ids:
            placeholders = ",".join("?" for _ in missing_affected_ids)
            await cursor.execute(
                f"""
                SELECT c.id, c.user_id, c.start_date, c.chat_name,
                       NULL as external_platform,
                       c.locked, l.model AS llm_model,
                       COALESCE(p.disable_web_search, 0) AS web_search_disabled,
                       COALESCE(p.force_web_search, 0) AS web_search_forced,
                       p.forced_llm_id, p.hide_llm_name, p.allowed_llms,
                       COALESCE(p.is_paid, 0) AS is_paid,
                       c.last_activity, c.llm_id, l.machine, c.role_id, c.folder_id
                FROM conversations c
                LEFT JOIN LLM l ON l.id = c.llm_id
                LEFT JOIN prompts p ON p.id = c.role_id
                WHERE c.user_id = ? AND c.id IN ({placeholders})
                """,
                [current_user.id, *missing_affected_ids],
            )
            extra_affected_conversations = list(await cursor.fetchall())

    all_visible_conversations = list(visible_conversations)
    known_ids = {int(conv[0]) for conv in all_visible_conversations}
    if platform_conversation and int(platform_conversation[0]) not in known_ids:
        all_visible_conversations.append(platform_conversation)
        known_ids.add(int(platform_conversation[0]))
    for conversation in extra_affected_conversations:
        if int(conversation[0]) not in known_ids:
            all_visible_conversations.append(conversation)
            known_ids.add(int(conversation[0]))

    binding_summaries = await get_conversation_binding_summaries(
        current_user.id,
        [
            conv[0]
            for conv in all_visible_conversations
            if not has_messaging_channel(
                channel_summary_with_legacy(
                    channel_summaries.get(int(conv[0])), conv[4]
                )
            )
        ],
    )

    updated_conversations = [
        {
            "id": conv[0],
            "user_id": conv[1],
            "start_date": conv[2],
            "chat_name": conv[3],
            "external_platform": legacy_external_platform(
                channel_summary_with_legacy(
                    channel_summaries.get(int(conv[0])), conv[4]
                )
            ),
            "external_channels": channel_summary_with_legacy(
                channel_summaries.get(int(conv[0])), conv[4]
            )["external_channels"],
            "phone_binding": channel_summary_with_legacy(
                channel_summaries.get(int(conv[0])), conv[4]
            )["phone_binding"],
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
            "folder_id": conv[17],
            "external_bindings": (
                None
                if has_messaging_channel(
                    channel_summary_with_legacy(
                        channel_summaries.get(int(conv[0])), conv[4]
                    )
                )
                else binding_summaries.get(int(conv[0]))
            ),
        }
        for conv in all_visible_conversations
    ]

    return JSONResponse(
        content={
            "success": True,
            "updatedConversations": updated_conversations,
        }
    )


@router.get("/api/platform-mode/{platform}/{conversation_id}")
async def get_platform_mode(
    platform: str,
    conversation_id: int,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not validate_platform(platform):
        raise HTTPException(status_code=400, detail="Invalid platform")

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            "SELECT user_id FROM conversations WHERE id = ?",
            (conversation_id,),
        )
        result = await cursor.fetchone()
        if not result or result[0] != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied")

        await cursor.execute(
            "SELECT external_platforms FROM USER_DETAILS WHERE user_id = ?",
            (current_user.id,),
        )
        result = await cursor.fetchone()
        external_platforms = orjson.loads(result[0]) if result and result[0] else {}
        platform_data = external_platforms.get(platform, {})

        if platform_data.get("conversation_id") != conversation_id:
            raise HTTPException(
                status_code=400,
                detail=f"Conversation is not assigned to {platform}",
            )

        current_mode = platform_data.get("answer", "text")
        return JSONResponse(content={"mode": current_mode})


@router.post("/api/platform-mode/{platform}/{conversation_id}")
async def set_platform_mode(
    platform: str,
    conversation_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    data = await request.json()
    result = await change_conversation_response_mode(
        current_user.id,
        platform,
        conversation_id,
        data.get("mode"),
    )
    if not result["success"]:
        if result["error"] == "conversation_not_found":
            raise HTTPException(status_code=403, detail="Access denied")
        if result["error"] in ("invalid_platform", "invalid_mode"):
            raise HTTPException(status_code=400, detail=result["message"])
        raise HTTPException(status_code=400, detail=result["message"])

    return JSONResponse(
        content={
            "success": True,
            "message": result["message"],
            "mode": result["mode"],
        }
    )
