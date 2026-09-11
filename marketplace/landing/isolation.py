"""Isolation helpers for creator-authored marketplace content.

Creator HTML, CSS and JavaScript are intentionally treated as untrusted.  They
must never be rendered on an Aurvek application origin because doing so would
give the creator's JavaScript access to that origin's cookies and APIs.

The shared creator-content origin is opt-in.  ``CREATOR_CONTENT_ORIGIN`` must
be an HTTPS origin on a different site from every configured Aurvek primary
domain.  If that cannot be established conservatively, callers fail closed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import time
from dataclasses import dataclass
from html import escape
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

from fastapi.responses import HTMLResponse, Response


_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_TOKEN_DEFAULT_MAX_LIFETIME_SECONDS = 15 * 60
_TOKEN_PURPOSE_MAX_LIFETIME_SECONDS = {
    # Welcome worlds can stay open for a working session and may request lazy
    # media or byte ranges long after the first document load.
    "welcome": 8 * 60 * 60,
}


@dataclass(frozen=True, slots=True)
class CreatorContentConfig:
    origin: str
    host: str
    primary_origin: str
    primary_host: str


def _normalize_host(value: str | None) -> str:
    host = (value or "").strip().lower().rstrip(".")
    if ":" in host and not host.startswith("["):
        host = host.split(":", 1)[0]
    return host


def _configured_primary_hosts() -> set[str]:
    values = {
        os.getenv("PRIMARY_APP_DOMAIN", ""),
        os.getenv("CLOUDFLARE_DOMAIN", ""),
    }
    values.update(os.getenv("AURVEK_PRIMARY_DOMAINS", "").split(","))
    return {host for value in values if (host := _normalize_host(value))}


def _conservative_site_suffix(host: str) -> str | None:
    """Return a conservative site suffix used only to reject unsafe hosts.

    The repository deliberately does not add a public-suffix dependency for
    this check.  Comparing the last two labels can reject some valid domains
    such as independent ``*.co.uk`` sites, but it does not approve sibling
    subdomains of the same ordinary registrable domain.  False negatives are
    preferable here because the feature remains safely disabled.
    """
    labels = host.split(".")
    if len(labels) < 2 or any(not label for label in labels):
        return None
    return ".".join(labels[-2:])


def _valid_public_hostname(host: str) -> bool:
    if not host or not _HOST_RE.fullmatch(host):
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return _conservative_site_suffix(host) is not None


def is_host_isolated_from_primary(
    host: str,
    *,
    primary_hosts: set[str] | None = None,
) -> bool:
    """Return whether ``host`` is conservatively separate from Aurvek sites."""
    host = _normalize_host(host)
    primaries = primary_hosts if primary_hosts is not None else _configured_primary_hosts()
    primaries = {_normalize_host(item) for item in primaries if _normalize_host(item)}
    valid_primaries = {item for item in primaries if _valid_public_hostname(item)}
    if not _valid_public_hostname(host) or not valid_primaries:
        return False

    host_site = _conservative_site_suffix(host)
    for primary in valid_primaries:
        if host == primary:
            return False
        if host.endswith("." + primary) or primary.endswith("." + host):
            return False
        if host_site == _conservative_site_suffix(primary):
            return False
    return True


def get_creator_content_config(
    *,
    primary_hosts: set[str] | None = None,
) -> CreatorContentConfig | None:
    """Resolve and validate the opt-in creator-content origin."""
    raw_origin = os.getenv("CREATOR_CONTENT_ORIGIN", "").strip()
    if not raw_origin:
        return None

    try:
        parsed = urlsplit(raw_origin)
        parsed_port = parsed.port
    except ValueError:
        return None

    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or (parsed_port not in (None, 443))
    ):
        return None

    host = _normalize_host(parsed.hostname)
    primaries = primary_hosts if primary_hosts is not None else _configured_primary_hosts()
    primaries = {_normalize_host(item) for item in primaries if _normalize_host(item)}
    if not is_host_isolated_from_primary(host, primary_hosts=primaries):
        return None

    primary_host = _normalize_host(os.getenv("PRIMARY_APP_DOMAIN", ""))
    if not primary_host or primary_host not in primaries:
        return None

    return CreatorContentConfig(
        origin=f"https://{host}",
        host=host,
        primary_origin=f"https://{primary_host}",
        primary_host=primary_host,
    )


def is_creator_content_host(host: str, *, primary_hosts: set[str] | None = None) -> bool:
    config = get_creator_content_config(primary_hosts=primary_hosts)
    return bool(config and _normalize_host(host) == config.host)


def is_creator_content_request(request: Any) -> bool:
    return bool(request is not None and getattr(request.state, "creator_content_origin", False))


def build_creator_content_url(path: str, query: Mapping[str, Any] | None = None) -> str | None:
    config = get_creator_content_config()
    if config is None or not path.startswith("/") or path.startswith("//"):
        return None
    suffix = ""
    if query:
        values = {key: str(value) for key, value in query.items() if value is not None}
        if values:
            suffix = "?" + urlencode(values)
    return config.origin + path + suffix


def primary_app_url(path: str) -> str | None:
    primary_host = _normalize_host(os.getenv("PRIMARY_APP_DOMAIN", ""))
    if (
        not _valid_public_hostname(primary_host)
        or not path.startswith("/")
        or path.startswith("//")
    ):
        return None
    return f"https://{primary_host}{path}"


def _token_secret() -> bytes | None:
    raw = os.getenv("APP_SECRET_KEY", "")
    if not raw:
        return None
    return hmac.new(raw.encode("utf-8"), b"aurvek-creator-content-v1", hashlib.sha256).digest()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def sign_content_token(payload: Mapping[str, Any], *, ttl_seconds: int = 300) -> str | None:
    """Create a short-lived, purpose-bound token for isolated content."""
    secret = _token_secret()
    if secret is None:
        return None
    max_lifetime = _TOKEN_PURPOSE_MAX_LIFETIME_SECONDS.get(
        str(payload.get("purpose") or ""),
        _TOKEN_DEFAULT_MAX_LIFETIME_SECONDS,
    )
    ttl_seconds = max(1, min(int(ttl_seconds), max_lifetime))
    now = int(time.time())
    body = dict(payload)
    body.update({"iat": now, "exp": now + ttl_seconds, "v": 1})
    encoded = _b64encode(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    signature = _b64encode(hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def verify_content_token(
    token: str | None,
    *,
    expected: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Validate a content token and optional exact payload bindings."""
    secret = _token_secret()
    if secret is None or not token or len(token) > 4096 or token.count(".") != 1:
        return None
    encoded, supplied_signature = token.split(".", 1)
    try:
        encoded_bytes = encoded.encode("ascii", "strict")
    except UnicodeEncodeError:
        return None
    expected_signature = _b64encode(
        hmac.new(secret, encoded_bytes, hashlib.sha256).digest()
    )
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return None
    try:
        payload = json.loads(_b64decode(encoded).decode("utf-8"))
        now = int(time.time())
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    if payload.get("v") != 1:
        return None
    if issued_at > now + 30 or expires_at < now:
        return None
    max_lifetime = _TOKEN_PURPOSE_MAX_LIFETIME_SECONDS.get(
        str(payload.get("purpose") or ""),
        _TOKEN_DEFAULT_MAX_LIFETIME_SECONDS,
    )
    if expires_at <= issued_at or expires_at - issued_at > max_lifetime:
        return None
    for key, value in (expected or {}).items():
        if payload.get(key) != value:
            return None
    return payload


