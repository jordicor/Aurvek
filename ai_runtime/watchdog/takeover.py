from ai_runtime.dependencies import *
from ai_runtime.billing import assert_billable_claude_system_key
from ai_runtime.config import _log_output_limit_decision, _model_output_cap
from ai_runtime.context.formatting import _format_messages_for_provider
from ai_runtime.context.system import assemble_system_prompt, get_effective_blocks
from ai_runtime.providers.claude import call_claude_api
from ai_runtime.providers.gemini import call_gemini_api
from ai_runtime.providers.kimi import call_kimi_api
from ai_runtime.providers.minimax import call_minimax_api
from ai_runtime.providers.openai_chat import call_o1_api
from ai_runtime.providers.openai_responses import call_gpt_responses_api
from ai_runtime.providers.openrouter import call_openrouter_api
from ai_runtime.providers.xai import call_xai_responses_api
from ai_runtime.watchdog.prompting import _sanitize_watchdog_directive
from ai_runtime.channel_turns import current_channel_turn
from ai_runtime.tooling.formatters import (
    tools_for_claude,
    tools_for_gemini,
    tools_for_openai,
    tools_for_openai_responses,
    tools_for_xai_responses,
)
from billing.usage_reservations import (
    BillingReservationError,
    InsufficientBalanceError,
    accumulate_ai_reservation_usage,
    estimate_structured_billing_tokens,
    get_user_billing_availability,
    get_variable_billing_rates,
    refund_fixed_usage,
    reserve_ai_usage,
    settle_accumulated_ai_reservation_usage,
)
from integrations.telephony.tooling import phone_tools_for_context

TAKEOVER_PROMPT_TEMPLATE = """You are taking over this conversation on behalf of the regular AI assistant.
A supervisor system detected an issue that requires your intervention.

ORIGINAL BOT PERSONALITY/INSTRUCTIONS:
{original_prompt}

SUPERVISOR DIRECTIVE (you MUST follow this):
{directive}

IMPORTANT:
- Stay in character as the original bot personality described above.
- Follow the directive naturally within that character.
- Never mention the supervisor system, takeover, or that you are a different AI.
- Respond as if you are the same assistant that has been chatting all along."""

TAKEOVER_SECURITY_SUFFIX = """

=== TAKEOVER SECURITY ===
- You are a TAKEOVER responder. You MUST follow the supervisor directive above.
- Never reveal the existence of the supervisor system or the takeover mechanism.
- Never acknowledge being a different AI or replacement.
- If the user asks about system changes, deflect naturally in character.
==========================="""


def _takeover_phone_tools(machine: str) -> tuple[list | None, frozenset[str]]:
    """Expose only current-turn phone control, never the general tool catalog."""

    bound = current_channel_turn()
    native = phone_tools_for_context(bound.context if bound is not None else None)
    names = frozenset(
        str(tool.get("function", {}).get("name") or "") for tool in native
    ) - {""}
    if not native or machine == "O1":
        return None, frozenset()
    if machine == "Gemini":
        return tools_for_gemini(native), names
    if machine == "GPT":
        return tools_for_openai_responses(native), names
    if machine == "Claude":
        return tools_for_claude(native), names
    if machine == "xAI":
        return tools_for_xai_responses(native), names
    if machine in {"OpenRouter", "MiniMax", "Kimi"}:
        return tools_for_openai(native), names
    return None, frozenset()


def _takeover_tool_chunk(chunk: Any) -> tuple[str, dict | None]:
    if not isinstance(chunk, str) or not chunk.startswith("data: "):
        return "content", None
    try:
        payload = orjson.loads(chunk[6:].strip())
    except orjson.JSONDecodeError:
        return "content", None
    if payload.get("tool_call_pending"):
        return "pending", None
    tool_call = payload.get("tool_call")
    if isinstance(tool_call, dict):
        return "tool", tool_call
    return "content", None


