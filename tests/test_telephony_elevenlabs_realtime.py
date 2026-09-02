from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
import threading
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest

from integrations.telephony import elevenlabs_realtime as realtime
from integrations.telephony.elevenlabs_realtime import (
    ELEVENLABS_AUDIO_CHUNK_BYTES,
    ElevenLabsConnectionClosed,
    ElevenLabsCredentialsError,
    ElevenLabsMetadataEvent,
    ElevenLabsProtocolError,
    ElevenLabsProviderError,
    ElevenLabsRealtimeClient,
    ElevenLabsRealtimeOptions,
    ElevenLabsSpeechStartedEvent,
    ElevenLabsTranscriptEvent,
    ElevenLabsUtteranceEndEvent,
    ElevenLabsWarningEvent,
    parse_elevenlabs_message,
)


class FakeWebSocket:
    def __init__(self, messages=None, *, close_waiter=None, receive_waiter=None):
        if messages is None:
            messages = [
                ws_message(aiohttp.WSMsgType.TEXT, json.dumps(session_started()))
            ]
        self.messages = list(messages)
        self.sent_json = []
        self.closed = False
        self.close_calls = 0
        self.close_waiter = close_waiter
        self.receive_waiter = receive_waiter

    async def send_json(self, payload):
        self.sent_json.append(payload)

    async def receive(self):
        if self.receive_waiter is not None:
            await self.receive_waiter.wait()
        return self.messages.pop(0)

    async def close(self):
        self.close_calls += 1
        if self.close_waiter is not None:
            await self.close_waiter.wait()
        self.closed = True


class FakeSession:
    def __init__(self, websocket, *, close_waiter=None):
        self.websocket = websocket
        self.connect_url = None
        self.connect_kwargs = None
        self.closed = False
        self.close_calls = 0
        self.close_waiter = close_waiter

    async def ws_connect(self, url, **kwargs):
        self.connect_url = url
        self.connect_kwargs = kwargs
        return self.websocket

    async def close(self):
        self.close_calls += 1
        if self.close_waiter is not None:
            await self.close_waiter.wait()
        self.closed = True


def ws_message(message_type, data=None, extra=None):
    return SimpleNamespace(type=message_type, data=data, extra=extra)


def session_started(*, language_code=None):
    return {
        "message_type": "session_started",
        "session_id": "sess-test",
        "config": {
            "audio_format": "ulaw_8000",
            "language_code": language_code,
            "model_id": "scribe_v2_realtime",
            "sample_rate": 8000,
        },
    }


async def next_events(iterator, count):
    return [await anext(iterator) for _ in range(count)]


def test_options_use_official_scribe_vad_and_raw_ulaw_without_auto_language():
    options = ElevenLabsRealtimeOptions(language="multi", endpointing_ms=700)
    query = parse_qs(urlsplit(options.websocket_url()).query)

    assert query == {
        "model_id": ["scribe_v2_realtime"],
        "audio_format": ["ulaw_8000"],
        "commit_strategy": ["vad"],
        "vad_silence_threshold_secs": ["0.7"],
    }
    assert "language_code" not in query
    assert ElevenLabsRealtimeOptions(language="auto").language_code is None


@pytest.mark.parametrize(
    ("locale", "expected"),
    [("es-ES", "es"), ("EN_us", "en"), ("spa", "spa")],
)
def test_fixed_locale_is_safely_normalized_to_iso_base(locale, expected):
    options = ElevenLabsRealtimeOptions(language=locale)
    query = parse_qs(urlsplit(options.websocket_url()).query)

    assert options.language_code == expected
    assert query["language_code"] == [expected]


@pytest.mark.parametrize(
    "language", ["", "english", "es ES", "multi&audio_format=pcm_16000"]
)
def test_invalid_language_fails_before_opening_a_socket(language):
    with pytest.raises(ValueError, match="language"):
        ElevenLabsRealtimeOptions(language=language)


@pytest.mark.parametrize("endpointing_ms", [299, 3001, True, 700.5])
def test_endpointing_respects_scribe_vad_limits(endpointing_ms):
    with pytest.raises(ValueError, match="endpointing_ms"):
        ElevenLabsRealtimeOptions(endpointing_ms=endpointing_ms)


