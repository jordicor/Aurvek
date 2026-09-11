from ai_runtime.dependencies import *
from ai_runtime.config import safe_log_headers, _log_truncated_response
from ai_runtime.errors import _extract_human_error_message, _human_exception_error, _provider_error_payload
from ai_runtime.persistence.messages import persistence_error_payload, persistence_result_payload, save_content_to_db
from ai_runtime.reasoning_tags import TaggedThinkingStreamParser
from ai_runtime.providers.claude_capabilities import (
    claude_omits_temperature,
    claude_supports_adaptive_thinking,
)
from ai_runtime.tooling.citations import build_citation_event
from ai_runtime.provider_health import record_provider_error_for_label, record_provider_success_for_label
from billing.usage_reservations import (
    accumulate_ai_provider_call_usage,
    accumulate_ai_reservation_usage,
)
from billing.hosted_search import prepare_hosted_search
from billing.usage_reservations import BillingReservationError
from ai_runtime.reasoning import (
    ReasoningSelection,
    parse_reasoning_selection,
    selection_from_legacy_thinking_budget,
)


def _claude_cache_creation_usage(usage: dict) -> tuple[int, int]:
    """Split the provider's cache writes; legacy aggregate usage has a 5m TTL."""
    total = int(usage.get("cache_creation_input_tokens") or 0)
    detail = usage.get("cache_creation") or {}
    one_hour = int(detail.get("ephemeral_1h_input_tokens") or 0)
    five_minutes = int(detail.get("ephemeral_5m_input_tokens", total - one_hour))
    if min(total, one_hour, five_minutes) < 0 or five_minutes + one_hour != total:
        raise BillingReservationError("Claude cache usage is inconsistent")
    return five_minutes, one_hour


def _merge_claude_stream_usage(accumulated: dict, latest: dict) -> None:
    """Merge cumulative counters without retaining an obsolete cache split.

    Server tools add automatic 5m cache writes. Their final streaming delta
    can update the aggregate while omitting the TTL breakdown from start.
    Explicit 1h writes remain unchanged; infer the updated 5m remainder.
    https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-use-with-prompt-caching
    """
    if ("cache_creation_input_tokens" in latest and "cache_creation" not in latest
            and latest["cache_creation_input_tokens"] != accumulated.get("cache_creation_input_tokens")):
        breakdown = accumulated.get("cache_creation")
        if breakdown:
            accumulated["cache_creation"] = {
                key: value for key, value in breakdown.items()
                if key != "ephemeral_5m_input_tokens"
            }
    accumulated.update(latest)


def _claude_cache_api_cost(
    *, model: str, uncached_input: int, output: int,
    cache_read: int, cache_write_5m: int, cache_write_1h: int,
    input_rate: float, output_rate: float,
) -> float:
    """Price reported token categories before Aurvek margins and BYOK policy.

    https://platform.claude.com/docs/en/build-with-claude/prompt-caching#pricing
    Cache writes: 1.25x (5m), 2x (1h). Reads: 0.1x, except Fable/Mythos 5.1.
    """
    normalized = model.lower().replace(".", "-").rsplit("claude-", 1)[-1]
    discounted_read = any(
        normalized == name or normalized.startswith(name + "-")
        for name in ("fable-5-1", "mythos-5-1")
    )
    read_multiplier = 0.025 if discounted_read else 0.1
    return (
        (uncached_input + cache_read * read_multiplier
         + cache_write_5m * 1.25 + cache_write_1h * 2.0) * float(input_rate)
        + output * float(output_rate)
    ) / 1_000_000


