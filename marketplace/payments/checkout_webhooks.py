"""Marketplace product fulfillment for Stripe Checkout webhooks."""

from __future__ import annotations

import json
import math

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from common import get_pricing_config, record_creator_earnings, upsert_creator_relationship
from database import get_db_connection
from log_config import logger
from marketplace.config import marketplace_checkout_enabled
from marketplace.payments.webhooks import record_disabled_marketplace_checkout
from marketplace.services.acquisition import apply_landing_config_to_user
from marketplace.services.entitlements import (
    ASSET_TYPE_PACK,
    ASSET_TYPE_PROMPT,
    active_entitlement_condition,
    grant_pack_entitlement,
    grant_prompt_entitlement,
    refund_entitlement,
)


MARKETPLACE_CHECKOUT_TYPES = {"pack_purchase", "prompt_purchase"}


def _session_id(session) -> str:
    return getattr(session, "id", None) or session.get("id")


async def handle_marketplace_checkout_completed(session, metadata: dict):
    product_checkout_type = metadata.get("type")

    if product_checkout_type not in MARKETPLACE_CHECKOUT_TYPES:
        return {"status": "ignored", "reason": "not_marketplace_checkout"}

    if not marketplace_checkout_enabled():
        record_status = await record_disabled_marketplace_checkout(
            session,
            metadata,
            product_checkout_type,
        )
        logger.warning(
            "Marketplace product checkout not fulfilled because checkout is disabled: session=%s type=%s record_status=%s",
            _session_id(session),
            product_checkout_type,
            record_status,
        )
        return JSONResponse(
            content={
                "status": "refund_required",
                "reason": "marketplace_checkout_disabled",
                "record_status": record_status,
            }
        )

    if product_checkout_type == "pack_purchase":
        return await _handle_pack_checkout_completed(session, metadata)
    return await _handle_prompt_checkout_completed(session, metadata)


