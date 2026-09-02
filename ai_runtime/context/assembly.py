from ai_runtime.dependencies import *
from ai_runtime.memory.context import _context_messages_for_memory_provider, _resolve_memory_context
from ai_runtime.context.formatting import flatten_multi_ai_context, parse_stored_message
from ai_runtime.context.system import assemble_system_prompt, get_effective_blocks
from ai_runtime.watchdog.prompting import (
    _build_escalated_hint_block,
    _sanitize_watchdog_directive,
    prepare_pre_watchdog_and_model_prompt,
)

async def apply_rate_limit(user_id: int) -> tuple[bool, str | None]:
    """Apply rate limiting for AI calls. Wraps check_rate_limit().
    Returns (ok, error_message). ok=True means allowed.
    """
    allowed = await check_rate_limit(user_id, action='ai_call', limit=120, window_minutes=1)
    if not allowed:
        return (False, "Rate limit exceeded. Please wait before sending another message.")
    return (True, None)


async def update_chat_name_if_empty(conversation_id: int, user_message: str) -> None:
    """If the conversation has no chat_name, set it from the first 25 chars of the message.
    Pure DB operation, no Request needed.
    """
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT chat_name FROM CONVERSATIONS WHERE id = ?", (conversation_id,)
        )
        row = await cursor.fetchone()

    if not row or row[0]:
        return  # Already has a name or conversation not found

    # Extract text, clean HTML tags, limit to 25 chars
    try:
        message_list = orjson.loads(user_message)
        text = next((m['text'] for m in message_list if m.get('type') == 'text'), '')
    except (orjson.JSONDecodeError, TypeError, ValueError):
        text = user_message

    text = re.sub(r'<[^>]+>', '', text)[:25].strip()
    if not text:
        return

    async with get_db_connection() as conn:
        await conn.execute(
            "UPDATE CONVERSATIONS SET chat_name = ? WHERE id = ?", (text, conversation_id)
        )
        await conn.commit()


async def check_own_only_gransabio(user_id: int, conversation_id: int) -> str | None:
    """Check if user is own_only and the prompt has gransabio_enabled.
    Returns error message if blocked, None if OK.
    Called from save_message, process_save_message, and process_gransabio_external.
    """
    from common import API_KEY_MODE_OWN_ONLY
    async with get_db_connection(readonly=True) as conn_ro:
        cursor = await conn_ro.execute(
            "SELECT ud.api_key_mode, COALESCE(ep.gransabio_enabled, 0) "
            "FROM CONVERSATIONS c "
            "LEFT JOIN USER_DETAILS ud ON ud.user_id = c.user_id "
            "LEFT JOIN PROMPTS ep ON ep.id = COALESCE(c.role_id, ud.current_prompt_id) "
            "WHERE c.id = ? AND c.user_id = ?",
            (conversation_id, user_id),
        )
        row = await cursor.fetchone()
    if not row:
        return None
    api_key_mode, gs_enabled = row
    if bool(gs_enabled) and api_key_mode == API_KEY_MODE_OWN_ONLY:
        return "GranSabio is not available in own-keys-only mode. Contact admin."
    return None


async def run_input_moderation(
    user_message: str, images: list | None, enable_moderation: bool
) -> tuple[bool, dict | None]:
    """Call OpenAI Moderation API if enable_moderation is True.
    Request-free: no HTTP context needed.
    Returns (flagged, categories). flagged=True means message was rejected.
    """
    if not enable_moderation:
        return (False, None)

    # Build moderation input
    moderation_input = []
    if images:
        for item in images:
            if isinstance(item, dict):
                if item.get('type') == 'text':
                    moderation_input.append({"type": "text", "text": item['text']})
                elif item.get('type') == 'image_url':
                    moderation_input.append({
                        "type": "image_url",
                        "image_url": {"url": item['image_url']['url']}
                    })
                elif item.get('type') == 'image':
                    source = item.get('source', {})
                    if source.get('type') == 'base64':
                        moderation_input.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{source['media_type']};base64,{source['data']}"
                            }
                        })

    if not moderation_input:
        moderation_input = [{"type": "text", "text": user_message}]

    try:
        response = openai.moderations.create(
            model="omni-moderation-latest",
            input=moderation_input,
        )
        for result in response.results:
            if result.flagged:
                categories = {k: v for k, v in vars(result.categories).items() if v}
                return (True, categories)
        return (False, None)
    except Exception as e:
        logger.error(f"Moderation API error (standalone): {e}")
        # Fail open: allow the message if moderation API fails
        return (False, None)


