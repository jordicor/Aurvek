"""Focused delivery-safety tests for foreground changes during AI turns."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiosqlite
import orjson
import pytest
from fastapi.responses import StreamingResponse

from ai_runtime.channel_turns import ChannelContext, current_channel_turn
from ai_runtime import messages as runtime_messages
from ai_runtime.multi_ai import service as multi_ai_service
from integrations.devices import service as device_service
from integrations.telegram import routes as telegram_routes
from integrations.whatsapp import routes as whatsapp_routes


def _sse(payload: dict) -> bytes:
    return b"data: " + orjson.dumps(payload) + b"\n\n"


async def _body(*payloads: dict):
    for payload in payloads:
        yield _sse(payload)


async def _chunks(*chunks):
    for chunk in chunks:
        yield chunk


async def _broken_chunks():
    yield _sse({"content": "partial"})
    yield _sse({"message_ids": {"user": 91, "bot": 92}})
    raise RuntimeError("stream iterator failed after yielding")


@pytest.mark.asyncio
async def test_whatsapp_buffer_collects_text_and_structured_items_until_eof():
    lifecycle = []
    items_yielded = asyncio.Event()
    allow_eof = asyncio.Event()

    async def stream():
        lifecycle.append("started")
        yield _sse({"content": "Hello "})
        lifecycle.append("after_text")
        yield _sse({"content": [{"type": "image", "url": "https://example.test/a"}]})
        lifecycle.append("after_list")
        yield _sse({"content": {"type": "audio", "url": "https://example.test/b"}})
        yield _sse({"content": "again"})
        yield _sse({"message_ids": {"user": 41, "bot": 42}})
        items_yielded.set()
        await allow_eof.wait()
        lifecycle.append("eof")

    buffering = asyncio.create_task(
        whatsapp_routes._buffer_whatsapp_runtime_output(stream())
    )
    await items_yielded.wait()

    # Even after all content has arrived, callers receive nothing until the
    # runtime reaches a clean EOF and can no longer append a stale terminal.
    assert not buffering.done()
    allow_eof.set()
    result = await buffering

    assert lifecycle == ["started", "after_text", "after_list", "eof"]
    assert result == (
        [
            "Hello ",
            [{"type": "image", "url": "https://example.test/a"}],
            {"type": "audio", "url": "https://example.test/b"},
            "again",
        ],
        None,
        False,
        True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payloads", "expected_terminal", "expected_persistence", "expected_durable"),
    [
        (
            (
                {"content": "must not be delivered"},
                {"terminal": "stale_channel_turn"},
            ),
            "stale_channel_turn",
            False,
            False,
        ),
        (
            (
                {"content": "must not be delivered"},
                {"persistence_error": True},
            ),
            None,
            True,
            False,
        ),
    ],
)
async def test_whatsapp_buffer_reports_non_deliverable_runtime_outcomes(
    payloads,
    expected_terminal,
    expected_persistence,
    expected_durable,
):
    items, terminal, persistence_error, durable_message_ids = (
        await whatsapp_routes._buffer_whatsapp_runtime_output(_body(*payloads))
    )

    # Content may have arrived before the failure, but the helper only returns
    # staged data; the route can therefore suppress it before calling Twilio.
    assert items == ["must not be delivered"]
    assert terminal == expected_terminal
    assert persistence_error is expected_persistence
    assert durable_message_ids is expected_durable


@pytest.mark.asyncio
async def test_provider_exception_emits_persistence_terminal_for_external_buffers(
    monkeypatch,
):
    async def fail_before_provider_work():
        raise RuntimeError("simulated provider setup failure")

    monkeypatch.setattr(
        runtime_messages,
        "ensure_conversation_privacy_schema",
        fail_before_provider_work,
    )

    stream = runtime_messages.get_ai_response(
        message="inbound",
        context_messages=[],
        conversation_id=19,
        machine="GPT",
        model="test-model",
        current_user=SimpleNamespace(id=7),
        request=SimpleNamespace(),
        max_tokens=50,
    )
    chunks = [chunk async for chunk in stream]

    assert chunks
    assert all(chunk is not None for chunk in chunks)
    whatsapp_result = await whatsapp_routes._buffer_whatsapp_runtime_output(
        _chunks(*chunks)
    )
    telegram_result = await telegram_routes._buffer_telegram_runtime_output(
        _chunks(*chunks)
    )

    assert whatsapp_result[1:] == ("persistence_error", True, False)
    assert telegram_result[1:] == ("persistence_error", True, False)


@pytest.mark.asyncio
async def test_external_buffers_treat_legacy_none_chunk_as_persistence_failure():
    whatsapp_result = await whatsapp_routes._buffer_whatsapp_runtime_output(
        _chunks(_sse({"content": "partial"}), None)
    )
    telegram_result = await telegram_routes._buffer_telegram_runtime_output(
        _chunks(_sse({"content": "partial"}), None)
    )

    assert whatsapp_result == (["partial"], None, True, False)
    assert telegram_result == ("partial", None, True, False)


@pytest.mark.asyncio
async def test_external_buffers_contain_iterator_failure_as_non_durable():
    whatsapp_result = await whatsapp_routes._buffer_whatsapp_runtime_output(
        _broken_chunks()
    )
    telegram_result = await telegram_routes._buffer_telegram_runtime_output(
        _broken_chunks()
    )

    assert whatsapp_result == (["partial"], None, True, False)
    assert telegram_result == ("partial", None, True, False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payloads", "expected_terminal"),
    [
        (
            (
                {"content": "partial reply"},
                {"message_ids": {"user": 1, "bot": 2}},
                {"terminal": "stale_channel_turn"},
            ),
            "stale_channel_turn",
        ),
        (
            (
                {"content": "partial reply"},
                {"message_ids": {"user": 1, "bot": 2}},
                {"persistence_error": True},
            ),
            "persistence_error",
        ),
        (({"content": "apparently successful but not durable"},), "persistence_error"),
    ],
)
async def test_device_parser_suppresses_reply_for_non_durable_streams(
    payloads,
    expected_terminal,
):
    response = StreamingResponse(_body(*payloads), media_type="text/event-stream")

    reply, terminal = await device_service._streaming_response_to_reply(response)

    assert reply == ""
    assert terminal == expected_terminal


@pytest.mark.asyncio
async def test_device_parser_returns_reply_only_after_durable_message_ids():
    response = StreamingResponse(
        _body(
            {"content": "durable "},
            {"content": "reply"},
            {"message_ids": {"user": 10, "bot": 11}},
        ),
        media_type="text/event-stream",
    )

    assert await device_service._streaming_response_to_reply(response) == (
        "durable reply",
        None,
    )


@pytest.mark.asyncio
async def test_device_retry_release_allows_same_external_message_id(tmp_path, monkeypatch):
    db_path = tmp_path / "device-retry.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE EXTERNAL_DEVICE_EVENTS (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL,
                conversation_id INTEGER,
                external_message_id TEXT,
                direction TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                latency_ms INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX idx_external_device_events_message_id
            ON EXTERNAL_DEVICE_EVENTS(device_id, external_message_id)
            WHERE external_message_id IS NOT NULL;
            """
        )
        await conn.commit()

    @asynccontextmanager
    async def get_test_connection(readonly=False):
        mode = "ro" if readonly else "rwc"
        conn = await aiosqlite.connect(f"file:{db_path}?mode={mode}", uri=True)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    monkeypatch.setattr(device_service, "get_db_connection", get_test_connection)

    first = await device_service.reserve_incoming_message_event(
        device_id=7,
        conversation_id=19,
        external_message_id="same-id",
        metadata={},
        text="first attempt",
    )
    assert first["state"] == "reserved"

    await device_service.release_retryable_incoming_message_event(
        event_id=first["event_id"]
    )
    retry = await device_service.reserve_incoming_message_event(
        device_id=7,
        conversation_id=19,
        external_message_id="same-id",
        metadata={"retry": True},
        text="second attempt",
    )

    assert retry["state"] == "reserved"
    assert retry["event_id"] != first["event_id"]


