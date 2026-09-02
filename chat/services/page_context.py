from datetime import datetime, timezone, timedelta

import orjson
from fastapi import HTTPException

from common import (
    AVATAR_TOKEN_EXPIRE_HOURS,
    CLOUDFLARE_BASE_URL,
    MAX_API_IMAGE_SIZE_MB,
    MAX_CHAT_IMAGE_DIMENSION,
    READONLY_MODE,
    _get_marketplace_template_flags,
    get_subscription_auth_enabled,
    get_user_api_key_mode,
    templates,
    user_has_valid_api_keys,
    user_requires_own_keys,
)
from database import get_db_connection
from log_config import logger
from models import User
from prompt_access import get_user_accessible_prompts
from save_images import generate_img_token

from chat.services.attachment_uploads import ATTACHMENT_UPLOAD_CHUNK_SIZE_BYTES
from chat.services.avatar_urls import get_signed_bot_avatar_urls
from chat.services.deletion import purge_stale_incognito_conversations_for_user
from chat.services.privacy import ensure_conversation_privacy_schema
from chat.services.conversation_channels import (
    channel_summary_with_legacy,
    get_conversation_channel_summaries,
    has_messaging_channel,
    legacy_external_platform,
)
from integrations.devices.service import get_conversation_binding_summaries
from request_security import ensure_csrf_token


_INITIAL_CONVERSATION_COLUMNS = """
    c.id, c.user_id, c.start_date, c.chat_name,
    NULL as external_platform,
    c.locked, l.model as llm_model,
    COALESCE(p.disable_web_search, 0) as web_search_disabled,
    COALESCE(p.force_web_search, 0) as web_search_forced,
    p.forced_llm_id, p.hide_llm_name, p.allowed_llms,
    COALESCE(p.is_paid, 0) as is_paid,
    c.last_activity, c.llm_id, l.machine, c.role_id,
    c.folder_id, COALESCE(c.is_incognito, 0) as is_incognito
"""


def _requested_conversation_id(request) -> int | None:
    raw_value = request.query_params.get("conversation_id")
    if not raw_value or not raw_value.isascii() or not raw_value.isdecimal():
        return None
    parsed = int(raw_value)
    return parsed if parsed > 0 else None


async def _load_requested_conversation_row(cursor, user_id: int, conversation_id: int):
    await cursor.execute(
        f"""
        SELECT {_INITIAL_CONVERSATION_COLUMNS}
        FROM conversations c
        JOIN llm l ON c.llm_id = l.id
        LEFT JOIN prompts p ON c.role_id = p.id
        WHERE c.id = ? AND c.user_id = ?
          AND COALESCE(c.hidden_from_history, 0) = 0
        """,
        (conversation_id, user_id),
    )
    return await cursor.fetchone()


async def handle_recent_conversation(current_user: User, recent_conversation):
    if not recent_conversation:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    'INSERT INTO conversations (user_id, start_date, role_id) VALUES (?, datetime("now"), ?) RETURNING id, datetime(start_date, "localtime") as start_date, role_id',
                    (current_user.id, current_user.current_prompt_id),
                )
                recent_conversation = await cursor.fetchone()
            await conn.commit()
    return recent_conversation["id"], recent_conversation["start_date"], recent_conversation["role_id"]


