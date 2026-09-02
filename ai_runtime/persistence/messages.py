import inspect
import math
import re
from typing import Awaitable, Callable

from ai_runtime.channel_turns import (
    AudibleConfirmation,
    ChannelCommit,
    ChannelContext,
    StaleChannelTurnError,
    assert_commit_guard_in_transaction,
    current_channel_turn,
)
from ai_runtime.dependencies import *
from ai_runtime.memory.recording import _record_memory_turn_best_effort
from ai_runtime.multi_ai.errors import MultiAiBillingError
from ai_runtime.reasoning_tags import strip_tagged_thinking_prefix
from billing.usage_reservations import (
    BillingReservationError,
    complete_ai_reservation_settlement,
    prepare_ai_reservation_settlement,
    settle_fixed_usage_in_transaction,
)

_get_post_watchdog_config = extract_post_watchdog_config

AURVEK_ACTION_BLOCK_RE = re.compile(
    r"(?:\[AURVEK_ACTIONS\].*?\[/AURVEK_ACTIONS\]|```aurvek-actions\s*.*?```)",
    re.IGNORECASE | re.DOTALL,
)

PERSISTENCE_ERROR_MESSAGE = (
    "The response was generated but could not be saved. Copy it before retrying."
)


def persistence_error_payload() -> dict:
    """Build the SSE payload used when generation succeeded but persistence failed."""
    return {
        "error": PERSISTENCE_ERROR_MESSAGE,
        "persistence_error": True,
    }


def strip_aurvek_action_blocks(content: str | None) -> str:
    clean = AURVEK_ACTION_BLOCK_RE.sub("", str(content or "")).strip()
    return clean or "[Structured device action returned]"


def assistant_content_for_storage(content, *, strip_device_action_blocks: bool = False):
    content = strip_tagged_thinking_prefix(content)
    if strip_device_action_blocks:
        return strip_aurvek_action_blocks(content)
    return content


def _combined_reservation_api_cost(
    *,
    accumulated_api_cost: float | None,
    current_input_tokens: int,
    current_output_tokens: int,
    input_cost_per_million: float,
    output_cost_per_million: float,
    current_byok: bool,
    current_override_api_cost: float | None,
) -> float | None:
    """Combine heterogeneous prior provider cost with the current model call.

    ``None`` preserves the legacy behavior where accumulated tokens are priced
    using the final model's rates.  When component costs exist, returning one
    raw API-cost override lets ``consume_token`` apply the marketplace margin
    once while retaining token-based creator/referral markups.
    """
    if accumulated_api_cost is None:
        return current_override_api_cost
    try:
        accumulated = float(accumulated_api_cost)
        input_rate = float(input_cost_per_million or 0.0)
        output_rate = float(output_cost_per_million or 0.0)
        current_override = (
            None
            if current_override_api_cost is None
            else float(current_override_api_cost)
        )
    except (TypeError, ValueError) as exc:
        raise BillingReservationError("AI provider cost is invalid") from exc
    values = [accumulated, input_rate, output_rate]
    if current_override is not None:
        values.append(current_override)
    if min(values) < 0 or not all(math.isfinite(value) for value in values):
        raise BillingReservationError("AI provider cost is invalid")

    if current_byok:
        current_cost = 0.0
    elif current_override is not None:
        current_cost = current_override
    else:
        current_cost = (
            max(0, int(current_input_tokens)) * input_rate
            + max(0, int(current_output_tokens)) * output_rate
        ) / 1_000_000
    combined = accumulated + current_cost
    if not math.isfinite(combined):
        raise BillingReservationError("AI provider cost is invalid")
    return combined


