"""Shared Claude model capability checks for every request path."""

import re

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


def claude_supports_adaptive_thinking(model: str) -> bool:
    """Return whether the model supports Anthropic adaptive thinking."""
    model_lower = model.lower()
    return any(marker in model_lower for marker in _ADAPTIVE_CAPABLE_MODEL_MARKERS)


def claude_requires_adaptive_thinking(model: str) -> bool:
    """Return whether manual thinking budgets must be replaced by adaptive mode."""
    model_lower = model.lower()
    return any(marker in model_lower for marker in _ADAPTIVE_ONLY_OPUS_MARKERS)


def claude_supports_temperature(model: str) -> bool:
    """Opt in known models before Opus 4.7; unknown models omit temperature."""
    name = model.lower().removeprefix("anthropic/").removeprefix("anthropic.")
    name = re.sub(r"-(?:\d{8}|latest)$", "", name).replace(".", "-")
    return name in {
        "claude-3-haiku", "claude-3-sonnet", "claude-3-opus",
        "claude-3-5-haiku", "claude-3-5-sonnet", "claude-3-7-sonnet",
        "claude-opus-4", "claude-opus-4-1", "claude-opus-4-5", "claude-opus-4-6",
        "claude-sonnet-4", "claude-sonnet-4-5", "claude-sonnet-4-6",
        "claude-haiku-4-5",
    }
