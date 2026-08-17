"""Marketplace product fulfillment for Stripe Checkout webhooks."""

from __future__ import annotations

import json

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
            if pack_data and pack_data[1]:
                try:
                    landing_config = json.loads(pack_data[1]) if isinstance(pack_data[1], str) else pack_data[1]
                    initial_balance_cost = await _apply_pack_landing_config_for_webhook(
                        cursor,
                        landing_config,
                        buyer_user_id,
                        creator_id,
                        pack_id,
                        creator_share,
                        discount_value,
                    )
                except Exception as lrc_err:
                    logger.warning("Failed to apply pack landing config: %s", lrc_err)

            creator_net = creator_share - initial_balance_cost
            if creator_net < 0:
                logger.warning("Pack %s: creator_net negative (%.2f), clamping to 0", pack_id, creator_net)
                creator_net = 0
            if creator_id and creator_net > 0:
                await cursor.execute(
                    "UPDATE USER_DETAILS SET pending_earnings = COALESCE(pending_earnings, 0) + ? WHERE user_id = ?",
                    (creator_net, creator_id),
                )
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


async def _apply_pack_landing_config_for_webhook(
    cursor,
    landing_config: dict,
    buyer_user_id: int,
    creator_id: int | None,
    pack_id: int,
    creator_share: float,
    discount_value: float,
) -> float:
    initial_balance_cost = 0
    ib = float(landing_config.get("initial_balance", 0))
    if ib > 0:
        scale = (1 - discount_value / 100) if discount_value > 0 else 1
        scaled_ib = ib * scale
        if scaled_ib > creator_share:
            logger.warning(
                "Pack %s: initial_balance %.2f exceeds creator_share %.2f, clamping",
                pack_id,
                scaled_ib,
                creator_share,
            )
            scaled_ib = creator_share
        if scaled_ib > 0:
            initial_balance_cost = scaled_ib
            await cursor.execute(
                "UPDATE USER_DETAILS SET balance = balance + ? WHERE user_id = ?",
                (scaled_ib, buyer_user_id),
            )

    ud_cur = await cursor.execute(
        """SELECT public_prompts_access, billing_account_id,
                  allow_file_upload, allow_image_generation
           FROM USER_DETAILS WHERE user_id = ?""",
        (buyer_user_id,),
    )
    ud_row = await ud_cur.fetchone()
    cur_public = ud_row[0] if ud_row else 0
    cur_billing = ud_row[1] if ud_row else None
    cur_file = ud_row[2] if ud_row else 0
    cur_imggen = ud_row[3] if ud_row else 0

    if landing_config.get("billing_mode") == "user_pays" and creator_id:
        if cur_billing is None:
            await cursor.execute(
                "UPDATE USER_DETAILS SET billing_account_id = ? WHERE user_id = ?",
                (creator_id, buyer_user_id),
            )
        else:
            logger.warning(
                "Webhook: billing_account_id not overwritten for user %s (already %s)",
                buyer_user_id,
                cur_billing,
            )
    if "public_prompts_access" in landing_config:
        if landing_config["public_prompts_access"] and not cur_public:
            await cursor.execute(
                "UPDATE USER_DETAILS SET public_prompts_access = 1 WHERE user_id = ?",
                (buyer_user_id,),
            )
        elif not landing_config["public_prompts_access"] and cur_public:
            logger.warning("Webhook: skipping public_prompts_access downgrade for user %s", buyer_user_id)
    if "allow_file_upload" in landing_config:
        if landing_config["allow_file_upload"] and not cur_file:
            await cursor.execute(
                "UPDATE USER_DETAILS SET allow_file_upload = 1 WHERE user_id = ?",
                (buyer_user_id,),
            )
        elif not landing_config["allow_file_upload"] and cur_file:
            logger.warning("Webhook: skipping allow_file_upload downgrade for user %s", buyer_user_id)
    if "allow_image_generation" in landing_config:
        if landing_config["allow_image_generation"] and not cur_imggen:
            await cursor.execute(
                "UPDATE USER_DETAILS SET allow_image_generation = 1 WHERE user_id = ?",
                (buyer_user_id,),
            )
        elif not landing_config["allow_image_generation"] and cur_imggen:
            logger.warning("Webhook: skipping allow_image_generation downgrade for user %s", buyer_user_id)

    return initial_balance_cost


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
            if prompt_data and prompt_data[1]:
                try:
                    landing_config = json.loads(prompt_data[1]) if isinstance(prompt_data[1], str) else prompt_data[1]
                    initial_balance_cost = await apply_landing_config_to_user(
                        conn,
                        landing_config,
                        buyer_user_id,
                        creator_user_id=creator_id,
                        discount_pct=discount_value,
                        creator_share=creator_share,
                    )
                except Exception as lrc_err:
                    logger.warning("Failed to apply prompt landing config: %s", lrc_err)

            creator_net = creator_share - initial_balance_cost
            if creator_net < 0:
                logger.warning("Prompt %s: creator_net negative (%.2f), clamping to 0", prompt_id, creator_net)
                creator_net = 0
            if creator_id and creator_net > 0:
                await cursor.execute(
                    "UPDATE USER_DETAILS SET pending_earnings = COALESCE(pending_earnings, 0) + ? WHERE user_id = ?",
                    (creator_net, creator_id),
                )
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