@pytest.mark.parametrize("endpointing_ms", [300, 3_000])
def test_endpointing_accepts_official_scribe_vad_boundaries(endpointing_ms):
    assert ElevenLabsRealtimeOptions(endpointing_ms=endpointing_ms).endpointing_ms == (
        endpointing_ms
    )


@pytest.mark.asyncio
async def test_client_connects_to_official_endpoint_with_header_not_query_key():
    websocket = FakeWebSocket()
    session = FakeSession(websocket)
    client = ElevenLabsRealtimeClient("synthetic-secret", session=session)

    await client.connect()

    assert session.connect_url.startswith(
        "wss://api.elevenlabs.io/v1/speech-to-text/realtime?"
    )
    assert "synthetic-secret" not in session.connect_url
    assert session.connect_kwargs["headers"] == {
        "xi-api-key": "synthetic-secret",
        "User-Agent": "Aurvek-Telephony/1.0",
    }
    assert session.connect_kwargs["autoping"] is True


@pytest.mark.asyncio
async def test_sync_key_provider_runs_off_event_loop_and_async_provider_is_awaited():
    event_loop_thread = threading.get_ident()
    provider_threads = []

    def sync_provider():
        provider_threads.append(threading.get_ident())
        return "sync-secret"

    sync_session = FakeSession(FakeWebSocket())
    sync_client = ElevenLabsRealtimeClient(
        api_key_provider=sync_provider,
        session=sync_session,
    )
    await sync_client.connect()

    async def async_provider():
        await asyncio.sleep(0)
        return "async-secret"

    async_session = FakeSession(FakeWebSocket())
    async_client = ElevenLabsRealtimeClient(
        api_key_provider=async_provider,
        session=async_session,
    )
    await async_client.connect()

    assert provider_threads and provider_threads[0] != event_loop_thread
    assert sync_session.connect_kwargs["headers"]["xi-api-key"] == "sync-secret"
    assert async_session.connect_kwargs["headers"]["xi-api-key"] == "async-secret"


@pytest.mark.asyncio
async def test_total_connect_budget_bounds_hung_key_resolution() -> None:
    never = asyncio.Event()
    session_factory_calls = []

    async def key_provider():
        await never.wait()
        return "unreachable-secret"

    def session_factory():
        session_factory_calls.append(True)
        return FakeSession(FakeWebSocket())

    client = ElevenLabsRealtimeClient(
        api_key_provider=key_provider,
        session_factory=session_factory,
        connect_timeout_seconds=0.01,
    )

    with pytest.raises(ElevenLabsConnectionClosed, match="timed out"):
        await asyncio.wait_for(client.connect(), timeout=0.1)

    assert session_factory_calls == []
    assert client.connected is False


