"""Shared Claude model capability checks for every request path."""


_ADAPTIVE_CAPABLE_MODEL_MARKERS = (
    "fable-5",
    "mythos-5",
    "mythos-preview",
    "opus-5",
    "sonnet-5",
    "opus-4-8",
    "opus-4.8",
    "opus-4-7",
    "opus-4.7",
    "opus-4-6",
    "opus-4.6",
    "sonnet-4-6",
    "sonnet-4.6",
)

_ADAPTIVE_ONLY_OPUS_MARKERS = (
    "fable-5",
    "mythos-5",
    "mythos-preview",
    "opus-5",
    "sonnet-5",
    "opus-4-8",
    "opus-4.8",
    "opus-4-7",
    "opus-4.7",
)

_NO_CUSTOM_TEMPERATURE_MODEL_MARKERS = (
    *_ADAPTIVE_CAPABLE_MODEL_MARKERS,
    "sonnet-5",
)


def claude_supports_adaptive_thinking(model: str) -> bool:
    """Return whether the model supports Anthropic adaptive thinking."""
    model_lower = model.lower()
    return any(marker in model_lower for marker in _ADAPTIVE_CAPABLE_MODEL_MARKERS)


def claude_requires_adaptive_thinking(model: str) -> bool:
    """Return whether manual thinking budgets must be replaced by adaptive mode."""
    model_lower = model.lower()
    return any(marker in model_lower for marker in _ADAPTIVE_ONLY_OPUS_MARKERS)


def claude_omits_temperature(model: str) -> bool:
    """Return whether custom temperature must be omitted from the request."""
    model_lower = model.lower()
    return any(
        marker in model_lower
        for marker in _NO_CUSTOM_TEMPERATURE_MODEL_MARKERS
    )
