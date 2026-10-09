"""Marketplace acquisition helpers shared by checkout, auth, and entitlements."""

from __future__ import annotations

from common import get_balance
from log_config import logger


async def apply_landing_config_to_user(
    conn,
    config: dict,
    user_id: int,
    creator_user_id: int = None,
    discount_pct: float = 0,
    creator_share: float = None,
    *,
    creator_balance: float | None = None,
    billing_acquisition_ref: str | None = None,
) -> float:
    """Apply a prompt/pack landing_registration_config to an existing user.

    Expand-only: never restricts existing permissions or overwrites billing.
    A creator_share identifies a paid purchase: assign its billing policy even
    when the creator has no funds; usage reservations check funds when spent.
    Free callers can reuse a creator_balance they have already checked.
    Does not commit; caller owns the transaction.
    """
    ud_cursor = await conn.execute(
        """
        SELECT public_prompts_access, billing_account_id,
               allow_file_upload, allow_image_generation
        FROM USER_DETAILS WHERE user_id = ?
        """,
        (user_id,),
    )
    ud_row = await ud_cursor.fetchone()
    if not ud_row:
        if creator_share is not None:
            raise ValueError(f"User details unavailable for paid acquisition: {user_id}")
        return 0

    cur_public, cur_billing, cur_file, cur_imggen = ud_row
    updates = []
    params = []
    initial_balance_cost = 0

    initial_balance = float(config.get("initial_balance", 0))
    if initial_balance > 0:
        scaled_balance = initial_balance * (1 - discount_pct / 100) if discount_pct > 0 else initial_balance
        if creator_share is not None and scaled_balance > creator_share:
            logger.warning(
                "Landing config: initial_balance %.2f exceeds creator_share %.2f, clamping",
                scaled_balance,
                creator_share,
            )
            scaled_balance = creator_share
        if scaled_balance > 0:
            initial_balance_cost = scaled_balance
            updates.append("balance = balance + ?")
            params.append(scaled_balance)

    if config.get("billing_mode") == "user_pays" and creator_user_id:
        if cur_billing is not None:
            logger.warning(
                "Landing config: billing_account_id not overwritten for user %s (already %s)",
                user_id,
                cur_billing,
            )
        elif creator_share is None and (
            creator_balance if creator_balance is not None else await get_balance(creator_user_id)
        ) <= 0:
            logger.warning(
                "Landing config: creator %s has zero balance, skipping billing setup for user %s",
                creator_user_id,
                user_id,
            )
        else:
            updates.extend(["billing_account_id = ?", "billing_acquisition_ref = ?"])
            params.extend([creator_user_id, billing_acquisition_ref])
            billing_limit = config.get("billing_limit")
            if billing_limit is not None:
                updates.append("billing_limit = ?")
                params.append(float(billing_limit))
            updates.append("billing_limit_action = ?")
            params.append(config.get("billing_limit_action", "block"))
            updates.append("billing_auto_refill_amount = ?")
            params.append(float(config.get("billing_auto_refill_amount", 10.0)))
            billing_max_limit = config.get("billing_max_limit")
            if billing_max_limit is not None:
                updates.append("billing_max_limit = ?")
                params.append(float(billing_max_limit))

    if "public_prompts_access" in config:
        if config["public_prompts_access"] and not cur_public:
            updates.append("public_prompts_access = 1")
        elif not config["public_prompts_access"] and cur_public:
            logger.warning("Landing config: skipping public_prompts_access downgrade for user %s", user_id)
    if "allow_file_upload" in config:
        if config["allow_file_upload"] and not cur_file:
            updates.append("allow_file_upload = 1")
        elif not config["allow_file_upload"] and cur_file:
            logger.warning("Landing config: skipping allow_file_upload downgrade for user %s", user_id)
    if "allow_image_generation" in config:
        if config["allow_image_generation"] and not cur_imggen:
            updates.append("allow_image_generation = 1")
        elif not config["allow_image_generation"] and cur_imggen:
            logger.warning("Landing config: skipping allow_image_generation downgrade for user %s", user_id)

    if updates:
        params.append(user_id)
        sql = f"UPDATE USER_DETAILS SET {', '.join(updates)} WHERE user_id = ?"
        await conn.execute(sql, params)

    return initial_balance_cost