def _resolve_system_block(sys_key: str, content: str, is_enabled: bool) -> dict | None:
    """Resolve a system block from DB row, applying runtime policy.
    Returns the resolved block dict, or None if it should be excluded."""
    if sys_key not in SYSTEM_BLOCK_METADATA:
        return None
    meta = SYSTEM_BLOCK_METADATA[sys_key]
    default = DEFAULT_SYSTEM_BLOCKS[sys_key]
    if sys_key in MANDATORY_SYSTEM_KEYS:
        effective_content = content.strip() if content and content.strip() else default["content"]
        return {
            "system_key": sys_key,
            "content": effective_content,
            "position": meta["position"],
            "condition": meta["condition"],
        }
    if not is_enabled:
        return None
    effective_content = content.strip() if content and content.strip() else default["content"]
    return {
        "system_key": sys_key,
        "content": effective_content,
        "position": meta["position"],
        "condition": meta["condition"],
    }


async def get_effective_blocks() -> list[dict]:
    """Fetch blocks for runtime prompt assembly.
    Known system blocks are resolved via _resolve_system_block (normalized, policy-enforced).
    Custom blocks: enabled only, as-is from DB.
    Missing system blocks: filled from code defaults.
    All sorted by position then display_order."""
    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                """SELECT system_key, content, position, condition,
                          is_enabled, is_system, display_order
                   FROM SYSTEM_PROMPT_BLOCKS
                   WHERE is_system = 1 OR is_enabled = 1
                   ORDER BY CASE WHEN position = 'pre_prompt' THEN 0 ELSE 1 END,
                            display_order ASC, id ASC"""
            )
            rows = await cursor.fetchall()
    except Exception:
        logger.warning("Failed to read SYSTEM_PROMPT_BLOCKS, using code defaults")
        return sorted(DEFAULT_SYSTEM_BLOCKS.values(),
                      key=lambda b: (0 if b["position"] == "pre_prompt" else 1, b["display_order"]))

    blocks = []
    seen_system_keys = set()

    for sys_key, content, position, condition, is_enabled, is_system, display_order in rows:
        if sys_key and sys_key in SYSTEM_BLOCK_METADATA:
            if sys_key in seen_system_keys:
                logger.warning("Duplicate system block '%s', skipping", sys_key)
                continue
            seen_system_keys.add(sys_key)
            resolved = _resolve_system_block(sys_key, content, is_enabled)
            if resolved is None:
                continue
            resolved["display_order"] = SYSTEM_BLOCK_METADATA[sys_key]["display_order"]
            blocks.append(resolved)
        elif not sys_key and not is_system:
            blocks.append({
                "system_key": None,
                "content": content,
                "position": position,
                "condition": condition,
                "display_order": display_order,
            })
        else:
            logger.warning("Dropping invalid block row: system_key=%s, is_system=%s", sys_key, is_system)

    for key, default in DEFAULT_SYSTEM_BLOCKS.items():
        if key not in seen_system_keys:
            logger.warning("System block '%s' missing from DB, using code default", key)
            blocks.append(default)

    blocks.sort(key=lambda b: (0 if b["position"] == "pre_prompt" else 1, b.get("display_order", 0)))
    return blocks


def _render_block(block: dict, variables: dict) -> str:
    """Render a block's content with variable substitution."""
    rendered = _BLOCK_VAR_PATTERN.sub(
        lambda m: variables.get(m.group(1), m.group(0)), block["content"]
    )
    return rendered.strip()


