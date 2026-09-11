from ai_runtime.dependencies import *
from ai_runtime.attachments.paths import _resolve_legacy_attachment_path

async def hydrate_image_for_context(
    image_block: dict,
    machine: str,
    current_user,
    force_base64: bool = False,
    conversation_id: int | None = None,
) -> dict:
    """Re-hydrate a stored image block with a fresh token URL for AI provider access.

    Takes a stored block like {"type":"image_url","image_url":{"url":"https://cdn.../hash_fullsize.webp"}}
    and returns a provider-appropriate format with authenticated URL.

    For xAI: reads WebP from disk and converts to JPEG base64 (xAI does not support WebP).
    """
    image_info = image_block.get("image_url", {})
    from chat.services.generated_media import parse_generated_media_url, read_generated_image_bytes
    generated_url = image_info.get("fullsize_url") or image_info.get("url", "")
    if parse_generated_media_url(generated_url):
        data = await read_generated_image_bytes(
            generated_url, user_id=current_user.id, conversation_id=conversation_id,
        )
        if data is None:
            return None
        with PilImage.open(io.BytesIO(data)) as generated_image:
            mime_type = PilImage.MIME.get(generated_image.format, "image/webp")
        return await asyncio.to_thread(
            image_block_to_provider_block, data=data, mime_type=mime_type,
            machine=machine, force_base64=force_base64,
        )
    attachment_ref = image_info.get("attachment_ref")
    if attachment_ref:
        try:
            result = await read_attachment_bytes(
                attachment_ref,
                user_id=current_user.id,
                conversation_id=conversation_id,
                require_kind="image",
            )
        except Exception as exc:
            logger.warning("[hydrate_image_for_context] Could not read attachment %s: %s", attachment_ref, exc)
            result = None
        if result:
            image_data, attachment = result
            provider_block = await asyncio.to_thread(
                image_block_to_provider_block,
                data=image_data,
                mime_type=attachment.get("mime_detected") or "image/webp",
                machine=machine,
                force_base64=force_base64,
            )
            if provider_block is not None:
                return provider_block

    base_url = image_block.get("image_url", {}).get("url", "")
    resolved_legacy = _resolve_legacy_attachment_path(
        base_url,
        current_user,
        conversation_id=conversation_id,
        expected_kind="image",
    )
    if not resolved_legacy:
        logger.warning("[hydrate_image_for_context] Rejected unsafe legacy image URL")
        return None
    image_path, disk_path = resolved_legacy

    # Only enter thread when Pillow/IO work is actually needed
    needs_pillow = (
        (machine == "xAI" and image_path.lower().endswith(".webp") and not force_base64)
        or force_base64
    )

    if needs_pillow:
        result = await asyncio.to_thread(
            _convert_image_for_provider_sync, disk_path, image_path, machine, force_base64
        )
        # result is dict (success) or None (error, already logged in sync helper)
        return result

    # Generate authenticated URL
    if CLOUDFLARE_FOR_IMAGES:
        token_url = generate_signed_url_cloudflare(image_path, expiration_seconds=3600)
    else:
        token = await get_or_generate_img_token(current_user)
        token_url = f"{CLOUDFLARE_BASE_URL}{image_path}?token={token}"

    if machine == "Claude":
        return {
            "type": "image",
            "source": {
                "type": "url",
                "url": token_url,
            }
        }
    # OpenAI-compatible providers and Gemini use image_url format with token URL.
    return {
        "type": "image_url",
        "image_url": {"url": token_url}
    }

def _convert_image_for_provider_sync(
    disk_path: str, image_path: str, machine: str, force_base64: bool
) -> dict | None:
    """Read image from disk and convert for provider. Sync -- called via to_thread.

    Returns the formatted image block dict, or None on failure (caller skips image).
    Only called when Pillow work is needed (caller checks conditions).
    """
    # Branch 1: xAI WebP conversion (non-force_base64)
    if machine == "xAI" and image_path.lower().endswith(".webp") and not force_base64:
        try:
            img = PilImage.open(disk_path)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            b64 = base64.b64encode(buf.getvalue()).decode()
            return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
        except Exception as e:
            logger.warning(f"[hydrate_image_for_context] Could not convert WebP for xAI, skipping image: {e}")
            return None

    # Branch 2: force_base64 (all providers)
    if force_base64:
        try:
            with open(disk_path, "rb") as f:
                raw_bytes = f.read()
            b64 = base64.b64encode(raw_bytes).decode()

            # Detect media type from file extension (not all disk files are WebP)
            lower_path = image_path.lower()
            if lower_path.endswith(".png"):
                media_type = "image/png"
            elif lower_path.endswith((".jpg", ".jpeg")):
                media_type = "image/jpeg"
            else:
                media_type = "image/webp"

            # xAI: WebP -> JPEG conversion
            if machine == "xAI" and media_type == "image/webp":
                img = PilImage.open(io.BytesIO(raw_bytes))
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                b64 = base64.b64encode(buf.getvalue()).decode()
                media_type = "image/jpeg"

            # Claude uses a different content block format
            if machine == "Claude":
                return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}
            return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}}
        except Exception as e:
            logger.warning(f"[hydrate_image_for_context] force_base64 failed for {disk_path}: {e}")
            return None

    return None  # Should not be reached (caller checks needs_pillow)


def format_image_for_provider(machine: str, image_url_base: str, image_data_b64: str, media_type: str):
    """Return (content_to_save, content_to_send) for an image, per provider.

    content_to_save uses a uniform OpenAI-compatible format (image_url with base URL).
    content_to_send varies by provider API requirements.
    """
    content_to_save = {
        "type": "image_url",
        "image_url": {"url": image_url_base}
    }

    if machine == "xAI":
        # xAI only accepts JPEG/PNG — convert WebP to JPEG on the fly
        if media_type == "image/webp":
            jpeg_b64 = convert_to_jpeg_b64(image_data_b64)
            send_media = "image/jpeg"
            send_b64 = jpeg_b64
        else:
            send_media = media_type
            send_b64 = image_data_b64
        content_to_send = {
            "type": "image_url",
            "image_url": {"url": f"data:{send_media};base64,{send_b64}"}
        }
    elif machine in ("GPT", "OpenRouter", "MiniMax", "Kimi"):
        content_to_send = {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{image_data_b64}"}
        }
    elif machine == "Claude":
        content_to_send = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": image_data_b64,
            }
        }
    elif machine == "Gemini":
        content_to_send = {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{image_data_b64}"}
        }
    else:
        raise ValueError(f"Unsupported provider for images: {machine}")

    return content_to_save, content_to_send
