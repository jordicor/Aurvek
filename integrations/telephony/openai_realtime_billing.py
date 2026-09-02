"""Exact token billing for OpenAI Realtime telephone responses.

Realtime responses mix text, audio and cached input tokens, each with a
different provider rate.  The legacy two-rate LLM catalog cannot represent
that shape, so this module calculates the provider cost reported by one
``response.done`` event and records it as an exact reservation component.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math

from billing.usage_reservations import (
    accumulate_ai_reservation_usage,
    mark_ai_reservation_provider_started,
)
from integrations.telephony.openai_realtime import (
    OpenAIRealtimeUsage,
)


_TOKENS_PER_MILLION = 1_000_000
_MAX_IDEMPOTENCY_KEY_LENGTH = 200
_PCMU_BYTES_PER_ESTIMATED_AUDIO_TOKEN = 400
_AUDIO_INPUT_TOKEN_MARGIN = 0.10


@dataclass(frozen=True, slots=True)
class _RealtimeTokenRates:
    text_input: float
    text_cached_input: float
    text_output: float
    audio_input: float
    audio_cached_input: float
    audio_output: float


@dataclass(frozen=True, slots=True)
class OpenAIRealtimePreflight:
    """Conservative raw provider-cost bounds for one captured phone turn."""

    input_api_cost: float
    markup_tokens: int
    output_api_cost_per_token: float
    estimated_audio_input_tokens: int


# Official OpenAI prices in USD per one million tokens.
_OPENAI_REALTIME_TOKEN_RATES = {
    "gpt-realtime-2.1": _RealtimeTokenRates(
        text_input=4.00,
        text_cached_input=0.40,
        text_output=24.00,
        audio_input=32.00,
        audio_cached_input=0.40,
        audio_output=64.00,
    ),
    "gpt-realtime-2.1-mini": _RealtimeTokenRates(
        text_input=0.60,
        text_cached_input=0.06,
        text_output=2.40,
        audio_input=10.00,
        audio_cached_input=0.30,
        audio_output=20.00,
    ),
}


def _token_count(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _normalized_model(model: str) -> str:
    normalized = str(model or "").strip().lower()
    if normalized not in _OPENAI_REALTIME_TOKEN_RATES:
        raise ValueError("unsupported OpenAI Realtime billing model")
    return normalized


def calculate_openai_realtime_api_cost(
    model: str,
    usage: OpenAIRealtimeUsage,
) -> float:
    """Return the exact provider cost for one Realtime response in USD.

    Cached tokens are a subset of each input modality.  All non-audio output
    is billed at the text-output rate; this includes reasoning tokens already
    present in ``output_tokens`` and therefore avoids charging them twice.
    """

    rates = _OPENAI_REALTIME_TOKEN_RATES[_normalized_model(model)]
    input_tokens = _token_count(usage.input_tokens, "input_tokens")
    output_tokens = _token_count(usage.output_tokens, "output_tokens")
    text_input = _token_count(usage.text_input_tokens, "text_input_tokens")
    audio_input = _token_count(usage.audio_input_tokens, "audio_input_tokens")
    cached_input = _token_count(
        usage.cached_input_tokens, "cached_input_tokens"
    )
    cached_text = _token_count(
        usage.cached_text_input_tokens, "cached_text_input_tokens"
    )
    cached_audio = _token_count(
        usage.cached_audio_input_tokens, "cached_audio_input_tokens"
    )
    audio_output = _token_count(
        usage.audio_output_tokens, "audio_output_tokens"
    )

    if text_input + audio_input != input_tokens:
        raise ValueError(
            "Realtime input token details do not match input_tokens"
        )
    if cached_text + cached_audio != cached_input:
        raise ValueError(
            "Realtime cached token details do not match cached_input_tokens"
        )
    if cached_text > text_input or cached_audio > audio_input:
        raise ValueError("Realtime cached tokens exceed their input modality")
    if audio_output > output_tokens:
        raise ValueError("Realtime audio output exceeds output_tokens")

    uncached_text = text_input - cached_text
    uncached_audio = audio_input - cached_audio
    non_audio_output = output_tokens - audio_output
    weighted_cost = math.fsum(
        (
            uncached_text * rates.text_input,
            cached_text * rates.text_cached_input,
            uncached_audio * rates.audio_input,
            cached_audio * rates.audio_cached_input,
            non_audio_output * rates.text_output,
            audio_output * rates.audio_output,
        )
    )
    return weighted_cost / _TOKENS_PER_MILLION


def calculate_openai_realtime_preflight(
    *,
    model: str,
    text_input_tokens: int,
    captured_pcmu_bytes: int,
    byok: bool = False,
) -> OpenAIRealtimePreflight:
    """Bound multimodal input cost before opening the provider response.

    Reserve input at a conservative 20 audio tokens per second.  At 8 kHz
    PCMU that is one token per 400 bytes.  A ten-percent whole-token margin
    absorbs endpointing/accounting variance without making the hold
    disproportionate for short calls.
    """

    normalized_model = _normalized_model(model)
    text_tokens = _token_count(text_input_tokens, "text_input_tokens")
    pcmu_bytes = _token_count(captured_pcmu_bytes, "captured_pcmu_bytes")
    if not isinstance(byok, bool):
        raise ValueError("byok must be boolean")

    raw_audio_tokens = math.ceil(
        pcmu_bytes / _PCMU_BYTES_PER_ESTIMATED_AUDIO_TOKEN
    )
    audio_margin = (
        max(1, math.ceil(raw_audio_tokens * _AUDIO_INPUT_TOKEN_MARGIN))
        if raw_audio_tokens
        else 0
    )
    audio_tokens = raw_audio_tokens + audio_margin
    rates = _OPENAI_REALTIME_TOKEN_RATES[normalized_model]
    if byok:
        input_api_cost = 0.0
        output_api_cost_per_token = 0.0
    else:
        input_api_cost = (
            text_tokens * rates.text_input
            + audio_tokens * rates.audio_input
        ) / _TOKENS_PER_MILLION
        output_api_cost_per_token = rates.audio_output / _TOKENS_PER_MILLION
    return OpenAIRealtimePreflight(
        input_api_cost=input_api_cost,
        markup_tokens=text_tokens + audio_tokens,
        output_api_cost_per_token=output_api_cost_per_token,
        estimated_audio_input_tokens=audio_tokens,
    )


def _response_idempotency_key(response_id: str) -> str:
    normalized = str(response_id or "").strip()
    if not normalized:
        raise ValueError("OpenAI Realtime response_id is required")
    key = f"openai-realtime:{normalized}"
    if len(key) <= _MAX_IDEMPOTENCY_KEY_LENGTH:
        return key
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"openai-realtime:sha256:{digest}"


async def accumulate_openai_realtime_response_usage(
    *,
    reservation_id: str | None,
    user_id: int,
    prompt_id: int | None,
    model: str,
    response_id: str,
    usage: OpenAIRealtimeUsage,
    byok: bool = False,
) -> tuple[int, int, float]:
    """Record one idempotent ``response.done`` usage component.

    The exact API cost is stored as an override because a single linear pair
    of LLM token rates cannot represent mixed Realtime modalities.
    """

    normalized_model = _normalized_model(model)
    input_tokens = _token_count(usage.input_tokens, "input_tokens")
    output_tokens = _token_count(usage.output_tokens, "output_tokens")
    api_cost = (
        0.0
        if byok
        else calculate_openai_realtime_api_cost(normalized_model, usage)
    )
    if not math.isfinite(api_cost) or api_cost < 0:
        raise ValueError("OpenAI Realtime API cost is invalid")

    await accumulate_ai_reservation_usage(
        reservation_id=reservation_id,
        user_id=int(user_id),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        component={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input_cost_per_million": 0.0,
            "output_cost_per_million": 0.0,
            "prompt_id": prompt_id,
            "byok": bool(byok),
            "override_api_cost": api_cost,
            "idempotency_key": _response_idempotency_key(response_id),
        },
    )
    return input_tokens, output_tokens, api_cost


async def mark_openai_realtime_usage_uncertain(
    *,
    reservation_id: str | None,
    user_id: int,
) -> bool:
    """Preserve a hold when cancellation produced no final usage event."""

    return await mark_ai_reservation_provider_started(
        reservation_id=reservation_id,
        user_id=int(user_id),
    )
