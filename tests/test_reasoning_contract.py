import pytest

from ai_runtime.reasoning import (
    ReasoningCapabilities,
    ReasoningSelection,
    ReasoningValidationError,
    normalize_reasoning_capabilities,
    parse_reasoning_capabilities,
    resolve_and_validate,
)


def test_missing_or_non_configurable_capabilities_accept_only_safe_default():
    for capabilities in (None, {"behavior": "none"}, {"behavior": "fixed"}):
        assert resolve_and_validate(None, capabilities) == ReasoningSelection()
        with pytest.raises(ReasoningValidationError):
            resolve_and_validate({"mode": "high"}, capabilities)


def test_configurable_effort_is_limited_to_published_modes():
    capabilities = {"behavior": "configurable", "modes": ["low", "high"]}

    assert resolve_and_validate({"mode": "high"}, capabilities) == ReasoningSelection("high")
    with pytest.raises(ReasoningValidationError, match="unavailable"):
        resolve_and_validate({"mode": "auto"}, capabilities)
    with pytest.raises(ReasoningValidationError, match="only custom"):
        resolve_and_validate({"mode": "low", "budget_tokens": 42}, capabilities)


def test_custom_budget_requires_limits_and_is_never_silently_clamped():
    capabilities = {
        "behavior": "configurable",
        "modes": ["custom"],
        "budget_tokens": {"min": 1024, "max": 4096, "step": 1024},
    }

    assert resolve_and_validate({"mode": "custom", "budget_tokens": 2048}, capabilities).budget_tokens == 2048
    for budget in (1000, 2500, 5000):
        with pytest.raises(ReasoningValidationError):
            resolve_and_validate({"mode": "custom", "budget_tokens": budget}, capabilities)


def test_capability_normalization_preserves_only_canonical_reasoning_fields():
    normalized = normalize_reasoning_capabilities(
        {
            "vision": {"enabled": True},
            "reasoning": {
                "behavior": "configurable",
                "modes": ["high", "custom"],
                "budget_tokens": {"min": 100, "max": 500, "step": 100},
                "source": "curated",
            },
        }
    )

    assert normalized == {
        "behavior": "configurable",
        "modes": ["high", "custom"],
        "default_mode": "default",
        "budget_tokens": {"min": 100, "max": 500, "step": 100},
        "source": "curated",
    }
    assert parse_reasoning_capabilities({"reasoning": None}) == ReasoningCapabilities()


def test_legacy_values_are_translated_then_validated_against_capability():
    auto_capability = {"behavior": "configurable", "modes": ["auto"]}
    custom_capability = {
        "behavior": "configurable",
        "modes": ["custom"],
        "budget_tokens": {"min": 1024, "max": 4096, "step": 1024},
    }

    assert resolve_and_validate(None, auto_capability, legacy_thinking_budget_tokens=-1).mode == "auto"
    assert resolve_and_validate(None, custom_capability, legacy_thinking_budget_tokens=2048).to_dict() == {
        "mode": "custom",
        "budget_tokens": 2048,
    }
    with pytest.raises(ReasoningValidationError):
        resolve_and_validate(None, auto_capability, legacy_thinking_budget_tokens=2048)
