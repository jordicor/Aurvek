"""Aurvek provider adapter for one native OpenAI Realtime phone turn.

Transport/playback remain in ``integrations.telephony``.  This adapter keeps
the normal runtime contract: it streams transcript SSE, delegates tool
execution through an injected callback, and hands the final audible draft to
the existing canonical persistence/billing/memory/watchdog path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from typing import Any

import orjson

from ai_runtime.channel_turns import current_channel_turn
from ai_runtime.dependencies import openai_key
from ai_runtime.persistence.messages import persistence_error_payload, save_content_to_db
from ai_runtime.provider_health import (
    record_provider_error_for_label,
    record_provider_success_for_label,
)
from ai_runtime.reasoning import ReasoningSelection, parse_reasoning_selection
from integrations.telephony.openai_realtime_billing import (
    accumulate_openai_realtime_response_usage,
    mark_openai_realtime_usage_uncertain,
)
from integrations.telephony.realtime_bridge import (
    RealtimeBridgeHandle,
    RealtimeDoneEvent,
    RealtimeErrorEvent,
    RealtimeStatusEvent,
    RealtimeToolCallEvent,
    RealtimeTranscriptEvent,
    RealtimeTurnBridge,
)


PROVIDER_LABEL = "OpenAI Realtime"
_PHONE_TOOL_AUDIO_INSTRUCTION = (
    "For telephone tool calls, call the function without speaking a preamble. "
    "Wait for its function result, then answer naturally in speech using that "
    "result. If end_call succeeds, say the returned final_message verbatim as "
    "the complete farewell and do not continue after it."
)


def _sse(payload: Mapping[str, Any]) -> str:
    return f"data: {orjson.dumps(payload).decode()}\n\n"


def _reasoning_effort(
    selection: ReasoningSelection | Mapping[str, Any] | str | None,
) -> str | None:
    resolved = parse_reasoning_selection(selection)
    if resolved.mode == "default":
        # Phone defaults optimize for conversational latency.  Prompt authors
        # can explicitly raise the effort when the use case values deeper
        # reasoning over response time.
        return "minimal"
    if resolved.mode == "auto":
        return None
    # Realtime has no true disabled effort; minimal is its documented floor.
    if resolved.mode == "off":
        return "minimal"
    if resolved.mode == "max":
        return "xhigh"
    if resolved.mode == "custom":
        raise ValueError("custom reasoning token budgets are unavailable for Realtime")
    if resolved.mode not in {"minimal", "low", "medium", "high", "xhigh"}:
        raise ValueError("unsupported Realtime reasoning effort")
    return resolved.mode


def _realtime_tools(tools: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for tool in tools or ():
        if not isinstance(tool, Mapping) or tool.get("type") != "function":
            raise ValueError("OpenAI Realtime supports function tools only")
        if isinstance(tool.get("function"), Mapping):
            function = tool["function"]
            normalized.append(
                {
                    "type": "function",
                    "name": function.get("name"),
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {}),
                }
            )
        else:
            normalized.append(dict(tool))
    return normalized


def _bridge_from_context() -> RealtimeBridgeHandle | RealtimeTurnBridge:
    bound = current_channel_turn()
    if bound is None or bound.context.channel != "phone":
        raise RuntimeError("OpenAI Realtime requires an active phone channel turn")
    bridge = bound.context.provenance.get("openai_realtime_bridge")
    if not getattr(bridge, "_aurvek_internal_realtime_bridge", False):
        raise RuntimeError("Phone channel has no trusted OpenAI Realtime bridge")
    return bridge


def _last_function_output(messages: Any) -> tuple[str, Any] | None:
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes, bytearray)):
        return None
    for item in reversed(messages):
        if not isinstance(item, Mapping):
            continue
        if item.get("type") != "function_call_output":
            return None
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("Realtime function output has no call_id")
        return call_id, item.get("output", "")
    return None


async def call_openai_realtime_phone_api(
    messages,
    model,
    temperature,
    max_tokens,
    prompt,
    conversation_id,
    current_user,
    request,
    user_message=None,
    user_api_key=None,
    tools=None,
    input_token_fallback=None,
    pdf_error_metadata=None,
    prompt_id=None,
    watchdog_config=None,
    watchdog_hint_active=False,
    watchdog_hint_eval_id=None,
    llm_id=None,
    save_to_db: bool = True,
    web_search_mode=None,
    byok: bool = False,
    pending_attachment_refs=None,
    perf_trace=None,
    strip_device_action_blocks: bool = False,
    billing_reservation_id: str | None = None,
    reasoning_selection: ReasoningSelection | Mapping[str, Any] | str | None = None,
    realtime_bridge: RealtimeBridgeHandle | RealtimeTurnBridge | None = None,
):
    """Stream one speech-to-speech turn through Aurvek's provider contract."""

    del temperature, request, pdf_error_metadata, web_search_mode
    bridge_ref = realtime_bridge or _bridge_from_context()
    if not getattr(bridge_ref, "_aurvek_internal_realtime_bridge", False):
        raise RuntimeError("untrusted OpenAI Realtime bridge")

    key = user_api_key or openai_key
    if not isinstance(key, str) or not key.strip():
        yield _sse({"error": "OpenAI Realtime is not configured."})
        yield "data: [DONE]\n\n"
        return
    # The closure is resolved only by OpenAIRealtimeClient.connect and the
    # transport discards the returned key after the handshake.
    api_key_provider = lambda: key
    if isinstance(bridge_ref, RealtimeBridgeHandle):
        bridge = await bridge_ref.bind_provider(
            api_key_provider=api_key_provider, model=model
        )
    else:
        bridge = bridge_ref
    if billing_reservation_id:
        bridge.set_usage_uncertain_handler(
            lambda: mark_openai_realtime_usage_uncertain(
                reservation_id=billing_reservation_id,
                user_id=current_user.id,
            )
        )
    else:
        bridge.set_usage_uncertain_handler(None)

    effort = _reasoning_effort(reasoning_selection)
    realtime_tools = _realtime_tools(tools)
    output_cap: int | str = max_tokens or "inf"
    if isinstance(output_cap, int) and output_cap > 32_000:
        output_cap = 32_000

    continuation = _last_function_output(messages)
    is_continuation = bridge.started
    if is_continuation and continuation is None:
        yield _sse({"error": "Realtime continuation has no function output."})
        return

    content_parts: list[str] = []
    final_event: RealtimeDoneEvent | None = None
    error_event: RealtimeErrorEvent | None = None
    try:
        if is_continuation:
            await bridge.continue_function_call(*continuation)
        else:
            realtime_instructions = "\n\n".join(
                part
                for part in (str(prompt or "").strip(), _PHONE_TOOL_AUDIO_INSTRUCTION)
                if part
            )
            await bridge.start_turn(
                messages,
                instructions=realtime_instructions,
                tools=realtime_tools,
                tool_choice="auto" if realtime_tools else "none",
                reasoning_effort=effort,
                max_output_tokens=output_cap,
            )
        pending_tool: RealtimeToolCallEvent | None = None
        async for event in bridge.runtime_events():
            if isinstance(event, RealtimeTranscriptEvent):
                content_parts.append(event.text)
                yield _sse({"content": event.text})
            elif isinstance(event, RealtimeToolCallEvent):
                pending_tool = event
            elif isinstance(event, RealtimeStatusEvent):
                try:
                    if billing_reservation_id and save_to_db:
                        if event.response_usage is None:
                            await mark_openai_realtime_usage_uncertain(
                                reservation_id=billing_reservation_id,
                                user_id=current_user.id,
                            )
                        else:
                            await accumulate_openai_realtime_response_usage(
                                reservation_id=billing_reservation_id,
                                user_id=current_user.id,
                                prompt_id=prompt_id,
                                model=model,
                                response_id=event.response_id,
                                usage=event.response_usage,
                                byok=byok,
                            )
                except BaseException:
                    if billing_reservation_id:
                        await mark_openai_realtime_usage_uncertain(
                            reservation_id=billing_reservation_id,
                            user_id=current_user.id,
                        )
                    raise
                finally:
                    event.acknowledge_accounting()
                yield _sse(
                    {
                        "realtime_status": event.status,
                        "response_id": event.response_id,
                    }
                )
                if (
                    pending_tool is not None
                    and pending_tool.response_id == event.response_id
                    and event.status == "completed"
                ):
                    event = pending_tool
                    pending_tool = None
                else:
                    continue
                try:
                    parsed_arguments = json.loads(event.arguments or "{}")
                except json.JSONDecodeError:
                    parsed_arguments = {"_raw": event.arguments}
                await record_provider_success_for_label(
                    PROVIDER_LABEL, model=model, byok=byok
                )
                yield _sse(
                    {
                        "tool_call": {
                            "id": event.call_id,
                            "name": event.name,
                            "arguments": parsed_arguments,
                        },
                        "pre_tool_content": "".join(content_parts),
                    }
                )
                yield _sse({"tool_call_pending": True})
                return
            elif isinstance(event, RealtimeErrorEvent):
                error_event = event
            elif isinstance(event, RealtimeDoneEvent):
                final_event = event
    except Exception:
        error_event = RealtimeErrorEvent(
            "realtime_provider_error", "OpenAI Realtime request failed"
        )

    if error_event is not None or final_event is None or final_event.status != "completed":
        await record_provider_error_for_label(
            PROVIDER_LABEL,
            message=(error_event.message if error_event else "incomplete response"),
            model=model,
            byok=byok,
        )
        yield _sse(
            {
                "error": (
                    error_event.message
                    if error_event is not None
                    else "The realtime response did not complete."
                )
            }
        )
        yield "data: [DONE]\n\n"
        return

    # The bridge transcript spans all responses in a tool round-trip, whereas
    # this invocation may only have streamed the post-tool portion.
    content = final_event.transcript.strip() or "".join(content_parts).strip()
    usage = final_event.usage
    if not content:
        await record_provider_error_for_label(
            PROVIDER_LABEL,
            message="empty response",
            model=model,
            byok=byok,
        )
        yield _sse({"error": "The AI returned an empty response. Please try again."})
        yield "data: [DONE]\n\n"
        return

    if save_to_db:
        if perf_trace:
            trace_event = perf_trace.sse("db_save_start")
            if trace_event:
                yield trace_event
        user_message_id, bot_message_id = await save_content_to_db(
            content,
            usage.input_tokens,
            usage.output_tokens,
            usage.total_tokens,
            conversation_id,
            current_user.id,
            model,
            user_message=user_message,
            input_token_fallback=input_token_fallback,
            prompt_id=prompt_id,
            watchdog_config=watchdog_config,
            watchdog_hint_active=watchdog_hint_active,
            watchdog_hint_eval_id=watchdog_hint_eval_id,
            llm_id=llm_id,
            byok=byok,
            pending_attachment_refs=pending_attachment_refs,
            strip_device_action_blocks=strip_device_action_blocks,
            billing_reservation_id=billing_reservation_id,
            billing_only_accumulated_usage=bool(billing_reservation_id),
        )
        if perf_trace:
            trace_event = perf_trace.sse(
                "db_save_done",
                user_message_id=user_message_id,
                bot_message_id=bot_message_id,
            )
            if trace_event:
                yield trace_event
        if not user_message_id or not bot_message_id:
            yield _sse(persistence_error_payload())
            return
        yield _sse(
            {"message_ids": {"user": user_message_id, "bot": bot_message_id}}
        )
        await record_provider_success_for_label(
            PROVIDER_LABEL, model=model, byok=byok
        )
        yield content
    else:
        await record_provider_success_for_label(
            PROVIDER_LABEL, model=model, byok=byok
        )
        yield _sse(
            {
                "token_info": True,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            }
        )
        yield "data: [DONE]\n\n"
