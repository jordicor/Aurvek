"""Native signup scoped from verified app context, only for newly created users.

Passwords, email verification and Google authentication remain native. The
wrapper is called exclusively in the native NEW-account branch; it cannot take
an existing account ID and never changes an existing user's global privileges.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from .identity import get_embed_store
from .models import EmbedError
from .store import _digest, _one


@dataclass(frozen=True)
class SignupContext:
    app_id: str
    prompt_id: int
    issuer: str


async def resolve_signup_context(request, *, transaction: str | None = None,
                                 next_url: str | None = None) -> SignupContext | None:
    """Reject invalid/expired embed admission rather than creating a broad user."""
    if next_url is not None:
        parsed = urlsplit(next_url)
        if not parsed.path.startswith("/embed/"):
            return None
        query = parse_qs(parsed.query)
        if (parsed.scheme or parsed.netloc or parsed.fragment or parsed.path != "/embed/authorize/complete"
                or set(query) != {"transaction"} or len(query["transaction"]) != 1):
            raise EmbedError("invalid_request", 400)
        transaction = query["transaction"][0]
    if transaction is None:
        return None
    if (not isinstance(transaction, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", transaction)
            or transaction not in request.session.get("embed_authorizations", [])):
        raise EmbedError("unauthenticated")
    config = await get_embed_store().authorization_app(transaction)
    if request.headers.get("host", "").lower() != urlsplit(config.issuer).netloc:
        raise EmbedError("unauthenticated")
    return SignupContext(config.app_id, config.prompt_id, config.issuer)


async def bind_email_registration(context: SignupContext, verification_token: str) -> None:
    """Bind the native 24-hour verification token before any email is sent."""
    store = get_embed_store()
    async with store.transaction() as connection:
        await connection.execute("""INSERT INTO EMBED_REGISTRATIONS VALUES (?,?,?,?,?,?)""",
            (_digest(verification_token), context.app_id, context.prompt_id, context.issuer, store.now() + 86400, store.now()))


async def email_signup_context(verification_token: str) -> SignupContext | None:
    store = get_embed_store()
    async with store.connection(readonly=True) as connection:
        try:
            row = await _one(connection, "SELECT * FROM EMBED_REGISTRATIONS WHERE verification_hash=?", (_digest(verification_token),))
        except sqlite3.OperationalError as error:
            if "no such table" in str(error):
                return None
            raise
    if not row:
        return None
    if row["expires_at"] <= store.now():
        raise EmbedError("unauthenticated")
    config = await store.get_app(row["app_id"])
    if config.prompt_id != row["prompt_id"] or config.issuer != row["issuer"]:
        raise EmbedError("unauthenticated")
    return SignupContext(config.app_id, config.prompt_id, config.issuer)


async def create_scoped_account(context: SignupContext, native_add_user, *,
                                username: str, email: str, llm_id: int,
                                authentication_mode: str,
                                initial_password: str | None = None,
                                ui_language: str | None = None) -> int:
    """Create through the native factory with fixed pilot defaults, no payer swap."""
    store = get_embed_store()
    config = await store.get_app(context.app_id)
    if config.prompt_id != context.prompt_id or config.issuer != context.issuer:
        raise EmbedError("unauthenticated")
    user_id = await native_add_user(
        username=username, email=email, prompt_id=context.prompt_id,
        all_prompts_access=False, public_prompts_access=False, llm_id=llm_id,
        allow_file_upload=False, allow_image_generation=False, balance=0.0,
        phone=None, role_name="customer", authentication_mode=authentication_mode,
        initial_password=initial_password, can_change_password=True,
        company_id=None, current_user=None, billing_account_id=None,
        initial_balance_funder_id=None,
        ui_language=ui_language,
    )
    if not user_id:
        raise EmbedError("service_unavailable", 503)
    # If a later step fails, this already-created account remains restricted and
    # without active membership. An operator can reconcile; never broaden access.
    await store.provision_membership(context.app_id, int(user_id), active=False)
    from marketplace.services.entitlements import grant_prompt_entitlement
    async with store.transaction() as connection:
        await grant_prompt_entitlement(connection, user_id=int(user_id), prompt_id=context.prompt_id,
            source="embed_registration", source_ref_type="embed_app", source_ref_id=context.app_id)
    return int(user_id)
