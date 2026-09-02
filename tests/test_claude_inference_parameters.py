from unittest.mock import AsyncMock

import orjson
import pytest

import security_guard_llm
from ai_runtime.providers import claude, openrouter
from ai_runtime.providers.claude_capabilities import claude_omits_temperature
from tools import llm_caller


class _User:
    id = 1


class _FakeContent:
    def __init__(self, lines):
        self._lines = iter(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._lines)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeResponse:
    status = 200

    def __init__(self, result=None, stream_lines=None):
        self._result = result or {}
        self.content = _FakeContent(stream_lines or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._result

    async def text(self):
        return ""


class _FakeSession:
    def __init__(self, captured, result=None, stream_lines=None):
        self._captured = captured
        self._result = result
        self._stream_lines = stream_lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, _url, headers=None, json=None, **_kwargs):
        self._captured["json"] = json or {}
        return _FakeResponse(self._result, self._stream_lines)


_CLAUDE_STREAM = [
    b'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}\n',
    b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n',
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n',
    b'data: {"type":"content_block_stop","index":0}\n',
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n',
    b'data: {"type":"message_stop"}\n',
]


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-5", True),
        ("CLAUDE-OPUS-5", True),
        ("anthropic/claude-opus-5-fast", True),
        ("anthropic.claude-opus-5", True),
        ("claude-opus-4-8", True),
        ("claude-opus-4-5-20251101", False),
        ("claude-haiku-4-5", False),
    ],
)
def test_claude_temperature_capability(model, expected):
    assert claude_omits_temperature(model) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "expected_temperature"),
    [
        ("claude-opus-5", None),
        ("claude-opus-4-5-20251101", 0.37),
    ],
)
async def test_streaming_claude_request_temperature(
    monkeypatch, model, expected_temperature
):
    captured = {}
    monkeypatch.setattr(
        claude.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeSession(
            captured,
            stream_lines=_CLAUDE_STREAM,
        ),
    )
    monkeypatch.setattr(
        claude,
        "record_provider_success_for_label",
        AsyncMock(),
    )

    chunks = [
        chunk
        async for chunk in claude.call_claude_api(
            messages=[{"role": "user", "content": "hello"}],
            model=model,
            temperature=0.37,
            max_tokens=100,
            prompt="system",
            conversation_id=987654,
            current_user=_User(),
            request=None,
            user_api_key="test-key",
            save_to_db=False,
        )
    ]

    assert chunks
    if expected_temperature is None:
        assert "temperature" not in captured["json"]
    else:
        assert captured["json"]["temperature"] == expected_temperature


@pytest.mark.asyncio
async def test_claude_disabled_thinking_keeps_requested_temperature(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        claude.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeSession(captured, stream_lines=_CLAUDE_STREAM),
    )
    monkeypatch.setattr(claude, "record_provider_success_for_label", AsyncMock())

    _ = [
        chunk async for chunk in claude.call_claude_api(
            messages=[{"role": "user", "content": "hello"}],
            model="claude-opus-4-5-20251101",
            temperature=0.37,
            max_tokens=100,
            prompt="system",
            conversation_id=987657,
            current_user=_User(),
            request=None,
            user_api_key="test-key",
            reasoning_selection={"mode": "off"},
            save_to_db=False,
        )
    ]

    assert captured["json"]["thinking"] == {"type": "disabled"}
    assert captured["json"]["temperature"] == 0.37


@pytest.mark.asyncio
async def test_claude_effort_does_not_enable_unsupported_adaptive_thinking(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        claude.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeSession(captured, stream_lines=_CLAUDE_STREAM),
    )
    monkeypatch.setattr(claude, "record_provider_success_for_label", AsyncMock())

    _ = [
        chunk async for chunk in claude.call_claude_api(
            messages=[{"role": "user", "content": "hello"}],
            model="claude-opus-4-5-20251101",
            temperature=0.37,
            max_tokens=100,
            prompt="system",
            conversation_id=987659,
            current_user=_User(),
            request=None,
            user_api_key="test-key",
            reasoning_selection={"mode": "high"},
            tools=[{"name": "lookup", "input_schema": {"type": "object"}}],
            tool_choice={"type": "none"},
            save_to_db=False,
        )
    ]

    assert "thinking" not in captured["json"]
    assert captured["json"]["output_config"] == {"effort": "high"}
    assert captured["json"]["tool_choice"] == {"type": "none"}
    assert captured["json"]["temperature"] == 0.37


@pytest.mark.asyncio
async def test_claude_tool_call_keeps_signed_thinking_blocks(monkeypatch):
    captured = {}
    stream = [
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}\n',
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"plan"}}\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"signed"}}\n',
        b'data: {"type":"content_block_stop","index":0}\n',
        b'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_1","name":"lookup","input":{}}}\n',
        b'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{}"}}\n',
        b'data: {"type":"content_block_stop","index":1}\n',
        b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":1}}\n',
        b'data: {"type":"message_stop"}\n',
    ]
    monkeypatch.setattr(claude.aiohttp, "ClientSession", lambda *args, **kwargs: _FakeSession(captured, stream_lines=stream))
    monkeypatch.setattr(claude, "record_provider_success_for_label", AsyncMock())

    chunks = [
        chunk async for chunk in claude.call_claude_api(
            messages=[{"role": "user", "content": "hello"}], model="claude-opus-4-5-20251101",
            temperature=0.37, max_tokens=100, prompt="system", conversation_id=987658,
            current_user=_User(), request=None, user_api_key="test-key",
        )
    ]

    tool_event = next(
        orjson.loads(chunk[6:].strip())["tool_call"]
        for chunk in chunks
        if chunk.startswith("data: {") and "tool_call" in chunk and "tool_call_pending" not in chunk
    )
    assert tool_event["claude_content_blocks"][0] == {
        "type": "thinking", "thinking": "plan", "signature": "signed"
    }