async def _save_content_to_db_immediate(
    content, input_tokens, output_tokens, total_tokens, conversation_id, user_id, model,
    user_message=None, input_token_fallback=None, prompt_id=None,
    watchdog_config=None, watchdog_hint_active=False, watchdog_hint_eval_id=None,
    llm_id=None, citations_json=None, byok=False, override_api_cost=None,
    pending_attachment_refs: Optional[list[str]] = None,
    strip_device_action_blocks: bool = False,
    billing_reservation_id: str | None = None,
    billing_only_accumulated_usage: bool = False,
    fixed_billing_reservation_id: str | None = None,
    save_assistant_message: bool = True,
    persistence_only: bool = False,
    skip_billing: bool = False,
    record_user_only_memory: bool = False,
    load_watchdog_config: bool = False,
    channel_context: ChannelContext | None = None,
    channel_confirmed_text: str | None = None,
    channel_played_ms: int | None = None,
    channel_commit_persistence_only: bool | None = None,
    on_database_commit: Callable[
        [tuple[int | None, int | None]], None | Awaitable[None]
    ] | None = None,
):
    # logger.info(f"Complete AI message:\n {content}")  # Commented to avoid encoding issues with emojis
    logger.info(f"Tokens usados:\ninput_tokens: {input_tokens}\noutput_tokens: {output_tokens}\ntotal_tokens: {total_tokens}")

    last_lock_error = None
    conversation_incognito = False
    try:
        from chat.services.privacy import is_incognito_conversation

        conversation_incognito = await is_incognito_conversation(
            int(conversation_id),
            user_id=int(user_id),
        )
    except Exception:
        logger.warning(
            "[atagia] Could not resolve conversation privacy for conversation_id=%s",
            conversation_id,
            exc_info=True,
        )

    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        wait_time = 0.0
        async with conversation_write_lock(conversation_id):
            async with get_db_connection() as conn:
                conn.row_factory = aiosqlite.Row
                transaction_started = False
                try:
                    await conn.execute("BEGIN IMMEDIATE")
                    transaction_started = True
                    if channel_context is not None:
                        await assert_commit_guard_in_transaction(
                            channel_context, conn
                        )
                    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
                    ai_credit = None
                    billable_input_tokens = 0
                    billable_output_tokens = 0
                    if not persistence_only and not skip_billing:
                        reported_input_tokens = int(input_tokens or 0)
                        fallback_user_input_tokens = estimate_message_tokens(user_message) if user_message else 0
                        try:
                            fallback_estimated_input_tokens = int(input_token_fallback or 0)
                        except (TypeError, ValueError):
                            fallback_estimated_input_tokens = 0
                        fallback_input_tokens = max(
                            fallback_user_input_tokens,
                            fallback_estimated_input_tokens,
                        )
                        # Providers generally report prompt tokens including the user message.
                        # Use reported tokens when available; only fallback when missing/zero.
                        billable_input_tokens = 0 if billing_only_accumulated_usage else (
                            reported_input_tokens
                            if reported_input_tokens > 0
                            else fallback_input_tokens
                        )
                        reported_output_tokens = int(output_tokens or 0)
                        billable_output_tokens = 0 if billing_only_accumulated_usage else (
                            reported_output_tokens
                            if reported_output_tokens > 0
                            else estimate_message_tokens(content)
                        )
                        current_billable_input_tokens = billable_input_tokens
                        current_billable_output_tokens = billable_output_tokens
                        ai_credit = await prepare_ai_reservation_settlement(
                            conn,
                            reservation_id=billing_reservation_id,
                            user_id=user_id,
                        )
                        if ai_credit is not None:
                            billable_input_tokens += ai_credit.accumulated_input_tokens
                            billable_output_tokens += ai_credit.accumulated_output_tokens
                    stored_assistant_content = (
                        assistant_content_for_storage(
                            content,
                            strip_device_action_blocks=strip_device_action_blocks,
                        )
                        if save_assistant_message
                        else None
                    )

                    user_message_id = None
                    if user_message is not None:
                        user_insert_query = '''
                            INSERT INTO messages (conversation_id, user_id, message, type, date)
                            VALUES (?, ?, ?, ?, ?)
                            RETURNING id
                        '''
                        cursor = await conn.execute(
                            user_insert_query,
                            (conversation_id, user_id, user_message, 'user', current_time)
                        )
                        user_row = await cursor.fetchone()
                        user_message_id = user_row[0] if user_row else None
                        if user_message_id is not None and pending_attachment_refs:
                            await finalize_message_attachments(
                                conn,
                                message_id=user_message_id,
                                conversation_id=conversation_id,
                                user_id=user_id,
                                message_json=user_message,
                            )

                    message_id = None
                    if save_assistant_message:
                        bot_insert_query = '''
                            INSERT INTO messages
                            (conversation_id, user_id, message, type, input_tokens_used, output_tokens_used, date, llm_id, citations_json)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            RETURNING id
                        '''
                        cursor = await conn.execute(
                            bot_insert_query,
                            (conversation_id, user_id, stored_assistant_content, 'bot', billable_input_tokens, billable_output_tokens, current_time, llm_id, citations_json)
                        )
                        row = await cursor.fetchone()
                        message_id = row[0] if row else None

                    if not persistence_only and not skip_billing:
                        try:
                            normalized_llm_id = int(llm_id) if llm_id is not None and int(llm_id) > 0 else None
                        except (TypeError, ValueError):
                            normalized_llm_id = None
                        if normalized_llm_id is not None:
                            cost_query = 'SELECT input_token_cost, output_token_cost FROM LLM WHERE id = ?'
                            cursor = await conn.execute(cost_query, (normalized_llm_id,))
                        else:
                            cost_query = 'SELECT input_token_cost, output_token_cost FROM LLM WHERE model = ?'
                            cursor = await conn.execute(cost_query, (model,))
                        token_cost_row = await cursor.fetchone()
                        if token_cost_row:
                            input_token_cost_per_million, output_token_cost_per_million = token_cost_row
                        else:
                            input_token_cost_per_million, output_token_cost_per_million = 0, 0

                        # Get prompt_id from conversation (role_id in CONVERSATIONS is the prompt_id)
                        if prompt_id is None:
                            prompt_query = 'SELECT role_id FROM CONVERSATIONS WHERE id = ?'
                            cursor = await conn.execute(prompt_query, (conversation_id,))
                            prompt_row = await cursor.fetchone()
                            prompt_id = prompt_row[0] if prompt_row else None

                        effective_override_api_cost = override_api_cost
                        if ai_credit is not None:
                            effective_override_api_cost = (
                                _combined_reservation_api_cost(
                                    accumulated_api_cost=(
                                        ai_credit.accumulated_api_cost
                                    ),
                                    current_input_tokens=(
                                        current_billable_input_tokens
                                    ),
                                    current_output_tokens=(
                                        current_billable_output_tokens
                                    ),
                                    input_cost_per_million=(
                                        input_token_cost_per_million
                                    ),
                                    output_cost_per_million=(
                                        output_token_cost_per_million
                                    ),
                                    current_byok=bool(byok),
                                    current_override_api_cost=(
                                        None
                                        if billing_only_accumulated_usage
                                        else override_api_cost
                                    ),
                                )
                            )

                        billing_ok = await consume_token(
                            user_id,
                            billable_input_tokens,
                            billable_output_tokens,
                            input_token_cost_per_million,
                            output_token_cost_per_million,
                            conn,
                            cursor,
                            prompt_id=prompt_id,
                            byok=byok,
                            override_api_cost=effective_override_api_cost,
                            billing_account_id_override=(
                                ai_credit.billing_account_id if ai_credit else None
                            ),
                        )
                        if not billing_ok:
                            await conn.rollback()
                            await discard_pending_attachments(pending_attachment_refs, "billing_failed")
                            return (None, None)
                        await complete_ai_reservation_settlement(conn, ai_credit)
                        if fixed_billing_reservation_id:
                            fixed_settled = await settle_fixed_usage_in_transaction(
                                conn,
                                fixed_billing_reservation_id,
                                expected_user_id=user_id,
                            )
                            if not fixed_settled:
                                raise BillingReservationError(
                                    "Fixed media billing reservation is not active"
                                )

                    # Update conversation last_activity for sort ordering
                    await conn.execute("UPDATE CONVERSATIONS SET last_activity = CURRENT_TIMESTAMP WHERE id = ?", (conversation_id,))

                    if (channel_context is not None
                            and channel_context.on_commit_in_transaction is not None):
                        hook_result = channel_context.on_commit_in_transaction(
                            ChannelCommit(
                                context=channel_context,
                                user_message_id=user_message_id,
                                assistant_message_id=message_id,
                                confirmed_text=channel_confirmed_text,
                                played_ms=channel_played_ms,
                                persistence_only=(
                                    persistence_only
                                    if channel_commit_persistence_only is None
                                    else channel_commit_persistence_only
                                ),
                            ),
                            conn,
                        )
                        if inspect.isawaitable(hook_result):
                            await hook_result

                    if load_watchdog_config and prompt_id is not None:
                        cursor = await conn.execute(
                            "SELECT watchdog_config FROM PROMPTS WHERE id = ?",
                            (prompt_id,),
                        )
                        watchdog_row = await cursor.fetchone()
                        try:
                            watchdog_config = (
                                orjson.loads(watchdog_row[0])
                                if watchdog_row and watchdog_row[0]
                                else None
                            )
                        except (orjson.JSONDecodeError, TypeError):
                            watchdog_config = None

                    await conn.commit()

                    if on_database_commit is not None:
                        database_commit_result = on_database_commit(
                            (user_message_id, message_id)
                        )
                        if inspect.isawaitable(database_commit_result):
                            await database_commit_result

                    # --- Hint consumption: post-commit, best-effort, fail-open ---
                    if (not persistence_only and watchdog_hint_active
                            and watchdog_hint_eval_id is not None):
                        try:
                            async with get_db_connection() as wconn:
                                await wconn.execute(
                                    """UPDATE WATCHDOG_STATE SET pending_hint = NULL, hint_severity = NULL
                                       WHERE conversation_id = ? AND prompt_id = ? AND last_evaluated_message_id = ?""",
                                    (conversation_id, prompt_id, watchdog_hint_eval_id)
                                )
                                await wconn.commit()
                        except Exception:
                            logging.getLogger("watchdog").warning(
                                "Failed to consume hint for conv=%d, will retry next turn",
                                conversation_id, exc_info=True
                            )

                    # --- Watchdog enqueue: fire-and-forget, non-blocking ---
                    post_watchdog_config = (
                        None if persistence_only
                        else _get_post_watchdog_config(watchdog_config)
                    )
                    if (prompt_id and post_watchdog_config and post_watchdog_config.get("enabled")
                            and user_message_id is not None and message_id is not None):
                        try:
                            from tools.watchdog import watchdog_evaluate_task
                            watchdog_evaluate_task.send(conversation_id, user_message_id, message_id, prompt_id)
                        except Exception:
                            logging.getLogger("watchdog").error(
                                "Failed to enqueue watchdog task for conv=%d", conversation_id, exc_info=True
                            )

                    if ((message_id is not None and not persistence_only)
                            or record_user_only_memory):
                        await _record_memory_turn_best_effort(
                            user_id=user_id,
                            conversation_id=conversation_id,
                            user_content=user_message,
                            assistant_content=stored_assistant_content,
                            prompt_id=prompt_id,
                            user_message_id=user_message_id,
                            assistant_message_id=message_id,
                            occurred_at=current_time,
                            incognito=conversation_incognito,
                        )

                    if message_id is not None:
                        try:
                            await record_chat_turn(
                                user_id=user_id,
                                conversation_id=conversation_id,
                                user_message=user_message,
                                assistant_message=stored_assistant_content,
                            )
                        except Exception:
                            logger.warning(
                                "[wellbeing] Failed to record chat turn for conversation_id=%s",
                                conversation_id,
                                exc_info=True,
                            )

                    return user_message_id, message_id

                except StaleChannelTurnError:
                    if transaction_started:
                        await conn.rollback()
                    raise
                except sqlite3.OperationalError as exc:
                    if transaction_started:
                        try:
                            await conn.rollback()
                        except Exception:
                            pass
                    if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                        wait_time = DB_RETRY_DELAY_BASE * (attempt + 1)
                        logger.warning(
                            "[save_content_to_db] - Database locked for conversation %s (attempt %s/%s). Retrying in %.2fs",
                            conversation_id,
                            attempt + 1,
                            DB_MAX_RETRIES,
                            wait_time,
                        )
                        last_lock_error = exc
                        retry_needed = True
                    else:
                        logger.error(f"[save_content_to_db] - Operational error: {exc}")
                        await discard_pending_attachments(pending_attachment_refs, "db_operational_error")
                        return (None, None)
                except Exception as e:
                    if transaction_started:
                        try:
                            await conn.rollback()
                        except Exception:
                            pass
                    logger.error(f"[save_content_to_db] - Error during transaction: {e}")
                    await discard_pending_attachments(pending_attachment_refs, "db_transaction_error")
                    return (None, None)

        if retry_needed:
            await asyncio.sleep(wait_time)
            continue
        break

    if last_lock_error:
        logger.error(
            "[save_content_to_db] - Could not save messages after %s retries: %s",
            DB_MAX_RETRIES,
            last_lock_error,
        )
        await discard_pending_attachments(pending_attachment_refs, "db_lock_retries_exhausted")
    return (None, None)


