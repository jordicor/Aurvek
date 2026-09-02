from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from billing import usage_reservations
from integrations.messaging_voice_notes import service
from tools import llm_caller


@pytest.mark.asyncio
async def test_comparison_llm_reserves_and_settles_reported_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reserve = AsyncMock(return_value=("comparison-reservation", 321))
    settle = AsyncMock(return_value=True)
    refund = AsyncMock()
    provider_result = llm_caller.LLMCallResult(
        text='{"verdict":"better"}',
        input_tokens=1_234,
        output_tokens=56,
        total_tokens=1_290,
    )
    call_provider = AsyncMock(return_value=provider_result)
    monkeypatch.setattr(usage_reservations, "reserve_ai_provider_call", reserve)
    monkeypatch.setattr(
        usage_reservations,
        "settle_ai_reservation_components",
        settle,
    )
    monkeypatch.setattr(usage_reservations, "refund_fixed_usage", refund)
    monkeypatch.setattr(
        llm_caller,
        "call_llm_non_streaming_with_usage",
        call_provider,
    )

    result = await service._call_billed_comparison_llm(
        user_id=77,
        machine="GPT",
        model="gpt-comparison",
        system_prompt="Compare both transcripts.",
        payload='{"old":"one","new":"two"}',
        input_token_cost=2.5,
        output_token_cost=7.5,
        api_key_override="byok-key",
        byok=True,
    )

    assert result is provider_result
    reserve.assert_awaited_once_with(
        user_id=77,
        prompt_id=None,
        input_payload=(
            "Compare both transcripts.",
            '{"old":"one","new":"two"}',
        ),
        maximum_output_tokens=500,
        input_cost_per_million=2.5,
        output_cost_per_million=7.5,
        byok=True,
    )
    call_provider.assert_awaited_once_with(
        "GPT",
        "gpt-comparison",
        "Compare both transcripts.",
        '{"old":"one","new":"two"}',
        timeout=180,
        max_tokens=500,
        api_key_override="byok-key",
    )
    settle.assert_awaited_once_with(
        reservation_id="comparison-reservation",
        user_id=77,
        prompt_id=None,
        components=[
            {
                "input_tokens": 1_234,
                "output_tokens": 56,
                "input_cost_per_million": 2.5,
                "output_cost_per_million": 7.5,
                "byok": True,
            }
        ],
    )
    refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_comparison_llm_refunds_reservation_when_provider_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reserve = AsyncMock(return_value=("comparison-reservation", 123))
    settle = AsyncMock(return_value=True)
    refund = AsyncMock(return_value=True)
    provider_error = RuntimeError("provider unavailable")
    call_provider = AsyncMock(side_effect=provider_error)
    monkeypatch.setattr(usage_reservations, "reserve_ai_provider_call", reserve)
    monkeypatch.setattr(
        usage_reservations,
        "settle_ai_reservation_components",
        settle,
    )
    monkeypatch.setattr(usage_reservations, "refund_fixed_usage", refund)
    monkeypatch.setattr(
        llm_caller,
        "call_llm_non_streaming_with_usage",
        call_provider,
    )

    with pytest.raises(RuntimeError, match="provider unavailable") as exc_info:
        await service._call_billed_comparison_llm(
            user_id=77,
            machine="Claude",
            model="claude-comparison",
            system_prompt="Compare both transcripts.",
            payload='{"old":"one","new":"two"}',
            input_token_cost=3.0,
            output_token_cost=15.0,
            api_key_override=None,
            byok=False,
        )

    assert exc_info.value is provider_error
    reserve.assert_awaited_once()
    call_provider.assert_awaited_once()
    refund.assert_awaited_once_with("comparison-reservation")
    settle.assert_not_awaited()


@pytest.mark.asyncio
async def test_comparison_llm_estimates_missing_provider_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reserve = AsyncMock(return_value=("comparison-reservation", 321))
    settle = AsyncMock(return_value=True)
    provider_result = llm_caller.LLMCallResult(
        text='{"verdict":"equal","rationale":"Sin diferencias claras."}',
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
    )
    monkeypatch.setattr(usage_reservations, "reserve_ai_provider_call", reserve)
    monkeypatch.setattr(
        usage_reservations,
        "settle_ai_reservation_components",
        settle,
    )
    monkeypatch.setattr(
        usage_reservations,
        "refund_fixed_usage",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        llm_caller,
        "call_llm_non_streaming_with_usage",
        AsyncMock(return_value=provider_result),
    )

    await service._call_billed_comparison_llm(
        user_id=77,
        machine="GPT",
        model="gpt-comparison",
        system_prompt="Compare both transcripts.",
        payload='{"old":"one","new":"two"}',
        input_token_cost=2.5,
        output_token_cost=7.5,
        api_key_override=None,
        byok=False,
    )

    component = settle.await_args.kwargs["components"][0]
    assert component["input_tokens"] == usage_reservations.estimate_structured_usage_tokens(
        "Compare both transcripts.",
        '{"old":"one","new":"two"}',
    )
    assert 1 <= component["output_tokens"] <= 500
    assert component["input_cost_per_million"] == 2.5
    assert component["output_cost_per_million"] == 7.5


@pytest.mark.asyncio
async def test_chunk_failure_keeps_billed_partial_assessment_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_result = llm_caller.LLMCallResult(
        text=json.dumps(
            {
                "verdict": "better",
                "confidence": 0.8,
                "rationale": "La primera parte es más clara.",
            }
        ),
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
    )
    billed_call = AsyncMock(
        side_effect=[first_result, RuntimeError("second chunk unavailable")]
    )
    persist = AsyncMock()
    monkeypatch.setattr(
        service,
        "_resolve_comparison_api_key",
        AsyncMock(return_value=(None, False)),
    )
    monkeypatch.setattr(
        service,
        "_revision_has_status",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(service, "_call_billed_comparison_llm", billed_call)
    monkeypatch.setattr(service, "_persist_comparison_progress", persist)

    verdict, confidence, rationale, comparison_json = (
        await service._compare_transcripts(
            old_text="a" * 40_000,
            new_text="b" * 40_000,
            user_id=77,
            llm_id=8,
            machine="GPT",
            model="judge-snapshot",
            input_token_cost=2.5,
            output_token_cost=7.5,
            revision_id=99,
        )
    )

    payload = json.loads(comparison_json)
    assert verdict == "uncertain"
    assert confidence == 0.0
    assert "1 de 2 partes" in rationale
    assert payload["completed_parts"] == 1
    assert payload["total_parts"] == 2
    assert payload["assessments"][0]["verdict"] == "better"
    assert "second chunk unavailable" in payload["error"]
    assert billed_call.await_count == 2
    persist.assert_awaited_once()