async def handle_get_request(request, user_id, current_user, conn, admin_view=False):
    effective_user_id = user_id if user_id is not None else current_user.id
    requested_conversation_id = _requested_conversation_id(request)
    requested_or_zero = requested_conversation_id or 0

    await ensure_conversation_privacy_schema()
    if not admin_view and effective_user_id == current_user.id:
        await purge_stale_incognito_conversations_for_user(current_user)

    # GPTSub rows are shared, but authorization is personal. Derive the selector
    # intersection from this user's decryptable live catalog; a linked boolean would
    # leak models available only to another account. Fail-safe HIDE on every error.
    gptsub_models: list[str] = []
    gptsub_reasoning: dict[str, dict] = {}
    try:
        gptsub_feature_enabled = await get_subscription_auth_enabled()
        if gptsub_feature_enabled:
            from subscription_auth.gate import linked_blob_for_user

            linked_blob = await linked_blob_for_user(effective_user_id)
            saved_models = (
                linked_blob.get("available_models", [])
                if linked_blob and linked_blob.get("models_sync_status") == "synced"
                else []
            )
            if isinstance(saved_models, list):
                gptsub_models = [
                    model
                    for model in saved_models
                    if isinstance(model, str) and model and len(model) <= 128
                ]
            saved_reasoning = linked_blob.get("model_reasoning", {}) if linked_blob else {}
            if isinstance(saved_reasoning, dict):
                gptsub_reasoning = {
                    model: value
                    for model, value in saved_reasoning.items()
                    if model in gptsub_models and isinstance(value, dict)
                }
    except Exception:
        gptsub_models = []
        gptsub_reasoning = {}

    async with conn.cursor() as cursor:
        await cursor.execute(
            """
            SELECT
                u.username,
                u.profile_picture,
                ud.current_prompt_id,
                ud.balance,
                ud.allow_file_upload,
                ud.all_prompts_access,
                ud.public_prompts_access,
                ud.category_access,
                ud.allow_image_generation,
                COALESCE(c.llm_id, ud.llm_id) AS current_model_type,
                ud.llm_id AS new_chat_model_type,
                COALESCE(ud.web_search_enabled, 1) AS web_search_enabled,
                (SELECT COUNT(*)
                 FROM conversations
                 WHERE user_id = u.id
                   AND COALESCE(hidden_from_history, 0) = 0) AS conversation_count,
                c.id AS conversation_id,
                c.start_date AS start_date,
                c.role_id,
                COALESCE(p.image, p2.image) AS bot_picture,
                COALESCE(p.description, p2.description) AS prompt_description,
                (SELECT json_group_array(json_object('id', id, 'name', name)) FROM Voices) AS voices_json,
                ud.current_alter_ego_id,
                ae.name AS alter_ego_name,
                ae.profile_picture AS alter_ego_profile_picture
            FROM users u
            JOIN user_details ud ON u.id = ud.user_id
            LEFT JOIN conversations c
              ON c.user_id = u.id
             AND COALESCE(c.hidden_from_history, 0) = 0
            LEFT JOIN (
                SELECT ep.value
                FROM user_details ud2
                LEFT JOIN json_each(ud2.external_platforms) AS ep
                WHERE ud2.user_id = ?
            ) AS ep ON c.id = ep.value
            LEFT JOIN Prompts p ON c.role_id = p.id
            LEFT JOIN Prompts p2 ON ud.current_prompt_id = p2.id
            LEFT JOIN USER_ALTER_EGOS ae ON ud.current_alter_ego_id = ae.id
            WHERE u.id = ?
              AND ((ep.value IS NULL OR ep.value = '') OR c.id = ?)
            ORDER BY CASE WHEN c.id = ? THEN 0 ELSE 1 END,
                     c.last_activity DESC, c.id DESC
            LIMIT 1
            """,
            (
                effective_user_id,
                effective_user_id,
                requested_or_zero,
                requested_or_zero,
            ),
        )

        full_data = await cursor.fetchone()
        if not full_data:
            raise HTTPException(status_code=404, detail="User not found")

        logger.debug("Retrieved start_date from database: %s", full_data["start_date"])

        from llm_catalog import get_selector_llms

        llm_model_rows = await get_selector_llms(
            conn,
            preserve_ids=[
                full_data["current_model_type"],
                full_data["new_chat_model_type"],
            ],
            gptsub_models=gptsub_models,
            gptsub_reasoning=gptsub_reasoning,
        )
        llm_models = [dict(row) for row in llm_model_rows]
        llm_models.sort(key=lambda m: (m.get("machine", ""), m.get("display_name") or m.get("model", "")))
        available_voices = orjson.loads(full_data["voices_json"]) if full_data["voices_json"] else []

        if full_data["current_alter_ego_id"]:
            username = full_data["alter_ego_name"]
            user_profile_picture = full_data["alter_ego_profile_picture"]
        else:
            username = full_data["username"]
            user_profile_picture = full_data["profile_picture"]

        if user_profile_picture:
            current_time = datetime.now(timezone.utc)
            new_expiration = current_time + timedelta(hours=AVATAR_TOKEN_EXPIRE_HOURS)
            profile_picture_url = f"{user_profile_picture}_32.webp"
            token = generate_img_token(profile_picture_url, new_expiration, current_user)
            user_profile_picture = f"{CLOUDFLARE_BASE_URL}{profile_picture_url}?token={token}"

        bot_avatar_urls = get_signed_bot_avatar_urls(
            full_data["bot_picture"],
            current_user,
        )

        prompts = await get_user_accessible_prompts(
            current_user,
            cursor,
            full_data["all_prompts_access"],
            full_data["public_prompts_access"],
            full_data["category_access"],
        )

        api_key_mode = await get_user_api_key_mode(effective_user_id)
        requires_own_keys = await user_requires_own_keys(effective_user_id)
        has_own_keys = await user_has_valid_api_keys(effective_user_id)
        # Keep this as the generic API-key capability. The browser adds the
        # exact active GPTSub llm_id dynamically; treating the mere presence of
        # any linked model as permission would incorrectly enable a different
        # platform model that still requires an API key.
        can_send_messages = not (requires_own_keys and not has_own_keys)

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
            (effective_user_id,),
        )
        folders_rows = await cursor.fetchall()
        chat_folders = [
            {
                "id": row[0],
                "name": row[1],
                "color": row[2],
                "created_at": row[3],
                "updated_at": row[4],
                "conversation_count": row[5],
            }
            for row in folders_rows
        ]

        initial_ext_conversations = []
        initial_ext_exclude = ""
        initial_ext_exclude_params = []

        channel_summaries = await get_conversation_channel_summaries(
            effective_user_id, conn=conn
        )
        init_ext_ids = sorted(channel_summaries)

        if init_ext_ids:
            placeholders = ",".join(["?" for _ in init_ext_ids])
            initial_ext_exclude = f" AND c.id NOT IN ({placeholders})"
            initial_ext_exclude_params = list(init_ext_ids)

            await cursor.execute(
                f"""
                SELECT {_INITIAL_CONVERSATION_COLUMNS}
                FROM conversations c
                JOIN llm l ON c.llm_id = l.id
                LEFT JOIN prompts p ON c.role_id = p.id
                WHERE c.id IN ({placeholders}) AND c.user_id = ?
                  AND COALESCE(c.hidden_from_history, 0) = 0
                ORDER BY c.last_activity DESC, c.id DESC
                """,
                [*init_ext_ids, effective_user_id],
            )
            initial_ext_conversations = list(await cursor.fetchall())

        # Pinned external conversations do not consume the normal-chat page.
        init_normal_limit = 25
        await cursor.execute(
            f"""
            SELECT {_INITIAL_CONVERSATION_COLUMNS}
            FROM conversations c
            JOIN llm l ON c.llm_id = l.id
            LEFT JOIN prompts p ON c.role_id = p.id
            WHERE c.user_id = ? AND (c.folder_id IS NULL OR c.folder_id = 0){initial_ext_exclude}
              AND COALESCE(c.hidden_from_history, 0) = 0
            ORDER BY c.last_activity DESC, c.id DESC
            LIMIT ?
            """,
            [effective_user_id] + initial_ext_exclude_params + [init_normal_limit],
        )
        conversations_rows = await cursor.fetchall()

        all_init_conversations = list(initial_ext_conversations) + list(conversations_rows)
        requested_is_selected = (
            requested_conversation_id is not None
            and int(full_data["conversation_id"] or 0) == requested_conversation_id
        )
        if requested_is_selected and not any(
            int(row[0]) == requested_conversation_id
            for row in all_init_conversations
        ):
            requested_row = await _load_requested_conversation_row(
                cursor,
                effective_user_id,
                requested_conversation_id,
            )
            if requested_row is not None:
                all_init_conversations.append(requested_row)

        binding_summaries = await get_conversation_binding_summaries(
            effective_user_id,
            [
                row[0]
                for row in all_init_conversations
                if not has_messaging_channel(
                    channel_summary_with_legacy(
                        channel_summaries.get(int(row[0])), row[4]
                    )
                )
            ],
        )
        initial_conversations = [
            {
                "id": row[0],
                "user_id": row[1],
                "start_date": row[2],
                "chat_name": row[3] if row[3] else "New Chat",
                "external_platform": legacy_external_platform(
                    channel_summary_with_legacy(
                        channel_summaries.get(int(row[0])), row[4]
                    )
                ),
                "external_channels": channel_summary_with_legacy(
                    channel_summaries.get(int(row[0])), row[4]
                )["external_channels"],
                "phone_binding": channel_summary_with_legacy(
                    channel_summaries.get(int(row[0])), row[4]
                )["phone_binding"],
                "locked": bool(row[5]) if row[5] is not None else False,
                "llm_model": row[6],
                "web_search_allowed": not bool(row[7]),
                "web_search_forced": bool(row[8]),
                "forced_llm_id": row[9],
                "hide_llm_name": bool(row[10]) if row[10] else False,
                "allowed_llms": orjson.loads(row[11]) if row[11] else None,
                "is_paid": bool(row[12]),
                "last_activity": row[13],
                "llm_id": row[14],
                "machine": row[15],
                "prompt_id": row[16],
                "folder_id": row[17],
                "is_incognito": bool(row[18]),
                "external_bindings": (
                    None
                    if has_messaging_channel(
                        channel_summary_with_legacy(
                            channel_summaries.get(int(row[0])), row[4]
                        )
                    )
                    else binding_summaries.get(int(row[0]))
                ),
            }
            for row in all_init_conversations
        ]

        context = {
            "request": request,
            "user_id": effective_user_id,
            "username": username,
            "conversation_id": full_data["conversation_id"],
            "start_date_iso": full_data["start_date"],
            "prompts": prompts,
            "current_prompt_id": full_data["current_prompt_id"],
            "conversation_count": full_data["conversation_count"],
            "all_prompts_access": full_data["all_prompts_access"],
            "public_prompts_access": full_data["public_prompts_access"],
            "admin_view": admin_view,
            "have_vision": full_data["allow_image_generation"],
            "is_admin": await current_user.is_admin,
            "is_user": await current_user.is_user,
            "llm_models": llm_models,
            "current_model_type": full_data["current_model_type"],
            "new_chat_model_type": full_data["new_chat_model_type"],
            "user_balance": full_data["balance"],
            "available_voices": available_voices,
            "can_send_files": current_user.can_send_files,
            "can_generate_images": full_data["allow_image_generation"],
            "user_profile_picture": user_profile_picture,
            **bot_avatar_urls,
            "current_alter_ego_id": full_data["current_alter_ego_id"],
            "prompt_description": full_data["prompt_description"],
            "api_key_mode": api_key_mode,
            "can_send_messages": can_send_messages,
            "requires_own_keys": requires_own_keys,
            "has_own_keys": has_own_keys,
            "chat_folders": chat_folders,
            "initial_conversations": initial_conversations,
            "web_search_enabled": bool(full_data["web_search_enabled"]),
            "readonly_mode": READONLY_MODE,
            "max_api_image_size_mb": MAX_API_IMAGE_SIZE_MB,
            "max_chat_image_dimension": MAX_CHAT_IMAGE_DIMENSION,
            "attachment_upload_chunk_size_bytes": ATTACHMENT_UPLOAD_CHUNK_SIZE_BYTES,
            "marketplace": _get_marketplace_template_flags(),
            "telephony_csrf_token": ensure_csrf_token(request),
        }

    return templates.TemplateResponse("/chat/chat.html", context)
