from __future__ import annotations

from typing import Any

from ai_runtime.atagia.recording import (
    _aurvek_atagia_message_id,
    _link_atagia_message_best_effort,
    _record_atagia_assistant_response,
)
from ai_runtime.atagia.state import _current_atagia_user_message_id
from ai_runtime.memory.context import _message_text_for_memory, _admitted_application_memory
from log_config import logger
from memory.config import get_active_memory_provider, get_user_memory_preferences
from memory.providers.mem0 import get_mem0_provider
from memory.sync import record_memory_conversation_link, record_memory_message_link
from integrations.embed.runtime import is_embed_conversation
from integrations.applications.memory import resolve_application_memory, record_memory_destination


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
        application_scope = await resolve_application_memory(conversation_id, user_id)
        admitted = _admitted_application_memory.get()
        admitted_matches = admitted is not None and admitted[:2] == (user_id, conversation_id)
        if admitted_matches:
            previous_write = admitted[2].write
            if (application_scope is None or previous_write is None or application_scope.write is None
                    or previous_write.key != application_scope.write.key):
                return False
        if application_scope is not None and (application_scope.write is None or incognito):
            return False
        provider = await get_active_memory_provider()
        if admitted_matches and admitted[3] != provider:
            return False
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
                if application_scope is not None:
                    preferences = await get_user_memory_preferences(user_id, provider)
                    if preferences.get("remember_across_chats") is False:
                        return False
                    return await _record_application_memory_turn(
                        provider=provider, namespace=application_scope.write,
                        user_id=user_id, conversation_id=conversation_id, prompt_id=prompt_id,
                        assistant_content=assistant_content, user_content=user_content,
                        assistant_message_id=assistant_message_id, user_message_id=user_message_id,
                        occurred_at=occurred_at, lease=provider_lease)
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
    namespace: Any | None = None,
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
                    **({"namespace": namespace} if namespace is not None else {}),
                )
                if prompt_id is None or namespace is not None:
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
                **({"namespace": namespace} if namespace is not None else {}),
            )
        except Exception:
            logger.warning("Failed to purge Mem0 conversation data", exc_info=True)
            return False
    return False


async def _record_application_memory_turn(
    *, provider, namespace, user_id, conversation_id, prompt_id, assistant_content,
    user_content, assistant_message_id, user_message_id, occurred_at, lease,
) -> bool:
    """Persist both roles to one destination; retrieval never ingests app turns."""
    turns = [("user", user_message_id, user_content),
             ("assistant", assistant_message_id, assistant_content)]
    turns = [(role, message_id, _message_text_for_memory(content).strip())
             for role, message_id, content in turns if content is not None and message_id is not None]
    turns = [turn for turn in turns if turn[2]]
    if not turns or provider not in {"atagia", "mem0"}:
        return False
    for _, message_id, _ in turns:
        await record_memory_destination(provider=provider, conversation_id=conversation_id,
            user_id=user_id, namespace=namespace, message_id=message_id)
    await record_memory_conversation_link(provider=provider, conversation_id=conversation_id,
        user_id=user_id, metadata={"namespace": namespace.as_dict(), "prompt_id": str(prompt_id)})
    if provider == "atagia":
        from atagia_bridge import get_atagia_bridge
        from ai_runtime.atagia.state import ATAGIA_LIVE_INGEST_ORIGIN, ATAGIA_LIVE_CONFIRMATION_STRATEGY

        bridge = get_atagia_bridge()
        for role, message_id, text in turns:
            if await _application_message_is_recorded(provider, message_id):
                continue
            await lease.mark_provider_started()
            if not await _application_write_still_allowed(provider, namespace, user_id, conversation_id):
                return False
            recorded = await bridge.ingest_message(user_id=user_id, conversation_id=conversation_id,
                role=role, text=text, occurred_at=occurred_at, prompt_id=prompt_id,
                message_id=message_id, source_seq=message_id, namespace=namespace,
                ingest_origin=ATAGIA_LIVE_INGEST_ORIGIN,
                confirmation_strategy=ATAGIA_LIVE_CONFIRMATION_STRATEGY, incognito=False)
            if not recorded:
                raise RuntimeError("Atagia application recording outcome is ambiguous")
            await record_memory_message_link(provider=provider, message_id=message_id,
                provider_message_id=namespace.provider_message_id(message_id),
                conversation_id=conversation_id, user_id=user_id, role=role,
                metadata={"namespace": namespace.as_dict()})
        return True
    mem0 = await get_mem0_provider()
    # Individual source messages retain their destination and correction lineage.
    for role, message_id, text in turns:
        if await _application_message_is_recorded(provider, message_id):
            continue
        await lease.mark_provider_started()
        if not await _application_write_still_allowed(provider, namespace, user_id, conversation_id):
            return False
        result = await mem0.add_messages(user_id=user_id, conversation_id=conversation_id,
            messages=[{"role": role, "content": text}], prompt_id=prompt_id,
            namespace=namespace, incognito=False,
            metadata={"source": "live", "message_id": str(message_id),
                      "occurred_at": occurred_at, "namespace": namespace.as_dict()})
        if not result:
            raise RuntimeError("Mem0 application recording outcome is ambiguous")
        await record_memory_message_link(provider=provider, message_id=message_id,
            provider_message_id=_mem0_provider_message_id(message_id, result),
            provider_event_id=_extract_provider_event_id(result), conversation_id=conversation_id,
            user_id=user_id, role=role, metadata=result)
    return True


async def _application_write_still_allowed(provider, namespace, user_id, conversation_id):
    """Each external write revalidates its original destination, never redirects."""
    if await get_active_memory_provider() != provider:
        return False
    preferences = await get_user_memory_preferences(user_id, provider)
    if preferences.get("remember_across_chats") is False:
        return False
    scope = await resolve_application_memory(conversation_id, user_id)
    return scope is not None and scope.write is not None and scope.write.key == namespace.key


async def _application_message_is_recorded(provider, message_id):
    from database import get_db_connection

    async with get_db_connection(readonly=True) as connection:
        cursor = await connection.execute("SELECT 1 FROM MEMORY_PROVIDER_MESSAGE_LINKS WHERE provider=? AND message_id=?",
                                          (provider, message_id))
        return await cursor.fetchone() is not None


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