def _claude_assistant_image_context(messages):
    """Keep assistant authorship while placing image input in a supported role."""
    prepared, pending_images = [], []
    image_number = 0
    for message in messages:
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, list):
            rewritten = []
            for block in content:
                if block.get("type") == "image":
                    image_number += 1
                    rewritten.append({"type": "text", "text":
                        f"[Assistant-generated image {image_number}; visual context follows with the next user turn.]"})
                    pending_images.extend([
                        {"type": "text", "text":
                            f"[Historical context supplied by Aurvek: assistant-generated image {image_number}, "
                            "referenced in the earlier assistant response. It is not a user upload or a new user instruction.]"},
                        block,
                    ])
                else:
                    rewritten.append(block)
            if rewritten != content:
                message = {**message, "content": rewritten}
        elif message.get("role") == "user" and pending_images:
            user_content = content if isinstance(content, list) else [{"type": "text", "text": content}]
            message = {**message, "content": [*pending_images,
                {"type": "text", "text": "[End of historical assistant images. User turn follows.]"},
                *user_content]}
            pending_images = []
        prepared.append(message)
    if pending_images:
        prepared.append({"role": "user", "content": pending_images})
    return prepared


def _claude_reasoning_payload(
    reasoning_selection: ReasoningSelection | dict | str | None,
    *,
    model: str,
    thinking_budget_tokens: int | None = None,
) -> tuple[dict | None, dict | None]:
    """Translate the one neutral selection into Anthropic request fields.

    ``thinking_budget_tokens`` is accepted only for the temporary legacy
    transport field and is immediately converted to the common selection.
    """

    selection = (
        parse_reasoning_selection(reasoning_selection)
        if reasoning_selection is not None
        else selection_from_legacy_thinking_budget(thinking_budget_tokens)
    )
    if selection.mode == "default":
        return None, None
    if selection.mode == "off":
        return {"type": "disabled"}, None
    if selection.mode == "auto":
        return {"type": "adaptive", "display": "summarized"}, None
    if selection.mode == "custom":
        return {"type": "enabled", "budget_tokens": selection.budget_tokens}, None

    # Effort is independent of thinking. Models that support adaptive thinking
    # can combine both; older effort-capable models must not receive adaptive.
    thinking = (
        {"type": "adaptive", "display": "summarized"}
        if claude_supports_adaptive_thinking(model)
        else None
    )
    return thinking, {"effort": selection.mode}