async def _handle_pack_checkout_completed(session, metadata: dict) -> dict:
    session_id = _session_id(session)
    pack_id = int(metadata["pack_id"])
    buyer_user_id = int(metadata["buyer_user_id"])
    final_amount = float(metadata.get("final_amount", 0))
    discount_code = metadata.get("discount_code", "")
    discount_value = float(metadata.get("discount_value", 0))

    logger.info("Pack purchase completed: buyer=%s, pack=%s, amount=$%.2f", buyer_user_id, pack_id, final_amount)

    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        try:
            await conn.execute("BEGIN IMMEDIATE")

            existing = await cursor.execute(
                "SELECT id, status FROM PACK_PURCHASES WHERE payment_reference = ?",
                (session_id,),
            )
            existing_purchase = await existing.fetchone()
            if existing_purchase:
                if existing_purchase[1] == "completed":
                    await grant_pack_entitlement(
                        conn,
                        user_id=buyer_user_id,
                        pack_id=pack_id,
                        source="purchase",
                        source_ref_type="stripe_session",
                        source_ref_id=session_id,
                        metadata={
                            "pack_purchase_id": existing_purchase[0],
                            "payment_method": "stripe",
                            "amount": final_amount,
                            "discount_code": discount_code or None,
                            "webhook_replay": True,
                        },
                    )
                    await conn.commit()
                else:
                    await conn.rollback()
                logger.info("Pack purchase already processed: session=%s", session_id)
                return {"status": "success"}

            await cursor.execute(
                """INSERT INTO PACK_PURCHASES
                   (buyer_user_id, pack_id, amount, currency, payment_method, payment_reference, status)
                   VALUES (?, ?, ?, 'USD', 'stripe', ?, 'completed')""",
                (buyer_user_id, pack_id, final_amount, session_id),
            )
            pack_purchase_id = cursor.lastrowid

            await grant_pack_entitlement(
                conn,
                user_id=buyer_user_id,
                pack_id=pack_id,
                source="purchase",
                source_ref_type="stripe_session",
                source_ref_id=session_id,
                metadata={
                    "pack_purchase_id": pack_purchase_id,
                    "payment_method": "stripe",
                    "amount": final_amount,
                    "discount_code": discount_code or None,
                },
            )

            first_prompt_cursor = await cursor.execute(
                """SELECT prompt_id FROM PACK_ITEMS
                   WHERE pack_id = ? AND is_active = 1
                   AND (disable_at IS NULL OR disable_at > datetime('now'))
                   ORDER BY display_order LIMIT 1""",
                (pack_id,),
            )
            first_prompt_row = await first_prompt_cursor.fetchone()
            if first_prompt_row:
                await cursor.execute(
                    "UPDATE USER_DETAILS SET current_prompt_id = ? WHERE user_id = ?",
                    (first_prompt_row[0], buyer_user_id),
                )

            pricing = await get_pricing_config()
            commission_rate = pricing.get("commission", 0.30)
            creator_share = final_amount * (1 - commission_rate)

            pack_row = await cursor.execute(
                "SELECT created_by_user_id, landing_reg_config FROM PACKS WHERE id = ?",
                (pack_id,),
            )
            pack_data = await pack_row.fetchone()
            creator_id = pack_data[0] if pack_data else None

            if creator_id:
                try:
                    await upsert_creator_relationship(
                        cursor,
                        buyer_user_id,
                        creator_id,
                        "purchased_from",
                        "pack",
                        pack_id,
                    )
                except Exception as ucr_err:
                    logger.warning("Could not record creator relationship for pack purchase: %s", ucr_err)

            bal_cursor = await cursor.execute(
                "SELECT balance FROM USER_DETAILS WHERE user_id = ?",
                (buyer_user_id,),
            )
            bal_row = await bal_cursor.fetchone()
            buyer_balance_before = bal_row[0] if bal_row else 0

            initial_balance_cost = 0
            landing_config = {}
            if pack_data and pack_data[1]:
                landing_config = json.loads(pack_data[1]) if isinstance(pack_data[1], str) else pack_data[1]
                initial_balance_cost = await apply_landing_config_to_user(
                    conn,
                    landing_config,
                    buyer_user_id,
                    creator_user_id=creator_id,
                    discount_pct=discount_value,
                    creator_share=creator_share,
                    billing_acquisition_ref=session_id,
                )

            creator_net = creator_share - initial_balance_cost
            if creator_net < 0:
                logger.warning("Pack %s: creator_net negative (%.2f), clamping to 0", pack_id, creator_net)
                creator_net = 0
            if creator_id and creator_net > 0:
                await cursor.execute(
                    "UPDATE USER_DETAILS SET pending_earnings = COALESCE(pending_earnings, 0) + ? WHERE user_id = ?",
                    (creator_net, creator_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Creator details unavailable for purchase earnings")
                # Record the sale in the creator ledger alongside the wallet increment.
                # Idempotent via the PACK_PURCHASES existence guard above (this block runs
                # only on first fulfillment) plus the partial UNIQUE index on source_ref.
                if first_prompt_row:
                    await record_creator_earnings(
                        creator_id=creator_id,
                        prompt_id=first_prompt_row[0],
                        consumer_id=buyer_user_id,
                        tokens_consumed=0,
                        gross_amount=final_amount,
                        platform_commission=final_amount * commission_rate,
                        net_earnings=creator_net,
                        conn=conn,
                        cursor=cursor,
                        earning_type="pack_purchase",
                        source_ref=session_id,
                        pack_id=pack_id,
                    )
                else:
                    logger.warning(
                        "Pack %s purchase: no active prompt to attribute CREATOR_EARNINGS row (session=%s)",
                        pack_id,
                        session_id,
                    )

            await cursor.execute(
                """
                INSERT INTO TRANSACTIONS
                (user_id, type, amount, balance_before, balance_after,
                 description, reference_id, discount_code)
                VALUES (?, 'pack_purchase', ?, ?, ?, ?, ?, ?)
                """,
                (
                    buyer_user_id,
                    final_amount,
                    buyer_balance_before,
                    buyer_balance_before,
                    f"Pack purchase: pack_id={pack_id}, paid=${final_amount:.2f}",
                    session_id,
                    discount_code if discount_code else None,
                ),
            )

            if initial_balance_cost > 0:
                await cursor.execute(
                    """
                    INSERT INTO TRANSACTIONS
                    (user_id, type, amount, balance_before, balance_after,
                     description, reference_id)
                    VALUES (?, 'balance_credit', ?, ?, ?, ?, ?)
                    """,
                    (
                        buyer_user_id,
                        initial_balance_cost,
                        buyer_balance_before,
                        buyer_balance_before + initial_balance_cost,
                        f"Balance credit from pack purchase: pack_id={pack_id}",
                        session_id,
                    ),
                )

            await _record_purchase_fulfillment(
                conn,
                session_id=session_id,
                buyer_user_id=buyer_user_id,
                asset_type=ASSET_TYPE_PACK,
                asset_id=pack_id,
                balance_credit=initial_balance_cost,
                creator_id=creator_id,
                creator_net=creator_net if creator_id else 0,
                earnings_recorded=bool(creator_id and creator_net > 0 and first_prompt_row),
                billing_support_creator_id=(
                    creator_id if landing_config.get("billing_mode") == "user_pays" else None
                ),
            )
            await conn.commit()
            logger.info(
                "Pack purchase processed: buyer=%s, pack=%s, creator_net=$%.2f",
                buyer_user_id,
                pack_id,
                creator_net,
            )
            return {"status": "success"}

        except Exception as exc:
            await conn.rollback()
            logger.error("Error processing pack purchase webhook: %s", exc)
            raise HTTPException(status_code=500, detail="Error processing pack purchase") from exc


async def _handle_prompt_checkout_completed(session, metadata: dict) -> dict:
    session_id = _session_id(session)
    prompt_id = int(metadata["prompt_id"])
    buyer_user_id = int(metadata["buyer_user_id"])
    final_amount = float(metadata.get("final_amount", 0))
    discount_code = metadata.get("discount_code", "")
    discount_value = float(metadata.get("discount_value", 0))

    logger.info(
        "Prompt purchase completed: buyer=%s, prompt=%s, amount=$%.2f",
        buyer_user_id,
        prompt_id,
        final_amount,
    )

    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        try:
            await conn.execute("BEGIN IMMEDIATE")

            existing = await cursor.execute(
                "SELECT id, status FROM PROMPT_PURCHASES WHERE payment_reference = ?",
                (session_id,),
            )
            existing_purchase = await existing.fetchone()
            if existing_purchase:
                if existing_purchase[1] == "completed":
                    await grant_prompt_entitlement(
                        conn,
                        user_id=buyer_user_id,
                        prompt_id=prompt_id,
                        source="purchase",
                        source_ref_type="stripe_session",
                        source_ref_id=session_id,
                        metadata={
                            "prompt_purchase_id": existing_purchase[0],
                            "payment_method": "stripe",
                            "amount": final_amount,
                            "discount_code": discount_code or None,
                            "webhook_replay": True,
                        },
                    )
                    await conn.commit()
                else:
                    await conn.rollback()
                logger.info("Prompt purchase already processed: session=%s", session_id)
                return {"status": "success"}

            await cursor.execute(
                """INSERT INTO PROMPT_PURCHASES
                   (buyer_user_id, prompt_id, amount, currency, payment_method, payment_reference, status)
                   VALUES (?, ?, ?, 'USD', 'stripe', ?, 'completed')""",
                (buyer_user_id, prompt_id, final_amount, session_id),
            )
            prompt_purchase_id = cursor.lastrowid

            await grant_prompt_entitlement(
                conn,
                user_id=buyer_user_id,
                prompt_id=prompt_id,
                source="purchase",
                source_ref_type="stripe_session",
                source_ref_id=session_id,
                metadata={
                    "prompt_purchase_id": prompt_purchase_id,
                    "payment_method": "stripe",
                    "amount": final_amount,
                    "discount_code": discount_code or None,
                },
            )

            await cursor.execute(
                "UPDATE USER_DETAILS SET current_prompt_id = ? WHERE user_id = ?",
                (prompt_id, buyer_user_id),
            )

            pricing = await get_pricing_config()
            commission_rate = pricing.get("commission", 0.30)
            creator_share = final_amount * (1 - commission_rate)

            prompt_row = await cursor.execute(
                "SELECT created_by_user_id, landing_registration_config FROM PROMPTS WHERE id = ?",
                (prompt_id,),
            )
            prompt_data = await prompt_row.fetchone()
            creator_id = prompt_data[0] if prompt_data else None

            if creator_id:
                try:
                    await upsert_creator_relationship(
                        cursor,
                        buyer_user_id,
                        creator_id,
                        "purchased_from",
                        "prompt",
                        prompt_id,
                    )
                except Exception as ucr_err:
                    logger.warning("Could not record creator relationship for prompt purchase: %s", ucr_err)

            bal_cursor = await cursor.execute(
                "SELECT balance FROM USER_DETAILS WHERE user_id = ?",
                (buyer_user_id,),
            )
            bal_row = await bal_cursor.fetchone()
            buyer_balance_before = bal_row[0] if bal_row else 0

            initial_balance_cost = 0
            landing_config = {}
            if prompt_data and prompt_data[1]:
                landing_config = json.loads(prompt_data[1]) if isinstance(prompt_data[1], str) else prompt_data[1]
                initial_balance_cost = await apply_landing_config_to_user(
                    conn,
                    landing_config,
                    buyer_user_id,
                    creator_user_id=creator_id,
                    discount_pct=discount_value,
                    creator_share=creator_share,
                    billing_acquisition_ref=session_id,
                )

            creator_net = creator_share - initial_balance_cost
            if creator_net < 0:
                logger.warning("Prompt %s: creator_net negative (%.2f), clamping to 0", prompt_id, creator_net)
                creator_net = 0
            if creator_id and creator_net > 0:
                await cursor.execute(
                    "UPDATE USER_DETAILS SET pending_earnings = COALESCE(pending_earnings, 0) + ? WHERE user_id = ?",
                    (creator_net, creator_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Creator details unavailable for purchase earnings")
                # Record the sale in the creator ledger alongside the wallet increment.
                # Idempotent via the PROMPT_PURCHASES existence guard above (this block runs
                # only on first fulfillment) plus the partial UNIQUE index on source_ref.
                await record_creator_earnings(
                    creator_id=creator_id,
                    prompt_id=prompt_id,
                    consumer_id=buyer_user_id,
                    tokens_consumed=0,
                    gross_amount=final_amount,
                    platform_commission=final_amount * commission_rate,
                    net_earnings=creator_net,
                    conn=conn,
                    cursor=cursor,
                    earning_type="prompt_purchase",
                    source_ref=session_id,
                )

            await cursor.execute(
                """
                INSERT INTO TRANSACTIONS
                (user_id, type, amount, balance_before, balance_after,
                 description, reference_id, discount_code)
                VALUES (?, 'prompt_purchase', ?, ?, ?, ?, ?, ?)
                """,
                (
                    buyer_user_id,
                    final_amount,
                    buyer_balance_before,
                    buyer_balance_before,
                    f"Prompt purchase: prompt_id={prompt_id}, paid=${final_amount:.2f}",
                    session_id,
                    discount_code if discount_code else None,
                ),
            )

            if initial_balance_cost > 0:
                await cursor.execute(
                    """
                    INSERT INTO TRANSACTIONS
                    (user_id, type, amount, balance_before, balance_after,
                     description, reference_id)
                    VALUES (?, 'balance_credit', ?, ?, ?, ?, ?)
                    """,
                    (
                        buyer_user_id,
                        initial_balance_cost,
                        buyer_balance_before,
                        buyer_balance_before + initial_balance_cost,
                        f"Balance credit from prompt purchase: prompt_id={prompt_id}",
                        session_id,
                    ),
                )

            await _record_purchase_fulfillment(
                conn,
                session_id=session_id,
                buyer_user_id=buyer_user_id,
                asset_type=ASSET_TYPE_PROMPT,
                asset_id=prompt_id,
                balance_credit=initial_balance_cost,
                creator_id=creator_id,
                creator_net=creator_net if creator_id else 0,
                earnings_recorded=bool(creator_id and creator_net > 0),
                billing_support_creator_id=(
                    creator_id if landing_config.get("billing_mode") == "user_pays" else None
                ),
            )
            await conn.commit()
            logger.info(
                "Prompt purchase processed: buyer=%s, prompt=%s, creator_net=$%.2f",
                buyer_user_id,
                prompt_id,
                creator_net,
            )
            return {"status": "success"}

        except Exception as exc:
            await conn.rollback()
            logger.error("Error processing prompt purchase webhook: %s", exc)
            raise HTTPException(status_code=500, detail="Error processing prompt purchase") from exc


async def _record_purchase_fulfillment(
    conn, *, session_id: str, buyer_user_id: int, asset_type: str, asset_id: int,
    balance_credit: float, creator_id: int | None, creator_net: float,
    earnings_recorded: bool, billing_support_creator_id: int | None,
) -> None:
    # Missing ledger rows are meaningful only with this fulfillment evidence:
    # zero earnings and a pack without an active prompt otherwise look alike.
    snapshot = {
        "balance_credit": balance_credit,
        "creator_id": creator_id,
        "creator_net": creator_net,
        "earnings_recorded": earnings_recorded,
    }
    cursor = await conn.execute(
        """UPDATE ENTITLEMENTS
           SET metadata_json = json_set(COALESCE(metadata_json, '{}'),
                                        '$.purchase_fulfillment', json(?),
                                        '$.billing_support_creator_id', ?)
           WHERE user_id = ? AND asset_type = ? AND asset_id = ?
             AND source_ref_type = 'stripe_session' AND source_ref_id = ?""",
        (json.dumps(snapshot), billing_support_creator_id, buyer_user_id,
         asset_type, asset_id, session_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("Purchase entitlement unavailable for fulfillment evidence")


def _refund_evidence_error(reason: str) -> HTTPException:
    return HTTPException(status_code=409, detail=f"Purchase refund requires reconciliation: {reason}")


async def handle_marketplace_chargeback(session, metadata: dict, payment_intent_id: str):
    checkout_type = metadata.get("type")
    if checkout_type not in MARKETPLACE_CHECKOUT_TYPES:
        return {"status": "ignored", "reason": "not_marketplace_checkout"}
    asset_type = ASSET_TYPE_PACK if checkout_type == "pack_purchase" else ASSET_TYPE_PROMPT
    return await _handle_product_chargeback(session, metadata, payment_intent_id, asset_type)


async def _handle_product_chargeback(session, metadata: dict, payment_intent_id: str, asset_type: str) -> dict:
    session_id = _session_id(session)
    asset_id = int(metadata[f"{asset_type}_id"])
    buyer_user_id = int(metadata["buyer_user_id"])
    purchase_table = "PACK_PURCHASES" if asset_type == ASSET_TYPE_PACK else "PROMPT_PURCHASES"
    purchase_type = f"{asset_type}_purchase"

    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await conn.execute(
                f"SELECT id, status, buyer_user_id, {asset_type}_id, amount FROM {purchase_table} WHERE payment_reference = ?",
                (session_id,),
            )
            purchases = await cursor.fetchall()
            if not purchases:
                raise HTTPException(status_code=409, detail=f"Original {asset_type} purchase has not been processed yet")
            if len(purchases) != 1:
                raise _refund_evidence_error("ambiguous purchase records")
            purchase = purchases[0]
            if purchase[2] != buyer_user_id or purchase[3] != asset_id:
                raise _refund_evidence_error("purchase attribution does not match the session")
            if purchase[1] == "refunded":
                await conn.rollback()
                return {"status": "already_processed"}
            if purchase[1] != "completed":
                raise _refund_evidence_error("original purchase is not completed")

            cursor = await conn.execute(
                """SELECT metadata_json FROM ENTITLEMENTS
                   WHERE user_id = ? AND asset_type = ? AND asset_id = ?
                     AND source_ref_type = 'stripe_session' AND source_ref_id = ?""",
                (buyer_user_id, asset_type, asset_id, session_id),
            )
            entitlement = await cursor.fetchone()
            if entitlement is None:
                raise _refund_evidence_error("purchase entitlement is missing")
            try:
                entitlement_metadata = json.loads(entitlement[0]) if entitlement[0] else {}
            except (TypeError, json.JSONDecodeError) as exc:
                raise _refund_evidence_error("invalid purchase metadata") from exc
            if not isinstance(entitlement_metadata, dict):
                raise _refund_evidence_error("invalid purchase metadata")

            cursor = await conn.execute(
                "SELECT user_id, type, amount FROM TRANSACTIONS WHERE reference_id = ? AND type IN (?, 'balance_credit')",
                (session_id, purchase_type),
            )
            transactions = await cursor.fetchall()
            sale_rows = [row for row in transactions if row[1] == purchase_type]
            credit_rows = [row for row in transactions if row[1] == "balance_credit"]
            if (len(sale_rows) != 1 or sale_rows[0][0] != buyer_user_id
                    or not math.isclose(float(sale_rows[0][2]), float(purchase[4]))
                    or len(credit_rows) > 1 or any(row[0] != buyer_user_id for row in credit_rows)):
                raise _refund_evidence_error("original transaction records are missing or inconsistent")
            balance_credit = float(credit_rows[0][2]) if credit_rows else 0

            cursor = await conn.execute(
                """SELECT id, creator_id, consumer_id, earning_type, pack_id,
                          net_earnings, gross_amount
                   FROM CREATOR_EARNINGS WHERE source_ref = ?""",
                (session_id,),
            )
            earning = await cursor.fetchone()
            if earning is not None and (
                earning[2] != buyer_user_id or earning[3] != purchase_type
                or earning[4] != (asset_id if asset_type == ASSET_TYPE_PACK else None)
                or not math.isclose(float(earning[6]), float(purchase[4]))
            ):
                raise _refund_evidence_error("creator earnings attribution is inconsistent")

            snapshot = entitlement_metadata.get("purchase_fulfillment")
            if snapshot is None:
                # Legacy rows need both positive records; absence cannot prove
                # a zero credit or zero earnings (packs historically skipped it).
                if not credit_rows or earning is None:
                    raise _refund_evidence_error("historical fulfillment amounts are incomplete")
                creator_id, creator_net = earning[1], float(earning[5])
            else:
                try:
                    expected_credit = float(snapshot["balance_credit"])
                    creator_id = snapshot["creator_id"]
                    creator_net = float(snapshot["creator_net"])
                    earnings_recorded = snapshot["earnings_recorded"]
                except (KeyError, TypeError, ValueError) as exc:
                    raise _refund_evidence_error("invalid fulfillment amounts") from exc
                if (not math.isclose(balance_credit, expected_credit)
                        or not isinstance(earnings_recorded, bool)
                        or earnings_recorded != (earning is not None)
                        or (earning is not None and (earning[1] != creator_id
                            or not math.isclose(float(earning[5]), creator_net)))):
                    raise _refund_evidence_error("fulfillment records do not agree")
            if (not math.isfinite(balance_credit) or balance_credit < 0
                    or not math.isfinite(creator_net) or creator_net < 0
                    or (creator_net > 0 and not creator_id)):
                raise _refund_evidence_error("invalid credited amounts")

            cursor = await conn.execute("SELECT balance FROM USER_DETAILS WHERE user_id = ?", (buyer_user_id,))
            buyer = await cursor.fetchone()
            if buyer is None:
                raise _refund_evidence_error("buyer details are missing")
            balance_before = float(buyer[0] or 0)
            balance_after = max(0, balance_before - balance_credit)
            balance_debit = balance_before - balance_after
            await conn.execute("UPDATE USER_DETAILS SET balance = ? WHERE user_id = ?", (balance_after, buyer_user_id))
            if creator_net > 0:
                cursor = await conn.execute(
                    "UPDATE USER_DETAILS SET pending_earnings = COALESCE(pending_earnings, 0) - ? WHERE user_id = ?",
                    (creator_net, creator_id),
                )
                if cursor.rowcount != 1:
                    raise _refund_evidence_error("creator details are missing")
            if earning is not None:
                # Preserve the sale and offset it, so existing earnings reports
                # show the reversal. A paid-out sale becomes local pending debt.
                await conn.execute(
                    """INSERT INTO CREATOR_EARNINGS
                       (creator_id, prompt_id, consumer_id, referral_id, tokens_consumed,
                        gross_amount, platform_commission, net_earnings, earning_type, source_ref, pack_id)
                       SELECT creator_id, prompt_id, consumer_id, referral_id, 0,
                              -gross_amount, -platform_commission, -net_earnings, ?, ?, pack_id
                       FROM CREATOR_EARNINGS WHERE id = ?""",
                    (purchase_type + "_refund", "refund:" + session_id, earning[0]),
                )
            await conn.execute(
                """INSERT INTO TRANSACTIONS
                   (user_id, type, amount, balance_before, balance_after, description, reference_id)
                   VALUES (?, 'chargeback_reversal', ?, ?, ?, ?, ?)""",
                (buyer_user_id, balance_debit, balance_before, balance_after,
                 f"Chargeback reversal for {asset_type}: {asset_id}; credited=${balance_credit:.2f}", session_id),
            )
            await refund_entitlement(
                conn, user_id=buyer_user_id, asset_type=asset_type, asset_id=asset_id,
                source_ref_type="stripe_session", source_ref_id=session_id,
                metadata={"event": "charge.dispute.created"},
            )
            await conn.execute(f"UPDATE {purchase_table} SET status = 'refunded' WHERE id = ?", (purchase[0],))
            await _revoke_unsupported_acquisition_billing(conn, buyer_user_id)
            await conn.commit()
            logger.warning(
                "Chargeback processed: session=%s, user=%s, asset=%s:%s, credit=$%.2f, debited=$%.2f, earnings=$%.2f",
                session_id, buyer_user_id, asset_type, asset_id, balance_credit, balance_debit, creator_net,
            )
            return {"status": "success"}
        except Exception:
            await conn.rollback()
            raise


async def _revoke_unsupported_acquisition_billing(conn, user_id: int) -> None:
    cursor = await conn.execute(
        "SELECT billing_account_id, billing_acquisition_ref FROM USER_DETAILS WHERE user_id = ?",
        (user_id,),
    )
    payer_id, origin_ref = await cursor.fetchone()
    if payer_id is None or origin_ref is None:
        return  # Historical and administrative assignments have no purchase origin.

    cursor = await conn.execute(
        """SELECT asset_type, asset_id, status, metadata_json FROM ENTITLEMENTS
           WHERE user_id = ? AND source_ref_type = 'stripe_session' AND source_ref_id = ?""",
        (user_id, origin_ref),
    )
    origins = await cursor.fetchall()
    if len(origins) != 1:
        logger.warning("Billing origin needs reconciliation for user=%s, source=%s", user_id, origin_ref)
        return
    origin = origins[0]
    try:
        origin_metadata = json.loads(origin[3] or '{}')
    except (TypeError, json.JSONDecodeError):
        return
    if (not isinstance(origin_metadata, dict)
            or type(origin_metadata.get("billing_support_creator_id")) is not int
            or origin_metadata["billing_support_creator_id"] != payer_id
            or origin[0] not in (ASSET_TYPE_PACK, ASSET_TYPE_PROMPT)
            or origin[2] != "refunded"):
        return
    # A may have assigned billing and then been refunded while B supported it.
    # Refunding B must still review A's durable origin, without selecting B's
    # different limits or a replacement payer.
    origin_table = "PACK_PURCHASES" if origin[0] == ASSET_TYPE_PACK else "PROMPT_PURCHASES"
    cursor = await conn.execute(
        f"""SELECT 1 FROM {origin_table}
            WHERE buyer_user_id = ? AND {origin[0]}_id = ?
              AND payment_reference = ? AND status = 'refunded'""",
        (user_id, origin[1], origin_ref),
    )
    if await cursor.fetchone() is None:
        return
    cursor = await conn.execute(
        f"""SELECT e.metadata_json FROM ENTITLEMENTS e
            WHERE e.user_id = ? AND e.asset_type IN ('pack', 'prompt')
              AND {active_entitlement_condition('e')}""",
        (user_id,),
    )
    for row in await cursor.fetchall():
        try:
            support = json.loads(row[0] or '{}')
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(support, dict) or "billing_support_creator_id" not in support:
            logger.warning("Historical billing support needs reconciliation for user=%s", user_id)
            return
        supporter = support["billing_support_creator_id"]
        if supporter == payer_id or (
            supporter is not None and (type(supporter) is not int or supporter <= 0)
        ):
            return

    cursor = await conn.execute(
        "SELECT 1 FROM BILLING_USAGE_RESERVATIONS WHERE user_id = ? AND status = 'active' LIMIT 1",
        (user_id,),
    )
    if await cursor.fetchone() is not None:
        raise HTTPException(status_code=409, detail="Billing usage is in progress; retry purchase refund")
    await conn.execute(
        """UPDATE USER_DETAILS SET billing_account_id = NULL, billing_acquisition_ref = NULL,
                  billing_limit = NULL, billing_limit_action = 'block',
                  billing_auto_refill_amount = 10, billing_max_limit = NULL,
                  billing_current_month_spent = 0, billing_month_reset_date = NULL,
                  billing_auto_refill_count = 0
           WHERE user_id = ?""",
        (user_id,),
    )
