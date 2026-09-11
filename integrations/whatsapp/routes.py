import os

import orjson
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from auth import get_user_from_phone_number, get_user_by_id
from billing.usage_reservations import serialize_user_billing_response
from clients import async_twilio, twilio_validator
from common import (
    PRIMARY_APP_DOMAIN,
    cache_directory,
    validate_twilio_media_url,
)
from database import get_db_connection
from file_storage import create_pending_audio_attachment, discard_pending_attachments
from chat.services.localization import chat_translator
from integrations.conversations import (
    escape_markdown,
    can_use_platform,
    change_response_mode,
    create_new_platform_conversation,
    ensure_platform_conversation,
    get_chats_list,
    set_external_conversation,
)
from integrations.media import (
    download_external_audio,
    download_external_media,
    transcribe_external_audio_detailed,
)
from integrations.messaging_voice_notes.service import (
    attach_message_channel_provenance,
    get_voice_note_retention_enabled,
)
from integrations.telephony.channel_context import capture_non_phone_channel_turn
from integrations.whatsapp.service import get_phone_user_not_found
from integrations.whatsapp import ingress
from log_config import logger
from prompt_access import get_user_accessible_prompts
from prompts import can_user_access_prompt
from save_images import get_or_generate_img_token
from storage_quota import StorageQuotaExceededError
from tools.tts import handle_tts_request, insert_tts_break

from integrations.applications.messaging import (
    resolve_messaging, application_command, application_messaging_turn,
    check_messaging, AdmittedMessagingClient, messaging_tts_billing,
)
from integrations.embed.models import EmbedError
from ai_runtime.channel_turns import StaleChannelTurnError
from ai_runtime.messages import process_save_message, storage_quota_notice_from_response


router = APIRouter()


def _whatsapp_audio_content_kind(media_type: str | None) -> str:
    normalized = str(media_type or "").split(";", 1)[0].strip().lower()
    if any(marker in normalized for marker in ("ogg", "opus", "amr")):
        return "voice_note"
    return "audio"


async def _discard_voice_note_attachment(
    voice_note: dict | None,
    reason: str,
) -> None:
    attachment_ref = (
        voice_note.get("audio_attachment_ref")
        if isinstance(voice_note, dict)
        else None
    )
    if not attachment_ref:
        return
    try:
        await discard_pending_attachments([str(attachment_ref)], reason)
    except Exception:
        logger.exception("Could not discard pending WhatsApp voice-note audio")


async def _prepare_whatsapp_audio(
    *,
    user_id: int,
    conversation_id: int,
    media_url: str,
    media_type: str,
    user_agent: str | None,
    application_state=None,
) -> tuple[str, dict]:
    """Download once, optionally retain, and transcribe one inbound audio item."""
    await check_messaging(application_state, "stt")
    audio_content = await download_external_audio(media_url, max_bytes=16 * 1024 * 1024, total_timeout=60)
    attachment_ref = None
    retention_status = "disabled"

    if await get_voice_note_retention_enabled("whatsapp"):
        try:
            await check_messaging(application_state, "stt")
            pending = await create_pending_audio_attachment(
                user_id=int(user_id),
                conversation_id=int(conversation_id),
                data=audio_content,
                filename="whatsapp-voice-note",
                mime_detected=media_type or "audio/ogg",
                declared_mime=media_type or None,
            )
        except StorageQuotaExceededError:
            retention_status = "quota_skipped"
            logger.info(
                "WhatsApp voice-note retention skipped because user %s is over quota",
                user_id,
            )
        except Exception:
            retention_status = "failed"
            logger.exception(
                "Could not retain WhatsApp voice-note audio for conversation %s",
                conversation_id,
            )
        else:
            attachment_ref = pending.public_id
            retention_status = "stored"

    try:
        await check_messaging(application_state, "stt")
        transcription = await transcribe_external_audio_detailed(
            user_id=int(user_id),
            audio_content=audio_content,
            user_agent=user_agent,
        )
    except Exception:
        if attachment_ref:
            await _discard_voice_note_attachment(
                {"audio_attachment_ref": attachment_ref},
                "whatsapp_transcription_failed",
            )
        raise

    voice_note = {
        "transcript": transcription.text,
        "stt_provider": transcription.provider,
        "stt_model": transcription.model,
        "duration_seconds": transcription.duration_seconds,
        "retention_status": retention_status,
        "audio_attachment_ref": attachment_ref,
    }
    return transcription.text, voice_note


async def _release_whatsapp_retry_marker(message_sid: str | None) -> None:
    """Let Twilio retry a webhook whose inbound turn was not persisted."""
    if not message_sid:
        return
    try:
        async with get_db_connection() as conn:
            await conn.execute(
                "DELETE FROM WHATSAPP_PROCESSED_MESSAGES WHERE message_sid = ?",
                (message_sid,),
            )
            await conn.commit()
    except Exception:
        logger.exception("Could not release WhatsApp retry marker %s", message_sid)