async def call_claude_api(messages, model, temperature, max_tokens, prompt, conversation_id, current_user, request, user_message=None, thinking_budget_tokens=None, user_api_key=None, tools=None,
                          input_token_fallback=None,
                          pdf_error_metadata=None,
                          prompt_id=None, watchdog_config=None, watchdog_hint_active=False, watchdog_hint_eval_id=None,
                          llm_id=None, save_to_db: bool = True, web_search_mode=None, byok: bool = False,
                          pending_attachment_refs: Optional[list[str]] = None,
                          strip_device_action_blocks: bool = False,
                          billing_reservation_id: str | None = None,
                          reasoning_selection: ReasoningSelection | dict | str | None = None,
                          tool_choice: dict | None = None):
    global stop_signals
    logger.debug("Entering call_claude_api")

    messages = _claude_assistant_image_context(messages)
    user_id = current_user.id
    error_yielded = False

    # Use user's API key if provided, otherwise use default
    api_key_to_use = user_api_key or anthropic.api_key

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key_to_use,
        "anthropic-version": "2023-06-01"
    }

    model_max_tokens = int(max_tokens) if isinstance(max_tokens, (int, float)) else int(MAX_TOKENS)
    if model_max_tokens < 1:
        model_max_tokens = 1

    is_temperature_deprecated = claude_omits_temperature(model)

    data = {
        "model": model,
        "max_tokens": model_max_tokens,
        "system": [{
            "type": "text",
            "text": prompt,
            "cache_control": {"type": "ephemeral"}
        }],
        "messages": messages,
        "stream": True
    }
    if not is_temperature_deprecated:
        data["temperature"] = temperature

    # Shallow copy to avoid mutating the caller's list when appending server tools below
    if tools:
        data["tools"] = list(tools)
        if tool_choice is not None:
            data["tool_choice"] = tool_choice

    # Add native web search server tool when in native mode
    if web_search_mode == 'native':
        if "tools" not in data:
            data["tools"] = []
        data["tools"].append({
            "type": "web_search_20250305",
            "name": "web_search",
            # The configured bound and its reservation are resolved immediately
            # before each request, including pause_turn continuations.
            "max_uses": 5
        })

    thinking, output_config = _claude_reasoning_payload(
        reasoning_selection,
        model=model,
        thinking_budget_tokens=thinking_budget_tokens,
    )
    if thinking is not None:
        if thinking.get("type") == "enabled" and thinking["budget_tokens"] >= data["max_tokens"]:
            yield f"data: {orjson.dumps({'error': 'Claude thinking budget_tokens must be less than max_tokens'}).decode()}\n\n"
            return
        data["thinking"] = thinking
        if thinking.get("type") != "disabled" and not is_temperature_deprecated:
            # Anthropic legacy thinking requests require this fixed temperature.
            data["temperature"] = 1.0
        logger.info("Thinking mode: %s for %s", thinking["type"], model)
    if output_config is not None:
        data["output_config"] = output_config

    #logger.debug(f"data: {data}")

    content = ""
    tagged_thinking_parser = TaggedThinkingStreamParser()
    input_tokens = output_tokens = total_tokens = 0
    cache_creation_tokens = cache_read_tokens = 0
    cache_write_5m_tokens = cache_write_1h_tokens = 0

    # Tool use tracking
    tool_use_name = ""
    tool_use_id = ""
    tool_use_input_buffer = ""
    stop_reason = ""

    # Native web search tracking
    block_types = {}  # Maps block index -> block type string
    search_citations = []  # Accumulated citations from web search
    search_queries = []  # Queries Claude executed
    search_source_urls = []  # Source URLs from web_search_tool_result blocks
    all_citations = []  # Final merged citations for persistence
    server_tool_input_buffer = ""  # Buffer for server tool use input (search query)
    response_content_blocks = []  # Full content blocks for pause_turn continuation
    paused_content_blocks = []  # Earlier pieces of this same assistant turn
    current_block = None  # Currently open content block being streamed

    max_continuations = 3
    continuation_count = 0
    continuation_messages = list(messages)  # Don't mutate original
    unresolved_search_billing = False

    async with aiohttp.ClientSession() as session:
        while True:
            # Update messages for continuation calls
            data["messages"] = continuation_messages
            search_usage = None
            search_usage_complete = False
            search_final_count_reported = False
            search_request_rejected = False
            search_activity_seen = False
            search_stream_valid = True
            message_stopped = False
            request_usage = {}

            try:
                if web_search_mode == "native":
                    search_usage = await prepare_hosted_search(
                        "anthropic", user_id, byok=bool(byok or user_api_key)
                    )
                    data["tools"][-1]["max_uses"] = search_usage.max_uses
                    await search_usage.claim()
                async with session.post(url, headers=headers, json=data) as response:
                    if response.status == 200:
                        async for line in response.content:
                            if stop_signals.get(conversation_id):
                                logger.info("Stop signal received, exiting Claude API call loop.")
                                break

                            if line:
                                #logger.debug(f"line-> {line}")
                                line = line.decode("utf-8").strip()
                                if line[:7] == "data: {":
                                    json_data = line[6:]
                                    try:
                                        event = orjson.loads(json_data)
                                        event_type = event["type"]

                                        if event_type == "content_block_delta":
                                            delta = event.get("delta", {})
                                            delta_type = delta.get("type", "")
                                            block_index = event.get("index")
                                            current_block_type = block_types.get(block_index, "")

                                            if delta_type == "input_json_delta":
                                                partial_json = delta.get("partial_json", "")
                                                if current_block_type == "tool_use":
                                                    # Regular function-call tool input
                                                    tool_use_input_buffer += partial_json
                                                elif current_block_type == "server_tool_use":
                                                    # Server tool input (search query) - accumulate to extract query
                                                    server_tool_input_buffer += partial_json
                                            # Handle thinking tokens
                                            elif delta_type == "thinking_delta" and "thinking" in delta:
                                                thinking_chunk = delta["thinking"]
                                                if current_block and current_block.get("type") == "thinking":
                                                    current_block["thinking"] += thinking_chunk
                                                yield f"data: {orjson.dumps({'thinking': thinking_chunk, 'type': 'thinking'}).decode()}\n\n"
                                            elif delta_type == "signature_delta" and current_block:
                                                # Anthropic signs thinking blocks; the exact value must be
                                                # carried into a tool continuation with the original block.
                                                current_block["signature"] = delta.get("signature", "")
                                            # Handle regular text content
                                            elif delta_type == "text_delta" or "text" in delta:
                                                content_chunk = delta.get("text", "")
                                                if content_chunk:
                                                    for tagged_type, tagged_chunk in tagged_thinking_parser.feed(content_chunk):
                                                        if tagged_type == "thinking_start":
                                                            yield f"data: {orjson.dumps({'type': 'thinking_start'}).decode()}\n\n"
                                                        elif tagged_type == "thinking":
                                                            yield f"data: {orjson.dumps({'thinking': tagged_chunk, 'type': 'thinking'}).decode()}\n\n"
                                                        elif tagged_type == "thinking_end":
                                                            yield f"data: {orjson.dumps({'type': 'thinking_end'}).decode()}\n\n"
                                                        elif tagged_chunk:
                                                            content += tagged_chunk
                                                            if current_block and current_block.get("type") == "text":
                                                                current_block["text"] += tagged_chunk
                                                            yield f"data: {orjson.dumps({'content': tagged_chunk}).decode()}\n\n"
                                            elif delta_type == "citations_delta":
                                                # Citation attached to text during web search
                                                citation = delta.get("citation", {})
                                                if citation.get("type") == "web_search_result_location":
                                                    search_activity_seen = True
                                                    search_citations.append({
                                                        "url": citation.get("url", ""),
                                                        "title": citation.get("title", ""),
                                                        "cited_text": citation.get("cited_text", ""),
                                                    })

                                        elif event_type == "message_start":
                                            usage_info = event.get("message", {}).get("usage", {})
                                            server_usage = usage_info.get("server_tool_use") or {}
                                            if search_usage and "web_search_requests" in server_usage:
                                                search_usage.observe_usage(server_usage["web_search_requests"])
                                            request_usage.update(usage_info)

                                        elif event_type == "message_stop":
                                            search_usage_complete = True
                                            message_stopped = True
                                            break

                                        elif event_type == "message_delta":
                                            usage = event.get("usage", {})
                                            server_usage = usage.get("server_tool_use") or {}
                                            if search_usage and "web_search_requests" in server_usage:
                                                search_usage.observe_usage(server_usage["web_search_requests"])
                                                search_final_count_reported = True
                                            # Each event reports cumulative usage for this request.
                                            _merge_claude_stream_usage(request_usage, usage)
                                            # Check stop_reason for tool_use
                                            delta = event.get("delta", {})
                                            stop_reason = delta.get("stop_reason", "")
                                            # A terminal delta contains the final cumulative
                                            # usage even if the client leaves before message_stop.
                                            if stop_reason:
                                                search_usage_complete = True

                                        elif event_type == "content_block_start":
                                            content_block = event.get("content_block", {})
                                            block_type = content_block.get("type", "")
                                            if block_type in {"server_tool_use", "web_search_tool_result"}:
                                                search_activity_seen = True
                                            block_index = event.get("index")
                                            block_types[block_index] = block_type

                                            # Initialize current_block for pause_turn continuation tracking
                                            if block_type == "text":
                                                current_block = {**content_block, "text": ""}
                                            elif block_type in {"thinking", "redacted_thinking"}:
                                                # Preserve provider-signed and opaque thinking fields for a
                                                # subsequent tool continuation.
                                                current_block = dict(content_block)
                                                if block_type == "thinking":
                                                    current_block.setdefault("thinking", "")
                                                yield f"data: {orjson.dumps({'type': 'thinking_start'}).decode()}\n\n"
                                            elif block_type == "tool_use":
                                                # Regular function-call tool (generateImage, etc.)
                                                tool_use_name = content_block.get("name", "")
                                                tool_use_id = content_block.get("id", "")
                                                tool_use_input_buffer = ""
                                                current_block = {**content_block, "input": {}}
                                                logger.info(f"[call_claude_api] - Tool use started: {tool_use_name}")
                                            elif block_type == "server_tool_use":
                                                # Claude decided to search the web (server-side)
                                                server_tool_input_buffer = ""
                                                current_block = {**content_block, "input": {}}
                                                logger.info(f"[call_claude_api] - Server tool use started: {current_block['name']}")
                                            elif block_type == "web_search_tool_result":
                                                # Search results arrived - extract source URLs and preserve raw block
                                                search_content = content_block.get("content", [])
                                                for item in search_content if isinstance(search_content, list) else []:
                                                    if item.get("type") == "web_search_result":
                                                        search_source_urls.append({
                                                            "url": item.get("url", ""),
                                                            "title": item.get("title", ""),
                                                            "page_age": item.get("page_age", "")
                                                        })
                                                # Preserve the full block for continuation (includes encrypted_content)
                                                current_block = {
                                                    "type": "web_search_tool_result",
                                                    "tool_use_id": content_block.get("tool_use_id", ""),
                                                    "content": search_content
                                                }
                                                logger.info(f"[call_claude_api] - Web search results: {len(search_source_urls)} sources")
                                            continue

                                        elif event_type == "content_block_stop":
                                            block_index = event.get("index")
                                            stopped_block_type = block_types.get(block_index, "")
                                            if stopped_block_type in {"thinking", "redacted_thinking"}:
                                                yield f"data: {orjson.dumps({'type': 'thinking_end'}).decode()}\n\n"
                                            elif stopped_block_type == "tool_use":
                                                # Finalize regular tool block with parsed input
                                                if current_block and tool_use_input_buffer:
                                                    try:
                                                        current_block["input"] = orjson.loads(tool_use_input_buffer)
                                                    except orjson.JSONDecodeError:
                                                        pass
                                            elif stopped_block_type == "server_tool_use":
                                                # Extract search query from accumulated input
                                                if server_tool_input_buffer:
                                                    try:
                                                        search_input = orjson.loads(server_tool_input_buffer)
                                                        if current_block:
                                                            current_block["input"] = search_input
                                                        query = search_input.get("query", "")
                                                        if query:
                                                            search_queries.append(query)
                                                            logger.info(f"[call_claude_api] - Web search query: {query}")
                                                    except orjson.JSONDecodeError:
                                                        logger.warning(f"[call_claude_api] - Failed to parse search query: {server_tool_input_buffer}")
                                                yield f"data: {orjson.dumps({'content': '', 'searching': True}).decode()}\n\n"
                                                server_tool_input_buffer = ""
                                            # Save completed block for pause_turn continuation
                                            if current_block:
                                                response_content_blocks.append(current_block)
                                                current_block = None
                                            continue

                                    except orjson.JSONDecodeError as e:
                                        search_stream_valid = False
                                        logger.error(f"[call_claude_api] - Error decoding JSON: {e}")
                                        logger.debug(f"[call_claude_api] - JSON data: {json_data}")
                                        continue
                    else:
                        # These responses reject the request before model/tool
                        # work. Timeouts and server errors remain ambiguous.
                        search_request_rejected = response.status in {400, 401, 403, 404, 413, 422, 429}
                        error_body = await response.text()
                        raw_log = f"[call_claude_api] - Error: Received status code {response.status}. Response body: {error_body}"
                        logger.error(raw_log)
                        logger.error(f"Request headers: {safe_log_headers(headers)}")
                        logger.error(f"Request context: model={data.get('model', '?')}, "
                                     f"messages={len(data.get('messages', []))}, "
                                     f"conversation_id={conversation_id}")
                        human_msg = _extract_human_error_message(error_body, response.status, "Claude")
                        await record_provider_error_for_label(
                            "Claude",
                            message=human_msg,
                            status_code=response.status,
                            model=model,
                            byok=byok,
                        )
                        yield f"data: {orjson.dumps(_provider_error_payload('Claude', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
                        error_yielded = True
                        break  # Don't continue on error
            except BillingReservationError as exc:
                yield f"data: {orjson.dumps({'error': str(exc), 'error_code': 'hosted_search_billing_unavailable'}).decode()}\n\n"
                error_yielded = True
                break
            except asyncio.TimeoutError as exc:
                error_msg = f"[call_claude_api] - Request timed out for conversation {conversation_id}"
                logger.error(error_msg)
                human_msg = _human_exception_error(exc, "Claude")
                await record_provider_error_for_label("Claude", message=human_msg, exception=exc, model=model, byok=byok)
                yield f"data: {orjson.dumps(_provider_error_payload('Claude', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
                error_yielded = True
                break
            except aiohttp.ClientError as exc:
                error_msg = f"[call_claude_api] - Connection error: {str(exc)}"
                logger.error(error_msg)
                human_msg = _human_exception_error(exc, "Claude")
                await record_provider_error_for_label("Claude", message=human_msg, exception=exc, model=model, byok=byok)
                yield f"data: {orjson.dumps(_provider_error_payload('Claude', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
                error_yielded = True
                break
            except Exception as exc:
                error_msg = f"[call_claude_api] - Unexpected error: {str(exc)}"
                logger.error(error_msg)
                human_msg = _human_exception_error(exc, "Claude")
                await record_provider_error_for_label("Claude", message=human_msg, exception=exc, model=model, byok=byok)
                yield f"data: {orjson.dumps(_provider_error_payload('Claude', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
                error_yielded = True
                break
            finally:
                # Sum distinct pause_turn requests once, not cumulative SSE deltas.
                input_tokens += int(request_usage.get("input_tokens") or 0)
                output_tokens += int(request_usage.get("output_tokens") or 0)
                cache_read_tokens += int(request_usage.get("cache_read_input_tokens") or 0)
                write_5m, write_1h = _claude_cache_creation_usage(request_usage)
                cache_write_5m_tokens += write_5m
                cache_write_1h_tokens += write_1h
                cache_creation_tokens += write_5m + write_1h
                if search_usage is not None:
                    # Anthropic can omit server-tool usage for a text-only
                    # response. A complete stream with no search activity proves
                    # zero; initial counters or an interrupted stream do not.
                    if (message_stopped and search_stream_valid and not search_activity_seen
                            and search_usage.count in (None, 0)):
                        search_usage.observe_usage(0)
                        search_final_count_reported = True
                    resolved = await search_usage.finish(
                        complete=search_usage_complete and search_final_count_reported,
                        rejected=search_request_rejected,
                    )
                    unresolved_search_billing = unresolved_search_billing or not resolved

            # Check if we need to continue (pause_turn = Claude needs more turns)
            if unresolved_search_billing:
                break
            if stop_reason == "pause_turn" and continuation_count < max_continuations:
                continuation_count += 1
                # Append full content blocks as assistant message (required for proper continuation)
                continuation_messages.append({
                    "role": "assistant",
                    "content": response_content_blocks
                })
                paused_content_blocks.extend(response_content_blocks)
                # Reset per-iteration state (keep accumulated content, tokens, citations)
                stop_reason = ""
                block_types = {}
                response_content_blocks = []
                current_block = None
                logger.info(f"[call_claude_api] - pause_turn continuation {continuation_count}/{max_continuations}")
                continue
            else:
                if stop_reason == "pause_turn":
                    logger.warning(f"[call_claude_api] - Max continuations ({max_continuations}) reached, stopping")
                break

    for tagged_type, tagged_chunk in tagged_thinking_parser.finalize():
        if tagged_type == "thinking":
            yield f"data: {orjson.dumps({'thinking': tagged_chunk, 'type': 'thinking'}).decode()}\n\n"
        elif tagged_type == "thinking_end":
            yield f"data: {orjson.dumps({'type': 'thinking_end'}).decode()}\n\n"
        elif tagged_type == "content" and tagged_chunk:
            content += tagged_chunk
            yield f"data: {orjson.dumps({'content': tagged_chunk}).decode()}\n\n"

    uncached_input_tokens = input_tokens
    input_tokens += cache_creation_tokens + cache_read_tokens
    total_tokens = input_tokens + output_tokens
    override_api_cost = None
    if cache_creation_tokens or cache_read_tokens:
        input_cost, output_cost = await get_llm_token_costs(model=model, llm_id=llm_id)
        override_api_cost = 0.0 if byok else _claude_cache_api_cost(
            model=model, uncached_input=uncached_input_tokens, output=output_tokens,
            cache_read=cache_read_tokens, cache_write_5m=cache_write_5m_tokens,
            cache_write_1h=cache_write_1h_tokens,
            input_rate=input_cost, output_rate=output_cost,
        )
    logger.info(f"Tokens used Claude:\ninput_tokens: {input_tokens}\noutput_tokens: {output_tokens}\ntotal_tokens: {total_tokens}")
    logger.info(f"Cache tokens used:\ncache_creation_tokens: {cache_creation_tokens}\ncache_read_tokens: {cache_read_tokens}")
    if stop_reason == "max_tokens":
        _log_truncated_response("Claude", model, conversation_id, llm_id, stop_reason, data.get("max_tokens"))

    billing_input_tokens = input_tokens
    billing_output_tokens = output_tokens
    if (
        billing_reservation_id
        and save_to_db
        and (content or tool_use_name or input_tokens or output_tokens)
    ):
        if override_api_cost is not None:
            # Retain real token totals for usage/creator markups, and the exact
            # category-priced API cost for normal margin/BYOK settlement.
            await accumulate_ai_reservation_usage(
                reservation_id=billing_reservation_id, user_id=user_id,
                input_tokens=input_tokens, output_tokens=output_tokens,
                component={
                    "input_tokens": input_tokens, "output_tokens": output_tokens,
                    "input_cost_per_million": input_cost,
                    "output_cost_per_million": output_cost,
                    "override_api_cost": override_api_cost,
                    "prompt_id": prompt_id, "byok": byok,
                },
            )
        else:
            billing_input_tokens, billing_output_tokens = (
                await accumulate_ai_provider_call_usage(
                    reservation_id=billing_reservation_id,
                    user_id=user_id,
                    reported_input_tokens=input_tokens,
                    reported_output_tokens=output_tokens,
                    input_payload=(prompt, messages),
                    output_payload=(content, tool_use_name, tool_use_input_buffer),
                    input_token_fallback=input_token_fallback,
                    output_token_cap=max_tokens,
                    llm_id=llm_id,
                    model=model,
                    prompt_id=prompt_id,
                    byok=byok,
                )
            )

    if unresolved_search_billing:
        yield f"data: {orjson.dumps({'error': 'Hosted search usage could not be confirmed; its reservation is retained for reconciliation.', 'error_code': 'hosted_search_usage_unconfirmed'}).decode()}\n\n"
        return

    # If a tool use was detected, emit it and return without saving to DB
    # The caller (get_ai_response) will handle the tool call and save the result
    # When save_to_db=False (Multi-AI), skip tool handling entirely
    if tool_use_name and (stop_reason == "tool_use" or tool_use_input_buffer) and save_to_db:
        try:
            # Parse the accumulated input as JSON
            parsed_args = orjson.loads(tool_use_input_buffer) if tool_use_input_buffer else {}
        except orjson.JSONDecodeError:
            logger.error(f"[call_claude_api] - Failed to parse tool input: {tool_use_input_buffer}")
            parsed_args = {}

        logger.info(f"[call_claude_api] - Tool use detected: {tool_use_name}, pre_tool_content length: {len(content)}")

        # Include any text Claude generated before calling the tool
        await record_provider_success_for_label("Claude", model=model, byok=byok)
        yield f"data: {orjson.dumps({'tool_call': {'name': tool_use_name, 'arguments': parsed_args, 'id': tool_use_id, 'claude_content_blocks': [*paused_content_blocks, *response_content_blocks], '_billing_usage': {'input_tokens': billing_input_tokens, 'output_tokens': billing_output_tokens}}, 'pre_tool_content': content}).decode()}\n\n"
        yield f"data: {orjson.dumps({'tool_call_pending': True}).decode()}\n\n"
        return  # Don't save to DB - handler will do it

    # Emit native web search citations if any were collected
    if search_citations or search_source_urls:
        # Merge source URLs with citations - some sources may not have been cited inline
        all_citations = list(search_citations)  # Citations with position info
        # Add source URLs that weren't already in citations
        cited_urls = {c["url"] for c in all_citations}
        for source in search_source_urls:
            if source["url"] not in cited_urls:
                all_citations.append({
                    "url": source["url"],
                    "title": source["title"],
                })
        yield build_citation_event(all_citations, search_queries if search_queries else None)

    # Normal response - save to database
    if save_to_db:
        was_stopped = stop_signals.get(conversation_id, False)
        if not content.strip():
            if was_stopped:
                logger.info(f"User stopped stream before content for conversation {conversation_id}. Skipping save.")
            else:
                logger.warning(f"Empty bot response for conversation {conversation_id}, user {user_id}. "
                               f"Provider: claude. Not saving to DB.")
                if not error_yielded:
                    await record_provider_error_for_label("Claude", message="empty response", model=model, byok=byok)
                    empty_msg = "The AI returned an empty response. Please try again."
                    yield f"data: {orjson.dumps(_provider_error_payload('Claude', empty_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            return
        else:
            await record_provider_success_for_label("Claude", model=model, byok=byok)
            citations_data = orjson.dumps(all_citations).decode() if all_citations else None
            user_message_id, bot_message_id = await save_content_to_db(content, input_tokens, output_tokens, total_tokens, conversation_id, user_id, model, user_message=user_message,
                                                                        input_token_fallback=input_token_fallback,
                                                                        prompt_id=prompt_id, watchdog_config=watchdog_config, watchdog_hint_active=watchdog_hint_active, watchdog_hint_eval_id=watchdog_hint_eval_id,
                                                                        llm_id=llm_id, citations_json=citations_data, byok=byok, override_api_cost=override_api_cost, pending_attachment_refs=pending_attachment_refs,
                                                                        strip_device_action_blocks=strip_device_action_blocks,
                                                                        billing_reservation_id=billing_reservation_id,
                                                                        billing_only_accumulated_usage=bool(billing_reservation_id))
            persisted = persistence_result_payload(user_message_id, bot_message_id)
            yield f"data: {orjson.dumps(persisted).decode()}\n\n"
            if persisted.get("persistence_error"):
                return

        yield content.strip()
    else:
        if content.strip():
            await record_provider_success_for_label("Claude", model=model, byok=byok)
        elif not error_yielded:
            await record_provider_error_for_label("Claude", message="empty response", model=model, byok=byok)
            empty_msg = "The AI returned an empty response. Please try again."
            yield f"data: {orjson.dumps(_provider_error_payload('Claude', empty_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            yield "data: [DONE]\n\n"
            return
        yield f"data: {orjson.dumps({'token_info': True, 'input_tokens': input_tokens, 'output_tokens': output_tokens, **({'override_api_cost': override_api_cost} if override_api_cost is not None else {})}).decode()}\n\n"
        yield "data: [DONE]\n\n"