@pytest.mark.asyncio
async def test_repeated_sync_key_timeouts_reuse_one_bounded_worker_probe(
    monkeypatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    provider_calls = 0
    alternate_calls = 0

    def blocked_provider():
        nonlocal provider_calls
        provider_calls += 1
        entered.set()
        release.wait()
        return "eventual-secret"

    def alternate_provider():
        nonlocal alternate_calls
        alternate_calls += 1
        return "must-not-run"

    monkeypatch.setattr(realtime, "_SYNC_API_KEY_PROBE_CAPACITY", 1)

    first = ElevenLabsRealtimeClient(
        api_key_provider=blocked_provider,
        session_factory=lambda: FakeSession(FakeWebSocket()),
        connect_timeout_seconds=0.05,
    )
    try:
        with pytest.raises(ElevenLabsConnectionClosed, match="timed out"):
            await asyncio.wait_for(first.connect(), timeout=0.2)
        assert entered.is_set()

        for _ in range(10):
            repeated = ElevenLabsRealtimeClient(
                api_key_provider=blocked_provider,
                session_factory=lambda: FakeSession(FakeWebSocket()),
                connect_timeout_seconds=0.005,
            )
            with pytest.raises(ElevenLabsConnectionClosed, match="timed out"):
                await repeated.connect()

        saturated = ElevenLabsRealtimeClient(
            api_key_provider=alternate_provider,
            session_factory=lambda: FakeSession(FakeWebSocket()),
            connect_timeout_seconds=0.05,
        )
        with pytest.raises(ElevenLabsCredentialsError):
            await saturated.connect()

        assert provider_calls == 1
        assert alternate_calls == 0
        with realtime._SYNC_API_KEY_PROBE_GUARD:
            assert len(realtime._SYNC_API_KEY_PROBE_FLIGHTS) == 1
    finally:
        release.set()

    for _ in range(1_000):
        with realtime._SYNC_API_KEY_PROBE_GUARD:
            if not realtime._SYNC_API_KEY_PROBE_FLIGHTS:
                break
        await asyncio.sleep(0.001)
    else:
        raise AssertionError("sync adapter key probe did not release capacity")


@pytest.mark.asyncio
async def test_total_connect_budget_cleans_owned_session_after_hung_handshake() -> None:
    never = asyncio.Event()
    websocket = FakeWebSocket(messages=(), receive_waiter=never)
    session = FakeSession(websocket)
    client = ElevenLabsRealtimeClient(
        "synthetic-secret",
        session_factory=lambda: session,
        connect_timeout_seconds=0.01,
    )

    with pytest.raises(ElevenLabsConnectionClosed, match="timed out"):
        await asyncio.wait_for(client.connect(), timeout=0.1)

    assert session.closed is True
    assert websocket.closed is True
    assert client.connected is False


@pytest.mark.asyncio
async def test_client_is_not_connected_until_session_started_is_validated() -> None:
    release_handshake = asyncio.Event()
    websocket = FakeWebSocket(receive_waiter=release_handshake)
    client = ElevenLabsRealtimeClient(
        "synthetic-secret",
        session=FakeSession(websocket),
        connect_timeout_seconds=0.1,
    )

    connect_task = asyncio.create_task(client.connect())
    await asyncio.sleep(0)
    assert client.connected is False

    release_handshake.set()
    assert await connect_task is client
    assert client.connected is True


@pytest.mark.asyncio
async def test_connect_buffers_session_started_for_events_exactly_once() -> None:
    websocket = FakeWebSocket(
        [
            ws_message(aiohttp.WSMsgType.TEXT, json.dumps(session_started())),
            ws_message(
                aiohttp.WSMsgType.TEXT,
                json.dumps({"message_type": "warning", "warning": "test warning"}),
            ),
        ]
    )
    client = ElevenLabsRealtimeClient(
        "synthetic-secret", session=FakeSession(websocket)
    )

    await client.connect()
    iterator = client.events()

    assert isinstance(await anext(iterator), ElevenLabsMetadataEvent)
    assert isinstance(await anext(iterator), ElevenLabsWarningEvent)


@pytest.mark.asyncio
async def test_raw_twilio_bytes_are_buffered_to_100ms_and_preserved_exactly():
    websocket = FakeWebSocket()
    client = ElevenLabsRealtimeClient(
        "synthetic-secret", session=FakeSession(websocket)
    )
    await client.connect()
    source = bytes(index % 256 for index in range(ELEVENLABS_AUDIO_CHUNK_BYTES + 37))

    await client.send_audio(source[:319])
    assert websocket.sent_json == []
    await client.send_audio(source[319:])

    assert len(websocket.sent_json) == 1
    sent = websocket.sent_json[0]
    assert sent["message_type"] == "input_audio_chunk"
    assert "commit" not in sent
    assert base64.b64decode(sent["audio_base_64"]) == source[:800]
    assert client.pending_audio_bytes == 37


@pytest.mark.asyncio
async def test_finalize_flushes_remainder_with_commit_and_marks_final_event():
    websocket = FakeWebSocket(
        [
            ws_message(aiohttp.WSMsgType.TEXT, json.dumps(session_started())),
            ws_message(
                aiohttp.WSMsgType.TEXT,
                json.dumps(
                    {"message_type": "committed_transcript", "text": "Hola"}
                ),
            ),
        ]
    )
    client = ElevenLabsRealtimeClient(
        "synthetic-secret", session=FakeSession(websocket)
    )
    await client.connect()
    await client.send_audio(b"\x01\x02\x03")
    await client.finalize()

    assert websocket.sent_json == [
        {
            "message_type": "input_audio_chunk",
            "audio_base_64": base64.b64encode(b"\x01\x02\x03").decode("ascii"),
            "commit": True,
        }
    ]
    assert client.pending_audio_bytes == 0

    iterator = client.events()
    metadata, speech_start, transcript, utterance_end = await next_events(iterator, 4)
    assert isinstance(metadata, ElevenLabsMetadataEvent)
    assert isinstance(speech_start, ElevenLabsSpeechStartedEvent)
    assert transcript == ElevenLabsTranscriptEvent(
        text="Hola", is_final=True, speech_final=True, from_finalize=True
    )
    assert isinstance(utterance_end, ElevenLabsUtteranceEndEvent)


@pytest.mark.asyncio
async def test_finalize_after_exact_chunk_sends_an_empty_explicit_commit():
    websocket = FakeWebSocket()
    client = ElevenLabsRealtimeClient(
        "synthetic-secret", session=FakeSession(websocket)
    )
    await client.connect()
    await client.send_audio(b"\xff" * ELEVENLABS_AUDIO_CHUNK_BYTES)
    await client.finalize()

    assert base64.b64decode(websocket.sent_json[0]["audio_base_64"]) == (
        b"\xff" * ELEVENLABS_AUDIO_CHUNK_BYTES
    )
    assert websocket.sent_json[1] == {
        "message_type": "input_audio_chunk",
        "audio_base_64": "",
        "commit": True,
    }


@pytest.mark.asyncio
async def test_event_stream_synthesizes_one_speech_start_per_segment():
    provider_messages = [
        session_started(),
        {"message_type": "partial_transcript", "text": ""},
        {"message_type": "partial_transcript", "text": "ho"},
        {"message_type": "partial_transcript", "text": "hola"},
        {"message_type": "committed_transcript", "text": "hola"},
        {"message_type": "committed_transcript", "text": "otra vez"},
    ]
    websocket = FakeWebSocket(
        [
            ws_message(aiohttp.WSMsgType.TEXT, json.dumps(message))
            for message in provider_messages
        ]
    )
    client = ElevenLabsRealtimeClient(
        "synthetic-secret", session=FakeSession(websocket)
    )
    await client.connect()
    iterator = client.events()

    events = await next_events(iterator, 10)

    assert isinstance(events[0], ElevenLabsMetadataEvent)
    assert events[1] == ElevenLabsTranscriptEvent(
        text="", is_final=False, speech_final=False
    )
    assert isinstance(events[2], ElevenLabsSpeechStartedEvent)
    assert events[3].text == "ho"
    assert events[4].text == "hola"
    assert events[5].is_final is True
    assert isinstance(events[6], ElevenLabsUtteranceEndEvent)
    assert isinstance(events[7], ElevenLabsSpeechStartedEvent)
    assert events[8].text == "otra vez"
    assert isinstance(events[9], ElevenLabsUtteranceEndEvent)


def test_parser_exposes_session_transcripts_and_warning():
    assert parse_elevenlabs_message(session_started(language_code="es")) == (
        ElevenLabsMetadataEvent(
            session_id="sess-test",
            model_id="scribe_v2_realtime",
            audio_format="ulaw_8000",
            sample_rate_hz=8000,
            language_code="es",
        )
    )
    assert parse_elevenlabs_message(
        {"message_type": "partial_transcript", "text": "hola"}
    ) == ElevenLabsTranscriptEvent(
        text="hola", is_final=False, speech_final=False
    )
    assert parse_elevenlabs_message(
        {"message_type": "warning", "warning": "synthetic warning"}
    ) == ElevenLabsWarningEvent(
        code="warning", description="synthetic warning"
    )


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        [],
        {},
        {"message_type": "partial_transcript", "text": 7},
        {"message_type": "new_unhandled_event"},
        {
            "message_type": "session_started",
            "session_id": "sess",
            "config": {
                "audio_format": "pcm_16000",
                "model_id": "scribe_v2_realtime",
                "sample_rate": 16000,
            },
        },
    ],
)
def test_malformed_or_unsupported_messages_fail_closed(payload):
    with pytest.raises(ElevenLabsProtocolError):
        parse_elevenlabs_message(payload)


