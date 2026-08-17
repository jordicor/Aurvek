from unittest.mock import AsyncMock

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
