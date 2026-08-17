import orjson

from log_config import logger
from prompts import can_user_access_prompt


async def create_conversation_core(
    user_id: int,
    cursor,
    current_user,
    prompt_id: int | None = None,
    folder_id: int | None = None,
    strict_prompt_access: bool = False,
    llm_id: int | None = None,
) -> int:
    """Create a conversation using user defaults and return its ID."""
    await cursor.execute(
        "SELECT llm_id, current_prompt_id FROM USER_DETAILS WHERE user_id = ?",
        (user_id,),
    )
    user_details = await cursor.fetchone()
    if not user_details:
        raise ValueError("User details not found")

    effective_llm_id = llm_id if llm_id is not None else user_details[0]
    effective_prompt_id = prompt_id if prompt_id is not None else user_details[1]

    if effective_prompt_id:
        if current_user:
            if not await can_user_access_prompt(current_user, effective_prompt_id, cursor):
                if strict_prompt_access:
                    raise PermissionError("Access denied to this prompt")
                effective_prompt_id = None
        else:
            await cursor.execute("SELECT id FROM PROMPTS WHERE id = ?", (effective_prompt_id,))
            if not await cursor.fetchone():
                effective_prompt_id = None

    default_extension_id = None
    if effective_prompt_id:
        await cursor.execute(
            """
            SELECT forced_llm_id, allowed_llms, extensions_enabled
            FROM PROMPTS
            WHERE id = ?
            """,
            (effective_prompt_id,),
        )
        prompt_row = await cursor.fetchone()
        if prompt_row:
            if prompt_row[0]:
                effective_llm_id = prompt_row[0]
                logger.info(
                    "[FORCED_LLM] Prompt %s has forced_llm_id=%s, overriding user default",
                    effective_prompt_id,
                    effective_llm_id,
                )
            elif prompt_row[1]:
                allowed_ids = orjson.loads(prompt_row[1])
                if allowed_ids and int(effective_llm_id) not in allowed_ids:
                    effective_llm_id = allowed_ids[0]
                    logger.info(
                        "[ALLOWED_LLMS] Selected LLM not in allowed list for prompt %s, using first allowed: %s",
                        effective_prompt_id,
                        effective_llm_id,
                    )

            if prompt_row[2]:
                await cursor.execute(
                    """
                    SELECT id
                    FROM PROMPT_EXTENSIONS
                    WHERE prompt_id = ? AND is_default = 1
                    LIMIT 1
                    """,
                    (effective_prompt_id,),
                )
                ext = await cursor.fetchone()
                if not ext:
                    await cursor.execute(
                        """
                        SELECT id
                        FROM PROMPT_EXTENSIONS
                        WHERE prompt_id = ?
                        ORDER BY display_order
                        LIMIT 1
                        """,
                        (effective_prompt_id,),
                    )
                    ext = await cursor.fetchone()
                if ext:
                    default_extension_id = ext[0]

    await cursor.execute(
        "SELECT COALESCE(enabled, 1), machine, model FROM LLM WHERE id = ?",
        (effective_llm_id,),
    )
    llm_row = await cursor.fetchone()
    if not llm_row:
        raise ValueError("LLM model not found")
    if not bool(llm_row[0]) and (
        llm_row[1] == "GPTSub"
        or int(effective_llm_id) != int(user_details[0] or 0)
    ):
        raise ValueError("This LLM model is disabled")
    # GPTSub (ChatGPT subscription) models need an active per-user link. This is a
    # UX write-gate; the authoritative fail-closed gate runs at inference time.
    # Never preserve an unlinked GPTSub default: no-op/default inheritance is still
    # a new conversation write and must validate this account's exact model catalog.
    if llm_row[1] == "GPTSub":
        try:
            from subscription_auth import gptsub_allowed
        except ImportError:
            gptsub_allowed = None
        if gptsub_allowed is None or not await gptsub_allowed(
            current_user, model=llm_row[2]
        ):
            raise ValueError("This model requires connecting your ChatGPT subscription")

    await cursor.execute(
        """
        INSERT INTO CONVERSATIONS (user_id, llm_id, role_id, folder_id, active_extension_id, last_activity)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        RETURNING id
        """,
        (user_id, effective_llm_id, effective_prompt_id, folder_id, default_extension_id),
    )
    row = await cursor.fetchone()
    return row[0]
