from __future__ import annotations

import pytest

from ai_runtime import messages as runtime_messages
import integrations.telephony.openai_realtime_billing as realtime_billing
from integrations.telephony.openai_realtime import (
    OpenAIRealtimeUsage,
    OpenAIResponseDoneEvent,
    parse_openai_realtime_message,
)


def _mixed_usage(**overrides) -> OpenAIRealtimeUsage:
    values = {
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
        "cached_input_tokens": 30,
        "text_input_tokens": 40,
        "audio_input_tokens": 60,
        "text_output_tokens": 15,
        "audio_output_tokens": 30,
        "reasoning_output_tokens": 5,
        "cached_text_input_tokens": 10,
        "cached_audio_input_tokens": 20,
    }
    values.update(overrides)
    return OpenAIRealtimeUsage(**values)


@pytest.mark.parametrize(
    ("model", "expected_cost"),
    [
        ("gpt-realtime-2.1", 0.003812),
        ("gpt-realtime-2.1-mini", 0.0010726),
    ],
)
def test_calculates_exact_mixed_text_audio_cached_cost(
    model: str,
    expected_cost: float,
) -> None:
    cost = realtime_billing.calculate_openai_realtime_api_cost(
        model,
        _mixed_usage(),
    )

    assert cost == pytest.approx(expected_cost)


def test_calculator_rejects_usage_without_exact_cached_modality_split() -> None:
    with pytest.raises(ValueError, match="cached token details"):
        realtime_billing.calculate_openai_realtime_api_cost(
            "gpt-realtime-2.1-mini",
            _mixed_usage(
                cached_text_input_tokens=0,
                cached_audio_input_tokens=0,
            ),
        )


def test_preflight_bounds_text_and_captured_pcmu_at_audio_rates() -> None:
    estimate = realtime_billing.calculate_openai_realtime_preflight(
        model="gpt-realtime-2.1",
        text_input_tokens=1_000,
        captured_pcmu_bytes=8_000,
    )

    # 8,000 PCMU bytes = 20 estimated tokens, plus a 10% whole-token margin.
    assert estimate.estimated_audio_input_tokens == 22
    assert estimate.markup_tokens == 1_022
    assert estimate.input_api_cost == pytest.approx(0.004704)
    assert estimate.output_api_cost_per_token == pytest.approx(0.000064)


def test_preflight_byok_keeps_markup_tokens_but_zeroes_provider_cost() -> None:
    estimate = realtime_billing.calculate_openai_realtime_preflight(
        model="gpt-realtime-2.1-mini",
        text_input_tokens=100,
        captured_pcmu_bytes=400,
        byok=True,
    )

    assert estimate.estimated_audio_input_tokens == 2
    assert estimate.markup_tokens == 102
    assert estimate.input_api_cost == 0
    assert estimate.output_api_cost_per_token == 0


@pytest.mark.asyncio
async def test_customer_preflight_applies_margin_and_markup_to_both_bounds(
    monkeypatch,
) -> None:
    calls = []

    async def charge(**kwargs):
        calls.append(kwargs)
        return kwargs["api_cost"] * 2 + kwargs["maximum_tokens"] * 0.001

    monkeypatch.setattr(
        runtime_messages,
        "estimate_customer_charge_from_api_cost",
        charge,
    )
    estimate, input_charge, output_rate = (
        await runtime_messages._openai_realtime_customer_preflight(
            model="gpt-realtime-2.1-mini",
            text_input_tokens=100,
            captured_pcmu_bytes=400,
            byok=False,
            user_id=7,
            prompt_id=9,
        )
    )

    assert estimate.input_api_cost == pytest.approx(0.00008)
    assert input_charge == pytest.approx(0.10216)
    assert output_rate == pytest.approx(0.00104)
    assert calls == [
        {
            "user_id": 7,
            "prompt_id": 9,
            "api_cost": pytest.approx(0.00008),
            "maximum_tokens": 102,
        },
        {
            "user_id": 7,
            "prompt_id": 9,
            "api_cost": pytest.approx(0.00002),
            "maximum_tokens": 1,
        },
    ]


def test_response_done_parser_preserves_cached_modality_details() -> None:
    event = parse_openai_realtime_message(
        {
            "type": "response.done",
            "response": {
                "id": "resp-1",
                "status": "completed",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                    "input_token_details": {
                        "cached_tokens": 30,
                        "text_tokens": 40,
                        "audio_tokens": 60,
                        "cached_tokens_details": {
                            "text_tokens": 10,
                            "audio_tokens": 20,
                        },
                    },
                    "output_token_details": {
                        "text_tokens": 15,
                        "audio_tokens": 30,
                        "reasoning_tokens": 5,
                    },
                },
            },
        }
    )

    assert isinstance(event, OpenAIResponseDoneEvent)
    assert event.usage is not None
    assert event.usage.cached_text_input_tokens == 10
    assert event.usage.cached_audio_input_tokens == 20


@pytest.mark.asyncio
async def test_accumulator_records_exact_idempotent_component(monkeypatch) -> None:
    captured = []

    async def fake_accumulate(**kwargs) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(
        realtime_billing,
        "accumulate_ai_reservation_usage",
        fake_accumulate,
    )

    result = await realtime_billing.accumulate_openai_realtime_response_usage(
        reservation_id="reservation-1",
        user_id=42,
        prompt_id=7,
        model="gpt-realtime-2.1-mini",
        response_id="resp-1",
        usage=_mixed_usage(),
    )

    assert result == pytest.approx((100, 50, 0.0010726))
    assert captured == [
        {
            "reservation_id": "reservation-1",
            "user_id": 42,
            "input_tokens": 100,
            "output_tokens": 50,
            "component": {
                "input_tokens": 100,
                "output_tokens": 50,
                "input_cost_per_million": 0.0,
                "output_cost_per_million": 0.0,
                "prompt_id": 7,
                "byok": False,
                "override_api_cost": pytest.approx(0.0010726),
                "idempotency_key": "openai-realtime:resp-1",
            },
        }
    ]


@pytest.mark.asyncio
async def test_byok_accumulator_forces_zero_override_without_pricing_usage(
    monkeypatch,
) -> None:
    captured = []

    async def fake_accumulate(**kwargs) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(
        realtime_billing,
        "accumulate_ai_reservation_usage",
        fake_accumulate,
    )

    result = await realtime_billing.accumulate_openai_realtime_response_usage(
        reservation_id="reservation-2",
        user_id=43,
        prompt_id=None,
        model="gpt-realtime-2.1",
        response_id="resp-byok",
        usage=_mixed_usage(
            cached_text_input_tokens=0,
            cached_audio_input_tokens=0,
        ),
        byok=True,
    )

    assert result == (100, 50, 0.0)
    assert captured[0]["component"]["byok"] is True
    assert captured[0]["component"]["override_api_cost"] == 0.0
    assert captured[0]["component"]["idempotency_key"] == (
        "openai-realtime:resp-byok"
    )


@pytest.mark.asyncio
async def test_uncertain_usage_marks_existing_ai_hold(monkeypatch) -> None:
    marked = []

    async def fake_mark(**kwargs):
        marked.append(kwargs)
        return True

    monkeypatch.setattr(
        realtime_billing,
        "mark_ai_reservation_provider_started",
        fake_mark,
    )

    assert await realtime_billing.mark_openai_realtime_usage_uncertain(
        reservation_id="reservation-uncertain",
        user_id=44,
    ) is True
    assert marked == [
        {"reservation_id": "reservation-uncertain", "user_id": 44}
    ]
