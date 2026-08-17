"""Marketplace checkout routes for prompt and pack purchases."""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from auth import get_current_user
from billing.discounts import (
    DiscountError,
    claim_discount_usage_for_checkout,
    decrement_discount_usage,
    restore_discount_usage_for_checkout,
    validate_discount_code,
)
from common import (
    AVATAR_TOKEN_EXPIRE_HOURS,
    CLOUDFLARE_BASE_URL,
    STRIPE_SECRET_KEY,
    get_template_context,
    slugify,
    templates,
    upsert_creator_relationship,
)
from database import get_db_connection
from log_config import logger
from marketplace.config import require_checkout_enabled
from marketplace.services.acquisition import apply_landing_config_to_user
from marketplace.services.entitlements import (
    grant_prompt_entitlement,
    user_has_prompt_access as user_has_prompt_entitlement_access,
)
from mobile.client import ios_purchase_blocked, ios_purchase_disabled_response
from models import User
from save_images import generate_img_token


router = APIRouter()


@router.get("/purchase/prompt/{prompt_id}", response_class=HTMLResponse)
async def prompt_purchase_bridge(
    request: Request,
    prompt_id: int,
    current_user: User = Depends(get_current_user),
):
    """Move an isolated landing visitor onto the trusted checkout origin."""
    require_checkout_enabled()
    purchase_path = f"/purchase/prompt/{int(prompt_id)}"
    if current_user is None:
        return RedirectResponse(
            url="/login?" + urlencode({"next": purchase_path}),
            status_code=302,
        )

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """
            SELECT name, purchase_price
            FROM PROMPTS
            WHERE id = ? AND public = 1
            """,
            (prompt_id,),
        )
        prompt = await cursor.fetchone()
        if not prompt or not prompt[1] or float(prompt[1]) <= 0:
            raise HTTPException(status_code=404, detail="Prompt not available for purchase")
        if await user_has_prompt_entitlement_access(
            conn,
            user_id=current_user.id,
            prompt_id=prompt_id,
        ):
            return RedirectResponse(url="/chat", status_code=302)

    context = await get_template_context(request, current_user)
    context.update(
        {
            "prompt_id": int(prompt_id),
            "prompt_name": prompt[0],
            "purchase_price": float(prompt[1]),
        }
    )
    return templates.TemplateResponse("prompt_purchase_bridge.html", context)


@router.get("/pack-purchase-success", response_class=HTMLResponse)
async def pack_purchase_success_page(
    request: Request,
    session_id: str = None,
    current_user: dict = Depends(get_current_user)
):
    """Success page shown after completing a pack purchase via Stripe."""
    require_checkout_enabled()

    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    pack_name = "Pack"
    pack_id = None
    pack_cover_image = None
    pack_creator = None
    pack_landing_url = None
    prompt_count = 0
    payment_amount = None

    if session_id and STRIPE_SECRET_KEY:
        try:
            session = await asyncio.to_thread(
                stripe.checkout.Session.retrieve,
                session_id,
            )
            if session and session.metadata.get('user_id', session.metadata.get('buyer_user_id')) == str(current_user.id):
                payment_amount = float(session.metadata.get('final_amount', 0))
                pid = session.metadata.get('pack_id')
                if pid:
                    pack_id = int(pid)
                    async with get_db_connection(readonly=True) as conn:
                        cursor = await conn.cursor()
                        await cursor.execute(
                            """SELECT p.name, p.slug, p.public_id, p.cover_image,
                                      u.username,
                                      (SELECT COUNT(*) FROM PACK_ITEMS pi
                                       WHERE pi.pack_id = p.id AND pi.is_active = 1
                                       AND (pi.disable_at IS NULL OR pi.disable_at > datetime('now')))
                               FROM PACKS p
                               JOIN USERS u ON p.created_by_user_id = u.id
                               WHERE p.id = ?""",
                            (pack_id,)
                        )
                        pack_row = await cursor.fetchone()
                        if pack_row:
                            pack_name = pack_row[0]
                            pack_creator = pack_row[4]
                            prompt_count = pack_row[5]
                            pack_cover_image = pack_row[3]
                            pack_landing_url = f"/pack/{pack_row[2]}/{pack_row[1]}/"
        except Exception as e:
            logger.error(f"Error retrieving pack purchase session: {e}")

    context = await get_template_context(request, current_user)
    context.update({
        "pack_name": pack_name,
        "pack_id": pack_id,
        "pack_cover_image": pack_cover_image,
        "pack_creator": pack_creator,
        "pack_landing_url": pack_landing_url,
        "prompt_count": prompt_count,
        "payment_amount": payment_amount,
    })
    return templates.TemplateResponse("pack_purchase_success.html", context)


