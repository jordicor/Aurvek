"""Bounded admission and reply budgets for authenticated WhatsApp webhooks."""

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
import hashlib
import os
import time
from urllib.parse import parse_qsl

from fastapi import HTTPException, Request
from starlette.datastructures import FormData

from database import get_db_connection
from integrations.applications.channel_models import VerifiedChannelEnvelope
from rediscfg import check_rate_limit


MAX_WEBHOOK_BYTES = 64 * 1024
MAX_CONCURRENT_MESSAGES = int(os.getenv("WHATSAPP_MAX_CONCURRENT_MESSAGES", "16"))
RATE_LIMIT_PER_USER = int(os.getenv("WHATSAPP_RATE_LIMIT_PER_USER", "20"))
RATE_LIMIT_PER_RECEIVER = int(os.getenv("WHATSAPP_RATE_LIMIT_PER_RECEIVER", "100"))
RATE_LIMIT_GLOBAL = int(os.getenv("WHATSAPP_RATE_LIMIT_GLOBAL", "200"))
CONTROL_REPLY_DAILY_LIMIT = int(os.getenv("WHATSAPP_CONTROL_REPLY_DAILY_LIMIT", "200"))

_active_senders: set[str] = set()
_last_marker_cleanup = 0.0


def _scope(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


async def read_form(request: Request) -> FormData:
    """Bound the actual stream, including requests without Content-Length."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="Expected a form webhook")
    body = bytearray()
    try:
        async with asyncio.timeout(10):
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_WEBHOOK_BYTES:
                    raise HTTPException(status_code=413, detail="Webhook too large")
                body.extend(chunk)
        fields = parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=256)
    except TimeoutError:
        raise HTTPException(status_code=408, detail="Webhook body timeout") from None
    except (UnicodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid webhook form") from None
    if len({key for key, _ in fields}) != len(fields):
        raise HTTPException(status_code=400, detail="Duplicate webhook field")
    return FormData(fields)


async def _allow(scope: str, action: str, limit: int, minutes: int = 1) -> bool:
    # Reuse the shared Redis limiter and its bounded outage fallback. An
    # unresponsive limiter must not queue webhook tasks or allow paid work.
    try:
        async with asyncio.timeout(2):
            return await check_rate_limit(scope, action=action, limit=limit, window_minutes=minutes)
    except TimeoutError:
        return False


@asynccontextmanager
async def admit(envelope: VerifiedChannelEnvelope) -> AsyncIterator[bool]:
    """Authenticate first; bound work before routing, proof hashing or providers."""
    global _last_marker_cleanup
    receiver = _scope(envelope.provider_account_id, envelope.receiver_key)
    sender = _scope(receiver, envelope.provider_identity)
    if sender in _active_senders or len(_active_senders) >= MAX_CONCURRENT_MESSAGES:
        # Do not acknowledge an ordinary overlapping message as delivered.
        # The provider may retry the unclaimed event according to its policy.
        raise HTTPException(status_code=503, detail="WhatsApp is busy", headers={"Retry-After": "2"})
    # No await between checking and claiming the process-local execution slot.
    _active_senders.add(sender)
    try:
        if not await _allow(sender, "whatsapp_sender", RATE_LIMIT_PER_USER):
            yield False
            return
        if not await _allow(receiver, "whatsapp_receiver", RATE_LIMIT_PER_RECEIVER):
            yield False
            return
        if not await _allow("all", "whatsapp_global", RATE_LIMIT_GLOBAL):
            yield False
            return
        async with get_db_connection() as connection:
            now = time.monotonic()
            if now - _last_marker_cleanup >= 60:
                await connection.execute("DELETE FROM WHATSAPP_PROCESSED_MESSAGES WHERE created_at < datetime('now', '-1 day')")
                _last_marker_cleanup = now
            cursor = await connection.execute(
                "INSERT OR IGNORE INTO WHATSAPP_PROCESSED_MESSAGES(message_sid) VALUES (?)",
                (envelope.event_id,),
            )
            await connection.commit()
            claimed = cursor.rowcount == 1
        yield claimed
    finally:
        _active_senders.discard(sender)


async def allow_control_reply(data: Mapping[str, str], *, connected: bool = False) -> bool:
    """Reserve before sending; ambiguous delivery must not replenish the budget."""
    receiver = _scope(str(data.get("AccountSid") or ""), str(data.get("To") or ""))
    sender = _scope(receiver, str(data.get("From") or ""))
    # A failed-code notice must not suppress the later successful confirmation.
    action = "whatsapp_connected_reply" if connected else "whatsapp_control_reply"
    if not await _allow(sender, action, 1, 5):
        return False
    return await _allow(receiver, "whatsapp_control_reply_daily", CONTROL_REPLY_DAILY_LIMIT, 1440)
