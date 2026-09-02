import inspect
import math

from ai_runtime.channel_turns import (
    ChannelContext,
    StaleChannelTurnError,
    bind_channel_turn,
)
from ai_runtime.dependencies import *
from billing.usage_reservations import (
    BillingReservationError,
    InsufficientBalanceError,
    accumulate_ai_reservation_usage,
    estimate_structured_billing_tokens,
    estimate_structured_usage_tokens,
    get_user_billing_availability,
    get_variable_billing_rates,
    refund_fixed_usage,
    reserve_ai_usage,
    revalidate_user_billing,
    settle_ai_reservation_components,
)
from ai_runtime.attachments.pdf import (
    _estimate_pdf_input_tokens_for_preflight,
    _extract_pdf_metadata_from_context_messages,
    _messages_have_saved_pdfs,
    _pdf_upload_too_large_payload,
)
from ai_runtime.memory.context import _context_messages_for_memory_provider, _resolve_memory_context
from ai_runtime.billing import assert_billable_claude_system_key
from ai_runtime.config import _model_output_cap
from ai_runtime.context.formatting import _format_messages_for_provider, flatten_multi_ai_context, parse_stored_message
from ai_runtime.context.history import apply_no_memory_context_budget
from ai_runtime.context.message_provenance import (
    merge_internal_turn_context,
    prepare_trusted_history_context,
    render_current_input_context,
)
from ai_runtime.context.system import assemble_system_prompt, get_effective_blocks
from ai_runtime.context.warmup import _build_warmup_cache_key_from_state, _copy_warmup_context_messages
from ai_runtime.multi_ai.errors import MultiAiBillingError
from ai_runtime.persistence.messages import (
    persistence_error_payload,
    save_content_to_db,
    save_multi_ai_to_db,
)
from ai_runtime.reasoning import (
    ReasoningValidationError,
    parse_reasoning_selection,
    resolve_and_validate,
)
from ai_runtime.providers.claude import call_claude_api
from ai_runtime.providers.gemini import call_gemini_api
from ai_runtime.providers.kimi import call_kimi_api
from ai_runtime.providers.minimax import call_minimax_api
from ai_runtime.providers.openai_chat import call_o1_api
from ai_runtime.providers.openai_responses import call_gpt_responses_api
from ai_runtime.providers.openrouter import call_openrouter_api
from ai_runtime.providers.xai import call_xai_responses_api
from ai_runtime.watchdog.prompting import _build_escalated_hint_block, _sanitize_watchdog_directive
from memory.health import get_user_memory_health_snapshot, should_surface_memory_health

