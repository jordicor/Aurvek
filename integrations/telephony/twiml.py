"""TwiML generation for Aurvek's sole native phone transport."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from twilio.twiml.voice_response import VoiceResponse


MAX_RECONNECT_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class MediaStreamCorrelation:
    """Non-secret values echoed by Twilio in the Media Streams start event."""

    correlation_token: str
    stream_attempt: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.correlation_token, str):
            raise ValueError("correlation_token must be a string")
        token = self.correlation_token.strip()
        if not token or token != self.correlation_token:
            raise ValueError("correlation_token must be a non-empty opaque value")
        if len(token) > 400 or any(ord(character) < 33 for character in token):
            raise ValueError("correlation_token is not a valid opaque value")
        if (
            isinstance(self.stream_attempt, bool)
            or not isinstance(self.stream_attempt, int)
            or not 0 <= self.stream_attempt <= MAX_RECONNECT_ATTEMPTS
        ):
            raise ValueError("stream_attempt must be between 0 and 2")


def build_media_stream_twiml(
    *,
    stream_url: str,
    connect_action_url: str,
    stream_status_callback_url: str,
    correlation: MediaStreamCorrelation,
) -> str:
    """Build bidirectional ``<Connect><Stream>`` TwiML.

    Correlation travels in typed ``<Parameter>`` children rather than the
    WebSocket query string.  Credentials and arbitrary caller-supplied fields
    cannot be added through this API.
    """

    _require_url(
        stream_url,
        scheme="wss",
        field_name="stream_url",
        forbid_query=True,
    )
    _require_url(
        connect_action_url,
        scheme="https",
        field_name="connect_action_url",
    )
    _require_url(
        stream_status_callback_url,
        scheme="https",
        field_name="stream_status_callback_url",
    )
    response = VoiceResponse()
    connect = response.connect(action=connect_action_url, method="POST")
    stream = connect.stream(
        url=stream_url,
        # Twilio requires Stream names to be unique within a Call.  Including
        # the bounded attempt number keeps both reconnects valid without
        # exposing a call identifier in the name.
        name=f"aurvek-phone-{correlation.stream_attempt}",
        status_callback=stream_status_callback_url,
        status_callback_method="POST",
    )
    stream.parameter(
        name="correlation_token",
        value=correlation.correlation_token,
    )
    stream.parameter(
        name="stream_attempt",
        value=str(correlation.stream_attempt),
    )
    return str(response)


def _require_url(
    value: str,
    *,
    scheme: str,
    field_name: str,
    forbid_query: bool = False,
) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != scheme
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (forbid_query and parsed.query)
    ):
        suffix = " without query parameters" if forbid_query else ""
        raise ValueError(
            f"{field_name} must be an absolute {scheme.upper()} URL{suffix}"
        )
