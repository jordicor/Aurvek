import asyncio

from ai_runtime.dependencies import *
from ai_runtime.attachments.paths import _resolve_legacy_attachment_path

PDF_RETRY_TOKEN_TTL_SECONDS = 30 * 60


def _prepare_pdf_payload(pdf_data: bytes, extract_text: bool) -> tuple[str, str | None]:
    pdf_b64 = base64.b64encode(pdf_data).decode("utf-8")
    extracted_text = extract_pdf_text_local(pdf_data) if extract_text else None
    return pdf_b64, extracted_text


def _load_legacy_pdf_payload(file_path, extract_text: bool) -> tuple[str, str | None]:
    """Read and prepare a legacy PDF in one worker-thread block."""
    with open(file_path, "rb") as file_handle:
        pdf_data = file_handle.read()
    return _prepare_pdf_payload(pdf_data, extract_text)


def _merge_pdf_error_metadata(*metas: dict | None) -> dict | None:
    pdfs = []
    has_other_attachments = False
    for meta in metas:
        if not meta:
            continue
        has_other_attachments = has_other_attachments or bool(meta.get("has_other_attachments"))
        if isinstance(meta.get("pdfs"), list):
            pdfs.extend(meta["pdfs"])
        elif meta.get("filename") or meta.get("pages"):
            pdfs.append({
                "filename": meta.get("filename") or "document.pdf",
                "pages": meta.get("pages") or 0,
                "file_hash": meta.get("file_hash") or meta.get("retry_file_hash"),
                "retry_source_hash": meta.get("retry_source_hash"),
                "retry_source_pages": meta.get("retry_source_pages"),
            })
    if not pdfs:
        return None

    page_counts = []
    for pdf in pdfs:
        try:
            page_counts.append(max(0, int(pdf.get("pages") or 0)))
        except (TypeError, ValueError):
            page_counts.append(0)
    total_pages = sum(page_counts)
    if len(pdfs) == 1:
        filename = pdfs[0].get("filename") or "document.pdf"
        pages = page_counts[0]
        file_hash = pdfs[0].get("file_hash")
        retry_source_hash = pdfs[0].get("retry_source_hash")
        retry_source_pages = pdfs[0].get("retry_source_pages")
    else:
        filename = f"{len(pdfs)} PDF files"
        pages = total_pages
        file_hash = None
        retry_source_hash = None
        retry_source_pages = None
    return {
        "filename": filename,
        "pages": pages,
        "pdf_count": len(pdfs),
        "pdfs": pdfs,
        "has_other_attachments": has_other_attachments,
        "file_hash": file_hash,
        "retry_source_hash": retry_source_hash,
        "retry_source_pages": retry_source_pages,
    }


def _extract_pdf_file_hash_from_url(url: str | None) -> str | None:
    if not url:
        return None
    basename = os.path.basename(urllib.parse.urlparse(url).path)
    maybe_hash = basename.split("_", 1)[0]
    if re.fullmatch(r"[0-9a-fA-F]{40}", maybe_hash or ""):
        return maybe_hash.lower()
    return None


def _extract_pdf_metadata_from_saved_message(user_message) -> dict | None:
    """Extract PDF metadata from a saved multimodal message."""
    if user_message is None:
        return None
    try:
        parsed = orjson.loads(user_message) if isinstance(user_message, str) else user_message
    except (orjson.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, list):
        return None
    pdf_blocks = []
    has_other_attachments = False
    for block in parsed:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type != "document_url":
            if block_type in {"image_url", "text_file", "document", "document_bytes", "file"}:
                has_other_attachments = True
            continue
        info = block.get("document_url") or {}
        retry_source_hash = info.get("retry_source_hash")
        retry_source_pages = info.get("retry_source_pages")
        pdf_blocks.append({
            "filename": info.get("filename") or "document.pdf",
            "pages": info.get("pages") or 0,
            "file_hash": info.get("file_hash") or _extract_pdf_file_hash_from_url(info.get("url")),
            "retry_source_hash": retry_source_hash,
            "retry_source_pages": retry_source_pages,
        })
    if not pdf_blocks:
        return None
    return _merge_pdf_error_metadata({
        "pdfs": pdf_blocks,
        "has_other_attachments": has_other_attachments,
    })


