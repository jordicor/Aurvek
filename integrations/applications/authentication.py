"""Backend ingress and identity delegation limited to proven application links."""
from __future__ import annotations

import ipaddress
import secrets

from fastapi import Request

from integrations.embed.api import authenticate_backend_client
from integrations.embed.models import EmbedError
from integrations.embed.store import _digest


def authentication_column_statements(table, columns):
    if table not in {"EMBED_AUTH_CODES", "EMBED_DELEGATED_SESSIONS"}:
        raise ValueError("Unsupported authentication table")
    if not columns or "source_auth_time" in columns:
        return []
    return [f"ALTER TABLE {table} ADD COLUMN source_auth_time INTEGER NOT NULL DEFAULT 0"]


async def initialize_authentication_schema(connection):
    for table in ("EMBED_AUTH_CODES", "EMBED_DELEGATED_SESSIONS"):
        rows = await (await connection.execute(f"PRAGMA table_info({table})")).fetchall()
        for sql in authentication_column_statements(table, {row[1] for row in rows}):
            await connection.execute(sql)


async def application_backend_client(request: Request):
    config = await authenticate_backend_client(request)
    try:
        peer = ipaddress.ip_address(request.client.host if request.client else "")
    except ValueError:
        raise EmbedError("unauthenticated") from None
    # The public reverse-proxy location overwrites this marker, never forwards a
    # client supplied value. Direct remote connections must actually use TLS.
    public_ingress = request.headers.get("x-aurvek-backend-ingress") == "public"
    remote = public_ingress or not peer.is_loopback
    if remote:
        trusted_tls_proxy = peer.is_loopback and public_ingress and (
            request.headers.get("x-aurvek-backend-tls") == "https")
        if not config.remote_backend_access or not (request.url.scheme == "https" or trusted_tls_proxy):
            raise EmbedError("unauthenticated")
    # Only a matched route with proven client credentials and permitted ingress
    # can distinguish expected domain denials from anonymous scanner traffic.
    request.state.application_backend_authenticated = True
    return config


async def issue_account_session(store, app_id, external_user_id):
    """An authenticated app asserts its own login for an already proven link.

    No user ID/contact matching and no native step-up authentication are granted.
    The app backend must check its local login before calling this operation.
    """
    from .accounts import ApplicationAccountService
    accounts = ApplicationAccountService(store)
    credential = secrets.token_urlsafe(48)
    async with store.transaction() as connection:
        app, _ = await store._app(connection, app_id)
        binding = await accounts._binding(connection, app_id, external_user_id)
        user, member = await store._live(connection, app_id, binding["user_id"], binding["subject"])
        expires = store.now() + 8 * 3600
        await connection.execute("""INSERT INTO EMBED_DELEGATED_SESSIONS
            (session_id,credential_hash,app_id,app_version,subject,user_id,session_version,
             membership_version,source_session_hash,csrf_token,expires_at,revoked_at,created_at,source_auth_time)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?,0)""",
            (secrets.token_urlsafe(24), _digest(credential), app_id, app["version"], binding["subject"],
             binding["user_id"], user["session_version"], member["version"],
             _digest("application-login:" + secrets.token_urlsafe(32)), secrets.token_urlsafe(32),
             expires, store.now()))
    return {"app_id": app_id, "subject": binding["subject"], "expires_at": expires,
            "delegated_credential": credential}
