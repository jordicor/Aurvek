import os
import shutil

import aiosqlite

from common import generate_user_hash, users_directory
from database import get_db_connection
from file_storage import prune_unreferenced_blobs
from log_config import logger
from models import User

from chat.services.locks import conversation_write_lock
from chat.services.privacy import (
    delete_conversation_rows,
    ensure_conversation_privacy_schema,
    purge_conversation_local_records,
)
from chat.services.stop_signals import stop_signals


async def memory_link_providers_for_conversation(conversation_id: int) -> set[str]:
    providers: set[str] = set()
    async with get_db_connection(readonly=True) as conn:
        # 'atagia' is a normal provider in the generic MEMORY_PROVIDER_* tables, so
        # it is discovered by the DISTINCT provider scans below (no legacy table).
        if await _table_exists(conn, "MEMORY_PROVIDER_MESSAGE_LINKS"):
            cursor = await conn.execute(
                """
                SELECT DISTINCT provider
                FROM MEMORY_PROVIDER_MESSAGE_LINKS
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            )
            providers.update(str(row[0]) for row in await cursor.fetchall() if row[0])
        if await _table_exists(conn, "MEMORY_PROVIDER_CONVERSATION_LINKS"):
            cursor = await conn.execute(
                """
                SELECT DISTINCT provider
                FROM MEMORY_PROVIDER_CONVERSATION_LINKS
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            )
            providers.update(str(row[0]) for row in await cursor.fetchall() if row[0])
    return providers


async def _table_exists(conn: aiosqlite.Connection, table_name: str) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    )
    return await cursor.fetchone() is not None


async def purge_memory_conversation_best_effort(
    *,
    user_id: int,
    conversation_id: int,
    prompt_id: int | None = None,
    incognito: bool = False,
    provider: str | None = None,
) -> bool:
    try:
        from ai_runtime.memory.recording import _purge_memory_conversation_best_effort

        return await _purge_memory_conversation_best_effort(
            user_id=user_id,
            conversation_id=conversation_id,
            prompt_id=prompt_id,
            incognito=incognito,
            provider=provider,
        )
    except Exception:
        logger.warning(
            "Failed to purge memory provider data for conversation_id=%s",
            conversation_id,
            exc_info=True,
        )
        return False


async def purge_linked_memory_providers_best_effort(
    *,
    user_id: int,
    conversation_id: int,
    prompt_id: int | None = None,
    incognito: bool = False,
) -> set[str]:
    providers = await memory_link_providers_for_conversation(conversation_id)
    if not providers:
        from memory.config import get_active_memory_provider

        active_provider = await get_active_memory_provider()
        if active_provider != "none":
            providers.add(active_provider)

    purged: set[str] = set()
    for provider in sorted(providers):
        if await purge_memory_conversation_best_effort(
            user_id=user_id,
            conversation_id=conversation_id,
            prompt_id=prompt_id,
            incognito=incognito,
            provider=provider,
        ):
            purged.add(provider)
    return purged


def _remove_conversation_media_dir(username: str, conversation_id: int) -> bool:
    """Remove a conversation's real media dir under its owner's tree:
    data/users/{h1}/{h2}/{user_hash}/files/{conv:07d[:3]}/{conv[3:]}/.
    Returns True when the dir is gone (removed or never existed)."""
    hash_prefix1, hash_prefix2, user_hash = generate_user_hash(username)
    conversation_id_str = f"{conversation_id:07d}"
    conversation_folder = os.path.join(
        users_directory,
        hash_prefix1,
        hash_prefix2,
        user_hash,
        "files",
        conversation_id_str[:3],
        conversation_id_str[3:],
    )
    if not os.path.exists(conversation_folder):
        return True
    try:
        shutil.rmtree(conversation_folder)
        return True
    except OSError as exc:
        logger.error("Error deleting conversation folder %s: %s", conversation_id, str(exc))
        return False


async def delete_conversation_files_for_user(
    current_user: User,
    conversation_id: int,
) -> bool:
    return _remove_conversation_media_dir(current_user.username, conversation_id)


async def purge_stale_incognito_conversations_for_user(current_user: User) -> None:
    try:
        await ensure_conversation_privacy_schema()
        async with get_db_connection(readonly=True) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """
                SELECT id, role_id
                FROM CONVERSATIONS
                WHERE user_id = ?
                  AND COALESCE(is_incognito, 0) = 1
                """,
                (current_user.id,),
            )
            rows = await cursor.fetchall()
    except Exception:
        logger.warning(
            "Failed to load stale incognito conversations for user_id=%s",
            current_user.id,
            exc_info=True,
        )
        return

    for row in rows:
        conversation_id = int(row["id"])
        purged_memory_providers = await purge_linked_memory_providers_best_effort(
            user_id=current_user.id,
            conversation_id=conversation_id,
            prompt_id=row["role_id"],
            incognito=True,
        )
        try:
            purged = await purge_conversation_local_records(
                conversation_id=conversation_id,
                user_id=current_user.id,
                memory_link_providers_to_delete=purged_memory_providers,
            )
            if purged:
                await delete_conversation_files_for_user(current_user, conversation_id)
                await prune_unreferenced_blobs()
        except Exception:
            logger.warning(
                "Failed to purge stale incognito conversation_id=%s",
                conversation_id,
                exc_info=True,
            )


