from __future__ import annotations

from dataclasses import dataclass
from contextvars import ContextVar
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
from integrations.embed.runtime import append_interview_brief, is_embed_conversation
from integrations.applications.memory import resolve_application_memory, record_memory_destination

_admitted_application_memory = ContextVar("admitted_application_memory", default=None)


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
    # The persisted binding applies even when there is no browser principal.
    # Resolve before the provider fail-open block so a DB error cannot silently
    # omit the participant's brief or enable an unscoped provider read.
    full_prompt = await append_interview_brief(full_prompt, conversation_id, user_id)
    _admitted_application_memory.set(None)
    application_scope = await resolve_application_memory(conversation_id, user_id)
    if application_scope is not None:
        # A missing provider snapshot is deliberately non-writable if provider
        # resolution fails. Recording must not select a new provider later.
        _admitted_application_memory.set((user_id, conversation_id, application_scope, None))
    if application_scope is not None and not application_scope.reads and application_scope.write is None:
        return MemoryContextDecision(
            full_prompt, False, "application_memory_read_disabled", provider="none"
        )
    try:
        provider = await get_active_memory_provider()
        if application_scope is not None:
            _admitted_application_memory.set((user_id, conversation_id, application_scope, provider))
            if not application_scope.reads:
                return MemoryContextDecision(full_prompt, False, "application_memory_read_disabled", provider=provider)
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

        if application_scope is not None:
            preferences = await get_user_memory_preferences(user_id, provider)
            if preferences.get("remember_across_chats") is False:
                return MemoryContextDecision(full_prompt, False, "disabled_by_user", provider=provider)
            return await _resolve_application_provider_context(
                full_prompt, provider=provider, scope=application_scope,
                user_id=user_id, conversation_id=conversation_id,
                message_text=_message_text_for_memory(message).strip(), prompt_id=prompt_id)

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
    application_scope = await resolve_application_memory(conversation_id, user_id)
    try:
        if application_scope is not None and not (application_scope.reads or application_scope.write):
            return {
                "provider": "none", "ready": False, "atagia_ready": False,
                "reason": "application_memory_disabled",
            }
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
        if application_scope is not None:
            if incognito or provider == "none":
                return {"provider": provider, "ready": False, "atagia_ready": False}
            preferences = await get_user_memory_preferences(user_id, provider)
            if preferences.get("remember_across_chats") is False:
                return {"provider": provider, "ready": False, "atagia_ready": False}
            if provider == "mem0":
                return {"provider": provider, "ready": True, "atagia_ready": False}
            if provider == "atagia":
                from atagia_bridge import get_atagia_bridge
                spaces = {n.key: n for n in application_scope.reads}
                if application_scope.write is not None:
                    spaces[application_scope.write.key] = application_scope.write
                ready = True
                async with phone_memory_operation_lease(conversation_id, provider=provider,
                                                        operation="warmup") as provider_lease:
                    for namespace in spaces.values():
                        await record_memory_destination(provider=provider, conversation_id=conversation_id,
                            user_id=user_id, namespace=namespace)
                        await provider_lease.mark_provider_started()
                        result = await get_atagia_bridge().ensure_user_and_conversation(
                            user_id, conversation_id, prompt_id=prompt_id, namespace=namespace)
                        ready = ready and result is not None
                return {"provider": provider, "ready": ready, "atagia_ready": ready}
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


async def _resolve_application_provider_context(
    full_prompt, *, provider, scope, user_id, conversation_id, message_text, prompt_id,
) -> MemoryContextDecision:
    """Read only permitted spaces. Keep local history for this partial retrieval."""
    if not message_text:
        return MemoryContextDecision(full_prompt, False, "empty_message", provider=provider)
    from integrations.telephony.purge_state import phone_memory_operation_lease
    from ai_runtime.atagia.context import _extract_atagia_system_prompt

    blocks, results = [], []
    failed = False
    async with phone_memory_operation_lease(conversation_id, provider=provider,
                                            operation="application_memory_read") as lease:
        for namespace in scope.reads:
            await record_memory_destination(provider=provider, conversation_id=conversation_id,
                                             user_id=user_id, namespace=namespace)
            await lease.mark_provider_started()
            if provider == "atagia":
                from atagia_bridge import get_atagia_bridge
                result = await get_atagia_bridge().get_application_context(
                    namespace=namespace, conversation_id=conversation_id, message_text=message_text)
                text = _extract_atagia_system_prompt(result)
                failed = failed or result is None
            elif provider == "mem0":
                mem0 = await get_mem0_provider()
                search = await mem0.search_context(user_id=user_id, conversation_id=conversation_id,
                    message_text=message_text, prompt_id=prompt_id, namespace=namespace)
                result = search.raw
                text = "\n".join(search.memories)
                failed = failed or search.reason == "error"
            else:
                return MemoryContextDecision(full_prompt, False, "unknown_provider", provider=provider)
            results.append(result)
            if text:
                blocks.append(f"[{namespace.space} memory]\n{text}")
    if not blocks:
        return MemoryContextDecision(full_prompt, False, "error" if failed else "no_context",
                                     provider=provider, context=results)
    prompt = append_mem0_context_to_prompt(full_prompt, blocks)
    return MemoryContextDecision(prompt, True, "partial" if failed else "active",
                                 provider=provider, context=results)
