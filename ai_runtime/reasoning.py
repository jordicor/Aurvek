"""Provider-neutral reasoning capability parsing and request validation.

This module deliberately has no provider classes.  Catalog synchronization owns
capability discovery and provider adapters own payload translation; both meet at
the small, canonical contract defined here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


REASONING_BEHAVIORS = frozenset({"unknown", "none", "fixed", "configurable"})
REASONING_MODES = frozenset(
    {"default", "off", "auto", "minimal", "low", "medium", "high", "xhigh", "max", "custom"}
)


class ReasoningValidationError(ValueError):
    """Raised when reasoning metadata or a requested selection is invalid."""


@dataclass(frozen=True)
class ReasoningBudgetLimits:
    """Inclusive bounds for a provider that supports an exact token budget."""

    minimum: int | None = None
    maximum: int | None = None
    step: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("minimum", self.minimum),
            ("maximum", self.maximum),
            ("step", self.step),
        ):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ReasoningValidationError(f"budget_tokens.{name} must be an integer")
        if self.minimum is not None and self.minimum < 0:
            raise ReasoningValidationError("budget_tokens.minimum must be non-negative")
        if self.maximum is not None and self.maximum < 0:
            raise ReasoningValidationError("budget_tokens.maximum must be non-negative")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ReasoningValidationError("budget_tokens.minimum cannot exceed maximum")
        if self.step is not None and self.step <= 0:
            raise ReasoningValidationError("budget_tokens.step must be positive")

    def to_dict(self) -> dict[str, int]:
        return {
            key: value
            for key, value in (
                ("min", self.minimum),
                ("max", self.maximum),
                ("step", self.step),
            )
            if value is not None
        }


@dataclass(frozen=True)
class ReasoningCapabilities:
    """A normalized ``capabilities_json.reasoning`` entry.

    ``modes`` deliberately excludes an implied ``default`` for non-configurable
    models.  ``default`` is nevertheless accepted for every behavior.
    """

    behavior: str = "unknown"
    modes: tuple[str, ...] = ()
    default_mode: str = "default"
    budget_tokens: ReasoningBudgetLimits | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        if self.behavior not in REASONING_BEHAVIORS:
            raise ReasoningValidationError(f"unsupported reasoning behavior: {self.behavior!r}")
        if self.default_mode not in REASONING_MODES:
            raise ReasoningValidationError(f"unsupported default reasoning mode: {self.default_mode!r}")
        if not isinstance(self.modes, tuple):
            object.__setattr__(self, "modes", tuple(self.modes))
        if len(set(self.modes)) != len(self.modes):
            raise ReasoningValidationError("reasoning modes must not contain duplicates")
        unsupported = set(self.modes) - REASONING_MODES
        if unsupported:
            raise ReasoningValidationError(f"unsupported reasoning mode(s): {sorted(unsupported)!r}")
        if self.behavior != "configurable" and self.modes:
            raise ReasoningValidationError("only configurable reasoning may declare modes")
        if self.behavior != "configurable" and self.budget_tokens is not None:
            raise ReasoningValidationError("only configurable reasoning may declare budget_tokens")
        if self.budget_tokens is not None and "custom" not in self.modes:
            raise ReasoningValidationError("budget_tokens requires custom reasoning mode")
        if "custom" in self.modes and self.budget_tokens is None:
            raise ReasoningValidationError("custom reasoning mode requires budget_tokens limits")
        if self.default_mode != "default" and self.default_mode not in self.modes:
            raise ReasoningValidationError("default_mode must be default or an available mode")
        if self.source is not None and not isinstance(self.source, str):
            raise ReasoningValidationError("reasoning source must be a string")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "behavior": self.behavior,
            "modes": list(self.modes),
            "default_mode": self.default_mode,
        }
        if self.budget_tokens is not None:
            result["budget_tokens"] = self.budget_tokens.to_dict()
        if self.source is not None:
            result["source"] = self.source
        return result


@dataclass(frozen=True)
class ReasoningSelection:
    """The provider-neutral selection submitted by a browser or worker."""

    mode: str = "default"
    budget_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.mode not in REASONING_MODES:
            raise ReasoningValidationError(f"unsupported reasoning mode: {self.mode!r}")
        if self.budget_tokens is not None and (
            isinstance(self.budget_tokens, bool) or not isinstance(self.budget_tokens, int)
        ):
            raise ReasoningValidationError("budget_tokens must be an integer")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"mode": self.mode}
        if self.budget_tokens is not None:
            result["budget_tokens"] = self.budget_tokens
        return result


def parse_reasoning_capabilities(
    value: ReasoningCapabilities | Mapping[str, Any] | None,
) -> ReasoningCapabilities:
    """Parse a reasoning entry, or a whole capabilities mapping, safely.

    Missing capability metadata always becomes ``unknown``.  Malformed supplied
    metadata raises instead of being relaxed into an unsupported request path.
    """

    if value is None:
        return ReasoningCapabilities()
    if isinstance(value, ReasoningCapabilities):
        return value
    if not isinstance(value, Mapping):
        raise ReasoningValidationError("reasoning capabilities must be an object")

    # Accept ``capabilities_json`` as well as its ``reasoning`` sub-object.
    if "reasoning" in value:
        value = value["reasoning"]
        if value is None:
            return ReasoningCapabilities()
        if not isinstance(value, Mapping):
            raise ReasoningValidationError("reasoning capabilities must be an object")

    behavior = value.get("behavior", "unknown")
    modes = value.get("modes", ())
    if isinstance(modes, str) or not isinstance(modes, (list, tuple)):
        raise ReasoningValidationError("reasoning modes must be a list")
    if not all(isinstance(mode, str) for mode in modes):
        raise ReasoningValidationError("reasoning modes must contain strings")

    raw_budget = value.get("budget_tokens")
    if raw_budget is None:
        budget_tokens = None
    else:
        if not isinstance(raw_budget, Mapping):
            raise ReasoningValidationError("budget_tokens must be an object")
        unexpected = set(raw_budget) - {"min", "max", "step"}
        if unexpected:
            raise ReasoningValidationError(f"unsupported budget_tokens field(s): {sorted(unexpected)!r}")
        budget_tokens = ReasoningBudgetLimits(
            minimum=raw_budget.get("min"),
            maximum=raw_budget.get("max"),
            step=raw_budget.get("step"),
        )

    return ReasoningCapabilities(
        behavior=behavior,
        modes=tuple(modes),
        default_mode=value.get("default_mode", "default"),
        budget_tokens=budget_tokens,
        source=value.get("source"),
    )


def normalize_reasoning_capabilities(
    value: ReasoningCapabilities | Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return the canonical JSON-safe representation of reasoning metadata."""

    return parse_reasoning_capabilities(value).to_dict()