def _extract_pdf_metadata_from_context_messages(context_messages) -> dict | None:
    metas = []
    for msg in context_messages or []:
        if isinstance(msg, dict):
            content = msg.get("message")
        else:
            content = getattr(msg, "message", None)
        metas.append(_extract_pdf_metadata_from_saved_message(content))
    return _merge_pdf_error_metadata(*metas)


def _pdf_page_total_from_messages(context_messages) -> int:
    meta = _extract_pdf_metadata_from_context_messages(context_messages)
    return int((meta or {}).get("pages") or 0)


def _pdf_count_from_metadata(meta: dict | None) -> int:
    try:
        return int((meta or {}).get("pdf_count") or 0)
    except (TypeError, ValueError):
        return 0


def _messages_have_saved_pdfs(context_messages) -> bool:
    return _extract_pdf_metadata_from_context_messages(context_messages) is not None


def _drop_pdf_blocks_from_context(context_messages: list) -> list:
    filtered = []
    skip_next_assistant = False
    for msg in context_messages or []:
        if not isinstance(msg, dict):
            if skip_next_assistant:
                skip_next_assistant = False
                continue
            filtered.append(msg)
            continue
        msg_type = msg.get("type")
        if skip_next_assistant and msg_type != "user":
            skip_next_assistant = False
            continue
        content = msg.get("message")
        if not isinstance(content, list):
            filtered.append(msg)
            continue
        had_pdf = any(
            isinstance(block, dict) and block.get("type") == "document_url"
            for block in content
        )
        blocks = [
            block for block in content
            if not (isinstance(block, dict) and block.get("type") == "document_url")
        ]
        if had_pdf and msg_type == "user":
            skip_next_assistant = True
        if blocks:
            filtered.append({**msg, "message": blocks})
    return filtered


def _looks_like_pdf_size_error(
    message: str,
    has_pdf: bool = False,
    mixed_attachments: bool = False,
) -> bool:
    """Detect provider errors that mean the attached PDF must be reduced."""
    text = (message or "").lower()
    explicit_pdf_terms = ("pdf", "document", "page", "pages")
    strong_context_terms = (
        "too many pages",
        "page limit",
        "maximum pages",
        "maximum of",
        "maximum number of pages",
        "context length",
        "context window",
        "context_length_exceeded",
        "prompt is too long",
        "input is too long",
        "too many tokens",
        "token limit",
        "tokens exceed",
        "maximum context",
        "maximum input",
        "input length",
    )
    generic_size_terms = (
        "pdf_too_large",
        "pdf-too-large",
        "request_too_large",
        "payload_too_large",
        "content_too_large",
        "request entity too large",
        "payload too large",
        "413:",
        "service error (413)",
        " 413",
        "exceeds",
        "too large",
        "file size",
        "request body",
    )
    if has_pdf:
        has_explicit_pdf_context = any(term in text for term in explicit_pdf_terms)
        explicit_size_codes = (
            "pdf_too_large",
            "pdf-too-large",
            "request_too_large",
            "payload_too_large",
            "content_too_large",
            "413:",
            "service error (413)",
            " 413",
        )
        if text.strip() == "413" or any(term in text for term in explicit_size_codes):
            return True
        if any(term in text for term in strong_context_terms):
            return (not mixed_attachments) or has_explicit_pdf_context
        if mixed_attachments and not has_explicit_pdf_context:
            return False
        return any(term in text for term in generic_size_terms)
    if "pdf" not in text and "document" not in text and "file" not in text:
        return False
    return any(term in text for term in (*strong_context_terms, *generic_size_terms))


def _looks_like_generic_context_limit_error(message: str) -> bool:
    text = (message or "").lower()
    return any(term in text for term in (
        "context length",
        "context window",
        "context_length_exceeded",
        "prompt is too long",
        "input is too long",
        "too many tokens",
        "token limit",
        "tokens exceed",
        "maximum context",
        "maximum input",
        "input length",
    ))


def _message_mentions_pdf_context(message: str) -> bool:
    text = (message or "").lower()
    return any(term in text for term in ("pdf", "document", "page", "pages"))


