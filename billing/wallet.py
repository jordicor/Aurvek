import asyncio
import math
import secrets

import stripe
from fastapi import HTTPException

from billing.discounts import (
    DISCOUNT_SCOPE_WALLET,
    WALLET_REDEMPTION_PURPOSE,
    DiscountError,
    decrement_discount_usage,
    validate_wallet_credit_code,
)
from common import STRIPE_SECRET_KEY
from database import get_db_connection
from log_config import logger


MIN_WALLET_TOPUP = 5
MAX_WALLET_TOPUP = 500


def validate_wallet_amount(amount: float) -> float:
    amount = float(amount)
    if (
        not math.isfinite(amount)
        or amount < MIN_WALLET_TOPUP
        or amount > MAX_WALLET_TOPUP
    ):
        raise HTTPException(status_code=400, detail="Amount must be between $5 and $500")
    return amount


async def create_wallet_checkout(data: dict, base_url: str, user_id: int) -> dict:
    try:
        discount_code = (data.get("discount_code", "") or "").strip()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid request data: {exc}") from exc

    if discount_code:
        try:
            wallet_credit = await validate_wallet_credit_code(discount_code, user_id)
        except DiscountError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        return {
            "free_purchase": True,
            "grant_amount": wallet_credit.grant_amount,
            "message": "Wallet credit code validated",
        }

    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Stripe is not configured")

    try:
        original_amount = validate_wallet_amount(float(data.get("amount", 0)))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid request data: {exc}") from exc

    try:
        session = await asyncio.to_thread(
            stripe.checkout.Session.create,
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": int(original_amount * 100),
                        "product_data": {
                            "name": f"AURVEK Balance - ${original_amount:.2f}",
                            "description": (
                                f"Add ${original_amount:.2f} to your AURVEK account balance"
                            ),
                        },
                    },
                    "quantity": 1,
                }
            ],
            mode="payment",
            success_url=f"{base_url}/payment-success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}/payment?cancelled=true",
            metadata={
                "user_id": str(user_id),
                "original_amount": str(original_amount),
                "final_amount": str(original_amount),
                "discount_code": "",
                "discount_claimed": "0",
                "discount_claim_reference": "",
            },
        )
        return {"url": session.url}
    except stripe.error.StripeError as exc:
        logger.error("Stripe error creating checkout session: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Payment service error: {str(exc)}",
        ) from exc


async def credit_free_wallet_topup(
    *,
    user_id: int,
    discount_code: str,
    description_prefix: str,
    reference_prefix: str,
) -> dict:
    discount_code = (discount_code or "").strip()
    if not discount_code:
        raise HTTPException(status_code=400, detail="Discount code is required")

    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        try:
            await conn.execute("BEGIN IMMEDIATE")
            wallet_credit = await validate_wallet_credit_code(
                discount_code,
                user_id,
                conn=conn,
            )
            grant_amount = wallet_credit.grant_amount

            await cursor.execute(
                "SELECT balance FROM USER_DETAILS WHERE user_id = ?",
                (user_id,),
            )
            user_details = await cursor.fetchone()
            if not user_details:
                await conn.rollback()
                raise HTTPException(status_code=404, detail="User not found")

            balance_before = user_details[0]
            balance_after = balance_before + grant_amount
            reference_id = f"{reference_prefix}_{user_id}_{secrets.token_hex(8)}"

            await cursor.execute(
                """
                INSERT INTO DISCOUNT_REDEMPTIONS
                    (discount_code, user_id, purpose, grant_amount,
                     transaction_reference, redeemed_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    discount_code,
                    user_id,
                    WALLET_REDEMPTION_PURPOSE,
                    grant_amount,
                    reference_id,
                ),
            )

            await cursor.execute(
                "UPDATE USER_DETAILS SET balance = ? WHERE user_id = ?",
                (balance_after, user_id),
            )
            await cursor.execute(
                """
                INSERT INTO TRANSACTIONS
                (user_id, type, amount, balance_before, balance_after,
                 description, reference_id, discount_code)
                VALUES (?, 'payment', ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    grant_amount,
                    balance_before,
                    balance_after,
                    f"{description_prefix} - ${grant_amount:.2f} balance credited",
                    reference_id,
                    discount_code,
                ),
            )
            await decrement_discount_usage(
                conn,
                discount_code,
                scope=DISCOUNT_SCOPE_WALLET,
            )
            await conn.commit()
            return {
                "new_balance": balance_after,
                "reference_id": reference_id,
                "grant_amount": grant_amount,
            }
        except DiscountError as exc:
            await conn.rollback()
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        except HTTPException:
            await conn.rollback()
            raise
        except Exception as exc:
            await conn.rollback()
            logger.error("Error crediting free wallet top-up: %s", exc)
            raise HTTPException(status_code=500, detail="Database query error") from exc


