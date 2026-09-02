from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ai_runtime.atagia.context import (
    _context_messages_for_provider as _context_messages_for_atagia,
    _message_text_for_atagia,
    _resolve_atagia_context,
    _warmup_atagia_sidecar,
)
from log_config import logger
from memory.config import get_active_memory_provider, get_user_memory_preferences
from memory.providers.mem0 import append_mem0_context_to_prompt, get_mem0_provider
from memory.sync import record_memory_conversation_link


@dataclass(slots=True)
class MemoryContextDecision:
    full_prompt: str
    active: bool
    reason: str
    provider: str = "none"
    provider_decision: Any | None = None
    context: Any | None = None


_message_text_for_memory = _message_text_for_atagia


async def _resolve_memory_context(
    full_prompt: str,
    *,
    user_id: int,
    conversation_id: int,
    message: Any,
    occurred_at: str | None = None,
    prompt_id: int | str | None = None,
    message_id: int | str | None = None,
    incognito: bool | None = None,
) -> MemoryContextDecision:
    try:
        provider = await get_active_memory_provider()
        if provider == "none" or incognito:
            return MemoryContextDecision(
                full_prompt, False, "disabled", provider=provider
            )
        from integrations.telephony.purge_state import (
            PhoneMemoryWriteBlocked,
            is_conversation_memory_blocked,
            phone_memory_operation_lease,
        )

        if await is_conversation_memory_blocked(conversation_id):
            return MemoryContextDecision(
                full_prompt,
                False,
                "phone_data_rebuild",
                provider=provider,
            )

        if provider == "atagia":
            preferences = await get_user_memory_preferences(user_id, "atagia")
            if preferences.get("remember_across_chats") is False:
                return MemoryContextDecision(
                    full_prompt, False, "disabled_by_user", provider="atagia"
                )
            provider_prompt_id = (
                None if preferences.get("memory_scope") == "global" else prompt_id
            )
            try:
                async with phone_memory_operation_lease(
                    conversation_id,
                    provider="atagia",
                    operation="context_turn_ingest",
                ) as provider_lease:
                    await record_memory_conversation_link(
                        provider="atagia",
                        conversation_id=conversation_id,
                        user_id=user_id,
                        metadata={
                            "prompt_id": str(provider_prompt_id)
                            if provider_prompt_id is not None
                            else None
                        },
                    )
                    await provider_lease.mark_provider_started()
                    decision = await _resolve_atagia_context(
                        full_prompt,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        message=message,
                        occurred_at=occurred_at,
                        prompt_id=provider_prompt_id,
                        message_id=message_id,
                        incognito=incognito,
                    )
                    if decision.reason == "error" or (
                        decision.reason == "no_context"
                        and _atagia_last_error() is not None
                    ):
                        raise RuntimeError("Atagia context turn outcome is ambiguous")
            except PhoneMemoryWriteBlocked:
                return MemoryContextDecision(
                    full_prompt,
                    False,
                    "phone_data_rebuild",
                    provider="atagia",
                )
            return MemoryContextDecision(
                decision.full_prompt,
                decision.active,
                decision.reason,
                provider="atagia",
                provider_decision=decision,
                context=decision.context,
            )

        if provider == "mem0":
            preferences = await get_user_memory_preferences(user_id, "mem0")
            if preferences.get("remember_across_chats") is False:
                return MemoryContextDecision(
                    full_prompt, False, "disabled_by_user", provider="mem0"
                )

            message_text = _message_text_for_memory(message).strip()
            if not message_text:
                return MemoryContextDecision(
                    full_prompt, False, "empty_message", provider="mem0"
                )
            mem0 = await get_mem0_provider()
            search = await mem0.search_context(
                user_id=user_id,
                conversation_id=conversation_id,
                message_text=message_text,
                prompt_id=prompt_id,
            )
            if not search.active:
                return MemoryContextDecision(
                    full_prompt,
                    False,
                    search.reason,
                    provider="mem0",
                    context=search.raw,
                )
            return MemoryContextDecision(
                append_mem0_context_to_prompt(full_prompt, search.memories),
                True,
                search.reason,
                provider="mem0",
                context=search.raw,
            )

        return MemoryContextDecision(
            full_prompt, False, "unknown_provider", provider=provider
        )
    except Exception:
        logger.warning(
            "[memory] Failed to resolve memory context for conversation_id=%s",
            conversation_id,
            exc_info=True,
        )
        return MemoryContextDecision(full_prompt, False, "error", provider="unknown")


def _context_messages_for_memory_provider(
    context_messages: list[dict[str, Any]],
    decision: MemoryContextDecision,
) -> list[dict[str, Any]]:
    if decision.provider == "atagia" and decision.provider_decision is not None:
        return _context_messages_for_atagia(
            context_messages, decision.provider_decision
        )
    return context_messages


async def _warmup_memory_provider(
    user_id: int,
    conversation_id: int,
    *,
    prompt_id: int | str | None = None,
    incognito: bool | None = None,
) -> dict[str, Any]:
    try:
        provider = await get_active_memory_provider()
        from integrations.telephony.purge_state import (
            PhoneMemoryWriteBlocked,
            is_conversation_memory_blocked,
            phone_memory_operation_lease,
        )

        if await is_conversation_memory_blocked(conversation_id):
            return {
                "provider": provider,
                "ready": False,
                "atagia_ready": False,
                "reason": "phone_data_rebuild",
            }
        if provider == "atagia":
            preferences = await get_user_memory_preferences(user_id, "atagia")
            if preferences.get("remember_across_chats") is False:
                return {"provider": "atagia", "ready": False, "atagia_ready": False}
            provider_prompt_id = (
                None if preferences.get("memory_scope") == "global" else prompt_id
            )
            try:
                async with phone_memory_operation_lease(
                    conversation_id,
                    provider="atagia",
                    operation="warmup",
                ) as provider_lease:
                    await provider_lease.mark_provider_started()
                    ready = await _warmup_atagia_sidecar(
                        user_id,
                        conversation_id,
                        prompt_id=provider_prompt_id,
                        incognito=incognito,
                    )
                    if not ready and _atagia_last_error() is not None:
                        raise RuntimeError("Atagia warm-up outcome is ambiguous")
            except PhoneMemoryWriteBlocked:
                return {
                    "provider": "atagia",
                    "ready": False,
                    "atagia_ready": False,
                    "reason": "phone_data_rebuild",
                }
            return {"provider": "atagia", "ready": ready, "atagia_ready": ready}
        if provider == "mem0" and not incognito:
            return {"provider": "mem0", "ready": True, "atagia_ready": False}
        return {"provider": provider, "ready": False, "atagia_ready": False}
    except Exception:
        logger.warning(
            "[memory] Warm-up provider preparation failed for conversation_id=%s",
            conversation_id,
            exc_info=True,
        )
        return {"provider": "unknown", "ready": False, "atagia_ready": False}


def _atagia_last_error() -> Any | None:
    """Read the bridge's fail-open error without treating disabled as ambiguous."""

    from atagia_bridge import get_atagia_bridge

    return get_atagia_bridge().last_error
