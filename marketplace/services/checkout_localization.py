"""Present known checkout business failures without exposing arbitrary exceptions."""

from billing.discounts import DiscountError
from i18n import Translator


_DISCOUNT_ERROR_KEYS = {
    "invalid_scope": "marketplace.checkout.discount_invalid_scope",
    "invalid_code": "marketplace.checkout.discount_invalid_code",
    "wrong_scope": "marketplace.checkout.discount_wrong_scope",
    "expired": "marketplace.checkout.discount_expired",
    "usage_limit": "marketplace.checkout.discount_usage_limit",
    "invalid_value": "marketplace.checkout.discount_invalid_value",
    "invalid_amount": "marketplace.checkout.discount_invalid_amount",
}


def discount_error_message(error: DiscountError, translator: Translator) -> str:
    return translator.t(_DISCOUNT_ERROR_KEYS.get(error.code, "marketplace.checkout.discount_error"))


def purchase_session_state(session, user_id: int, product_type: str) -> str:
    """Describe an already retrieved session; this never grants product access."""
    if not session:
        return "unverified"
    metadata = session.metadata or {}
    buyer = metadata.get("buyer_user_id", metadata.get("user_id"))
    if buyer != str(user_id) or metadata.get("type") != f"{product_type}_purchase":
        return "unverified"
    status = getattr(session, "status", None)
    if status == "complete" and getattr(session, "payment_status", None) == "paid":
        return "paid"
    if status == "expired":
        return "not_completed"
    if status in {"open", "complete"}:
        return "pending"
    return "unverified"
