import asyncio
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from auth import get_current_user, hash_password
from auth_flows import generate_unique_username
from common import get_auth_base_url, templates
from database import get_db_connection
from email_service import email_service
from email_validation import validate_email_robust
from log_config import logger
from marketplace.config import marketplace_checkout_enabled, marketplace_public_landings_enabled
from marketplace.services.acquisition_context import handle_pack_for_existing_user
from marketplace.services.entitlements import (
    grant_prompt_entitlement,
    user_has_pack_access as user_has_pack_entitlement_access,
    user_has_prompt_access as user_has_prompt_entitlement_access,
)
from marketplace.services.pending_entitlements import send_entitlement_claim_email
from marketplace.services.pending_registrations import (
    cleanup_expired_registrations,
    create_pending_registration,
    get_user_by_email_record,
)
from rate_limiter import (
    RateLimitConfig as RLC,
    check_failure_limit,
    check_rate_limits,
    record_failure,
)


router = APIRouter()


@router.get("/claim-entitlement/{token}")
async def claim_entitlement(request: Request, token: str):
    """
    Claim a pack/prompt entitlement for an existing user.
    User clicks this link from email and must be logged in to complete.
    """
    current_user = await get_current_user(request)
    if not current_user:
        return RedirectResponse(f"/login?next=/claim-entitlement/{token}")

    async with get_db_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, user_id, prompt_id, pack_id, expires_at FROM PENDING_ENTITLEMENTS WHERE token = ?",
            (token,),
        )
        row = await cursor.fetchone()

    if not row:
        return templates.TemplateResponse(
            "verify_email.html",
            {
                "request": request,
                "success": False,
                "error": "This claim link is invalid or has already been used.",
            },
        )

    pending_id, target_user_id, prompt_id, pack_id, expires_at = row[0], row[1], row[2], row[3], row[4]

    if (prompt_id or pack_id) and (
        not marketplace_public_landings_enabled() or not marketplace_checkout_enabled()
    ):
        return templates.TemplateResponse(
            "verify_email.html",
            {
                "request": request,
                "success": False,
                "error": "This claim link is no longer available.",
            },
        )

    if datetime.fromisoformat(expires_at) < datetime.now():
        async with get_db_connection() as conn:
            await conn.execute("DELETE FROM PENDING_ENTITLEMENTS WHERE id = ?", (pending_id,))
            await conn.commit()
        return templates.TemplateResponse(
            "verify_email.html",
            {
                "request": request,
                "success": False,
                "error": "This claim link has expired. Please try registering again.",
            },
        )

    if current_user.id != target_user_id:
        return templates.TemplateResponse(
            "verify_email.html",
            {
                "request": request,
                "success": False,
                "error": "This claim link belongs to a different account. Please log in with the correct account.",
            },
        )

    redirect_url = "/chat"
    consume_pending = True
    if pack_id:
        try:
            pack_redirect = await handle_pack_for_existing_user(pack_id, current_user.id)
            if pack_redirect:
                redirect_url = pack_redirect
                consume_pending = False
        except Exception as grant_err:
            logger.error("Failed to grant pack claim entitlement: %s", grant_err)
            return templates.TemplateResponse(
                "verify_email.html",
                {
                    "request": request,
                    "success": False,
                    "error": "This claim could not be completed right now. Please try again.",
                },
            )
        if redirect_url == "/chat":
            async with get_db_connection(readonly=True) as conn:
                if not await user_has_pack_entitlement_access(conn, user_id=current_user.id, pack_id=pack_id):
                    logger.error("Pack claim did not grant access: user=%s pack=%s", current_user.id, pack_id)
                    return templates.TemplateResponse(
                        "verify_email.html",
                        {
                            "request": request,
                            "success": False,
                            "error": "This claim could not be completed right now. Please try again.",
                        },
                    )
    elif prompt_id:
        try:
            async with get_db_connection() as conn:
                prompt_cursor = await conn.execute(
                    "SELECT created_by_user_id, purchase_price FROM PROMPTS WHERE id = ?",
                    (prompt_id,),
                )
                prompt_row = await prompt_cursor.fetchone()
                if not prompt_row:
                    await conn.execute("DELETE FROM PENDING_ENTITLEMENTS WHERE id = ?", (pending_id,))
                    await conn.commit()
                    return templates.TemplateResponse(
                        "verify_email.html",
                        {
                            "request": request,
                            "success": False,
                            "error": "This claim link targets a prompt that is no longer available.",
                        },
                    )

                purchase_price = float(prompt_row[1]) if prompt_row[1] else 0.0
                if purchase_price > 0:
                    # Paid prompt: a claim link must never grant it for free.
                    if not await user_has_prompt_entitlement_access(
                        conn, user_id=current_user.id, prompt_id=prompt_id
                    ):
                        # User does not own it -> consume the token and route to purchase.
                        await conn.execute(
                            "DELETE FROM PENDING_ENTITLEMENTS WHERE id = ?", (pending_id,)
                        )
                        await conn.commit()
                        logger.info(
                            "Claim link for paid prompt %s routed to purchase flow: user=%s",
                            prompt_id,
                            current_user.id,
                        )
                        return RedirectResponse(f"/purchase/prompt/{prompt_id}")
                    # User already owns it (prior purchase or pack) -> just select it.
                    await conn.execute(
                        "UPDATE USER_DETAILS SET current_prompt_id = ? WHERE user_id = ?",
                        (prompt_id, current_user.id),
                    )
                    await conn.execute(
                        "DELETE FROM PENDING_ENTITLEMENTS WHERE id = ?", (pending_id,)
                    )
                    await conn.commit()
                else:
                    # Free prompt: grant the claim entitlement as before.
                    await grant_prompt_entitlement(
                        conn,
                        user_id=current_user.id,
                        prompt_id=prompt_id,
                        source="claim_link",
                        source_ref_type="pending_entitlement",
                        source_ref_id=pending_id,
                        metadata={"token_id": pending_id},
                        created_by_user_id=prompt_row[0],
                    )
                    await conn.execute(
                        "UPDATE USER_DETAILS SET current_prompt_id = ? WHERE user_id = ?",
                        (prompt_id, current_user.id),
                    )
                    await conn.execute(
                        "DELETE FROM PENDING_ENTITLEMENTS WHERE id = ?", (pending_id,)
                    )
                    await conn.commit()
        except Exception as e:
            logger.error(f"Error setting prompt for claim: {e}")
            raise

    if pack_id and consume_pending:
        async with get_db_connection() as conn:
            await conn.execute("DELETE FROM PENDING_ENTITLEMENTS WHERE id = ?", (pending_id,))
            await conn.commit()

    logger.info(f"Entitlement claimed: user={current_user.id}, pack={pack_id}, prompt={prompt_id}")
    return RedirectResponse(redirect_url)