@pytest.mark.asyncio
async def test_claude_pause_turn_blocks_survive_later_client_tool_call(monkeypatch):
    first_stream = [
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}\n',
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"plan"}}\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"signed-pause"}}\n',
        b'data: {"type":"content_block_stop","index":0}\n',
        b'data: {"type":"message_delta","delta":{"stop_reason":"pause_turn"},"usage":{"output_tokens":1}}\n',
        b'data: {"type":"message_stop"}\n',
    ]
    second_stream = [
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}\n',
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_2","name":"lookup","input":{}}}\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"q\\":\\"docs\\"}"}}\n',
        b'data: {"type":"content_block_stop","index":0}\n',
        b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":1}}\n',
        b'data: {"type":"message_stop"}\n',
    ]

    class _SequenceSession:
        def __init__(self):
            self.streams = iter((first_stream, second_stream))

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def post(self, *_args, **_kwargs):
            return _FakeResponse(stream_lines=next(self.streams))

    monkeypatch.setattr(claude.aiohttp, "ClientSession", _SequenceSession)
    monkeypatch.setattr(claude, "record_provider_success_for_label", AsyncMock())

    events = [
        orjson.loads(chunk[6:].strip())
        async for chunk in claude.call_claude_api(
            messages=[{"role": "user", "content": "hello"}],
            model="claude-opus-4-6",
            temperature=0.37,
            max_tokens=100,
            prompt="system",
            conversation_id=987660,
            current_user=_User(),
            request=None,
            user_api_key="test-key",
            save_to_db=True,
        )
        if chunk.startswith("data: {")
    ]

    tool_event = next(event["tool_call"] for event in events if "tool_call" in event)
    assert tool_event["claude_content_blocks"] == [
        {"type": "thinking", "thinking": "plan", "signature": "signed-pause"},
        {"type": "tool_use", "id": "toolu_2", "name": "lookup", "input": {"q": "docs"}},
    ]


@pytest.mark.asyncio
async def test_claude_textual_thinking_tag_is_not_saved_as_answer(monkeypatch):
    captured = {}
    stream = [
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}\n',
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"antml:thi"}}\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"nking>private reasoning</thi"}}\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"nking>\\n\\nVisible answer"}}\n',
        b'data: {"type":"content_block_stop","index":0}\n',
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n',
        b'data: {"type":"message_stop"}\n',
    ]
    save = AsyncMock(return_value=(10, 11))
    monkeypatch.setattr(
        claude.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeSession(captured, stream_lines=stream),
    )
    monkeypatch.setattr(claude, "save_content_to_db", save)
    monkeypatch.setattr(
        claude,
        "record_provider_success_for_label",
        AsyncMock(),
    )

    chunks = [
        chunk
        async for chunk in claude.call_claude_api(
            messages=[{"role": "user", "content": "hello"}],
            model="claude-opus-4-6",
            temperature=0.37,
            max_tokens=100,
            prompt="system",
            conversation_id=987656,
            current_user=_User(),
            request=None,
            user_api_key="test-key",
            user_message="hello",
            save_to_db=True,
        )
    ]

    payloads = [
        orjson.loads(chunk[6:].strip())
        for chunk in chunks
        if chunk.startswith("data: {")
    ]
    assert payloads[0] == {"type": "thinking_start"}
    assert "".join(
        item.get("thinking", "") for item in payloads
    ) == "private reasoning"
    assert {"type": "thinking_end"} in payloads
    assert "".join(item.get("content", "") for item in payloads) == "Visible answer"
    save.assert_awaited_once()
    assert save.await_args.args[0] == "Visible answer"


@pytest.mark.asyncio
async def test_security_guard_claude_opus_5_omits_temperature(monkeypatch):
    captured = {}
    result = {"content": [{"text": '{"decision":"ALLOW"}'}]}
    monkeypatch.setattr(
        security_guard_llm.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeSession(captured, result=result),
    )

    await security_guard_llm._call_claude_security_check(
        "claude-opus-5",
        "hello",
    )

    assert "temperature" not in captured["json"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "model", "result"),
    [
        (
            llm_caller._call_claude,
            "claude-opus-5",
            {"content": [{"text": "ok"}], "usage": {}},
        ),
        (
            llm_caller._call_openrouter,
            "anthropic/claude-opus-5",
            {"choices": [{"message": {"content": "ok"}}], "usage": {}},
        ),
    ],
)
async def test_background_claude_opus_5_omits_temperature(
    monkeypatch, call, model, result
):
    captured = {}
    monkeypatch.setattr(
        llm_caller.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeSession(captured, result=result),
    )

    await call(
        model=model,
        system_prompt="system",
        user_message="hello",
        timeout=30,
        max_tokens=100,
        api_key_override="test-key",
    )

    assert "temperature" not in captured["json"]


@pytest.mark.asyncio
async def test_openrouter_streaming_forwards_temperature_omission(monkeypatch):
    captured = {}

    async def _fake_call_llm_api(*args, **kwargs):
        captured.update(kwargs)
        yield "done"

    monkeypatch.setattr(openrouter, "call_llm_api", _fake_call_llm_api)

    chunks = [
        chunk
        async for chunk in openrouter.call_openrouter_api(
            messages=[{"role": "user", "content": "hello"}],
            model="Claude Opus 5",
            temperature=0.37,
            max_tokens=100,
            prompt="system",
            conversation_id=987655,
            current_user=_User(),
            request=None,
            user_api_key="test-key",
            api_model="anthropic/claude-opus-5",
            save_to_db=False,
        )
    ]

    assert chunks == ["done"]
    assert captured["omit_temperature"] is True