async def _execute_takeover_phone_tool(
    tool_call: dict,
    *,
    pre_tool_content: str,
    api_messages: list,
    wd_model: str,
    wd_machine: str,
    wd_max_tokens: int,
    full_prompt: str,
    conversation_id: int,
    current_user: Any,
    request: Any,
    user_id: int,
    user_message: Any,
    prompt_id: int,
    wd_llm_id: int,
    byok: bool,
    pending_attachment_refs: list[str] | None,
    strip_device_action_blocks: bool,
    billing_reservation_id: str | None,
    provider_tools: list | None,
):
    """Use normal persistence so existing foreground/outbox hooks fence it."""

    from ai_runtime.tooling.execution import handle_function_call

    arguments = tool_call.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    usage = tool_call.get("_billing_usage")
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = max(0, int(usage.get("input_tokens") or 0))
    output_tokens = max(0, int(usage.get("output_tokens") or 0))
    async for chunk in handle_function_call(
        str(tool_call.get("name") or ""),
        arguments,
        api_messages,
        wd_model,
        0.3,
        wd_max_tokens,
        pre_tool_content,
        conversation_id,
        current_user,
        request,
        input_tokens,
        output_tokens,
        input_tokens + output_tokens,
        None,
        user_id,
        wd_machine,
        full_prompt,
        user_message,
        prompt_id=prompt_id,
        watchdog_config=None,
        llm_id=wd_llm_id,
        byok=byok,
        pending_attachment_refs=pending_attachment_refs,
        strip_device_action_blocks=strip_device_action_blocks,
        billing_reservation_id=billing_reservation_id,
        billing_first_call_accumulated=bool(billing_reservation_id),
        tool_call=tool_call,
        provider_tools=provider_tools,
    ):
        yield chunk


async def _reserve_takeover_usage(
    *,
    user_id: int,
    prompt_id: int | None,
    machine: str,
    maximum_output_tokens: int,
    input_cost_per_million: float,
    output_cost_per_million: float,
    byok: bool,
    full_prompt: str,
    api_messages: list,
) -> tuple[int, str | None]:
    rates = await get_variable_billing_rates(
        user_id=user_id,
        prompt_id=prompt_id,
        input_cost_per_million=input_cost_per_million,
        output_cost_per_million=output_cost_per_million,
        byok=byok,
    )
    input_tokens = estimate_structured_billing_tokens(full_prompt, api_messages)
    if machine == "Claude":
        input_charge = input_tokens * 4 * rates.input_per_token
        output_rate = 6 * rates.input_per_token + 4 * rates.output_per_token
    else:
        input_charge = input_tokens * rates.input_per_token
        output_rate = rates.output_per_token
    availability = await get_user_billing_availability(user_id)
    remaining = availability["available"] - input_charge
    if remaining < -1e-12:
        raise InsufficientBalanceError("Insufficient balance")
    if output_rate > 0:
        maximum_output_tokens = min(
            maximum_output_tokens,
            int(max(0.0, remaining) / output_rate),
        )
    if maximum_output_tokens < 1:
        raise InsufficientBalanceError("Insufficient balance")
    maximum_charge = input_charge + maximum_output_tokens * output_rate
    reservation_id = await reserve_ai_usage(
        user_id=user_id,
        maximum_amount=maximum_charge,
    )
    return maximum_output_tokens, reservation_id


async def _settle_or_refund_takeover_reservation(
    *,
    reservation_id: str,
    user_id: int,
    input_cost_per_million: float,
    output_cost_per_million: float,
    prompt_id: int | None,
    byok: bool,
    provider_usage_observed: bool = False,
) -> None:
    """Capture durable takeover usage, refunding only a genuinely unused hold."""

    try:
        captured = await settle_accumulated_ai_reservation_usage(
            reservation_id=reservation_id,
            user_id=user_id,
            input_cost_per_million=input_cost_per_million,
            output_cost_per_million=output_cost_per_million,
            prompt_id=prompt_id,
            byok=byok,
        )
    except BillingReservationError:
        logger.exception(
            "Could not capture unfinished takeover usage %s",
            reservation_id,
        )
        return

    if captured or provider_usage_observed:
        return
    try:
        await refund_fixed_usage(reservation_id)
    except BillingReservationError:
        logger.exception(
            "Could not release unused takeover reservation %s",
            reservation_id,
        )


