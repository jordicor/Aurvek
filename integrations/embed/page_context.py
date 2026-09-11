"""Render the ordinary chat composition using only a verified interview binding."""

import json

from fastapi import HTTPException

from common import (
    MAX_API_IMAGE_SIZE_MB,
    MAX_CHAT_IMAGE_DIMENSION,
    READONLY_MODE,
    get_subscription_auth_enabled,
    get_user_api_key_mode,
    templates,
    user_has_valid_api_keys,
    user_requires_own_keys,
)
from database import get_db_connection
from i18n import Translator
from chat.services.attachment_uploads import ATTACHMENT_UPLOAD_CHUNK_SIZE_BYTES
from llm_catalog import get_selector_llms
from log_config import logger


async def render_embed_chat(request, principal, app):
    """Never call the native page resolver, which includes other conversations."""
    application = getattr(principal, "application", None)
    capabilities = principal.capabilities
    gptsub_models, gptsub_reasoning = [], {}
    if application is not None:
        try:
            if await get_subscription_auth_enabled():
                from subscription_auth.gate import linked_blob_for_user
                blob = await linked_blob_for_user(principal.user_id)
                if blob and blob.get("models_sync_status") == "synced":
                    saved_models = blob.get("available_models", [])
                    gptsub_models = saved_models if isinstance(saved_models, list) else []
                    gptsub_reasoning = blob.get("model_reasoning", {})
        except Exception:
            # As on the native page, an unavailable personal catalog hides only
            # subscription models; ordinary platform models remain usable.
            logger.warning("Could not load embed personal model catalog for user_id=%s", principal.user_id)
    async with get_db_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT c.*, p.name AS prompt_name, p.description AS prompt_description,
                   COALESCE(p.is_paid, 0) AS is_paid, u.username, ud.balance,
                   p.forced_llm_id, p.forced_reasoning_json, p.allowed_llms,
                   p.hide_llm_name, p.disable_web_search, p.force_web_search,
                   p.extensions_enabled, p.extensions_free_selection,
                   p.gransabio_enabled, ud.allow_file_upload, ud.allow_image_generation,
                   COALESCE(ud.web_search_enabled, 1) AS web_search_enabled
            FROM CONVERSATIONS c
            JOIN PROMPTS p ON p.id = c.role_id
            JOIN USERS u ON u.id = c.user_id
            JOIN USER_DETAILS ud ON ud.user_id = u.id
            WHERE c.id = ? AND c.user_id = ? AND c.role_id = ?
            """,
            (principal.conversation_id, principal.user_id, principal.prompt_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        conversation = dict(row)
        cursor = await conn.execute("SELECT * FROM LLM WHERE id = ?", (row["llm_id"],))
        model_row = await cursor.fetchone()
        if model_row is None:
            raise HTTPException(status_code=503, detail="service_unavailable")
        model = dict(model_row)
        models = [model]
        application_display_name = None
        extensions = []
        if application is not None:
            cursor = await conn.execute(
                "SELECT config_json FROM APPLICATION_ASSISTANTS WHERE app_id=? AND assistant_id=?",
                (principal.app_id, principal.application.assistant_id),
            )
            assistant_row = await cursor.fetchone()
            if assistant_row is None:
                raise HTTPException(status_code=404, detail="not_found")
            application_display_name = json.loads(assistant_row["config_json"])["display_name"]
            models = await get_selector_llms(
                conn, preserve_ids=[model["id"]], gptsub_models=gptsub_models,
                gptsub_reasoning=gptsub_reasoning,
            )
            allowed = json.loads(conversation["allowed_llms"]) if conversation["allowed_llms"] else None
            forced = conversation["forced_llm_id"]
            if forced:
                models = [item for item in models if item["id"] == forced]
            elif allowed is not None:
                models = [item for item in models if item["id"] in allowed]
            if (conversation["hide_llm_name"] or not (
                    capabilities.get("model_selection") or capabilities.get("multi_ai"))):
                models = [item for item in models if item["id"] == model["id"]]
            if conversation["extensions_enabled"] and capabilities.get("extensions"):
                cursor = await conn.execute(
                    "SELECT id,name,slug,description,display_order FROM PROMPT_EXTENSIONS "
                    "WHERE prompt_id=? ORDER BY display_order,id", (principal.prompt_id,),
                )
                extensions = [dict(item) for item in await cursor.fetchall()]
        # The selector's public representation omits raw provider metadata.
        model = next((item for item in models if item["id"] == model["id"]), {
            key: model.get(key) for key in ("id", "machine", "model", "vision", "enabled", "display_name")
        })
        if application is None:
            models = [model]

    gransabio_enabled = bool(application and capabilities.get("gransabio") and conversation["gransabio_enabled"])
    controls = {
        "model_selection": bool(application and capabilities.get("model_selection") and not (
            conversation["forced_llm_id"] or conversation["hide_llm_name"] or gransabio_enabled)),
        "reasoning": bool(application and capabilities.get("reasoning") and not (
            conversation["forced_reasoning_json"] or gransabio_enabled)),
        "extensions": bool(application and capabilities.get("extensions") and conversation["extensions_enabled"]),
        "multi_ai": bool(application and capabilities.get("multi_ai") and not (
            conversation["forced_llm_id"] or conversation["hide_llm_name"] or
            conversation["force_web_search"] or conversation["gransabio_enabled"])
            and len([item for item in models if item["machine"] != "GPTSub" and item.get("enabled") != 0]) >= 2),
        "gransabio": gransabio_enabled,
        "attachments": bool(capabilities.get("attachments") and conversation["allow_file_upload"] and not gransabio_enabled),
        "image_generation": bool(application and capabilities.get("image_generation") and
                                 conversation["allow_image_generation"] and not gransabio_enabled),
        "export_pdf": bool(application and capabilities.get("export_pdf")),
        "export_mp3": bool(application and capabilities.get("export_mp3") and capabilities.get("tts")),
    }
    web_search_allowed = bool(application and capabilities.get("tools") and capabilities.get("web_search")
                              and not conversation["disable_web_search"] and not gransabio_enabled)

    requires_keys = await user_requires_own_keys(principal.user_id)
    has_keys = await user_has_valid_api_keys(principal.user_id)
    can_send = bool(principal.capabilities.get("text")) and not (
        requires_keys and not has_keys and model.get("machine") != "GPTSub"
    ) and not READONLY_MODE
    bootstrap = {
        "contract_version": "aurvek_embed.v1",
        "app_id": principal.app_id,
        "external_project_id": principal.external_project_id,
        "conversation_id": str(principal.conversation_id),
        "frame_instance_id": principal.frame_instance_id,
        "parent_origin": principal.parent_origin,
        "ui_language": principal.ui_language,
        "capabilities": principal.capabilities,
        "controls": controls,
        "csrf_token": principal.csrf_token,
        "expires_at": principal.expires_at,
    }
    if getattr(principal, "application", None) is not None:
        bootstrap["application"] = {
            "contract_version": "aurvek_applications.v1",
            "app_id": principal.app_id,
            "context_id": principal.application.context_id,
            "assistant_id": principal.application.assistant_id,
            "conversation_id": principal.conversation_id,
            "display_name": application_display_name,
        }
    initial = {
        "id": principal.conversation_id,
        "user_id": principal.user_id,
        "prompt_id": principal.prompt_id,
        "chat_name": conversation.get("chat_name") or app.display_name,
        "start_date": conversation["start_date"],
        "last_activity": conversation.get("last_activity"),
        "locked": bool(conversation.get("locked")),
        "llm_id": model["id"],
        "llm_model": model["model"],
        "llm_enabled": model.get("enabled") != 0,
        "machine": model["machine"],
        "forced_llm_id": conversation["forced_llm_id"] if application else model["id"],
        "allowed_llms": allowed if application else [model["id"]],
        "hide_llm_name": bool(conversation["hide_llm_name"]) if application else True,
        "is_paid": bool(conversation["is_paid"]),
        "web_search_allowed": web_search_allowed,
        "web_search_forced": bool(web_search_allowed and conversation["force_web_search"]),
        "gransabio_enabled": gransabio_enabled,
        "extensions_enabled": controls["extensions"],
        "extensions_free_selection": bool(conversation["extensions_free_selection"]),
        "extensions": extensions,
        "active_extension": next((item for item in extensions if item["id"] == conversation.get("active_extension_id")), None),
        "external_platform": None,
        "external_channels": [],
        "folder_id": None,
        "is_incognito": False,
    }
    context = {
        "request": request,
        "presentation": "embedded",
        "embed_config": bootstrap,
        "embed_theme": app.theme,
        "ui_language": principal.ui_language,
        "user_id": principal.user_id,
        "username": conversation["username"],
        "conversation_id": principal.conversation_id,
        "start_date_iso": conversation["start_date"],
        "current_prompt_id": principal.prompt_id,
        "prompts": [{"id": principal.prompt_id, "text": conversation["prompt_name"]}],
        "conversation_count": 1,
        "all_prompts_access": False,
        "public_prompts_access": False,
        "admin_view": False,
        "is_admin": False,
        "is_user": False,
        "have_vision": bool(model.get("vision")),
        "llm_models": models,
        "current_model_type": model["id"],
        "new_chat_model_type": model["id"],
        "user_balance": conversation["balance"],
        "available_voices": [],
        "can_send_files": controls["attachments"],
        "can_generate_images": controls["image_generation"],
        "user_profile_picture": "",
        "bot_profile_picture": "",
        "current_alter_ego_id": None,
        "prompt_description": "",
        "api_key_mode": await get_user_api_key_mode(principal.user_id),
        "can_send_messages": can_send,
        "requires_own_keys": requires_keys,
        "has_own_keys": has_keys,
        "chat_folders": [],
        "initial_conversations": [initial],
        "web_search_enabled": bool(web_search_allowed and conversation["web_search_enabled"]),
        "readonly_mode": READONLY_MODE,
        "max_api_image_size_mb": MAX_API_IMAGE_SIZE_MB,
        "max_chat_image_dimension": MAX_CHAT_IMAGE_DIMENSION,
        "attachment_upload_chunk_size_bytes": ATTACHMENT_UPLOAD_CHUNK_SIZE_BYTES,
        "marketplace": {},
        "telephony_csrf_token": principal.csrf_token,
    }
    # Shared UI components use the verified frame language, never the native
    # account preference or an unrelated cookie on the embed host.
    request.state.i18n = Translator(principal.ui_language)
    return templates.TemplateResponse("/chat/chat.html", context)
