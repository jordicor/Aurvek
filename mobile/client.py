"""Helpers for native mobile clients.

The web app remains the source of truth, but native clients need a stable way
to discover platform-specific capabilities before rendering purchase or auth UI.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi.responses import JSONResponse

from auth_apple import get_apple_sign_in_status


IOS_PURCHASE_DISABLED_REASON = "storekit_required"
IOS_PURCHASE_DISABLED_ERROR = "ios_purchases_disabled"

_TRUE_VALUES = {"1", "true", "yes", "y", "on", "enabled"}
_IOS_CLIENT_VALUES = {"ios", "iphone", "ipad", "aurvek-ios", "aurvek-ios-app"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


def _first_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _request_header(request: Any, name: str, default: str = "") -> str:
    headers = getattr(request, "headers", {}) or {}
    try:
        return headers.get(name, default)
    except AttributeError:
        return default


def _normalize_base_url(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        return f"https://{value}"
    return value


def configured_public_base_url() -> str:
    """Return the configured canonical public base URL, if available."""
    return _normalize_base_url(
        _first_env(
            "MOBILE_PUBLIC_BASE_URL",
            "AURVEK_PUBLIC_BASE_URL",
            "PUBLIC_BASE_URL",
            "PRIMARY_APP_DOMAIN",
        )
    )


def request_public_base_url(request: Any) -> str:
    """Build a stable public base URL from env, Cloudflare headers, or request."""
    configured = configured_public_base_url()
    if configured:
        return configured

    scheme = _request_header(request, "x-forwarded-proto")
    if not scheme:
        scheme = "https" if _request_header(request, "cf-connecting-ip") else getattr(getattr(request, "url", None), "scheme", "http")

    host = _request_header(request, "host")
    if not host:
        host = getattr(getattr(request, "url", None), "hostname", "localhost")

    if _request_header(request, "x-forwarded-proto") or _request_header(request, "cf-connecting-ip"):
        return f"{scheme}://{host}".rstrip("/")

    port = getattr(getattr(request, "url", None), "port", None)
    if port and port not in (80, 443) and ":" not in host:
        return f"{scheme}://{host}:{port}".rstrip("/")
    return f"{scheme}://{host}".rstrip("/")


def is_ios_client(request: Any) -> bool:
    """Detect the native iOS app by explicit headers, with UA fallback."""
    client = _request_header(request, "x-aurvek-client").strip().lower()
    platform = _request_header(request, "x-aurvek-platform").strip().lower()
    user_agent = _request_header(request, "user-agent").strip().lower()

    if client in _IOS_CLIENT_VALUES or platform in {"ios", "ipados"}:
        return True
    return "aurvek-ios" in user_agent


def ios_purchases_enabled() -> bool:
    """True only after StoreKit/iOS purchase handling is explicitly enabled."""
    return _env_bool("IOS_PURCHASES_ENABLED", default=False)


def ios_purchase_blocked(request: Any) -> bool:
    return is_ios_client(request) and not ios_purchases_enabled()


def ios_purchase_disabled_response(*, message: str | None = None) -> JSONResponse:
    return JSONResponse(
        {
            "error": IOS_PURCHASE_DISABLED_ERROR,
            "reason": IOS_PURCHASE_DISABLED_REASON,
            "message": message if message is not None else "Purchases are unavailable in the iOS client until in-app purchases are configured.",
            "purchase_available": False,
        },
        status_code=409,
    )


def _price_is_positive(price: Any) -> bool:
    if price is None:
        return False
    try:
        return float(price) > 0
    except (TypeError, ValueError):
        return False


def purchase_metadata_for_request(
    request: Any,
    *,
    is_paid: bool,
    user_has_access: bool = False,
    price: Any = None,
) -> dict[str, Any]:
    """Return purchase availability fields safe to expose in catalog APIs."""
    paid = bool(is_paid) or _price_is_positive(price)

    if not paid or user_has_access:
        return {
            "purchase_available": False,
            "purchase_provider": None,
            "purchase_unavailable_reason": None,
        }

    if ios_purchase_blocked(request):
        return {
            "purchase_available": False,
            "purchase_provider": None,
            "purchase_unavailable_reason": IOS_PURCHASE_DISABLED_REASON,
        }

    return {
        "purchase_available": True,
        "purchase_provider": "storekit" if is_ios_client(request) else "stripe",
        "purchase_unavailable_reason": None,
    }


def legal_urls_for_request(request: Any) -> dict[str, Any]:
    base_url = request_public_base_url(request)
    support_email = _first_env("MOBILE_SUPPORT_EMAIL", "SUPPORT_EMAIL")
    if not support_email:
        host = base_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        support_email = f"support@{host}" if host and host != "localhost" else None

    return {
        "privacy_policy_url": _first_env("MOBILE_PRIVACY_URL", "PRIVACY_POLICY_URL", "APP_PRIVACY_URL") or f"{base_url}/privacy",
        "terms_url": _first_env("MOBILE_TERMS_URL", "TERMS_URL", "APP_TERMS_URL") or f"{base_url}/terms",
        "support_url": _first_env("MOBILE_SUPPORT_URL", "SUPPORT_URL", "APP_SUPPORT_URL") or f"{base_url}/support",
        "support_email": support_email,
    }


def mobile_config_payload(request: Any) -> dict[str, Any]:
    ios_client = is_ios_client(request)
    ios_storekit_ready = ios_purchases_enabled()
    apple_sign_in = get_apple_sign_in_status()
    return {
        "api_version": "mobile-v1",
        "platform": {
            "is_ios_client": ios_client,
            "client_header": _request_header(request, "x-aurvek-client") or None,
        },
        "features": {
            "chat_streaming": True,
            "attachments": True,
            "voice_calls": _env_bool("MOBILE_VOICE_CALLS_ENABLED", default=False),
            "delete_account": True,
            "native_purchases": (not ios_client) or ios_storekit_ready,
            "ios_storekit_purchases": ios_storekit_ready,
        },
        "auth": {
            "session_cookie": "session",
            "google_oauth_available": bool(os.getenv("GOOGLE_CLIENT_ID")),
            "apple_sign_in_available": bool(apple_sign_in["enabled"]),
            "apple_sign_in_required_for_ios_review": True,
            "apple_sign_in": apple_sign_in,
        },
        "purchase_policy": {
            "ios_purchases_enabled": ios_storekit_ready,
            "ios_purchases_unavailable_reason": None if ios_storekit_ready else IOS_PURCHASE_DISABLED_REASON,
        },
        "legal": legal_urls_for_request(request),
    }
