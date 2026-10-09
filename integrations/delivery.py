from pathlib import Path
from urllib.parse import quote, urlencode

from clients import async_telegram, async_twilio
from chat.services.localization import chat_error, chat_translator
from common import cache_directory, get_auth_base_url, get_runtime_request_url
from log_config import logger
from save_images import get_or_generate_img_token
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


async def chunk_whatsapp_response(text: str) -> list[str]:
    """Split text and voice fallbacks without applying TTS markup cleanup."""
    return chunk_telegram_response(text, max_len=900)


async def whatsapp_audio_url(audio_path, user, *, request=None) -> str:
    """Build the authenticated public URL for an audio file in the TTS cache."""
    relative_path = (
        Path(str(audio_path).replace("\\", "/")).resolve()
        .relative_to(Path(cache_directory).resolve())
        .as_posix()
    )
    base_url = (
        get_auth_base_url(request)
        if request is not None
        else get_runtime_request_url().rstrip("/")
    )
    token = await get_or_generate_img_token(user)
    return f"{base_url}/get-audio/{quote(relative_path)}?{urlencode({'token': token})}"


async def deliver_to_platform(platform: str, ctx: dict, text: str, *, user=None, is_current=None) -> bool:
    """Send text/audio, checking an optional foreground guard before TTS and sends."""
    answer_mode = ctx.get("answer_mode", "text")
    conversation_id = ctx.get("conversation_id", 0)
    if answer_mode == "voice" and user is None:
        raise ValueError("Voice delivery requires a resolved user")

    if platform == "telegram":
        chat_id = ctx.get("chat_id")
        if not chat_id:
            logger.error("deliver_to_platform: missing chat_id for telegram")
            return False
        if async_telegram is None:
            logger.error("deliver_to_platform: Telegram client is not configured")
            return False

        if answer_mode == "voice":
            if is_current is not None and not await is_current():
                return False
            try:
                audio_path, error = await handle_tts_request(
                    None,
                    {"text": text, "author": "bot", "conversationId": conversation_id},
                    user,
                    is_whatsapp=True,
                    tts_context="external",
                )
                if not error and audio_path:
                    audio_file = Path(audio_path)
                    if audio_file.exists():
                        if is_current is not None and not await is_current():
                            return False
                        await async_telegram.send_voice(chat_id, audio_file.read_bytes())
                        return True
            except Exception as tts_err:
                logger.warning("Telegram TTS failed, falling back to text: %s", tts_err)

        for chunk in chunk_telegram_response(text):
            if is_current is not None and not await is_current():
                return False
            await async_telegram.send_message(chat_id, chunk)
        return True

    if platform == "whatsapp":
        from_number = ctx.get("from_number")
        to_number = ctx.get("to_number")
        if async_twilio is None:
            logger.error("deliver_to_platform: Twilio client is not configured")
            return False

        if answer_mode == "voice":
            if is_current is not None and not await is_current():
                return False
            try:
                audio_path, error = await handle_tts_request(
                    None,
                    {"text": text, "author": "bot", "conversationId": conversation_id},
                    user,
                    is_whatsapp=True,
                    tts_context="external",
                )
                if not error and audio_path:
                    audio_url = await whatsapp_audio_url(audio_path, user)
                    if is_current is not None and not await is_current():
                        return False
                    await async_twilio.send_message(
                        body="",
                        media_url=[audio_url],
                        from_=to_number,
                        to=from_number,
                    )
                    return True
            except Exception as tts_err:
                logger.warning("WhatsApp TTS failed, falling back to text: %s", tts_err)

        for chunk in await chunk_whatsapp_response(text):
            if is_current is not None and not await is_current():
                return False
            await async_twilio.send_message(body=chunk, from_=to_number, to=from_number)
        return True

    logger.error("deliver_to_platform: unknown platform %s", platform)
    return False


async def send_platform_error(platform: str, ctx: dict, error_code: str, *, translator=None, is_current=None) -> bool:
    """Deliver localized service errors; provider diagnostics stay in server logs."""
    try:
        tr = chat_translator(translator)
        if isinstance(error_code, str) and error_code in {"gransabio_disabled", "gransabio_own_keys", "generation_stopped"}:
            error_msg = tr.t("channel_notices." + error_code)
        else:
            error_msg = chat_error(tr, error_code)
        return await deliver_to_platform(
            platform, {**ctx, "answer_mode": "text"}, error_msg, is_current=is_current,
        )
    except Exception as exc:
        logger.warning(
            "send_platform_error: failed to deliver error to %s: %s",
            platform,
            exc,
        )
        return False