def _extract_token_limit_details(message: str) -> tuple[int, int] | None:
    match = re.search(r"(\d[\d,]*)\s+tokens?\s*>\s*(\d[\d,]*)\s+maximum", message or "", re.IGNORECASE)
    if not match:
        return None
    try:
        used = int(match.group(1).replace(",", ""))
        limit = int(match.group(2).replace(",", ""))
    except ValueError:
        return None
    if used <= 0 or limit <= 0:
        return None
    return used, limit


def _suggest_retry_pages_for_token_limit(pdf_meta: dict, message: str) -> int | None:
    details = _extract_token_limit_details(message)
    if not details:
        return None
    used, limit = details
    try:
        retry_pages = int(pdf_meta.get("retry_pages") or pdf_meta.get("pages") or 0)
    except (TypeError, ValueError):
        retry_pages = 0
    if retry_pages <= 1:
        return None
    ratio = min(1.0, limit / used)
    suggested = int((retry_pages * ratio * 0.8) + 0.999)
    return max(1, min(retry_pages - 1, suggested))


def _create_pdf_retry_token(
    pdf_meta: dict | None,
    current_user=None,
    conversation_id: int | None = None,
) -> str | None:
    if not pdf_meta or current_user is None or conversation_id is None:
        return None
    current_pdf_count = _pdf_count_from_metadata({"pdf_count": pdf_meta.get("current_pdf_count")})
    if current_pdf_count != 1 or not pdf_meta.get("range_retry_available", True):
        return None
    retry_file_hash = (
        pdf_meta.get("retry_source_hash")
        or pdf_meta.get("retry_file_hash")
        or pdf_meta.get("file_hash")
        or next(
            (p.get("file_hash") for p in pdf_meta.get("pdfs", []) if isinstance(p, dict) and p.get("file_hash")),
            None,
        )
    )
    if not retry_file_hash:
        return None
    retry_pages = int(pdf_meta.get("retry_pages") or pdf_meta.get("pages") or 0)
    payload = {
        "kind": "pdf_range_retry",
        "user_id": int(current_user.id),
        "conversation_id": int(conversation_id),
        "retry_filename": pdf_meta.get("retry_filename") or pdf_meta.get("filename"),
        "retry_pages": retry_pages,
        "source_pages": int(pdf_meta.get("retry_source_pages") or pdf_meta.get("source_pages") or retry_pages),
        "file_hash": retry_file_hash,
        "allow_skip_context_pdfs": True,
        "exp": datetime.now(timezone.utc) + timedelta(seconds=PDF_RETRY_TOKEN_TTL_SECONDS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _decode_pdf_retry_token(token: str | None, current_user, conversation_id: int) -> dict | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
    try:
        if payload.get("kind") != "pdf_range_retry":
            return None
        if int(payload.get("user_id")) != int(current_user.id):
            return None
        if int(payload.get("conversation_id")) != int(conversation_id):
            return None
    except (TypeError, ValueError):
        return None
    return payload


def _validate_pdf_retry_upload(pdf_retry_payload: dict, pdf_data: bytes, page_count: int) -> str | None:
    expected_hash = (pdf_retry_payload or {}).get("file_hash")
    if expected_hash:
        actual_hash = hashlib.sha1(pdf_data).hexdigest()
        if actual_hash != str(expected_hash).lower():
            return "PDF range retry must use the same PDF that failed."
    try:
        expected_pages = int((pdf_retry_payload or {}).get("source_pages") or 0)
    except (TypeError, ValueError):
        expected_pages = 0
    if expected_pages and int(page_count or 0) != expected_pages:
        return "PDF range retry must use the same PDF page count that failed."
    return None


def _pdf_too_large_payload(
    provider_label: str,
    message: str,
    user_message=None,
    pdf_metadata: dict | None = None,
    current_user=None,
    conversation_id: int | None = None,
) -> dict:
    pdf_meta = pdf_metadata or _extract_pdf_metadata_from_saved_message(user_message) or {}
    pages = pdf_meta.get("pages") or 0
    filename = pdf_meta.get("filename") or "document.pdf"
    pdf_count = int(pdf_meta.get("pdf_count") or 0)
    range_retry_available = bool(pdf_meta.get("range_retry_available", pdf_count == 1))
    token_limit_details = _extract_token_limit_details(message)
    suggested_page_end = _suggest_retry_pages_for_token_limit(pdf_meta, message)
    retry_reason = "token_limit" if token_limit_details else "pdf_limit"
    friendly = "PDF too large for the selected AI model."
    if pdf_count > 1 and pages:
        friendly = f"PDFs too large for the selected AI model ({pdf_count} files, {pages} pages total)."
    elif pdf_count > 1:
        friendly = f"PDFs too large for the selected AI model ({pdf_count} files)."
    elif pages:
        friendly = f"PDF too large for the selected AI model ({pages} pages)."
    payload = {
        "error": friendly,
        "error_code": "pdf_too_large",
        "pdf_too_large": True,
        "provider": provider_label,
        "provider_message": message,
        "filename": filename,
        "pages": pages,
        "pdf_count": pdf_count,
        "current_pdf_count": int(pdf_meta.get("current_pdf_count") or 0),
        "context_pdf_count": int(pdf_meta.get("context_pdf_count") or 0),
        "range_retry_available": range_retry_available,
        "retry_filename": pdf_meta.get("retry_filename"),
        "retry_pages": pdf_meta.get("retry_pages") or 0,
        "retry_reason": retry_reason,
    }
    if retry_reason == "token_limit":
        payload["retry_hint"] = "That page range is still too large for this model's context window. Select fewer pages."
    else:
        payload["retry_hint"] = "This model cannot accept the PDF as sent. Select a smaller page range."
    if suggested_page_end:
        payload["suggested_page_end"] = suggested_page_end
    retry_token = _create_pdf_retry_token(pdf_meta, current_user, conversation_id)
    if retry_token:
        payload["retry_token"] = retry_token
    return payload

def _pdf_upload_too_large_payload(
    message: str,
    current_pdf_count: int,
    current_pages: int = 0,
    context_pdf_count: int = 0,
    context_pages: int = 0,
    filename: str | None = None,
    current_user=None,
    conversation_id: int | None = None,
    retry_file_hash: str | None = None,
) -> dict:
    total_pages = int(current_pages or 0) + int(context_pages or 0)
    total_pdf_count = int(current_pdf_count or 0) + int(context_pdf_count or 0)
    retry_available = int(current_pdf_count or 0) == 1
    payload = {
        "success": False,
        "message": message,
        "error": message,
        "error_code": "pdf_too_large",
        "pdf_too_large": True,
        "provider": "Aurvek",
        "provider_message": message,
        "filename": filename or ("document.pdf" if total_pdf_count == 1 else f"{total_pdf_count} PDF files"),
        "pages": total_pages,
        "pdf_count": total_pdf_count,
        "current_pdf_count": int(current_pdf_count or 0),
        "context_pdf_count": int(context_pdf_count or 0),
        "range_retry_available": retry_available,
        "retry_filename": filename,
        "retry_pages": int(current_pages or 0),
    }
    if retry_available:
        retry_token = _create_pdf_retry_token(
            {
                "current_pdf_count": int(current_pdf_count or 0),
                "context_pdf_count": int(context_pdf_count or 0),
                "range_retry_available": True,
                "retry_filename": filename,
                "retry_pages": int(current_pages or 0),
                "retry_file_hash": retry_file_hash,
            },
            current_user,
            conversation_id,
        )
        if retry_token:
            payload["retry_token"] = retry_token
    return payload


def _estimate_pdf_input_tokens_for_preflight(page_count: int, machine: str) -> int:
    per_page = 300 if machine == "Gemini" else 1500
    return max(0, int(page_count or 0)) * per_page

def format_pdf_for_provider(machine: str, pdf_url_base: str, pdf_data_b64: str,
                            filename: str, page_count: int, extracted_text: str = None):
    """Format a PDF for storage and for sending to the current AI provider."""
    content_to_save = {
        "type": "document_url",
        "document_url": {"url": pdf_url_base, "filename": filename, "pages": page_count}
    }

    if machine == "Claude":
        content_to_send = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_data_b64}
        }
    elif machine == "Gemini":
        content_to_send = {
            "type": "document_bytes",
            "data": pdf_data_b64,
            "mime_type": "application/pdf"
        }
    elif machine in ("OpenRouter", "GPT", "xAI"):
        content_to_send = {
            "type": "file",
            "file": {
                "filename": filename,
                "file_data": f"data:application/pdf;base64,{pdf_data_b64}"
            }
        }
    elif machine in ("O1", "MiniMax", "Kimi"):
        content_to_send = {
            "type": "text",
            "text": f"[Content of uploaded PDF: {filename} ({page_count} pages)]\n\n{extracted_text}"
        }
    else:
        raise ValueError(f"Unsupported provider for PDFs: {machine}")

    return content_to_save, content_to_send


