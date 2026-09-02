from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest

from integrations.telephony.deepgram_live import (
    DeepgramConnectionClosed,
    DeepgramLiveClient,
    DeepgramLiveOptions,
    DeepgramMetadataEvent,
    DeepgramProtocolError,
    DeepgramProviderError,
    DeepgramSpeechStartedEvent,
    DeepgramTranscriptEvent,
    DeepgramUtteranceEndEvent,
    DeepgramWarningEvent,
    parse_deepgram_message,
)


class FakeWebSocket:
    def __init__(self, messages=(), *, close_code=None, provider_exception=None):
        self.messages = list(messages)
        self.sent_bytes = []
        self.sent_json = []
        self.closed = False
        self.close_code = close_code
        self.provider_exception = provider_exception

    async def send_bytes(self, payload):
        self.sent_bytes.append(payload)

    async def send_json(self, payload):
        self.sent_json.append(payload)

    async def receive(self):
        return self.messages.pop(0)

    async def close(self):
        self.closed = True

    def exception(self):
        return self.provider_exception


class FakeSession:
    def __init__(self, websocket):
        self.websocket = websocket
        self.connect_url = None
        self.connect_kwargs = None
        self.closed = False

    async def ws_connect(self, url, **kwargs):
        self.connect_url = url
        self.connect_kwargs = kwargs
        return self.websocket

    async def close(self):
        self.closed = True


def ws_message(message_type, data=None, extra=None):
    return SimpleNamespace(type=message_type, data=data, extra=extra)


def test_options_lock_nova3_raw_mulaw_8khz_and_explicit_language():
    options = DeepgramLiveOptions(language="es-ES", endpointing_ms=450)
    query = parse_qs(urlsplit(options.websocket_url()).query)

    assert query == {
        "model": ["nova-3"],
        "encoding": ["mulaw"],
        "sample_rate": ["8000"],
        "channels": ["1"],
        "language": ["es-ES"],
        "interim_results": ["true"],
        "vad_events": ["true"],
        "endpointing": ["450"],
        "punctuate": ["true"],
    }
    assert DeepgramLiveOptions().language == "multi"


@pytest.mark.parametrize("language", ["", "auto", "es ES", "multi&model=other"])
def test_invalid_language_fails_before_opening_a_socket(language):
    with pytest.raises(ValueError):
        DeepgramLiveOptions(language=language)


def test_results_parser_preserves_interim_final_and_word_timing():
    event = parse_deepgram_message(
        {
            "type": "Results",
            "is_final": True,
            "speech_final": True,
            "from_finalize": False,
            "start": 1.25,
            "duration": 0.75,
            "channel": {
                "alternatives": [
                    {
                        "transcript": "Hola, mundo.",
                        "confidence": 0.97,
                        "words": [
                            {
                                "word": "hola",
                                "punctuated_word": "Hola,",
                                "start": 1.25,
                                "end": 1.5,
                                "confidence": 0.99,
                            },
                            {
                                "word": "mundo",
                                "punctuated_word": "mundo.",
                                "start": 1.55,
                                "end": 2.0,
                                "confidence": 0.95,
                            },
                        ],
                    }
                ]
            },
        }
    )

    assert isinstance(event, DeepgramTranscriptEvent)
    assert event.text == "Hola, mundo."
    assert event.is_final is True
    assert event.speech_final is True
    assert event.from_finalize is False
    assert event.start_seconds == 1.25
    assert event.duration_seconds == 0.75
    assert event.words[0].text == "Hola,"
    assert event.words[1].end_seconds == 2.0


def test_parser_exposes_vad_utterance_metadata_and_warning_events():
    speech = parse_deepgram_message({"type": "SpeechStarted", "timestamp": 2.5})
    utterance = parse_deepgram_message(
        {"type": "UtteranceEnd", "last_word_end": 4.2}
    )
    metadata = parse_deepgram_message(
        {"type": "Metadata", "request_id": "req-1", "duration": 9.0}
    )
    warning = parse_deepgram_message(
        {"type": "Warning", "warn_code": "W1", "warn_msg": "synthetic"}
    )

    assert speech == DeepgramSpeechStartedEvent(timestamp_seconds=2.5)
    assert utterance == DeepgramUtteranceEndEvent(last_word_end_seconds=4.2)
    assert metadata == DeepgramMetadataEvent(
        request_id="req-1", duration_seconds=9.0
    )
    assert warning == DeepgramWarningEvent(code="W1", description="synthetic")


def test_parser_fails_closed_for_provider_error_or_malformed_results():
    with pytest.raises(DeepgramProviderError) as provider_error:
        parse_deepgram_message(
            {"type": "Error", "code": "BAD_REQUEST", "description": "detail"}
        )
    assert provider_error.value.code == "BAD_REQUEST"
    assert "detail" not in str(provider_error.value)

    with pytest.raises(DeepgramProtocolError):
        parse_deepgram_message(
            {"type": "Results", "channel": {"alternatives": []}}
        )
    with pytest.raises(DeepgramProtocolError):
        parse_deepgram_message("not-json")