async def handle_marketplace_chargeback(session, metadata: dict, payment_intent_id: str):
    checkout_type = metadata.get("type")
    if checkout_type == "pack_purchase":
        return await _handle_pack_chargeback(session, metadata, payment_intent_id)
    if checkout_type == "prompt_purchase":
        return await _handle_prompt_chargeback(session, metadata, payment_intent_id)
    return {"status": "ignored", "reason": "not_marketplace_checkout"}


async def _handle_pack_chargeback(session, metadata: dict, payment_intent_id: str) -> dict:
    session_id = _session_id(session)
    pack_id = int(metadata["pack_id"])
    buyer_user_id = int(metadata["buyer_user_id"])
    final_amount = float(metadata.get("final_amount", 0))
    discount_value = float(metadata.get("discount_value", 0))

    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")

        check_cursor = await conn.execute(
            "SELECT status FROM PACK_PURCHASES WHERE payment_reference = ?",
            (session_id,),
        )
        purchase_check = await check_cursor.fetchone()
        if not purchase_check:
            await conn.rollback()
            logger.warning(
                "Pack chargeback arrived before purchase completion: session=%s",
                session_id,
            )
            raise HTTPException(
                status_code=409,
                detail="Original pack purchase has not been processed yet",
            )
        if purchase_check and purchase_check[0] == "refunded":
            await conn.rollback()
            logger.info("Chargeback already processed for payment_intent=%s", payment_intent_id)
            return {"status": "already_processed"}

        try:
            await refund_entitlement(
                conn,
                user_id=buyer_user_id,
                asset_type=ASSET_TYPE_PACK,
                asset_id=pack_id,
                source_ref_type="stripe_session",
                source_ref_id=session_id,
                metadata={"event": "charge.dispute.created"},
            )
            await conn.execute(
                "UPDATE PACK_PURCHASES SET status = 'refunded' WHERE pack_id = ? AND buyer_user_id = ? AND payment_reference = ?",
                (pack_id, buyer_user_id, session_id),
            )

            pricing = await get_pricing_config()
            commission_rate = pricing.get("commission", 0.30)
            creator_share = final_amount * (1 - commission_rate)

            initial_balance_cost = 0
            pack_cursor = await conn.execute(
                "SELECT created_by_user_id, landing_reg_config FROM PACKS WHERE id = ?",
                (pack_id,),
            )
            pack_data = await pack_cursor.fetchone()
            creator_id = pack_data[0] if pack_data else None

            if pack_data and pack_data[1]:
                try:
                    lrc = json.loads(pack_data[1]) if isinstance(pack_data[1], str) else pack_data[1]
                    ib = float(lrc.get("initial_balance", 0))
                    if ib > 0:
                        scale = (1 - discount_value / 100) if discount_value > 0 else 1
                        scaled_ib = ib * scale
                        if scaled_ib > 0:
                            initial_balance_cost = scaled_ib
                except Exception:
                    pass

            if initial_balance_cost > 0:
                await conn.execute(
                    "UPDATE USER_DETAILS SET balance = MAX(0, balance - ?) WHERE user_id = ?",
                    (initial_balance_cost, buyer_user_id),
                )

            creator_net = creator_share - initial_balance_cost
            if creator_net < 0:
                creator_net = 0
            if creator_id and creator_net > 0:
                await conn.execute(
                    "UPDATE USER_DETAILS SET pending_earnings = MAX(0, COALESCE(pending_earnings, 0) - ?) WHERE user_id = ?",
                    (creator_net, creator_id),
                )

            await conn.commit()
            logger.warning(
                "Chargeback processed: revoked access for user=%s, pack=%s, reverted ib=$%.2f, creator_net=$%.2f",
                buyer_user_id,
                pack_id,
                initial_balance_cost,
                creator_net,
            )
            return {"status": "success"}
        except Exception:
            await conn.rollback()
            raise


