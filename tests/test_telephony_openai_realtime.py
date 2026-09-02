from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import aiohttp
import pytest

from integrations.telephony.openai_realtime import (
    OPENAI_REALTIME_MAX_AUDIO_CHUNK_BYTES,
    OPENAI_REALTIME_MAX_FUNCTION_OUTPUT_CHARS,
    OPENAI_REALTIME_MODELS,
    OPENAI_REALTIME_REASONING_EFFORTS,
    OPENAI_REALTIME_VOICES,
    OpenAIFunctionCallEvent,
    OpenAIInputTranscriptFailedEvent,
    OpenAIInputTranscriptEvent,
    OpenAIOutputAudioEvent,
    OpenAIOutputTextEvent,
    OpenAIProviderErrorEvent,
    OpenAIRealtimeClient,
    OpenAIRealtimeConnectionClosed,
    OpenAIRealtimeOptions,
    OpenAIRealtimeProtocolError,
    OpenAIResponseDoneEvent,
    OpenAISessionEvent,
    OpenAISpeechEvent,
    SemanticVadOptions,
    ServerVadOptions,
    parse_openai_realtime_message,
)


class FakeWebSocket:
    def __init__(self, messages=(), *, send_waiter=None, receive_waiter=None):
        self.messages = list(messages)
        self.sent_json = []
        self.closed = False
        self.close_calls = 0
        self.send_waiter = send_waiter
        self.receive_waiter = receive_waiter

    async def send_json(self, payload):
        if self.send_waiter is not None:
            await self.send_waiter.wait()
        self.sent_json.append(payload)

    async def receive(self):
        if self.receive_waiter is not None:
            await self.receive_waiter.wait()
        return self.messages.pop(0)

    async def close(self):
        self.close_calls += 1
        self.closed = True


class FakeSession:
    def __init__(self, websocket):
        self.websocket = websocket
        self.connect_url = None
        self.connect_kwargs = None
        self.closed = False
        self.close_calls = 0

    async def ws_connect(self, url, **kwargs):
        self.connect_url = url
        self.connect_kwargs = kwargs
        return self.websocket

    async def close(self):
        self.close_calls += 1
        self.closed = True


def ws_message(payload):
    return SimpleNamespace(
        type=aiohttp.WSMsgType.TEXT,
        data=json.dumps(payload),
    )


def make_client(websocket=None, *, options=None, **kwargs):
    websocket = websocket or FakeWebSocket()
    session = FakeSession(websocket)
    client = OpenAIRealtimeClient(
        api_key_provider=lambda: "synthetic-openai-secret",
        options=options,
        session=session,
        **kwargs,
    )
    return client, session, websocket


def test_options_are_strictly_limited_to_current_models_voices_and_efforts():
    assert OPENAI_REALTIME_MODELS == {
        "gpt-realtime-2.1",
        "gpt-realtime-2.1-mini",
    }
    assert OPENAI_REALTIME_VOICES == {
        "alloy",
        "ash",
        "ballad",
        "coral",
        "echo",
        "sage",
        "shimmer",
        "verse",
        "marin",
        "cedar",
    }
    assert OPENAI_REALTIME_REASONING_EFFORTS == {
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    }

    with pytest.raises(ValueError, match="model"):
        OpenAIRealtimeOptions(model="gpt-realtime")
    with pytest.raises(ValueError, match="voice"):
        OpenAIRealtimeOptions(voice="Dan")
    with pytest.raises(ValueError, match="reasoning"):
        OpenAIRealtimeOptions(reasoning_effort="none")


def test_session_update_uses_ga_nested_pcmu_and_manual_semantic_vad():
    options = OpenAIRealtimeOptions(
        model="gpt-realtime-2.1-mini",
        voice="marin",
        instructions="Speak briefly.",
        reasoning_effort="minimal",
        vad=SemanticVadOptions(
            eagerness="high",
            create_response=False,
            interrupt_response=True,
        ),
        input_transcription_model="gpt-live-transcribe",
        tools=(
            {
                "type": "function",
                "name": "lookup_memory",
                "description": "Read grounded memory.",
                "parameters": {"type": "object", "properties": {}},
            },
        ),
    )

    update = options.session_update()

    assert update["type"] == "session.update"
    session = update["session"]
    assert session["type"] == "realtime"
    assert session["model"] == "gpt-realtime-2.1-mini"
    assert session["output_modalities"] == ["audio"]
    assert session["audio"] == {
        "input": {
            "format": {"type": "audio/pcmu"},
            "turn_detection": {
                "type": "semantic_vad",
                "eagerness": "high",
                "create_response": False,
                "interrupt_response": True,
            },
            "transcription": {"model": "gpt-live-transcribe"},
        },
        "output": {
            "format": {"type": "audio/pcmu"},
            "voice": "marin",
        },
    }
    assert session["reasoning"] == {"effort": "minimal"}
    assert session["tools"][0]["name"] == "lookup_memory"


