"""Same-origin and session-bound CSRF protection for state-changing routes.

This module intentionally lives outside optional/private integrations so the
open-source build can protect its core mutation endpoints without importing
those integrations.
"""

from __future__ import annotations

import os
import secrets
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

CSRF_HEADER = "X-GPTSub-CSRF"
CSRF_SESSION_KEY = "gptsub_csrf_token"
PRIMARY_APP_DOMAIN = os.getenv("PRIMARY_APP_DOMAIN", "").strip()


def ensure_csrf_token(request: Request) -> str:
    """Return the session's opaque CSRF token, creating it when necessary."""
    token = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def validate_mutation_request(
    request: Request,
    *,
    supplied_token: str | None = None,
) -> JSONResponse | None:
    """Reject a cross-site or un-tokened state-changing request.

    The synchronizer token is the primary defense. JSON clients send it in the
    canonical header; native HTML forms may pass their hidden field explicitly.
    Origin/Referer and Fetch Metadata are checked as a second independent signal.
    Proxy forwarding headers are deliberately ignored here: unless ASGI has first
    replaced the trusted request scope, they are attacker-controlled input. A
    configured primary app domain is an exact HTTPS origin and remains authoritative
    across TLS-terminating proxies; otherwise the comparison uses the sanitized ASGI
    scheme and HTTP ``Host`` authority.
    """
    expected = request.session.get(CSRF_SESSION_KEY)
    supplied = request.headers.get(CSRF_HEADER)
    if supplied is None:
        supplied = supplied_token
    if (
        not isinstance(expected, str)
        or not isinstance(supplied, str)
        or not secrets.compare_digest(expected, supplied)
    ):
        return _forbidden("Invalid or missing CSRF token.")

    fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if fetch_site in {"cross-site", "none"}:
        return _forbidden("Cross-site request rejected.")

    source = request.headers.get("origin") or request.headers.get("referer")
    if not source or not _same_origin(request, source):
        return _forbidden("Request origin does not match Aurvek.")
    return None


def _same_origin(request: Request, source: str) -> bool:
    if not isinstance(source, str) or any(ord(ch) < 0x20 for ch in source):
        return False
    try:
        parsed = urlsplit(source)
    except ValueError:
        return False
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False

    source_origin = _normalize_origin(parsed)
    if source_origin is None:
        return False

    if PRIMARY_APP_DOMAIN:
        configured_origin = _configured_primary_origin(PRIMARY_APP_DOMAIN)
        if configured_origin is None or source_origin != configured_origin:
            return False
        expected_authority = (
            request.headers.get("host") or request.url.netloc
        ).strip()
        try:
            host_origin = _normalize_origin(urlsplit(f"https://{expected_authority}"))
        except ValueError:
            return False
        return host_origin == configured_origin

    expected_scheme = request.url.scheme.strip().lower()
    expected_authority = (request.headers.get("host") or request.url.netloc).strip()
    if not expected_authority or expected_scheme not in {"http", "https"}:
        return False
    try:
        request_origin = _normalize_origin(
            urlsplit(f"{expected_scheme}://{expected_authority}")
        )
    except ValueError:
        return False
    return source_origin == request_origin


def _configured_primary_origin(domain: str) -> tuple[str, str, int] | None:
    """Return the exact HTTPS origin for a domain-only deployment setting."""
    if (
        not isinstance(domain, str)
        or not domain
        or domain != domain.strip()
        or any(ord(ch) < 0x21 for ch in domain)
        or any(ch in domain for ch in "/?#@")
    ):
        return None
    try:
        parsed = urlsplit(f"https://{domain}")
        if parsed.port is not None:
            return None
    except ValueError:
        return None
    if parsed.netloc != domain or parsed.path or parsed.query or parsed.fragment:
        return None
    return _normalize_origin(parsed)


def _normalize_origin(parsed) -> tuple[str, str, int] | None:
    """Return a comparison-safe (scheme, IDNA host, effective port) tuple."""
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    if not host or any(ch.isspace() for ch in host):
        return None
    return scheme, host, port or (443 if scheme == "https" else 80)


def _forbidden(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"status": "forbidden", "message": message},
    )