async def _whatsapp_retry_response(message_sid: str | None) -> JSONResponse:
    await _release_whatsapp_retry_marker(message_sid)
    return JSONResponse(
        content={"status": "retry", "error": "inbound_not_persisted"},
        status_code=503,
    )


async def _buffer_whatsapp_runtime_output(body_iterator):
    """Consume the whole runtime stream before any irreversible delivery."""
    buffered_items = []
    text_parts = []
    terminal = None
    persistence_error = False
    durable_message_ids = False

    def flush_text() -> None:
        if text_parts:
            buffered_items.append("".join(text_parts))
            text_parts.clear()

    try:
        async for chunk in body_iterator:
            if chunk is None:
                persistence_error = True
                continue
            chunk_str = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
            for line in chunk_str.split("\n"):
                if line[:5] != "data:":
                    continue
                try:
                    data = orjson.loads(line[5:].strip())
                except orjson.JSONDecodeError:
                    logger.error("Error decoding WhatsApp runtime JSON: %s", line)
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("terminal"):
                    terminal = str(data["terminal"])
                if data.get("persistence_error") is True:
                    persistence_error = True
                message_ids = data.get("message_ids")
                if isinstance(message_ids, dict):
                    durable_message_ids = bool(
                        message_ids.get("user") and message_ids.get("bot")
                    )
                content = data.get("content", "")
                if isinstance(content, (list, dict)):
                    flush_text()
                    buffered_items.append(content)
                elif isinstance(content, str) and content:
                    text_parts.append(content)
    except Exception:
        logger.exception("WhatsApp runtime stream failed before durable completion")
        persistence_error = True

    flush_text()
    if persistence_error:
        durable_message_ids = False
    return buffered_items, terminal, persistence_error, durable_message_ids

@router.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    return await _authenticated_whatsapp_webhook(request, async_twilio, twilio_validator)


@router.post("/whatsapp/{receiver_id}")
async def whatsapp_receiver_webhook(receiver_id: str, request: Request):
    if not request.headers.get("X-Twilio-Signature"):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    from integrations.applications.channels import get_application_channel_service
    from twilio_async import AsyncTwilioClient
    from twilio.request_validator import RequestValidator
    receiver = await get_application_channel_service().get_receiver(receiver_id)
    if receiver is None or receiver.channel != "whatsapp" or not receiver.enabled:
        raise HTTPException(status_code=404, detail="Receiver unavailable")
    token = os.environ.get(receiver.twilio_auth_token_env or "", "")
    if not token:
        raise HTTPException(status_code=503, detail="Receiver authentication unavailable")
    client = AsyncTwilioClient(receiver.provider_account_id, token)
    try:
        return await _authenticated_whatsapp_webhook(request, client,
            RequestValidator(token), receiver.receiver_key)
    finally:
        await client.close()


async def _authenticated_whatsapp_webhook(request, client, validator, receiver_key=None):
    if client is None:
        logger.warning("WhatsApp webhook called but Twilio is not configured")
        return Response(content="<Response></Response>", media_type="application/xml", status_code=200)

    # Security: Validate Twilio signature to prevent spoofed requests
    if validator:
        signature = request.headers.get("X-Twilio-Signature", "")
        if not signature:
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")
        # Reconstruct the full URL that Twilio signed.
        # Use PRIMARY_APP_DOMAIN to build the canonical URL, avoiding proxy header
        # fragility (works with nginx, Cloudflare Tunnel, or any other reverse proxy).
        url = f"https://{PRIMARY_APP_DOMAIN}{request.url.path}"
        if request.url.query:
            url += f"?{request.url.query}"

        form_data = await ingress.read_form(request)
        # Convert form data to dict for validation
        params = {key: form_data[key] for key in form_data}

        is_valid = validator.validate(url, params, signature)
        if not is_valid:
            logger.warning(f"Invalid Twilio signature from {request.client.host}")
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")

        data = form_data
    else:
        raise HTTPException(status_code=503, detail="Webhook authentication unavailable")

    from integrations.applications.channel_models import VerifiedChannelEnvelope
    try:
        account_id = str(data.get("AccountSid") or "")
        if account_id != client.account_sid:
            raise HTTPException(status_code=403, detail="Invalid Twilio account")
        if receiver_key is not None and str(data.get("To") or "").removeprefix("whatsapp:") != receiver_key:
            raise HTTPException(status_code=403, detail="Invalid Twilio receiver")
        envelope = VerifiedChannelEnvelope(channel="whatsapp", provider="twilio",
            receiver_key=str(data.get("To") or "").removeprefix("whatsapp:"),
            provider_identity=str(data.get("From") or "").removeprefix("whatsapp:"),
            session_key="messaging", event_id=str(data.get("MessageSid") or ""),
            provider_account_id=account_id)
        async with ingress.admit(envelope) as admitted:
            if not admitted:
                return Response(content="<Response></Response>", media_type="application/xml")
            state, reply = await resolve_messaging(envelope, str(data.get("Body") or ""))
            if reply is not None:
                if await ingress.allow_control_reply(data, connected=state is not None):
                    await client.send_message(body=reply, from_=data.get("To"), to=data.get("From"))
                return {"status": "success"}
            async with application_messaging_turn(state):
                return await _process_whatsapp_message(request, data, application_state=state,
                    twilio_client=AdmittedMessagingClient(client, state))
    except (EmbedError, ValueError):
        return {"status": "denied"}


