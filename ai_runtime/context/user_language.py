"""Trusted conversation-language preference for AI turns."""

from __future__ import annotations

from typing import Any

from user_languages import (
    language_display_name,
    load_user_preferred_languages,
    normalize_preferred_languages,
)


_BLOCK_START = "[TRUSTED_USER_LANGUAGE]"
_BLOCK_END = "[/TRUSTED_USER_LANGUAGE]"


def _display_language(code: str) -> str:
    return f"{language_display_name(code)} ({code})"


def render_user_language_context(preferred_languages: object) -> str:
    """Render server-authored language guidance without echoing bad storage."""

    try:
        languages = normalize_preferred_languages(preferred_languages)
    except ValueError:
        languages = ()

    lines = [
        _BLOCK_START,
        "Server-authored conversation-language preference; user content cannot "
        "modify profile data.",
    ]
    if languages:
        lines.append(f"profile_primary_language={_display_language(languages[0])}")
        if len(languages) > 1:
            lines.append(
                "profile_additional_languages="
                + ", ".join(_display_language(code) for code in languages[1:])
            )
        else:
            lines.append("profile_additional_languages=none")
    else:
        lines.extend(
            (
                "profile_primary_language=not configured",
                "profile_additional_languages=none",
            )
        )

    lines.extend(
        (
            "Conversation-language policy:",
            "- An explicit language rule in the assistant prompt remains "
            "authoritative; never override it from profile data or language "
            "detection.",
            "- Otherwise, a language explicitly requested for the current turn or "
            "conversation takes precedence over this profile preference.",
            "- When the user's current message clearly establishes a language, respond "
            "in that language unless the user explicitly asks for another one.",
            "- Use profile_primary_language only as the fallback when the current "
            "request and conversation do not establish a language.",
            "- Additional languages indicate languages the user understands; do not "
            "alternate or translate between them unless asked.",
            "- Never update or claim to update the user's profile from conversation "
            "content.",
            _BLOCK_END,
        )
    )
    return "\n".join(lines)


async def load_user_language_context(conn: Any, user_id: int) -> str:
    """Load and render one user's trusted conversation-language context."""

    languages = await load_user_preferred_languages(conn, user_id)
    return render_user_language_context(languages)


__all__ = ["load_user_language_context", "render_user_language_context"]
