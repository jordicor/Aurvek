"""Payload translation tests for the provider-neutral reasoning contract."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import orjson
import pytest

from ai_runtime.channel_turns import StaleChannelTurnError
from ai_runtime.providers import claude, gemini, kimi, minimax, openai_chat, openai_responses, openrouter
from ai_runtime.tooling import execution
from ai_runtime.tooling.execution import _build_tool_response_messages


def test_responses_effort_uses_none_for_off_and_omits_default() -> None:
    assert openai_responses._responses_reasoning_payload({"mode": "default"}) is None
    assert openai_responses._responses_reasoning_payload({"mode": "off"}) == {
        "effort": "none",
        "summary": "auto",
    }


def test_openai_chat_effort_omits_default_and_rejects_non_effort_modes() -> None:
    assert openai_chat._chat_reasoning_effort({"mode": "default"}) is None
    assert openai_chat._chat_reasoning_effort({"mode": "off"}) == "none"
    assert openai_chat._chat_reasoning_effort({"mode": "high"}) == "high"
    with pytest.raises(ValueError, match="does not support"):
        openai_chat._chat_reasoning_effort({"mode": "auto"})
    assert openai_responses._responses_reasoning_payload({"mode": "high"}) == {
        "effort": "high",
        "summary": "auto",
    }


def test_claude_uses_one_neutral_path_including_legacy_translation() -> None:
    assert claude._claude_reasoning_payload(
        {"mode": "medium"}, model="claude-opus-4-6"
    ) == (
        {"type": "adaptive", "display": "summarized"},
        {"effort": "medium"},
    )
    assert claude._claude_reasoning_payload(
        {"mode": "medium"}, model="claude-opus-4-5"
    ) == (None, {"effort": "medium"})
    assert claude._claude_reasoning_payload(
        {"mode": "off"}, model="claude-opus-4-5"
    ) == ({"type": "disabled"}, None)
    assert claude._claude_reasoning_payload(
        None, model="claude-opus-4-5", thinking_budget_tokens=4096
    ) == (
        {"type": "enabled", "budget_tokens": 4096},
        None,
    )


def test_gateway_and_toggle_adapters_keep_provider_shapes_distinct() -> None:
    assert openrouter._openrouter_reasoning_payload({"mode": "custom", "budget_tokens": 8000}) == {
        "enabled": True,
        "max_tokens": 8000,
    }
    assert minimax._minimax_reasoning_payload({"mode": "off"}) == {"type": "disabled"}
    assert minimax._minimax_reasoning_payload({"mode": "auto"}) == {"type": "adaptive"}
    assert kimi._kimi_reasoning_payload({"mode": "high"}) == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }


def test_gemini_uses_level_or_exact_budget(monkeypatch) -> None:
    class FakeThinkingConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(gemini.genai_types, "ThinkingConfig", FakeThinkingConfig)

    assert gemini._gemini_thinking_config({"mode": "high"}).kwargs == {
        "thinkingLevel": "high",
        "includeThoughts": True,
    }
    assert gemini._gemini_thinking_config({"mode": "custom", "budget_tokens": 8000}).kwargs == {
        "thinkingBudget": 8000,
        "includeThoughts": True,
    }
    assert gemini._gemini_thinking_config({"mode": "auto"}).kwargs == {
        "thinkingBudget": -1,
        "includeThoughts": True,
    }
    assert gemini._gemini_thinking_config({"mode": "default"}) is None


def test_gemini_native_content_round_trip_preserves_thought_signature() -> None:
    content = gemini.genai_types.Content(
        role="model",
        parts=[gemini.genai_types.Part(thoughtSignature=b"signed-thought")],
    )
    serialized = gemini._serialize_gemini_content(content)
    rebuilt = gemini.genai_types.Content.model_validate(serialized)

    assert serialized["parts"][0]["thoughtSignature"] == "c2lnbmVkLXRob3VnaHQ="
    assert rebuilt.parts[0].thought_signature == b"signed-thought"

    messages = []
    _build_tool_response_messages(
        messages,
        {"name": "lookup", "arguments": {}, "id": "fc_1", "gemini_content": serialized},
        "result",
        "Gemini",
    )
    assert messages[0].parts[0].thought_signature == b"signed-thought"
    assert messages[1].parts[0].function_response.response == {"result": "result"}
    assert messages[1].parts[0].function_response.id == "fc_1"


@pytest.mark.asyncio
async def test_gemini_tool_event_carries_native_signed_content(monkeypatch) -> None:
    signed_content = gemini.genai_types.Content(
        role="model",
        parts=[gemini.genai_types.Part(
            functionCall=gemini.genai_types.FunctionCall(
                id="fc_signed", name="lookup", args={"q": "docs"}
            ),
            thoughtSignature=b"signed-tool-call",
        )],
    )
    chunk = SimpleNamespace(
        prompt_feedback=SimpleNamespace(block_reason=None),
        candidates=[SimpleNamespace(content=signed_content, finish_reason=None)],
        usage_metadata=SimpleNamespace(
            prompt_token_count=2,
            candidates_token_count=3,
            total_token_count=5,
        ),
    )

    class Stream:
        def __aiter__(self):
            self.sent = False
            return self

        async def __anext__(self):
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return chunk

    class Models:
        async def generate_content_stream(self, **_kwargs):
            return Stream()

    fake_client = SimpleNamespace(aio=SimpleNamespace(models=Models()))
    monkeypatch.setattr(gemini.google_genai, "Client", lambda **_kwargs: fake_client)
    monkeypatch.setattr(gemini, "record_provider_success_for_label", AsyncMock())

    events = [
        orjson.loads(value[6:].strip())
        async for value in gemini.call_gemini_api(
            messages=[{"role": "user", "content": "hi"}],
            model="gemini-3.5-flash",
            temperature=0.7,
            max_tokens=100,
            prompt="system",
            conversation_id=123,
            current_user=SimpleNamespace(id=1),
            request=None,
            user_api_key="test",
            tools=[{"name": "lookup"}],
        )
        if value.startswith("data: {")
    ]

    tool_call = next(event["tool_call"] for event in events if "tool_call" in event)
    assert tool_call["gemini_content"]["parts"][0]["thoughtSignature"] == (
        "c2lnbmVkLXRvb2wtY2FsbA=="
    )
    assert tool_call["id"] == "fc_signed"


@pytest.mark.asyncio
async def test_gemini_stale_commit_is_terminal_not_persistence_content(monkeypatch) -> None:
    chunk = SimpleNamespace(
        prompt_feedback=SimpleNamespace(block_reason=None),
        candidates=[],
        text="provisional answer",
        usage_metadata=SimpleNamespace(
            prompt_token_count=2,
            candidates_token_count=3,
            total_token_count=5,
        ),
    )

    class Models:
        async def generate_content_stream(self, **_kwargs):
            async def stream():
                yield chunk

            return stream()

    fake_client = SimpleNamespace(aio=SimpleNamespace(models=Models()))

    async def stale_save(*_args, **_kwargs):
        raise StaleChannelTurnError("phone acquired foreground")

    monkeypatch.setattr(gemini.google_genai, "Client", lambda **_kwargs: fake_client)
    monkeypatch.setattr(gemini, "save_content_to_db", stale_save)
    monkeypatch.setattr(gemini, "record_provider_success_for_label", AsyncMock())

    events = []
    with pytest.raises(StaleChannelTurnError):
        async for event in gemini.call_gemini_api(
            messages=[{"role": "user", "content": "hi"}],
            model="gemini-3.5-flash",
            temperature=0.7,
            max_tokens=100,
            prompt="system",
            conversation_id=123,
            current_user=SimpleNamespace(id=1),
            request=None,
            user_message="hi",
            user_api_key="test",
        ):
            events.append(event)

    assert any("provisional answer" in event for event in events)
    assert not any("persistence_error" in event for event in events)


def test_responses_tool_continuation_reuses_native_output_before_result() -> None:
    native_output = [
        {"type": "reasoning", "encrypted_content": "opaque"},
        {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
    ]
    messages = []
    _build_tool_response_messages(
        messages,
        {"name": "lookup", "arguments": {}, "id": "call_1", "response_output_items": native_output},
        "result",
        "GPT",
    )

    assert messages[:2] == native_output
    assert messages[2] == {"type": "function_call_output", "call_id": "call_1", "output": "result"}


@pytest.mark.asyncio
async def test_failed_claude_tool_followup_keeps_signed_turn_and_tool_definition(monkeypatch) -> None:
    captured = {}

    async def failing_handler(*_args, **_kwargs):
        yield 'data: {"content":"failed","is_error":true}\n\n'

    async def fake_claude(**kwargs):
        captured.update(kwargs)
        yield 'data: {"content":"recovered"}\n\n'

    monkeypatch.setitem(execution.function_handlers, "signed_lookup", failing_handler)
    monkeypatch.setattr(execution, "call_claude_api", fake_claude)
    monkeypatch.setattr(execution, "revalidate_user_billing", AsyncMock(return_value=True))

    signed_blocks = [
        {"type": "thinking", "thinking": "plan", "signature": "opaque"},
        {"type": "tool_use", "id": "toolu_1", "name": "signed_lookup", "input": {}},
    ]
    provider_tools = [{"name": "signed_lookup", "input_schema": {"type": "object"}}]
    messages = []
    chunks = [
        chunk async for chunk in execution.handle_function_call(
            "signed_lookup", {}, messages, "claude-opus-4-6", 0.7, 100, "",
            1, SimpleNamespace(id=1), None, 1, 1, 2, None, 1, "Claude", "system",
            user_message="hello",
            reasoning_selection={"mode": "high"},
            tool_call={
                "name": "signed_lookup", "arguments": {}, "id": "toolu_1",
                "claude_content_blocks": signed_blocks,
            },
            provider_tools=provider_tools,
        )
    ]

    assert chunks == ['data: {"content":"recovered"}\n\n']
    assert messages[0]["content"] == signed_blocks
    assert captured["tools"] == provider_tools
    assert captured["tool_choice"] == {"type": "none"}


@pytest.mark.asyncio
async def test_xai_responses_request_contains_neutral_effort(monkeypatch) -> None:
    # The existing xAI response stream test owns full transport coverage.  This
    # asserts the outgoing request gained only the contract-derived field.
    from tests.test_xai_responses import _FakeSession, _User
    import ai_runtime.providers.xai as xai

    payload = "\n\n".join([
        'event: response.completed\ndata: {"response":{"usage":{}}}',
        "data: [DONE]",
        "",
    ])
    captured = {}
    monkeypatch.setattr(xai.aiohttp, "ClientSession", lambda *args, **kwargs: _FakeSession(payload, captured))

    chunks = [
        chunk async for chunk in xai.call_xai_responses_api(
            messages=[{"role": "user", "content": "hi"}],
            model="grok-4.3",
            temperature=0.7,
            max_tokens=100,
            prompt="system",
            conversation_id=123,
            current_user=_User(),
            request=None,
            user_api_key="test",
            save_to_db=False,
            reasoning_selection={"mode": "off"},
        )
    ]

    assert captured["json"]["reasoning"] == {"effort": "none"}
    assert chunks
