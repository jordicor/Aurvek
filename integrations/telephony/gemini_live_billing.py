"""Gemini 3.8 Live modality pricing for canonical phone reservations."""

from integrations.telephony.realtime_bridge import RealtimeUsage


# https://ai.google.dev/gemini-api/docs/pricing (September 16, 2026), USD/1M.
TEXT_INPUT_RATE = 0.75
AUDIO_INPUT_RATE = 3.0
VISUAL_INPUT_RATE = 1.0
TEXT_OUTPUT_RATE = 4.5
AUDIO_OUTPUT_RATE = 12.0
MODEL = "gemini-3.8-live"


def require_supported_model(model: str) -> None:
    if model.removeprefix("models/") != MODEL:
        raise ValueError("Unsupported Gemini Live phone model")


def parse_usage(metadata) -> RealtimeUsage:
    """Live totals include tool tokens; thinking is reported separately.

    Modality details omit internal/tool text tokens. Attribute the remainder
    of each authoritative total to text, including output thinking once.
    """
    def count(value):
        value = value or 0
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Invalid Gemini Live token count")
        return value

    def modality_counts(details, total, *, output=False):
        audio = visual = described = 0
        for detail in details or ():
            tokens = count(detail.token_count)
            if detail.modality not in ({"TEXT", "AUDIO"} if output else {"TEXT", "AUDIO", "IMAGE", "VIDEO"}):
                raise ValueError("Unsupported Gemini Live billing modality")
            described += tokens
            if detail.modality == "AUDIO":
                audio += tokens
            elif detail.modality in {"IMAGE", "VIDEO"}:
                visual += tokens
        if described > total:
            raise ValueError("Gemini Live modality counts exceed the total")
        return audio, visual

    inputs = count(metadata.prompt_token_count)
    responses = count(metadata.response_token_count)
    thoughts = count(metadata.thoughts_token_count)
    if count(getattr(metadata, "cached_content_token_count", 0)):
        raise ValueError("Gemini Live cached billing is unsupported")
    audio_in, visual_in = modality_counts(metadata.prompt_tokens_details, inputs)
    audio_out, _ = modality_counts(metadata.response_tokens_details, responses, output=True)
    return RealtimeUsage(
        input_tokens=inputs,
        output_tokens=responses + thoughts,
        total_tokens=inputs + responses + thoughts,
        text_input_tokens=inputs - audio_in - visual_in,
        visual_input_tokens=visual_in,
        audio_input_tokens=audio_in,
        text_output_tokens=responses - audio_out,
        audio_output_tokens=audio_out,
        reasoning_output_tokens=thoughts,
    )


def calculate_api_cost(usage: RealtimeUsage) -> float:
    return (
        usage.text_input_tokens * TEXT_INPUT_RATE
        + usage.audio_input_tokens * AUDIO_INPUT_RATE
        + usage.visual_input_tokens * VISUAL_INPUT_RATE
        + (usage.text_output_tokens + usage.reasoning_output_tokens) * TEXT_OUTPUT_RATE
        + usage.audio_output_tokens * AUDIO_OUTPUT_RATE
    ) / 1_000_000
