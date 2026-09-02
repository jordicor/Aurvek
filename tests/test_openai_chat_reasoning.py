import orjson
import aiohttp
import pytest
from unittest.mock import AsyncMock

from ai_runtime.providers import openai_chat


class _User:
    id = 1


class _FakeContent:
    def __init__(self, payload: str):
        self._payload = payload.encode("utf-8")

    async def iter_chunked(self, _size):
        yield self._payload


class _FakeResponse:
    status = 200

    def __init__(self, payload: str):
        self.content = _FakeContent(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return ""

    async def json(self):
        return {}


class _FakeSession:
    def __init__(self, payload: str, captured: dict):
        self._payload = payload
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, _url, headers=None, json=None):
        self._captured["headers"] = headers or {}
        self._captured["json"] = json or {}
        return _FakeResponse(self._payload)


def _sse_payload(event: str) -> dict:
    assert event.startswith("data: ")
    return orjson.loads(event[6:].strip())


@pytest.mark.asyncio
async def test_partial_stream_usage_is_accumulated_even_when_provider_then_fails(
    monkeypatch,
):
    class _FailingContent:
        async def iter_chunked(self, _size):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise aiohttp.ClientError("connection lost")

    class _FailingResponse(_FakeResponse):
        def __init__(self):
            self.content = _FailingContent()

    class _FailingSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            return _FailingResponse()

    accumulate = AsyncMock(return_value=(20, 2))
    save = AsyncMock(return_value=(10, 11))
    monkeypatch.setattr(openai_chat.aiohttp, "ClientSession", _FailingSession)
    monkeypatch.setattr(
        openai_chat,
        "accumulate_ai_provider_call_usage",
        accumulate,
    )
    monkeypatch.setattr(openai_chat, "save_content_to_db", save)
    monkeypatch.setattr(openai_chat, "record_provider_error_for_label", AsyncMock())
    monkeypatch.setattr(openai_chat, "record_provider_success_for_label", AsyncMock())

    chunks = [
        chunk
        async for chunk in openai_chat.call_llm_api(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-test",
            temperature=0.7,
            max_tokens=100,
            prompt="system",
            conversation_id=123,
            current_user=_User(),
            request=None,
            api_url="https://example.invalid/v1/chat/completions",
            api_key="test",
            provider_label="OpenAI (GPT)",
            user_message="hi",
            save_to_db=True,
            billing_reservation_id="ai-reservation",
        )
    ]

    assert any("partial" in chunk for chunk in chunks)
    accumulate.assert_awaited_once()
    assert accumulate.await_args.kwargs["output_payload"][0] == "partial"
    save.assert_awaited_once()


@pytest.mark.asyncio
async def test_persistence_failure_emits_error_without_normal_completion(monkeypatch):
    payload = "\n\n".join([
        'data: {"choices":[{"delta":{"content":"generated answer"}}]}',
        'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}',
        "data: [DONE]",
        "",
    ])
    captured = {}
    monkeypatch.setattr(
        openai_chat.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeSession(payload, captured),
    )
    monkeypatch.setattr(
        openai_chat,
        "save_content_to_db",
        AsyncMock(return_value=(None, None)),
    )
    monkeypatch.setattr(
        openai_chat,
        "record_provider_success_for_label",
        AsyncMock(),
    )

    chunks = [
        chunk
        async for chunk in openai_chat.call_llm_api(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-test",
            temperature=0.7,
            max_tokens=100,
            prompt="system",
            conversation_id=123,
            current_user=_User(),
            request=None,
            api_url="https://example.invalid/v1/chat/completions",
            api_key="test",
            provider_label="OpenAI (GPT)",
            user_message="hi",
            save_to_db=True,
        )
    ]

    payloads = [_sse_payload(chunk) for chunk in chunks if chunk.startswith("data: {")]
    assert any(item.get("content") == "generated answer" for item in payloads)
    assert payloads[-1]["persistence_error"] is True
    assert chunks[-1].startswith("data: ")


