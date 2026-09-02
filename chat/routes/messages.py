import base64
import zlib
from typing import List, Optional

import orjson
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from auth import get_current_user, unauthenticated_response
from admin_audit import log_admin_action
from billing.usage_reservations import (
    serialize_user_billing_response,
    serialize_user_billing_stream,
)
from common import (
    API_KEY_MODE_OWN_ONLY,
    MAX_PDF_SIZE_MB,
    MAX_RAW_UPLOAD_SIZE_MB,
    MAX_TEXT_FILE_SIZE_MB,
    custom_unescape,
    decrypt_api_key,
    get_user_api_key_mode,
)
from database import get_db_connection
from integrations.conversations import is_whatsapp_conversation
from integrations.telephony.channel_context import capture_non_phone_channel_turn
from ai_runtime.messages import process_save_message
from ai_runtime.reasoning import (
    ReasoningValidationError,
    parse_reasoning_selection,
    resolve_and_validate,
    selection_from_legacy_thinking_budget,
)
from ai_runtime.reasoning_tags import strip_tagged_thinking_prefix
from ai_runtime.multi_ai.service import process_multi_ai_message
from ai_runtime.provider_health import provider_from_machine, touch_provider_activity
from log_config import logger
from models import User
from chat.services.attachment_uploads import parse_attachment_refs_value
from chat.services.avatar_urls import get_signed_bot_avatar_urls
from chat.services.file_inputs import is_text_file
from chat.services.message_rendering import (
    preload_attachment_records_for_messages,
    process_message,
)
from chat.services.message_requests import validate_message_request
from chat.services.phone_history import load_phone_history_page
from integrations.messaging_voice_notes.service import load_message_channel_provenance
from chat.services.privacy import delete_message_rows, ensure_conversation_privacy_schema

router = APIRouter()


def _reasoning_selection_from_form(
    reasoning_mode: str | None,
    reasoning_budget_tokens: int | None,
    thinking_budget_tokens: int | None,
):
    """Parse the new request fields, with one temporary legacy translation."""
    if reasoning_mode is not None or reasoning_budget_tokens is not None:
        return parse_reasoning_selection(
            {
                "mode": reasoning_mode or "default",
                **(
                    {"budget_tokens": reasoning_budget_tokens}
                    if reasoning_budget_tokens is not None
                    else {}
                ),
            }
        )
    return selection_from_legacy_thinking_budget(thinking_budget_tokens)


async def _request_reasoning_capabilities(
    *, machine: str, model: str, capabilities_json, user_id: int
):
    try:
        capabilities = orjson.loads(capabilities_json) if capabilities_json else {}
    except (orjson.JSONDecodeError, TypeError):
        capabilities = {}
    if not isinstance(capabilities, dict):
        capabilities = {}

    if machine == "GPTSub":
        account_capabilities = None
        try:
            from subscription_auth.reasoning import user_model_reasoning_capabilities

            account_capabilities = await user_model_reasoning_capabilities(user_id, model)
        except Exception:
            logger.warning(
                "Could not resolve GPTSub reasoning capabilities for user_id=%s",
                user_id,
                exc_info=True,
            )
        capabilities = {
            **capabilities,
            "reasoning": account_capabilities or {"behavior": "unknown"},
        }
    return capabilities


