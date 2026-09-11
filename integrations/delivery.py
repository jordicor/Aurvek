from pathlib import Path

from clients import async_telegram, async_twilio
from chat.services.localization import chat_error, chat_translator
from common import PRIMARY_APP_DOMAIN
from log_config import logger
from tools.tts import handle_tts_request


def chunk_telegram_response(text: str, max_len: int = 3800) -> list[str]:
    """Split Telegram responses at natural boundaries under message limits."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = max_len
        for sep in ["\n\n", "\n", ". ", "! ", "? "]:
            idx = text.rfind(sep, 0, max_len)
            if idx > max_len // 2:
                split_at = idx + len(sep)
                break
        chunks.append(text[:split_at])
        text = text[split_at:]
    return chunks


async def deliver_to_platform(platform: str, ctx: dict, text: str):
    """Send accumulated text to an external channel."""
    answer_mode = ctx.get("answer_mode", "text")
    conversation_id = ctx.get("conversation_id", 0)

    if platform == "telegram":
        chat_id = ctx.get("chat_id")
        if not chat_id:
            logger.error("deliver_to_platform: missing chat_id for telegram")
            return
        if async_telegram is None:
            logger.error("deliver_to_platform: Telegram client is not configured")
            return

        if answer_mode == "voice":
            try:
                audio_path, error = await handle_tts_request(
                    None,
                    {"text": text, "author": "bot", "conversationId": conversation_id},
                    None,
                    is_whatsapp=True,
                    tts_context="external",
                )
                if not error and audio_path:
                    audio_file = Path(audio_path)
                    if audio_file.exists():
                        await async_telegram.send_voice(chat_id, audio_file.read_bytes())
                        return
            except Exception as tts_err:
                logger.warning("Telegram TTS failed, falling back to text: %s", tts_err)

        for chunk in chunk_telegram_response(text):
            await async_telegram.send_message(chat_id, chunk)
        return

    if platform == "whatsapp":
        from_number = ctx.get("from_number")
        to_number = ctx.get("to_number")
        if async_twilio is None:
            logger.error("deliver_to_platform: Twilio client is not configured")
            return

        if answer_mode == "voice":
            try:
                audio_path, error = await handle_tts_request(
                    None,
                    {"text": text, "author": "bot", "conversationId": conversation_id},
                    None,
                    is_whatsapp=True,
                    tts_context="external",
                )
                if not error and audio_path:
                    audio_url = f"https://{PRIMARY_APP_DOMAIN}/{audio_path}"
                    await async_twilio.send_message(
                        body="",
                        media_url=[audio_url],
                        from_=to_number,
                        to=from_number,
                    )
                    return
            except Exception as tts_err:
                logger.warning("WhatsApp TTS failed, falling back to text: %s", tts_err)

        await async_twilio.send_message(body=text, from_=to_number, to=from_number)
        return

    logger.error("deliver_to_platform: unknown platform %s", platform)


async def send_platform_error(platform: str, ctx: dict, error_code: str, *, translator=None):
    """Deliver localized service errors; provider diagnostics stay in server logs."""
    try:
        tr = chat_translator(translator)
        if isinstance(error_code, str) and error_code in {"gransabio_disabled", "gransabio_own_keys", "generation_stopped"}:
            error_msg = tr.t("channel_notices." + error_code)
        else:
            error_msg = chat_error(tr, error_code)
        await deliver_to_platform(platform, ctx, error_msg)
    except Exception as exc:
        logger.warning(
            "send_platform_error: failed to deliver error to %s: %s",
            platform,
            exc,
        )
