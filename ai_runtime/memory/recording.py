from __future__ import annotations

from typing import Any

from ai_runtime.atagia.recording import (
    _aurvek_atagia_message_id,
    _link_atagia_message_best_effort,
    _record_atagia_assistant_response,
)
from ai_runtime.atagia.state import _current_atagia_user_message_id
from ai_runtime.memory.context import _message_text_for_memory
from log_config import logger
from memory.config import get_active_memory_provider, get_user_memory_preferences
from memory.providers.mem0 import get_mem0_provider
from memory.sync import record_memory_conversation_link, record_memory_message_link


async def _record_memory_turn_best_effort(
    *,
    user_id: int,
    conversation_id: int,
    assistant_content: Any | None,
    user_content: Any | None = None,
    prompt_id: int | str | None = None,
    assistant_message_id: int | None = None,
    user_message_id: int | None = None,
    occurred_at: str | None = None,
    incognito: bool | None = None,
) -> bool:
    try:
        provider = await get_active_memory_provider()
        if provider == "none":
            return False
        if provider == "mem0" and incognito:
            return False
        from integrations.telephony.purge_state import (
            PhoneMemoryWriteBlocked,
            phone_memory_operation_lease,
        )

        try:
            async with phone_memory_operation_lease(
                conversation_id, provider=provider, operation="record_turn"
            ) as provider_lease:
                if provider == "atagia":
                    preferences = await get_user_memory_preferences(user_id, "atagia")
                    if preferences.get("remember_across_chats") is False:
                        return False
                    provider_prompt_id = (
                        None
                        if preferences.get("memory_scope") == "global"
                        else prompt_id
                    )
                    await record_memory_conversation_link(
                        provider="atagia",
                        conversation_id=conversation_id,
                        user_id=user_id,
                        metadata={
                            "prompt_id": (
                                str(provider_prompt_id)
                                if provider_prompt_id is not None
                                else None
                            )
                        },
                    )
                    if user_message_id is not None:
                        await _link_atagia_message_best_effort(
                            message_id=user_message_id,
                            atagia_message_id=_current_atagia_user_message_id.get(),
                            conversation_id=conversation_id,
                            user_id=user_id,
                            role="user",
                        )
                    if assistant_message_id is None:
                        return False
                    await provider_lease.mark_provider_started()
                    recorded = await _record_atagia_assistant_response(
                        user_id=user_id,
                        conversation_id=conversation_id,
                        content=assistant_content,
                        prompt_id=provider_prompt_id,
                        message_id=assistant_message_id,
                        source_seq=assistant_message_id,
                        incognito=incognito,
                    )
                    if not recorded:
                        raise RuntimeError("Atagia recording outcome is ambiguous")
                    if recorded:
                        await _link_atagia_message_best_effort(
                            message_id=assistant_message_id,
                            atagia_message_id=_aurvek_atagia_message_id(
                                assistant_message_id
                            ),
                            conversation_id=conversation_id,
                            user_id=user_id,
                            role="assistant",
                        )
                    return recorded

                if provider != "mem0":
                    return False
                preferences = await get_user_memory_preferences(user_id, "mem0")
                if preferences.get("remember_across_chats") is False:
                    return False
                await record_memory_conversation_link(
                    provider="mem0",
                    conversation_id=conversation_id,
                    user_id=user_id,
                    metadata={
                        "prompt_id": str(prompt_id) if prompt_id is not None else None
                    },
                )
                mem0 = await get_mem0_provider()
                await provider_lease.mark_provider_started()
                result = await mem0.add_turn(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    user_text=_message_text_for_memory(user_content).strip()
                    if user_content is not None
                    else None,
                    assistant_text=(
                        _message_text_for_memory(assistant_content).strip()
                        if assistant_content is not None
                        else None
                    ),
                    prompt_id=prompt_id,
                    message_id=assistant_message_id,
                    user_message_id=user_message_id,
                    occurred_at=occurred_at,
                    incognito=incognito,
                )
                if not result:
                    raise RuntimeError("Mem0 recording outcome is ambiguous")
                provider_event_id = _extract_provider_event_id(result)
                if user_message_id is not None:
                    await record_memory_message_link(
                        provider="mem0",
                        message_id=user_message_id,
                        provider_message_id=_mem0_provider_message_id(
                            user_message_id, result
                        ),
                        provider_event_id=provider_event_id,
                        conversation_id=conversation_id,
                        user_id=user_id,
                        role="user",
                        metadata=result,
                    )
                if assistant_message_id is not None:
                    await record_memory_message_link(
                        provider="mem0",
                        message_id=assistant_message_id,
                        provider_message_id=_mem0_provider_message_id(
                            assistant_message_id, result
                        ),
                        provider_event_id=provider_event_id,
                        conversation_id=conversation_id,
                        user_id=user_id,
                        role="assistant",
                        metadata=result,
                    )
                return True
        except PhoneMemoryWriteBlocked:
            return False
    except Exception:
        logger.warning(
            "[memory] Failed to record turn for conversation_id=%s",
            conversation_id,
            exc_info=True,
        )
        return False


async def _purge_memory_conversation_best_effort(
    *,
    user_id: int,
    conversation_id: int,
    prompt_id: int | None = None,
    incognito: bool = False,
    provider: str | None = None,
) -> bool:
    provider_name = provider or await get_active_memory_provider()
    if provider_name == "none":
        return False
    if provider_name == "atagia":
        try:
            from atagia_bridge import AtagiaBridge
            from atagia_config import bridge_config_from_mapping, get_atagia_config

            config = bridge_config_from_mapping(
                await get_atagia_config(), enabled_override=True
            )
            bridge = AtagiaBridge(config)
            try:
                prompt_purged = await bridge.purge_conversation(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    prompt_id=prompt_id,
                    incognito=incognito,
                )
                if prompt_id is None:
                    return prompt_purged
                global_purged = await bridge.purge_conversation(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    prompt_id=None,
                    incognito=incognito,
                )
                return prompt_purged and global_purged
            finally:
                await bridge.close()
        except Exception:
            logger.warning("Failed to purge Atagia conversation data", exc_info=True)
            return False
    if provider_name == "mem0":
        try:
            mem0 = await get_mem0_provider()
            return await mem0.purge_conversation(
                user_id=user_id,
                conversation_id=conversation_id,
                prompt_id=prompt_id,
                incognito=incognito,
            )
        except Exception:
            logger.warning("Failed to purge Mem0 conversation data", exc_info=True)
            return False
    return False


def _extract_provider_event_id(result: dict[str, Any]) -> str | None:
    for key in ("event_id", "id", "request_id"):
        value = result.get(key)
        if value:
            return str(value)
    results = result.get("results") or result.get("memories")
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            value = first.get("id") or first.get("memory_id")
            if value:
                return str(value)
    return None


def _mem0_provider_message_id(message_id: int | str, result: dict[str, Any]) -> str:
    event_id = _extract_provider_event_id(result)
    if event_id:
        return f"mem0:{event_id}:msg:{message_id}"
    return f"mem0:aurvek:msg:{message_id}"
