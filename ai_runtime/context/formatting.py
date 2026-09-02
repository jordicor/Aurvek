from ai_runtime.dependencies import *
from ai_runtime.attachments.media import hydrate_image_for_context
from ai_runtime.attachments.pdf import hydrate_pdf_for_context
from ai_runtime.attachments.text_files import text_file_block_to_text_for_context
from ai_runtime.reasoning_tags import strip_tagged_thinking_prefix

def filter_invalid_context_messages(context_messages: list) -> list:
    """Remove messages with empty/null/whitespace-only content from context.
    Defense-in-depth: prevents empty messages from crashing API calls.
    Returns the filtered list. Logs warnings for each removed message."""
    filtered = []
    for msg in context_messages:
        message_content = msg.get('message') if isinstance(msg, dict) else msg['message']
        if (
            isinstance(msg, dict)
            and msg.get('type') == 'bot'
            and isinstance(message_content, str)
        ):
            clean_content = strip_tagged_thinking_prefix(message_content)
            if clean_content != message_content:
                msg = {**msg, 'message': clean_content}
                message_content = clean_content
        if message_content is None:
            logger.warning(f"Filtered out message with None content (type={msg.get('type', '?')})")
            continue
        if isinstance(message_content, list):
            # Multimodal: sanitize internal text blocks -- remove empty ones
            sanitized = [
                block for block in message_content
                if not (isinstance(block, dict) and block.get('type') == 'text'
                        and not block.get('text', '').strip())
            ]
            if sanitized:
                if len(sanitized) < len(message_content):
                    logger.warning(f"Removed {len(message_content) - len(sanitized)} empty text block(s) "
                                   f"from multimodal {msg.get('type', '?')} message")
                    msg = {**msg, 'message': sanitized}
                filtered.append(msg)
            else:
                logger.warning(f"Filtered out multimodal {msg.get('type', '?')} message: all blocks empty")
            continue
        if isinstance(message_content, str) and not message_content.strip():
            logger.warning(f"Filtered out empty {msg.get('type', '?')} message from context")
            continue
        filtered.append(msg)
    return filtered

def _flatten_multi_ai_bot_message(raw_message: str) -> Optional[str]:
    """Flatten a stored Multi-AI JSON bot message into plain text context."""
    if not isinstance(raw_message, str):
        return None

    try:
        parsed = orjson.loads(raw_message)
    except (orjson.JSONDecodeError, TypeError, ValueError):
        return None

    responses = parsed.get("responses") if isinstance(parsed, dict) else None
    if not (isinstance(parsed, dict) and parsed.get("multi_ai") and isinstance(responses, list)):
        return None

    parts = ["[Multi-AI Response]"]
    for idx, response in enumerate(responses):
        if not isinstance(response, dict):
            continue
        model_label = response.get("model") or response.get("machine") or f"Model {idx + 1}"
        content = response.get("content", "")
        if content is None:
            content = ""
        content_text = str(content)
        if response.get("error"):
            parts.append(f"{model_label}: [Error: {content_text}]")
        else:
            parts.append(f"{model_label}: {content_text}")
    parts.append("[End Multi-AI Response]")
    return "\n".join(parts)


def flatten_multi_ai_context(messages_dicts: list) -> list:
    """Return a copy of context messages with Multi-AI bot payloads flattened."""
    flattened = []
    for msg in messages_dicts or []:
        if not isinstance(msg, dict):
            flattened.append(msg)
            continue

        if msg.get("type") == "bot":
            flattened_message = _flatten_multi_ai_bot_message(msg.get("message"))
            if flattened_message is not None:
                new_msg = msg.copy()
                new_msg["message"] = flattened_message
                flattened.append(new_msg)
                continue

        flattened.append(msg)
    return flattened


def parse_stored_message(content):
    """Parse a stored message that may be a JSON-encoded list (image messages).

    Messages with images are stored as JSON strings like:
      '[{"type":"image_url","image_url":{"url":"..."}},{"type":"text","text":"..."}]'
    This returns the parsed list, or the original string if it's not a JSON list.
    """
    if isinstance(content, str) and content.startswith('['):
        try:
            parsed = orjson.loads(content)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    return content