async def delete_owned_conversation(current_user: User, conversation_id: int) -> dict:
    async with conversation_write_lock(conversation_id):
        async with get_db_connection() as conn:
            cursor = await conn.cursor()
            await ensure_conversation_privacy_schema(conn)

            await cursor.execute(
                """
                SELECT user_id, role_id, COALESCE(is_incognito, 0) AS is_incognito
                FROM conversations
                WHERE id = ?
                """,
                (conversation_id,),
            )
            result = await cursor.fetchone()
            if not result:
                return {"success": False, "error": "Conversation not found", "status_code": 404}

            user_id = result[0]
            if user_id != current_user.id:
                return {"success": False, "error": "Access denied", "status_code": 403}

            stop_signals[conversation_id] = True
            prompt_id = result[1]
            is_incognito = bool(result[2])
            purged_memory_providers = await purge_linked_memory_providers_best_effort(
                user_id=current_user.id,
                conversation_id=conversation_id,
                prompt_id=prompt_id,
                incognito=is_incognito,
            )

            await delete_conversation_rows(
                conn,
                conversation_id=conversation_id,
                user_id=current_user.id,
                memory_link_providers_to_delete=purged_memory_providers,
            )
            await conn.commit()

    await prune_unreferenced_blobs()
    await delete_conversation_files_for_user(current_user, conversation_id)
    return {
        "success": True,
        "atagia_purged": "atagia" in purged_memory_providers,
        "memory_purged": bool(purged_memory_providers),
        "memory_purged_providers": sorted(purged_memory_providers),
    }


async def close_incognito_conversation_for_user(current_user: User, privacy: dict) -> dict:
    conversation_id = int(privacy["id"])
    async with conversation_write_lock(conversation_id):
        purged_memory_providers = await purge_linked_memory_providers_best_effort(
            user_id=current_user.id,
            conversation_id=conversation_id,
            prompt_id=privacy.get("role_id"),
            incognito=True,
        )
        purged = await purge_conversation_local_records(
            conversation_id=conversation_id,
            user_id=current_user.id,
            memory_link_providers_to_delete=purged_memory_providers,
        )

    if purged:
        await delete_conversation_files_for_user(current_user, conversation_id)
        await prune_unreferenced_blobs()

    return {
        "success": True,
        "purged": bool(purged),
        "atagia_purged": "atagia" in purged_memory_providers,
        "memory_purged": bool(purged_memory_providers),
        "memory_purged_providers": sorted(purged_memory_providers),
    }


async def delete_conversation_recursively(conversation_id):
    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        await ensure_conversation_privacy_schema(conn)
        await cursor.execute(
            """
            SELECT user_id, role_id, COALESCE(is_incognito, 0) AS is_incognito
            FROM conversations
            WHERE id = ?
            """,
            (conversation_id,),
        )
        result = await cursor.fetchone()
        if result:
            user_id = result[0]
            role_id = result[1]
            is_incognito = bool(result[2])
            # Resolve the owner's username so the real per-conversation media dir
            # can be removed below. The admin caller only removes the legacy
            # static dir; without this the real dir (data/users/.../files/...)
            # would leak on every admin delete.
            await cursor.execute("SELECT username FROM users WHERE id = ?", (user_id,))
            owner_row = await cursor.fetchone()
            owner_username = owner_row[0] if owner_row else None
            purged_memory_providers = await purge_linked_memory_providers_best_effort(
                user_id=user_id,
                conversation_id=conversation_id,
                prompt_id=role_id,
                incognito=is_incognito,
            )
            await delete_conversation_rows(
                conn,
                conversation_id=conversation_id,
                memory_link_providers_to_delete=purged_memory_providers,
            )
            await conn.commit()
            await prune_unreferenced_blobs()
            if owner_username:
                _remove_conversation_media_dir(owner_username, conversation_id)
            return user_id
    return None


async def delete_conversation_folder(static_directory, user_id, conversation_id):
    conversation_folder = os.path.join(str(static_directory), "files", str(user_id), str(conversation_id))
    try:
        if os.path.exists(conversation_folder):
            shutil.rmtree(conversation_folder)
    except OSError as exc:
        logger.error("Error deleting conversation folder %s: %s", conversation_id, str(exc))
        return False
    return True