async def _handle_prompt_chargeback(session, metadata: dict, payment_intent_id: str) -> dict:
    session_id = _session_id(session)
    prompt_id = int(metadata["prompt_id"])
    buyer_user_id = int(metadata["buyer_user_id"])
    final_amount = float(metadata.get("final_amount", 0))
    discount_value = float(metadata.get("discount_value", 0))

    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")

        check_cursor = await conn.execute(
            "SELECT id, status FROM PROMPT_PURCHASES WHERE payment_reference = ?",
            (session_id,),
        )
        purchase_check = await check_cursor.fetchone()
        if not purchase_check:
            await conn.rollback()
            logger.warning(
                "Prompt chargeback arrived before purchase completion: session=%s",
                session_id,
            )
            raise HTTPException(
                status_code=409,
                detail="Original prompt purchase has not been processed yet",
            )
        if purchase_check and purchase_check[1] == "refunded":
            await conn.rollback()
            logger.info("Prompt chargeback already processed for payment_intent=%s", payment_intent_id)
            return {"status": "already_processed"}

        purchase_id = purchase_check[0] if purchase_check else None

        try:
            await refund_entitlement(
                conn,
                user_id=buyer_user_id,
                asset_type=ASSET_TYPE_PROMPT,
                asset_id=prompt_id,
                source_ref_type="stripe_session",
                source_ref_id=session_id,
                metadata={"event": "charge.dispute.created"},
            )
            if purchase_id:
                await conn.execute(
                    "UPDATE PROMPT_PURCHASES SET status = 'refunded' WHERE id = ?",
                    (purchase_id,),
                )

            pricing = await get_pricing_config()
            commission_rate = pricing.get("commission", 0.30)
            creator_share = final_amount * (1 - commission_rate)

            initial_balance_cost = 0
            prompt_cursor = await conn.execute(
                "SELECT created_by_user_id, landing_registration_config FROM PROMPTS WHERE id = ?",
                (prompt_id,),
            )
            prompt_data = await prompt_cursor.fetchone()
            creator_id = prompt_data[0] if prompt_data else None

            if prompt_data and prompt_data[1]:
                try:
                    lrc = json.loads(prompt_data[1]) if isinstance(prompt_data[1], str) else prompt_data[1]
                    ib = float(lrc.get("initial_balance", 0))
                    if ib > 0:
                        scale = (1 - discount_value / 100) if discount_value > 0 else 1
                        scaled_ib = ib * scale
                        if scaled_ib > 0:
                            initial_balance_cost = scaled_ib
                except Exception:
                    pass

            if initial_balance_cost > 0:
                await conn.execute(
                    "UPDATE USER_DETAILS SET balance = MAX(0, balance - ?) WHERE user_id = ?",
                    (initial_balance_cost, buyer_user_id),
                )

            creator_net = creator_share - initial_balance_cost
            if creator_net < 0:
                creator_net = 0
            if creator_id and creator_net > 0:
                await conn.execute(
                    "UPDATE USER_DETAILS SET pending_earnings = MAX(0, COALESCE(pending_earnings, 0) - ?) WHERE user_id = ?",
                    (creator_net, creator_id),
                )

            await conn.commit()
            logger.warning(
                "Prompt chargeback processed: revoked access for user=%s, prompt=%s, reverted ib=$%.2f, creator_net=$%.2f",
                buyer_user_id,
                prompt_id,
                initial_balance_cost,
                creator_net,
            )
            return {"status": "success"}
        except Exception:
            await conn.rollback()
            raise