def test_server_vad_and_disabled_vad_have_exact_ga_shapes():
    server = OpenAIRealtimeOptions(
        vad=ServerVadOptions(
            threshold=0.65,
            prefix_padding_ms=240,
            silence_duration_ms=650,
            idle_timeout_ms=15_000,
            create_response=True,
            interrupt_response=False,
        )
    ).session_update()["session"]["audio"]["input"]["turn_detection"]
    assert server == {
        "type": "server_vad",
        "threshold": 0.65,
        "prefix_padding_ms": 240,
        "silence_duration_ms": 650,
        "create_response": True,
        "interrupt_response": False,
        "idle_timeout_ms": 15_000,
    }

    disabled = OpenAIRealtimeOptions(vad=None).session_update()
    assert disabled["session"]["audio"]["input"]["turn_detection"] is None


@pytest.mark.parametrize("idle_timeout_ms", [4_999, 30_001])
def test_server_vad_rejects_idle_timeout_outside_ga_bounds(
    idle_timeout_ms: int,
) -> None:
    with pytest.raises(ValueError, match="idle_timeout_ms"):
        ServerVadOptions(idle_timeout_ms=idle_timeout_ms)


@pytest.mark.asyncio
async def test_connect_uses_server_authorization_and_sends_session_update():
    options = OpenAIRealtimeOptions(model="gpt-realtime-2.1", voice="cedar")
    client, session, websocket = make_client(options=options)

    await client.connect()

    assert session.connect_url == (
        "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1"
    )
    assert session.connect_kwargs["headers"] == {
        "Authorization": "Bearer synthetic-openai-secret",
        "User-Agent": "Aurvek-Telephony/1.0",
    }
    assert "synthetic-openai-secret" not in session.connect_url
    assert session.connect_kwargs["max_msg_size"] == 4 * 1024 * 1024
    assert websocket.sent_json == [options.session_update()]


@pytest.mark.asyncio
async def test_all_required_client_commands_have_exact_ga_shapes():
    client, _, websocket = make_client()
    await client.connect()
    websocket.sent_json.clear()

    await client.append_audio(b"\xff\x7f")
    await client.commit_audio()
    await client.clear_audio()
    await client.truncate_item("item-1", 425, content_index=0)
    await client.cancel_response("resp-1")
    await client.cancel_response()
    await client.create_response()
    await client.create_response("Use the refreshed Aurvek context.")

    assert websocket.sent_json == [
        {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(b"\xff\x7f").decode("ascii"),
        },
        {"type": "input_audio_buffer.commit"},
        {"type": "input_audio_buffer.clear"},
        {
            "type": "conversation.item.truncate",
            "item_id": "item-1",
            "content_index": 0,
            "audio_end_ms": 425,
        },
        {"type": "response.cancel", "response_id": "resp-1"},
        {"type": "response.cancel"},
        {"type": "response.create"},
        {
            "type": "response.create",
            "response": {"instructions": "Use the refreshed Aurvek context."},
        },
    ]


@pytest.mark.asyncio
async def test_partial_session_update_changes_only_requested_ga_fields():
    client, _, websocket = make_client()
    await client.connect()
    websocket.sent_json.clear()
    tools = (
        {
            "type": "function",
            "name": "read_memory",
            "description": "Read grounded context.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    )

    await client.update_session(
        instructions="Use the latest Aurvek context.",
        tools=tools,
        tool_choice="auto",
        max_output_tokens=512,
        reasoning_effort="low",
    )
    await client.update_session(
        instructions="",
        tools=(),
    )

    assert websocket.sent_json == [
        {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": "Use the latest Aurvek context.",
                "tools": list(tools),
                "tool_choice": "auto",
                "max_output_tokens": 512,
                "reasoning": {"effort": "low"},
            },
        },
        {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": "",
                "tools": [],
            },
        },
    ]


