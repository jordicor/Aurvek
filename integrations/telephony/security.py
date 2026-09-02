"""Twilio signature validation against Aurvek's canonical public URLs.

Reverse-proxy request metadata is intentionally not accepted here.  Twilio
signs the exact URL configured for a webhook or WebSocket, so Aurvek rebuilds
that URL exclusively from ``PRIMARY_APP_DOMAIN`` plus the route path/query.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from twilio.request_validator import RequestValidator

import common


class TwilioCanonicalURLConfigurationError(RuntimeError):
    """Raised when a safe canonical Twilio URL cannot be constructed."""


def _validated_public_domain() -> str:
    domain = common.PRIMARY_APP_DOMAIN.strip()
    if not domain:
        raise TwilioCanonicalURLConfigurationError(
            "PRIMARY_APP_DOMAIN is required for Twilio webhook validation"
        )

    try:
        parsed = urlsplit(f"//{domain}")
        configured_port = parsed.port
    except ValueError as exc:
        raise TwilioCanonicalURLConfigurationError(
            "PRIMARY_APP_DOMAIN is not a valid public host"
        ) from exc
    if (
        not parsed.hostname
        or configured_port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.netloc != domain
    ):
        raise TwilioCanonicalURLConfigurationError(
            "PRIMARY_APP_DOMAIN must contain only the public host without a port"
        )
    return domain


def canonical_twilio_url(
    path: str,
    *,
    raw_query_string: str | bytes = "",
    websocket: bool = False,
) -> str:
    """Return the exact public URL used for Twilio signature validation.

    ``raw_query_string`` is intentionally not parsed or re-encoded.  Twilio's
    signature includes the original percent encoding, so callers should pass
    the ASGI raw query string (without the leading ``?``) when one exists.
    """

    if not path.startswith("/") or "?" in path or "#" in path:
        raise ValueError("path must be an absolute path without query or fragment")
    if any(ord(character) < 32 for character in path):
        raise ValueError("path cannot contain control characters")

    if isinstance(raw_query_string, bytes):
        try:
            query = raw_query_string.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("raw_query_string must be ASCII URL data") from exc
    else:
        query = raw_query_string

    if query.startswith("?") or "#" in query:
        raise ValueError("raw_query_string must not include '?' or a fragment")
    if any(ord(character) < 32 for character in query):
        raise ValueError("raw_query_string cannot contain control characters")

    scheme = "wss" if websocket else "https"
    url = f"{scheme}://{_validated_public_domain()}{path}"
    return f"{url}?{query}" if query else url


class TwilioSignatureVerifier:
    """Validate HTTP callbacks and Media Streams WebSocket handshakes."""

    def __init__(self, auth_token: str):
        if not auth_token:
            raise ValueError("auth_token is required")
        self._validator = RequestValidator(auth_token)

    def validate_http(
        self,
        *,
        path: str,
        signature: str,
        form_params: Mapping[str, Any] | Any,
        raw_query_string: str | bytes = "",
    ) -> bool:
        """Validate a form-encoded Twilio webhook using every received field."""

        if not signature:
            return False
        url = canonical_twilio_url(
            path,
            raw_query_string=raw_query_string,
            websocket=False,
        )
        return bool(self._validator.validate(url, form_params, signature))

    def validate_websocket(
        self,
        *,
        path: str,
        signature: str,
        raw_query_string: str | bytes = "",
    ) -> bool:
        """Validate the signed Media Streams WebSocket upgrade request."""

        if not signature:
            return False
        url = canonical_twilio_url(
            path,
            raw_query_string=raw_query_string,
            websocket=True,
        )
        return bool(self._validator.validate(url, {}, signature))