@pytest.mark.asyncio
async def test_webhook_retry_responses_release_dedupe_markers(tmp_path, monkeypatch):
    db_path = tmp_path / "webhook-retry.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE WHATSAPP_PROCESSED_MESSAGES (
                message_sid TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE TELEGRAM_PROCESSED_UPDATES (
                update_id INTEGER PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO WHATSAPP_PROCESSED_MESSAGES(message_sid) VALUES ('SM-retry');
            INSERT INTO TELEGRAM_PROCESSED_UPDATES(update_id) VALUES (9001);
            """
        )
        await conn.commit()

    @asynccontextmanager
    async def get_test_connection(readonly=False):
        mode = "ro" if readonly else "rwc"
        conn = await aiosqlite.connect(f"file:{db_path}?mode={mode}", uri=True)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    monkeypatch.setattr(whatsapp_routes, "get_db_connection", get_test_connection)
    monkeypatch.setattr(telegram_routes, "get_db_connection", get_test_connection)

    whatsapp_response = await whatsapp_routes._whatsapp_retry_response("SM-retry")
    telegram_response = await telegram_routes._telegram_retry_response(9001)

    assert whatsapp_response.status_code == 503
    assert telegram_response.status_code == 503
    async with aiosqlite.connect(db_path) as conn:
        whatsapp_count = (
            await (
                await conn.execute("SELECT COUNT(*) FROM WHATSAPP_PROCESSED_MESSAGES")
            ).fetchone()
        )[0]
        telegram_count = (
            await (
                await conn.execute("SELECT COUNT(*) FROM TELEGRAM_PROCESSED_UPDATES")
            ).fetchone()
        )[0]
    assert whatsapp_count == 0
    assert telegram_count == 0


@pytest.mark.asyncio
async def test_multi_ai_stale_recovery_persists_user_only_in_recovered_context(
    monkeypatch,
):
    recovered = ChannelContext(channel="whatsapp", persistence="ingest_only")
    recovery_calls = []
    saved = []

    async def recover(original):
        recovery_calls.append(original)
        return recovered

    original = ChannelContext(
        channel="whatsapp",
        persistence="immediate",
        recover_stale_context=recover,
    )

    async def save_content(*args, **kwargs):
        bound = current_channel_turn()
        assert bound is not None
        assert bound.context is recovered
        saved.append((args, kwargs))
        return 73, None

    monkeypatch.setattr(multi_ai_service, "save_content_to_db", save_content)

    payload = await multi_ai_service._recover_stale_multi_ai_turn(
        channel_context=original,
        conversation_id=9,
        user_id=4,
        user_message="keep this user turn",
        prompt_id=2,
        llm_id=3,
    )

    assert recovery_calls == [original]
    assert payload == {"terminal": "queued_for_active_phone", "message_id": 73}
    assert len(saved) == 1
    assert saved[0][1]["user_message"] == "keep this user turn"
    assert saved[0][1]["prompt_id"] == 2
    assert saved[0][1]["llm_id"] == 3