def test_provider_error_is_typed_and_does_not_expose_payload():
    secret = "synthetic-secret-must-not-leak"
    with pytest.raises(ElevenLabsProviderError) as captured:
        parse_elevenlabs_message(
            {
                "message_type": "rate_limited",
                "error": f"provider echoed {secret}",
            }
        )

    assert captured.value.code == "rate_limited"
    assert secret not in str(captured.value)


@pytest.mark.asyncio
async def test_data_before_session_and_any_remote_close_fail_closed():
    transcript_first = ElevenLabsRealtimeClient(
        "synthetic-secret",
        session=FakeSession(
            FakeWebSocket(
                [
                    ws_message(
                        aiohttp.WSMsgType.TEXT,
                        json.dumps(
                            {"message_type": "partial_transcript", "text": "hi"}
                        ),
                    )
                ]
            )
        ),
    )
    with pytest.raises(ElevenLabsProtocolError):
        await transcript_first.connect()
    assert transcript_first.connected is False

    remote_close = ElevenLabsRealtimeClient(
        "synthetic-secret",
        session=FakeSession(
            FakeWebSocket([ws_message(aiohttp.WSMsgType.CLOSE, 1000)])
        ),
    )
    with pytest.raises(ElevenLabsConnectionClosed, match="during handshake"):
        await remote_close.connect()
    assert remote_close.connected is False