async def _format_messages_for_provider(
    context_messages: list,
    message,
    full_prompt: str,
    machine: str,
    current_user=None,
    force_base64: bool = False,
    conversation_id: int | None = None,
) -> list | str:
    """Format messages for a specific LLM provider.
    Extracted from get_ai_response() to be reused by watchdog_takeover_response()."""
    context_messages = flatten_multi_ai_context(context_messages)
    context_messages = filter_invalid_context_messages(context_messages)
    api_messages = []

    if machine == "Gemini":
        contents = []
        for msg in context_messages:
            role = "user" if msg["type"] == "user" else "model"
            message_content = msg["message"]
            if isinstance(message_content, list):
                parts = []
                for block in message_content:
                    if block.get("type") == "text":
                        parts.append(genai_types.Part.from_text(text=block["text"]))
                    elif block.get("type") == "image_url":
                        url = block["image_url"]["url"]
                        if current_user:
                            hydrated_block = await hydrate_image_for_context(
                                block,
                                "Gemini",
                                current_user,
                                force_base64=force_base64,
                                conversation_id=conversation_id,
                            )
                            if hydrated_block is None:
                                continue
                            token_url = hydrated_block["image_url"]["url"]
                        else:
                            token_url = url
                        mime = "image/webp"
                        if url.lower().endswith(".png"):
                            mime = "image/png"
                        elif url.lower().endswith(".jpg") or url.lower().endswith(".jpeg"):
                            mime = "image/jpeg"
                        if token_url.startswith("data:"):
                            header, b64_data = token_url.split(",", 1)
                            mime = header.split(":")[1].split(";")[0]
                            parts.append(genai_types.Part.from_bytes(data=base64.b64decode(b64_data), mime_type=mime))
                        else:
                            parts.append(genai_types.Part.from_uri(file_uri=token_url, mime_type=mime))
                    elif block.get("type") == "document_url":
                        hydrated_block = await hydrate_pdf_for_context(block, "Gemini", current_user, conversation_id=conversation_id)
                        if hydrated_block is not None:
                            parts.append(genai_types.Part.from_bytes(
                                data=base64.b64decode(hydrated_block["data"]),
                                mime_type="application/pdf"
                            ))
                    elif block.get("type") == "text_file":
                        parts.append(genai_types.Part.from_text(text=await text_file_block_to_text_for_context(block, current_user, conversation_id=conversation_id)))
                if parts:
                    contents.append(genai_types.Content(role=role, parts=parts))
            else:
                contents.append(genai_types.Content(role=role, parts=[genai_types.Part.from_text(text=str(message_content))]))

        # Add new user message
        if isinstance(message, list):
            parts = []
            for block in message:
                if block.get("type") == "text":
                    parts.append(genai_types.Part.from_text(text=block["text"]))
                elif block.get("type") == "image_url":
                    url = block["image_url"]["url"]
                    if url.startswith("data:"):
                        # New message: base64 data URL -> use from_bytes
                        header, b64_data = url.split(",", 1)
                        mime = header.split(":")[1].split(";")[0]
                        parts.append(genai_types.Part.from_bytes(data=base64.b64decode(b64_data), mime_type=mime))
                    else:
                        # Token URL -> use from_uri
                        mime = "image/webp"
                        if url.lower().endswith(".png"):
                            mime = "image/png"
                        elif url.lower().endswith(".jpg") or url.lower().endswith(".jpeg"):
                            mime = "image/jpeg"
                        parts.append(genai_types.Part.from_uri(file_uri=url, mime_type=mime))
                elif block.get("type") == "document_bytes":
                    parts.append(genai_types.Part.from_bytes(
                        data=base64.b64decode(block["data"]),
                        mime_type=block["mime_type"]
                    ))
            contents.append(genai_types.Content(role="user", parts=parts))
        else:
            contents.append(genai_types.Content(role="user", parts=[genai_types.Part.from_text(text=str(message))]))
        return contents

    elif machine == "O1":
        # ``full_prompt`` is passed separately to the O1 provider as a
        # privileged developer message.  Never duplicate it into user content.
        combined_message_content = str(message)
        for msg in context_messages:
            msg_content = msg["message"]
            if isinstance(msg_content, list):
                text_parts = []
                for block in msg_content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block["text"])
                        elif block.get("type") == "document_url":
                            hydrated = await hydrate_pdf_for_context(block, "O1", current_user, conversation_id=conversation_id)
                            if hydrated is not None:
                                text_parts.append(hydrated["text"])
                        elif block.get("type") == "text_file":
                            text_parts.append(await text_file_block_to_text_for_context(block, current_user, conversation_id=conversation_id))
                        elif block.get("type") == "image_url":
                            text_parts.append("[An image was shared]")
                msg_content = "\n".join(text_parts) if text_parts else str(msg_content)
            api_messages.append({
                "role": "user" if msg["type"] == "user" else "assistant",
                "content": msg_content,
            })
        api_messages.append({"role": "user", "content": combined_message_content})

    else:
        # GPT, Claude, xAI, OpenRouter
        for i, msg in enumerate(context_messages):
            content = msg["message"]
            if isinstance(content, list):
                # Hydrate image and PDF blocks with fresh data
                hydrated = []
                for block in content:
                    if block.get("type") == "image_url" and current_user:
                        result = await hydrate_image_for_context(
                            block,
                            machine,
                            current_user,
                            force_base64=force_base64,
                            conversation_id=conversation_id,
                        )
                        if result is not None:
                            hydrated.append(result)
                    elif block.get("type") == "document_url":
                        result = await hydrate_pdf_for_context(block, machine, current_user, conversation_id=conversation_id)
                        if result is not None:
                            hydrated.append(result)
                    elif block.get("type") == "text_file":
                        hydrated.append({"type": "text", "text": await text_file_block_to_text_for_context(block, current_user, conversation_id=conversation_id)})
                    else:
                        hydrated.append(block)
                api_messages.append({
                    "role": "user" if msg["type"] == "user" else "assistant",
                    "content": hydrated,
                })
            else:
                if i == len(context_messages) - 2 and msg["type"] == "user" and machine == "Claude":
                    content = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
                else:
                    content = [{"type": "text", "text": content}]
                api_messages.append({
                    "role": "user" if msg["type"] == "user" else "assistant",
                    "content": content,
                })
        # Add new user message
        if machine == "Claude":
            if isinstance(message, list):
                api_messages.append({"role": "user", "content": message})
            else:
                api_messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": message, "cache_control": {"type": "ephemeral"}}],
                })
        else:
            if isinstance(message, list):
                api_messages.append({"role": "user", "content": message})
            else:
                api_messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": message}],
                })

    return api_messages