# ---- Individual prompt purchase endpoint ----
@router.post("/api/prompts/{prompt_id}/purchase")
async def api_purchase_prompt(prompt_id: int, request: Request, current_user: User = Depends(get_current_user)):
    """Create a Stripe Checkout Session to purchase an individual prompt."""
    require_checkout_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if ios_purchase_blocked(request):
        return ios_purchase_disabled_response()

    try:
        body = await request.json()
    except Exception:
        body = {}

    discount_code = str(body.get("discount_code", "")).strip() if body.get("discount_code") else ""

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        # Fetch prompt details
        await cursor.execute(
            "SELECT name, public, purchase_price, created_by_user_id, public_id, description, landing_registration_config FROM PROMPTS WHERE id = ?",
            (prompt_id,)
        )
        prompt_row = await cursor.fetchone()
        if not prompt_row:
            raise HTTPException(status_code=404, detail="Prompt not found")

        prompt_name, is_public, purchase_price, creator_user_id, public_id, prompt_description, landing_reg_config = prompt_row

        if not is_public:
            raise HTTPException(status_code=404, detail="Prompt not found")

        if purchase_price is None or purchase_price == 0:
            raise HTTPException(status_code=400, detail="This prompt is not available for individual purchase")

        # Self-purchase prevention
        if creator_user_id == current_user.id:
            raise HTTPException(status_code=400, detail="You cannot purchase your own prompt")

        if await user_has_prompt_entitlement_access(cursor, user_id=current_user.id, prompt_id=prompt_id):
            return JSONResponse({"message": "You already have access to this prompt", "redirect": "/chat"})

    original_price = float(purchase_price)
    final_amount = original_price
    discount_value = 0

    # Validate and apply discount code
    if discount_code:
        try:
            discount = await validate_discount_code(discount_code, original_price)
        except DiscountError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        discount_value = discount.discount_value
        final_amount = discount.final_amount

    # Reject amounts between $0.01-$0.49 (below Stripe minimum)
    if 0 < final_amount < 0.50:
        raise HTTPException(
            status_code=400,
            detail=f"Final price after discount (${final_amount:.2f}) is below the minimum processing amount ($0.50). The discount must either cover the full price or leave at least $0.50."
        )

    # Generate slug for cancel URL
    prompt_slug = slugify(prompt_name) if prompt_name else ""

    # 100% discount: immediate grant without Stripe
    if final_amount == 0:
        async with get_db_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                if discount_code:
                    try:
                        discount = await validate_discount_code(discount_code, original_price, conn=conn)
                    except DiscountError as exc:
                        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
                    if discount.final_amount != 0:
                        raise HTTPException(status_code=400, detail="Discount does not fully cover this payment")

                # Record purchase
                purchase_cursor = await conn.execute(
                    """INSERT INTO PROMPT_PURCHASES
                       (buyer_user_id, prompt_id, amount, currency, payment_method, payment_reference, status)
                       VALUES (?, ?, 0.0, 'USD', 'free', ?, 'completed')""",
                    (current_user.id, prompt_id, f"discount_{discount_code or 'free'}_user_{current_user.id}_prompt_{prompt_id}_{int(datetime.now(timezone.utc).timestamp())}")
                )

                await grant_prompt_entitlement(
                    conn,
                    user_id=current_user.id,
                    prompt_id=prompt_id,
                    source="discount_claim",
                    source_ref_type="prompt_purchase",
                    source_ref_id=purchase_cursor.lastrowid,
                    metadata={
                        "payment_method": "free",
                        "discount_code": discount_code or None,
                        "amount": 0,
                    },
                    created_by_user_id=creator_user_id,
                )

                # Set current_prompt_id
                await conn.execute(
                    "UPDATE USER_DETAILS SET current_prompt_id = ? WHERE user_id = ?",
                    (prompt_id, current_user.id)
                )

                # Record transaction
                bal_cur = await conn.execute(
                    "SELECT balance FROM USER_DETAILS WHERE user_id = ?",
                    (current_user.id,)
                )
                bal_row = await bal_cur.fetchone()
                cur_balance = bal_row[0] if bal_row else 0
                await conn.execute('''
                    INSERT INTO TRANSACTIONS
                    (user_id, type, amount, balance_before, balance_after,
                     description, reference_id, discount_code)
                    VALUES (?, 'prompt_purchase', 0, ?, ?, ?, ?, ?)
                ''', (
                    current_user.id,
                    cur_balance,
                    cur_balance,
                    f'Free prompt purchase (100% discount): prompt_id={prompt_id}',
                    f'discount_{discount_code}_user_{current_user.id}',
                    discount_code if discount_code else None
                ))

                await decrement_discount_usage(conn, discount_code)

                if creator_user_id:
                    try:
                        await upsert_creator_relationship(
                            conn,
                            current_user.id,
                            creator_user_id,
                            "purchased_from",
                            "prompt",
                            prompt_id,
                        )
                    except Exception as ucr_err:
                        logger.warning("Could not record creator relationship for free prompt purchase: %s", ucr_err)

                # Apply landing_registration_config (same as paid purchases)
                if landing_reg_config:
                    try:
                        import json as _json
                        lrc = _json.loads(landing_reg_config) if isinstance(landing_reg_config, str) else landing_reg_config
                        await apply_landing_config_to_user(
                            conn, lrc, current_user.id,
                            creator_user_id=creator_user_id,
                            discount_pct=discount_value)
                    except Exception as lrc_err:
                        logger.warning(f"Failed to apply landing config for free purchase: {lrc_err}")

                await conn.commit()
            except Exception:
                await conn.execute("ROLLBACK")
                raise

        logger.info(f"Free prompt purchase (100% discount): user={current_user.id}, prompt={prompt_id}, code={discount_code}")
        return JSONResponse({
            "message": "Prompt access granted with discount",
            "redirect": "/chat",
            "free_purchase": True
        })

    # Stripe checkout for paid purchases
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Payment service is not configured")

    base_url = str(request.base_url).rstrip('/')
    discount_claimed = False
    discount_claim_reference = None
    try:
        if discount_code:
            discount_claim_reference = f"discount-claim-{secrets.token_hex(16)}"
            try:
                discount = await claim_discount_usage_for_checkout(discount_code, original_price)
            except DiscountError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
            discount_claimed = True
            discount_value = discount.discount_value
            final_amount = discount.final_amount
            if final_amount == 0:
                raise HTTPException(
                    status_code=400,
                    detail="Discount now fully covers this purchase. Please retry to claim it without Stripe.",
                )
            if 0 < final_amount < 0.50:
                raise HTTPException(
                    status_code=400,
                    detail=f"Final price after discount (${final_amount:.2f}) is below the minimum processing amount ($0.50). The discount must either cover the full price or leave at least $0.50."
                )

        session = await asyncio.to_thread(
            stripe.checkout.Session.create,
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'unit_amount': int(final_amount * 100),
                    'product_data': {
                        'name': prompt_name or "AI Prompt",
                        'description': ((prompt_description or "AI prompt")[:500]),
                    },
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=f"{base_url}/prompt-purchase-success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}/p/{public_id}/{prompt_slug}/?cancelled=true",
            metadata={
                'type': 'prompt_purchase',
                'prompt_id': str(prompt_id),
                'buyer_user_id': str(current_user.id),
                'original_price': str(original_price),
                'final_amount': str(final_amount),
                'discount_code': discount_code,
                'discount_value': str(discount_value),
                'discount_claimed': '1' if discount_claimed else '0',
                'discount_claim_reference': discount_claim_reference or '',
            }
        )
        return JSONResponse({"checkout_url": session.url})
    except HTTPException:
        if discount_claimed:
            await restore_discount_usage_for_checkout(
                discount_code,
                reference_id=discount_claim_reference,
                user_id=current_user.id,
            )
        raise
    except Exception as e:
        if discount_claimed:
            await restore_discount_usage_for_checkout(
                discount_code,
                reference_id=discount_claim_reference,
                user_id=current_user.id,
            )
        logger.error(f"Stripe session creation failed for prompt purchase: {e}")
        raise HTTPException(status_code=500, detail="Payment processing error")


