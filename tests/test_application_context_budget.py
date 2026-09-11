"""Long local history remains bounded when scoped memory fails or is partial."""
from unittest.mock import AsyncMock

import pytest

from ai_runtime.context import history
from ai_runtime.memory.context import MemoryContextDecision, _context_messages_for_memory_provider


@pytest.mark.asyncio
@pytest.mark.parametrize("active,provider", [(False, "unknown"), (False, "atagia"), (True, "atagia"), (True, "mem0")])
async def test_prompt_memory_input_tools_and_output_leave_only_recent_fitting_history(monkeypatch, active, provider):
    monkeypatch.setattr(history, "estimate_message_tokens", len)
    monkeypatch.setattr(history, "resolve_no_memory_context_max_tokens", AsyncMock(return_value=(10000, "test")))
    monkeypatch.setattr(history, "model_input_token_limit", AsyncMock(return_value=500))
    context = [{"type": "user" if i % 2 else "bot", "message": str(i) + "x" * 30} for i in range(100)]
    decision = MemoryContextDecision("prompt-and-permitted-memory" * 3, active, "partial" if active else "error", provider)
    retained = _context_messages_for_memory_provider(context, decision)
    tools = [{"name": "app_http_lookup", "description": "Read permitted progress", "schema": {"type": "object"}}]
    result = await history.apply_no_memory_context_budget(retained, llm_id=1, prompt_id=10,
        full_prompt=decision.full_prompt, current_message="current input and extracted file text" * 2,
        tools=tools, output_tokens=128)
    assert 0 < len(result) < len(context)
    assert result[-1] == context[-1] and context[0] not in result
    used = sum(history.estimate_context_message_tokens(item) for item in result)
    used += len(decision.full_prompt) + len("current input and extracted file text" * 2)
    used += len(history._text_for_token_estimate(tools)) + 128
    assert used <= 500


@pytest.mark.asyncio
async def test_oversized_prompt_and_current_material_never_add_old_history(monkeypatch):
    monkeypatch.setattr(history, "estimate_message_tokens", len)
    monkeypatch.setattr(history, "resolve_no_memory_context_max_tokens", AsyncMock(return_value=(1000, "test")))
    monkeypatch.setattr(history, "model_input_token_limit", AsyncMock(return_value=200))
    result = await history.apply_no_memory_context_budget([{"message": "old"}], llm_id=1, prompt_id=1,
        full_prompt="x" * 180, current_message="y" * 60, tools=[{"name": "a"}], output_tokens=50)
    assert result == []


@pytest.mark.asyncio
@pytest.mark.parametrize("machine", ["GPT", "Claude", "Gemini", "OpenRouter"])
async def test_tool_continuation_fits_without_losing_current_turn_or_provider_roundtrip(monkeypatch, machine):
    from ai_runtime.tooling import context_budget as budget
    monkeypatch.setattr(budget, "model_input_token_limit", AsyncMock(return_value=3200))
    prefix = [{"role": "user", "content": "Old turn " + "words " * 700} for _ in range(15)]
    prefix.append({"role": "user", "content": "Current question: report progress."})
    call = {"name": "app_http_lookup", "arguments": {"q": "progress"}, "id": "call_progress"}
    if machine == "GPT":
        call["response_output_items"] = [
            {"type": "reasoning", "id": "opaque", "encrypted_content": "SIGNED_OPAQUE_VALUE", "summary": []},
            {"type": "function_call", "call_id": "call_progress", "name": "app_http_lookup", "arguments": '{"q":"progress"}'}]
    prepared = await budget.prepare_application_tool_budget(prefix, call, machine=machine,
        full_prompt="The original assistant prompt.", llm_id=1, max_tokens=300)
    messages = prepared.result_messages("A synthetic record. " * 5000)
    assert budget._tokens(messages) <= prepared.message_budget
    assert "Current question" in str(messages) and "app_http_lookup" in str(messages)
    assert "truncated by Aurvek" in str(messages)
    assert len(prepared.prefix) < len(prefix)
    if machine == "GPT":
        assert any(item.get("encrypted_content") == "SIGNED_OPAQUE_VALUE" for item in messages)


@pytest.mark.asyncio
async def test_too_large_current_input_rejects_before_remote_effect_can_begin(monkeypatch):
    from ai_runtime.tooling import context_budget as budget
    from integrations.embed.models import EmbedError
    monkeypatch.setattr(budget, "model_input_token_limit", AsyncMock(return_value=1000))
    with pytest.raises(EmbedError, match="tool_context_too_large"):
        await budget.prepare_application_tool_budget([{"role": "user", "content": "x " * 6000}],
            {"name": "app_http_write", "arguments": {}, "id": "write"},
            machine="GPT", full_prompt="prompt", llm_id=1, max_tokens=100)


@pytest.mark.asyncio
async def test_media_error_continuation_uses_its_prepared_application_context(monkeypatch):
    from types import SimpleNamespace
    from ai_runtime.tooling import context_budget as budget, execution

    monkeypatch.setattr(budget, "model_input_token_limit", AsyncMock(return_value=2000))
    messages = [{"role": "user", "content": "old history " * 500} for _ in range(8)]
    messages.append({"role": "user", "content": "Draw a red square."})
    call = {"name": "synthetic_media_error", "arguments": {}, "id": "media_error"}
    prepared = await budget.prepare_application_tool_budget(messages, call,
        machine="Claude", full_prompt="Prompt", llm_id=1, max_tokens=300)
    expected = prepared.result_messages("Error: No image data returned from Gemini model")

    async def handler(*args):
        yield 'data: {"content":"No image data returned from Gemini model","is_error":true}\n\n'

    async def response(**kwargs):
        assert kwargs["messages"] == expected
        assert budget._tokens(kwargs["messages"]) <= prepared.message_budget
        assert kwargs["billing_reservation_id"] == "source-ai-hold"
        yield 'data: {"content":"No se pudo crear la imagen."}\n\n'

    monkeypatch.setitem(execution.function_handlers, call["name"], handler)
    monkeypatch.setattr(execution, "call_claude_api", response)
    monkeypatch.setattr(execution, "revalidate_user_billing", AsyncMock(return_value=True))
    extend = AsyncMock()
    monkeypatch.setattr(execution, "extend_ai_reservation", extend)
    chunks = [chunk async for chunk in execution.handle_function_call(
        call["name"], {}, messages, "test-model", 0.3, 300, "", 1,
        SimpleNamespace(id=1), None, 10, 4, 14, None, 1, "Claude", "Prompt",
        billing_reservation_id="source-ai-hold", billing_followup_hold_amount=0.04,
        application_tool_budget=prepared, tool_call=call)]
    assert chunks == ['data: {"content":"No se pudo crear la imagen."}\n\n']
    extend.assert_awaited_once()