def _ranged_pdf_warning_text(
    filename: str,
    *,
    page_start: int | None,
    page_end: int | None,
    source_page_count: int | None,
) -> str:
    range_text = f"pages {page_start}-{page_end}" if page_start and page_end else "a page range"
    source_text = f" of the original {source_page_count}-page PDF" if source_page_count else ""
    return (
        "[WARNING] The attached PDF had to be cropped before upload because the full PDF was too large "
        f"for this model. This attachment contains only {range_text}{source_text}: {filename}. "
        "Pages outside that range are not attached. If a table of contents, index, footer, or other text mentions "
        "pages outside the attached range, treat those as references to missing pages, not as pages you can read."
    )


async def hydrate_pdf_for_context(
    block: dict,
    machine: str,
    current_user=None,
    conversation_id: int | None = None,
) -> dict | None:
    """Re-hydrate a stored document_url block for sending to AI provider."""
    doc_info = block["document_url"]
    url = doc_info.get("url", "")
    filename = doc_info.get("filename", "document.pdf")
    page_count = doc_info.get("pages", 0)

    attachment_ref = doc_info.get("attachment_ref")
    if attachment_ref and current_user is not None:
        try:
            result = await read_attachment_bytes(
                attachment_ref,
                user_id=current_user.id,
                conversation_id=conversation_id,
                require_kind="pdf",
            )
        except Exception as exc:
            logger.warning("[hydrate_pdf_for_context] Could not read attachment %s: %s", attachment_ref, exc)
            result = None
        if result:
            pdf_data, attachment = result
            page_count = attachment.get("page_count") or page_count

            pdf_b64, extracted_text = await asyncio.to_thread(
                _prepare_pdf_payload,
                pdf_data,
                machine in ("O1", "MiniMax", "Kimi"),
            )

            if machine == "Claude":
                return {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}
                }
            elif machine == "Gemini":
                return {
                    "type": "document_bytes",
                    "data": pdf_b64,
                    "mime_type": "application/pdf"
                }
            elif machine in ("OpenRouter", "GPT", "xAI"):
                return {
                    "type": "file",
                    "file": {
                        "filename": filename,
                        "file_data": f"data:application/pdf;base64,{pdf_b64}"
                    }
                }
            elif machine in ("O1", "MiniMax", "Kimi"):
                return {
                    "type": "text",
                    "text": f"[Content of PDF: {filename} ({page_count} pages)]\n\n{extracted_text}"
                }
            else:
                raise ValueError(f"Unsupported provider for PDF hydration: {machine}")

    resolved_legacy = _resolve_legacy_attachment_path(
        url,
        current_user,
        conversation_id=conversation_id,
        expected_kind="pdf",
    )
    if not resolved_legacy:
        logger.warning("[hydrate_pdf_for_context] Rejected unsafe legacy PDF URL")
        return None
    _, file_path = resolved_legacy

    try:
        pdf_b64, extracted_text = await asyncio.to_thread(
            _load_legacy_pdf_payload,
            file_path,
            machine in ("O1", "MiniMax", "Kimi"),
        )
    except FileNotFoundError:
        logger.warning(f"PDF file not found: {file_path}")
        return None

    if machine == "Claude":
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}
        }
    elif machine == "Gemini":
        return {
            "type": "document_bytes",
            "data": pdf_b64,
            "mime_type": "application/pdf"
        }
    elif machine in ("OpenRouter", "GPT", "xAI"):
        return {
            "type": "file",
            "file": {
                "filename": filename,
                "file_data": f"data:application/pdf;base64,{pdf_b64}"
            }
        }
    elif machine in ("O1", "MiniMax", "Kimi"):
        return {
            "type": "text",
            "text": f"[Content of PDF: {filename} ({page_count} pages)]\n\n{extracted_text}"
        }
    else:
        raise ValueError(f"Unsupported provider for PDF hydration: {machine}")