async def save_content_to_db(
    content, input_tokens, output_tokens, total_tokens, conversation_id, user_id, model,
    user_message=None, input_token_fallback=None, prompt_id=None,
    watchdog_config=None, watchdog_hint_active=False, watchdog_hint_eval_id=None,
    llm_id=None, citations_json=None, byok=False, override_api_cost=None,
    pending_attachment_refs: Optional[list[str]] = None,
    strip_device_action_blocks: bool = False,
    billing_reservation_id: str | None = None,
    billing_only_accumulated_usage: bool = False,
    fixed_billing_reservation_id: str | None = None,
    persistence_only: bool = False,
):
    """Persist immediately or through the active channel's one-shot gate.

    Existing web and integration callers retain immediate behavior.  A phone
    turn publishes its completed draft and blocks here until the transport
    confirms an exact audible prefix.  Billing and all post-commit side effects
    therefore continue to execute in the original transaction exactly once.
    """
    bound = current_channel_turn()
    common_kwargs = dict(
        input_token_fallback=input_token_fallback,
        prompt_id=prompt_id,
        watchdog_config=watchdog_config,
        watchdog_hint_active=watchdog_hint_active,
        watchdog_hint_eval_id=watchdog_hint_eval_id,
        llm_id=llm_id,
        citations_json=citations_json,
        byok=byok,
        override_api_cost=override_api_cost,
        pending_attachment_refs=pending_attachment_refs,
        strip_device_action_blocks=strip_device_action_blocks,
        billing_reservation_id=billing_reservation_id,
        billing_only_accumulated_usage=billing_only_accumulated_usage,
        fixed_billing_reservation_id=fixed_billing_reservation_id,
    )

    if bound is None or bound.context.persistence == "immediate":
        result = await _save_content_to_db_immediate(
            content, input_tokens, output_tokens, total_tokens, conversation_id,
            user_id, model, user_message=user_message,
            channel_context=(bound.context if bound is not None else None),
            channel_confirmed_text=(str(content or "") if bound is not None else None),
            persistence_only=persistence_only,
            **common_kwargs,
        )
        if (bound is not None and bound.context.on_commit is not None
                and any(message_id is not None for message_id in result)):
            try:
                observer_result = bound.context.on_commit(
                    ChannelCommit(
                        context=bound.context,
                        user_message_id=result[0], assistant_message_id=result[1],
                        confirmed_text=str(content or ""), played_ms=None,
                    )
                )
                if inspect.isawaitable(observer_result):
                    await observer_result
            except Exception:
                logger.warning("Post-commit channel observer failed", exc_info=True)
        return result

    if bound.context.ingest_only:
        record_phone_user_memory = (
            bound.context.channel == "phone"
            and user_message is not None
            and not persistence_only
            and not bound.context.provenance.get("phone_memory_outbox")
        )
        result = await _save_content_to_db_immediate(
            None, 0, 0, 0, conversation_id, user_id, model,
            user_message=user_message, save_assistant_message=False,
            persistence_only=True, channel_context=bound.context,
            channel_commit_persistence_only=persistence_only,
            record_user_only_memory=record_phone_user_memory,
            **common_kwargs,
        )
        if (bound.context.on_commit is not None
                and any(message_id is not None for message_id in result)):
            try:
                observer_result = bound.context.on_commit(
                    ChannelCommit(
                        context=bound.context,
                        user_message_id=result[0], assistant_message_id=None,
                        confirmed_text=None, played_ms=None,
                        persistence_only=persistence_only,
                    )
                )
                if inspect.isawaitable(observer_result):
                    await observer_result
            except Exception:
                logger.warning("Post-commit channel observer failed", exc_info=True)
        return result

    handle = bound.handle
    if handle is None:
        raise RuntimeError("Deferred channel persistence requires a bound turn handle")

    async def commit(confirmation: AudibleConfirmation):
        has_audible_assistant = bool(confirmation.text_prefix)
        result = await _save_content_to_db_immediate(
            confirmation.text_prefix if has_audible_assistant else None,
            input_tokens,
            output_tokens,
            total_tokens,
            conversation_id,
            user_id,
            model,
            user_message=user_message,
            save_assistant_message=has_audible_assistant,
            channel_context=bound.context,
            channel_confirmed_text=confirmation.text_prefix or None,
            channel_played_ms=confirmation.played_ms,
            persistence_only=persistence_only,
            record_user_only_memory=(
                not has_audible_assistant and not persistence_only
            ),
            on_database_commit=handle.database_commit_completed,
            **common_kwargs,
        )
        if any(message_id is not None for message_id in result):
            await handle.notify_commit(
                result,
                confirmed_text=confirmation.text_prefix or None,
                played_ms=confirmation.played_ms,
            )
        return result

    return await handle.defer_commit(str(content or ""), commit)


