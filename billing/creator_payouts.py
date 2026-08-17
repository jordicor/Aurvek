import asyncio
import secrets

import stripe
from fastapi.responses import JSONResponse

from common import STRIPE_SECRET_KEY
from database import get_db_connection
from log_config import logger


MIN_CREATOR_PAYOUT_USD = 50
PAYOUT_IDEMPOTENCY_RETRY_WINDOW_SECONDS = 20 * 60 * 60


def _is_ambiguous_stripe_error(exc: stripe.error.StripeError) -> bool:
    return isinstance(
        exc,
        (
            stripe.error.APIConnectionError,
            stripe.error.APIError,
            stripe.error.IdempotencyError,
            stripe.error.RateLimitError,
        ),
    )


async def _complete_reserved_payout(
    *,
    current_user,
    pending: float,
    connect_account_id: str,
    payout_tx_id: int,
    idempotency_key: str,
) -> JSONResponse:
    async with get_db_connection() as processing_conn:
        await processing_conn.execute("BEGIN IMMEDIATE")
        processing_cursor = await processing_conn.execute(
            """
            UPDATE TRANSACTIONS
            SET type = 'payout_processing',
                description = 'Creator earnings payout processing via Stripe'
            WHERE id = ? AND type = 'payout_pending'
            """,
            (payout_tx_id,),
        )
        if processing_cursor.rowcount == 0:
            await processing_conn.rollback()
            return JSONResponse(
                content={
                    "success": False,
                    "message": "A payout is already being processed. Please wait for it to complete.",
                },
                status_code=409,
            )
        await processing_conn.commit()

    try:
        amount_cents = int(pending * 100)
        transfer = await asyncio.to_thread(
            stripe.Transfer.create,
            amount=amount_cents,
            currency="usd",
            destination=connect_account_id,
            description=f"Creator earnings payout for user {current_user.id}",
            metadata={
                "user_id": str(current_user.id),
                "payout_type": "creator_earnings",
                "payout_reference": idempotency_key,
            },
            idempotency_key=idempotency_key,
        )

        async with get_db_connection() as update_conn:
            await update_conn.execute("BEGIN IMMEDIATE")
            await update_conn.execute(
                """
                UPDATE TRANSACTIONS
                SET type = 'payout_completed',
                    description = 'Creator earnings payout via Stripe',
                    reference_id = ?
                WHERE id = ? AND type = 'payout_processing'
                """,
                (transfer.id, payout_tx_id),
            )
            await update_conn.commit()

        logger.info(
            "Payout completed for user %s: $%.2f, transfer_id=%s",
            current_user.id,
            pending,
            transfer.id,
        )

        return JSONResponse(
            content={
                "success": True,
                "message": (
                    f"Payout of ${pending:.2f} has been sent to your bank account. "
                    "It may take 2-3 business days to arrive."
                ),
                "amount": pending,
                "transfer_id": transfer.id,
            }
        )

    except stripe.error.StripeError as exc:
        logger.error("Stripe Transfer error for user %s: %s", current_user.id, exc)

        if _is_ambiguous_stripe_error(exc):
            async with get_db_connection() as retry_conn:
                await retry_conn.execute("BEGIN IMMEDIATE")
                await retry_conn.execute(
                    """
                    UPDATE TRANSACTIONS
                    SET type = 'payout_pending',
                        description = 'Creator earnings payout awaiting retry after ambiguous Stripe response'
                    WHERE id = ? AND type = 'payout_processing'
                    """,
                    (payout_tx_id,),
                )
                await retry_conn.commit()
            return JSONResponse(
                content={
                    "success": False,
                    "message": (
                        "Payout status is still being confirmed. "
                        "Please wait before trying again."
                    ),
                },
                status_code=503,
            )

        async with get_db_connection() as fail_conn:
            await fail_conn.execute("BEGIN IMMEDIATE")
            update_cursor = await fail_conn.execute(
                """
                UPDATE TRANSACTIONS
                SET type = 'payout_failed',
                    description = ?
                WHERE id = ? AND type = 'payout_processing'
                """,
                (f"Payout failed: {str(exc)}", payout_tx_id),
            )
            if update_cursor.rowcount == 0:
                await fail_conn.rollback()
                return JSONResponse(
                    content={
                        "success": False,
                        "message": "Payout attempt was already resolved. Please refresh and try again.",
                    },
                    status_code=409,
                )
            await fail_conn.execute(
                "UPDATE USER_DETAILS SET pending_earnings = pending_earnings + ? WHERE user_id = ?",
                (pending, current_user.id),
            )
            await fail_conn.commit()

        return JSONResponse(
            content={
                "success": False,
                "message": f"Payout failed: {str(exc)}. Your pending earnings have been preserved.",
            },
            status_code=400,
        )