def build_multi_ai_message(results: dict, model_ids: list) -> str:
    """Build the JSON string for a Multi-AI bot message.

    Args:
        results: dict of llm_id -> {content, input_tokens, output_tokens, error, model, machine}
        model_ids: ordered list of llm_ids

    Returns:
        JSON string for storage in MESSAGES.message column
    """
    responses = []
    for llm_id in model_ids:
        r = results[llm_id]
        response = {
            "llm_id": llm_id,
            "machine": r["machine"],
            "model": r["model"],
            "content": r["content"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
        }
        if r.get("error"):
            response["error"] = True
        responses.append(response)

    return orjson.dumps({"multi_ai": True, "responses": responses}).decode()


async def _recover_stale_multi_ai_turn(
    *,
    channel_context: ChannelContext | None,
    conversation_id: int,
    user_id: int,
    user_message: str,
    prompt_id: int | None,
    llm_id: int | None,
) -> dict:
    if channel_context is None or channel_context.recover_stale_context is None:
        return {"terminal": "stale_channel_turn"}
    try:
        recovered = channel_context.recover_stale_context(channel_context)
        if inspect.isawaitable(recovered):
            recovered = await recovered
        if recovered is None or not recovered.ingest_only:
            return {"terminal": "stale_channel_turn"}
        with bind_channel_turn(recovered):
            user_message_id, _ = await save_content_to_db(
                None,
                0,
                0,
                0,
                conversation_id,
                user_id,
                "multi-ai",
                user_message=user_message,
                prompt_id=prompt_id,
                llm_id=llm_id,
            )
        if user_message_id is None:
            return {
                "terminal": "stale_channel_turn",
                **persistence_error_payload(),
            }
        return {
            "terminal": "queued_for_active_phone",
            "message_id": int(user_message_id),
        }
    except StaleChannelTurnError:
        return {"terminal": "stale_channel_turn"}
    except Exception:
        logger.exception(
            "[process_multi_ai_message] Could not recover stale turn for conversation %s",
            conversation_id,
        )
        return {
            "terminal": "stale_channel_turn",
            **persistence_error_payload(),
        }

async def _is_prompt_paid(prompt_id: int) -> bool:
    """Check if a prompt is a paid prompt."""
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute("SELECT is_paid FROM PROMPTS WHERE id = ?", (prompt_id,))
        row = await cursor.fetchone()
        return bool(row[0]) if row else False

async def _run_single_ai(
    queue: asyncio.Queue,
    llm_id: int,
    llm_info: dict,
    context_messages: list,
    user_message: str,
    system_prompt: str,
    conversation_id: int,
    current_user,
    request,
    max_tokens: int,
    reasoning_selection=None,
    user_api_key: str = None,
    prompt_id: int = None,
    temperature: float = 0.7,
    input_token_fallback: int = 0,
    pdf_error_metadata: dict | None = None,
    apply_no_memory_limit: bool = False,
):
    """Run a single AI model and put results into the shared queue.

    Does NOT save to DB - the orchestrator handles combined save.
    Tools are DISABLED for all Multi-AI workers.
    """
    machine = llm_info["machine"]
    model = llm_info["model"]
    provider_machine = machine
    api_model = None
    pdf_redirect_active = False
    input_tokens_collected = 0
    output_tokens_collected = 0
    content_collected = ""
    provider_error = None

    try:
        system_prompt, context_messages = await prepare_trusted_history_context(
            system_prompt,
            context_messages,
        )

        if apply_no_memory_limit:
            context_messages = await apply_no_memory_context_budget(
                context_messages,
                llm_id=llm_id,
                prompt_id=prompt_id,
                full_prompt=system_prompt,
                current_message=user_message,
            )

        if machine in ("GPT", "xAI") and _messages_have_saved_pdfs(context_messages):
            pdf_redirect_active = True
            provider_machine = "OpenRouter"
            api_model = OPENROUTER_MODEL_MAP.get(
                model,
                f"openai/{model}" if machine == "GPT" else f"x-ai/{model}"
            )
            logger.info(
                "Multi-AI PDF redirect: %s/%s -> OpenRouter/%s",
                machine,
                model,
                api_model,
            )

        # Format messages for the provider
        api_messages = await _format_messages_for_provider(
            context_messages, user_message, system_prompt, provider_machine, current_user,
            conversation_id=conversation_id,
        )

        # Select the appropriate call function based on machine
        if provider_machine == "Gemini":
            api_func = call_gemini_api
        elif provider_machine == "O1":
            api_func = call_o1_api
        elif provider_machine == "GPT":
            api_func = call_gpt_responses_api
        elif provider_machine == "Claude":
            api_func = call_claude_api
        elif provider_machine == "xAI":
            api_func = call_xai_responses_api
        elif provider_machine == "OpenRouter":
            api_func = call_openrouter_api
        elif provider_machine == "MiniMax":
            api_func = call_minimax_api
        elif provider_machine == "Kimi":
            api_func = call_kimi_api
        else:
            raise ValueError(f"Unknown machine type: {provider_machine}")

        # Build kwargs with save_to_db=False, tools disabled
        kwargs = {
            "messages": api_messages,
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "prompt": system_prompt,
            "conversation_id": conversation_id,
            "current_user": current_user,
            "request": request,
            "user_message": None,  # Don't save user message per-worker
            "input_token_fallback": input_token_fallback,
            "pdf_error_metadata": pdf_error_metadata,
            "save_to_db": False,
            "llm_id": llm_id,
            "prompt_id": prompt_id,
            "reasoning_selection": reasoning_selection,
        }

        # O1 doesn't accept tools parameter - only add for functions that support it
        if provider_machine != "O1":
            kwargs["tools"] = None  # Tools disabled for Multi-AI

        if api_model:
            kwargs["api_model"] = api_model

        if user_api_key:
            kwargs["user_api_key"] = user_api_key

        # Iterate over the async generator
        async for chunk in api_func(**kwargs):
            # Check stop signal
            if stop_signals.get(conversation_id):
                break

            if not isinstance(chunk, str):
                continue

            # Parse SSE lines
            if chunk.startswith("data: "):
                data_part = chunk[6:].strip()

                if data_part == "[DONE]":
                    break

                if data_part.startswith("{"):
                    try:
                        chunk_data = orjson.loads(data_part)

                        if "token_info" in chunk_data:
                            input_tokens_collected = chunk_data.get("input_tokens", 0)
                            output_tokens_collected = chunk_data.get("output_tokens", 0)
                        elif "content" in chunk_data:
                            content_text = chunk_data["content"]
                            content_collected += content_text
                            await queue.put({
                                "type": "chunk",
                                "llm_id": llm_id,
                                "model": model,
                                "content": content_text,
                            })
                        elif "error" in chunk_data:
                            provider_error = {
                                "type": "error",
                                "llm_id": llm_id,
                                "model": model,
                                "error": str(chunk_data["error"])[:200],
                            }
                            for key in (
                                "error_code",
                                "pdf_too_large",
                                "provider",
                                "provider_message",
                                "provider_health",
                                "provider_status",
                                "provider_health_message",
                                "filename",
                                "pages",
                                "pdf_count",
                                "current_pdf_count",
                                "context_pdf_count",
                                "range_retry_available",
                                "retry_filename",
                                "retry_pages",
                                "retry_token",
                            ):
                                if key in chunk_data:
                                    provider_error[key] = chunk_data[key]
                    except orjson.JSONDecodeError:
                        pass

        usage_observed = bool(
            input_tokens_collected
            or output_tokens_collected
            or content_collected
        )
        usage_payload = {
            "input_tokens": (
                input_tokens_collected
                or (int(input_token_fallback or 0) if usage_observed or not provider_error else 0)
            ),
            "output_tokens": (
                output_tokens_collected
                or estimate_message_tokens(content_collected)
            ),
            "billable_usage": usage_observed or provider_error is None,
        }
        if provider_error:
            provider_error.update(usage_payload)
            await queue.put(provider_error)
        else:
            await queue.put({
                "type": "done",
                "llm_id": llm_id,
                "model": model,
                **usage_payload,
            })

    except Exception as exc:
        error_id = str(uuid.uuid4())[:8]
        logger.error(
            "[_run_single_ai] Error for llm_id=%d model=%s error_id=%s: %s",
            llm_id, model, error_id, exc, exc_info=True,
        )
        usage_observed = bool(
            input_tokens_collected
            or output_tokens_collected
            or content_collected
        )
        await queue.put({
            "type": "error",
            "llm_id": llm_id,
            "model": model,
            "error": f"Internal error (ref: {error_id})",
            "input_tokens": (
                input_tokens_collected
                or (int(input_token_fallback or 0) if usage_observed else 0)
            ),
            "output_tokens": (
                output_tokens_collected
                or estimate_message_tokens(content_collected)
            ),
            "billable_usage": usage_observed,
        })


async def process_multi_ai_message(
    request,
    conversation_id: int,
    current_user,
    user_message: str,
    model_ids: list,
    reasoning_selection=None,
    user_api_keys: dict = None,
    channel_context: ChannelContext | None = None,
):
    """Process a Multi-AI comparison request.

    Sends the same message to multiple AI models in parallel.
    Yields multiplexed SSE events.
    """
    global stop_signals

    # --- 1. Validation ---
    await ensure_conversation_privacy_schema()
    async with get_db_connection(readonly=True) as conn_ro:
        cursor = await conn_ro.execute(
            """SELECT c.locked, c.llm_id, c.user_id, c.chat_name,
                      CASE WHEN c.role_id IS NULL THEN ud.current_prompt_id ELSE c.role_id END AS effective_prompt_id,
                      c.active_extension_id,
                      (
                          SELECT COALESCE(MAX(m.id), 0)
                          FROM MESSAGES m
                          WHERE m.conversation_id = c.id
                      ) AS last_message_id,
                      COALESCE(p.enable_moderation, 0) AS enable_moderation,
                      COALESCE(p.forced_llm_id, 0) AS forced_llm_id,
                      p.allowed_llms,
                      COALESCE(p.force_web_search, 0) AS force_web_search,
                      COALESCE(p.gransabio_enabled, 0) AS gransabio_enabled,
                      COALESCE(c.is_incognito, 0) AS is_incognito
               FROM CONVERSATIONS c
               LEFT JOIN USER_DETAILS ud ON ud.user_id = c.user_id
               LEFT JOIN PROMPTS p ON p.id = COALESCE(c.role_id, ud.current_prompt_id)
               WHERE c.id = ?""",
            (conversation_id,),
        )
        conv_row = await cursor.fetchone()

    if not conv_row:
        yield f"data: {orjson.dumps({'error': 'Conversation not found'}).decode()}\n\n"
        return

    (
        is_locked,
        conv_llm_id,
        conv_user_id,
        chat_name,
        prompt_id,
        validation_active_extension_id,
        validation_last_message_id,
        enable_moderation,
        forced_llm_id,
        allowed_llms_raw,
        force_web_search,
        gransabio_enabled,
        conversation_incognito,
    ) = conv_row
    conversation_incognito = bool(conversation_incognito)

    # Verify user owns conversation
    if current_user.id != conv_user_id:
        yield f"data: {orjson.dumps({'error': 'Not authorized'}).decode()}\n\n"
        return

    # Block Multi-AI for WhatsApp conversations (server-side enforcement)
    try:
        if await is_whatsapp_conversation(conversation_id):
            yield f"data: {orjson.dumps({'error': 'Multi-AI is not available via WhatsApp'}).decode()}\n\n"
            return
    except Exception as exc:
        logger.warning(
            "[process_multi_ai_message] Could not verify WhatsApp status for conversation %s: %s",
            conversation_id,
            exc,
        )
        yield f"data: {orjson.dumps({'error': 'Could not verify conversation channel'}).decode()}\n\n"
        return

    # Verify conversation not locked
    if is_locked:
        yield f"data: {orjson.dumps({'error': 'Conversation is locked'}).decode()}\n\n"
        return

    # Deduplicate model_ids preserving order
    seen = set()
    unique_model_ids = []
    for mid in model_ids:
        if mid not in seen:
            seen.add(mid)
            unique_model_ids.append(mid)
    model_ids = unique_model_ids

    if len(model_ids) < 2 or len(model_ids) > 4:
        yield f"data: {orjson.dumps({'error': 'Multi-AI requires 2-4 unique model IDs'}).decode()}\n\n"
        return

    try:
        requested_reasoning = parse_reasoning_selection(reasoning_selection)
    except ReasoningValidationError as exc:
        yield f"data: {orjson.dumps({'error': str(exc), 'error_code': 'invalid_reasoning_selection'}).decode()}\n\n"
        return
    if requested_reasoning.mode == "custom":
        yield f"data: {orjson.dumps({'error': 'Multi-AI does not support custom reasoning budgets', 'error_code': 'invalid_reasoning_selection'}).decode()}\n\n"
        return

    # Reject Multi-AI if prompt has forced_llm_id
    if forced_llm_id:
        yield f"data: {orjson.dumps({'error': 'This prompt requires a specific model and cannot use Multi-AI'}).decode()}\n\n"
        return

    # Reject Multi-AI if prompt forces web search (Multi-AI disables all tools)
    if force_web_search:
        yield f"data: {orjson.dumps({'error': 'This prompt requires web search and cannot use Multi-AI'}).decode()}\n\n"
        return

    # Reject Multi-AI if prompt uses GranSabio pipeline (defense-in-depth)
    if bool(gransabio_enabled):
        yield f"data: {orjson.dumps({'error': 'This prompt uses GranSabio pipeline and cannot use Multi-AI comparison mode.'}).decode()}\n\n"
        return

    # Enforce allowed_llms strictly if set on prompt
    if allowed_llms_raw:
        try:
            parsed_allowed = orjson.loads(allowed_llms_raw)
            if not isinstance(parsed_allowed, list):
                raise ValueError("allowed_llms must be a JSON array")

            allowed_set = set()
            for allowed_id in parsed_allowed:
                if isinstance(allowed_id, int):
                    allowed_set.add(allowed_id)
                elif isinstance(allowed_id, str) and allowed_id.strip().isdigit():
                    allowed_set.add(int(allowed_id.strip()))
                else:
                    raise ValueError("allowed_llms contains non-integer values")
        except (orjson.JSONDecodeError, TypeError, ValueError):
            yield f"data: {orjson.dumps({'error': 'Prompt model restrictions are misconfigured'}).decode()}\n\n"
            return

        disallowed = [mid for mid in model_ids if mid not in allowed_set]
        if disallowed:
            yield f"data: {orjson.dumps({'error': f'Selected models are not allowed for this prompt: {disallowed}'}).decode()}\n\n"
            return

    # Verify each LLM exists
    llm_infos = {}
    for mid in model_ids:
        info = await get_llm_info(mid)
        if not info:
            yield f"data: {orjson.dumps({'error': f'Model ID {mid} not found'}).decode()}\n\n"
            return
        llm_infos[mid] = info

    reasoning_by_id = {}
    for mid, info in llm_infos.items():
        try:
            raw_capabilities = info.get("capabilities_json")
            try:
                capabilities = orjson.loads(raw_capabilities) if raw_capabilities else {}
            except (orjson.JSONDecodeError, TypeError):
                capabilities = {}
            if not isinstance(capabilities, dict):
                capabilities = {}
            if info.get("machine") == "GPTSub":
                account_capabilities = None
                try:
                    from subscription_auth.reasoning import user_model_reasoning_capabilities

                    account_capabilities = await user_model_reasoning_capabilities(
                        current_user.id,
                        info.get("model"),
                    )
                except Exception:
                    logger.warning(
                        "Could not resolve GPTSub reasoning capabilities for user_id=%s",
                        current_user.id,
                        exc_info=True,
                    )
                capabilities = {
                    **capabilities,
                    "reasoning": account_capabilities or {"behavior": "unknown"},
                }
            reasoning_by_id[mid] = resolve_and_validate(
                requested_reasoning,
                capabilities,
            )
        except ReasoningValidationError as exc:
            yield f"data: {orjson.dumps({'error': str(exc), 'error_code': 'invalid_reasoning_selection', 'llm_id': mid}).decode()}\n\n"
            return

    # --- 2. Load context (once) ---
    context_months = 2
    start_date = (
        datetime.now(timezone.utc) - timedelta(days=context_months * 30)
    ).strftime("%Y-%m-%d %H:%M:%S.%f")
    watchdog_config = None
    watchdog_hint_active = False
    watchdog_hint_eval_id = None
    multi_warmup_state = {
        "llm_id": conv_llm_id,
        "effective_prompt_id": prompt_id,
        "active_extension_id": validation_active_extension_id,
        "last_message_id": validation_last_message_id or 0,
    }
    multi_warmup_key = _build_warmup_cache_key_from_state(
        multi_warmup_state,
        current_user.id,
        conversation_id,
        mode="multi",
        multi_ai_model_ids=model_ids,
    )
    multi_warmup_snapshot = get_warmup_snapshot(multi_warmup_key)
    context_messages_dicts = _copy_warmup_context_messages(multi_warmup_snapshot)
    if context_messages_dicts is not None:
        mark_warmup_consumed()
        logger.debug(
            "[process_multi_ai_message] Reused warm-up context for conversation_id=%s",
            conversation_id,
        )

    async with get_db_connection(readonly=True) as conn_ro:
        # Load prompt / system prompt
        cursor = await conn_ro.execute(
            """SELECT p.prompt,
                      u.user_info,
                      ud.current_alter_ego_id,
                      COALESCE(p.extensions_enabled, 0) AS extensions_enabled,
                      COALESCE(p.extensions_auto_advance, 0) AS extensions_auto_advance,
                      c.active_extension_id,
                      pe.name AS extension_name,
                      pe.prompt_text AS extension_prompt_text,
                      p.watchdog_config
               FROM CONVERSATIONS c
               LEFT JOIN PROMPTS p ON p.id = ?
               LEFT JOIN USERS u ON u.id = c.user_id
               LEFT JOIN USER_DETAILS ud ON ud.user_id = c.user_id
               LEFT JOIN PROMPT_EXTENSIONS pe ON c.active_extension_id = pe.id
               WHERE c.id = ?""",
            (prompt_id, conversation_id),
        )
        prompt_row = await cursor.fetchone()
        if not prompt_row:
            yield f"data: {orjson.dumps({'error': 'Could not load prompt'}).decode()}\n\n"
            return

        (
            raw_prompt,
            user_info,
            current_alter_ego_id,
            extensions_enabled,
            extensions_auto_advance,
            active_extension_id,
            extension_name,
            extension_prompt_text,
            raw_watchdog_config,
        ) = prompt_row

        # Build system prompt
        prompt_base = raw_prompt or ""

        # Handle alter-ego
        if current_alter_ego_id:
            cursor = await conn_ro.execute(
                "SELECT name, description FROM USER_ALTER_EGOS WHERE id = ? AND user_id = ?",
                (current_alter_ego_id, current_user.id),
            )
            alter_ego_row = await cursor.fetchone()
            if alter_ego_row:
                ae_name, ae_desc = alter_ego_row
                if ae_desc:
                    prompt_base = f"User info:\nName: {ae_name}\n{ae_desc}\n\n-----\nSystem info:\n{prompt_base}"
                else:
                    prompt_base = f"User info:\nName: {ae_name}\n\n-----\nSystem info:\n{prompt_base}"
            elif user_info:
                prompt_base = f"User info:\n{user_info}\n\n-----\nSystem info:\n{prompt_base}"
        elif user_info:
            prompt_base = f"User info:\n{user_info}\n\n-----\nSystem info:\n{prompt_base}"

        # Extensions: inject current extension and level context (same behavior as single-model flow).
        if extensions_enabled and extension_prompt_text:
            prompt_base = (
                f"{prompt_base}\n\n"
                f"--- ACTIVE EXTENSION: {extension_name} ---\n"
                f"{extension_prompt_text}\n"
                f"--- END EXTENSION ---"
            )

        if extensions_enabled and extensions_auto_advance and prompt_id:
            cursor = await conn_ro.execute(
                """SELECT id, name, display_order, description
                   FROM PROMPT_EXTENSIONS
                   WHERE prompt_id = ?
                   ORDER BY display_order""",
                (prompt_id,),
            )
            all_extensions = await cursor.fetchall()
            if all_extensions:
                ext_list = "\n".join([
                    f"  - [{e[0]}] {e[1]}{' (CURRENT)' if e[0] == active_extension_id else ''}: {e[3] or 'No description'}"
                    for e in all_extensions
                ])
                extensions_context = (
                    f"\n\n--- EXTENSION LEVELS ---\n"
                    f"This conversation has the following levels/phases. You are currently on the one marked (CURRENT).\n"
                    f"Multi-AI compare mode has tool-calling disabled, so do not attempt to call advanceExtension.\n"
                    f"Keep responses aligned with the CURRENT level objectives.\n"
                    f"{ext_list}\n"
                    f"--- END EXTENSION LEVELS ---"
                )
                prompt_base += extensions_context

        prompt_base = merge_internal_turn_context(
            prompt_base,
            render_current_input_context(channel_context),
        )

        # Watchdog: reuse prompt-hint injection in Multi-AI so behavior matches single flow.
        watchdog_hint_block = ""
        if raw_watchdog_config:
            try:
                parsed_watchdog = orjson.loads(raw_watchdog_config)
                watchdog_config = extract_post_watchdog_config(parsed_watchdog)
            except orjson.JSONDecodeError:
                watchdog_config = None

        watchdog_enabled = bool(watchdog_config and watchdog_config.get("enabled"))
        if watchdog_enabled and prompt_id:
            cursor = await conn_ro.execute(
                """SELECT pending_hint, hint_severity, last_evaluated_message_id, consecutive_hint_count
                   FROM WATCHDOG_STATE
                   WHERE conversation_id = ? AND prompt_id = ?
                   AND pending_hint IS NOT NULL""",
                (conversation_id, prompt_id),
            )
            hint_row = await cursor.fetchone()
            if hint_row and hint_row[0]:
                sanitized_hint = _sanitize_watchdog_directive(hint_row[0])
                hint_severity = hint_row[1]
                consecutive_count = hint_row[3] or 0
                watchdog_hint_block = _build_escalated_hint_block(
                    sanitized_hint, hint_severity, consecutive_count
                )
                watchdog_hint_active = True
                watchdog_hint_eval_id = hint_row[2]

        # Determine user privilege level for system prompt blocks
        if await current_user.is_admin:
            user_level = "admin"
        elif await current_user.is_user:
            user_level = "user"
        else:
            user_level = "customer"
        # Assemble system_prompt via global system prompt blocks
        blocks = await get_effective_blocks()
        variables = {
            "user_level": user_level,
            "current_datetime_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        }
        system_prompt = assemble_system_prompt(blocks, variables, prompt_base,
                                              watchdog_enabled, watchdog_hint_block)

        # Load context messages unless the warm-up cache already prepared them.
        if context_messages_dicts is None:
            cursor = await conn_ro.execute(
                """SELECT id, message, type FROM messages
                   WHERE conversation_id = ? AND date >= ?
                   ORDER BY id ASC, date ASC""",
                (conversation_id, start_date),
            )
            context_rows = await cursor.fetchall()
            context_messages_dicts = [
                {
                    "id": int(row[0]),
                    "message": parse_stored_message(custom_unescape(row[1])),
                    "type": row[2],
                }
                for row in context_rows
            ]
            context_messages_dicts = flatten_multi_ai_context(context_messages_dicts)

    # --- 3. Moderation (once) ---
    if enable_moderation:
        try:
            moderation_input = [{"type": "text", "text": user_message}]
            response = openai.moderations.create(
                model="omni-moderation-latest",
                input=moderation_input,
            )
            for result in response.results:
                if result.flagged:
                    yield f"data: {orjson.dumps({'error': 'Message blocked by moderation'}).decode()}\n\n"
                    return
        except Exception as exc:
            logger.error("[process_multi_ai_message] Moderation error: %s", exc)
            yield f"data: {orjson.dumps({'error': 'Moderation check failed'}).decode()}\n\n"
            return

    memory_decision = await _resolve_memory_context(
        system_prompt,
        user_id=current_user.id,
        conversation_id=conversation_id,
        message=user_message,
        prompt_id=prompt_id,
        incognito=conversation_incognito,
    )
    system_prompt = memory_decision.full_prompt
    context_messages_dicts = _context_messages_for_memory_provider(
        context_messages_dicts,
        memory_decision,
    )
    memory_health = get_user_memory_health_snapshot(
        memory_decision.provider,
        enabled=(
            memory_decision.provider != "none"
            and memory_decision.reason != "disabled_by_user"
        ),
    )
    if should_surface_memory_health(memory_health):
        yield f"data: {orjson.dumps({'type': 'memory_health', 'memory_health': memory_health}).decode()}\n\n"
    apply_no_memory_limit = memory_decision.provider == "none"
    billing_system_prompt, billing_context_messages = (
        await prepare_trusted_history_context(
            system_prompt,
            context_messages_dicts,
        )
    )
    context_pdf_error_metadata = _extract_pdf_metadata_from_context_messages(context_messages_dicts)
    context_pdf_pages = int((context_pdf_error_metadata or {}).get("pages") or 0)
    context_pdf_count = int((context_pdf_error_metadata or {}).get("pdf_count") or 0)
    if context_pdf_error_metadata:
        context_pdf_error_metadata["current_pdf_count"] = 0
        context_pdf_error_metadata["context_pdf_count"] = context_pdf_count
        context_pdf_error_metadata["range_retry_available"] = False
    if context_pdf_pages > MAX_PDF_PAGES:
        payload = _pdf_upload_too_large_payload(
            f'PDF page total exceeds {MAX_PDF_PAGES} page limit ({context_pdf_pages} pages in conversation context)',
            current_pdf_count=0,
            current_pages=0,
            context_pdf_count=context_pdf_count,
            context_pages=context_pdf_pages,
        )
        yield f"data: {orjson.dumps(payload).decode()}\n\n"
        return

    # --- 4. Chat name generation (once) ---
    updated_chat_name = None
    if chat_name is None:
        message_text = re.sub(r"<[^>]+>", "", user_message)[:25]
        updated_chat_name = message_text
        async with conversation_write_lock(conversation_id):
            async with get_db_connection() as conn_rw:
                try:
                    await conn_rw.execute("BEGIN IMMEDIATE")
                    await conn_rw.execute(
                        "UPDATE conversations SET chat_name = ? WHERE id = ?",
                        (updated_chat_name, conversation_id),
                    )
                    await conn_rw.commit()
                except Exception as exc:
                    try:
                        await conn_rw.rollback()
                    except Exception:
                        pass
                    logger.warning("[process_multi_ai_message] Could not update chat_name: %s", exc)

    if updated_chat_name:
        yield f"data: {orjson.dumps({'updated_chat_name': updated_chat_name}).decode()}\n\n"

    # --- 5. BYOK resolution (per model) ---
    from common import resolve_api_key_for_provider, get_user_api_key_mode
    api_key_mode = await get_user_api_key_mode(current_user.id)

    resolved_keys = {}
    excluded_models = []
    for mid in model_ids:
        info = llm_infos[mid]
        # GPTSub (ChatGPT subscription) is interactive-single-chat only. Its
        # dispatch/credential/kill-switch paths live in messages.py, not here; a
        # GPTSub id in multi-AI would crash the dispatch (else: raise ValueError),
        # never receive its token, and bypass the kill-switch. Exclude it explicitly
        # before the resolver, which would otherwise return (None, True) in
        # system/both modes and let GPTSub slip through to the crashing dispatch.
        if info["machine"] == "GPTSub":
            excluded_models.append(mid)
            logger.info(
                "[process_multi_ai_message] Excluding GPTSub model %s from multi-AI "
                "(subscription models are single-chat only)",
                mid,
            )
            yield f"data: {orjson.dumps({'multi_ai_error': True, 'llm_id': mid, 'model': info['model'], 'error': 'ChatGPT subscription models are not available in multi-AI mode.'}).decode()}\n\n"
            continue
        provider_for_key = (
            "OpenRouter"
            if context_pdf_pages > 0 and info["machine"] in ("GPT", "xAI")
            else info["machine"]
        )
        resolved_key, use_system = resolve_api_key_for_provider(
            user_api_keys or {}, api_key_mode, provider_for_key
        )
        if (
            provider_for_key == "OpenRouter"
            and not resolved_key
            and use_system
            and not openrouter_key
        ):
            excluded_models.append(mid)
            yield f"data: {orjson.dumps({'multi_ai_error': True, 'llm_id': mid, 'model': info['model'], 'error': 'PDF files with this model require OpenRouter integration.'}).decode()}\n\n"
            continue
        if resolved_key:
            resolved_keys[mid] = resolved_key
        elif use_system:
            resolved_keys[mid] = None  # Will use system key
        else:
            # own_only mode without key for this provider
            excluded_models.append(mid)
            yield f"data: {orjson.dumps({'multi_ai_error': True, 'llm_id': mid, 'model': info['model'], 'error': f'API key required for {provider_for_key}'}).decode()}\n\n"

    # Remove excluded models
    model_ids = [mid for mid in model_ids if mid not in excluded_models]
    if len(model_ids) < 2:
        yield f"data: {orjson.dumps({'error': 'Not enough models with available API keys (minimum 2)'}).decode()}\n\n"
        return

    # --- 6. Balance check ---
    # Determine which models are BYOK (user's own API key)
    byok_models = {mid for mid in model_ids if resolved_keys.get(mid) is not None}

    billing_availability = await get_user_billing_availability(current_user.id)
    current_balance = billing_availability["available"]
    billing_preflight_amount = 0.0
    model_output_caps = {}
    model_output_fallbacks = {}
    for mid in model_ids:
        cap, fallback_used = _model_output_cap(llm_infos[mid].get("max_output_tokens"))
        model_output_caps[mid] = cap
        model_output_fallbacks[mid] = fallback_used
    shared_model_output_cap = min(model_output_caps.values()) if model_output_caps else int(MAX_TOKENS)

    # Estimate max_tokens based on the SUM of costs across all selected models.
    # This is conservative and prevents partial billing failures at commit time.
    input_tokens_est_base = max(
        estimate_message_tokens(user_message),
        estimate_structured_billing_tokens(
            billing_system_prompt,
            billing_context_messages,
            user_message,
        ),
    )
    input_tokens_est_by_model = {
        mid: input_tokens_est_base + _estimate_pdf_input_tokens_for_preflight(
            context_pdf_pages,
            llm_infos[mid].get("machine"),
        )
        for mid in model_ids
    }
    input_usage_fallback_base = estimate_structured_usage_tokens(
        billing_system_prompt,
        billing_context_messages,
        user_message,
    )
    input_usage_fallback_by_model = {
        mid: input_usage_fallback_base + _estimate_pdf_input_tokens_for_preflight(
            context_pdf_pages,
            llm_infos[mid].get("machine"),
        )
        for mid in model_ids
    }

    async with get_db_connection(readonly=True) as conn_ro:
        placeholders = ",".join("?" for _ in model_ids)
        cursor = await conn_ro.execute(
            f"SELECT id, input_token_cost, output_token_cost FROM LLM WHERE id IN ({placeholders})",
            tuple(model_ids),
        )
        cost_rows = await cursor.fetchall()

    costs_by_id = {
        int(row[0]): (float(row[1] or 0.0), float(row[2] or 0.0))
        for row in cost_rows
    }

    missing_cost_ids = [mid for mid in model_ids if mid not in costs_by_id]
    if missing_cost_ids:
        yield f"data: {orjson.dumps({'error': f'Cost configuration missing for models: {missing_cost_ids}'}).decode()}\n\n"
        return

    rates_by_id = {}
    for mid in model_ids:
        input_cost_million, output_cost_million = costs_by_id[mid]
        if output_cost_million < 0:
            model_name = llm_infos[mid]["model"]
            yield f"data: {orjson.dumps({'error': f'Invalid output token cost for model: {model_name}'}).decode()}\n\n"
            return
        if input_cost_million < 0:
            model_name = llm_infos[mid]["model"]
            yield f"data: {orjson.dumps({'error': f'Invalid input token cost for model: {model_name}'}).decode()}\n\n"
            return
        info = llm_infos[mid]
        guard_error = assert_billable_claude_system_key(
            machine=info.get("machine"),
            model=info.get("model"),
            llm_id=mid,
            is_byok=mid in byok_models,
            input_token_cost=input_cost_million,
            output_token_cost=output_cost_million,
        )
        if guard_error:
            logger.error(guard_error)
            yield f"data: {orjson.dumps({'error': guard_error}).decode()}\n\n"
            return

        rates_by_id[mid] = await get_variable_billing_rates(
            user_id=current_user.id,
            prompt_id=prompt_id,
            input_cost_per_million=input_cost_million,
            output_cost_per_million=output_cost_million,
            byok=mid in byok_models,
        )

    estimated_input_charge = 0.0
    combined_output_rate = 0.0
    for mid in model_ids:
        rates = rates_by_id[mid]
        if llm_infos[mid].get("machine") == "Claude":
            estimated_input_charge += (
                input_tokens_est_by_model[mid] * 4 * rates.input_per_token
            )
            combined_output_rate += (
                6 * rates.input_per_token + 4 * rates.output_per_token
            )
        else:
            estimated_input_charge += (
                input_tokens_est_by_model[mid] * rates.input_per_token
            )
            combined_output_rate += rates.output_per_token

    available_for_visible_output = current_balance - estimated_input_charge
    if available_for_visible_output < -1e-12:
        yield f"data: {orjson.dumps({'error': 'Insufficient balance'}).decode()}\n\n"
        return

    if combined_output_rate > 0:
        max_affordable_tokens = math.floor(
            max(0.0, available_for_visible_output) / combined_output_rate
        )
        max_tokens = int(min(shared_model_output_cap, max_affordable_tokens))
        balance_limited = max_affordable_tokens < shared_model_output_cap
    else:
        max_tokens = int(shared_model_output_cap)
        balance_limited = False

    if max_tokens < 1:
        yield f"data: {orjson.dumps({'error': 'Insufficient balance'}).decode()}\n\n"
        return
    for mid in model_ids:
        selection = reasoning_by_id[mid]
        if (
            llm_infos[mid].get("machine") == "Claude"
            and selection.mode == "custom"
            and selection.budget_tokens >= max_tokens
        ):
            yield f"data: {orjson.dumps({'error': 'Claude reasoning budget must be lower than the available output-token limit', 'error_code': 'invalid_reasoning_selection', 'llm_id': mid}).decode()}\n\n"
            return
    billing_preflight_amount = (
        estimated_input_charge
        + max_tokens * combined_output_rate
    )

    logger.info(
        "[process_multi_ai_message] Cost pre-check passed: models=%s, byok_models=%s, "
        "model_output_caps=%s, fallback_ids=%s, max_tokens=%d, balance_limited=%s, balance=%.6f",
        model_ids,
        list(byok_models),
        model_output_caps,
        [mid for mid, fallback_used in model_output_fallbacks.items() if fallback_used],
        max_tokens,
        balance_limited,
        current_balance,
    )

    # --- 7. Parallel execution ---
    if not await revalidate_user_billing(
        current_user.id,
        billing_preflight_amount,
    ):
        yield f"data: {orjson.dumps({'error': 'Insufficient balance'}).decode()}\n\n"
        return

    try:
        ai_reservation_id = await reserve_ai_usage(
            user_id=current_user.id,
            maximum_amount=billing_preflight_amount,
        )
    except InsufficientBalanceError:
        yield f"data: {orjson.dumps({'error': 'Insufficient balance'}).decode()}\n\n"
        return
    except BillingReservationError:
        logger.exception("Could not reserve Multi-AI usage")
        yield f"data: {orjson.dumps({'error': 'AI billing is temporarily unavailable'}).decode()}\n\n"
        return

    stop_signals[conversation_id] = False

    queue = asyncio.Queue()
    tasks = {}
    results = {}

    async def _persist_completed_component(mid: int) -> None:
        result = results[mid]
        if (
            not ai_reservation_id
            or not result.get("billable_usage")
            or result.get("component_persisted")
        ):
            return
        input_cost, output_cost = costs_by_id[mid]
        input_tokens = int(result.get("input_tokens") or 0)
        output_tokens = int(result.get("output_tokens") or 0)
        if input_tokens <= 0:
            input_tokens = input_usage_fallback_by_model[mid]
        if output_tokens <= 0:
            output_tokens = estimate_message_tokens(result.get("content", ""))
        try:
            await accumulate_ai_reservation_usage(
                reservation_id=ai_reservation_id,
                user_id=current_user.id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                component={
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "input_cost_per_million": input_cost,
                    "output_cost_per_million": output_cost,
                    "prompt_id": prompt_id,
                    "byok": mid in byok_models,
                },
            )
            result["component_persisted"] = True
        except BillingReservationError:
            logger.exception(
                "Could not persist completed Multi-AI component %s",
                mid,
            )

    for mid in model_ids:
        info = llm_infos[mid]
        messages_copy = [msg.copy() for msg in context_messages_dicts]

        task = asyncio.create_task(
            _run_single_ai(
                queue=queue,
                llm_id=mid,
                llm_info=info,
                context_messages=messages_copy,
                user_message=user_message,
                system_prompt=system_prompt,
                conversation_id=conversation_id,
                current_user=current_user,
                request=request,
                max_tokens=max_tokens,
                reasoning_selection=reasoning_by_id[mid],
                user_api_key=resolved_keys.get(mid),
                prompt_id=prompt_id,
                temperature=0.7,
                input_token_fallback=input_usage_fallback_by_model.get(
                    mid,
                    input_usage_fallback_base,
                ),
                pdf_error_metadata=context_pdf_error_metadata,
                apply_no_memory_limit=apply_no_memory_limit,
            )
        )
        tasks[mid] = task
        results[mid] = {
            "content": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "error": False,
            "model": info["model"],
            "machine": info["machine"],
            "completed": False,
            "billable_usage": False,
            "component_persisted": False,
        }

    done_count = 0
    total = len(model_ids)

    try:
        while done_count < total:
            item = await queue.get()
            item_llm_id = item["llm_id"]

            if item["type"] == "chunk":
                results[item_llm_id]["content"] += item["content"]
                results[item_llm_id]["billable_usage"] = True
                yield f"data: {orjson.dumps({'multi_ai': True, 'llm_id': item_llm_id, 'model': item['model'], 'content': item['content']}).decode()}\n\n"

            elif item["type"] == "done":
                results[item_llm_id]["input_tokens"] = item.get("input_tokens", 0)
                results[item_llm_id]["output_tokens"] = item.get("output_tokens", 0)
                results[item_llm_id]["completed"] = True
                results[item_llm_id]["billable_usage"] = bool(
                    item.get("billable_usage", True)
                )
                await _persist_completed_component(item_llm_id)
                done_count += 1
                yield f"data: {orjson.dumps({'multi_ai_done': True, 'llm_id': item_llm_id, 'model': item['model']}).decode()}\n\n"

            elif item["type"] == "error":
                if item.get("error_code") == "pdf_too_large" or item.get("pdf_too_large") is True:
                    stop_signals[conversation_id] = True
                    for task in tasks.values():
                        if not task.done():
                            task.cancel()
                    pdf_payload = {
                        key: item[key]
                        for key in (
                            "error",
                            "error_code",
                            "pdf_too_large",
                            "provider",
                            "provider_message",
                            "provider_health",
                            "provider_status",
                            "provider_health_message",
                            "filename",
                            "pages",
                            "pdf_count",
                            "current_pdf_count",
                            "context_pdf_count",
                            "range_retry_available",
                            "retry_filename",
                            "retry_pages",
                            "retry_token",
                        )
                        if key in item
                    }
                    yield f"data: {orjson.dumps(pdf_payload).decode()}\n\n"
                    return
                if not results[item_llm_id]["content"]:
                    results[item_llm_id]["content"] = item.get("error", "Unknown error")
                results[item_llm_id]["input_tokens"] = item.get("input_tokens", 0)
                results[item_llm_id]["output_tokens"] = item.get("output_tokens", 0)
                results[item_llm_id]["billable_usage"] = bool(
                    item.get("billable_usage", False)
                )
                results[item_llm_id]["error"] = True
                await _persist_completed_component(item_llm_id)
                done_count += 1
                error_payload = {
                    "multi_ai_error": True,
                    "llm_id": item_llm_id,
                    "model": item["model"],
                    "error": item["error"],
                }
                for key in ("provider_health", "provider", "provider_status", "provider_health_message"):
                    if key in item:
                        error_payload[key] = item[key]
                yield f"data: {orjson.dumps(error_payload).decode()}\n\n"

    except (asyncio.CancelledError, Exception):
        stop_signals[conversation_id] = True
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        raise
    else:
        # --- 8. Save combined result ---
        combined_message = build_multi_ai_message(results, model_ids)
        total_input = sum(r["input_tokens"] for r in results.values())
        total_output = sum(r["output_tokens"] for r in results.values())

        try:
            user_msg_id, bot_msg_id = await save_multi_ai_to_db(
                combined_message, results, model_ids,
                total_input, total_output,
                conversation_id, current_user.id, user_message,
                prompt_id=prompt_id,
                watchdog_config=watchdog_config,
                watchdog_hint_active=watchdog_hint_active,
                watchdog_hint_eval_id=watchdog_hint_eval_id,
                byok_models=byok_models,
                incognito=conversation_incognito,
                billing_reservation_id=ai_reservation_id,
                channel_context=channel_context,
            )

            if user_msg_id and bot_msg_id:
                yield f"data: {orjson.dumps({'message_ids': {'user': user_msg_id, 'bot': bot_msg_id}}).decode()}\n\n"
            else:
                yield f"data: {orjson.dumps(persistence_error_payload()).decode()}\n\n"
                return
        except StaleChannelTurnError:
            terminal = await _recover_stale_multi_ai_turn(
                channel_context=channel_context,
                conversation_id=conversation_id,
                user_id=current_user.id,
                user_message=user_message,
                prompt_id=prompt_id,
                llm_id=conv_llm_id,
            )
            yield f"data: {orjson.dumps(terminal).decode()}\n\n"
            return
        except MultiAiBillingError as exc:
            logger.warning("[process_multi_ai_message] Multi-AI billing failed: %s", exc)
            yield f"data: {orjson.dumps(persistence_error_payload()).decode()}\n\n"
            return
        except Exception as exc:
            logger.error("[process_multi_ai_message] Failed to save to DB: %s", exc, exc_info=True)
            yield f"data: {orjson.dumps(persistence_error_payload()).decode()}\n\n"
            return

        yield "data: [DONE]\n\n"
    finally:
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        while True:
            try:
                pending_item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            pending_llm_id = pending_item.get("llm_id")
            if pending_item.get("type") == "done" and pending_llm_id in results:
                results[pending_llm_id]["input_tokens"] = pending_item.get(
                    "input_tokens", 0
                )
                results[pending_llm_id]["output_tokens"] = pending_item.get(
                    "output_tokens", 0
                )
                results[pending_llm_id]["completed"] = True
                results[pending_llm_id]["billable_usage"] = bool(
                    pending_item.get("billable_usage", True)
                )
            elif pending_item.get("type") == "error" and pending_llm_id in results:
                results[pending_llm_id]["input_tokens"] = pending_item.get(
                    "input_tokens", 0
                )
                results[pending_llm_id]["output_tokens"] = pending_item.get(
                    "output_tokens", 0
                )
                results[pending_llm_id]["billable_usage"] = bool(
                    pending_item.get("billable_usage", False)
                )
                results[pending_llm_id]["error"] = True
        try:
            if ai_reservation_id:
                for mid in model_ids:
                    await _persist_completed_component(mid)
                components = []
                for mid in model_ids:
                    result = results[mid]
                    if not (
                        result.get("completed")
                        or result.get("billable_usage")
                    ):
                        continue
                    input_cost, output_cost = costs_by_id[mid]
                    reported_input = int(result.get("input_tokens") or 0)
                    reported_output = int(result.get("output_tokens") or 0)
                    components.append(
                        {
                            "input_tokens": (
                                reported_input
                                if reported_input > 0
                                else input_usage_fallback_by_model[mid]
                            ),
                            "output_tokens": (
                                reported_output
                                if reported_output > 0
                                else estimate_message_tokens(result.get("content", ""))
                            ),
                            "input_cost_per_million": input_cost,
                            "output_cost_per_million": output_cost,
                            "byok": mid in byok_models,
                        }
                    )
                await settle_ai_reservation_components(
                    reservation_id=ai_reservation_id,
                    user_id=current_user.id,
                    prompt_id=prompt_id,
                    components=components,
                )
        except BillingReservationError:
            logger.exception(
                "Could not capture unfinished Multi-AI usage %s",
                ai_reservation_id,
            )
        try:
            if ai_reservation_id:
                await refund_fixed_usage(ai_reservation_id)
        except BillingReservationError:
            logger.exception(
                "Could not release unfinished Multi-AI reservation %s",
                ai_reservation_id,
            )
