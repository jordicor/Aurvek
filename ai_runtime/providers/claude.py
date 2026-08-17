from ai_runtime.dependencies import *
from ai_runtime.config import safe_log_headers, _log_truncated_response
from ai_runtime.errors import _extract_human_error_message, _human_exception_error, _provider_error_payload
from ai_runtime.persistence.messages import persistence_error_payload, save_content_to_db
from ai_runtime.providers.claude_capabilities import (
    claude_omits_temperature,
    claude_requires_adaptive_thinking,
    claude_supports_adaptive_thinking,
)
from ai_runtime.tooling.citations import build_citation_event
from ai_runtime.provider_health import record_provider_error_for_label, record_provider_success_for_label
from billing.usage_reservations import accumulate_ai_provider_call_usage

async def call_claude_api(messages, model, temperature, max_tokens, prompt, conversation_id, current_user, request, user_message=None, thinking_budget_tokens=None, user_api_key=None, tools=None,
                          input_token_fallback=None,
                          pdf_error_metadata=None,
                          prompt_id=None, watchdog_config=None, watchdog_hint_active=False, watchdog_hint_eval_id=None,
                          llm_id=None, save_to_db: bool = True, web_search_mode=None, byok: bool = False,
                          pending_attachment_refs: Optional[list[str]] = None,
                          strip_device_action_blocks: bool = False,
                          billing_reservation_id: str | None = None):
    global stop_signals
    logger.debug("Entering call_claude_api")

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

    model_lower = model.lower()
    model_max_tokens = int(max_tokens) if isinstance(max_tokens, (int, float)) else int(MAX_TOKENS)
    if model_max_tokens < 1:
        model_max_tokens = 1

    is_opus_adaptive_only = claude_requires_adaptive_thinking(model)
    is_adaptive_capable = claude_supports_adaptive_thinking(model)
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

    # Add native web search server tool when in native mode
    if web_search_mode == 'native':
        if "tools" not in data:
            data["tools"] = []
        data["tools"].append({
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5
        })

    # Add thinking mode for Claude models that support it (Claude 3.7, Claude 4)
    if thinking_budget_tokens:
        thinking_models = [
            "claude-3.7", "claude-3-7",
            "claude-4", "claude-sonnet-4", "claude-opus-4"
        ]

        if any(model_part in model_lower for model_part in thinking_models):
            if is_opus_adaptive_only:
                # Opus 4.7+ only supports adaptive; manual budget_tokens is rejected.
                if thinking_budget_tokens > 0:
                    logger.info(
                        "Opus 4.7+ does not accept manual thinking budget; "
                        "ignoring budget_tokens=%d and using adaptive.",
                        thinking_budget_tokens,
                    )
                # display defaults to "omitted" on Opus 4.7+, which would strip thinking text from
                # the stream and break the UI render. Force "summarized" to keep reasoning visible.
                data["thinking"] = {"type": "adaptive", "display": "summarized"}
            elif is_adaptive_capable and thinking_budget_tokens == -1:
                # Opus 4.6 / Sonnet 4.6 in Auto mode -> adaptive thinking (Claude decides budget)
                data["thinking"] = {"type": "adaptive"}
            elif thinking_budget_tokens > 0:
                # Manual budget for Claude 3.7 / 4.1 / 4.5 and legacy 4.6 manual overrides
                # Ensure max_tokens > budget_tokens (API requirement)
                anthropic_thinking_budget_min = 1024
                min_required_max_tokens = anthropic_thinking_budget_min + 1
                if thinking_budget_tokens < anthropic_thinking_budget_min:
                    logger.error(
                        "Manual thinking budget %d is below Anthropic's minimum of %d.",
                        thinking_budget_tokens,
                        anthropic_thinking_budget_min,
                    )
                    error_payload = {
                        "error": (
                            "Manual thinking budget must be at least "
                            f"{anthropic_thinking_budget_min} tokens (got {thinking_budget_tokens})."
                        )
                    }
                    yield f"data: {orjson.dumps(error_payload).decode()}\n\n"
                    return
                if data["max_tokens"] < min_required_max_tokens:
                    logger.error(
                        "Manual thinking requires max_tokens >= %d; got %d.",
                        min_required_max_tokens,
                        data["max_tokens"],
                    )
                    error_payload = {
                        "error": (
                            "Insufficient balance for extended thinking "
                            f"(need at least {min_required_max_tokens} output tokens, "
                            f"have {data['max_tokens']})."
                        )
                    }
                    yield f"data: {orjson.dumps(error_payload).decode()}\n\n"
                    return
                if thinking_budget_tokens >= data["max_tokens"]:
                    data["max_tokens"] = min(thinking_budget_tokens + 16384, model_max_tokens)
                # Final safety: if budget still >= max_tokens after cap, clamp budget
                actual_budget = min(thinking_budget_tokens, data["max_tokens"] - 1)
                data["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": actual_budget
                }
            # else: -1 on non-adaptive-capable model = no thinking (skip silently)

            if "thinking" in data:
                if not is_temperature_deprecated:
                    # Legacy models require temperature=1.0 when thinking is enabled
                    data["temperature"] = 1.0
                mode_label = 'adaptive' if data["thinking"].get("type") == "adaptive" else f'manual ({data["thinking"].get("budget_tokens")})'
                logger.info(f"Thinking mode: {mode_label} for {model}")

    #logger.debug(f"data: {data}")

    content = ""
    input_tokens = output_tokens = total_tokens = 0
    cache_creation_tokens = cache_read_tokens = 0

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
    current_block = None  # Currently open content block being streamed

    max_continuations = 3
    continuation_count = 0
    continuation_messages = list(messages)  # Don't mutate original

    async with aiohttp.ClientSession() as session:
        while True:
            # Update messages for continuation calls
            data["messages"] = continuation_messages

            try:
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
                                            # Handle regular text content
                                            elif delta_type == "text_delta" or "text" in delta:
                                                content_chunk = delta.get("text", "")
                                                if content_chunk:
                                                    content += content_chunk
                                                    if current_block and current_block.get("type") == "text":
                                                        current_block["text"] += content_chunk
                                                    yield f"data: {orjson.dumps({'content': content_chunk}).decode()}\n\n"
                                            elif delta_type == "citations_delta":
                                                # Citation attached to text during web search
                                                citation = delta.get("citation", {})
                                                if citation.get("type") == "web_search_result_location":
                                                    search_citations.append({
                                                        "url": citation.get("url", ""),
                                                        "title": citation.get("title", ""),
                                                        "cited_text": citation.get("cited_text", ""),
                                                    })

                                        elif event_type == "message_start":
                                            usage_info = event.get("message", {}).get("usage", {})
                                            # Accumulate input tokens across continuations
                                            input_tokens += usage_info.get("input_tokens", 0)
                                            cache_creation_tokens += usage_info.get("cache_creation_input_tokens", 0)
                                            cache_read_tokens += usage_info.get("cache_read_input_tokens", 0)

                                        elif event_type == "message_stop":
                                            break

                                        elif event_type == "message_delta":
                                            usage = event.get("usage", {})
                                            # Accumulate output tokens across continuations
                                            output_tokens += usage.get("output_tokens", 0)
                                            # Check stop_reason for tool_use
                                            delta = event.get("delta", {})
                                            stop_reason = delta.get("stop_reason", "")

                                        elif event_type == "content_block_start":
                                            content_block = event.get("content_block", {})
                                            block_type = content_block.get("type", "")
                                            block_index = event.get("index")
                                            block_types[block_index] = block_type

                                            # Initialize current_block for pause_turn continuation tracking
                                            if block_type == "text":
                                                current_block = {"type": "text", "text": ""}
                                            elif block_type == "thinking":
                                                current_block = {"type": "thinking", "thinking": ""}
                                                yield f"data: {orjson.dumps({'type': 'thinking_start'}).decode()}\n\n"
                                            elif block_type == "tool_use":
                                                # Regular function-call tool (generateImage, etc.)
                                                tool_use_name = content_block.get("name", "")
                                                tool_use_id = content_block.get("id", "")
                                                tool_use_input_buffer = ""
                                                current_block = {
                                                    "type": "tool_use",
                                                    "id": tool_use_id,
                                                    "name": tool_use_name,
                                                    "input": {}
                                                }
                                                logger.info(f"[call_claude_api] - Tool use started: {tool_use_name}")
                                            elif block_type == "server_tool_use":
                                                # Claude decided to search the web (server-side)
                                                server_tool_input_buffer = ""
                                                current_block = {
                                                    "type": "server_tool_use",
                                                    "id": content_block.get("id", ""),
                                                    "name": content_block.get("name", ""),
                                                    "input": {}
                                                }
                                                logger.info(f"[call_claude_api] - Server tool use started: {current_block['name']}")
                                            elif block_type == "web_search_tool_result":
                                                # Search results arrived - extract source URLs and preserve raw block
                                                search_content = content_block.get("content", [])
                                                for item in search_content:
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
                                            if stopped_block_type == "thinking":
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
                                        logger.error(f"[call_claude_api] - Error decoding JSON: {e}")
                                        logger.debug(f"[call_claude_api] - JSON data: {json_data}")
                                        continue
                    else:
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

            # Check if we need to continue (pause_turn = Claude needs more turns)
            if stop_reason == "pause_turn" and continuation_count < max_continuations:
                continuation_count += 1
                # Append full content blocks as assistant message (required for proper continuation)
                continuation_messages.append({
                    "role": "assistant",
                    "content": response_content_blocks
                })
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

    total_tokens = input_tokens + output_tokens
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
        yield f"data: {orjson.dumps({'tool_call': {'name': tool_use_name, 'arguments': parsed_args, 'id': tool_use_id, '_billing_usage': {'input_tokens': billing_input_tokens, 'output_tokens': billing_output_tokens}}, 'pre_tool_content': content}).decode()}\n\n"
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
                                                                        llm_id=llm_id, citations_json=citations_data, byok=byok, pending_attachment_refs=pending_attachment_refs,
                                                                        strip_device_action_blocks=strip_device_action_blocks,
                                                                        billing_reservation_id=billing_reservation_id,
                                                                        billing_only_accumulated_usage=bool(billing_reservation_id))
            if user_message_id and bot_message_id:
                yield f"data: {orjson.dumps({'message_ids': {'user': user_message_id, 'bot': bot_message_id}}).decode()}\n\n"
            else:
                yield f"data: {orjson.dumps(persistence_error_payload()).decode()}\n\n"
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
        yield f"data: {orjson.dumps({'token_info': True, 'input_tokens': input_tokens, 'output_tokens': output_tokens}).decode()}\n\n"
        yield "data: [DONE]\n\n"