def parse_reasoning_selection(
    value: ReasoningSelection | Mapping[str, Any] | str | None,
) -> ReasoningSelection:
    """Parse a canonical selection; omitted input means the safe default."""

    if value is None:
        return ReasoningSelection()
    if isinstance(value, ReasoningSelection):
        return value
    if isinstance(value, str):
        return ReasoningSelection(mode=value)
    if not isinstance(value, Mapping):
        raise ReasoningValidationError("reasoning selection must be an object")
    unexpected = set(value) - {"mode", "budget_tokens"}
    if unexpected:
        raise ReasoningValidationError(f"unsupported reasoning selection field(s): {sorted(unexpected)!r}")
    return ReasoningSelection(mode=value.get("mode", "default"), budget_tokens=value.get("budget_tokens"))


def selection_from_legacy_thinking_budget(value: int | None) -> ReasoningSelection:
    """Translate the temporary Claude-era field into the canonical contract."""

    if value is None:
        return ReasoningSelection()
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReasoningValidationError("legacy thinking_budget_tokens must be an integer")
    if value == -1:
        return ReasoningSelection(mode="auto")
    if value > 0:
        return ReasoningSelection(mode="custom", budget_tokens=value)
    raise ReasoningValidationError("legacy thinking_budget_tokens must be -1 or positive")


def resolve_and_validate(
    selection: ReasoningSelection | Mapping[str, Any] | str | None,
    capabilities: ReasoningCapabilities | Mapping[str, Any] | None,
    *,
    legacy_thinking_budget_tokens: int | None = None,
) -> ReasoningSelection:
    """Resolve one canonical selection and validate it against model capability.

    Canonical input takes precedence over the temporary legacy field.  The
    returned object is suitable for forwarding unchanged to a provider adapter.
    """

    resolved = (
        parse_reasoning_selection(selection)
        if selection is not None
        else selection_from_legacy_thinking_budget(legacy_thinking_budget_tokens)
    )
    capability = parse_reasoning_capabilities(capabilities)

    if resolved.mode == "default":
        if resolved.budget_tokens is not None:
            raise ReasoningValidationError("default reasoning mode cannot include budget_tokens")
        return resolved

    if capability.behavior != "configurable":
        raise ReasoningValidationError(
            f"reasoning mode {resolved.mode!r} is unavailable for {capability.behavior} reasoning"
        )
    if resolved.mode not in capability.modes:
        raise ReasoningValidationError(f"reasoning mode {resolved.mode!r} is unavailable for this model")

    if resolved.mode != "custom":
        if resolved.budget_tokens is not None:
            raise ReasoningValidationError("only custom reasoning mode can include budget_tokens")
        return resolved

    if resolved.budget_tokens is None:
        raise ReasoningValidationError("custom reasoning mode requires budget_tokens")
    limits = capability.budget_tokens
    # This is guaranteed by ReasoningCapabilities, retained as a defensive guard
    # for future callers that may construct values differently.
    if limits is None:
        raise ReasoningValidationError("custom reasoning mode is not supported by this model")
    if limits.minimum is not None and resolved.budget_tokens < limits.minimum:
        raise ReasoningValidationError(f"budget_tokens must be at least {limits.minimum}")
    if limits.maximum is not None and resolved.budget_tokens > limits.maximum:
        raise ReasoningValidationError(f"budget_tokens must be at most {limits.maximum}")
    if (
        limits.step is not None
        and (resolved.budget_tokens - (limits.minimum or 0)) % limits.step != 0
    ):
        raise ReasoningValidationError(f"budget_tokens must follow step {limits.step}")
    return resolved
