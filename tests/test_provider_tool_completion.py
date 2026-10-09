"""Provider streams must finish one valid call before it can reach an effect."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import orjson
import pytest

from ai_runtime.providers import claude, openai_chat, openai_responses, xai
from ai_runtime.tooling.execution import _build_tool_response_messages
from tests.test_claude_inference_parameters import _FakeSession as ClaudeSession
from tests.test_openai_chat_reasoning import _FakeSession as ChatSession
from tests.test_xai_responses import _FakeSession as ResponsesSession


PROVIDERS = {
    "openai": (openai_responses, openai_responses.call_gpt_responses_api, "GPT"),
    "xai": (xai, xai.call_xai_responses_api, "xAI"),
    "chat": (openai_chat, openai_chat.call_llm_api, "OpenRouter"),
    "claude": (claude, claude.call_claude_api, "Claude"),
}
CASES = ["complete", "empty_object", "failed", "transport", "timeout", "exception", "cancelled", "eof", "incomplete", "stop",
         "invalid_json", "invalid_shape", "multiple", "multiple_id"]
INTERRUPTED_CASES = {"transport", "timeout", "exception", "cancelled"}


def _tool_stream(provider, case):
    arguments = {"invalid_json": "{", "invalid_shape": "[]", "empty_object": "{}"}.get(case, '{"q":"docs"}')
    multiple = case in {"multiple", "multiple_id"}
    second_index = 0 if case == "multiple_id" else 1
    if provider == "claude":
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 20}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": arguments[:1]}},
        ]
        if multiple:
            events += [
                {"type": "content_block_start", "index": second_index,
                 "content_block": {"type": "tool_use", "id": "call_2", "name": "lookup", "input": {}}},
                {"type": "content_block_delta", "index": second_index,
                 "delta": {"type": "input_json_delta", "partial_json": '{"q":"other"}'}},
            ]
        events += [
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": arguments[1:]}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens" if case == "incomplete" else "tool_use"},
             "usage": {"output_tokens": 2}},
        ]
        if case == "failed":
            events += [{"type": "error", "error": {"type": "overloaded_error", "message": "Synthetic failure"}}]
        if case != "eof" and case not in INTERRUPTED_CASES:
            events += [{"type": "message_stop"}]
        return [b"data: " + orjson.dumps(event) + b"\n" for event in events]

    if provider == "chat":
        events = [{"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "lookup", "arguments": arguments[:1]}}
        ]}}], "usage": {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22}}]
        if multiple:
            events += [{"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": second_index, "id": "call_2", "function": {"name": "lookup", "arguments": '{"q":"other"}'}}
            ]}}]}]
        events += [{"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": arguments[1:]}}
        ]}}]}]
        if case == "failed":
            events += [{"error": {"message": "Synthetic failure"}}]
        elif case != "eof" and case not in INTERRUPTED_CASES:
            events += [{"choices": [{"index": 0, "delta": {},
                        "finish_reason": "length" if case == "incomplete" else "tool_calls"}]}]
        data = ["data: " + orjson.dumps(event).decode() for event in events]
        if case not in {"eof", "missing_done"} and case not in INTERRUPTED_CASES:
            data += ["data: [DONE]"]
        return "\n\n".join(data) + "\n\n"

    function = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "lookup", "arguments": arguments}
    events = [
        {"type": "response.output_item.added", "output_index": 0, "item": {**function, "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "fc_1", "delta": arguments[:1]},
    ]
    final_output = [function]
    if multiple:
        second = {**function, "id": "fc_2", "call_id": "call_2", "arguments": '{"q":"other"}'}
        events += [
            {"type": "response.output_item.added", "output_index": second_index, "item": {**second, "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "output_index": second_index, "item_id": "fc_2", "delta": second["arguments"]},
        ]
        final_output += [second]
    events += [{"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "fc_1", "delta": arguments[1:]}]
    # Real partial usage can be reported by a failed response, not only completed.
    response = {"usage": {"input_tokens": 20, "output_tokens": 2}, "output": final_output}
    if multiple:
        # A summary must not hide the two calls seen in interleaved deltas.
        del response["output"]
    if case == "missing_final_call":
        response["output"] = []
    if case == "multiple_final":
        events = []
        response["output"] += [{**function, "id": "fc_2", "call_id": "call_2"}]
    if case == "failed":
        events += [{"type": "response.failed", "response": {**response, "status": "failed", "error": {"message": "Synthetic failure"}}}]
    elif case == "incomplete":
        events += [{"type": "response.incomplete", "response": {**response, "status": "incomplete"}}]
    elif case != "eof" and case not in INTERRUPTED_CASES:
        events += [{"type": "response.completed", "response": {**response, "status": "completed"}}]
    return "\n\n".join("data: " + orjson.dumps(event).decode() for event in events) + "\n\n"


class _InterruptedContent:
    """Use the existing HTTP harness, interrupting only its transport boundary."""
    def __init__(self, original, case):
        self.original, self.case = original, case

    async def _iterate(self, iterator):
        async for chunk in iterator:
            yield chunk
        if self.case == "transport":
            raise aiohttp.ClientError("Synthetic connection lost")
        if self.case == "timeout":
            raise asyncio.TimeoutError("Synthetic timeout")
        if self.case == "exception":
            raise ValueError("Synthetic stream failure")
        if self.case == "cancelled":
            raise asyncio.CancelledError

    def __aiter__(self):
        return self._iterate(self.original)

    def iter_any(self):
        return self._iterate(self.original.iter_any())

    def iter_chunked(self, size):
        return self._iterate(self.original.iter_chunked(size))


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,case", [(provider, case) for provider in PROVIDERS for case in CASES]
                         + [(provider, case) for provider in ("openai", "xai")
                            for case in ("missing_final_call", "multiple_final")])
async def test_only_one_complete_object_call_can_reach_dispatch(monkeypatch, provider, case):
    module, call, machine = PROVIDERS[provider]
    captured = {}
    payload = _tool_stream(provider, case)
    base = ClaudeSession if provider == "claude" else ChatSession if provider == "chat" else ResponsesSession

    class Session(base):
        def post(self, *args, **kwargs):
            response = super().post(*args, **kwargs)
            response.content = _InterruptedContent(response.content, case)
            return response

    session = Session(captured, stream_lines=payload) if provider == "claude" else Session(payload, captured)
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda *args, **kwargs: session)
    monkeypatch.setitem(module.stop_signals, 123, False)
    save, success, error = AsyncMock(return_value=(10, 11)), AsyncMock(), AsyncMock()
    async def accumulate(**kwargs):
        if case == "stop":
            monkeypatch.setitem(module.stop_signals, 123, True)
        return 20, 2
    accumulated = AsyncMock(side_effect=accumulate)
    for name, mock in (("save_content_to_db", save), ("record_provider_success_for_label", success),
                       ("record_provider_error_for_label", error), ("accumulate_ai_provider_call_usage", accumulated)):
        monkeypatch.setattr(module, name, mock)
    kwargs = dict(messages=[{"role": "user", "content": "Look up the docs."}], model="test-model",
                  temperature=0.3, max_tokens=100, prompt="Assistant", conversation_id=123,
                  current_user=SimpleNamespace(id=1), request=None, billing_reservation_id="test-hold")
    if provider == "chat":
        kwargs.update(api_url="https://example.invalid", api_key="fixture", provider_label="OpenRouter",
                      tools=[{"type": "function", "function": {"name": "lookup"}}])
    else:
        kwargs.update(user_api_key="fixture", tools=[{"name": "lookup", "input_schema": {"type": "object"}}]
                      if provider == "claude" else [{"type": "function", "name": "lookup"}])

    if case == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            [chunk async for chunk in call(**kwargs)]
        accumulated.assert_awaited_once()
        save.assert_not_awaited()
        success.assert_not_awaited()
        error.assert_not_awaited()
        return
    stream = call(**kwargs)
    chunks = []
    async for chunk in stream:
        chunks.append(chunk)
        if chunk.startswith("data: {") and orjson.loads(chunk[6:]).get("error"):
            if provider == "openai":
                accumulated.assert_awaited_once()
            if case in INTERRUPTED_CASES:
                await stream.aclose()
                break
    events = [orjson.loads(chunk[6:]) for chunk in chunks if chunk.startswith("data: {")]
    calls = [event["tool_call"] for event in events if "tool_call" in event]
    accumulated.assert_awaited_once()
    if case in INTERRUPTED_CASES:
        assert accumulated.await_args.kwargs["output_payload"][-1] == '{"q":"docs"}'
    save.assert_not_awaited()
    if case in {"complete", "empty_object"}:
        assert len(calls) == 1 and calls[0]["arguments"] == ({} if case == "empty_object" else {"q": "docs"})
        continuation = []
        _build_tool_response_messages(continuation, calls[0], "Found", machine)
        if provider in {"openai", "xai"}:
            assert continuation[-2]["call_id"] == continuation[-1]["call_id"] == "call_1"
            assert captured["json"]["parallel_tool_calls"] is False
        elif provider == "claude":
            assert continuation[-2]["content"][0]["id"] == continuation[-1]["content"][0]["tool_use_id"] == "call_1"
        else:
            assert continuation[-2]["tool_calls"][0]["id"] == continuation[-1]["tool_call_id"] == "call_1"
            assert "parallel_tool_calls" not in captured["json"]
        success.assert_awaited_once()
        error.assert_not_awaited()
    else:
        assert calls == [] and not any(event.get("tool_call_pending") for event in events)
        assert any(event.get("error") for event in events)
        if case in {"invalid_json", "invalid_shape", "multiple", "multiple_id", "multiple_final"}:
            assert events[-1]["error_code"] == ("multiple_tool_calls_unsupported" if case.startswith("multiple") else "invalid_tool_arguments")
        success.assert_not_awaited()
        error.assert_awaited_once()
        if case == "failed":
            assert accumulated.await_args.kwargs["reported_input_tokens"] == 20
            assert accumulated.await_args.kwargs["reported_output_tokens"] == 2


@pytest.mark.asyncio
async def test_chat_finish_reason_without_done_does_not_dispatch(monkeypatch):
    monkeypatch.setattr(openai_chat.aiohttp, "ClientSession",
                        lambda *args, **kwargs: ChatSession(_tool_stream("chat", "missing_done"), {}))
    success = AsyncMock()
    monkeypatch.setattr(openai_chat, "record_provider_success_for_label", success)
    monkeypatch.setattr(openai_chat, "record_provider_error_for_label", AsyncMock())
    monkeypatch.setitem(openai_chat.stop_signals, 123, False)
    events = [orjson.loads(chunk[6:]) async for chunk in openai_chat.call_llm_api(
        messages=[], model="test-model", temperature=0.3, max_tokens=100, prompt="Assistant",
        conversation_id=123, current_user=SimpleNamespace(id=1), request=None,
        api_url="https://example.invalid", api_key="fixture", provider_label="OpenRouter")]
    assert events == [{"error": "tool_call_incomplete", "error_code": "tool_call_incomplete"}]
    success.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_closing_consumer_keeps_received_usage(monkeypatch, provider):
    module, call, _ = PROVIDERS[provider]
    if provider == "claude":
        payload = [b"data: " + orjson.dumps(event) + b"\n" for event in [
            {"type": "message_start", "message": {"usage": {"input_tokens": 20}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "partial"}},
        ]]
        session = ClaudeSession({}, stream_lines=payload)
    else:
        event = ({"choices": [{"delta": {"content": "partial"}}],
                  "usage": {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22}}
                 if provider == "chat" else {"type": "response.output_text.delta", "delta": "partial"})
        payload = "data: " + orjson.dumps(event).decode() + "\n\n"
        session = ChatSession(payload, {}) if provider == "chat" else ResponsesSession(payload, {})
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda *args, **kwargs: session)
    monkeypatch.setitem(module.stop_signals, 123, False)
    accumulated = AsyncMock(return_value=(20, 2))
    success, save = AsyncMock(), AsyncMock()
    monkeypatch.setattr(module, "accumulate_ai_provider_call_usage", accumulated)
    monkeypatch.setattr(module, "record_provider_success_for_label", success)
    monkeypatch.setattr(module, "save_content_to_db", save)
    kwargs = dict(messages=[], model="test-model", temperature=0.3, max_tokens=100, prompt="Assistant",
                  conversation_id=123, current_user=SimpleNamespace(id=1), request=None,
                  billing_reservation_id="test-hold")
    if provider == "chat":
        kwargs.update(api_url="https://example.invalid", api_key="fixture", provider_label="OpenRouter")
    else:
        kwargs.update(user_api_key="fixture")
    stream = call(**kwargs)
    assert orjson.loads((await anext(stream))[6:])["content"] == "partial"
    await stream.aclose()
    accumulated.assert_awaited_once()
    assert accumulated.await_args.kwargs["output_payload"][0] == "partial"
    success.assert_not_awaited()
    save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_label,allowed", [("MiniMax", True), ("OpenRouter", False)])
async def test_chat_stop_finish_reason_is_provider_specific(monkeypatch, provider_label, allowed):
    payload = _tool_stream("chat", "complete").replace('"finish_reason":"tool_calls"', '"finish_reason":"stop"')
    monkeypatch.setattr(openai_chat.aiohttp, "ClientSession", lambda *args, **kwargs: ChatSession(payload, {}))
    monkeypatch.setattr(openai_chat, "record_provider_success_for_label", AsyncMock())
    monkeypatch.setattr(openai_chat, "record_provider_error_for_label", AsyncMock())
    monkeypatch.setitem(openai_chat.stop_signals, 123, False)
    events = [orjson.loads(chunk[6:]) async for chunk in openai_chat.call_llm_api(
        messages=[], model="test-model", temperature=0.3, max_tokens=100, prompt="Assistant",
        conversation_id=123, current_user=SimpleNamespace(id=1), request=None,
        api_url="https://example.invalid", api_key="fixture", provider_label=provider_label)]
    assert len([event for event in events if "tool_call" in event]) == int(allowed)
    assert any(event.get("error") for event in events) is not allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "xai", "claude"])
async def test_failed_plain_text_keeps_partial_persistence_without_health_success(monkeypatch, provider):
    module, call, _ = PROVIDERS[provider]
    failure = {"message": "Synthetic failure"}
    if provider == "claude":
        payload = [b"data: " + orjson.dumps(event) + b"\n" for event in [
            {"type": "message_start", "message": {"usage": {"input_tokens": 20}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "partial"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "error", "error": failure},
        ]]
        session = ClaudeSession({}, stream_lines=payload)
    else:
        payload = "\n\n".join("data: " + orjson.dumps(event).decode() for event in [
            {"type": "response.output_text.delta", "delta": "partial"},
            {"type": "response.failed", "response": {"error": failure, "usage": {"input_tokens": 20, "output_tokens": 2}}},
        ]) + "\n\n"
        session = ResponsesSession(payload, {})
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda *args, **kwargs: session)
    monkeypatch.setitem(module.stop_signals, 123, False)
    save, success = AsyncMock(return_value=(10, 11)), AsyncMock()
    accumulated = AsyncMock(return_value=(20, 2))
    monkeypatch.setattr(module, "save_content_to_db", save)
    monkeypatch.setattr(module, "record_provider_success_for_label", success)
    monkeypatch.setattr(module, "record_provider_error_for_label", AsyncMock())
    monkeypatch.setattr(module, "accumulate_ai_provider_call_usage", accumulated)
    chunks = [chunk async for chunk in call(messages=[], model="test-model", temperature=0.3,
        max_tokens=100, prompt="Assistant", conversation_id=123, current_user=SimpleNamespace(id=1),
        request=None, user_api_key="fixture", billing_reservation_id="test-hold")]
    assert any('"error"' in chunk for chunk in chunks)
    accumulated.assert_awaited_once()
    save.assert_awaited_once()
    assert save.await_args.args[0] == "partial"
    success.assert_not_awaited()
