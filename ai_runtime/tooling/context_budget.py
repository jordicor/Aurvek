"""Bound an application tool roundtrip while retaining provider-native items."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ai_runtime.context.history import model_input_token_limit
from ai_runtime.dependencies import estimate_message_tokens
from integrations.embed.models import EmbedError
from memory.config import resolve_no_memory_context_max_tokens


def _serializable(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {str(key): _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    return value


def _tokens(value):
    import orjson
    return estimate_message_tokens(orjson.dumps(_serializable(value), default=str).decode())


@dataclass
class ApplicationToolBudget:
    prefix: list
    tool_call: dict
    machine: str
    message_budget: int

    def _messages(self, result):
        from .execution import _build_tool_response_messages
        messages = list(self.prefix)
        _build_tool_response_messages(messages, self.tool_call, result, self.machine)
        return messages

    def maximum_input_tokens(self, full_prompt, provider_tools=None):
        """Price the known tool call plus the already bounded result text."""
        from billing.usage_reservations import estimate_structured_billing_tokens
        from .execution import TOOL_RESULT_MAX_BYTES

        return estimate_structured_billing_tokens(
            full_prompt, _serializable(self._messages("")),
            _serializable(provider_tools) if self.machine == "Claude" else None,
        ) + TOOL_RESULT_MAX_BYTES + 2048

    def result_messages(self, result: Any):
        from .execution import truncate_tool_result_for_ai
        text = truncate_tool_result_for_ai(result)
        candidate = self._messages(text)
        if _tokens(candidate) <= self.message_budget:
            return candidate
        # Cut only the untrusted tool result; signed reasoning, current input
        # and the tool-call/result pair retain their complete native structure.
        suffix = "\n[Tool result truncated by Aurvek to fit this model. Do not infer omitted data.]"
        low, high = 0, len(text)
        best = self._messages(suffix)
        if _tokens(best) > self.message_budget:
            raise EmbedError("tool_context_too_large", 413)
        while low <= high:
            middle = (low + high) // 2
            candidate = self._messages(text[:middle] + suffix)
            if _tokens(candidate) <= self.message_budget:
                best, low = candidate, middle + 1
            else:
                high = middle - 1
        return best


async def prepare_application_tool_budget(messages, tool_call, *, machine, full_prompt,
                                           llm_id, prompt_id=None, max_tokens=0, provider_tools=None):
    """Validate room before any effect; trim old history, never the current turn."""
    limit = await model_input_token_limit(llm_id)
    if limit <= 0:
        # Unknown model limits use the operator's conservative context setting.
        limit, _ = await resolve_no_memory_context_max_tokens(llm_id=llm_id, prompt_id=prompt_id)
    budget = limit - estimate_message_tokens(full_prompt or "") - max(0, int(max_tokens)) - 256
    if machine == "Claude" and provider_tools:
        budget -= _tokens(provider_tools)  # Required again for signed tool_use.
    prepared = ApplicationToolBudget(list(messages), tool_call, machine, budget)
    # Keep useful result room in addition to the complete opaque tool roundtrip.
    probe = "x " * 512
    while len(prepared.prefix) > 1 and _tokens(prepared._messages(probe)) > budget:
        prepared.prefix.pop(0)
    if _tokens(prepared._messages(probe)) > budget:
        raise EmbedError("tool_context_too_large", 413)
    return prepared
