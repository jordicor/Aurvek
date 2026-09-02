from ai_runtime.dependencies import *
from ai_runtime.memory.context import _warmup_memory_provider
from ai_runtime.context.formatting import flatten_multi_ai_context, parse_stored_message
from ai_runtime.context.system import assemble_system_prompt, get_effective_blocks
from ai_runtime.watchdog.prompting import _build_escalated_hint_block, _sanitize_watchdog_directive

_WARMUP_ACTIVITIES = {"typing", "attachment", "audio_recording", "voice_call"}

def _coerce_nonnegative_int(value: Any, default: int = 0, maximum: int = 10_000_000) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    return min(parsed, maximum)


def _sanitize_warmup_payload(payload: Any) -> tuple[dict[str, Any] | None, str | None]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return None, "Warm-up payload must be a JSON object."

    activity = payload.get("activity", "typing")
    if activity not in _WARMUP_ACTIVITIES:
        return None, "Invalid warm-up activity."

    attachment_kinds = payload.get("attachment_kinds") or []
    if not isinstance(attachment_kinds, list):
        attachment_kinds = []
    clean_kinds = []
    for kind in attachment_kinds[:16]:
        if not isinstance(kind, str):
            continue
        normalized = re.sub(r"[^a-z0-9_-]", "", kind.lower())[:32]
        if normalized:
            clean_kinds.append(normalized)

    multi_ai_model_ids = normalize_warmup_model_ids(payload.get("multi_ai_model_ids"))

    return {
        "activity": activity,
        "draft_length": _coerce_nonnegative_int(payload.get("draft_length")),
        "has_attachments": bool(payload.get("has_attachments")),
        "attachment_kinds": clean_kinds,
        "multi_ai_model_ids": multi_ai_model_ids,
        "last_known_message_id": _coerce_nonnegative_int(payload.get("last_known_message_id")),
    }, None


def _warmup_mode_from_model_ids(model_ids: tuple[int, ...]) -> str:
    return "multi" if len(model_ids) >= 2 else "single"


def _build_warmup_cache_key_from_state(
    state: dict[str, Any],
    user_id: int,
    conversation_id: int,
    mode: str = "single",
    multi_ai_model_ids: tuple[int, ...] | list[int] | None = None,
) -> WarmupCacheKey:
    return WarmupCacheKey(
        user_id=int(user_id),
        conversation_id=int(conversation_id),
        llm_id=int(state.get("llm_id") or 0),
        effective_prompt_id=int(state.get("effective_prompt_id") or 0),
        active_extension_id=int(state.get("active_extension_id") or 0),
        last_message_id=int(state.get("last_message_id") or 0),
        mode=mode,
        multi_ai_model_ids=normalize_warmup_model_ids(multi_ai_model_ids),
    )