@router.get("/prompt-purchase-success", response_class=HTMLResponse)
async def prompt_purchase_success_page(
    request: Request,
    session_id: str = None,
    current_user: dict = Depends(get_current_user)
):
    """Success page shown after completing a prompt purchase via Stripe."""
    require_checkout_enabled()

    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    prompt_name = "Prompt"
    prompt_id = None
    prompt_image_url = None
    prompt_creator = None
    prompt_landing_url = None
    payment_amount = None

    if session_id and STRIPE_SECRET_KEY:
        try:
            session = await asyncio.to_thread(
                stripe.checkout.Session.retrieve,
                session_id,
            )
            if session and session.metadata.get('buyer_user_id') == str(current_user.id):
                payment_amount = float(session.metadata.get('final_amount', 0))
                pid = session.metadata.get('prompt_id')
                if pid:
                    prompt_id = int(pid)
                    async with get_db_connection(readonly=True) as conn:
                        cursor = await conn.cursor()
                        await cursor.execute(
                            """SELECT p.name, p.public_id, p.image,
                                      u.username
                               FROM PROMPTS p
                               JOIN USERS u ON p.created_by_user_id = u.id
                               WHERE p.id = ?""",
                            (prompt_id,)
                        )
                        row = await cursor.fetchone()
                        if row:
                            prompt_name = row[0]
                            prompt_creator = row[3]
                            slug = slugify(row[0]) if row[0] else ""
                            prompt_landing_url = f"/p/{row[1]}/{slug}/" if row[1] else None
                            if row[2]:  # image
                                current_time = datetime.now(timezone.utc)
                                new_expiration = current_time + timedelta(hours=AVATAR_TOKEN_EXPIRE_HOURS)
                                img_base = f"{row[2]}_128.webp"
                                token = generate_img_token(img_base, new_expiration, current_user)
                                prompt_image_url = f"{CLOUDFLARE_BASE_URL}{img_base}?token={token}"
        except Exception as e:
            logger.error(f"Error retrieving prompt purchase session: {e}")

    context = await get_template_context(request, current_user)
    context.update({
        "prompt_name": prompt_name,
        "prompt_id": prompt_id,
        "prompt_image_url": prompt_image_url,
        "prompt_creator": prompt_creator,
        "prompt_landing_url": prompt_landing_url,
        "payment_amount": payment_amount,
    })
    return templates.TemplateResponse("prompt_purchase_success.html", context)
