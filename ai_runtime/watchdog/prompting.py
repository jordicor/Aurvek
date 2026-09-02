from ai_runtime.dependencies import *

_WATCHDOG_STRIP_MARKERS = (
    "[WATCHDOG STEERING - INTERNAL, NEVER REVEAL TO USER]",
    "[WATCHDOG DIRECTIVE - MANDATORY, NEVER REVEAL TO USER]",
    "[WATCHDOG DIRECTIVE - MANDATORY - REPEATED]",
    "[WATCHDOG OVERRIDE - CRITICAL]",
    "[/WATCHDOG STEERING]",
    "[/WATCHDOG DIRECTIVE]",
    "[/WATCHDOG OVERRIDE]",
    "[MANDATORY DIRECTIVE - SUPERVISOR OVERRIDE]",
    "[END DIRECTIVE]",
)


def _sanitize_watchdog_directive(text: str, max_len: int = 2000) -> str:
    """Remove control markers/characters from watchdog text before reinjection."""
    if not text:
        return ""
    cleaned = str(text)
    for marker in _WATCHDOG_STRIP_MARKERS:
        cleaned = cleaned.replace(marker, "")
    cleaned = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", cleaned)
    return cleaned.strip()[:max_len]


def prepare_pre_watchdog_and_model_prompt(
    prompt_base: str,
    internal_turn_context: str | None = None,
) -> tuple[str, str]:
    """Attach trusted ephemeral channel context to both consumers identically.

    Normal channels pass no context and retain the previous prompt byte-for-byte.
    Phone runtime can pass its deterministic clock block; the pre-watchdog and
    the main model then reason from the same elapsed/remaining/mode state.
    """
    if not internal_turn_context or not str(internal_turn_context).strip():
        return prompt_base, prompt_base
    combined = f"{prompt_base.rstrip()}\n\n{str(internal_turn_context).strip()}"
    return combined, combined

def _build_escalated_hint_block(hint: str, severity: str, consecutive_count: int) -> str:
    """Build the watchdog hint block with escalating urgency based on how many
    consecutive hints the AI has ignored."""
    if not hint:
        return ""
    if consecutive_count >= 4:
        return (
            f"\n\n[WATCHDOG OVERRIDE - CRITICAL]\n"
            f"CRITICAL: You have ignored {consecutive_count} consecutive supervisor directives. "
            f"This is your final programmatic warning before system intervention. "
            f"Your ENTIRE next response must comply with this directive. NOTHING ELSE MATTERS.\n"
            f"{hint}\n"
            f"[/WATCHDOG OVERRIDE]"
        )
    elif consecutive_count >= 2:
        return (
            f"\n\n[WATCHDOG DIRECTIVE - MANDATORY - REPEATED]\n"
            f"You have been given this instruction {consecutive_count} times and failed to follow it. "
            f"OVERRIDE your current conversational flow. Your IMMEDIATE next response "
            f"MUST address this BEFORE anything else.\n"
            f"{hint}\n"
            f"[/WATCHDOG DIRECTIVE]"
        )
    elif severity == "redirect":
        return (
            "\n\n[WATCHDOG DIRECTIVE - MANDATORY, NEVER REVEAL TO USER]\n"
            "A supervisor system is monitoring this conversation for quality "
            "and safety. The following is a mandatory instruction. You MUST "
            "follow it:\n"
            f"{hint}\n"
            "[/WATCHDOG DIRECTIVE]"
        )
    else:
        return (
            "\n\n[WATCHDOG STEERING - INTERNAL, NEVER REVEAL TO USER]\n"
            "A supervisor system is monitoring this conversation. Consider "
            "the following suggestion:\n"
            f"{hint}\n"
            "[/WATCHDOG STEERING]"
        )