async def handle_wallet_checkout_completed(session) -> dict:
    metadata = session.get("metadata", {}) or {}
    session_id = session.get("id")
    try:
        user_id = int(metadata.get("user_id") or 0)
        original_amount = float(metadata.get("original_amount"))
        final_amount = float(metadata.get("final_amount"))
    except (TypeError, ValueError):
        logger.warning("Malformed wallet checkout metadata for session=%s", session_id)
        return {"status": "ignored", "reason": "malformed_wallet_metadata"}

    if not session_id or user_id <= 0 or original_amount <= 0:
        logger.warning("Missing wallet checkout metadata for session=%s", session_id)
        return {"status": "ignored", "reason": "missing_wallet_metadata"}

    discount_code = metadata.get("discount_code", "")
    logger.info("Stripe payment completed: user_id=%s, amount=$%.2f", user_id, final_amount)

    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        try:
            await conn.execute("BEGIN IMMEDIATE")
            existing = await cursor.execute(
                "SELECT id FROM TRANSACTIONS WHERE reference_id = ?",
                (session_id,),
            )
            if await existing.fetchone():
                await conn.rollback()
                logger.info("Balance top-up already processed: session=%s", session_id)
                return {"status": "success"}

            await cursor.execute(
                "SELECT balance FROM USER_DETAILS WHERE user_id = ?",
                (user_id,),
            )
            result = await cursor.fetchone()
            balance_before = result[0] if result else 0
            balance_after = balance_before + original_amount

            await cursor.execute(
                "UPDATE USER_DETAILS SET balance = ? WHERE user_id = ?",
                (balance_after, user_id),
            )
            await cursor.execute(
                """
                INSERT INTO TRANSACTIONS
                (user_id, type, amount, balance_before, balance_after,
                 description, reference_id, discount_code)
                VALUES (?, 'payment', ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    original_amount,
                    balance_before,
                    balance_after,
                    f"Stripe payment - ${final_amount:.2f} paid for ${original_amount:.2f} balance",
                    session_id,
                    discount_code if discount_code else None,
                ),
            )
            await conn.commit()
            logger.info(
                "Balance updated for user %s: $%.2f -> $%.2f",
                user_id,
                balance_before,
                balance_after,
            )
            return {"status": "success"}
        except Exception as exc:
            await conn.rollback()
            logger.error("Error processing wallet Stripe webhook: %s", exc)
            raise HTTPException(status_code=500, detail="Error processing payment") from exc


async def get_payment_success_model(session_id: str, user_id: int) -> dict | None:
    if not STRIPE_SECRET_KEY:
        return None
    try:
        session = await asyncio.to_thread(stripe.checkout.Session.retrieve, session_id)
        if not session or session.metadata.get("user_id") != str(user_id):
            return None

        payment_amount = float(session.metadata.get("original_amount", 0))
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT balance FROM USER_DETAILS WHERE user_id = ?",
                (user_id,),
            )
            result = await cursor.fetchone()
            new_balance = result[0] if result else 0

        return {"new_balance": new_balance, "payment_amount": payment_amount}
    except Exception as exc:
        logger.error("Error retrieving Stripe session: %s", exc)
        return None


async def handle_wallet_chargeback(session) -> dict:
    metadata = session.get("metadata", {}) or {}
    session_id = session.get("id")
    try:
        user_id = int(metadata.get("user_id", 0))
    except (TypeError, ValueError):
        user_id = 0
    if not user_id:
        logger.warning(
            "Balance chargeback skipped: no user_id in session metadata for session=%s",
            session_id,
        )
        return {"status": "missing_user_id"}

    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            check_cursor = await conn.execute(
                "SELECT id FROM TRANSACTIONS WHERE reference_id = ? AND type = 'chargeback_reversal'",
                (session_id,),
            )
            if await check_cursor.fetchone():
                await conn.rollback()
                logger.info("Balance chargeback already processed for session=%s", session_id)
                return {"status": "already_processed"}

            txn_cursor = await conn.execute(
                "SELECT amount FROM TRANSACTIONS WHERE reference_id = ? AND type = 'payment'",
                (session_id,),
            )
            txn_row = await txn_cursor.fetchone()
            if not txn_row:
                await conn.rollback()
                logger.warning("No original payment transaction found for session=%s", session_id)
                raise HTTPException(
                    status_code=409,
                    detail="Original payment transaction has not been processed yet",
                )

            topup_amount = txn_row[0]
            bal_cursor = await conn.execute(
                "SELECT balance FROM USER_DETAILS WHERE user_id = ?",
                (user_id,),
            )
            bal_row = await bal_cursor.fetchone()
            balance_before = bal_row[0] if bal_row else 0
            balance_after = max(0, balance_before - topup_amount)

            await conn.execute(
                "UPDATE USER_DETAILS SET balance = MAX(0, balance - ?) WHERE user_id = ?",
                (topup_amount, user_id),
            )
            await conn.execute(
                """
                INSERT INTO TRANSACTIONS
                (user_id, type, amount, balance_before, balance_after,
                 description, reference_id)
                VALUES (?, 'chargeback_reversal', ?, ?, ?, 'Chargeback reversal for payment', ?)
                """,
                (user_id, topup_amount, balance_before, balance_after, session_id),
            )

            if balance_after == 0 and topup_amount > 0:
                await conn.execute(
                    "UPDATE USERS SET is_enabled = 0 WHERE id = ?",
                    (user_id,),
                )
                logger.warning(
                    "User %s disabled after balance chargeback (balance zeroed)",
                    user_id,
                )

            await conn.commit()
            logger.warning(
                "Balance chargeback processed: user=%s, amount=$%.2f, balance $%.2f -> $%.2f",
                user_id,
                topup_amount,
                balance_before,
                balance_after,
            )
            return {"status": "success"}
        except Exception:
            await conn.rollback()
            raise
