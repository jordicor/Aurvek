"""Presentation of native chat failures, without changing inference or history."""

import re

import orjson
from starlette.responses import JSONResponse, StreamingResponse

from i18n import Translator, get_catalogs

_FRAME_END = re.compile(br"\r?\n\r?\n")


def chat_translator(user=None) -> Translator:
    """Use the loaded identity; a delegated frame keeps its own preference."""
    if isinstance(user, Translator):
        return user
    principal = getattr(user, "embed_principal", None)
    return Translator(getattr(principal or user, "ui_language", "en"))


def chat_text(user, key: str, **params) -> str:
    return chat_translator(user).t("chat_errors." + key, **params)


def chat_error(user, code=None) -> str:
    return _error_text(chat_translator(user), code)


def _error_text(translator: Translator, code) -> str:
    # Codes are protocol identifiers. Provider exception text is never a key
    # or a translation parameter, and never crosses this UI boundary.
    messages = get_catalogs()["en"].get("chat_errors", {})
    value = messages.get(code) if isinstance(code, str) else None
    if isinstance(value, str) and "{" not in value:
        return translator.t("chat_errors." + code)
    return translator.t("chat_errors.request_failed")


def localize_error_payload(payload: dict, translator: Translator) -> dict:
    """Keep actionable metadata and codes; replace only product error prose."""
    code = payload.get("error_code") or payload.get("code")
    if not code and isinstance(payload.get("error"), str):
        code = payload["error"]
    result = dict(payload)
    result.pop("provider_message", None)
    result.pop("provider_health_message", None)
    text = _error_text(translator, code)
    for field in ("error", "message", "detail"):
        if field in result and isinstance(result[field], str):
            result[field] = text
    if "retry_hint" in result and code == "pdf_too_large":
        result["retry_hint"] = translator.t("chat_errors.pdf_too_large")
    return result


def _localize_frame(frame: bytes, translator: Translator) -> bytes:
    lines = frame.splitlines(keepends=True)
    data_lines = [line[5:].lstrip(b" ") for line in lines if line.startswith(b"data:")]
    if not data_lines:
        return frame
    try:
        payload = orjson.loads(b"\n".join(line.rstrip(b"\r\n") for line in data_lines))
    except orjson.JSONDecodeError:
        return frame  # Includes [DONE], which is a protocol sentinel.
    if not isinstance(payload, dict) or not payload.get("error"):
        return frame
    localized = orjson.dumps(localize_error_payload(payload, translator))
    result = []
    written = False
    for line in lines:
        if line.startswith(b"data:"):
            if not written:
                newline = b"\r\n" if line.endswith(b"\r\n") else b"\n"
                result.append(b"data: " + localized + newline)
                written = True
        else:
            result.append(line)
    return b"".join(result)


async def localized_chat_stream(source, translator: Translator):
    """Capture locale before streaming; retain SSE framing and source cleanup."""
    buffer = b""
    try:
        async for chunk in source:
            buffer += chunk.encode("utf-8") if isinstance(chunk, str) else chunk
            while (boundary := _FRAME_END.search(buffer)) is not None:
                frame, buffer = buffer[:boundary.end()], buffer[boundary.end():]
                yield _localize_frame(frame, translator)
        if buffer:
            yield _localize_frame(buffer, translator)
    finally:
        close = getattr(source, "aclose", None)
        if close is not None:
            await close()


def localize_chat_response(response, translator: Translator):
    """Adapt only the native chat endpoint's inference response, after billing."""
    if isinstance(response, StreamingResponse) and response.media_type == "text/event-stream":
        response.body_iterator = localized_chat_stream(response.body_iterator, translator)
    elif isinstance(response, JSONResponse):
        try:
            payload = orjson.loads(response.body)
        except orjson.JSONDecodeError:
            return response
        if isinstance(payload, dict) and (
            response.status_code >= 400 or payload.get("error") or payload.get("success") is False
        ):
            response.body = orjson.dumps(localize_error_payload(payload, translator))
            response.headers["content-length"] = str(len(response.body))
    return response
