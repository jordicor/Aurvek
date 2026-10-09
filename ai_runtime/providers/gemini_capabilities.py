"""Sampling parameters for Gemini's text request paths."""


def gemini_supports_temperature(model: str) -> bool:
    """Keep custom sampling on known 2.x models; newer models use defaults."""
    return model.removeprefix("models/").startswith(("gemini-2.0-", "gemini-2.5-"))