@pytest.mark.asyncio
async def test_provider_error_before_session_started_fails_connect() -> None:
    websocket = FakeWebSocket(
        [
            ws_message(
                aiohttp.WSMsgType.TEXT,
                json.dumps({"message_type": "rate_limited", "error": "secret"}),
            )
        ]
    )
    client = ElevenLabsRealtimeClient(
        "synthetic-secret", session=FakeSession(websocket)
    )

    with pytest.raises(ElevenLabsProviderError) as caught:
        await client.connect()

    assert caught.value.code == "rate_limited"
    assert client.connected is False
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_owned_session_close_is_bounded_and_idempotent():
    never = asyncio.Event()
    websocket = FakeWebSocket(close_waiter=never)
    session = FakeSession(websocket)
    client = ElevenLabsRealtimeClient(
        "synthetic-secret",
        session_factory=lambda: session,
        close_timeout_seconds=0.01,
    )
    await client.connect()

    await asyncio.wait_for(client.close(), timeout=0.1)
    await client.close()

    assert websocket.close_calls == 1
    assert session.close_calls == 1
    assert client.connected is False
    with pytest.raises(ElevenLabsConnectionClosed):
        await client.send_audio(b"\xff")


@pytest.mark.asyncio
async def test_cancelled_connect_cleans_up_owned_session_and_propagates():
    session = FakeSession(FakeWebSocket())

    async def cancelled_connect(url, **kwargs):
        raise asyncio.CancelledError

    session.ws_connect = cancelled_connect
    client = ElevenLabsRealtimeClient(
        "synthetic-secret", session_factory=lambda: session
    )

    with pytest.raises(asyncio.CancelledError):
        await client.connect()

    assert session.closed is True
    assert client.connected is False


@pytest.mark.asyncio
async def test_invalid_provider_key_is_sanitized_and_socket_is_not_opened():
    secret = "sensitive provider exception"

    def provider():
        raise RuntimeError(secret)

    session = FakeSession(FakeWebSocket())
    client = ElevenLabsRealtimeClient(
        api_key_provider=provider,
        session=session,
    )

    with pytest.raises(ElevenLabsCredentialsError) as captured:
        await client.connect()

    assert secret not in str(captured.value)
    assert session.connect_url is None


@pytest.mark.asyncio
async def test_audio_validation_and_open_connection_are_enforced():
    client = ElevenLabsRealtimeClient(
        "synthetic-secret", session=FakeSession(FakeWebSocket())
    )
    with pytest.raises(ElevenLabsConnectionClosed):
        await client.finalize()
    with pytest.raises(ValueError, match="bytes-like"):
        await client.send_audio(160)
    with pytest.raises(ValueError, match="cannot be empty"):
        await client.send_audio(b"")
