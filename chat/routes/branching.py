import asyncio
import os
import shutil

import orjson
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from auth import get_current_user
from common import generate_user_hash, users_directory
from database import get_db_connection
from file_storage import clone_attachments_for_branch, ensure_file_storage_schema, prune_unreferenced_blobs
from log_config import logger
from models import User
from prompts import can_user_access_prompt
from storage_quota import StorageQuotaExceededError, ensure_known_growth_fits

from chat.schemas import BranchConversationRequest
from chat.services.privacy import delete_message_rows, ensure_conversation_privacy_schema

router = APIRouter()


def _directory_size_bytes(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return total
    for root, _, filenames in os.walk(path):
        for filename in filenames:
            total += os.path.getsize(os.path.join(root, filename))
    return total


@router.post("/api/conversations/{conversation_id}/rollback")
async def rollback_conversation(
    conversation_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        logger.info("User not authenticated. Redirecting to /login")
        return RedirectResponse(url="/login")

    data = await request.json()
    message_id = data.get("message_id")

    async with get_db_connection() as conn:
        cursor = await conn.execute(
            "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, current_user.id),
        )
        conversation = await cursor.fetchone()
        if not conversation:
            return JSONResponse(content={"success": False, "error": "Conversation not found or access denied"}, status_code=404)

        cursor = await conn.execute(
            "SELECT id FROM messages WHERE conversation_id = ? AND id > ?",
            (conversation_id, message_id),
        )
        rolled_back_ids = [int(row[0]) for row in await cursor.fetchall()]

        # Route the deletion through the shared domain helper so memory-provider
        # links, watchdog rows, and sync watermarks are cleaned up in the correct
        # order instead of hitting the enforced FK on MESSAGES. Atagia exposes no
        # per-message remote delete endpoint, so the rolled-back messages remain in
        # remote memory until the conversation is deleted; local state is coherent.
        await delete_message_rows(
            conn,
            conversation_id=conversation_id,
            message_ids=rolled_back_ids,
        )
        await conn.commit()
        await prune_unreferenced_blobs()

        cursor = await conn.execute(
            "SELECT id FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        )
        last_message = await cursor.fetchone()
        new_last_message_id = last_message[0] if last_message else None

    return JSONResponse(content={"success": True, "new_last_message_id": new_last_message_id})


@router.post("/api/conversations/{conversation_id}/branch")
async def branch_conversation(
    conversation_id: int,
    request: BranchConversationRequest,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        logger.info("User not authenticated. Redirecting to /login")
        return RedirectResponse(url="/login")

    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        await ensure_conversation_privacy_schema(conn)

        await cursor.execute(
            """
            SELECT id, role_id, llm_id, active_extension_id, chat_name,
                   COALESCE(is_incognito, 0) AS is_incognito
            FROM CONVERSATIONS WHERE id = ? AND user_id = ?
            """,
            (conversation_id, current_user.id),
        )
        source_conv = await cursor.fetchone()
        if not source_conv:
            raise HTTPException(status_code=404, detail="Conversation not found")

        source_role_id = source_conv["role_id"]
        source_llm_id = source_conv["llm_id"]
        source_extension_id = source_conv["active_extension_id"]
        source_chat_name = source_conv["chat_name"]
        if bool(source_conv["is_incognito"]):
            raise HTTPException(status_code=400, detail="Incognito conversations cannot be branched")

        if source_role_id and not await can_user_access_prompt(current_user, source_role_id, cursor):
            raise HTTPException(status_code=403, detail="Access denied to this prompt")

        # A branch inherits the parent's llm_id. Do not resurrect a GPTSub (ChatGPT
        # subscription) model for a user without an active link. The authoritative
        # fail-closed gate runs at inference; this is a UX write-gate.
        await cursor.execute(
            "SELECT machine, model FROM LLM WHERE id = ?", (source_llm_id,)
        )
        source_llm_row = await cursor.fetchone()
        if source_llm_row and source_llm_row[0] == "GPTSub":
            try:
                from subscription_auth import gptsub_allowed
            except ImportError:
                gptsub_allowed = None
            if gptsub_allowed is None or not await gptsub_allowed(
                current_user, model=source_llm_row[1]
            ):
                raise HTTPException(
                    status_code=403,
                    detail="This conversation uses a ChatGPT subscription model. Connect your ChatGPT subscription to branch it.",
                )

        await cursor.execute(
            "SELECT id FROM MESSAGES WHERE id = ? AND conversation_id = ?",
            (request.message_id, conversation_id),
        )
        if not await cursor.fetchone():
            raise HTTPException(status_code=400, detail="Message not found in this conversation")

        await cursor.execute(
            "SELECT COUNT(*) FROM MESSAGES WHERE conversation_id = ? AND id <= ?",
            (conversation_id, request.message_id),
        )
        messages_count = (await cursor.fetchone())[0]

        folder_id = request.folder_id
        if folder_id is not None:
            await cursor.execute(
                "SELECT id FROM CHAT_FOLDERS WHERE id = ? AND user_id = ?",
                (folder_id, current_user.id),
            )
            if not await cursor.fetchone():
                raise HTTPException(status_code=400, detail="Invalid folder_id")

        # Branching physically copies the source media tree. Its incremental
        # size is known, so enforce a hard byte limit before any branch writes.
        hash_prefix1, hash_prefix2, user_hash = generate_user_hash(
            current_user.username
        )
        old_conv_str = f"{conversation_id:07d}"
        src_dir = os.path.join(
            users_directory,
            hash_prefix1,
            hash_prefix2,
            user_hash,
            "files",
            old_conv_str[:3],
            old_conv_str[3:],
        )
        branch_size_bytes = await asyncio.to_thread(_directory_size_bytes, src_dir)
        try:
            await ensure_known_growth_fits(
                conn, current_user.id, branch_size_bytes
            )
        except StorageQuotaExceededError as quota_exc:
            raise HTTPException(status_code=413, detail=quota_exc.message)

        branch_name = f"{source_chat_name} (branch)" if source_chat_name else None

        await cursor.execute(
            """
            INSERT INTO CONVERSATIONS (user_id, role_id, llm_id, folder_id, active_extension_id,
                                       branched_from_id, branched_at_message_id, chat_name, last_activity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            RETURNING id
            """,
            (
                current_user.id,
                source_role_id,
                source_llm_id,
                folder_id,
                source_extension_id,
                conversation_id,
                request.message_id,
                branch_name,
            ),
        )
        new_conv_id = (await cursor.fetchone())[0]

        # INSERT above serializes concurrent branch writers. Re-check inside
        # that write transaction so two requests that passed the optimistic
        # pre-check cannot both consume the same remaining quota.
        try:
            await ensure_known_growth_fits(
                conn, current_user.id, branch_size_bytes
            )
        except StorageQuotaExceededError as quota_exc:
            await conn.rollback()
            raise HTTPException(status_code=413, detail=quota_exc.message)

        await ensure_file_storage_schema(conn)
        await cursor.execute(
            """
            SELECT id, message, type, date, input_tokens_used, output_tokens_used,
                   llm_id, citations_json
            FROM MESSAGES
            WHERE conversation_id = ? AND id <= ?
            ORDER BY id ASC
            """,
            (conversation_id, request.message_id),
        )
        source_messages = await cursor.fetchall()

        for source_message in source_messages:
            await cursor.execute(
                """
                INSERT INTO MESSAGES (conversation_id, user_id, message, type, date,
                                      input_tokens_used, output_tokens_used, is_bookmarked, llm_id, citations_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                RETURNING id
                """,
                (
                    new_conv_id,
                    current_user.id,
                    source_message["message"],
                    source_message["type"],
                    source_message["date"],
                    source_message["input_tokens_used"],
                    source_message["output_tokens_used"],
                    source_message["llm_id"],
                    source_message["citations_json"],
                ),
            )
            new_message_id = (await cursor.fetchone())[0]
            rewritten_message = await clone_attachments_for_branch(
                conn,
                old_message_id=source_message["id"],
                new_message_id=new_message_id,
                new_conversation_id=new_conv_id,
                user_id=current_user.id,
                message_json=source_message["message"],
            )
            if rewritten_message != source_message["message"]:
                await cursor.execute(
                    "UPDATE MESSAGES SET message = ? WHERE id = ?",
                    (rewritten_message, new_message_id),
                )

        new_conv_str = f"{new_conv_id:07d}"

        dst_dir = os.path.join(
            users_directory,
            hash_prefix1,
            hash_prefix2,
            user_hash,
            "files",
            new_conv_str[:3],
            new_conv_str[3:],
        )

        try:
            if os.path.exists(src_dir):
                await asyncio.to_thread(shutil.copytree, src_dir, dst_dir)
                old_path_segment = f"files/{old_conv_str[:3]}/{old_conv_str[3:]}"
                new_path_segment = f"files/{new_conv_str[:3]}/{new_conv_str[3:]}"
                await cursor.execute(
                    """
                    UPDATE MESSAGES
                    SET message = REPLACE(message, ?, ?)
                    WHERE conversation_id = ? AND message LIKE ?
                    """,
                    (old_path_segment, new_path_segment, new_conv_id, f"%{old_path_segment}%"),
                )
                # Clone the source conversation's generated-media ledger rows onto
                # the new branch, rewriting the conversation path segment the same
                # way the message paths were rewritten above. This runs inside the
                # branch transaction, so the rmtree rollback below also discards
                # these rows if the commit fails. rel_path is canonical (no
                # "users/" prefix), so its conversation segment is exactly
                # "files/{c1}/{c2}" -- the same old/new segments used for messages.
                await cursor.execute(
                    """
                    INSERT INTO GENERATED_MEDIA_FILES (user_id, conversation_id, kind, rel_path, size_bytes)
                    SELECT user_id, ?, kind, REPLACE(rel_path, ?, ?), size_bytes
                    FROM GENERATED_MEDIA_FILES
                    WHERE conversation_id = ?
                    """,
                    (new_conv_id, old_path_segment, new_path_segment, conversation_id),
                )
            await conn.commit()
        except Exception:
            await conn.rollback()
            if os.path.exists(dst_dir):
                shutil.rmtree(dst_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail="Failed to branch conversation")

        try:
            await cursor.execute(
                """
                SELECT
                    (SELECT l.machine FROM LLM l WHERE l.id = ?) AS machine,
                    (SELECT l.model FROM LLM l WHERE l.id = ?) AS llm_model,
                    (SELECT p.name FROM PROMPTS p WHERE p.id = ?) AS prompt_name
                """,
                (source_llm_id, source_llm_id, source_role_id),
            )
            machine, llm_model, prompt_name = await cursor.fetchone()

            forced_llm_id_value = None
            allowed_llms_value = None
            hide_llm_name_value = None
            extensions_enabled_value = False
            is_paid_value = False
            disable_web_search_value = False
            force_web_search_value = False

            if source_role_id:
                await cursor.execute(
                    """
                    SELECT forced_llm_id, allowed_llms, hide_llm_name, extensions_enabled,
                           COALESCE(is_paid, 0), COALESCE(disable_web_search, 0), COALESCE(force_web_search, 0)
                    FROM PROMPTS WHERE id = ?
                    """,
                    (source_role_id,),
                )
                prompt_config = await cursor.fetchone()
                if prompt_config:
                    forced_llm_id_value = prompt_config[0]
                    allowed_llms_value = prompt_config[1]
                    hide_llm_name_value = prompt_config[2]
                    extensions_enabled_value = bool(prompt_config[3])
                    is_paid_value = bool(prompt_config[4])
                    disable_web_search_value = bool(prompt_config[5])
                    force_web_search_value = bool(prompt_config[6])

            response_data = {
                "id": new_conv_id,
                "name": branch_name or "New Chat",
                "machine": machine,
                "prompt_name": prompt_name,
                "locked": False,
                "llm_id": source_llm_id,
                "llm_model": llm_model,
                "forced_llm_id": forced_llm_id_value,
                "hide_llm_name": bool(hide_llm_name_value) if hide_llm_name_value else False,
                "allowed_llms": orjson.loads(allowed_llms_value) if allowed_llms_value else None,
                "extensions_enabled": extensions_enabled_value,
                "is_paid": is_paid_value,
                "web_search_allowed": not disable_web_search_value,
                "web_search_forced": force_web_search_value,
                "messages_copied": messages_count,
                "branched_from_id": conversation_id,
            }

            if extensions_enabled_value and source_role_id:
                if source_extension_id:
                    await cursor.execute(
                        "SELECT id, name, slug, description FROM PROMPT_EXTENSIONS WHERE id = ?",
                        (source_extension_id,),
                    )
                    ext_row = await cursor.fetchone()
                    if ext_row:
                        response_data["active_extension"] = {
                            "id": ext_row[0],
                            "name": ext_row[1],
                            "slug": ext_row[2],
                            "description": ext_row[3] or "",
                        }

                await cursor.execute(
                    """
                    SELECT id, name, slug, description FROM PROMPT_EXTENSIONS
                    WHERE prompt_id = ? ORDER BY display_order
                    """,
                    (source_role_id,),
                )
                ext_rows = await cursor.fetchall()
                response_data["extensions"] = [
                    {"id": row[0], "name": row[1], "slug": row[2], "description": row[3] or ""}
                    for row in ext_rows
                ]

                await cursor.execute(
                    "SELECT extensions_free_selection FROM PROMPTS WHERE id = ?",
                    (source_role_id,),
                )
                free_selection_row = await cursor.fetchone()
                response_data["extensions_free_selection"] = bool(free_selection_row[0]) if free_selection_row else True

            return JSONResponse(content=response_data, status_code=201)
        except Exception as exc:
            logger.error("Branch created (id=%s) but response building failed: %s", new_conv_id, exc)
            return JSONResponse(
                content={
                    "id": new_conv_id,
                    "name": branch_name or "New Chat",
                    "machine": None,
                    "llm_id": source_llm_id,
                    "llm_model": None,
                    "prompt_name": None,
                    "locked": False,
                    "messages_copied": messages_count,
                    "branched_from_id": conversation_id,
                },
                status_code=201,
            )