def assemble_system_prompt(blocks: list[dict], variables: dict, prompt_base: str,
                           watchdog_enabled: bool, watchdog_hint_block: str = "") -> str:
    """Assemble the full system prompt from blocks, prompt_base, and optional watchdog hint."""
    pre_parts = []
    post_parts = []
    hint_inserted = False

    for block in blocks:
        if block["condition"] == "watchdog_only" and not watchdog_enabled:
            continue
        rendered = _render_block(block, variables)
        if not rendered:
            continue
        if block["position"] == "pre_prompt":
            pre_parts.append(rendered)
        else:
            post_parts.append(rendered)
            if (block.get("system_key") == "watchdog_preamble"
                    and watchdog_hint_block and not hint_inserted):
                hint = watchdog_hint_block.strip()
                if hint:
                    post_parts.append(hint)
                    hint_inserted = True

    if watchdog_enabled and watchdog_hint_block and not hint_inserted:
        hint = watchdog_hint_block.strip()
        if hint:
            post_parts.append(hint)

    all_parts = pre_parts + [prompt_base.strip()] + post_parts
    return "\n\n".join(p for p in all_parts if p)

async def build_full_prompt_context(
    user_id: int, prompt_id: int, conversation_id: int, user_message: str,
    context_messages: list | None = None, user_api_keys: dict | None = None,
    internal_turn_context: str | None = None,
) -> dict:
    """Encapsulates the full prompt assembly pipeline from get_ai_response().

    Request-free: takes IDs, loads everything from DB. Used by
    process_gransabio_external() for Telegram/WhatsApp background tasks.

    Returns dict with:
        action: 'continue' | 'takeover' | 'takeover_lock'
        full_prompt: str (assembled system prompt, only when action='continue')
        takeover_directive: str | None
        takeover_watchdog_config: dict | None
        takeover_context_messages: list | None
        takeover_source: str | None ('pre' or 'post' when action is takeover)
        pending_hint_event_type: str (event type from watchdog hint, e.g. 'security', 'drift')
        watchdog_config: dict | None (post-watchdog config for passing to streaming)
        watchdog_hint_active: bool
        watchdog_hint_eval_id: int | None
        gransabio_config_raw: str | None
        user_level: str ('admin' | 'user' | 'customer')
        original_prompt: str (prompt_base before final assembly, for takeover original_prompt)
    """
    result = {
        "action": "continue",
        "full_prompt": "",
        "takeover_directive": None,
        "takeover_watchdog_config": None,
        "takeover_context_messages": None,
        "takeover_source": None,  # "pre" or "post" when action is takeover
        "pending_hint_event_type": "",
        "watchdog_config": None,
        "watchdog_hint_active": False,
        "watchdog_hint_eval_id": None,
        "gransabio_config_raw": None,
        "user_level": "customer",
        "original_prompt": "",
        "atagia_context_active": False,
        "atagia_context_reason": "",
    }

    if context_messages is None:
        context_messages = []

    async with get_db_connection(readonly=True) as conn_ro:
        async with conn_ro.cursor() as cursor_ro:
            # Same query as get_ai_response but without gransabio columns (already resolved)
            await cursor_ro.execute("""
                SELECT
                    c.role_id,
                    p.prompt,
                    CASE WHEN c.role_id IS NULL THEN ud.current_prompt_id ELSE c.role_id END AS effective_role_id,
                    u.user_info,
                    ud.current_alter_ego_id,
                    COALESCE(p.extensions_enabled, 0),
                    COALESCE(p.extensions_auto_advance, 0),
                    COALESCE(p.extensions_free_selection, 1),
                    c.active_extension_id,
                    pe.name AS extension_name,
                    pe.prompt_text AS extension_prompt_text,
                    p.gransabio_config,
                    u.role_id AS user_role_id
                FROM CONVERSATIONS c
                LEFT JOIN PROMPTS p ON c.role_id = p.id
                LEFT JOIN USER_DETAILS ud ON ud.user_id = ?
                LEFT JOIN USERS u ON u.id = ?
                LEFT JOIN PROMPT_EXTENSIONS pe ON c.active_extension_id = pe.id
                WHERE c.id = ? AND c.user_id = ?
            """, (user_id, user_id, conversation_id, user_id))

            row = await cursor_ro.fetchone()
            if not row:
                logger.error(
                    "build_full_prompt_context: no conversation %d for user %d",
                    conversation_id, user_id,
                )
                return result

            (conversation_role_id, prompt, effective_role_id, user_info,
             current_alter_ego_id, extensions_enabled, extensions_auto_advance,
             extensions_free_selection, active_extension_id,
             extension_name, extension_prompt_text,
             gransabio_config_raw, user_role_id) = row

            result["gransabio_config_raw"] = gransabio_config_raw

            # Resolve effective prompt if role_id was NULL (rehydrate ALL prompt-dependent fields)
            if conversation_role_id is None and effective_role_id:
                async with get_db_connection() as conn_rw:
                    await conn_rw.execute(
                        "UPDATE CONVERSATIONS SET role_id = ? WHERE id = ?",
                        (effective_role_id, conversation_id),
                    )
                    await conn_rw.commit()
                await cursor_ro.execute(
                    "SELECT prompt, gransabio_config, extensions_enabled, "
                    "extensions_auto_advance, extensions_free_selection "
                    "FROM PROMPTS WHERE id = ?", (effective_role_id,)
                )
                pr = await cursor_ro.fetchone()
                if pr:
                    prompt = pr[0] or prompt
                    gransabio_config_raw = pr[1]
                    extensions_enabled = bool(pr[2]) if pr[2] else False
                    extensions_auto_advance = bool(pr[3]) if pr[3] else False
                    extensions_free_selection = bool(pr[4]) if pr[4] is not None else True
                    result["gransabio_config_raw"] = gransabio_config_raw

            if not prompt:
                prompt = ""

            # User level (request-free: resolve from DB role_id)
            user_level = "customer"
            if user_role_id:
                await cursor_ro.execute(
                    "SELECT role_name FROM USER_ROLES WHERE id = ?", (user_role_id,)
                )
                role_row = await cursor_ro.fetchone()
                if role_row:
                    role_name = (role_row[0] or "").lower()
                    if role_name == "admin":
                        user_level = "admin"
                    elif role_name == "user":
                        user_level = "user"

            # --- Alter-ego / user_info injection ---
            if current_alter_ego_id:
                await cursor_ro.execute(
                    "SELECT name, description FROM USER_ALTER_EGOS WHERE id = ? AND user_id = ?",
                    (current_alter_ego_id, user_id),
                )
                ae_row = await cursor_ro.fetchone()
                if ae_row:
                    ae_name, ae_desc = ae_row
                    if ae_desc:
                        prompt_base = f"User info:\nName: {ae_name}\n{ae_desc}\n\n-----\nSystem info:\n{prompt}"
                    else:
                        prompt_base = f"User info:\nName: {ae_name}\n\n-----\nSystem info:\n{prompt}"
                else:
                    prompt_base = f"User info:\n{user_info}\n\n-----\nSystem info:\n{prompt}" if user_info else prompt
            else:
                prompt_base = f"User info:\n{user_info}\n\n-----\nSystem info:\n{prompt}" if user_info else prompt

            # --- Extensions injection ---
            if extensions_enabled and extension_prompt_text:
                prompt_base = (
                    f"{prompt_base}\n\n"
                    f"--- ACTIVE EXTENSION: {extension_name} ---\n"
                    f"{extension_prompt_text}\n"
                    f"--- END EXTENSION ---"
                )

            if extensions_enabled and extensions_auto_advance:
                async with get_db_connection(readonly=True) as conn_ext:
                    cursor_ext = await conn_ext.execute(
                        "SELECT id, name, display_order, description FROM PROMPT_EXTENSIONS WHERE prompt_id = ? ORDER BY display_order",
                        (effective_role_id,),
                    )
                    all_extensions = await cursor_ext.fetchall()
                    if all_extensions:
                        ext_list = "\n".join([
                            f"  - [{e[0]}] {e[1]}{' (CURRENT)' if e[0] == active_extension_id else ''}: {e[3] or 'No description'}"
                            for e in all_extensions
                        ])
                        prompt_base += (
                            f"\n\n--- EXTENSION LEVELS ---\n"
                            f"This conversation has the following levels/phases. "
                            f"You are currently on the one marked (CURRENT).\n"
                            f"When you determine the current level's objectives are sufficiently covered, "
                            f"use the advanceExtension tool to transition to the next level.\n"
                            f"{ext_list}\n--- END EXTENSION LEVELS ---"
                        )

            # A channel may provide trusted ephemeral state (the native phone
            # clock is the first consumer).  Apply it once before pre-watchdog
            # so the evaluator and main model see exactly the same base block.
            prompt_base, pre_watchdog_prompt_context = (
                prepare_pre_watchdog_and_model_prompt(
                    prompt_base,
                    internal_turn_context,
                )
            )

            # --- Watchdog config ---
            watchdog_config = None
            watchdog_hint_block = ""
            watchdog_hint_active = False
            watchdog_hint_eval_id = None
            watchdog_enabled = False
            pre_watchdog_config = None
            post_watchdog_config = None

            if effective_role_id:
                await cursor_ro.execute(
                    "SELECT watchdog_config FROM PROMPTS WHERE id = ?", (effective_role_id,)
                )
                wd_row = await cursor_ro.fetchone()
                if wd_row and wd_row[0]:
                    try:
                        raw_wd = orjson.loads(wd_row[0])
                        post_watchdog_config = extract_post_watchdog_config(raw_wd)
                        pre_watchdog_config = extract_pre_watchdog_config(raw_wd)
                        watchdog_config = post_watchdog_config
                    except orjson.JSONDecodeError:
                        pass

                # --- Pre-watchdog evaluation ---
                if pre_watchdog_config and pre_watchdog_config.get("enabled"):
                    try:
                        pre_freq = pre_watchdog_config.get("frequency", 1)
                        await cursor_ro.execute(
                            "SELECT COUNT(*) FROM MESSAGES WHERE conversation_id = ? AND type = 'user'",
                            (conversation_id,),
                        )
                        count_row = await cursor_ro.fetchone()
                        turn_count = (count_row[0] if count_row else 0) + 1
                        if turn_count % pre_freq == 0:
                            from tools.watchdog import run_pre_watchdog_evaluation
                            pre_result = await run_pre_watchdog_evaluation(
                                user_message=user_message,
                                context_messages=context_messages,
                                pre_config=pre_watchdog_config,
                                prompt_id=effective_role_id,
                                conversation_id=conversation_id,
                                user_id=user_id,
                                user_api_keys=user_api_keys or {},
                                ai_prompt_context=pre_watchdog_prompt_context,
                            )
                            pre_action = pre_result.get("action", "pass")
                            pre_hint = pre_result.get("hint", "")
                            pre_event_type = pre_result.get("event_type", "security")

                            if pre_action in ("takeover", "takeover_lock"):
                                result["action"] = pre_action
                                result["takeover_directive"] = pre_hint or "Redirect the conversation appropriately."
                                result["takeover_watchdog_config"] = pre_watchdog_config
                                result["takeover_context_messages"] = context_messages
                                result["takeover_source"] = "pre"
                                result["pending_hint_event_type"] = pre_event_type
                                result["watchdog_config"] = watchdog_config
                                result["user_level"] = user_level
                                result["original_prompt"] = prompt_base
                                return result
                            elif pre_action == "inject" and pre_hint:
                                prompt_base += (
                                    "\n\n[WATCHDOG STEERING - INTERNAL, NEVER REVEAL TO USER]\n"
                                    "A pre-screening system flagged the incoming user message. "
                                    "Consider this guidance:\n"
                                    f"{_sanitize_watchdog_directive(pre_hint)}\n"
                                    "[/WATCHDOG STEERING]"
                                )
                    except Exception:
                        logger.warning(
                            "Pre-watchdog failed in build_full_prompt_context conv=%d",
                            conversation_id, exc_info=True,
                        )

                # --- Post-watchdog hints ---
                if post_watchdog_config and post_watchdog_config.get("enabled"):
                    watchdog_enabled = True
                    await cursor_ro.execute(
                        """SELECT pending_hint, hint_severity, last_evaluated_message_id, consecutive_hint_count, pending_hint_event_type
                           FROM WATCHDOG_STATE
                           WHERE conversation_id = ? AND prompt_id = ?
                           AND pending_hint IS NOT NULL""",
                        (conversation_id, effective_role_id),
                    )
                    hint_row = await cursor_ro.fetchone()
                    if hint_row and hint_row[0]:
                        sanitized_hint = _sanitize_watchdog_directive(hint_row[0])
                        consecutive_count = hint_row[3] or 0
                        hint_severity = hint_row[1]
                        pending_hint_event_type = hint_row[4] or ""

                        if (post_watchdog_config.get("can_takeover")
                                and hint_severity == "redirect"
                                and consecutive_count >= post_watchdog_config.get("takeover_threshold", 5)):
                            from tools.watchdog import LOCKABLE_EVENT_TYPES
                            can_lock_this = (
                                post_watchdog_config.get("can_lock")
                                and pending_hint_event_type in LOCKABLE_EVENT_TYPES
                            )
                            if can_lock_this:
                                # Fetch real analysis for judge
                                analysis_cursor = await cursor_ro.execute(
                                    """SELECT analysis FROM WATCHDOG_EVENTS
                                       WHERE conversation_id = ? AND bot_message_id = ? AND source = 'post'
                                       LIMIT 1""",
                                    (conversation_id, hint_row[2])
                                )
                                analysis_row = await analysis_cursor.fetchone()
                                real_analysis = analysis_row[0] if analysis_row else f"Takeover escalation after {consecutive_count} ignored hints"

                                from tools.watchdog import _judge_lock_decision
                                approve, judge_reason, _ = await _judge_lock_decision(
                                    conversation_id, effective_role_id, pending_hint_event_type, real_analysis
                                )
                                if not approve:
                                    can_lock_this = False
                                    logger.info("Lock Judge rejected takeover lock for conv=%d: %s", conversation_id, judge_reason)
                            result["action"] = "takeover_lock" if can_lock_this else "takeover"
                            result["takeover_directive"] = sanitized_hint
                            result["takeover_watchdog_config"] = post_watchdog_config
                            result["takeover_context_messages"] = context_messages
                            result["takeover_source"] = "post"
                            result["pending_hint_event_type"] = pending_hint_event_type
                            result["last_evaluated_message_id"] = hint_row[2]
                            result["watchdog_config"] = watchdog_config
                            result["user_level"] = user_level
                            result["original_prompt"] = prompt_base
                            return result

                        watchdog_hint_block = _build_escalated_hint_block(
                            sanitized_hint, hint_row[1], consecutive_count
                        )
                        watchdog_hint_active = True
                        watchdog_hint_eval_id = hint_row[2]

            # --- Final assembly with global system prompt blocks ---
            blocks = await get_effective_blocks()
            variables = {
                "user_level": user_level,
                "current_datetime_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            }
            full_prompt = assemble_system_prompt(
                blocks, variables, prompt_base, watchdog_enabled, watchdog_hint_block
            )
            memory_decision = await _resolve_memory_context(
                full_prompt,
                user_id=user_id,
                conversation_id=conversation_id,
                message=user_message,
                prompt_id=effective_role_id,
            )
            full_prompt = memory_decision.full_prompt
            context_messages = _context_messages_for_memory_provider(
                context_messages,
                memory_decision,
            )

    result["full_prompt"] = full_prompt
    result["atagia_context_active"] = memory_decision.active and memory_decision.provider == "atagia"
    result["atagia_context_reason"] = memory_decision.reason
    result["memory_context_active"] = memory_decision.active
    result["memory_context_reason"] = memory_decision.reason
    result["memory_provider"] = memory_decision.provider
    result["watchdog_config"] = watchdog_config
    result["watchdog_hint_active"] = watchdog_hint_active
    result["watchdog_hint_eval_id"] = watchdog_hint_eval_id
    result["user_level"] = user_level
    result["original_prompt"] = prompt_base
    return result
