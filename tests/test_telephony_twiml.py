from __future__ import annotations

from xml.etree import ElementTree

import pytest

from integrations.telephony.twiml import (
    MediaStreamCorrelation,
    build_media_stream_twiml,
)


def make_twiml(**overrides) -> str:
    values = {
        "stream_url": "wss://aurvek.example/ws/twilio/media-stream",
        "connect_action_url": (
            "https://aurvek.example/webhooks/twilio/voice/connect-action"
        ),
        "stream_status_callback_url": (
            "https://aurvek.example/webhooks/twilio/voice/stream-status"
        ),
        "correlation": MediaStreamCorrelation(
            correlation_token="opaque-call-token",
            stream_attempt=0,
        ),
    }
    values.update(overrides)
    return build_media_stream_twiml(**values)


def test_media_stream_twiml_has_one_bidirectional_connect_path() -> None:
    root = ElementTree.fromstring(make_twiml())

    assert root.tag == "Response"
    assert len(root) == 1
    connect = root.find("Connect")
    assert connect is not None
    assert connect.attrib == {
        "action": "https://aurvek.example/webhooks/twilio/voice/connect-action",
        "method": "POST",
    }
    assert len(connect) == 1
    stream = connect.find("Stream")
    assert stream is not None
    assert stream.attrib == {
        "name": "aurvek-phone-0",
        "statusCallback": (
            "https://aurvek.example/webhooks/twilio/voice/stream-status"
        ),
        "statusCallbackMethod": "POST",
        "url": "wss://aurvek.example/ws/twilio/media-stream",
    }
    assert [(item.attrib["name"], item.attrib["value"]) for item in stream] == [
        ("correlation_token", "opaque-call-token"),
        ("stream_attempt", "0"),
    ]


def test_reconnection_attempt_is_carried_as_a_parameter() -> None:
    root = ElementTree.fromstring(
        make_twiml(
            correlation=MediaStreamCorrelation(
                correlation_token="opaque-call-token",
                stream_attempt=2,
            )
        )
    )

    parameters = {
        item.attrib["name"]: item.attrib["value"]
        for item in root.find("Connect").find("Stream")
    }
    assert parameters == {
        "correlation_token": "opaque-call-token",
        "stream_attempt": "2",
    }
    assert root.find("Connect").find("Stream").attrib["name"] == "aurvek-phone-2"


@pytest.mark.parametrize(
    "overrides",
    [
        {"stream_url": "ws://aurvek.example/ws/twilio/media-stream"},
        {
            "stream_url": (
                "wss://aurvek.example/ws/twilio/media-stream?auth=not-allowed"
            )
        },
        {"connect_action_url": "http://aurvek.example/connect-action"},
        {"stream_status_callback_url": "//aurvek.example/stream-status"},
    ],
)
def test_twiml_rejects_unsafe_transport_or_callback_urls(overrides) -> None:
    with pytest.raises(ValueError):
        make_twiml(**overrides)


@pytest.mark.parametrize(
    "correlation",
    [
        {"correlation_token": "", "stream_attempt": 0},
        {"correlation_token": " token", "stream_attempt": 0},
        {"correlation_token": "opaque token", "stream_attempt": 0},
        {"correlation_token": "opaque", "stream_attempt": -1},
        {"correlation_token": "opaque", "stream_attempt": 3},
        {"correlation_token": "opaque", "stream_attempt": True},
        {"correlation_token": "opaque", "stream_attempt": 1.5},
        {"correlation_token": 123, "stream_attempt": 0},
    ],
)
def test_correlation_parameters_are_narrow_and_bounded(correlation) -> None:
    with pytest.raises(ValueError):
        MediaStreamCorrelation(**correlation)