@router.get("/api/conversations/{conversation_id}/messages")
async def get_messages(
    conversation_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    limit: int = Query(25, ge=1, le=100),
    before_id: Optional[int] = Query(None),
):
    if current_user is None:
        return unauthenticated_response()

    logger.debug("Requested messages for conversation ID: %s", conversation_id)
    await ensure_conversation_privacy_schema()
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        is_user_admin = await current_user.is_admin

        if is_user_admin:
            await cursor.execute("SELECT id, user_id FROM conversations WHERE id = ?", (conversation_id,))
            conversation = await cursor.fetchone()
            if conversation and conversation["user_id"] != current_user.id:
                await log_admin_action(
                    admin_id=current_user.id,
                    action_type="view_conversation",
                    request=request,
                    target_user_id=conversation["user_id"],
                    target_resource_type="conversation",
                    target_resource_id=conversation_id,
                    details=f"Admin viewed conversation of user {conversation['user_id']}",
                )
        else:
            await cursor.execute(
                "SELECT id, user_id FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, current_user.id),
            )
            conversation = await cursor.fetchone()

        if not conversation:
            return JSONResponse(content={"error": "Conversation not found or access denied"}, status_code=404)

        await cursor.execute(
            """
            SELECT c.id, c.role_id, c.active_extension_id,
                   p.name AS prompt_name, p.image AS bot_picture, p.description AS prompt_description,
                   p.extensions_enabled, p.extensions_free_selection,
                   l.machine, l.model,
                   COALESCE(p.is_paid, 0) AS is_paid,
                   COALESCE(c.is_incognito, 0) AS is_incognito,
                   COALESCE(c.hidden_from_history, 0) AS hidden_from_history,
                   COALESCE(c.purge_on_close, 0) AS purge_on_close
            FROM CONVERSATIONS c
            LEFT JOIN PROMPTS p ON c.role_id = p.id
            LEFT JOIN LLM l ON c.llm_id = l.id
            WHERE c.id = ?
            """,
            (conversation_id,),
        )
        conv_row = await cursor.fetchone()

        fetch_limit = limit + 1
        if before_id is not None:
            await cursor.execute(
                """
                SELECT m.id AS message_id, m.conversation_id, m.user_id, u.username,
                       m.message, m.type, strftime('%Y-%m-%d %H:%M:%S', m.date) as date_utc,
                       m.is_bookmarked, m.llm_id, l.machine AS llm_machine, l.model AS llm_model,
                       m.citations_json
                FROM MESSAGES m
                LEFT JOIN USERS u ON m.user_id = u.id
                LEFT JOIN LLM l ON m.llm_id = l.id
                WHERE m.conversation_id = ? AND m.id < ?
                ORDER BY m.id DESC
                LIMIT ?
                """,
                (conversation_id, before_id, fetch_limit),
            )
        else:
            await cursor.execute(
                """
                SELECT m.id AS message_id, m.conversation_id, m.user_id, u.username,
                       m.message, m.type, strftime('%Y-%m-%d %H:%M:%S', m.date) as date_utc,
                       m.is_bookmarked, m.llm_id, l.machine AS llm_machine, l.model AS llm_model,
                       m.citations_json
                FROM MESSAGES m
                LEFT JOIN USERS u ON m.user_id = u.id
                LEFT JOIN LLM l ON m.llm_id = l.id
                WHERE m.conversation_id = ?
                ORDER BY m.id DESC
                LIMIT ?
                """,
                (conversation_id, fetch_limit),
            )

        rows = await cursor.fetchall()
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        extensions_data = {}
        if conv_row and conv_row["extensions_enabled"]:
            prompt_role_id = conv_row["role_id"]
            active_ext_id = conv_row["active_extension_id"]
            active_ext_data = None
            if active_ext_id:
                await cursor.execute(
                    "SELECT id, name, slug, description FROM PROMPT_EXTENSIONS WHERE id = ?",
                    (active_ext_id,),
                )
                ext_row = await cursor.fetchone()
                if ext_row:
                    active_ext_data = {
                        "id": ext_row["id"],
                        "name": ext_row["name"],
                        "slug": ext_row["slug"],
                        "description": ext_row["description"] or "",
                    }

            await cursor.execute(
                "SELECT id, name, slug, description FROM PROMPT_EXTENSIONS WHERE prompt_id = ? ORDER BY display_order",
                (prompt_role_id,),
            )
            ext_rows = await cursor.fetchall()
            all_extensions = [
                {"id": r["id"], "name": r["name"], "slug": r["slug"], "description": r["description"] or ""}
                for r in ext_rows
            ]

            extensions_data = {
                "extensions_enabled": True,
                "active_extension": active_ext_data,
                "extensions": all_extensions,
                "extensions_free_selection": bool(conv_row["extensions_free_selection"]),
            }

        await conn.close()

    empty_bot_ids = [
        row["message_id"]
        for row in rows
        if row["type"] == "bot" and (not row["message"] or not row["message"].strip())
    ]
    if empty_bot_ids:
        logger.warning(
            "Auto-repair: removing %s empty bot message(s) from conversation %s: %s",
            len(empty_bot_ids),
            conversation_id,
            empty_bot_ids,
        )
        async with get_db_connection(readonly=False) as write_conn:
            # Shared domain helper: clears memory-provider links, watchdog rows,
            # and sync watermarks so the auto-repair does not hit the enforced FK
            # on MESSAGES or leave orphaned links behind.
            await delete_message_rows(
                write_conn,
                conversation_id=conversation_id,
                message_ids=empty_bot_ids,
            )
            await write_conn.commit()
        empty_bot_set = set(empty_bot_ids)
        rows = [row for row in rows if row["message_id"] not in empty_bot_set]

    empty_user_ids = [
        row["message_id"]
        for row in rows
        if row["type"] == "user" and (not row["message"] or not row["message"].strip())
    ]
    if empty_user_ids:
        logger.warning(
            "Found %s empty USER message(s) in conversation %s: %s. Not auto-deleting.",
            len(empty_user_ids),
            conversation_id,
            empty_user_ids,
        )

    bot_avatar_urls = get_signed_bot_avatar_urls(
        conv_row["bot_picture"] if conv_row else None,
        current_user,
    )

    conversation_info = {
        "id": conv_row["id"],
        "prompt_name": conv_row["prompt_name"],
        "machine": conv_row["machine"],
        "model": conv_row["model"],
        "provider_health": touch_provider_activity(provider_from_machine(conv_row["machine"], conv_row["model"])),
        **bot_avatar_urls,
        "prompt_description": conv_row["prompt_description"],
        "is_paid": bool(conv_row["is_paid"]),
        "is_incognito": bool(conv_row["is_incognito"]),
        "hidden_from_history": bool(conv_row["hidden_from_history"]),
        "purge_on_close": bool(conv_row["purge_on_close"]),
        **extensions_data,
    }

    phone_history = await load_phone_history_page(
        get_db_connection,
        conversation_id=conversation_id,
        owner_user_id=current_user.id,
        message_ids=[int(row["message_id"]) for row in rows],
        newest_page=before_id is None,
        allow_admin=is_user_admin,
    )
    phone_history_payload = phone_history.public_payload()
    channel_provenance = await load_message_channel_provenance(
        [int(row["message_id"]) for row in rows if row["message_id"] is not None]
    )
    has_phone_history = bool(
        phone_history_payload["calls"] or phone_history_payload["markers"]
    )

    if is_user_admin:
        async with get_db_connection(readonly=True) as admin_conn:
            admin_cursor = await admin_conn.execute(
                "SELECT locked, locked_reason FROM CONVERSATIONS WHERE id = ?",
                (conversation_id,),
            )
            lock_row = await admin_cursor.fetchone()
            if lock_row:
                conversation_info["locked"] = bool(lock_row["locked"]) if lock_row["locked"] is not None else False
                conversation_info["locked_reason"] = lock_row["locked_reason"]
            admin_cursor = await admin_conn.execute(
                "SELECT COUNT(*) as msg_count, COALESCE(SUM(input_tokens_used + output_tokens_used), 0) as total_tokens FROM MESSAGES WHERE conversation_id = ?",
                (conversation_id,),
            )
            stats = await admin_cursor.fetchone()
            conversation_info["message_count"] = stats["msg_count"]
            conversation_info["total_tokens"] = stats["total_tokens"]

    if not rows:
        response_content = {
            "conversation_info": conversation_info,
            "messages": [],
            "has_more": False,
        }
        if has_phone_history:
            response_content["phone_history"] = phone_history_payload
        return JSONResponse(content=response_content)

    render_rows = [
        (
            row,
            strip_tagged_thinking_prefix(custom_unescape(row["message"]))
            if row["type"] == "bot"
            else custom_unescape(row["message"]),
        )
        for row in rows
        if row["message_id"] is not None
    ]
    attachment_records = await preload_attachment_records_for_messages(
        [(row["message_id"], message) for row, message in render_rows],
        user_id=current_user.id,
        conversation_id=conversation_id,
        allow_admin=is_user_admin,
    )

    messages_list = []
    for row, unescaped_message in render_rows:
        if row["message_id"] is not None:
            processed_message = await process_message(
                unescaped_message,
                request,
                current_user,
                media_owner_username=row["username"],
                conversation_id=conversation_id,
                message_id=row["message_id"],
                attachment_records=attachment_records,
                can_admin_view=is_user_admin,
            )
            msg_data = {
                "id": row["message_id"],
                "conversation_id": conversation_id,
                "user_id": row["user_id"],
                "username": row["username"],
                "message": processed_message,
                "type": row["type"],
                "date": row["date_utc"],
                "is_bookmarked": bool(row["is_bookmarked"]),
                "llm_id": row["llm_id"],
                "llm_machine": row["llm_machine"],
                "llm_model": row["llm_model"],
            }
            phone_metadata = phone_history.message_metadata.get(
                int(row["message_id"])
            )
            if phone_metadata is not None:
                msg_data.update(phone_metadata)
            msg_data.update(channel_provenance.get(int(row["message_id"]), {}))
            if row["citations_json"]:
                try:
                    msg_data["citations"] = orjson.loads(row["citations_json"])
                except (orjson.JSONDecodeError, Exception):
                    pass
            messages_list.append(msg_data)

    messages_list.reverse()
    response_content = {
        "conversation_info": conversation_info,
        "messages": messages_list,
        "has_more": has_more,
    }
    if has_phone_history:
        response_content["phone_history"] = phone_history_payload
    return JSONResponse(content=response_content)


