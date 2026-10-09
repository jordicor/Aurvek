"""Request capabilities shared by OpenAI's streaming and background callers."""

import re


def openai_supports_temperature(model: str) -> bool:
    """Opt in only known older models; new models omit sampling parameters."""
    name = model.removeprefix("openai/")
    if name.startswith("ft:"):
        name = name.split(":", 2)[1]
    name = re.sub(r"-(?:\d{4}-\d{2}-\d{2}|\d{4})(?:-preview)?$", "", name)
    return name in {
        "gpt-3.5-turbo", "gpt-3.5-turbo-16k",
        "gpt-4", "gpt-4-32k", "gpt-4-turbo", "gpt-4-turbo-preview",
        "gpt-4o", "gpt-4o-mini", "chatgpt-4o-latest",
        "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano",
    }