@pytest.mark.asyncio
async def test_o1_reasoning_tokens_are_not_charged_twice(monkeypatch):
    captured_request = {}
    response_payload = {
        "choices": [{"message": {"content": "answer"}}],
        "usage": {
            "prompt_tokens": 5,
            "completion_tokens": 11,
            "total_tokens": 16,
            "completion_tokens_details": {"reasoning_tokens": 7},
        },
    }

    class _O1Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def json(self):
            return response_payload

        async def text(self):
            return ""

    class _O1Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            captured_request.update(kwargs["json"])
            return _O1Response()

    save = AsyncMock(return_value=(10, 11))
    monkeypatch.setattr(openai_chat.aiohttp, "ClientSession", _O1Session)
    monkeypatch.setattr(openai_chat, "save_content_to_db", save)
    monkeypatch.setattr(
        openai_chat,
        "accumulate_ai_provider_call_usage",
        AsyncMock(return_value=(5, 11)),
    )
    monkeypatch.setattr(
        openai_chat,
        "record_provider_success_for_label",
        AsyncMock(),
    )

    chunks = [
        chunk
        async for chunk in openai_chat.call_o1_api(
            messages=[{"role": "user", "content": "hi"}],
            model="o1-test",
            temperature=1,
            max_tokens=100,
            prompt="system",
            conversation_id=321,
            current_user=_User(),
            request=None,
            user_message="hi",
            llm_id=9,
            billing_reservation_id="ai-reservation",
        )
    ]

    assert chunks
    save.assert_awaited_once()
    args = save.await_args.args
    assert args[1:4] == (5, 11, 16)
    assert save.await_args.kwargs["billing_reservation_id"] == "ai-reservation"
    assert captured_request["messages"][0] == {
        "role": "developer",
        "content": "system",
    }


@pytest.mark.asyncio
async def test_openai_compatible_reasoning_streams_as_thinking(monkeypatch):
    payload = "\n\n".join([
        'data: {"choices":[{"delta":{"reasoning_content":"think "}}]}',
        'data: {"choices":[{"delta":{"reasoning_content":"more"}}]}',
        'data: {"choices":[{"delta":{"content":"answer"}}],"usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}',
        "data: [DONE]",
        "",
    ])
    captured = {}
    monkeypatch.setattr(
        openai_chat.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeSession(payload, captured),
    )

    chunks = [
        chunk async for chunk in openai_chat.call_llm_api(
            messages=[{"role": "user", "content": "hi"}],
            model="kimi-k2.7-code",
            temperature=0.7,
            max_tokens=100,
            prompt="system",
            conversation_id=123,
            current_user=_User(),
            request=None,
            api_url="https://example.invalid/v1/chat/completions",
            api_key="test",
            provider_label="Kimi",
            save_to_db=False,
            omit_temperature=True,
        )
    ]

    parsed = [_sse_payload(chunk) for chunk in chunks if chunk.startswith("data: {")]
    assert parsed[:4] == [
        {"type": "thinking_start"},
        {"thinking": "think ", "type": "thinking"},
        {"thinking": "more", "type": "thinking"},
        {"type": "thinking_end"},
    ]
    assert parsed[4] == {"content": "answer"}
    assert "temperature" not in captured["json"]


@pytest.mark.asyncio
async def test_openai_compatible_textual_thinking_tag_is_not_saved_as_answer(
    monkeypatch,
):
    payload = "\n\n".join([
        'data: {"choices":[{"delta":{"content":"antml:thi"}}]}',
        'data: {"choices":[{"delta":{"content":"nking>private "}}]}',
        'data: {"choices":[{"delta":{"content":"reasoning</thi"}}]}',
        'data: {"choices":[{"delta":{"content":"nking>\\n\\nVisible answer"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}',
        "data: [DONE]",
        "",
    ])
    captured = {}
    save = AsyncMock(return_value=(10, 11))
    monkeypatch.setattr(
        openai_chat.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeSession(payload, captured),
    )
    monkeypatch.setattr(openai_chat, "save_content_to_db", save)
    monkeypatch.setattr(
        openai_chat,
        "record_provider_success_for_label",
        AsyncMock(),
    )

    chunks = [
        chunk async for chunk in openai_chat.call_llm_api(
            messages=[{"role": "user", "content": "hi"}],
            model="google/gemini-test",
            temperature=0.7,
            max_tokens=100,
            prompt="system",
            conversation_id=123,
            current_user=_User(),
            request=None,
            api_url="https://example.invalid/v1/chat/completions",
            api_key="test",
            provider_label="OpenRouter",
            user_message="hi",
            save_to_db=True,
        )
    ]

    payloads = [_sse_payload(chunk) for chunk in chunks if chunk.startswith("data: {")]
    assert payloads[0] == {"type": "thinking_start"}
    assert "".join(
        item.get("thinking", "") for item in payloads
    ) == "private reasoning"
    assert {"type": "thinking_end"} in payloads
    assert "".join(item.get("content", "") for item in payloads) == "Visible answer"
    save.assert_awaited_once()
    assert save.await_args.args[0] == "Visible answer"


