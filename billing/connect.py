import asyncio

import stripe
from fastapi.responses import JSONResponse, RedirectResponse

from common import STRIPE_SECRET_KEY, get_auth_base_url
from database import get_db_connection
from log_config import logger


async def create_connect_onboarding_response(request, current_user):
    if not STRIPE_SECRET_KEY:
        return JSONResponse(
            content={"success": False, "message": "Stripe not configured"},
            status_code=503,
        )

    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT stripe_connect_account_id FROM USER_DETAILS WHERE user_id = ?",
                (current_user.id,),
            )
            result = await cursor.fetchone()
            existing_account_id = result[0] if result else None

        if existing_account_id:
            account_id = existing_account_id
            logger.info("Using existing Connect account %s for user %s", account_id, current_user.id)
        else:
            account = await asyncio.to_thread(
                stripe.Account.create,
                type="express",
                email=current_user.email if getattr(current_user, "email", None) else None,
                metadata={"user_id": str(current_user.id)},
                capabilities={
                    "transfers": {"requested": True},
                },
            )
            account_id = account.id
            logger.info("Created new Connect account %s for user %s", account_id, current_user.id)

            async with get_db_connection() as conn:
                await conn.execute(
                    "UPDATE USER_DETAILS SET stripe_connect_account_id = ? WHERE user_id = ?",
                    (account_id, current_user.id),
                )
                await conn.commit()

        base_url = get_auth_base_url(request).rstrip("/")
        account_link = await asyncio.to_thread(
            stripe.AccountLink.create,
            account=account_id,
            refresh_url=f"{base_url}/api/connect/refresh",
            return_url=f"{base_url}/api/connect/return",
            type="account_onboarding",
        )

        return JSONResponse(content={"success": True, "url": account_link.url})

    except stripe.error.StripeError as exc:
        logger.error("Stripe Connect onboard error: %s", exc)
        return JSONResponse(content={"success": False, "message": str(exc)}, status_code=400)
    except Exception as exc:
        logger.error("Connect onboard error: %s", exc)
        return JSONResponse(
            content={"success": False, "message": "Error starting onboarding"},
            status_code=500,
        )


async def handle_connect_return_response(current_user):
    if not STRIPE_SECRET_KEY:
        return RedirectResponse(url="/my-earnings?error=stripe_not_configured", status_code=302)

    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT stripe_connect_account_id FROM USER_DETAILS WHERE user_id = ?",
                (current_user.id,),
            )
            result = await cursor.fetchone()
            account_id = result[0] if result else None

        if not account_id:
            return RedirectResponse(url="/my-earnings?error=no_account", status_code=302)

        account = await asyncio.to_thread(stripe.Account.retrieve, account_id)
        charges_enabled = 1 if account.charges_enabled else 0
        payouts_enabled = 1 if account.payouts_enabled else 0
        details_submitted = 1 if account.details_submitted else 0

        async with get_db_connection() as conn:
            await conn.execute(
                """
                UPDATE USER_DETAILS SET
                    stripe_connect_onboarding_complete = ?,
                    stripe_connect_charges_enabled = ?,
                    stripe_connect_payouts_enabled = ?
                WHERE user_id = ?
                """,
                (details_submitted, charges_enabled, payouts_enabled, current_user.id),
            )
            await conn.commit()

        logger.info(
            "Connect return for user %s: details_submitted=%s, payouts_enabled=%s",
            current_user.id,
            details_submitted,
            payouts_enabled,
        )

        if payouts_enabled:
            return RedirectResponse(url="/my-earnings?success=connected", status_code=302)
        if details_submitted:
            return RedirectResponse(url="/my-earnings?warning=pending_verification", status_code=302)
        return RedirectResponse(url="/my-earnings?warning=incomplete", status_code=302)

    except stripe.error.StripeError as exc:
        logger.error("Stripe Connect return error: %s", exc)
        return RedirectResponse(url="/my-earnings?error=stripe_error", status_code=302)
    except Exception as exc:
        logger.error("Connect return error: %s", exc)
        return RedirectResponse(url="/my-earnings?error=unknown", status_code=302)


async def get_connect_status_response(current_user):
    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT stripe_connect_account_id, stripe_connect_onboarding_complete,
                       stripe_connect_charges_enabled, stripe_connect_payouts_enabled
                FROM USER_DETAILS WHERE user_id = ?
                """,
                (current_user.id,),
            )
            result = await cursor.fetchone()

        if not result or not result[0]:
            return JSONResponse(
                content={
                    "connected": False,
                    "onboarding_complete": False,
                    "payouts_enabled": False,
                    "can_receive_payouts": False,
                }
            )

        account_id, onboarding_complete, charges_enabled, payouts_enabled = result

        if STRIPE_SECRET_KEY and onboarding_complete and not payouts_enabled:
            try:
                account = await asyncio.to_thread(stripe.Account.retrieve, account_id)
                payouts_enabled = 1 if account.payouts_enabled else 0
                charges_enabled = 1 if account.charges_enabled else 0

                if account.payouts_enabled:
                    async with get_db_connection() as conn:
                        await conn.execute(
                            """
                            UPDATE USER_DETAILS SET
                                stripe_connect_charges_enabled = ?,
                                stripe_connect_payouts_enabled = ?
                            WHERE user_id = ?
                            """,
                            (charges_enabled, payouts_enabled, current_user.id),
                        )
                        await conn.commit()
            except stripe.error.StripeError:
                pass

        return JSONResponse(
            content={
                "connected": True,
                "account_id": account_id[:8] + "..." if account_id else None,
                "onboarding_complete": bool(onboarding_complete),
                "charges_enabled": bool(charges_enabled),
                "payouts_enabled": bool(payouts_enabled),
                "can_receive_payouts": bool(payouts_enabled),
            }
        )

    except Exception as exc:
        logger.error("Connect status error: %s", exc)
        return JSONResponse(
            content={"success": False, "message": "Error checking status"},
            status_code=500,
        )


async def handle_account_updated(account) -> dict:
    account_id = account["id"]
    logger.info("Connect account updated: %s", account_id)

    charges_enabled = 1 if account.get("charges_enabled") else 0
    payouts_enabled = 1 if account.get("payouts_enabled") else 0
    details_submitted = 1 if account.get("details_submitted") else 0

    async with get_db_connection() as conn:
        await conn.execute(
            """
            UPDATE USER_DETAILS SET
                stripe_connect_onboarding_complete = ?,
                stripe_connect_charges_enabled = ?,
                stripe_connect_payouts_enabled = ?
            WHERE stripe_connect_account_id = ?
            """,
            (details_submitted, charges_enabled, payouts_enabled, account_id),
        )
        await conn.commit()

    logger.info(
        "Connect account %s: payouts_enabled=%s, charges_enabled=%s",
        account_id,
        payouts_enabled,
        charges_enabled,
    )
    return {"status": "success"}