@pytest.mark.asyncio
async def test_client_connects_directly_sends_audio_and_yields_typed_events():
    results = {
        "type": "Results",
        "is_final": False,
        "speech_final": False,
        "channel": {"alternatives": [{"transcript": "hel", "words": []}]},
    }
    websocket = FakeWebSocket(
        [
            ws_message(aiohttp.WSMsgType.TEXT, json.dumps(results)),
            ws_message(aiohttp.WSMsgType.TEXT, json.dumps({"type": "SpeechStarted"})),
            ws_message(aiohttp.WSMsgType.CLOSE, 1000),
        ]
    )
    session = FakeSession(websocket)
    client = DeepgramLiveClient(
        "synthetic-key",
        options=DeepgramLiveOptions(language="multi"),
        session=session,
    )

    await client.connect()
    await client.send_audio(b"\xff" * 160)
    await client.keep_alive()
    await client.finalize()
    events = [event async for event in client.events()]
    await client.close()

    query = parse_qs(urlsplit(session.connect_url).query)
    assert query["model"] == ["nova-3"]
    assert query["language"] == ["multi"]
    assert session.connect_kwargs["headers"]["Authorization"] == "Token synthetic-key"
    assert websocket.sent_bytes == [b"\xff" * 160]
    assert websocket.sent_json == [
        {"type": "KeepAlive"},
        {"type": "Finalize"},
        {"type": "CloseStream"},
    ]
    assert isinstance(events[0], DeepgramTranscriptEvent)
    assert events[0].is_final is False
    assert isinstance(events[1], DeepgramSpeechStartedEvent)
    assert websocket.closed is True
    assert session.closed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("close_code", [1000, 1001])
async def test_normal_close_frame_ends_event_stream(close_code):
    websocket = FakeWebSocket(
        [ws_message(aiohttp.WSMsgType.CLOSE, close_code)]
    )
    client = DeepgramLiveClient(
        "synthetic-key", session=FakeSession(websocket)
    )
    await client.connect()

    assert [event async for event in client.events()] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "provider_exception"),
    [
        (ws_message(aiohttp.WSMsgType.CLOSE, 1011), None),
        (
            ws_message(
                aiohttp.WSMsgType.CLOSE,
                1000,
                extra="sensitive provider close reason",
            ),
            None,
        ),
        (
            ws_message(aiohttp.WSMsgType.CLOSED, 1000),
            RuntimeError("sensitive provider exception"),
        ),
    ],
)
async def test_abnormal_close_reason_or_exception_is_visible_without_payload(
    message,
    provider_exception,
):
    websocket = FakeWebSocket(
        [message],
        provider_exception=provider_exception,
    )
    client = DeepgramLiveClient(
        "synthetic-key", session=FakeSession(websocket)
    )
    await client.connect()

    with pytest.raises(DeepgramConnectionClosed) as captured:
        [event async for event in client.events()]

    assert "sensitive" not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.asyncio
async def test_client_owns_factory_session_and_close_is_idempotent():
    websocket = FakeWebSocket()
    session = FakeSession(websocket)
    client = DeepgramLiveClient("synthetic-key", session_factory=lambda: session)

    async with client:
        assert client.connected is True

    await client.close()
    assert websocket.closed is True
    assert session.closed is True
    with pytest.raises(DeepgramConnectionClosed):
        await client.send_audio(b"\xff")


@pytest.mark.asyncio
async def test_cancelled_connect_closes_owned_session_and_propagates_cancellation():
    websocket = FakeWebSocket()
    session = FakeSession(websocket)

    async def cancelled_connect(url, **kwargs):
        raise asyncio.CancelledError

    session.ws_connect = cancelled_connect
    client = DeepgramLiveClient(
        "synthetic-key",
        session_factory=lambda: session,
    )

    with pytest.raises(asyncio.CancelledError):
        await client.connect()

    assert session.closed is True
    assert client.connected is False


@pytest.mark.asyncio
async def test_operations_require_an_open_connection():
    client = DeepgramLiveClient(
        "synthetic-key", session=FakeSession(FakeWebSocket())
    )

    with pytest.raises(DeepgramConnectionClosed):
        await client.send_audio(b"\xff")
    with pytest.raises(DeepgramConnectionClosed):
        await client.finalize()


@pytest.mark.asyncio
async def test_audio_input_must_be_bytes_like():
    websocket = FakeWebSocket()
    client = DeepgramLiveClient(
        "synthetic-key", session=FakeSession(websocket)
    )
    await client.connect()

    with pytest.raises(ValueError, match="bytes-like"):
        await client.send_audio(160)
    assert websocket.sent_bytes == []