@pytest.mark.asyncio
async def test_text_history_function_output_and_delete_use_ga_item_events():
    client, _, websocket = make_client()
    await client.connect()
    websocket.sent_json.clear()

    await client.create_conversation_item("System context", role="system")
    await client.create_conversation_item(
        "Question", role="user", previous_item_id="item-system"
    )
    await client.create_conversation_item("Prior answer", role="assistant")
    await client.send_function_output(
        "call-1", {"answer": 42, "grounded": True}
    )
    await client.delete_item("item-obsolete")

    assert websocket.sent_json == [
        {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": "System context"}],
            },
        },
        {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Question"}],
            },
            "previous_item_id": "item-system",
        },
        {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "Prior answer"}
                ],
            },
        },
        {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": '{"answer":42,"grounded":true}',
            },
        },
        {
            "type": "conversation.item.delete",
            "item_id": "item-obsolete",
        },
    ]


@pytest.mark.asyncio
async def test_dynamic_commands_validate_empty_updates_roles_and_output_bounds():
    client, _, _ = make_client()
    await client.connect()

    with pytest.raises(ValueError, match="at least one"):
        await client.update_session()
    with pytest.raises(ValueError, match="tool_choice"):
        await client.update_session(tool_choice="sometimes")
    with pytest.raises(ValueError, match="reasoning"):
        await client.update_session(reasoning_effort="none")
    with pytest.raises(ValueError, match="role"):
        await client.create_conversation_item("history", role="developer")
    with pytest.raises(ValueError, match="text"):
        await client.create_conversation_item("")
    with pytest.raises(ValueError, match="JSON serializable"):
        await client.send_function_output("call-1", {object()})
    with pytest.raises(ValueError, match="too large"):
        await client.send_function_output(
            "call-1", "x" * (OPENAI_REALTIME_MAX_FUNCTION_OUTPUT_CHARS + 1)
        )


@pytest.mark.asyncio
async def test_events_translate_session_speech_transcripts_audio_and_text():
    audio = b"\xff\x00\x80"
    messages = [
        ws_message(
            {
                "type": "session.created",
                "session": {
                    "id": "sess-1",
                    "model": "gpt-realtime-2.1-mini",
                    "expires_at": 1_900_000_000,
                },
            }
        ),
        ws_message(
            {
                "type": "input_audio_buffer.speech_started",
                "item_id": "caller-1",
                "audio_start_ms": 120,
            }
        ),
        ws_message(
            {
                "type": "conversation.item.input_audio_transcription.delta",
                "item_id": "caller-1",
                "content_index": 0,
                "delta": "hola",
            }
        ),
        ws_message(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "caller-1",
                "content_index": 0,
                "transcript": "hola mundo",
                "usage": {
                    "type": "tokens",
                    "input_tokens": 8,
                    "output_tokens": 2,
                    "total_tokens": 10,
                    "input_token_details": {
                        "audio_tokens": 7,
                        "text_tokens": 1,
                    },
                },
            }
        ),
        ws_message(
            {
                "type": "response.output_audio.delta",
                "response_id": "resp-1",
                "item_id": "assistant-1",
                "content_index": 0,
                "delta": base64.b64encode(audio).decode("ascii"),
            }
        ),
        ws_message(
            {
                "type": "response.output_audio_transcript.done",
                "response_id": "resp-1",
                "item_id": "assistant-1",
                "content_index": 0,
                "transcript": "Hola, Jordi.",
            }
        ),
    ]
    client, _, _ = make_client(FakeWebSocket(messages))
    await client.connect()
    iterator = client.events()

    events = [await anext(iterator) for _ in messages]

    assert events[0] == OpenAISessionEvent(
        event_type="session.created",
        session_id="sess-1",
        model="gpt-realtime-2.1-mini",
        expires_at=1_900_000_000,
    )
    assert events[1] == OpenAISpeechEvent(
        started=True,
        item_id="caller-1",
        audio_offset_ms=120,
    )
    assert events[2] == OpenAIInputTranscriptEvent(
        item_id="caller-1", text="hola", is_final=False
    )
    assert events[3].text == "hola mundo"
    assert events[3].is_final is True
    assert events[3].usage.total_tokens == 10
    assert events[3].usage.audio_input_tokens == 7
    assert events[4] == OpenAIOutputAudioEvent(
        response_id="resp-1",
        item_id="assistant-1",
        content_index=0,
        audio=audio,
        is_final=False,
    )
    assert events[5] == OpenAIOutputTextEvent(
        channel="audio_transcript",
        response_id="resp-1",
        item_id="assistant-1",
        content_index=0,
        text="Hola, Jordi.",
        is_final=True,
    )