async def _process_whatsapp_message(request, data, *, application_state=None, twilio_client):
    message_body = (data.get("Body") or "").strip()
    from_number = data.get("From")
    to_number = data.get("To")
    # Collect all media from Twilio (supports up to 10 items)
    media_items = []
    for i in range(10):
        m_url = data.get(f"MediaUrl{i}")
        m_type = data.get(f"MediaContentType{i}")
        if m_url:
            media_items.append({"url": m_url, "type": m_type or ""})
        else:
            break
    media_url = media_items[0]["url"] if media_items else None
    media_type = media_items[0]["type"] if media_items else None

    # Authentication, deduplication and capacity limits precede all handlers,
    # including replies to unlinked senders, in the webhook adapter.
    message_sid = data.get("MessageSid")

    tr = chat_translator()

    # Security: Validate all media URLs to prevent SSRF attacks
    valid_media_items = []
    for item in media_items:
        if validate_twilio_media_url(item["url"]):
            valid_media_items.append(item)
        else:
            logger.warning(f"Rejected invalid media URL in WhatsApp webhook: {item['url'][:100]}")
    media_items = valid_media_items
    media_url = media_items[0]["url"] if media_items else None
    media_type = media_items[0]["type"] if media_items else None

    voice_note_metadata = None
    try:
        current_user = (await get_user_by_id(application_state.admission.scope.user_id)
                        if application_state is not None else await get_user_from_phone_number(from_number))
        tr = chat_translator(current_user)
        if current_user is None:
            response_text = get_phone_user_not_found(tr)
            if await ingress.allow_control_reply(data):
                await twilio_client.send_message(body=response_text, from_=to_number, to=from_number)
            return {"status": "success", "message": "User not found"}
        logger.debug(f"WhatsApp message from user: {current_user.username}")

        if not current_user.is_enabled:
            return {"status": "success"}

        if application_state is not None:
            command_reply = await application_command(application_state, message_body, message_sid, translator=tr)
            if command_reply is not None:
                await twilio_client.send_message(body=command_reply, from_=to_number, to=from_number)
                return {"status": "success"}
            conversation_id = application_state.admission.scope.conversation_id
            answer_mode = application_state.admission.response_mode
        else:
            message_lower = message_body.lower()

            if message_lower == "!help":
                help_text = (
                    tr.t("channel_notices.help_whatsapp")
                )
                await twilio_client.send_message(body=help_text, from_=to_number, to=from_number)
                return {"status": "success", "message": "Help sent"}

            async with get_db_connection(readonly=True) as conn:
                cursor = await conn.cursor()
                await cursor.execute(
                    "SELECT external_platforms FROM USER_DETAILS WHERE user_id = ?",
                    (current_user.id,),
                )
                result = await cursor.fetchone()
                platforms = orjson.loads(result[0]) if result and result[0] else {}
                whatsapp_data = platforms.get("whatsapp") or {}
                if not isinstance(whatsapp_data, dict):
                    whatsapp_data = {}
                is_first_whatsapp = not whatsapp_data

            async with get_db_connection(readonly=True) as conn:
                cursor = await conn.cursor()
                ok, _, err_msg = await can_use_platform(current_user.id, "whatsapp", cursor, translator=tr)
                if not ok:
                    await twilio_client.send_message(
                        body=err_msg,
                        from_=to_number,
                        to=from_number,
                    )
                    return {"status": "success"}

            if message_lower == "!chats":
                async with get_db_connection(readonly=True) as conn:
                    chats_message = await get_chats_list(
                        current_user.id,
                        "whatsapp",
                        conn,
                        translator=tr,
                        markdown=True,
                    )
                await twilio_client.send_message(body=chats_message, from_=to_number, to=from_number)
                return {"status": "success"}

            if message_lower == "!set" or message_lower.startswith("!set "):
                parts = message_body[4:].strip().split() if len(message_body) > 4 else []
                if not parts or len(parts) > 2:
                    await twilio_client.send_message(
                        body=tr.t("channel_notices.set_usage"),
                        from_=to_number,
                        to=from_number,
                    )
                    return {"status": "success"}

                raw_id = parts[0]
                clean_id = raw_id[1:] if raw_id.startswith("#") else raw_id
                if not clean_id.isdigit() or int(clean_id) <= 0:
                    await twilio_client.send_message(
                        body=tr.t("channel_notices.set_usage"),
                        from_=to_number,
                        to=from_number,
                    )
                    return {"status": "success"}

                target_platform = "whatsapp"
                if len(parts) == 2:
                    platform_map = {
                        "whatsapp": "whatsapp",
                        "wa": "whatsapp",
                        "telegram": "telegram",
                        "tg": "telegram",
                    }
                    target_platform = platform_map.get(parts[1].lower())
                    if target_platform is None:
                        await twilio_client.send_message(
                            body=tr.t("channel_notices.invalid_platform"),
                            from_=to_number,
                            to=from_number,
                        )
                        return {"status": "success"}

                result = await set_external_conversation(
                    current_user.id,
                    int(clean_id),
                    target_platform,
                    "whatsapp",
                    translator=tr,
                )
                await twilio_client.send_message(
                    body=result["message"],
                    from_=to_number,
                    to=from_number,
                )
                return {"status": "success"}

            if message_lower in ["text_mode", "text mode", "!text"]:
                async with get_db_connection() as conn:
                    confirmation_message = await change_response_mode(current_user.id, "text", conn=conn, translator=tr)
                await twilio_client.send_message(
                    body=confirmation_message,
                    from_=to_number,
                    to=from_number,
                )
                return {"status": "success", "message": confirmation_message}

            if message_lower in ["voice_mode", "voice mode", "!voice"]:
                async with get_db_connection() as conn:
                    confirmation_message = await change_response_mode(current_user.id, "voice", conn=conn, translator=tr)
                await twilio_client.send_message(
                    body=confirmation_message,
                    from_=to_number,
                    to=from_number,
                )
                return {"status": "success", "message": confirmation_message}

            if message_lower == "!prompt list":
                async with get_db_connection(readonly=True) as conn:
                    cursor = await conn.cursor()
                    ud_cursor = await conn.execute(
                        "SELECT all_prompts_access, public_prompts_access, category_access FROM USER_DETAILS WHERE user_id = ?",
                        (current_user.id,),
                    )
                    ud_row = await ud_cursor.fetchone()
                    prompts_list = await get_user_accessible_prompts(
                        current_user,
                        cursor,
                        all_prompts_access=ud_row[0] if ud_row else False,
                        public_prompts_access=ud_row[1] if ud_row else False,
                        category_access=ud_row[2] if ud_row else None,
                    )

                if not prompts_list:
                    await twilio_client.send_message(body=tr.t("channel_notices.no_prompts"), from_=to_number, to=from_number)
                    return {"status": "success"}

                prompt_lines = [f"*{p['id']}* - {escape_markdown(p['name'])}" for p in prompts_list[:20]]
                msg = tr.t("channel_notices.prompts_header") + "\n".join(prompt_lines)
                if len(prompts_list) > 20:
                    msg += tr.t("channel_notices.prompts_more", count=len(prompts_list) - 20)
                msg += tr.t("channel_notices.prompts_footer")

                await twilio_client.send_message(body=msg, from_=to_number, to=from_number)
                return {"status": "success"}

            if message_lower == "!new":
                await create_new_platform_conversation(current_user.id, "whatsapp", current_user)
                await twilio_client.send_message(
                    body=tr.t("channel_notices.new_conversation"),
                    from_=to_number,
                    to=from_number,
                )
                return {"status": "success"}

            whatsapp_data, created_binding = await ensure_platform_conversation(
                current_user.id,
                "whatsapp",
                current_user,
            )
            if created_binding and is_first_whatsapp:
                try:
                    async with get_db_connection(readonly=True) as config_conn:
                        config_cursor = await config_conn.execute(
                            "SELECT value FROM SYSTEM_CONFIG WHERE key = 'whatsapp_welcome_message'"
                        )
                        row = await config_cursor.fetchone()
                    welcome_template = row[0] if row else None
                    if not welcome_template:
                        welcome_template = os.getenv("WHATSAPP_WELCOME_MESSAGE", "")
                    if welcome_template:
                        welcome_msg = welcome_template.replace("{username}", current_user.username)
                        await twilio_client.send_message(
                            body=welcome_msg,
                            from_=to_number,
                            to=from_number,
                        )
                except Exception as welcome_err:
                    logger.error(f"Failed to send WhatsApp welcome message: {welcome_err}")

            conversation_id = whatsapp_data["conversation_id"]
            answer_mode = whatsapp_data.get("answer", "text")

            logger.debug(f"WhatsApp response mode: {answer_mode}")
            logger.debug("WhatsApp message body received")

            async with get_db_connection(readonly=True) as conn:
                cursor = await conn.cursor()
                await cursor.execute(
                    "SELECT locked FROM CONVERSATIONS WHERE id = ? AND user_id = ?",
                    (conversation_id, current_user.id),
                )
                lock_row = await cursor.fetchone()
                if not lock_row:
                    await twilio_client.send_message(
                        body=tr.t("channel_notices.conversation_missing_new"),
                        from_=to_number,
                        to=from_number,
                    )
                    return {"status": "success", "message": "Conversation not found"}
                if lock_row[0]:
                    async with get_db_connection() as log_conn:
                        await log_conn.execute(
                            "INSERT INTO WHATSAPP_LOG (user_id, phone_number, direction, message_type, response_mode) VALUES (?, ?, 'in', 'text', ?)",
                            (current_user.id, from_number, answer_mode),
                        )
                        await log_conn.commit()
                    logger.info(
                        f"WhatsApp message blocked: conversation {conversation_id} locked for user {current_user.id}"
                    )
                    await twilio_client.send_message(
                        body=tr.t("channel_notices.conversation_locked_new"),
                        from_=to_number,
                        to=from_number,
                    )
                    return {"status": "success", "message": "Conversation locked"}

            async with get_db_connection(readonly=True) as conn:
                cursor = await conn.cursor()
                await cursor.execute(
                    "SELECT external_platforms FROM USER_DETAILS WHERE user_id = ?",
                    (current_user.id,),
                )
                row = await cursor.fetchone()
                if row and row[0]:
                    fresh_platforms = orjson.loads(row[0])
                    fresh_conv_id = (fresh_platforms.get("whatsapp", {}) or {}).get("conversation_id")
                    if fresh_conv_id and fresh_conv_id != conversation_id:
                        conversation_id = fresh_conv_id
                        await cursor.execute(
                            "SELECT locked FROM CONVERSATIONS WHERE id = ? AND user_id = ?",
                            (conversation_id, current_user.id),
                        )
                        lock_row = await cursor.fetchone()
                        if not lock_row:
                            await twilio_client.send_message(
                                body=tr.t("channel_notices.conversation_missing_new"),
                                from_=to_number,
                                to=from_number,
                            )
                            return {"status": "success", "message": "Conversation not found"}
                        if lock_row[0]:
                            await twilio_client.send_message(
                                body=tr.t("channel_notices.conversation_locked_new"),
                                from_=to_number,
                                to=from_number,
                            )
                            return {"status": "success", "message": "Conversation locked"}

            if message_lower.startswith("!prompt ") and message_lower != "!prompt list":
                prompt_query = message_body[8:].strip()

                async with get_db_connection() as conn:
                    cursor = await conn.cursor()
                    target_prompt = None
                    if prompt_query.isdigit():
                        p_cursor = await conn.execute(
                            "SELECT id, name FROM PROMPTS WHERE id = ?",
                            (int(prompt_query),),
                        )
                        target_prompt = await p_cursor.fetchone()

                    if not target_prompt:
                        p_cursor = await conn.execute(
                            "SELECT id, name FROM PROMPTS WHERE LOWER(name) = LOWER(?)",
                            (prompt_query,),
                        )
                        target_prompt = await p_cursor.fetchone()

                    if not target_prompt:
                        p_cursor = await conn.execute(
                            "SELECT id, name FROM PROMPTS WHERE LOWER(name) LIKE LOWER(?)",
                            (f"%{prompt_query}%",),
                        )
                        target_prompt = await p_cursor.fetchone()

                    if not target_prompt:
                        await twilio_client.send_message(
                            body=tr.t("channel_notices.prompt_missing", name=escape_markdown(prompt_query)),
                            from_=to_number,
                            to=from_number,
                        )
                        return {"status": "success"}

                    if not await can_user_access_prompt(current_user, target_prompt[0], cursor):
                        await twilio_client.send_message(
                            body=tr.t("channel_notices.prompt_denied"),
                            from_=to_number,
                            to=from_number,
                        )
                        return {"status": "success"}

                    update_cursor = await cursor.execute(
                        """
                        UPDATE CONVERSATIONS
                        SET role_id = ?
                        WHERE id = ? AND user_id = ? AND COALESCE(locked, 0) = 0
                        """,
                        (target_prompt[0], conversation_id, current_user.id),
                    )
                    if update_cursor.rowcount == 0:
                        await conn.rollback()
                        await twilio_client.send_message(
                            body=tr.t("channel_notices.conversation_locked_new"),
                            from_=to_number,
                            to=from_number,
                        )
                        return {"status": "success"}

                    await conn.commit()

                    await twilio_client.send_message(
                        body=tr.t("channel_notices.prompt_changed", name=escape_markdown(target_prompt[1])),
                        from_=to_number,
                        to=from_number,
                    )
                    return {"status": "success"}

        if answer_mode == "voice":
            await check_messaging(application_state, "tts")
        for item in media_items:
            kind = str(item["type"]).lower()
            capability = "stt" if "audio" in kind or kind.startswith("application/ogg") else "attachments"
            await check_messaging(application_state, capability)
        transcribed_text = ""
        audio_content_kind = None
        file_dict = None
        files_list = []  # Support for multiple files

        if media_items:
            for media_item in media_items:
                m_url = media_item["url"]
                m_type = media_item["type"]

                normalized_media_type = str(m_type or "").strip().lower()
                if "audio" in normalized_media_type or normalized_media_type.startswith("application/ogg"):
                    try:
                        await _discard_voice_note_attachment(
                            voice_note_metadata,
                            "whatsapp_additional_audio_replaced",
                        )
                        transcribed_text, voice_note_metadata = (
                            await _prepare_whatsapp_audio(
                                user_id=current_user.id,
                                conversation_id=conversation_id,
                                media_url=m_url,
                                media_type=m_type,
                                user_agent=request.headers.get("user-agent"),
                                application_state=application_state,
                            )
                        )
                        audio_content_kind = _whatsapp_audio_content_kind(m_type)
                    except Exception as e:
                        logger.error(f"Error transcribing audio: {e}")
                        await twilio_client.send_message(
                            body=tr.t("channel_notices.audio_failed"),
                            from_=to_number,
                            to=from_number
                        )
                        return {"status": "error", "message": "Error transcribing audio"}
                elif "image" in m_type:
                    try:
                        await check_messaging(application_state, "attachments")
                        img_data = await download_external_media(m_url, max_bytes=5 * 1024 * 1024, total_timeout=60)
                        files_list.append({
                            'data': img_data,
                            'content_type': m_type,
                            'filename': f"image_{len(files_list)}.jpg"
                        })
                    except Exception as e:
                        logger.error(f"Error downloading image: {e}")
                elif "pdf" in m_type or "document" in m_type:
                    await _discard_voice_note_attachment(
                        voice_note_metadata,
                        "whatsapp_unsupported_document",
                    )
                    await twilio_client.send_message(
                        body=tr.t("channel_notices.documents_unsupported"),
                        from_=to_number,
                        to=from_number
                    )
                    return {"status": "success", "message": "Unsupported media type"}
                else:
                    logger.warning(f"Unsupported WhatsApp media type: {m_type}")
                    await _discard_voice_note_attachment(
                        voice_note_metadata,
                        "whatsapp_unsupported_media",
                    )
                    await twilio_client.send_message(
                        body=tr.t("channel_notices.media_unsupported", media_type=m_type),
                        from_=to_number,
                        to=from_number
                    )
                    return {"status": "success", "message": "Unsupported media type"}

            file_dict = files_list[0] if files_list else None

        user_message = transcribed_text if transcribed_text else message_body
        if not user_message and not file_dict:
            await _discard_voice_note_attachment(
                voice_note_metadata,
                "whatsapp_empty_message",
            )
            return {"status": "success", "message": "Empty message ignored"}

        if voice_note_metadata is not None:
            content_kind = audio_content_kind or "audio"
        elif files_list:
            content_kind = "mixed" if message_body else "image"
        else:
            content_kind = "text"

        message_provenance = {
            "channel": "whatsapp",
            "conversation_id": int(conversation_id),
            "user_id": int(current_user.id),
            "external_message_id": message_sid,
            "content_kind": content_kind,
            "response_mode": answer_mode,
        }
        if voice_note_metadata is not None:
            message_provenance["voice_note"] = voice_note_metadata

        if application_state is not None:
            message_provenance["application_channel"] = application_state.admission.as_dict()
            if application_state.operation is not None:
                message_provenance["application_operation_id"] = application_state.operation.operation_id
        # Log incoming WhatsApp message
        msg_type = "audio" if transcribed_text else ("image" if file_dict else "text")
        async with get_db_connection() as log_conn:
            await log_conn.execute(
                "INSERT INTO WHATSAPP_LOG (user_id, phone_number, direction, message_type, response_mode) VALUES (?, ?, 'in', ?, ?)",
                (current_user.id, from_number, msg_type, answer_mode)
            )
            await log_conn.commit()

        def parse_structured_whatsapp_content(payload):
            if isinstance(payload, dict):
                return payload if isinstance(payload.get("type"), str) else None

            if isinstance(payload, list) and payload:
                if all(isinstance(item, dict) and isinstance(item.get("type"), str) for item in payload):
                    return payload

            return None

        async def send_whatsapp_text_message(text: str):
            if not text or not text.strip():
                return

            if answer_mode == "voice":
                logger.debug("WhatsApp voice response mode")
                logger.debug(f"WhatsApp conversation id: {conversation_id}")
                await check_messaging(application_state, "tts", delivery=True)
                audio_path, error = await handle_tts_request(None, {"text": text, "author": "bot", "conversationId": conversation_id}, current_user, is_whatsapp=True, tts_context="external", billing_adapter=messaging_tts_billing(application_state))

                if error:
                    error_message = tr.t("channel_notices.voice_fallback")
                    await twilio_client.send_message(
                        body=f"{error_message}\n\n{text}",
                        from_=to_number,
                        to=from_number
                    )
                    logger.error(f"Error generating voice message: {error}")
                    return

                token = await get_or_generate_img_token(current_user)
                relative_path = audio_path[len(str(cache_directory)):]
                media_url = f"{request.url.scheme}://{request.url.hostname}/get-audio{relative_path}?token={token}"
                media_url = media_url.replace('\\', '/')
                logger.debug("Generated TTS media URL for WhatsApp")
                await twilio_client.send_message(
                    media_url=[media_url],
                    from_=to_number,
                    to=from_number
                )
                return

            logger.debug("WhatsApp text response mode")
            await twilio_client.send_message(
                body=text,
                from_=to_number,
                to=from_number
            )

        async def send_whatsapp_text(text: str, *, chunk_long_text: bool = False):
            if not text or not text.strip():
                return

            if chunk_long_text and len(text) >= 900:
                text_chunks = await insert_tts_break(text, min_length=700, max_length=900, look_ahead=100)
                for text_chunk in text_chunks:
                    await send_whatsapp_text_message(text_chunk)
                return

            await send_whatsapp_text_message(text)

        async def send_whatsapp_block(block) -> bool:
            if not isinstance(block, dict):
                return False

            block_type = block.get('type')
            if block_type == 'text':
                await send_whatsapp_text(block.get('text', ''), chunk_long_text=True)
                return True

            if block_type == 'image_url':
                logger.debug("Processing image URL for WhatsApp delivery")
                image_data = block.get('image_url', {})
                image_url = image_data.get('url')
                alt_text = image_data.get('alt') or tr.t('channel_notices.image')
                if not image_url:
                    return False
                logger.debug("Sending image via Twilio")
                await twilio_client.send_message(
                    body=tr.t("channel_notices.image_caption", description=alt_text),
                    media_url=[image_url],
                    from_=to_number,
                    to=from_number
                )
                return True

            if block_type == 'audio_url':
                logger.debug("Processing audio URL for WhatsApp delivery")
                audio_data = block.get('audio_url', {})
                audio_url = audio_data.get('url')
                if not audio_url:
                    return False
                logger.debug("Sending audio via Twilio")
                await twilio_client.send_message(
                    media_url=[audio_url],
                    from_=to_number,
                    to=from_number
                )
                return True

            return False

        async def send_chunks(chunks):
            for chunk in chunks:
                json_content = parse_structured_whatsapp_content(chunk)
                if isinstance(json_content, list):
                    handled_any = False
                    for block in json_content:
                        block_handled = await send_whatsapp_block(block)
                        handled_any = handled_any or block_handled
                    if handled_any:
                        continue
                elif json_content and await send_whatsapp_block(json_content):
                    continue

                await send_whatsapp_text(chunk if isinstance(chunk, str) else orjson.dumps(chunk).decode())

        # Prepare files list with all downloaded images
        files = files_list if files_list else None

        foreground_turn = await capture_non_phone_channel_turn(
            conversation_id=conversation_id,
            channel="whatsapp",
            connection_factory=get_db_connection,
        )
        channel_context = attach_message_channel_provenance(
            foreground_turn.context,
            message_provenance,
        )

        if application_state is not None:
            channel_context = application_state.attach(channel_context)

        # --- GranSabio check: if enabled, process in background ---
        async with get_db_connection(readonly=True) as conn_gs:
            gs_row = await conn_gs.execute(
                "SELECT COALESCE(ep.gransabio_enabled, 0) FROM CONVERSATIONS c "
                "LEFT JOIN USER_DETAILS ud ON ud.user_id = c.user_id "
                "LEFT JOIN PROMPTS ep ON ep.id = COALESCE(c.role_id, ud.current_prompt_id) "
                "WHERE c.id = ?", (conversation_id,)
            )
            gs_result = await gs_row.fetchone()
        is_gransabio = (
            bool((gs_result or [0])[0])
            and not foreground_turn.decision.phone_active
            and application_state is None
        )

        if is_gransabio:
            # Reject file attachments for GranSabio (text only)
            if files_list:
                if not await foreground_turn.is_current():
                    await _discard_voice_note_attachment(
                        voice_note_metadata,
                        "whatsapp_stale_before_gransabio",
                    )
                    return await _whatsapp_retry_response(message_sid)
                await _discard_voice_note_attachment(
                    voice_note_metadata,
                    "whatsapp_gransabio_file_rejected",
                )
                await twilio_client.send_message(
                    body=tr.t("channel_notices.gransabio_files"),
                    from_=to_number, to=from_number,
                )
                return JSONResponse(content={"status": "success"})

            from gransabio_config import GRANSABIO_USE_DRAMATIQ, get_gransabio_config
            from gransabio_service import (
                merge_gransabio_config, estimate_pipeline_timeout,
                load_prompt_gransabio_config, process_gransabio_external,
            )

            admin_config = await get_gransabio_config()
            if admin_config.get("gransabio_enabled") != "true":
                if not await foreground_turn.is_current():
                    await _discard_voice_note_attachment(
                        voice_note_metadata,
                        "whatsapp_stale_before_gransabio",
                    )
                    return await _whatsapp_retry_response(message_sid)
                await _discard_voice_note_attachment(
                    voice_note_metadata,
                    "whatsapp_gransabio_disabled",
                )
                await twilio_client.send_message(
                    body=tr.t("channel_notices.gransabio_disabled"),
                    from_=to_number, to=from_number,
                )
                return JSONResponse(content={"status": "success"})

            prompt_config = await load_prompt_gransabio_config(conversation_id)
            merged_config = merge_gransabio_config(prompt_config, admin_config)
            estimated_timeout = estimate_pipeline_timeout(merged_config)

            platform_context = {
                "from_number": from_number,
                "to_number": to_number,
                "answer_mode": answer_mode,
                "conversation_id": conversation_id,
                "message_provenance": message_provenance,
            }

            if GRANSABIO_USE_DRAMATIQ:
                from tasks import gransabio_external_task
                gransabio_external_task.send_with_options(
                    args=(
                        conversation_id,
                        orjson.dumps(user_message).decode(),
                        "whatsapp",
                        orjson.dumps(platform_context).decode(),
                        estimated_timeout,
                        foreground_turn.decision.commit_guard.epoch,
                    ),
                    time_limit=28_800_000,
                )
            else:
                import asyncio
                asyncio.create_task(
                    process_gransabio_external(
                        conversation_id=conversation_id,
                        user_id=current_user.id,
                        user_message=user_message,
                        platform="whatsapp",
                        platform_context=platform_context,
                        estimated_timeout=estimated_timeout,
                        foreground_epoch=foreground_turn.decision.commit_guard.epoch,
                    )
                )

            return JSONResponse(content={"status": "success"})

        # --- Normal (non-GranSabio) path continues below ---
        # Use process_save_message directly to avoid Form() object issues
        await check_messaging(application_state)
        response = await serialize_user_billing_response(
            current_user.id,
            process_save_message(
                request=request,
                conversation_id=conversation_id,
                current_user=current_user,
                text_plain=user_message,
                files=files,
                full_response=False,
                is_whatsapp=True,
                thinking_budget_tokens=None,
                channel_context=channel_context,
            ),
        )

        quota_notice = storage_quota_notice_from_response(response, translator=tr)

        if isinstance(response, StreamingResponse):
            (
                buffered_items,
                runtime_terminal,
                persistence_error,
                durable_message_ids,
            ) = (
                await _buffer_whatsapp_runtime_output(response.body_iterator)
            )

            if runtime_terminal == "queued_for_active_phone":
                return JSONResponse(content={"status": "success"})

            if (
                runtime_terminal == "stale_channel_turn"
                or persistence_error
                or not durable_message_ids
            ):
                await _discard_voice_note_attachment(
                    voice_note_metadata,
                    "whatsapp_message_not_persisted",
                )
                return await _whatsapp_retry_response(message_sid)

            if not persistence_error:
                for item in buffered_items:
                    if isinstance(item, str) and len(item) >= 900:
                        chunks = await insert_tts_break(
                            item,
                            min_length=700,
                            max_length=900,
                            look_ahead=100,
                        )
                        await send_chunks(chunks)
                    else:
                        await send_chunks([item])

            if quota_notice and not persistence_error:
                await twilio_client.send_message(
                    body=quota_notice,
                    from_=to_number,
                    to=from_number
                )
        else:
            await _discard_voice_note_attachment(
                voice_note_metadata,
                "whatsapp_message_not_persisted",
            )
            # Handle non-streaming responses (rate limit, insufficient balance, etc.)
            if quota_notice:
                # Media-only message rejected for lack of storage: tell the user.
                await twilio_client.send_message(
                    body=quota_notice,
                    from_=to_number,
                    to=from_number
                )
            else:
                status_code = response.status_code if hasattr(response, 'status_code') else 500
                error_messages = {
                    429: tr.t("channel_notices.message_limit"),
                    402: tr.t("channel_notices.insufficient_balance"),
                    403: tr.t("channel_notices.conversation_unavailable"),
                }
                user_msg = error_messages.get(status_code, tr.t("channel_notices.message_failed"))
                await twilio_client.send_message(
                    body=user_msg,
                    from_=to_number,
                    to=from_number
                )

        # Log outgoing WhatsApp response
        async with get_db_connection() as log_conn:
            await log_conn.execute(
                "INSERT INTO WHATSAPP_LOG (user_id, phone_number, direction, message_type, response_mode) VALUES (?, ?, 'out', 'text', ?)",
                (current_user.id, from_number, answer_mode)
            )
            await log_conn.commit()

    except EmbedError:
        await _discard_voice_note_attachment(voice_note_metadata, "application_channel_denied")
        return {"status": "denied"}
    except StaleChannelTurnError:
        await _discard_voice_note_attachment(
            voice_note_metadata,
            "whatsapp_stale_channel_turn",
        )
        logger.info(
            "WhatsApp response suppressed because phone foreground changed for conversation %s",
            conversation_id,
        )
        return await _whatsapp_retry_response(message_sid)
    except Exception as e:
        await _discard_voice_note_attachment(
            voice_note_metadata,
            "whatsapp_webhook_failed",
        )
        logger.error(f"!!! Error in whatsapp_webhook: {e}")
        try:
            error_message = tr.t("channel_notices.processing_failed_whatsapp")
            await twilio_client.send_message(
                body=error_message,
                from_=to_number,
                to=from_number
            )
        except Exception as send_err:
            logger.error(f"Failed to send WhatsApp error message: {send_err}")
        return JSONResponse(content={"status": "error"}, status_code=200)

    return {"status": "success"}
