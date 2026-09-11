import re
import os
import secrets
import time

import orjson
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from auth import get_user_from_phone_number, get_user_from_telegram_chat_id, get_user_by_id
from billing.usage_reservations import serialize_user_billing_response
from clients import async_telegram
from common import (
    TELEGRAM_RATE_LIMIT_GLOBAL,
    TELEGRAM_RATE_LIMIT_PER_USER,
    TELEGRAM_WEBHOOK_SECRET, TELEGRAM_BOT_TOKEN,
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
from integrations.delivery import chunk_telegram_response as _chunk_telegram_response
from integrations.media import transcribe_external_audio_detailed
from integrations.messaging_voice_notes.service import (
    attach_message_channel_provenance,
    get_voice_note_retention_enabled,
)
from integrations.telephony.channel_context import capture_non_phone_channel_turn
from log_config import logger
from prompt_access import get_user_accessible_prompts
from prompts import can_user_access_prompt
from storage_quota import StorageQuotaExceededError
from tools.tts import handle_tts_request

from integrations.applications.messaging import (
    resolve_messaging, application_command, application_messaging_turn,
    check_messaging, AdmittedMessagingClient, messaging_tts_billing,
)
from integrations.embed.models import EmbedError
from ai_runtime.channel_turns import StaleChannelTurnError
from ai_runtime.messages import process_save_message, storage_quota_notice_from_response


router = APIRouter()


# Telegram rate limiting (in-memory)
_telegram_rate_limits: dict[str, list[float]] = {}
_telegram_rate_limit_notices: dict[str, float] = {}
_telegram_global_timestamps: list[float] = []


def _telegram_external_message_id(
    *, update_id: int | None, chat_id: int | str, message_id: int | None, receiver_key="native"
) -> str | None:
    """Return an idempotency key that is unique across every Telegram chat."""
    if update_id is not None:
        key = f"update:{int(update_id)}"
        return key if receiver_key == "native" else f"{receiver_key}:{key}"
    if message_id is not None:
        key = f"chat:{chat_id}:message:{int(message_id)}"
        return key if receiver_key == "native" else f"{receiver_key}:{key}"
    return None


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
        logger.exception("Could not discard pending Telegram voice-note audio")


async def _prepare_telegram_voice(
    *,
    user_id: int,
    conversation_id: int,
    audio_content: bytes,
    mime_type: str,
    application_state=None,
) -> tuple[str, dict]:
    """Optionally retain and transcribe one Telegram voice message."""
    attachment_ref = None
    retention_status = "disabled"

    if await get_voice_note_retention_enabled("telegram"):
        try:
            await check_messaging(application_state, "stt")
            pending = await create_pending_audio_attachment(
                user_id=int(user_id),
                conversation_id=int(conversation_id),
                data=audio_content,
                filename="telegram-voice-note",
                mime_detected=mime_type or "audio/ogg",
                declared_mime=mime_type or None,
            )
        except StorageQuotaExceededError:
            retention_status = "quota_skipped"
            logger.info(
                "Telegram voice-note retention skipped because user %s is over quota",
                user_id,
            )
        except Exception:
            retention_status = "failed"
            logger.exception(
                "Could not retain Telegram voice-note audio for conversation %s",
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
        )
    except Exception:
        if attachment_ref:
            await _discard_voice_note_attachment(
                {"audio_attachment_ref": attachment_ref},
                "telegram_transcription_failed",
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


async def _release_telegram_retry_marker(update_id: int | None, *, receiver_key="native") -> None:
    """Let Telegram retry an update whose inbound turn was not persisted."""
    if update_id is None:
        return
    try:
        async with get_db_connection() as conn:
            await conn.execute(
                "DELETE FROM TELEGRAM_PROCESSED_UPDATES WHERE receiver_key = ? AND update_id = ?",
                (receiver_key, update_id),
            )
            await conn.commit()
    except Exception:
        logger.exception("Could not release Telegram retry marker %s", update_id)


async def _telegram_retry_response(update_id: int | None, *, receiver_key="native") -> JSONResponse:
    await _release_telegram_retry_marker(update_id, receiver_key=receiver_key)
    return JSONResponse(
        content={"ok": False, "error": "inbound_not_persisted"},
        status_code=503,
    )


async def _buffer_telegram_runtime_output(body_iterator):
    """Consume a runtime stream without making any Telegram delivery."""
    accumulated_text = ""
    terminal = None
    persistence_error = False
    durable_message_ids = False

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
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("persistence_error") is True:
                    persistence_error = True
                if data.get("terminal"):
                    terminal = str(data["terminal"])
                    continue
                message_ids = data.get("message_ids")
                if isinstance(message_ids, dict):
                    durable_message_ids = bool(
                        message_ids.get("user") and message_ids.get("bot")
                    )
                content = data.get("content", "")
                if isinstance(content, str):
                    accumulated_text += content
    except Exception:
        logger.exception("Telegram runtime stream failed before durable completion")
        persistence_error = True

    if persistence_error:
        durable_message_ids = False
    return accumulated_text, terminal, persistence_error, durable_message_ids


# ============================================================================
# Telegram Webhook
# ============================================================================

async def _log_telegram(
    direction: str, user_id: int, chat_id: int, message_type: str, response_mode: str
):
    """Insert a row into TELEGRAM_LOG and clean up old entries."""
    try:
        async with get_db_connection() as conn:
            await conn.execute(
                '''INSERT INTO TELEGRAM_LOG (user_id, chat_id, direction, message_type, response_mode)
                   VALUES (?, ?, ?, ?, ?)''',
                (user_id, chat_id, direction, message_type, response_mode),
            )
            # Retention cleanup: delete entries older than 90 days
            await conn.execute(
                "DELETE FROM TELEGRAM_LOG WHERE timestamp < datetime('now', '-90 days')"
            )
            await conn.commit()
    except Exception as e:
        logger.error(f"Failed to log Telegram message: {e}")


@router.post("/telegram")
async def telegram_webhook(request: Request):
    if async_telegram is None:
        return JSONResponse(content={"ok": True})
    bot_id = str(TELEGRAM_BOT_TOKEN or "").split(":", 1)[0]
    return await _authenticated_telegram_webhook(request, async_telegram,
        TELEGRAM_WEBHOOK_SECRET, bot_id)


@router.post("/telegram/{receiver_id}")
async def telegram_receiver_webhook(receiver_id: str, request: Request):
    from integrations.applications.channels import get_application_channel_service
    from telegram_async import AsyncTelegramClient
    receiver = await get_application_channel_service().get_receiver(receiver_id)
    if receiver is None or receiver.channel != "telegram" or not receiver.enabled:
        raise HTTPException(status_code=404, detail="Receiver unavailable")
    token = os.environ.get(receiver.telegram_token_env or "", "")
    secret = os.environ.get(receiver.telegram_webhook_secret_env or "", "")
    if not token or not secret or token.split(":", 1)[0] != receiver.receiver_key:
        raise HTTPException(status_code=503, detail="Receiver authentication unavailable")
    client = AsyncTelegramClient(token)
    try:
        return await _authenticated_telegram_webhook(request, client, secret, receiver.receiver_key)
    finally:
        await client.close()


async def _authenticated_telegram_webhook(request, client, webhook_secret, bot_id):
    received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not webhook_secret or not secrets.compare_digest(received, webhook_secret):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    try:
        from integrations.embed.api import read_bounded_body
        update = orjson.loads(await read_bounded_body(request, max_bytes=32768))
    except Exception:
        return JSONResponse(content={"ok": True})
    message = update.get("message")
    if not isinstance(message, dict):
        return JSONResponse(content={"ok": True})
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    # Private numeric identity is the authenticated Bot API sender, never username/contact.
    if chat.get("type") != "private" or type(sender.get("id")) is not int or sender["id"] != chat.get("id"):
        return JSONResponse(content={"ok": True})
    from integrations.applications.channel_models import VerifiedChannelEnvelope
    try:
        event_id = _telegram_external_message_id(update_id=update.get("update_id"),
            chat_id=chat["id"], message_id=message.get("message_id"))
        envelope = VerifiedChannelEnvelope(channel="telegram", provider="telegram",
            receiver_key=str(bot_id), provider_identity=str(sender["id"]),
            session_key="messaging", event_id=event_id or "")
        from integrations.applications.channels import get_application_channel_service
        service = get_application_channel_service()
        async with service.store.transaction() as connection:
            receiver = await service._receiver(connection, envelope)
            if receiver:
                # Control replies share the ordinary provider deduplication gate.
                if type(update.get('update_id')) is not int:
                    return JSONResponse(content={'ok': True})
                await connection.execute("DELETE FROM TELEGRAM_PROCESSED_UPDATES WHERE created_at < datetime('now', '-1 day')")
                cursor = await connection.execute('INSERT OR IGNORE INTO TELEGRAM_PROCESSED_UPDATES (receiver_key,update_id) VALUES (?,?)',
                    (f'telegram:{bot_id}', update['update_id']))
                if cursor.rowcount == 0:
                    return JSONResponse(content={'ok': True})
        state, reply = await resolve_messaging(envelope, str(message.get("text") or ""), contact=message.get('contact'))
        if reply is not None:
            if receiver:
                async with service.store.transaction() as connection:
                    if not await service._proof_attempt(connection, receiver, envelope):
                        return JSONResponse(content={'ok': True})
            options = {}
            if receiver and state is None:
                tr = chat_translator()
                options['reply_markup'] = {'keyboard': [[{'text': tr.t('channel_notices.share_phone_button'), 'request_contact': True}]],
                                           'resize_keyboard': True, 'one_time_keyboard': True}
            await client.send_message(chat["id"], reply, **options)
            return JSONResponse(content={"ok": True})
        receiver_key = f"telegram:{bot_id}" if state is not None else "native"
        async with application_messaging_turn(state):
            return await _process_telegram_message(request, update, message,
                application_state=state, telegram_client=AdmittedMessagingClient(client, state),
                receiver_key=receiver_key, deduplicated=bool(receiver))
    except (EmbedError, ValueError):
        return JSONResponse(content={"ok": True})


async def _process_telegram_message(request, update, message, *, application_state=None,
                                    telegram_client, receiver_key="native", deduplicated=False):
    update_id = update.get("update_id")
    chat_id = message["chat"]["id"]
    text = str(message.get("text") or "").strip()
    # --- Idempotency: deduplicate by update_id ---
    if update_id is not None and not deduplicated:
        async with get_db_connection() as conn:
            await conn.execute(
                "DELETE FROM TELEGRAM_PROCESSED_UPDATES WHERE created_at < datetime('now', '-1 day')"
            )
            cursor = await conn.execute(
                "INSERT OR IGNORE INTO TELEGRAM_PROCESSED_UPDATES (receiver_key,update_id) VALUES (?,?)",
                (receiver_key, update_id),
            )
            await conn.commit()
            if cursor.rowcount == 0:
                return JSONResponse(content={"ok": True})

    # --- Rate limiting ---
    now = time.time()

    # Global
    _telegram_global_timestamps[:] = [
        t for t in _telegram_global_timestamps if now - t < 60
    ]
    if len(_telegram_global_timestamps) >= TELEGRAM_RATE_LIMIT_GLOBAL:
        logger.warning("Telegram global rate limit exceeded")
        return JSONResponse(content={"ok": True})
    _telegram_global_timestamps.append(now)

    tr = chat_translator()

    # Per-user
    user_key = f"{receiver_key}:{chat_id}"
    user_timestamps = _telegram_rate_limits.get(user_key, [])
    user_timestamps = [t for t in user_timestamps if now - t < 60]
    _telegram_rate_limits[user_key] = user_timestamps

    if len(user_timestamps) >= TELEGRAM_RATE_LIMIT_PER_USER:
        last_notice = _telegram_rate_limit_notices.get(user_key, 0)
        if now - last_notice > 300:
            _telegram_rate_limit_notices[user_key] = now
            try:
                current_user = (await get_user_by_id(application_state.admission.scope.user_id)
                        if application_state is not None else await get_user_from_telegram_chat_id(chat_id))
                tr = chat_translator(current_user)
                await telegram_client.send_message(
                    chat_id,
                    tr.t("channel_notices.rate_limit"),
                )
            except Exception:
                pass
        return JSONResponse(content={"ok": True})

    user_timestamps.append(now)
    _telegram_rate_limits[user_key] = user_timestamps

    # --- User lookup ---
    voice_note_metadata = None
    try:
        current_user = (await get_user_by_id(application_state.admission.scope.user_id)
                        if application_state is not None else await get_user_from_telegram_chat_id(chat_id))
        tr = chat_translator(current_user)

        # If not linked, check if this is a contact-sharing message
        if current_user is None:
            contact = message.get("contact")
            if contact:
                # Validate that the contact belongs to the sender
                sender_id = message.get("from", {}).get("id")
                contact_user_id = contact.get("user_id")
                if not contact_user_id or contact_user_id != sender_id:
                    await telegram_client.send_message(
                        chat_id,
                        tr.t("channel_notices.share_own_phone"),
                        reply_markup={
                            "keyboard": [
                                [{"text": tr.t("channel_notices.share_phone_button"), "request_contact": True}]
                            ],
                            "one_time_keyboard": True,
                            "resize_keyboard": True,
                        },
                    )
                    return JSONResponse(content={"ok": True})

                # Normalize phone number
                import re
                phone = re.sub(r'[\s\-\(\)]', '', contact.get("phone_number", ""))
                if not phone.startswith("+"):
                    phone = f"+{phone}"

                linked_user = await get_user_from_phone_number(phone)
                if linked_user:
                    tr = chat_translator(linked_user)
                    # Check if account is enabled before linking
                    if not linked_user.is_enabled:
                        await telegram_client.send_message(
                            chat_id,
                            tr.t("channel_notices.account_disabled"),
                        )
                        return JSONResponse(content={"ok": True})

                    # Handle IntegrityError from unique constraint
                    try:
                        async with get_db_connection() as conn:
                            await conn.execute(
                                "UPDATE USERS SET telegram_chat_id = ? WHERE id = ?",
                                (chat_id, linked_user.id),
                            )
                            await conn.commit()
                    except Exception as link_err:
                        if "UNIQUE" in str(link_err).upper():
                            await telegram_client.send_message(
                                chat_id,
                                tr.t("channel_notices.already_linked"),
                            )
                        else:
                            await telegram_client.send_message(
                                chat_id,
                                tr.t("channel_notices.link_failed"),
                            )
                            logger.error(f"Telegram link error: {link_err}")
                        return JSONResponse(content={"ok": True})

                    # Send welcome message
                    try:
                        async with get_db_connection(readonly=True) as conn:
                            cursor = await conn.execute(
                                "SELECT value FROM SYSTEM_CONFIG WHERE key = 'telegram_welcome_message'"
                            )
                            row = await cursor.fetchone()
                        welcome = (row[0] if row and row[0] else "").replace(
                            "{username}", linked_user.username
                        )
                        if not welcome:
                            welcome = tr.t("channel_notices.account_linked", username=linked_user.username)
                        await telegram_client.send_message(chat_id, welcome)
                    except Exception as e:
                        logger.error(f"Failed to send Telegram welcome: {e}")

                    await _log_telegram('in', linked_user.id, chat_id, 'contact', 'text')
                    return JSONResponse(content={"ok": True})
                else:
                    # Phone not found in our system
                    msg = tr.t("channel_notices.phone_unknown")
                    try:
                        async with get_db_connection(readonly=True) as conn:
                            cursor = await conn.execute(
                                "SELECT value FROM SYSTEM_CONFIG WHERE key = 'telegram_unknown_user_message'"
                            )
                            row = await cursor.fetchone()
                        if row and row[0]:
                            msg = row[0]
                    except Exception as e:
                        logger.error(f"Failed to load telegram_unknown_user_message from SYSTEM_CONFIG: {e}")
                    try:
                        await telegram_client.send_message(chat_id, msg)
                    except Exception as e:
                        logger.error(f"Failed to send 'phone not found' message to Telegram chat {chat_id}: {e}")
                    return JSONResponse(content={"ok": True})
            else:
                # Not linked and didn't share contact -- ask them to share
                await telegram_client.send_message(
                    chat_id,
                    tr.t("channel_notices.share_phone_welcome"),
                    reply_markup={
                        "keyboard": [
                            [{"text": tr.t("channel_notices.share_phone_button"), "request_contact": True}]
                        ],
                        "one_time_keyboard": True,
                        "resize_keyboard": True,
                    },
                )
                return JSONResponse(content={"ok": True})

        if not current_user.is_enabled:
            return JSONResponse(content={"ok": True})

        if application_state is not None:
            command_reply = await application_command(application_state, text, update_id, translator=tr)
            if command_reply is not None:
                await telegram_client.send_message(chat_id, command_reply)
                return JSONResponse(content={"ok": True})
            conversation_id = application_state.admission.scope.conversation_id
            answer_mode = application_state.admission.response_mode
        else:
            text_lower = text.lower()

            if text_lower == "!help":
                help_text = (
                    tr.t("channel_notices.help_telegram")
                )
                await telegram_client.send_message(chat_id, help_text, parse_mode="Markdown")
                return JSONResponse(content={"ok": True})

            if text_lower == "!unlink":
                async with get_db_connection() as conn:
                    await conn.execute(
                        "UPDATE USERS SET telegram_chat_id = NULL WHERE id = ?",
                        (current_user.id,),
                    )
                    await conn.execute(
                        "UPDATE USER_DETAILS SET external_platforms = json_remove(COALESCE(NULLIF(external_platforms, ''), '{}'), '$.telegram') WHERE user_id = ?",
                        (current_user.id,),
                    )
                    await conn.commit()
                await telegram_client.send_message(
                    chat_id,
                    tr.t("channel_notices.unlinked"),
                )
                return JSONResponse(content={"ok": True})

            async with get_db_connection(readonly=True) as conn:
                cursor = await conn.cursor()
                await cursor.execute(
                    "SELECT external_platforms FROM USER_DETAILS WHERE user_id = ?",
                    (current_user.id,),
                )
                result = await cursor.fetchone()
                platforms = orjson.loads(result[0]) if result and result[0] else {}
                telegram_data = platforms.get("telegram") or {}
                if not isinstance(telegram_data, dict):
                    telegram_data = {}
                is_first_telegram = not telegram_data

            async with get_db_connection(readonly=True) as conn:
                cursor = await conn.cursor()
                ok, _, err_msg = await can_use_platform(current_user.id, "telegram", cursor, translator=tr)
                if not ok:
                    await telegram_client.send_message(chat_id, err_msg)
                    return JSONResponse(content={"ok": True})

            if text_lower == "!chats":
                async with get_db_connection(readonly=True) as conn:
                    chats_message = await get_chats_list(
                        current_user.id,
                        "telegram",
                        conn,
                        markdown=False,
                        translator=tr,
                    )
                await telegram_client.send_message(chat_id, chats_message)
                return JSONResponse(content={"ok": True})

            if text_lower == "!set" or text_lower.startswith("!set "):
                parts = text[4:].strip().split() if len(text) > 4 else []
                if not parts or len(parts) > 2:
                    await telegram_client.send_message(
                        chat_id,
                        tr.t("channel_notices.set_usage"),
                    )
                    return JSONResponse(content={"ok": True})

                raw_id = parts[0]
                clean_id = raw_id[1:] if raw_id.startswith("#") else raw_id
                if not clean_id.isdigit() or int(clean_id) <= 0:
                    await telegram_client.send_message(
                        chat_id,
                        tr.t("channel_notices.set_usage"),
                    )
                    return JSONResponse(content={"ok": True})

                target_platform = "telegram"
                if len(parts) == 2:
                    platform_map = {
                        "whatsapp": "whatsapp",
                        "wa": "whatsapp",
                        "telegram": "telegram",
                        "tg": "telegram",
                    }
                    target_platform = platform_map.get(parts[1].lower())
                    if target_platform is None:
                        await telegram_client.send_message(
                            chat_id,
                            tr.t("channel_notices.invalid_platform"),
                        )
                        return JSONResponse(content={"ok": True})

                result = await set_external_conversation(
                    current_user.id,
                    int(clean_id),
                    target_platform,
                    "telegram",
                    translator=tr,
                )
                await telegram_client.send_message(chat_id, result["message"])
                return JSONResponse(content={"ok": True})

            if text_lower in ("!text", "text_mode"):
                async with get_db_connection() as conn:
                    confirmation = await change_response_mode(
                        current_user.id,
                        "text",
                        platform="telegram",
                        conn=conn,
                        translator=tr,
                    )
                await telegram_client.send_message(chat_id, confirmation)
                return JSONResponse(content={"ok": True})

            if text_lower in ("!voice", "voice_mode"):
                async with get_db_connection() as conn:
                    confirmation = await change_response_mode(
                        current_user.id,
                        "voice",
                        platform="telegram",
                        conn=conn,
                        translator=tr,
                    )
                await telegram_client.send_message(chat_id, confirmation)
                return JSONResponse(content={"ok": True})

            if text_lower == "!prompt list":
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
                    await telegram_client.send_message(chat_id, tr.t("channel_notices.no_prompts"))
                    return JSONResponse(content={"ok": True})

                prompt_lines = [f"*{p['id']}* - {escape_markdown(p['name'])}" for p in prompts_list[:20]]
                msg = tr.t("channel_notices.prompts_header") + "\n".join(prompt_lines)
                if len(prompts_list) > 20:
                    msg += tr.t("channel_notices.prompts_more", count=len(prompts_list) - 20)
                msg += tr.t("channel_notices.prompts_footer")

                await telegram_client.send_message(chat_id, msg, parse_mode="Markdown")
                return JSONResponse(content={"ok": True})

            if text_lower == "!new":
                await create_new_platform_conversation(current_user.id, "telegram", current_user)
                await telegram_client.send_message(
                    chat_id,
                    tr.t("channel_notices.new_conversation"),
                )
                return JSONResponse(content={"ok": True})

            telegram_data, created_binding = await ensure_platform_conversation(
                current_user.id,
                "telegram",
                current_user,
            )
            if created_binding and is_first_telegram:
                try:
                    async with get_db_connection(readonly=True) as conn:
                        cursor = await conn.execute(
                            "SELECT value FROM SYSTEM_CONFIG WHERE key = 'telegram_welcome_message'"
                        )
                        row = await cursor.fetchone()
                    welcome = (row[0] if row and row[0] else "").replace(
                        "{username}",
                        current_user.username,
                    )
                    if welcome:
                        await telegram_client.send_message(chat_id, welcome)
                except Exception as welcome_err:
                    logger.error(f"Failed to send Telegram welcome: {welcome_err}")

            conversation_id = telegram_data["conversation_id"]
            answer_mode = telegram_data.get("answer", "text")

            async with get_db_connection(readonly=True) as conn:
                cursor = await conn.cursor()
                await cursor.execute(
                    "SELECT locked FROM CONVERSATIONS WHERE id = ? AND user_id = ?",
                    (conversation_id, current_user.id),
                )
                lock_row = await cursor.fetchone()
                if not lock_row:
                    await telegram_client.send_message(
                        chat_id,
                        tr.t("channel_notices.conversation_missing_new"),
                    )
                    return JSONResponse(content={"ok": True})
                if lock_row[0]:
                    await _log_telegram('in', current_user.id, chat_id, 'text', answer_mode)
                    logger.info(
                        f"Telegram message blocked: conversation {conversation_id} locked for user {current_user.id}"
                    )
                    await telegram_client.send_message(
                        chat_id,
                        tr.t("channel_notices.conversation_locked_new"),
                    )
                    return JSONResponse(content={"ok": True})

            async with get_db_connection(readonly=True) as conn:
                cursor = await conn.cursor()
                await cursor.execute(
                    "SELECT external_platforms FROM USER_DETAILS WHERE user_id = ?",
                    (current_user.id,),
                )
                row = await cursor.fetchone()
                if row and row[0]:
                    fresh_platforms = orjson.loads(row[0])
                    fresh_conv_id = (fresh_platforms.get("telegram", {}) or {}).get("conversation_id")
                    if fresh_conv_id and fresh_conv_id != conversation_id:
                        conversation_id = fresh_conv_id
                        await cursor.execute(
                            "SELECT locked FROM CONVERSATIONS WHERE id = ? AND user_id = ?",
                            (conversation_id, current_user.id),
                        )
                        lock_row = await cursor.fetchone()
                        if not lock_row:
                            await telegram_client.send_message(
                                chat_id,
                                tr.t("channel_notices.conversation_missing_new"),
                            )
                            return JSONResponse(content={"ok": True})
                        if lock_row[0]:
                            await _log_telegram('in', current_user.id, chat_id, 'text', answer_mode)
                            logger.info(
                                f"Telegram message blocked: conversation {conversation_id} locked for user {current_user.id}"
                            )
                            await telegram_client.send_message(
                                chat_id,
                                tr.t("channel_notices.conversation_locked_new"),
                            )
                            return JSONResponse(content={"ok": True})

            if text_lower.startswith("!prompt ") and text_lower != "!prompt list":
                prompt_query = text[8:].strip()

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
                        await telegram_client.send_message(
                            chat_id,
                            tr.t("channel_notices.prompt_missing", name=escape_markdown(prompt_query)),
                            parse_mode="Markdown",
                        )
                        return JSONResponse(content={"ok": True})

                    if not await can_user_access_prompt(current_user, target_prompt[0], cursor):
                        await telegram_client.send_message(chat_id, tr.t("channel_notices.prompt_denied"))
                        return JSONResponse(content={"ok": True})

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
                        await telegram_client.send_message(
                            chat_id,
                            tr.t("channel_notices.conversation_locked_new"),
                        )
                        return JSONResponse(content={"ok": True})

                    await conn.commit()

                    await telegram_client.send_message(
                        chat_id,
                        tr.t("channel_notices.prompt_changed", name=escape_markdown(target_prompt[1])),
                        parse_mode="Markdown",
                    )
                return JSONResponse(content={"ok": True})

        if answer_mode == "voice":
            await check_messaging(application_state, "tts")
        if message.get("voice"):
            await check_messaging(application_state, "stt")
        if message.get("photo"):
            await check_messaging(application_state, "attachments")
        # --- Media processing ---
        transcribed_text = ""
        files_list = []

        # Voice message
        voice = message.get("voice")
        if voice:
            try:
                await check_messaging(application_state, "stt")
                file_info = await telegram_client.get_file(voice["file_id"])
                await check_messaging(application_state, "stt")
                media_bytes = await telegram_client.download_file(file_info["file_path"])
                transcribed_text, voice_note_metadata = await _prepare_telegram_voice(
                    user_id=current_user.id,
                    conversation_id=conversation_id,
                    audio_content=media_bytes,
                    mime_type=str(voice.get("mime_type") or "audio/ogg"),
                    application_state=application_state,
                )
            except Exception as e:
                logger.error(f"Error transcribing Telegram voice: {e}")
                await telegram_client.send_message(
                    chat_id,
                    tr.t("channel_notices.audio_failed"),
                )
                return JSONResponse(content={"ok": True})

        # Photo (get largest size)
        photos = message.get("photo")
        if photos:
            try:
                await check_messaging(application_state, "attachments")
                largest = photos[-1]  # Telegram sends sizes in ascending order
                file_info = await telegram_client.get_file(largest["file_id"])
                await check_messaging(application_state, "attachments")
                media_bytes = await telegram_client.download_file(file_info["file_path"])
                files_list.append({
                    'data': media_bytes,
                    'content_type': "image/jpeg",
                    'filename': "telegram_photo.jpg"
                })
            except Exception as e:
                logger.error(f"Error downloading Telegram photo: {e}")

        user_message = transcribed_text if transcribed_text else text
        if not user_message and not files_list:
            await _discard_voice_note_attachment(
                voice_note_metadata,
                "telegram_empty_message",
            )
            return JSONResponse(content={"ok": True})

        if voice_note_metadata is not None:
            content_kind = "voice_note"
        elif files_list:
            content_kind = "mixed" if text else "image"
        else:
            content_kind = "text"

        external_message_id = _telegram_external_message_id(
            update_id=update_id,
            chat_id=chat_id,
            message_id=message.get("message_id"),
            receiver_key=receiver_key,
        )
        message_provenance = {
            "channel": "telegram",
            "conversation_id": int(conversation_id),
            "user_id": int(current_user.id),
            "external_message_id": external_message_id,
            "content_kind": content_kind,
            "response_mode": answer_mode,
        }
        if voice_note_metadata is not None:
            message_provenance["voice_note"] = voice_note_metadata

        if application_state is not None:
            message_provenance["application_channel"] = application_state.admission.as_dict()
            if application_state.operation is not None:
                message_provenance["application_operation_id"] = application_state.operation.operation_id
        # Log incoming message
        msg_type = "audio" if transcribed_text else ("image" if files_list else "text")
        await _log_telegram('in', current_user.id, chat_id, msg_type, answer_mode)

        foreground_turn = await capture_non_phone_channel_turn(
            conversation_id=conversation_id,
            channel="telegram",
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
                        "telegram_stale_before_gransabio",
                    )
                    return await _telegram_retry_response(update_id, receiver_key=receiver_key)
                await _discard_voice_note_attachment(
                    voice_note_metadata,
                    "telegram_gransabio_file_rejected",
                )
                await telegram_client.send_message(
                    chat_id, tr.t("channel_notices.gransabio_files")
                )
                return JSONResponse(content={"ok": True})

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
                        "telegram_stale_before_gransabio",
                    )
                    return await _telegram_retry_response(update_id, receiver_key=receiver_key)
                await _discard_voice_note_attachment(
                    voice_note_metadata,
                    "telegram_gransabio_disabled",
                )
                await telegram_client.send_message(chat_id, tr.t("channel_notices.gransabio_disabled"))
                return JSONResponse(content={"ok": True})

            prompt_config = await load_prompt_gransabio_config(conversation_id)
            merged_config = merge_gransabio_config(prompt_config, admin_config)
            estimated_timeout = estimate_pipeline_timeout(merged_config)

            platform_context = {
                "chat_id": chat_id,
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
                        "telegram",
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
                        platform="telegram",
                        platform_context=platform_context,
                        estimated_timeout=estimated_timeout,
                        foreground_epoch=foreground_turn.decision.commit_guard.epoch,
                    )
                )

            # Acknowledge webhook immediately (incoming already logged above)
            return JSONResponse(content={"ok": True})

        # --- Normal (non-GranSabio) path continues below ---
        files = files_list if files_list else None
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
                is_whatsapp=True,  # Same non-streaming behavior as WhatsApp
                thinking_budget_tokens=None,
                channel_context=channel_context,
            ),
        )

        quota_notice = storage_quota_notice_from_response(response, translator=tr)

        if isinstance(response, StreamingResponse):
            (
                accumulated_text,
                runtime_terminal,
                persistence_error,
                durable_message_ids,
            ) = await _buffer_telegram_runtime_output(response.body_iterator)

            if runtime_terminal == "queued_for_active_phone":
                return JSONResponse(content={"ok": True})

            if (
                runtime_terminal == "stale_channel_turn"
                or persistence_error
                or not durable_message_ids
            ):
                await _discard_voice_note_attachment(
                    voice_note_metadata,
                    "telegram_message_not_persisted",
                )
                return await _telegram_retry_response(update_id, receiver_key=receiver_key)

            # Send the accumulated response
            if accumulated_text.strip() and not persistence_error:
                if answer_mode == "voice":
                    try:
                        await check_messaging(application_state, "tts", delivery=True)
                        audio_path, error = await handle_tts_request(
                            None,
                            {"text": accumulated_text, "author": "bot", "conversationId": conversation_id},
                            current_user,
                            is_whatsapp=True,
                            tts_context="external",
                            billing_adapter=messaging_tts_billing(application_state),
                        )
                        if error:
                            # Fallback to text
                            for chunk in _chunk_telegram_response(accumulated_text):
                                await telegram_client.send_message(chat_id, chunk)
                        else:
                            # Read the audio file and send as voice
                            from pathlib import Path
                            audio_file_path = Path(audio_path)
                            if audio_file_path.exists():
                                voice_bytes = audio_file_path.read_bytes()
                                await telegram_client.send_voice(chat_id, voice_bytes)
                            else:
                                for chunk in _chunk_telegram_response(accumulated_text):
                                    await telegram_client.send_message(chat_id, chunk)
                    except Exception as tts_err:
                        logger.error(f"Telegram TTS error: {tts_err}")
                        for chunk in _chunk_telegram_response(accumulated_text):
                            await telegram_client.send_message(chat_id, chunk)
                else:
                    for chunk in _chunk_telegram_response(accumulated_text):
                        await telegram_client.send_message(chat_id, chunk)

            if quota_notice and not persistence_error:
                await telegram_client.send_message(chat_id, quota_notice)
        else:
            await _discard_voice_note_attachment(
                voice_note_metadata,
                "telegram_message_not_persisted",
            )
            # Handle non-streaming responses (rate limit, insufficient balance, etc.)
            if quota_notice:
                # Media-only message rejected for lack of storage: tell the user.
                await telegram_client.send_message(chat_id, quota_notice)
            else:
                status_code = response.status_code if hasattr(response, 'status_code') else 500
                error_messages = {
                    429: tr.t("channel_notices.message_limit"),
                    402: tr.t("channel_notices.insufficient_balance"),
                    403: tr.t("channel_notices.conversation_unavailable"),
                }
                user_msg = error_messages.get(status_code, tr.t("channel_notices.message_failed"))
                await telegram_client.send_message(chat_id, user_msg)

        # Log outgoing response
        await _log_telegram('out', current_user.id, chat_id, 'text', answer_mode)

        return JSONResponse(content={"ok": True})

    except EmbedError:
        await _discard_voice_note_attachment(voice_note_metadata, "application_channel_denied")
        return JSONResponse(content={"ok": True})
    except StaleChannelTurnError:
        await _discard_voice_note_attachment(
            voice_note_metadata,
            "telegram_stale_channel_turn",
        )
        logger.info(
            "Telegram response suppressed because phone foreground changed for conversation %s",
            conversation_id,
        )
        return await _telegram_retry_response(update_id, receiver_key=receiver_key)
    except Exception as e:
        await _discard_voice_note_attachment(
            voice_note_metadata,
            "telegram_webhook_failed",
        )
        logger.error(f"Telegram webhook error: {e}", exc_info=True)
        try:
            await telegram_client.send_message(
                chat_id,
                tr.t("channel_notices.processing_failed"),
            )
        except Exception:
            pass
        return JSONResponse(content={"ok": True})