@pytest.mark.asyncio
async def test_openai_compatible_tool_call_preserves_reasoning_content(monkeypatch):
    payload = "\n\n".join([
        'data: {"choices":[{"delta":{"reasoning_content":"need a search"}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"id":"call_1","function":{"name":"query_perplexity","arguments":"{\\"query\\":\\"docs\\"}"}}]},"finish_reason":"tool_calls"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}',
        "data: [DONE]",
        "",
    ])
    captured = {}
    monkeypatch.setattr(
        openai_chat.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeSession(payload, captured),
    )

    chunks = [
        chunk async for chunk in openai_chat.call_llm_api(
            messages=[{"role": "user", "content": "hi"}],
            model="kimi-k2.7-code",
            temperature=0.7,
            max_tokens=100,
            prompt="system",
            conversation_id=123,
            current_user=_User(),
            request=None,
            api_url="https://example.invalid/v1/chat/completions",
            api_key="test",
            provider_label="Kimi",
            user_message="hi",
            save_to_db=True,
            omit_temperature=True,
        )
    ]

    tool_events = [
        _sse_payload(chunk)["tool_call"]
        for chunk in chunks
        if chunk.startswith("data: {") and "tool_call" in chunk and "tool_call_pending" not in chunk
    ]
    assert tool_events == [{
        "name": "query_perplexity",
        "arguments": {"query": "docs"},
        "id": "call_1",
        "_billing_usage": {"input_tokens": 2, "output_tokens": 3},
        "reasoning_content": "need a search",
    }]


@pytest.mark.asyncio
async def test_openai_compatible_reasoning_details_use_cumulative_delta(monkeypatch):
    payload = "\n\n".join([
        'data: {"choices":[{"delta":{"reasoning_details":[{"type":"reasoning.text","text":"think"}]}}]}',
        'data: {"choices":[{"delta":{"reasoning_details":[{"type":"reasoning.text","text":"thinking","signature":"opaque"}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"id":"call_1","function":{"name":"query_perplexity","arguments":"{\\"query\\":\\"docs\\"}"}}]},"finish_reason":"tool_calls"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}',
        "data: [DONE]",
        "",
    ])
    captured = {}
    monkeypatch.setattr(
        openai_chat.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeSession(payload, captured),
    )

    chunks = [
        chunk async for chunk in openai_chat.call_llm_api(
            messages=[{"role": "user", "content": "hi"}],
            model="MiniMax-M3",
            temperature=1.0,
            max_tokens=100,
            prompt="system",
            conversation_id=123,
            current_user=_User(),
            request=None,
            api_url="https://example.invalid/v1/chat/completions",
            api_key="test",
            provider_label="MiniMax",
            save_to_db=True,
        )
    ]

    parsed = [_sse_payload(chunk) for chunk in chunks if chunk.startswith("data: {")]
    assert parsed[:4] == [
        {"type": "thinking_start"},
        {"thinking": "think", "type": "thinking"},
        {"thinking": "ing", "type": "thinking"},
        {"type": "thinking_end"},
    ]
    tool_call = next(event["tool_call"] for event in parsed if "tool_call" in event)
    assert tool_call["reasoning_details"] == [{
        "type": "reasoning.text",
        "text": "thinking",
        "signature": "opaque",
    }]


@pytest.mark.asyncio
async def test_openai_compatible_reasoning_details_preserve_unindexed_sequence(monkeypatch):
    payload = "\n\n".join([
        'data: {"choices":[{"delta":{"reasoning_details":[{"type":"reasoning.text","text":"think "}]}}]}',
        'data: {"choices":[{"delta":{"reasoning_details":[{"type":"reasoning.text","text":"more"}]}}]}',
        'data: {"choices":[{"delta":{"reasoning_details":[{"type":"reasoning.summary","summary":"short "}]}}]}',
        'data: {"choices":[{"delta":{"reasoning_details":[{"type":"reasoning.summary","summary":"summary"}]}}]}',
        'data: {"choices":[{"delta":{"reasoning_details":[{"type":"reasoning.encrypted","data":"abc"}]}}]}',
        'data: {"choices":[{"delta":{"reasoning_details":[{"type":"reasoning.encrypted","data":"def"}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"id":"call_1","function":{"name":"query_perplexity","arguments":"{\\"query\\":\\"docs\\"}"}}]},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
        "",
    ])
    captured = {}
    monkeypatch.setattr(
        openai_chat.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeSession(payload, captured),
    )

    chunks = [
        chunk async for chunk in openai_chat.call_llm_api(
            messages=[{"role": "user", "content": "hi"}],
            model="vendor/reasoning-model",
            temperature=0.7,
            max_tokens=100,
            prompt="system",
            conversation_id=124,
            current_user=_User(),
            request=None,
            api_url="https://example.invalid/v1/chat/completions",
            api_key="test",
            provider_label="OpenRouter",
            save_to_db=True,
        )
    ]

    parsed = [_sse_payload(chunk) for chunk in chunks if chunk.startswith("data: {")]
    tool_call = next(event["tool_call"] for event in parsed if "tool_call" in event)
    assert tool_call["reasoning_details"] == [
        {"type": "reasoning.text", "text": "think more"},
        {"type": "reasoning.summary", "summary": "short summary"},
        {"type": "reasoning.encrypted", "data": "abcdef"},
    ]