@router.post("/api/register-pack")
async def register_pack_submit(request: Request):
    """
    Process registration via pack landing page.
    """
    await cleanup_expired_registrations()

    if not marketplace_public_landings_enabled() or not marketplace_checkout_enabled():
        return JSONResponse({"status": "error", "message": "Invalid pack"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "message": "Invalid request"}, status_code=400)

    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    password_confirm = body.get("password_confirm") or ""
    pack_id = body.get("pack_id")
    public_id = body.get("public_id")

    rate_error = check_rate_limits(
        request,
        ip_limit=RLC.REGISTER_BY_IP_ALL,
        identifier=email,
        identifier_limit=RLC.REGISTER_BY_EMAIL,
        action_name="register",
    )
    if rate_error:
        return JSONResponse(rate_error, status_code=429)

    fail_error = check_failure_limit(request, "register", RLC.REGISTER_BY_IP_FAILURES)
    if fail_error:
        return JSONResponse(fail_error, status_code=429)

    if not email or not password or not password_confirm:
        record_failure(request, "register", email)
        return JSONResponse({"status": "error", "message": "All fields are required"}, status_code=400)

    if not pack_id or not public_id:
        record_failure(request, "register", email)
        return JSONResponse({"status": "error", "message": "Invalid pack"}, status_code=400)

    if password != password_confirm:
        record_failure(request, "register", email)
        return JSONResponse({"status": "error", "message": "Passwords do not match"}, status_code=400)

    if len(password) < 8:
        record_failure(request, "register", email)
        return JSONResponse({"status": "error", "message": "Password must be at least 8 characters"}, status_code=400)

    is_valid_email, email_error = validate_email_robust(email)
    if not is_valid_email:
        record_failure(request, "register", email)
        return JSONResponse({"status": "error", "message": email_error}, status_code=400)

    existing_user = await get_user_by_email_record(email)
    if existing_user:
        logger.info(f"Pack registration attempt with existing email: {email}")
        if pack_id and marketplace_checkout_enabled():
            await send_entitlement_claim_email(
                request,
                email,
                existing_user["id"],
                prompt_id=None,
                pack_id=pack_id,
            )
        return JSONResponse(
            {
                "status": "success",
                "message": "If this email is not already registered, you will receive a verification email shortly.",
            }
        )

    first_prompt_id = None
    pack_owner_id = None
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT id, public_id, status, is_public, created_by_user_id, is_paid FROM PACKS WHERE id = ?",
            (pack_id,),
        )
        pack_row = await cursor.fetchone()

        if not pack_row:
            record_failure(request, "register", email)
            return JSONResponse({"status": "error", "message": "Invalid pack"}, status_code=400)
        if pack_row[1] != public_id:
            record_failure(request, "register", email)
            return JSONResponse({"status": "error", "message": "Invalid pack"}, status_code=400)
        if pack_row[2] != "published" or not pack_row[3]:
            record_failure(request, "register", email)
            return JSONResponse({"status": "error", "message": "Invalid pack"}, status_code=400)

        pack_owner_id = pack_row[4]

        cursor = await conn.execute(
            """SELECT prompt_id FROM PACK_ITEMS
               WHERE pack_id = ? AND is_active = 1
               AND (disable_at IS NULL OR disable_at > datetime('now'))
               ORDER BY display_order ASC LIMIT 1""",
            (pack_id,),
        )
        first_item = await cursor.fetchone()
        if first_item:
            first_prompt_id = first_item[0]

    if not first_prompt_id:
        return JSONResponse(
            {"status": "error", "message": "This pack is currently unavailable"},
            status_code=400,
        )

    username = await generate_unique_username(email)
    password_hash = hash_password(password)
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now() + timedelta(hours=24)

    success = await create_pending_registration(
        email=email,
        username=username,
        password_hash=password_hash,
        token=token,
        target_role="customer",
        prompt_id=first_prompt_id,
        expires_at=expires_at,
        pack_id=pack_id,
    )

    if not success:
        record_failure(request, "register", email)
        return JSONResponse({"status": "error", "message": "Registration failed. Please try again."}, status_code=500)

    verification_url = f"{get_auth_base_url(request).rstrip('/')}/verify-email/{token}"

    branding = None
    if pack_owner_id:
        from common import get_user_branding

        branding = await get_user_branding(pack_owner_id)

    email_sent = await asyncio.to_thread(
        email_service.send_verification_email,
        to_email=email,
        verification_url=verification_url,
        is_user=False,
        prompt_name=None,
        branding=branding,
    )
    if not email_sent:
        logger.error("Failed to send pack verification email to %s", email)

    logger.info(f"Pack registration pending for {email}, pack_id={pack_id}")

    return JSONResponse(
        {
            "status": "success",
            "message": "If this email is not already registered, you will receive a verification email shortly.",
        }
    )