async def watchdog_takeover_response(
    conversation_id: int,
    prompt_id: int,
    user_id: int,
    watchdog_config: dict,
    original_prompt: str,
    directive: str,
    context_messages: list,
    user_message,
    message,
    should_lock: bool,
    current_user,
    request,
    user_api_keys: dict,
    machine: str,
    model: str,
    event_type: str = "security",
    source: str = "post",
    pending_attachment_refs: Optional[list[str]] = None,
    strip_device_action_blocks: bool = False,
):
    """Async generator: stream a takeover response from the watchdog LLM.

    Yields SSE chunks. If should_lock, also locks the conversation and yields
    an end_conversation event.
    """
    sanitized_directive = _sanitize_watchdog_directive(directive)
    bound_turn = current_channel_turn()
    takeover_channel = (
        bound_turn.context.channel if bound_turn is not None else "web"
    )
    lock_finalized = False
    if should_lock:
        # A response-generation or billing failure must never undo a lock that
        # the watchdog has already decided is required.
        from tools.watchdog import _finalize_takeover

        await _finalize_takeover(
            conversation_id,
            prompt_id,
            event_type,
            sanitized_directive,
            channel=takeover_channel,
            should_lock=True,
            locked_reason=f"WATCHDOG_{event_type.upper()}_TAKEOVER",
        )
        lock_finalized = True

    # 1. Resolve watchdog LLM
    wd_llm_id = watchdog_config.get("llm_id")
    wd_llm = await get_llm_info(wd_llm_id)
    if not wd_llm:
        logger.error("watchdog takeover: LLM id=%s not found", wd_llm_id)
        yield f"data: {orjson.dumps({'error': 'Watchdog LLM not found'}).decode()}\n\n"
        return

    wd_machine = wd_llm["machine"]
    wd_model = wd_llm["model"]
    if wd_machine == "GPTSub":
        logger.error("watchdog takeover: personal subscription LLM is not supported")
        yield f"data: {orjson.dumps({'error': 'Personal ChatGPT subscription models cannot be used for watchdog takeover.'}).decode()}\n\n"
        return
    wd_max_tokens, wd_limit_fallback = _model_output_cap(wd_llm.get("max_output_tokens"))
    _log_output_limit_decision(
        source="watchdog_takeover",
        conversation_id=conversation_id,
        llm_id=wd_llm_id,
        machine=wd_machine,
        model=wd_model,
        max_output_tokens=wd_llm.get("max_output_tokens"),
        fallback_used=wd_limit_fallback,
        final_limit=wd_max_tokens,
        balance_limited=False,
    )

    # 2. Resolve BYOK key for watchdog LLM
    api_key_mode = await get_user_api_key_mode(user_id)
    resolved_key, use_system = resolve_api_key_for_provider(
        user_api_keys or {}, api_key_mode, wd_machine
    )
    if not resolved_key and not use_system:
        logger.error("watchdog takeover: no API key for %s", wd_machine)
        yield f"data: {orjson.dumps({'error': 'API key required for takeover LLM'}).decode()}\n\n"
        return

    wd_guard_error = assert_billable_claude_system_key(
        machine=wd_machine,
        model=wd_model,
        llm_id=wd_llm_id,
        is_byok=resolved_key is not None,
        input_token_cost=wd_llm.get("input_token_cost", 0),
        output_token_cost=wd_llm.get("output_token_cost", 0),
    )
    if wd_guard_error:
        logger.error(wd_guard_error)
        yield f"data: {orjson.dumps({'error': wd_guard_error}).decode()}\n\n"
        return

    # 4. Build system prompt via global blocks (system blocks only for takeover)
    blocks = await get_effective_blocks()
    takeover_blocks = [b for b in blocks if b.get("system_key") in SYSTEM_BLOCK_METADATA]
    if await current_user.is_admin:
        user_level = "admin"
    elif await current_user.is_user:
        user_level = "user"
    else:
        user_level = "customer"
    variables = {
        "user_level": user_level,
        "current_datetime_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }

    takeover_base = TAKEOVER_PROMPT_TEMPLATE.format(
        original_prompt=original_prompt[:5000],
        directive=sanitized_directive,
    )
    assembled = assemble_system_prompt(takeover_blocks, variables, takeover_base,
                                        watchdog_enabled=True)
    full_prompt = assembled + "\n\n" + TAKEOVER_SECURITY_SUFFIX.strip()

    # 5. Format messages for the watchdog LLM's provider
    api_messages = await _format_messages_for_provider(
        context_messages, message, full_prompt, wd_machine, current_user,
        conversation_id=conversation_id,
    )

    # 6. Select streaming function
    if wd_machine == "Gemini":
        api_func = call_gemini_api
    elif wd_machine == "O1":
        api_func = call_o1_api
    elif wd_machine == "GPT":
        api_func = call_gpt_responses_api
    elif wd_machine == "Claude":
        api_func = call_claude_api
    elif wd_machine == "xAI":
        api_func = call_xai_responses_api
    elif wd_machine == "OpenRouter":
        api_func = call_openrouter_api
    elif wd_machine == "MiniMax":
        api_func = call_minimax_api
    elif wd_machine == "Kimi":
        api_func = call_kimi_api
    else:
        logger.error("watchdog takeover: unknown machine %s", wd_machine)
        yield f"data: {orjson.dumps({'error': f'Unknown LLM provider: {wd_machine}'}).decode()}\n\n"
        return

    try:
        wd_max_tokens, billing_reservation_id = await _reserve_takeover_usage(
            user_id=user_id,
            prompt_id=prompt_id,
            machine=wd_machine,
            maximum_output_tokens=wd_max_tokens,
            input_cost_per_million=wd_llm.get("input_token_cost", 0),
            output_cost_per_million=wd_llm.get("output_token_cost", 0),
            byok=resolved_key is not None,
            full_prompt=full_prompt,
            api_messages=api_messages,
        )
    except InsufficientBalanceError:
        yield f"data: {orjson.dumps({'error': 'Insufficient balance for takeover response'}).decode()}\n\n"
        return
    except BillingReservationError:
        logger.exception("Could not reserve takeover billing")
        yield f"data: {orjson.dumps({'error': 'Takeover billing is temporarily unavailable'}).decode()}\n\n"
        return

    # 7. Build kwargs. Takeover keeps general tools disabled, but preserves the
    # current foreground turn's one phone-control capability.
    provider_phone_tools, allowed_phone_tools = _takeover_phone_tools(wd_machine)
    kwargs = {
        "messages": api_messages,
        "model": wd_model,
        "temperature": 0.3,
        "max_tokens": wd_max_tokens,
        "prompt": full_prompt,
        "conversation_id": conversation_id,
        "current_user": current_user,
        "request": request,
        "user_message": user_message,
        "prompt_id": prompt_id,
        "watchdog_config": None,  # Prevent self-evaluation
        "watchdog_hint_active": False,
        "watchdog_hint_eval_id": None,
        "llm_id": wd_llm_id,
        "byok": resolved_key is not None,
        "pending_attachment_refs": pending_attachment_refs,
        "strip_device_action_blocks": strip_device_action_blocks,
        "billing_reservation_id": billing_reservation_id,
    }
    if resolved_key:
        kwargs["user_api_key"] = resolved_key
    if provider_phone_tools:
        kwargs["tools"] = provider_phone_tools

    # 8. Stream response
    try:
        collected_tool_call = None
        pre_tool_content = ""
        async for chunk in api_func(**kwargs):
            chunk_kind, tool_call = _takeover_tool_chunk(chunk)
            if chunk_kind == "tool":
                if (
                    collected_tool_call is not None
                    or tool_call is None
                    or str(tool_call.get("name") or "") not in allowed_phone_tools
                ):
                    raise RuntimeError(
                        "Watchdog takeover attempted an unavailable tool"
                    )
                collected_tool_call = tool_call
                if isinstance(chunk, str) and chunk.startswith("data: "):
                    payload = orjson.loads(chunk[6:].strip())
                    pre_tool_content = str(payload.get("pre_tool_content") or "")
                continue
            if chunk_kind == "pending":
                continue
            yield chunk
        if collected_tool_call is not None:
            async for tool_chunk in _execute_takeover_phone_tool(
                collected_tool_call,
                pre_tool_content=pre_tool_content,
                api_messages=api_messages,
                wd_model=wd_model,
                wd_machine=wd_machine,
                wd_max_tokens=wd_max_tokens,
                full_prompt=full_prompt,
                conversation_id=conversation_id,
                current_user=current_user,
                request=request,
                user_id=user_id,
                user_message=user_message,
                prompt_id=prompt_id,
                wd_llm_id=wd_llm_id,
                byok=resolved_key is not None,
                pending_attachment_refs=pending_attachment_refs,
                strip_device_action_blocks=strip_device_action_blocks,
                billing_reservation_id=billing_reservation_id,
                provider_tools=provider_phone_tools,
            ):
                yield tool_chunk
    except Exception as exc:
        logger.error("watchdog takeover: streaming failed for conv=%d: %s", conversation_id, exc)
        # Persist error event
        from tools.watchdog import _persist_error_event
        await _persist_error_event(conversation_id, prompt_id, 0, 0, f"Takeover streaming error: {exc}", source)
        raise
    finally:
        if billing_reservation_id:
            await _settle_or_refund_takeover_reservation(
                reservation_id=billing_reservation_id,
                user_id=user_id,
                input_cost_per_million=wd_llm.get("input_token_cost", 0),
                output_cost_per_million=wd_llm.get("output_token_cost", 0),
                prompt_id=prompt_id,
                byok=resolved_key is not None,
            )

    # 9. Finalize takeover (lock if needed, clean state, persist event)
    if not lock_finalized:
        from tools.watchdog import _finalize_takeover

        await _finalize_takeover(
            conversation_id, prompt_id, event_type, sanitized_directive,
            channel=takeover_channel, should_lock=False,
            locked_reason=None,
        )
    if should_lock:
        yield f"data: {orjson.dumps({'end_conversation': True}).decode()}\n\n"


class _StubUser:
    """Minimal user stub for provider functions that only need current_user.id."""
    __slots__ = ("id",)

    def __init__(self, user_id: int):
        self.id = user_id


async def watchdog_takeover_response_requestfree(
    directive: str,
    watchdog_config: dict,
    context_messages: list,
    user_id: int,
    conversation_id: int = 0,
    prompt_id: int = 0,
    original_prompt: str = "",
    user_level: str = "customer",
    source: str = "post",
    billing_context: dict | None = None,
):
    """Request-free watchdog takeover response generator.

    Extracted from watchdog_takeover_response() for use in both web chat
    (get_ai_response) and external channels (process_gransabio_external)
    where no FastAPI Request or full User object is available.

    Args:
        directive: The watchdog's instruction (what to generate).
        watchdog_config: Sub-config dict (pre or post watchdog) with llm_id, etc.
        context_messages: Conversation history for context.
        user_id: For BYOK key resolution.
        conversation_id: Conversation ID (for stop signals and logging).
        prompt_id: Prompt ID (for event persistence).
        original_prompt: The bot's system prompt (for takeover template).
        user_level: One of "admin", "user", "customer" (for system block variables).
        source: "pre" or "post" (for event persistence).

    Yields:
        SSE-formatted string chunks (same format as provider functions).
    """
    owns_billing_context = billing_context is None
    if billing_context is None:
        billing_context = {}
    # 1. Resolve watchdog LLM
    wd_llm_id = watchdog_config.get("llm_id")
    wd_llm = await get_llm_info(wd_llm_id)
    if not wd_llm:
        logger.error("watchdog takeover requestfree: LLM id=%s not found", wd_llm_id)
        yield f"data: {orjson.dumps({'error': 'Watchdog LLM not found'}).decode()}\n\n"
        return

    wd_machine = wd_llm["machine"]
    wd_model = wd_llm["model"]
    if wd_machine == "GPTSub":
        logger.error(
            "watchdog takeover requestfree: personal subscription LLM is not supported"
        )
        yield f"data: {orjson.dumps({'error': 'Personal ChatGPT subscription models cannot be used for watchdog takeover.'}).decode()}\n\n"
        return
    wd_max_tokens, wd_limit_fallback = _model_output_cap(wd_llm.get("max_output_tokens"))
    _log_output_limit_decision(
        source="watchdog_takeover_requestfree",
        conversation_id=conversation_id,
        llm_id=wd_llm_id,
        machine=wd_machine,
        model=wd_model,
        max_output_tokens=wd_llm.get("max_output_tokens"),
        fallback_used=wd_limit_fallback,
        final_limit=wd_max_tokens,
        balance_limited=False,
    )

    # 2. Resolve BYOK key for watchdog LLM
    from tools.watchdog import _read_user_api_keys
    user_api_keys = await _read_user_api_keys(user_id)
    api_key_mode = await get_user_api_key_mode(user_id)
    resolved_key, use_system = resolve_api_key_for_provider(
        user_api_keys, api_key_mode, wd_machine
    )
    if not resolved_key and not use_system:
        logger.error("watchdog takeover requestfree: no API key for %s", wd_machine)
        yield f"data: {orjson.dumps({'error': 'API key required for takeover LLM'}).decode()}\n\n"
        return

    wd_guard_error = assert_billable_claude_system_key(
        machine=wd_machine,
        model=wd_model,
        llm_id=wd_llm_id,
        is_byok=resolved_key is not None,
        input_token_cost=wd_llm.get("input_token_cost", 0),
        output_token_cost=wd_llm.get("output_token_cost", 0),
    )
    if wd_guard_error:
        logger.error(wd_guard_error)
        yield f"data: {orjson.dumps({'error': wd_guard_error}).decode()}\n\n"
        return

    # 3. Sanitize directive
    sanitized_directive = _sanitize_watchdog_directive(directive)

    # 4. Build system prompt via global blocks
    blocks = await get_effective_blocks()
    takeover_blocks = [b for b in blocks if b.get("system_key") in SYSTEM_BLOCK_METADATA]
    variables = {
        "user_level": user_level,
        "current_datetime_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }

    takeover_base = TAKEOVER_PROMPT_TEMPLATE.format(
        original_prompt=original_prompt[:5000],
        directive=sanitized_directive,
    )
    assembled = assemble_system_prompt(takeover_blocks, variables, takeover_base,
                                        watchdog_enabled=True)
    full_prompt = assembled + "\n\n" + TAKEOVER_SECURITY_SUFFIX.strip()

    # 5. Format messages for the watchdog LLM's provider
    # Extract last user message as plain text (no multimodal for external channels)
    last_user_msg = ""
    for msg in reversed(context_messages):
        if msg.get("type") == "user":
            content = msg.get("message", "")
            if isinstance(content, list):
                last_user_msg = " ".join(
                    b.get("text", "") for b in content if b.get("type") == "text"
                )
            else:
                last_user_msg = str(content)
            break

    api_messages = await _format_messages_for_provider(
        context_messages, last_user_msg, full_prompt, wd_machine,
        current_user=None,
        conversation_id=conversation_id,
    )

    # 6. Select streaming function
    if wd_machine == "Gemini":
        api_func = call_gemini_api
    elif wd_machine == "O1":
        api_func = call_o1_api
    elif wd_machine == "GPT":
        api_func = call_gpt_responses_api
    elif wd_machine == "Claude":
        api_func = call_claude_api
    elif wd_machine == "xAI":
        api_func = call_xai_responses_api
    elif wd_machine == "OpenRouter":
        api_func = call_openrouter_api
    elif wd_machine == "MiniMax":
        api_func = call_minimax_api
    elif wd_machine == "Kimi":
        api_func = call_kimi_api
    else:
        logger.error("watchdog takeover requestfree: unknown machine %s", wd_machine)
        yield f"data: {orjson.dumps({'error': f'Unknown LLM provider: {wd_machine}'}).decode()}\n\n"
        return

    try:
        wd_max_tokens, billing_reservation_id = await _reserve_takeover_usage(
            user_id=user_id,
            prompt_id=prompt_id,
            machine=wd_machine,
            maximum_output_tokens=wd_max_tokens,
            input_cost_per_million=wd_llm.get("input_token_cost", 0),
            output_cost_per_million=wd_llm.get("output_token_cost", 0),
            byok=resolved_key is not None,
            full_prompt=full_prompt,
            api_messages=api_messages,
        )
    except InsufficientBalanceError:
        yield f"data: {orjson.dumps({'error': 'Insufficient balance for takeover response'}).decode()}\n\n"
        return
    except BillingReservationError:
        logger.exception("Could not reserve request-free takeover billing")
        yield f"data: {orjson.dumps({'error': 'Takeover billing is temporarily unavailable'}).decode()}\n\n"
        return
    billing_context.update(
        {
            "reservation_id": billing_reservation_id,
            "byok": resolved_key is not None,
            "input_tokens": 0,
            "output_tokens": 0,
            "input_cost_per_million": wd_llm.get("input_token_cost", 0),
            "output_cost_per_million": wd_llm.get("output_token_cost", 0),
            "provider_usage_observed": False,
            "usage_accumulated": False,
        }
    )

    # 7. Build kwargs (stub user, no request, no tools, no watchdog to prevent recursion)
    # save_to_db=False: caller (process_gransabio_external or get_ai_response)
    # owns persistence. Prevents double-save when providers auto-persist.
    stub_user = _StubUser(user_id)
    kwargs = {
        "messages": api_messages,
        "model": wd_model,
        "temperature": 0.3,
        "max_tokens": wd_max_tokens,
        "prompt": full_prompt,
        "conversation_id": conversation_id,
        "current_user": stub_user,
        "request": None,
        "user_message": last_user_msg,
        "prompt_id": prompt_id,
        "watchdog_config": None,
        "watchdog_hint_active": False,
        "watchdog_hint_eval_id": None,
        "llm_id": wd_llm_id,
        "byok": resolved_key is not None,
        "save_to_db": False,
    }
    if resolved_key:
        kwargs["user_api_key"] = resolved_key

    # 8. Stream response
    try:
        async for chunk in api_func(**kwargs):
            if isinstance(chunk, str) and ("tool_call" in chunk and "tool_call_pending" not in chunk):
                billing_context["provider_usage_observed"] = True
                continue
            if isinstance(chunk, str) and "tool_call_pending" in chunk:
                billing_context["provider_usage_observed"] = True
                continue
            if isinstance(chunk, str) and chunk.startswith("data: "):
                try:
                    payload = orjson.loads(chunk[6:].strip())
                    if payload.get("content"):
                        billing_context["provider_usage_observed"] = True
                    if (
                        payload.get("token_info")
                        and not billing_context["usage_accumulated"]
                    ):
                        input_tokens = int(payload.get("input_tokens") or 0)
                        output_tokens = int(payload.get("output_tokens") or 0)
                        billing_context["provider_usage_observed"] = True
                        if billing_reservation_id:
                            await accumulate_ai_reservation_usage(
                                reservation_id=billing_reservation_id,
                                user_id=user_id,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                component={
                                    "input_tokens": input_tokens,
                                    "output_tokens": output_tokens,
                                    "input_cost_per_million": wd_llm.get(
                                        "input_token_cost", 0
                                    ),
                                    "output_cost_per_million": wd_llm.get(
                                        "output_token_cost", 0
                                    ),
                                    "prompt_id": prompt_id,
                                    "byok": resolved_key is not None,
                                    "idempotency_key": (
                                        "watchdog-requestfree:"
                                        f"{billing_reservation_id}"
                                    ),
                                },
                            )
                        billing_context["input_tokens"] = input_tokens
                        billing_context["output_tokens"] = output_tokens
                        billing_context["usage_accumulated"] = True
                except (orjson.JSONDecodeError, AttributeError, TypeError, ValueError):
                    pass
            yield chunk
    except Exception as exc:
        logger.error("watchdog takeover requestfree: streaming failed for conv=%d: %s",
                     conversation_id, exc)
        from tools.watchdog import _persist_error_event
        await _persist_error_event(
            conversation_id, prompt_id, 0, 0,
            f"Takeover requestfree streaming error: {exc}", source,
        )
        raise
    finally:
        if owns_billing_context and billing_reservation_id:
            await _settle_or_refund_takeover_reservation(
                reservation_id=billing_reservation_id,
                user_id=user_id,
                input_cost_per_million=wd_llm.get("input_token_cost", 0),
                output_cost_per_million=wd_llm.get("output_token_cost", 0),
                prompt_id=prompt_id,
                byok=resolved_key is not None,
                provider_usage_observed=bool(
                    billing_context.get("provider_usage_observed")
                ),
            )