def creator_content_unavailable_response(translator=None) -> HTMLResponse:
    """Return a non-sensitive fail-closed response for unconfigured isolation."""
    from i18n import Translator
    from html import escape

    translator = translator or Translator()
    return HTMLResponse(
        f"<!doctype html><html lang='{translator.language}'><head><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width,initial-scale=1'><title>"
        f"{escape(translator.t('public_shell.unavailable_title'))}</title>"
        f"</head><body><main><h1>{escape(translator.t('public_shell.unavailable'))}</h1>"
        f"<p>{escape(translator.t('public_shell.retry'))}</p></main></body></html>",
        status_code=503,
        headers={"Retry-After": "300", "Cache-Control": "no-store", "Content-Language": translator.language},
    )


def apply_creator_content_headers(
    response: Response,
    *,
    allow_primary_frame: bool = False,
    allow_cross_origin_resource: bool = False,
    no_store: bool = False,
) -> Response:
    """Apply defence-in-depth headers to an isolated creator response."""
    primary_url = primary_app_url("/")
    primary = primary_url.rstrip("/") if primary_url else "'none'"
    frame_ancestors = primary if allow_primary_frame and primary_url else "'none'"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self' https: data: blob:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https:; "
        "style-src 'self' 'unsafe-inline' https:; "
        "img-src 'self' https: data: blob:; "
        "font-src 'self' https: data:; "
        "media-src 'self' https: data: blob:; "
        "connect-src 'self' https:; object-src 'none'; base-uri 'none'; "
        f"form-action {primary}; frame-ancestors {frame_ancestors}"
    )
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = (
        "cross-origin"
        if allow_primary_frame or allow_cross_origin_resource
        else "same-origin"
    )
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=()"
    )
    response.headers["Referrer-Policy"] = "no-referrer" if no_store else "strict-origin-when-cross-origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if not allow_primary_frame:
        response.headers["X-Frame-Options"] = "DENY"
    elif "x-frame-options" in response.headers:
        del response.headers["X-Frame-Options"]
    if no_store:
        response.headers["Cache-Control"] = "no-store"
    return response


def safe_iframe_url(url: str) -> str:
    """Escape a validated URL for insertion into a trusted wrapper."""
    return escape(url, quote=True)
