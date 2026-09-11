"""Resolve the immutable Twilio provenance of an existing operation."""
from __future__ import annotations

import json
from typing import Any, Mapping


def twilio_snapshot(call: Mapping[str, Any]) -> dict[str, Any] | None:
    config = json.loads(call.get("config_snapshot_json") or "{}")
    value = config.get("_twilio")
    if value is None:
        return None
    if (not isinstance(value, dict) or not value.get("integration_id")
            or not value.get("account_sid")
            or value.get("billing_mode") != "creator_twilio"
            or type(value.get("credential_version")) is not int):
        raise ValueError("Invalid telephone account provenance")
    return value


async def account_for_call(call, *, allow_inactive=False, connection=None):
    snapshot = twilio_snapshot(call)
    if snapshot is None:
        return None
    from integrations.telephony.integrations import resolve_account
    account = await resolve_account(snapshot["integration_id"],
        allow_inactive=allow_inactive, connection=connection)
    if (account.account_sid != snapshot["account_sid"]
            or (not allow_inactive and account.version != snapshot["credential_version"])):
        from integrations.embed.models import EmbedError
        raise EmbedError('twilio_connection_changed', 403)
    return account


async def client_for_call(call, *, legacy_factory, allow_inactive=False, connection=None):
    from integrations.telephony.twilio_client import AsyncTwilioVoiceClient
    account = await account_for_call(call, allow_inactive=allow_inactive, connection=connection)
    return (AsyncTwilioVoiceClient(account.account_sid, account.auth_token)
            if account is not None else legacy_factory())


async def deleted_call_source(connection, *, token=None, call_sid=None):
    """The purge snapshot retains account attribution after private call erasure."""
    cursor = await connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='PHONE_CALL_TOMBSTONES'")
    if await cursor.fetchone() is None:
        return None
    cursor = await connection.execute('''SELECT j.source_snapshot_json
        FROM PHONE_CALL_TOMBSTONES t JOIN PHONE_DATA_PURGE_JOBS j ON j.id=t.purge_job_id
        WHERE (? IS NOT NULL AND t.dispatch_token=?)
           OR (? IS NULL AND ? IS NOT NULL AND t.provider_call_sid=?) LIMIT 1''',
        (token, token, token, call_sid, call_sid))
    row = await cursor.fetchone()
    return json.loads(row[0]) if row else None