@router.get("/api/conversations/{conversation_id}/provider-health")
async def get_conversation_provider_health(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()

    async with get_db_connection(readonly=True) as conn:
        is_user_admin = await current_user.is_admin
        if is_user_admin:
            cursor = await conn.execute(
                """
                SELECT l.machine, l.model
                FROM CONVERSATIONS c
                LEFT JOIN LLM l ON c.llm_id = l.id
                WHERE c.id = ?
                """,
                (conversation_id,),
            )
        else:
            cursor = await conn.execute(
                """
                SELECT l.machine, l.model
                FROM CONVERSATIONS c
                LEFT JOIN LLM l ON c.llm_id = l.id
                WHERE c.id = ? AND c.user_id = ?
                """,
                (conversation_id, current_user.id),
            )
        row = await cursor.fetchone()

    if not row:
        return JSONResponse(content={"error": "Conversation not found or access denied"}, status_code=404)

    provider = provider_from_machine(row["machine"], row["model"])
    return JSONResponse(content={"provider_health": touch_provider_activity(provider)})


@router.post("/api/conversations/{conversation_id}/messages")
async def save_message(
    request: Request,
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    text_compressed: Optional[UploadFile] = File(None),
    text_plain: Optional[str] = Form(None),
    file: List[Optional[UploadFile]] = File(None),
    full_response: bool = Form(False),
    is_whatsapp: bool = Form(False),
    reasoning_mode: Optional[str] = Form(None),
    reasoning_budget_tokens: Optional[int] = Form(None),
    thinking_budget_tokens: Optional[int] = Form(None),
    multi_ai_models: Optional[str] = Form(None),
    pdf_page_start: Optional[int] = Form(None),
    pdf_page_end: Optional[int] = Form(None),
    pdf_retry_token: Optional[str] = Form(None),
    attachment_refs: Optional[str] = Form(None),
    expected_llm_id: Optional[int] = Form(None),
):
    logger.info("enters in save_message (wrapper)")
    if current_user is None:
        return unauthenticated_response()

    async with get_db_connection(readonly=True) as conn:
        identity_cursor = await conn.execute(
            """
            SELECT c.locked, c.llm_id, l.machine, l.model, l.capabilities_json
            FROM CONVERSATIONS c
            JOIN LLM l ON l.id = c.llm_id
            WHERE c.id = ? AND c.user_id = ?
            """,
            (conversation_id, current_user.id),
        )
        conversation_identity = await identity_cursor.fetchone()

    if not conversation_identity or conversation_identity[0]:
        return JSONResponse(
            content={"success": False, "message": "Conversation is locked."},
            status_code=403,
        )

    guard_response = await validate_message_request(
        request=request,
        current_user=current_user,
        is_whatsapp=is_whatsapp,
    )
    if guard_response is not None:
        return guard_response

    try:
        parsed_attachment_refs = parse_attachment_refs_value(attachment_refs)
    except ValueError as exc:
        return JSONResponse(content={"success": False, "message": str(exc)}, status_code=400)

    foreground_turn = await capture_non_phone_channel_turn(
        conversation_id=conversation_id,
        channel="web",
        connection_factory=get_db_connection,
    )

    # Exact model identity fences inference in concurrent browser tabs.  It is
    # irrelevant when the phone owns foreground and this request only persists
    # the authenticated inbound turn.
    if not foreground_turn.decision.phone_active and not multi_ai_models:
        if expected_llm_id is None or expected_llm_id <= 0:
            return JSONResponse(
                content={
                    "success": False,
                    "error_code": "expected_llm_id_required",
                    "message": "Reload the conversation so Aurvek can confirm the selected AI model.",
                },
                status_code=428,
            )
        if int(conversation_identity[1]) != expected_llm_id:
            return JSONResponse(
                content={
                    "success": False,
                    "error_code": "conversation_model_changed",
                    "message": "The AI model changed in another session. Review it and send again.",
                    "llm_id": int(conversation_identity[1]),
                    "model": conversation_identity["model"],
                },
                status_code=409,
            )

    requested_reasoning = None
    if not foreground_turn.decision.phone_active:
        try:
            requested_reasoning = _reasoning_selection_from_form(
                reasoning_mode,
                reasoning_budget_tokens,
                thinking_budget_tokens,
            )
        except ReasoningValidationError as exc:
            return JSONResponse(
                content={
                    "success": False,
                    "error_code": "invalid_reasoning_selection",
                    "message": str(exc),
                },
                status_code=422,
            )

    parsed_model_ids = None
    if multi_ai_models and not foreground_turn.decision.phone_active:
        try:
            parsed_model_ids = orjson.loads(multi_ai_models)
        except orjson.JSONDecodeError:
            return JSONResponse(content={"error": "Invalid multi_ai_models format"}, status_code=400)
        if (
            not isinstance(parsed_model_ids, list)
            or len(parsed_model_ids) < 2
            or len(parsed_model_ids) > 4
            or not all(isinstance(model_id, int) for model_id in parsed_model_ids)
        ):
            return JSONResponse(content={"error": "Multi-AI requires 2-4 model IDs"}, status_code=400)

    if not foreground_turn.decision.phone_active and not multi_ai_models:
        try:
            reasoning_capabilities = await _request_reasoning_capabilities(
                machine=conversation_identity["machine"],
                model=conversation_identity["model"],
                capabilities_json=conversation_identity["capabilities_json"],
                user_id=current_user.id,
            )
            requested_reasoning = resolve_and_validate(
                requested_reasoning,
                reasoning_capabilities,
            )
        except ReasoningValidationError as exc:
            return JSONResponse(
                content={
                    "success": False,
                    "error_code": "invalid_reasoning_selection",
                    "message": str(exc),
                },
                status_code=422,
            )

    # API credentials are an inference gate, not an inbound-message gate.  A
    # phone-owned conversation still accepts and queues the authenticated web
    # turn even when the account could not start a new provider request.
    user_api_keys = None
    if not foreground_turn.decision.phone_active:
        user_keys_header = request.headers.get("X-User-API-Keys")
        if user_keys_header:
            try:
                user_api_keys = orjson.loads(base64.b64decode(user_keys_header))
                logger.debug("User API keys received from header")
            except Exception as exc:
                logger.warning("Failed to parse user API keys from header: %s", exc)

        if not user_api_keys:
            try:
                async with get_db_connection(readonly=True) as conn:
                    cursor = await conn.cursor()
                    await cursor.execute(
                        "SELECT user_api_keys FROM USER_DETAILS WHERE user_id = ?",
                        (current_user.id,),
                    )
                    result = await cursor.fetchone()
                    if result and result[0]:
                        keys_json = decrypt_api_key(result[0])
                        if keys_json:
                            user_api_keys = orjson.loads(keys_json)
                            logger.debug("User API keys loaded from server storage")
            except Exception as exc:
                logger.warning("Failed to load user API keys from server: %s", exc)

        api_key_mode = await get_user_api_key_mode(current_user.id)
        if api_key_mode == API_KEY_MODE_OWN_ONLY and not user_api_keys:
            # A linked ChatGPT subscription is also a user-owned credential.
            gptsub_request_allowed = False
            if not multi_ai_models and conversation_identity["machine"] == "GPTSub":
                try:
                    from subscription_auth.gate import gptsub_allowed

                    gptsub_request_allowed = await gptsub_allowed(
                        current_user,
                        model=conversation_identity["model"],
                    )
                except Exception:
                    gptsub_request_allowed = False

            if not gptsub_request_allowed:
                return JSONResponse(
                    content={
                        "error": "api_keys_required",
                        "message": "Your account requires you to configure your own API keys to use AI services.",
                        "action": "configure_api_keys",
                        "redirect": "/profile/api-credentials",
                    },
                    status_code=403,
                )

    if multi_ai_models and not foreground_turn.decision.phone_active:
        async with get_db_connection(readonly=True) as conn_gs_check:
            gs_row = await conn_gs_check.execute(
                "SELECT COALESCE(ep.gransabio_enabled, 0) FROM CONVERSATIONS c "
                "LEFT JOIN USER_DETAILS ud ON ud.user_id = c.user_id "
                "LEFT JOIN PROMPTS ep ON ep.id = COALESCE(c.role_id, ud.current_prompt_id) "
                "WHERE c.id = ?",
                (conversation_id,),
            )
            gs_result = await gs_row.fetchone()
        if gs_result and bool(gs_result[0]):
            return JSONResponse(
                content={"success": False, "message": "This prompt uses GranSabio pipeline and cannot use Multi-AI comparison mode."},
                status_code=400,
            )

        try:
            is_whatsapp_conv = bool(is_whatsapp)
            if not is_whatsapp_conv:
                try:
                    is_whatsapp_conv = await is_whatsapp_conversation(conversation_id)
                except Exception as exc:
                    logger.warning("[save_message] Could not verify WhatsApp status for conversation %s: %s", conversation_id, exc)
                    return JSONResponse(content={"error": "Could not verify conversation channel"}, status_code=503)
            if is_whatsapp_conv:
                return JSONResponse(content={"error": "Multi-AI is not available via WhatsApp"}, status_code=400)

            if file and any(f for f in file if f and f.filename):
                return JSONResponse(content={"error": "File attachments are not supported in Multi-AI mode"}, status_code=400)
            if parsed_attachment_refs:
                return JSONResponse(content={"error": "File attachments are not supported in Multi-AI mode"}, status_code=400)

            max_decompressed_size = 10 * 1024 * 1024
            max_compressed_size = 1 * 1024 * 1024
            if text_compressed:
                compressed_bytes = await text_compressed.read()
                if len(compressed_bytes) > max_compressed_size:
                    return JSONResponse(content={"error": "Compressed message too large"}, status_code=400)
                decompressor = zlib.decompressobj()
                decompressed = decompressor.decompress(compressed_bytes, max_length=max_decompressed_size)
                if decompressor.unconsumed_tail:
                    return JSONResponse(content={"error": "Decompressed message exceeds size limit"}, status_code=400)
                multi_user_message = decompressed.decode("utf-8")
            elif text_plain:
                multi_user_message = text_plain
            else:
                return JSONResponse(content={"error": "No message provided"}, status_code=400)

            return StreamingResponse(
                serialize_user_billing_stream(
                    current_user.id,
                    process_multi_ai_message(
                        request=request,
                        conversation_id=conversation_id,
                        current_user=current_user,
                        user_message=multi_user_message,
                        model_ids=parsed_model_ids,
                        reasoning_selection=requested_reasoning,
                        user_api_keys=user_api_keys,
                        channel_context=foreground_turn.context,
                    ),
                ),
                media_type="text/event-stream",
            )
        except orjson.JSONDecodeError:
            return JSONResponse(content={"error": "Invalid multi_ai_models format"}, status_code=400)

    files = None
    if file:
        valid_files = [f for f in file if f]
        if valid_files and not current_user.can_send_files:
            return JSONResponse(
                content={"success": False, "message": "File uploads are not enabled for your account"},
                status_code=403,
            )

        max_files_per_message = 16
        if len(valid_files) > max_files_per_message or len(valid_files) + len(parsed_attachment_refs) > max_files_per_message:
            return JSONResponse(
                content={"success": False, "message": f"Maximum {max_files_per_message} files per message."},
                status_code=400,
            )

        files = []
        for uploaded_file in valid_files:
            if uploaded_file.content_type == "application/pdf":
                max_bytes = MAX_PDF_SIZE_MB * 1024 * 1024
            elif is_text_file(uploaded_file.content_type, uploaded_file.filename):
                max_bytes = MAX_TEXT_FILE_SIZE_MB * 1024 * 1024
            elif uploaded_file.content_type and uploaded_file.content_type.startswith("image/"):
                max_bytes = MAX_RAW_UPLOAD_SIZE_MB * 1024 * 1024
            else:
                max_bytes = MAX_TEXT_FILE_SIZE_MB * 1024 * 1024

            data = await uploaded_file.read(max_bytes + 1)
            if len(data) > max_bytes:
                return JSONResponse(
                    content={"success": False, "message": f"File '{uploaded_file.filename}' exceeds the {max_bytes // (1024 * 1024)}MB size limit"},
                    status_code=400,
                )
            files.append({
                "data": data,
                "content_type": (uploaded_file.content_type or "").lower(),
                "filename": uploaded_file.filename,
            })

    text_compressed_bytes = None
    if text_compressed:
        text_compressed_bytes = await text_compressed.read()

    return await serialize_user_billing_response(
        current_user.id,
        process_save_message(
            request=request,
            conversation_id=conversation_id,
            current_user=current_user,
            text_compressed=text_compressed_bytes,
            text_plain=text_plain,
            files=files,
            full_response=full_response,
            is_whatsapp=is_whatsapp,
            reasoning_selection=requested_reasoning,
            user_api_keys=user_api_keys,
            prevalidated=True,
            pdf_page_start=pdf_page_start,
            pdf_page_end=pdf_page_end,
            pdf_retry_token=pdf_retry_token,
            attachment_refs=parsed_attachment_refs,
            expected_llm_id=expected_llm_id,
            channel_context=foreground_turn.context,
        ),
    )