def test_function_usage_and_error_events_are_bounded_internal_shapes():
    function_delta = parse_openai_realtime_message(
        {
            "type": "response.function_call_arguments.delta",
            "response_id": "resp-1",
            "item_id": "tool-1",
            "call_id": "call-1",
            "delta": '{"query":',
        }
    )
    function_done = parse_openai_realtime_message(
        {
            "type": "response.function_call_arguments.done",
            "response_id": "resp-1",
            "item_id": "tool-1",
            "call_id": "call-1",
            "name": "web_search",
            "arguments": '{"query":"Miami"}',
        }
    )
    done = parse_openai_realtime_message(
        {
            "type": "response.done",
            "response": {
                "id": "resp-1",
                "status": "completed",
                "usage": {
                    "input_tokens": 30,
                    "output_tokens": 12,
                    "total_tokens": 42,
                    "input_token_details": {
                        "cached_tokens": 10,
                        "text_tokens": 8,
                        "audio_tokens": 22,
                    },
                    "output_token_details": {
                        "text_tokens": 2,
                        "audio_tokens": 6,
                        "reasoning_tokens": 4,
                    },
                },
            },
        }
    )
    error = parse_openai_realtime_message(
        {
            "type": "error",
            "event_id": "evt-1",
            "error": {
                "type": "invalid_request_error",
                "code": "invalid_value",
                "message": "bad request\n",
                "param": "audio.input.format",
            },
        }
    )

    assert function_delta == OpenAIFunctionCallEvent(
        response_id="resp-1",
        item_id="tool-1",
        call_id="call-1",
        name=None,
        arguments='{"query":',
        is_final=False,
    )
    assert function_done.name == "web_search"
    assert function_done.is_final is True
    assert isinstance(done, OpenAIResponseDoneEvent)
    assert done.usage.total_tokens == 42
    assert done.usage.reasoning_output_tokens == 4
    assert isinstance(error, OpenAIProviderErrorEvent)
    assert error.message == "bad request"


def test_input_transcription_failure_is_an_item_scoped_terminal_event():
    failure = parse_openai_realtime_message(
        {
            "type": "conversation.item.input_audio_transcription.failed",
            "item_id": "caller-2",
            "content_index": 0,
            "error": {
                "type": "transcription_error",
                "code": "audio_unintelligible",
                "message": "Could not transcribe input audio.",
                "param": None,
            },
        }
    )

    assert failure == OpenAIInputTranscriptFailedEvent(
        item_id="caller-2",
        content_index=0,
        code="audio_unintelligible",
        error_type="transcription_error",
        message="Could not transcribe input audio.",
    )


def test_parsing_rejects_invalid_base64_and_commands_reject_oversized_audio():
    with pytest.raises(OpenAIRealtimeProtocolError, match="base64"):
        parse_openai_realtime_message(
            {
                "type": "response.output_audio.delta",
                "response_id": "resp-1",
                "item_id": "item-1",
                "content_index": 0,
                "delta": "not/base64!",
            }
        )


@pytest.mark.asyncio
async def test_command_size_limit_send_timeout_and_idempotent_close():
    client, _, websocket = make_client()
    await client.connect()
    with pytest.raises(ValueError, match="too large"):
        await client.append_audio(
            b"\x00" * (OPENAI_REALTIME_MAX_AUDIO_CHUNK_BYTES + 1)
        )

    await client.close()
    await client.close()
    assert websocket.close_calls == 1

    waiter = asyncio.Event()
    slow_client, _, slow_websocket = make_client(
        FakeWebSocket(send_waiter=waiter),
        send_timeout_seconds=0.01,
    )
    with pytest.raises(OpenAIRealtimeConnectionClosed, match="timed out"):
        await slow_client.connect()
    assert slow_websocket.close_calls == 1


@pytest.mark.asyncio
async def test_receive_timeout_is_optional_but_bounded_when_configured():
    waiter = asyncio.Event()
    client, _, _ = make_client(
        FakeWebSocket(receive_waiter=waiter),
        receive_timeout_seconds=0.01,
    )
    await client.connect()

    with pytest.raises(OpenAIRealtimeConnectionClosed, match="receive timed out"):
        await anext(client.events())