async def _load_warmup_conversation_state(conversation_id: int, user_id: int) -> dict[str, Any] | None:
    await ensure_conversation_privacy_schema()
    async with get_db_connection(readonly=True) as conn_ro:
        cursor = await conn_ro.execute(
            """
            SELECT
                c.id AS conversation_id,
                c.locked,
                c.user_id,
                c.llm_id,
                c.chat_name,
                c.role_id,
                CASE
                    WHEN c.role_id IS NULL THEN ud.current_prompt_id
                    ELSE c.role_id
                END AS effective_prompt_id,
                c.active_extension_id,
                L.machine,
                L.model,
                COALESCE(L.input_token_cost, 0) AS input_token_cost,
                COALESCE(L.output_token_cost, 0) AS output_token_cost,
                COALESCE(p.enable_moderation, 0) AS enable_moderation,
                COALESCE(p.is_paid, 0) AS prompt_is_paid,
                COALESCE(p.gransabio_enabled, 0) AS gransabio_enabled,
                COALESCE(p.disable_web_search, 0) AS disable_web_search,
                COALESCE(p.force_web_search, 0) AS force_web_search,
                COALESCE(ud.web_search_enabled, 1) AS user_web_search_enabled,
                COALESCE(ud.web_search_mode, 'native') AS web_search_mode,
                COALESCE(c.is_incognito, 0) AS is_incognito,
                (
                    SELECT COALESCE(MAX(m.id), 0)
                    FROM MESSAGES m
                    WHERE m.conversation_id = c.id
                ) AS last_message_id
            FROM CONVERSATIONS c
            JOIN LLM L ON c.llm_id = L.id
            LEFT JOIN USER_DETAILS ud ON ud.user_id = c.user_id
            LEFT JOIN PROMPTS p ON p.id = COALESCE(c.role_id, ud.current_prompt_id)
            WHERE c.id = ? AND c.user_id = ?
            """,
            (conversation_id, user_id),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def _load_warmup_context_messages(conversation_id: int, start_date: datetime) -> list[dict[str, Any]]:
    async with get_db_connection(readonly=True) as conn_ro:
        cursor = await conn_ro.execute(
            """
            SELECT id, message, type
            FROM messages
            WHERE conversation_id = ?
            AND date >= ?
            ORDER BY id ASC, date ASC
            """,
            (conversation_id, start_date),
        )
        rows = await cursor.fetchall()

    messages = [
        {
            "id": int(row[0]),
            "message": parse_stored_message(custom_unescape(row[1])),
            "type": row[2],
        }
        for row in rows
    ]
    return flatten_multi_ai_context(messages)


async def _load_warmup_prompt_runtime_snapshot(
    conversation_id: int,
    current_user: User,
    effective_prompt_id: int | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "effective_prompt_id": effective_prompt_id,
        "prompt_base": "",
        "full_prompt": "",
        "system_blocks_count": 0,
        "web_search": {
            "disable_web_search": False,
            "force_web_search": False,
            "user_web_search_enabled": True,
            "web_search_mode": "native",
        },
        "extensions": {
            "enabled": False,
            "auto_advance": False,
            "free_selection": True,
            "active_extension_id": None,
            "has_levels": False,
        },
        "watchdog": {
            "post_enabled": False,
            "pre_enabled": False,
            "hint_active": False,
            "hint_eval_id": None,
            "config": None,
        },
        "gransabio_config_raw": None,
        "memory_context": [],
    }

    async with get_db_connection(readonly=True) as conn_ro:
        cursor = await conn_ro.execute(
            """
            SELECT
                p.prompt,
                p.gransabio_config,
                p.watchdog_config,
                COALESCE(p.disable_web_search, 0) AS disable_web_search,
                COALESCE(p.force_web_search, 0) AS force_web_search,
                COALESCE(p.extensions_enabled, 0) AS extensions_enabled,
                COALESCE(p.extensions_auto_advance, 0) AS extensions_auto_advance,
                COALESCE(p.extensions_free_selection, 1) AS extensions_free_selection,
                u.user_info,
                u.role_id AS user_role_id,
                ud.current_alter_ego_id,
                COALESCE(ud.web_search_enabled, 1) AS user_web_search_enabled,
                COALESCE(ud.web_search_mode, 'native') AS web_search_mode,
                c.active_extension_id,
                pe.name AS extension_name,
                pe.prompt_text AS extension_prompt_text
            FROM CONVERSATIONS c
            LEFT JOIN USER_DETAILS ud ON ud.user_id = c.user_id
            LEFT JOIN USERS u ON u.id = c.user_id
            LEFT JOIN PROMPTS p ON p.id = ?
            LEFT JOIN PROMPT_EXTENSIONS pe ON c.active_extension_id = pe.id
            WHERE c.id = ? AND c.user_id = ?
            """,
            (effective_prompt_id, conversation_id, current_user.id),
        )
        row = await cursor.fetchone()
        if not row:
            return result

        data = dict(row)
        raw_prompt = data.get("prompt") or ""
        user_info = data.get("user_info")
        current_alter_ego_id = data.get("current_alter_ego_id")
        extensions_enabled = bool(data.get("extensions_enabled"))
        extensions_auto_advance = bool(data.get("extensions_auto_advance"))
        extensions_free_selection = bool(data.get("extensions_free_selection"))
        active_extension_id = data.get("active_extension_id")
        extension_name = data.get("extension_name")
        extension_prompt_text = data.get("extension_prompt_text")
        raw_watchdog_config = data.get("watchdog_config")

        result["web_search"] = {
            "disable_web_search": bool(data.get("disable_web_search")),
            "force_web_search": bool(data.get("force_web_search")),
            "user_web_search_enabled": bool(data.get("user_web_search_enabled")),
            "web_search_mode": data.get("web_search_mode") or "native",
        }
        result["extensions"].update({
            "enabled": extensions_enabled,
            "auto_advance": extensions_auto_advance,
            "free_selection": extensions_free_selection,
            "active_extension_id": active_extension_id,
        })
        result["gransabio_config_raw"] = data.get("gransabio_config")

        if await current_user.is_admin:
            user_level = "admin"
        elif await current_user.is_user:
            user_level = "user"
        else:
            user_level = "customer"

        if current_alter_ego_id:
            cursor = await conn_ro.execute(
                """
                SELECT name, description
                FROM USER_ALTER_EGOS
                WHERE id = ? AND user_id = ?
                """,
                (current_alter_ego_id, current_user.id),
            )
            alter_ego_row = await cursor.fetchone()
            if alter_ego_row:
                alter_ego_name, alter_ego_description = alter_ego_row
                if alter_ego_description:
                    prompt_base = (
                        f"User info:\nName: {alter_ego_name}\n{alter_ego_description}"
                        f"\n\n-----\nSystem info:\n{raw_prompt}"
                    )
                else:
                    prompt_base = f"User info:\nName: {alter_ego_name}\n\n-----\nSystem info:\n{raw_prompt}"
            elif user_info:
                prompt_base = f"User info:\n{user_info}\n\n-----\nSystem info:\n{raw_prompt}"
            else:
                prompt_base = raw_prompt
        elif user_info:
            prompt_base = f"User info:\n{user_info}\n\n-----\nSystem info:\n{raw_prompt}"
        else:
            prompt_base = raw_prompt

        if extensions_enabled and extension_prompt_text:
            prompt_base = (
                f"{prompt_base}\n\n"
                f"--- ACTIVE EXTENSION: {extension_name} ---\n"
                f"{extension_prompt_text}\n"
                f"--- END EXTENSION ---"
            )

        if extensions_enabled and extensions_auto_advance and effective_prompt_id:
            cursor = await conn_ro.execute(
                """
                SELECT id, name, display_order, description
                FROM PROMPT_EXTENSIONS
                WHERE prompt_id = ?
                ORDER BY display_order
                """,
                (effective_prompt_id,),
            )
            all_extensions = await cursor.fetchall()
            if all_extensions:
                result["extensions"]["has_levels"] = True
                ext_list = "\n".join([
                    f"  - [{ext[0]}] {ext[1]}{' (CURRENT)' if ext[0] == active_extension_id else ''}: {ext[3] or 'No description'}"
                    for ext in all_extensions
                ])
                prompt_base += (
                    "\n\n--- EXTENSION LEVELS ---\n"
                    "This conversation has the following levels/phases. You are currently on the one marked (CURRENT).\n"
                    "When you determine the current level's objectives are sufficiently covered, "
                    "use the advanceExtension tool to transition to the next level.\n"
                    f"{ext_list}\n"
                    "--- END EXTENSION LEVELS ---"
                )

        watchdog_config = None
        watchdog_hint_block = ""
        watchdog_enabled = False
        watchdog_hint_active = False
        watchdog_hint_eval_id = None
        pre_watchdog_config = None

        if raw_watchdog_config:
            try:
                parsed_watchdog = (
                    orjson.loads(raw_watchdog_config)
                    if isinstance(raw_watchdog_config, (str, bytes, bytearray))
                    else raw_watchdog_config
                )
                watchdog_config = extract_post_watchdog_config(parsed_watchdog)
                pre_watchdog_config = extract_pre_watchdog_config(parsed_watchdog)
            except (orjson.JSONDecodeError, TypeError, ValueError):
                watchdog_config = None
                pre_watchdog_config = None

        if watchdog_config and watchdog_config.get("enabled"):
            watchdog_enabled = True
            cursor = await conn_ro.execute(
                """
                SELECT pending_hint, hint_severity, last_evaluated_message_id,
                       consecutive_hint_count, pending_hint_event_type
                FROM WATCHDOG_STATE
                WHERE conversation_id = ? AND prompt_id = ?
                AND pending_hint IS NOT NULL
                """,
                (conversation_id, effective_prompt_id),
            )
            hint_row = await cursor.fetchone()
            if hint_row and hint_row[0]:
                sanitized_hint = _sanitize_watchdog_directive(hint_row[0])
                watchdog_hint_block = _build_escalated_hint_block(
                    sanitized_hint, hint_row[1], hint_row[3] or 0
                )
                watchdog_hint_active = True
                watchdog_hint_eval_id = hint_row[2]

        blocks = await get_effective_blocks()
        full_prompt = assemble_system_prompt(
            blocks,
            {"user_level": user_level},
            prompt_base,
            watchdog_enabled,
            watchdog_hint_block,
        )

    result["prompt_base"] = prompt_base
    result["full_prompt"] = full_prompt
    result["system_blocks_count"] = len(blocks)
    result["watchdog"] = {
        "post_enabled": bool(watchdog_config and watchdog_config.get("enabled")),
        "pre_enabled": bool(pre_watchdog_config and pre_watchdog_config.get("enabled")),
        "hint_active": watchdog_hint_active,
        "hint_eval_id": watchdog_hint_eval_id,
        "config": watchdog_config,
    }
    return result


async def _build_chat_warmup_snapshot(
    conversation_id: int,
    current_user: User,
    state: dict[str, Any],
    cache_key: WarmupCacheKey,
    activity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context_months = 2
    start_date = (
        datetime.now(timezone.utc) - timedelta(days=context_months * 30)
    ).strftime("%Y-%m-%d %H:%M:%S.%f")
    effective_prompt_id = state.get("effective_prompt_id")

    context_messages, prompt_runtime, memory_sidecar = await asyncio.gather(
        _load_warmup_context_messages(conversation_id, start_date),
        _load_warmup_prompt_runtime_snapshot(conversation_id, current_user, effective_prompt_id),
        _warmup_memory_provider(
            current_user.id,
            conversation_id,
            prompt_id=effective_prompt_id,
            incognito=bool(state.get("is_incognito")),
        ),
    )

    return {
        "cache_key": cache_key,
        "conversation_id": conversation_id,
        "user_id": current_user.id,
        "mode": cache_key.mode,
        "activity": activity or {},
        "state": {
            "llm_id": state.get("llm_id"),
            "effective_prompt_id": effective_prompt_id,
            "active_extension_id": state.get("active_extension_id"),
            "last_message_id": state.get("last_message_id") or 0,
            "machine": state.get("machine"),
            "model": state.get("model"),
            "chat_name": state.get("chat_name"),
            "web_search": {
                "disable_web_search": bool(state.get("disable_web_search")),
                "force_web_search": bool(state.get("force_web_search")),
                "user_web_search_enabled": bool(state.get("user_web_search_enabled")),
                "web_search_mode": state.get("web_search_mode") or "native",
            },
            "is_incognito": bool(state.get("is_incognito")),
        },
        "context_messages": context_messages,
        "context_count": len(context_messages),
        "last_message_id": state.get("last_message_id") or 0,
        "prompt_runtime": prompt_runtime,
        "memory_context": [],
        "sidecars": memory_sidecar,
    }


def _copy_warmup_context_messages(snapshot: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    if not snapshot:
        return None
    context_messages = snapshot.get("context_messages")
    if not isinstance(context_messages, list):
        return None
    return copy.deepcopy(context_messages)
