import math
from dataclasses import dataclass
from datetime import date, datetime

from database import get_db_connection
from log_config import logger


class DiscountError(ValueError):
    def __init__(self, message: str, status_code: int = 400, *, code: str = "unknown"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


DISCOUNT_SCOPE_MARKETPLACE = "marketplace"
DISCOUNT_SCOPE_WALLET = "wallet"
DISCOUNT_SCOPES = {DISCOUNT_SCOPE_MARKETPLACE, DISCOUNT_SCOPE_WALLET}
WALLET_REDEMPTION_PURPOSE = "wallet_credit"
MIN_WALLET_GRANT = 5.0
MAX_WALLET_GRANT = 500.0


@dataclass(frozen=True)
class DiscountResult:
    code: str
    discount_value: float
    original_amount: float
    final_amount: float
    scope: str = DISCOUNT_SCOPE_MARKETPLACE


@dataclass(frozen=True)
class WalletCreditResult:
    code: str
    grant_amount: float
    scope: str = DISCOUNT_SCOPE_WALLET


def validate_wallet_grant_amount(amount: float) -> float:
    try:
        grant_amount = round(float(amount), 2)
    except (TypeError, ValueError) as exc:
        raise DiscountError("Wallet credit grant must be a valid amount") from exc
    if (
        not math.isfinite(grant_amount)
        or grant_amount < MIN_WALLET_GRANT
        or grant_amount > MAX_WALLET_GRANT
    ):
        raise DiscountError("Wallet credit grant must be between $5 and $500")
    return grant_amount


async def _load_active_discount(conn, code: str, *, scope: str):
    if scope not in DISCOUNT_SCOPES:
        raise DiscountError("Invalid discount scope", code="invalid_scope")

    cursor = await conn.execute(
        """
        SELECT discount_value, active, usage_count, validity_date,
               unlimited_usage, unlimited_validity, scope, wallet_grant_amount
        FROM DISCOUNTS
        WHERE code = ?
        """,
        (code,),
    )
    discount = await cursor.fetchone()

    if not discount or not discount["active"]:
        raise DiscountError("Invalid or inactive discount code", code="invalid_code")
    if (discount["scope"] or DISCOUNT_SCOPE_MARKETPLACE) != scope:
        raise DiscountError("Discount code is not valid for this purchase", code="wrong_scope")

    validity_date = discount["validity_date"]
    if not discount["unlimited_validity"] and validity_date:
        validity = datetime.strptime(validity_date, "%Y-%m-%d").date()
        if date.today() > validity:
            raise DiscountError("Discount code has expired", code="expired")

    if not discount["unlimited_usage"] and discount["usage_count"] is not None:
        if discount["usage_count"] <= 0:
            raise DiscountError("Discount code usage limit reached", code="usage_limit")
    return discount


async def validate_discount_code(
    code: str,
    amount: float,
    *,
    scope: str = DISCOUNT_SCOPE_MARKETPLACE,
    conn=None,
) -> DiscountResult:
    discount_code = (code or "").strip()
    if not discount_code:
        return DiscountResult("", 0.0, float(amount), float(amount), scope)

    if conn is None:
        async with get_db_connection(readonly=True) as owned_conn:
            return await validate_discount_code(
                discount_code,
                amount,
                scope=scope,
                conn=owned_conn,
            )

    discount = await _load_active_discount(conn, discount_code, scope=scope)

    discount_value = float(discount["discount_value"])
    if not math.isfinite(discount_value) or discount_value < 0 or discount_value > 100:
        raise DiscountError("Invalid discount value", code="invalid_value")

    original_amount = float(amount)
    if not math.isfinite(original_amount) or original_amount < 0:
        raise DiscountError("Invalid original amount", code="invalid_amount")
    final_amount = max(0, original_amount * (1 - discount_value / 100))
    return DiscountResult(
        discount_code,
        discount_value,
        original_amount,
        final_amount,
        scope,
    )


async def validate_wallet_credit_code(
    code: str,
    user_id: int,
    *,
    conn=None,
) -> WalletCreditResult:
    discount_code = (code or "").strip()
    if not discount_code:
        raise DiscountError("Wallet credit code is required")
    if int(user_id) <= 0:
        raise DiscountError("Invalid user")

    if conn is None:
        async with get_db_connection(readonly=True) as owned_conn:
            return await validate_wallet_credit_code(
                discount_code,
                int(user_id),
                conn=owned_conn,
            )

    discount = await _load_active_discount(
        conn,
        discount_code,
        scope=DISCOUNT_SCOPE_WALLET,
    )
    grant_amount = validate_wallet_grant_amount(discount["wallet_grant_amount"])
    cursor = await conn.execute(
        """
        SELECT 1
        FROM DISCOUNT_REDEMPTIONS
        WHERE discount_code = ? AND user_id = ? AND purpose = ?
        """,
        (discount_code, int(user_id), WALLET_REDEMPTION_PURPOSE),
    )
    if await cursor.fetchone():
        raise DiscountError("Wallet credit code has already been redeemed", status_code=409)
    return WalletCreditResult(discount_code, grant_amount)


async def decrement_discount_usage(
    conn,
    code: str,
    *,
    scope: str = DISCOUNT_SCOPE_MARKETPLACE,
) -> None:
    discount_code = (code or "").strip()
    if not discount_code:
        return

    await conn.execute(
        """
        UPDATE DISCOUNTS SET usage_count = CASE
            WHEN unlimited_usage = 1 THEN usage_count
            WHEN usage_count IS NULL THEN usage_count
            ELSE MAX(0, COALESCE(usage_count, 1) - 1)
        END
        WHERE code = ? AND scope = ?
        """,
        (discount_code, scope),
    )


async def claim_discount_usage_for_checkout(code: str, amount: float) -> DiscountResult:
    discount_code = (code or "").strip()
    if not discount_code:
        return DiscountResult("", 0.0, float(amount), float(amount))

    async with get_db_connection() as conn:
        try:
            await conn.execute("BEGIN IMMEDIATE")
            discount = await validate_discount_code(
                discount_code,
                amount,
                scope=DISCOUNT_SCOPE_MARKETPLACE,
                conn=conn,
            )
            await decrement_discount_usage(
                conn,
                discount_code,
                scope=DISCOUNT_SCOPE_MARKETPLACE,
            )
            await conn.commit()
            return discount
        except Exception:
            await conn.rollback()
            raise


async def restore_discount_usage_for_checkout(
    code: str,
    *,
    reference_id: str | None = None,
    user_id: int | None = None,
) -> None:
    discount_code = (code or "").strip()
    if not discount_code:
        return

    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        if reference_id:
            existing = await conn.execute(
                "SELECT id FROM TRANSACTIONS WHERE reference_id = ? AND type = 'discount_restored'",
                (reference_id,),
            )
            if await existing.fetchone():
                await conn.rollback()
                return

        await conn.execute(
            """
            UPDATE DISCOUNTS SET usage_count = CASE
                WHEN unlimited_usage = 1 THEN usage_count
                WHEN usage_count IS NULL THEN usage_count
                ELSE usage_count + 1
            END
            WHERE code = ? AND scope = ?
            """,
            (discount_code, DISCOUNT_SCOPE_MARKETPLACE),
        )
        if reference_id and user_id:
            await conn.execute(
                """
                INSERT INTO TRANSACTIONS
                (user_id, type, amount, balance_before, balance_after,
                 description, reference_id, discount_code)
                VALUES (?, 'discount_restored', 0, 0, 0, ?, ?, ?)
                """,
                (
                    user_id,
                    "Discount restored after Stripe Checkout creation did not complete locally",
                    reference_id,
                    discount_code,
                ),
            )
        await conn.commit()


async def restore_discount_usage_for_expired_session(session) -> dict:
    metadata = session.get("metadata", {}) or {}
    if metadata.get("discount_claimed") != "1":
        return {"status": "ignored", "reason": "discount_not_claimed"}

    discount_code = (metadata.get("discount_code") or "").strip()
    if not discount_code:
        return {"status": "ignored", "reason": "missing_discount_code"}

    session_id = getattr(session, "id", None) or session.get("id")
    if not session_id:
        return {"status": "ignored", "reason": "missing_session_id"}
    restore_reference = metadata.get("discount_claim_reference") or session_id

    try:
        user_id = int(metadata.get("user_id") or metadata.get("buyer_user_id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    if user_id <= 0:
        return {"status": "ignored", "reason": "missing_user_id"}

    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            existing = await conn.execute(
                "SELECT id FROM TRANSACTIONS WHERE reference_id = ? AND type = 'discount_restored'",
                (restore_reference,),
            )
            if await existing.fetchone():
                await conn.rollback()
                return {"status": "already_processed"}

            await conn.execute(
                """
                UPDATE DISCOUNTS SET usage_count = CASE
                    WHEN unlimited_usage = 1 THEN usage_count
                    WHEN usage_count IS NULL THEN usage_count
                    ELSE usage_count + 1
                END
                WHERE code = ? AND scope = ?
                """,
                (discount_code, DISCOUNT_SCOPE_MARKETPLACE),
            )
            await conn.execute(
                """
                INSERT INTO TRANSACTIONS
                (user_id, type, amount, balance_before, balance_after,
                 description, reference_id, discount_code)
                VALUES (?, 'discount_restored', 0, 0, 0, ?, ?, ?)
                """,
                (
                    user_id,
                    "Discount restored after expired Stripe Checkout session",
                    restore_reference,
                    discount_code,
                ),
            )
            await conn.commit()
            logger.info(
                "Restored discount usage after expired Checkout session: session=%s code=%s",
                session_id,
                discount_code,
            )
            return {"status": "success"}
        except Exception:
            await conn.rollback()
            raise