async def save_interrupted_channel_turn(
    confirmation: AudibleConfirmation,
    *,
    context: ChannelContext,
    conversation_id: int,
    user_id: int,
    model: str,
    user_message: Any,
    prompt_id: int | None = None,
    watchdog_config: dict | None = None,
    llm_id: int | None = None,
    pending_attachment_refs: Optional[list[str]] = None,
    on_database_commit: Callable[
        [tuple[int | None, int | None]], None | Awaitable[None]
    ] | None = None,
    persistence_only: bool = False,
) -> tuple[int | None, int | None]:
    """Persist a pre-draft interruption without charging generated tokens again."""
    has_audible_assistant = bool(confirmation.text_prefix)
    result = await _save_content_to_db_immediate(
        confirmation.text_prefix if has_audible_assistant else None,
        0,
        0,
        0,
        conversation_id,
        user_id,
        model,
        user_message=user_message,
        prompt_id=prompt_id,
        watchdog_config=watchdog_config,
        llm_id=llm_id,
        pending_attachment_refs=pending_attachment_refs,
        save_assistant_message=has_audible_assistant,
        skip_billing=True,
        persistence_only=persistence_only,
        record_user_only_memory=(
            not has_audible_assistant and not persistence_only
        ),
        load_watchdog_config=not persistence_only,
        channel_context=context,
        channel_confirmed_text=confirmation.text_prefix or None,
        channel_played_ms=confirmation.played_ms,
        on_database_commit=on_database_commit,
    )
    return result

