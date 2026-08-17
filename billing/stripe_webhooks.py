import asyncio

import stripe
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from billing.connect import handle_account_updated
from billing.creator_payouts import handle_transfer_failed
from billing.discounts import restore_discount_usage_for_expired_session
from billing.wallet import handle_wallet_chargeback, handle_wallet_checkout_completed
from common import STRIPE_WEBHOOK_SECRET
from log_config import logger
from marketplace.payments.checkout_webhooks import (
    MARKETPLACE_CHECKOUT_TYPES,
    handle_marketplace_chargeback,
    handle_marketplace_checkout_completed,
)


router = APIRouter()


@router.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Webhook not configured")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError as exc:
        logger.error("Stripe webhook: Invalid payload")
        raise HTTPException(status_code=400, detail="Invalid payload") from exc
    except stripe.error.SignatureVerificationError as exc:
        logger.error("Stripe webhook: Invalid signature")
        raise HTTPException(status_code=400, detail="Invalid signature") from exc

    event_type = event["type"]

    if event_type == "checkout.session.completed":
        response = await _handle_checkout_session_completed(event["data"]["object"])
        if isinstance(response, Response):
            return response
        return JSONResponse(content=response)

    if event_type == "checkout.session.expired":
        response = await restore_discount_usage_for_expired_session(event["data"]["object"])
        return JSONResponse(content=response)

    if event_type == "account.updated":
        await handle_account_updated(event["data"]["object"])

    elif event_type == "transfer.failed":
        await handle_transfer_failed(event["data"]["object"])

    elif event_type == "charge.dispute.created":
        response = await _handle_charge_dispute_created(event["data"]["object"])
        if isinstance(response, Response):
            return response
        if response:
            return JSONResponse(content=response)

    return JSONResponse(content={"status": "success"})


async def _handle_checkout_session_completed(session):
    metadata = session.get("metadata", {}) or {}
    checkout_type = metadata.get("type")

    if checkout_type in MARKETPLACE_CHECKOUT_TYPES:
        return await handle_marketplace_checkout_completed(session, metadata)

    return await handle_wallet_checkout_completed(session)


async def _handle_charge_dispute_created(dispute):
    payment_intent_id = dispute.get("payment_intent")
    logger.warning(
        "Chargeback dispute created: %s, payment_intent=%s",
        dispute.get("id"),
        payment_intent_id,
    )

    if not payment_intent_id:
        return {"status": "ignored", "reason": "missing_payment_intent"}

    try:
        sessions = await asyncio.to_thread(
            stripe.checkout.Session.list,
            payment_intent=payment_intent_id,
            limit=1,
        )
        if not sessions or not sessions.data:
            return {"status": "ignored", "reason": "checkout_session_not_found"}

        session = sessions.data[0]
        metadata = session.get("metadata", {}) or {}
        if metadata.get("type") in MARKETPLACE_CHECKOUT_TYPES:
            return await handle_marketplace_chargeback(session, metadata, payment_intent_id)

        return await handle_wallet_chargeback(session)

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error processing chargeback")
        raise HTTPException(status_code=500, detail="Error processing chargeback") from exc
