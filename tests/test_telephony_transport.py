import asyncio
from types import SimpleNamespace

import orjson
import pytest
from fastapi.responses import JSONResponse, StreamingResponse

from ai_runtime.channel_turns import channel_turn_registry
from integrations.telephony.foreground import ForegroundCommitGuard
from integrations.telephony.phone_context import create_phone_channel_turn
from integrations.telephony.transport import (
    PhoneRuntimeBridgeError,
    iter_sse_payloads,
    persist_canonical_phone_caller_turn,
    start_canonical_phone_turn,
)


async def _chunks(*values):
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_iter_sse_payloads_handles_split_crlf_and_multiline_data():
    body = _chunks(
        b": ready\r",
        b"\ndata: {\"content\":\r\n",
        b"data: \"hello\"}\r\n\r",
        b"\n",
    )

    assert [item async for item in iter_sse_payloads(body)] == [
        {"content": "hello"}
    ]


@pytest.mark.asyncio
async def test_iter_sse_payloads_rejects_invalid_json():
    with pytest.raises(PhoneRuntimeBridgeError, match="invalid SSE JSON"):
        _ = [
            item
            async for item in iter_sse_payloads(
                _chunks(b"data: {not-json}\n\n")
            )
        ]


def _phone_turn(*, turn_id="turn-1"):
    return create_phone_channel_turn(
        ForegroundCommitGuard(
            conversation_id=7,
            epoch=3,
            expected_owner="phone",
            call_id="call-1",
            lease_owner="worker-1",
        ),
        turn_id=turn_id,
    )


@pytest.mark.asyncio
async def test_canonical_phone_turn_waits_for_exact_audible_confirmation():
    phone_turn = _phone_turn()

    async def runtime_invoker(_request, conversation_id, user, **kwargs):
        assert conversation_id == 7
        assert user.id == 9
        assert kwargs["text_plain"] == "How are you?"
        assert kwargs["prevalidated"] is False
        assert kwargs["is_whatsapp"] is True
        assert kwargs["expected_llm_id"] == 11
        assert kwargs["runtime_llm_id"] == 17
        assert kwargs["reasoning_selection"] == {"mode": "off"}
        context = kwargs["channel_context"]
        handle = await channel_turn_registry.register(context)

        async def body():
            handle.bind_owner_task()
            try:
                yield ': stream-ready\n\n'
                yield 'data: {"content":"Hello "}\n\n'
                yield 'data: {"content":"there."}\n\n'

                async def commit(confirmation):
                    assert confirmation.text_prefix == "Hello there."
                    assert confirmation.played_ms == 900
                    return 101, 102

                result = await handle.defer_commit("Hello there.", commit)
                yield "data: " + orjson.dumps(
                    {"message_ids": {"user": result[0], "bot": result[1]}}
                ).decode() + "\n\n"
            finally:
                await channel_turn_registry.unregister(handle.key, handle)

        return StreamingResponse(body(), media_type="text/event-stream")

    turn = await start_canonical_phone_turn(
        conversation_id=7,
        current_user=SimpleNamespace(id=9),
        caller_text="  How are you?  ",
        phone_turn=phone_turn,
        expected_llm_id=11,
        runtime_llm_id=17,
        reasoning_selection={"mode": "off"},
        runtime_invoker=runtime_invoker,
    )

    content = []
    async for event in turn.events_until_draft():
        if event.content:
            content.append(event.content)

    assert "".join(content) == "Hello there."
    assert (await turn.wait_for_draft()).content == "Hello there."
    assert await turn.confirm_audible("Hello there.", played_ms=900) == (101, 102)
    assert turn.handle.committed is True


@pytest.mark.asyncio
async def test_start_canonical_phone_turn_surfaces_runtime_rejection():
    async def runtime_invoker(request, *_args, **kwargs):
        assert request is None
        assert kwargs["prevalidated"] is False
        assert kwargs["is_whatsapp"] is True
        return JSONResponse(
            {"success": False, "message": "A break pause is required"},
            status_code=429,
        )

    with pytest.raises(PhoneRuntimeBridgeError, match="break pause is required"):
        await start_canonical_phone_turn(
            conversation_id=7,
            current_user=SimpleNamespace(id=9),
            caller_text="hello",
            phone_turn=_phone_turn(turn_id="turn-rejected"),
            expected_llm_id=11,
            runtime_invoker=runtime_invoker,
        )


@pytest.mark.asyncio
async def test_stopped_wire_caller_turn_uses_ingest_only_runtime_without_generation():
    phone_turn = create_phone_channel_turn(
        ForegroundCommitGuard(
            conversation_id=7,
            epoch=3,
            expected_owner="phone",
            call_id="call-1",
            lease_owner="worker-1",
        ),
        turn_id="turn-stop",
        persistence="ingest_only",
    )

    async def runtime_invoker(_request, conversation_id, user, **kwargs):
        assert conversation_id == 7
        assert user.id == 9
        assert kwargs["text_plain"] == "last words"
        assert kwargs["expected_llm_id"] == 11
        assert kwargs["runtime_llm_id"] == 17
        assert kwargs["reasoning_selection"] == {"mode": "low"}
        assert kwargs["channel_context"].ingest_only is True

        async def body():
            yield 'data: {"terminal":"queued_for_active_phone","message_id":301}\n\n'

        return StreamingResponse(body(), media_type="text/event-stream")

    message_id = await persist_canonical_phone_caller_turn(
        conversation_id=7,
        current_user=SimpleNamespace(id=9),
        caller_text=" last words ",
        phone_turn=phone_turn,
        expected_llm_id=11,
        runtime_llm_id=17,
        reasoning_selection={"mode": "low"},
        runtime_invoker=runtime_invoker,
    )

    assert message_id == 301

@pytest.mark.asyncio
async def test_canonical_phone_turn_interrupts_before_final_draft_once():
    phone_turn = _phone_turn(turn_id="turn-interrupt")
    generation_gate = asyncio.Event()

    async def runtime_invoker(*_args, **kwargs):
        context = kwargs["channel_context"]
        handle = await channel_turn_registry.register(context)

        async def fallback(confirmation, on_database_commit):
            assert confirmation.text_prefix == "Hello "
            assert confirmation.played_ms == 420
            result = (201, 202)
            on_database_commit(result)
            return result

        handle.register_interruption_fallback(fallback)

        async def body():
            handle.bind_owner_task()
            try:
                yield 'data: {"content":"Hello "}\n\n'
                await generation_gate.wait()
                await handle.defer_commit("Hello there.", lambda _confirmation: None)
            finally:
                await channel_turn_registry.unregister(handle.key, handle)

        return StreamingResponse(body(), media_type="text/event-stream")

    turn = await start_canonical_phone_turn(
        conversation_id=7,
        current_user=SimpleNamespace(id=9),
        caller_text="hello",
        phone_turn=phone_turn,
        expected_llm_id=11,
        runtime_invoker=runtime_invoker,
    )
    iterator = turn.events_until_draft()
    first = await anext(iterator)
    assert first.content == "Hello "
    assert await turn.interrupt("Hello ", played_ms=420) == (201, 202)
    assert turn._draft_task.done()
    generation_gate.set()
    await iterator.aclose()
