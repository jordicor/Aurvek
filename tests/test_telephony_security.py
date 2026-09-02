from __future__ import annotations

import pytest
from twilio.request_validator import RequestValidator

import common
from integrations.telephony.security import (
    TwilioCanonicalURLConfigurationError,
    TwilioSignatureVerifier,
    canonical_twilio_url,
)


AUTH_TOKEN = "test-auth-token"


def test_http_signature_uses_canonical_domain_and_exact_query(monkeypatch) -> None:
    monkeypatch.setattr(common, "PRIMARY_APP_DOMAIN", "voice.example.test")
    path = "/webhooks/twilio/voice/status"
    raw_query = b"message=hello%20world&type=test%2Bvalue"
    params = {
        "CallSid": "CA123",
        "CallStatus": "ringing",
    }
    canonical_url = (
        "https://voice.example.test/webhooks/twilio/voice/status"
        "?message=hello%20world&type=test%2Bvalue"
    )
    signature = RequestValidator(AUTH_TOKEN).compute_signature(
        canonical_url,
        params,
    )

    verifier = TwilioSignatureVerifier(AUTH_TOKEN)
    assert verifier.validate_http(
        path=path,
        raw_query_string=raw_query,
        form_params=params,
        signature=signature,
    )
    assert not verifier.validate_http(
        path=path,
        raw_query_string=raw_query,
        form_params={**params, "CallStatus": "completed"},
        signature=signature,
    )


def test_http_signature_never_accepts_an_attacker_host(monkeypatch) -> None:
    monkeypatch.setattr(common, "PRIMARY_APP_DOMAIN", "voice.example.test")
    params = {"CallSid": "CA123"}
    attacker_signature = RequestValidator(AUTH_TOKEN).compute_signature(
        "https://attacker.example/webhooks/twilio/voice/inbound",
        params,
    )

    assert not TwilioSignatureVerifier(AUTH_TOKEN).validate_http(
        path="/webhooks/twilio/voice/inbound",
        form_params=params,
        signature=attacker_signature,
    )


def test_websocket_signature_uses_wss_canonical_url(monkeypatch) -> None:
    monkeypatch.setattr(common, "PRIMARY_APP_DOMAIN", "voice.example.test")
    websocket_url = "wss://voice.example.test/ws/twilio/media-stream"
    signature = RequestValidator(AUTH_TOKEN).compute_signature(websocket_url, {})
    https_signature = RequestValidator(AUTH_TOKEN).compute_signature(
        "https://voice.example.test/ws/twilio/media-stream",
        {},
    )
    verifier = TwilioSignatureVerifier(AUTH_TOKEN)

    assert verifier.validate_websocket(
        path="/ws/twilio/media-stream",
        signature=signature,
    )
    assert not verifier.validate_websocket(
        path="/ws/twilio/media-stream",
        signature=https_signature,
    )
    assert not verifier.validate_websocket(
        path="/ws/twilio/media-stream",
        signature="",
    )


def test_missing_or_malformed_canonical_domain_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(common, "PRIMARY_APP_DOMAIN", "")
    with pytest.raises(TwilioCanonicalURLConfigurationError):
        canonical_twilio_url("/webhooks/twilio/voice/status")

    monkeypatch.setattr(common, "PRIMARY_APP_DOMAIN", "voice.example.test:443")
    with pytest.raises(TwilioCanonicalURLConfigurationError):
        canonical_twilio_url("/webhooks/twilio/voice/status")

    monkeypatch.setattr(
        common,
        "PRIMARY_APP_DOMAIN",
        "https://voice.example.test/callback",
    )
    with pytest.raises(TwilioCanonicalURLConfigurationError):
        canonical_twilio_url("/webhooks/twilio/voice/status")


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("https://attacker.example/callback", ""),
        ("/callback?CallSid=CA123", ""),
        ("/callback", "?CallSid=CA123"),
        ("/callback", "value\nother"),
    ],
)
def test_canonical_url_rejects_ambiguous_path_or_query(
    monkeypatch,
    path,
    query,
) -> None:
    monkeypatch.setattr(common, "PRIMARY_APP_DOMAIN", "voice.example.test")
    with pytest.raises(ValueError):
        canonical_twilio_url(path, raw_query_string=query)