async def save_multi_ai_to_db(
    combined_json: str,
    results: dict,
    model_ids: list,
    total_input: int,
    total_output: int,
    conversation_id: int,
    user_id: int,
    user_message: str,
    prompt_id: int = None,
    watchdog_config: Optional[dict] = None,
    watchdog_hint_active: bool = False,
    watchdog_hint_eval_id: Optional[int] = None,
    byok_models: set = None,
    incognito: bool = False,
    billing_reservation_id: str | None = None,
    channel_context: ChannelContext | None = None,
) -> tuple:
    """Save Multi-AI response as a single bot message. Bill each model separately.

    Returns (user_msg_id, bot_msg_id)
    """
    last_lock_error = None
    user_input_tokens = estimate_message_tokens(user_message) if user_message else 0

    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        wait_time = 0.0
        async with conversation_write_lock(conversation_id):
            async with get_db_connection() as conn:
                conn.row_factory = aiosqlite.Row
                transaction_started = False
                try:
                    await conn.execute("BEGIN IMMEDIATE")
                    transaction_started = True
                    if channel_context is not None:
                        await assert_commit_guard_in_transaction(
                            channel_context,
                            conn,
                        )
                    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

                    # INSERT user message (type='user', no llm_id)
                    user_msg_id = None
                    if user_message is not None:
                        cursor = await conn.execute(
                            """INSERT INTO messages (conversation_id, user_id, message, type, date)
                               VALUES (?, ?, ?, ?, ?)
                               RETURNING id""",
                            (conversation_id, user_id, user_message, "user", current_time),
                        )
                        user_row = await cursor.fetchone()
                        user_msg_id = user_row[0] if user_row else None

                    # INSERT bot message with combined_json, total tokens, llm_id=NULL (multi-model)
                    cursor = await conn.execute(
                        """INSERT INTO messages
                           (conversation_id, user_id, message, type, input_tokens_used, output_tokens_used, date, llm_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                           RETURNING id""",
                        (conversation_id, user_id, combined_json, "bot", total_input, total_output, current_time, None),
                    )
                    bot_row = await cursor.fetchone()
                    bot_msg_id = bot_row[0] if bot_row else None

                    ai_credit = await prepare_ai_reservation_settlement(
                        conn,
                        reservation_id=billing_reservation_id,
                        user_id=user_id,
                    )

                    # Bill each model separately
                    _byok_set = byok_models or set()
                    for llm_id in model_ids:
                        r = results[llm_id]
                        if r.get("error") and not r.get("billable_usage"):
                            continue

                        model_name = r["model"]
                        input_cost, output_cost = await get_llm_token_costs(conn=conn, llm_id=llm_id)

                        reported_input_tokens = int(r.get("input_tokens") or 0)
                        # Avoid double-counting user tokens when provider already reports prompt tokens.
                        billable_input = (
                            reported_input_tokens
                            if reported_input_tokens > 0
                            else user_input_tokens
                        )
                        reported_output_tokens = int(r.get("output_tokens") or 0)
                        billable_output = (
                            reported_output_tokens
                            if reported_output_tokens > 0
                            else estimate_message_tokens(r.get("content", ""))
                        )
                        bill_result = await consume_token(
                            user_id,
                            billable_input,
                            billable_output,
                            input_cost,
                            output_cost,
                            conn,
                            cursor,
                            prompt_id=prompt_id,
                            byok=llm_id in _byok_set,
                            billing_account_id_override=(
                                ai_credit.billing_account_id if ai_credit else None
                            ),
                        )
                        if not bill_result:
                            raise MultiAiBillingError(
                                f"Billing failed for user={user_id} model={model_name}"
                            )

                    await complete_ai_reservation_settlement(conn, ai_credit)

                    # Update conversation last_activity for sort ordering
                    await conn.execute("UPDATE CONVERSATIONS SET last_activity = CURRENT_TIMESTAMP WHERE id = ?", (conversation_id,))

                    if (channel_context is not None
                            and channel_context.on_commit_in_transaction is not None):
                        hook_result = channel_context.on_commit_in_transaction(
                            ChannelCommit(
                                context=channel_context,
                                user_message_id=user_msg_id,
                                assistant_message_id=bot_msg_id,
                                confirmed_text=combined_json,
                                played_ms=None,
                            ),
                            conn,
                        )
                        if inspect.isawaitable(hook_result):
                            await hook_result

                    await conn.commit()

                    # Keep watchdog state transitions aligned with single-model save flow.
                    if watchdog_hint_active and watchdog_hint_eval_id is not None:
                        try:
                            async with get_db_connection() as wconn:
                                await wconn.execute(
                                    """UPDATE WATCHDOG_STATE
                                       SET pending_hint = NULL, hint_severity = NULL
                                       WHERE conversation_id = ? AND prompt_id = ? AND last_evaluated_message_id = ?""",
                                    (conversation_id, prompt_id, watchdog_hint_eval_id),
                                )
                                await wconn.commit()
                        except Exception:
                            logging.getLogger("watchdog").warning(
                                "Failed to consume hint for conv=%d (multi-ai), will retry next turn",
                                conversation_id,
                                exc_info=True,
                            )

                    post_watchdog_config = _get_post_watchdog_config(watchdog_config)
                    if (prompt_id and post_watchdog_config and post_watchdog_config.get("enabled")
                            and user_msg_id is not None and bot_msg_id is not None):
                        try:
                            from tools.watchdog import watchdog_evaluate_task
                            watchdog_evaluate_task.send(conversation_id, user_msg_id, bot_msg_id, prompt_id)
                        except Exception:
                            logging.getLogger("watchdog").error(
                                "Failed to enqueue watchdog task for conv=%d (multi-ai)",
                                conversation_id,
                                exc_info=True,
                            )

                    if bot_msg_id is not None:
                        await _record_memory_turn_best_effort(
                            user_id=user_id,
                            conversation_id=conversation_id,
                            user_content=user_message,
                            assistant_content=combined_json,
                            prompt_id=prompt_id,
                            user_message_id=user_msg_id,
                            assistant_message_id=bot_msg_id,
                            occurred_at=current_time,
                            incognito=incognito,
                        )

                    try:
                        await record_chat_turn(
                            user_id=user_id,
                            conversation_id=conversation_id,
                            user_message=user_message,
                            assistant_message=combined_json,
                        )
                    except Exception:
                        logger.warning(
                            "[wellbeing] Failed to record multi-ai chat turn for conversation_id=%s",
                            conversation_id,
                            exc_info=True,
                        )

                    return (user_msg_id, bot_msg_id)

                except sqlite3.OperationalError as exc:
                    if transaction_started:
                        try:
                            await conn.rollback()
                        except Exception:
                            pass
                    if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                        wait_time = DB_RETRY_DELAY_BASE * (attempt + 1)
                        logger.warning(
                            "[save_multi_ai_to_db] Database locked (attempt %s/%s). Retrying in %.2fs",
                            attempt + 1, DB_MAX_RETRIES, wait_time,
                        )
                        last_lock_error = exc
                        retry_needed = True
                    else:
                        logger.error("[save_multi_ai_to_db] Operational error: %s", exc)
                        raise
                except Exception as exc:
                    if transaction_started:
                        try:
                            await conn.rollback()
                        except Exception:
                            pass
                    logger.error("[save_multi_ai_to_db] Transaction failed: %s", exc, exc_info=True)
                    raise

        if retry_needed:
            await asyncio.sleep(wait_time)
            continue
        break

    if last_lock_error:
        logger.error(
            "[save_multi_ai_to_db] Could not save after %s retries: %s",
            DB_MAX_RETRIES, last_lock_error,
        )
    return (None, None)