async def request_creator_payout_response(current_user):
    if not STRIPE_SECRET_KEY:
        return JSONResponse(
            content={"success": False, "message": "Payment system not configured"},
            status_code=503,
        )

    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            pending_tx_cursor = await conn.execute(
                """
                SELECT id, amount, reference_id, type,
                       strftime('%s', 'now') - strftime('%s', created_at) AS age_seconds
                FROM TRANSACTIONS
                WHERE user_id = ? AND type IN ('payout_pending', 'payout_processing')
                ORDER BY id DESC
                LIMIT 1
                """,
                (current_user.id,),
            )
            pending_tx = await pending_tx_cursor.fetchone()
            if pending_tx:
                pending_type = pending_tx[3]
                pending_age = pending_tx[4]
                if pending_age is None or pending_age > PAYOUT_IDEMPOTENCY_RETRY_WINDOW_SECONDS:
                    await conn.rollback()
                    return JSONResponse(
                        content={
                            "success": False,
                            "message": (
                                "A previous payout attempt needs manual review before "
                                "another payout can be requested."
                            ),
                        },
                        status_code=409,
                    )

                if pending_type == "payout_processing":
                    await conn.rollback()
                    return JSONResponse(
                        content={
                            "success": False,
                            "message": "A payout is already being processed. Please wait for it to complete.",
                        },
                        status_code=409,
                    )

                connect_cursor = await conn.execute(
                    """
                    SELECT stripe_connect_account_id, stripe_connect_payouts_enabled
                    FROM USER_DETAILS WHERE user_id = ?
                    """,
                    (current_user.id,),
                )
                connect_result = await connect_cursor.fetchone()
                await conn.rollback()
                if not connect_result or not connect_result[0] or not bool(connect_result[1]):
                    return JSONResponse(
                        content={
                            "success": False,
                            "message": "Your payout is pending, but your bank account setup is not currently complete.",
                        },
                        status_code=409,
                    )
                return await _complete_reserved_payout(
                    current_user=current_user,
                    pending=float(pending_tx[1] or 0),
                    connect_account_id=connect_result[0],
                    payout_tx_id=pending_tx[0],
                    idempotency_key=pending_tx[2],
                )

            cursor = await conn.execute(
                """
                SELECT pending_earnings, stripe_connect_account_id, stripe_connect_payouts_enabled
                FROM USER_DETAILS WHERE user_id = ?
                """,
                (current_user.id,),
            )
            result = await cursor.fetchone()

            if not result:
                await conn.rollback()
                return JSONResponse(
                    content={"success": False, "message": "User details not found"},
                    status_code=400,
                )

            pending = float(result[0] or 0)
            connect_account_id = result[1]
            payouts_enabled = bool(result[2])

            if pending < MIN_CREATOR_PAYOUT_USD:
                await conn.rollback()
                return JSONResponse(
                    content={
                        "success": False,
                        "message": f"Minimum withdrawal is $50. You have ${pending:.2f} pending.",
                    },
                    status_code=400,
                )

            if not connect_account_id:
                await conn.rollback()
                return JSONResponse(
                    content={
                        "success": False,
                        "message": "Please connect your bank account first to receive payouts.",
                    },
                    status_code=400,
                )

            if not payouts_enabled:
                await conn.rollback()
                return JSONResponse(
                    content={
                        "success": False,
                        "message": "Your bank account setup is not complete. Please finish onboarding in Stripe.",
                    },
                    status_code=400,
                )

            idempotency_key = f"creator-payout-{current_user.id}-{secrets.token_hex(16)}"
            payout_cursor = await conn.execute(
                """
                INSERT INTO TRANSACTIONS
                (user_id, type, amount, balance_before, balance_after,
                 description, reference_id, created_at)
                VALUES (?, 'payout_pending', ?, 0, 0, 'Creator earnings payout requested', ?, datetime('now'))
                """,
                (current_user.id, pending, idempotency_key),
            )
            payout_tx_id = payout_cursor.lastrowid
            await conn.execute(
                "UPDATE USER_DETAILS SET pending_earnings = 0 WHERE user_id = ?",
                (current_user.id,),
            )
            await conn.commit()

            return await _complete_reserved_payout(
                current_user=current_user,
                pending=pending,
                connect_account_id=connect_account_id,
                payout_tx_id=payout_tx_id,
                idempotency_key=idempotency_key,
            )

        except Exception:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise


async def handle_transfer_failed(transfer) -> dict:
    transfer_id = transfer["id"]
    amount = transfer.get("amount", 0) / 100
    destination = transfer.get("destination")

    logger.warning("Transfer failed: %s, amount=$%.2f, destination=%s", transfer_id, amount, destination)

    if not destination:
        return {"status": "ignored", "reason": "missing_destination"}

    metadata = transfer.get("metadata", {}) or {}
    payout_reference = metadata.get("payout_reference")
    references = [transfer_id]
    if payout_reference:
        references.append(payout_reference)
    placeholders = ", ".join("?" for _ in references)

    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        txn_cursor = await conn.execute(
            f"""
            SELECT id, user_id, amount, type
            FROM TRANSACTIONS
            WHERE reference_id IN ({placeholders})
            ORDER BY id DESC
            LIMIT 1
            """,
            references,
        )
        txn = await txn_cursor.fetchone()

        if not txn:
            await conn.rollback()
            logger.warning("Transfer failure ignored because payout transaction was not found: %s", transfer_id)
            return {"status": "ignored", "reason": "unknown_destination"}

        transaction_id = txn[0]
        user_id = txn[1]
        restored_amount = float(txn[2] or amount)
        if txn[3] not in {"payout_completed", "payout_pending", "payout_processing"}:
            await conn.rollback()
            logger.info("Transfer failure already processed: %s", transfer_id)
            return {"status": "already_processed"}

        await conn.execute(
            "UPDATE USER_DETAILS SET pending_earnings = pending_earnings + ? WHERE user_id = ?",
            (restored_amount, user_id),
        )
        await conn.execute(
            """
            UPDATE TRANSACTIONS SET type = 'payout_failed',
                                    description = 'Payout failed - amount restored',
                                    reference_id = ?
            WHERE id = ? AND type IN ('payout_completed', 'payout_pending', 'payout_processing')
            """,
            (transfer_id, transaction_id),
        )
        await conn.commit()

    logger.info(
        "Restored $%.2f to user %s pending earnings after failed transfer",
        restored_amount,
        user_id,
    )
    return {"status": "success"}
