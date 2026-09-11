from __future__ import annotations

from typing import Any

import orjson

from ai_runtime.dependencies import estimate_message_tokens, get_llm_info, logger
from memory.config import resolve_no_memory_context_max_tokens


def _text_for_token_estimate(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return orjson.dumps(value).decode("utf-8")
    except Exception:
        return str(value)


def estimate_context_message_tokens(message: dict[str, Any]) -> int:
    role_overhead = 4
    content = message.get("message") if isinstance(message, dict) else message
    return role_overhead + estimate_message_tokens(_text_for_token_estimate(content))


def trim_context_messages_by_token_budget(
    context_messages: list[dict[str, Any]],
    *,
    max_context_tokens: int,
) -> list[dict[str, Any]]:
    if max_context_tokens <= 0:
        return []

    selected: list[dict[str, Any]] = []
    used = 0
    for message in reversed(context_messages or []):
        message_tokens = estimate_context_message_tokens(message)
        if used + message_tokens > max_context_tokens:
            break
        selected.append(message)
        used += message_tokens

    selected.reverse()
    return selected


async def model_input_token_limit(llm_id: int | str | None) -> int:
    if llm_id is None:
        return 0
    try:
        info = await get_llm_info(int(llm_id))
    except (TypeError, ValueError):
        return 0
    if not info:
        return 0
    for key in ("max_input_tokens", "context_window_tokens"):
        try:
            value = int(info.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


async def apply_no_memory_context_budget(
    context_messages: list[dict[str, Any]],
    *,
    llm_id: int | str | None,
    prompt_id: int | str | None,
    full_prompt: str,
    current_message: Any,
    tools: list | None = None,
    output_tokens: int = 0,
) -> list[dict[str, Any]]:
    configured_budget, source = await resolve_no_memory_context_max_tokens(
        llm_id=llm_id,
        prompt_id=prompt_id,
    )
    prompt_tokens = estimate_message_tokens(full_prompt or "")
    current_tokens = estimate_message_tokens(_text_for_token_estimate(current_message))
    tool_tokens = estimate_message_tokens(_text_for_token_estimate(tools)) if tools else 0
    reserved_output = max(0, int(output_tokens))
    input_limit = await model_input_token_limit(llm_id)
    effective_budget = configured_budget
    if input_limit > 0:
        effective_budget = min(
            configured_budget,
            max(0, input_limit - prompt_tokens - current_tokens - tool_tokens - reserved_output),
        )

    trimmed = trim_context_messages_by_token_budget(
        context_messages,
        max_context_tokens=effective_budget,
    )
    if len(trimmed) != len(context_messages or []):
        logger.info(
            "[context_limit] provider=none source=%s llm_id=%s prompt_id=%s "
            "configured_budget=%s input_limit=%s effective_budget=%s "
            "messages_before=%s messages_after=%s",
            source,
            llm_id,
            prompt_id,
            configured_budget,
            input_limit,
            effective_budget,
            len(context_messages or []),
            len(trimmed),
        )
    return trimmed
