from ai_runtime.dependencies import *
from ai_runtime.config import _is_gpt5_model, safe_log_headers, _log_truncated_response
from ai_runtime.errors import _extract_human_error_message, _human_exception_error, _provider_error_payload
from ai_runtime.persistence.messages import persistence_error_payload, save_content_to_db
from ai_runtime.reasoning_tags import TaggedThinkingStreamParser
from ai_runtime.provider_health import record_provider_error_for_label, record_provider_success_for_label
from billing.usage_reservations import accumulate_ai_provider_call_usage
from ai_runtime.reasoning import ReasoningSelection, parse_reasoning_selection


def _chat_reasoning_effort(
    reasoning_selection: ReasoningSelection | dict | str | None,
) -> str | None:
    """Translate selections supported by Chat Completions' reasoning_effort."""

    if reasoning_selection is None:
        return None
    selection = parse_reasoning_selection(reasoning_selection)
    if selection.mode == "default":
        return None
    if selection.mode == "off":
        return "none"
    if selection.mode in {"minimal", "low", "medium", "high", "xhigh"}:
        return selection.mode
    raise ValueError(f"OpenAI Chat does not support reasoning mode {selection.mode!r}")

async def call_o1_api(messages, model, temperature, max_tokens, prompt, conversation_id, current_user, request, user_message=None, user_api_key=None,
                      input_token_fallback=None,
                      pdf_error_metadata=None,
                      prompt_id=None, watchdog_config=None, watchdog_hint_active=False, watchdog_hint_eval_id=None,
                      llm_id=None, save_to_db: bool = True, web_search_mode=None, byok: bool = False,
                      pending_attachment_refs: Optional[list[str]] = None,
                      strip_device_action_blocks: bool = False,
                      billing_reservation_id: str | None = None,
                      reasoning_selection: ReasoningSelection | dict | str | None = None):
    global stop_signals
    logger.debug("enters call_o1_api")

    user_id = current_user.id
    error_yielded = False

    # Use user's API key if provided
    api_key_to_use = user_api_key or openai.api_key

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key_to_use}"
    }

    # Keep server policy and trusted transport metadata above user content.
    api_messages = [{"role": "developer", "content": prompt}]

    # Add message history
    for msg in messages:
        if msg['role'] != 'system':  # Avoid duplicating system message
            api_messages.append(msg)

    data = {
        "model": model,
        "messages": api_messages
        # "o1" doesn't support 'stream' parameter
    }
    reasoning_effort = _chat_reasoning_effort(reasoning_selection)
    if reasoning_effort is not None:
        data["reasoning_effort"] = reasoning_effort

    content = ""
    input_tokens = output_tokens = total_tokens = 0
    reasoning_tokens = 0

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, headers=headers, json=data) as response:
                if response.status == 200:
                    response_json = await response.json()
                    logger.debug(f"call_o1_api -> response keys: {list(response_json.keys())}")

                    # Extract assistant response
                    if 'choices' in response_json and response_json['choices']:
                        assistant_message = response_json['choices'][0]['message']['content']
                        content = assistant_message

                        # Simulate streaming by splitting response into sentences
                        sentences = re.split('(?<=[.!?]) +', content)
                        for sentence in sentences:
                            if stop_signals.get(conversation_id):
                                logger.info("Stop signal received, exiting o1 API call loop.")
                                break
                            yield f"data: {orjson.dumps({'content': sentence.strip()}).decode()}\n\n"
                            await asyncio.sleep(0.1)  # Small pause to simulate streaming

                        # Extract token usage
                        usage = response_json.get('usage', {})
                        input_tokens = usage.get('prompt_tokens', 0)
                        output_tokens = usage.get('completion_tokens', 0)
                        total_tokens = usage.get('total_tokens', 0)
                        reasoning_tokens = usage.get('completion_tokens_details', {}).get('reasoning_tokens', 0)

                    else:
                        logger.error("[call_o1_api] - OpenAI (o1) response had no choices array")
                        empty_msg = "OpenAI (o1) returned an empty response. Please try again."
                        await record_provider_error_for_label("OpenAI (o1)", message="empty response", model=model, byok=byok)
                        yield f"data: {orjson.dumps(_provider_error_payload('OpenAI (o1)', empty_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
                        error_yielded = True
                else:
                    error_body = await response.text()
                    raw_log = f"[call_o1_api] - Error: Received status code {response.status}. Response body: {error_body}"
                    logger.error(raw_log)
                    human_msg = _extract_human_error_message(error_body, response.status, "OpenAI (o1)")
                    await record_provider_error_for_label(
                        "OpenAI (o1)",
                        message=human_msg,
                        status_code=response.status,
                        model=model,
                        byok=byok,
                    )
                    yield f"data: {orjson.dumps(_provider_error_payload('OpenAI (o1)', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
                    error_yielded = True
        except asyncio.TimeoutError as exc:
            error_msg = f"[call_o1_api] - Request timed out for conversation {conversation_id}"
            logger.error(error_msg)
            human_msg = _human_exception_error(exc, "OpenAI (o1)")
            await record_provider_error_for_label("OpenAI (o1)", message=human_msg, exception=exc, model=model, byok=byok)
            yield f"data: {orjson.dumps(_provider_error_payload('OpenAI (o1)', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            error_yielded = True
        except aiohttp.ClientError as exc:
            error_msg = f"[call_o1_api] - Connection error: {str(exc)}"
            logger.error(error_msg)
            human_msg = _human_exception_error(exc, "OpenAI (o1)")
            await record_provider_error_for_label("OpenAI (o1)", message=human_msg, exception=exc, model=model, byok=byok)
            yield f"data: {orjson.dumps(_provider_error_payload('OpenAI (o1)', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            error_yielded = True
        except Exception as exc:
            error_msg = f"[call_o1_api] - Unexpected error: {str(exc)}"
            logger.error(error_msg)
            human_msg = _human_exception_error(exc, "OpenAI (o1)")
            await record_provider_error_for_label("OpenAI (o1)", message=human_msg, exception=exc, model=model, byok=byok)
            yield f"data: {orjson.dumps(_provider_error_payload('OpenAI (o1)', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            error_yielded = True

    # OpenAI includes reasoning tokens in completion_tokens/total_tokens.
    # Keep the detail for diagnostics without charging those tokens twice.
    if (
        billing_reservation_id
        and save_to_db
        and not error_yielded
        and (content or input_tokens or output_tokens)
    ):
        input_tokens, output_tokens = await accumulate_ai_provider_call_usage(
            reservation_id=billing_reservation_id,
            user_id=user_id,
            reported_input_tokens=input_tokens,
            reported_output_tokens=output_tokens,
            input_payload=(prompt, messages),
            output_payload=content,
            input_token_fallback=input_token_fallback,
            output_token_cap=max_tokens,
            llm_id=llm_id,
            model=model,
            prompt_id=prompt_id,
            byok=byok,
        )
        total_tokens = input_tokens + output_tokens

    # Save the content to the database using read-write connection
    if save_to_db:
        was_stopped = stop_signals.get(conversation_id, False)
        if not content.strip():
            if was_stopped:
                logger.info(f"User stopped stream before content for conversation {conversation_id}. Skipping save.")
            else:
                logger.warning(f"Empty bot response for conversation {conversation_id}, user {user_id}. "
                               f"Provider: o1. Not saving to DB.")
                if not error_yielded:
                    await record_provider_error_for_label("OpenAI (o1)", message="empty response", model=model, byok=byok)
                    empty_msg = "The AI returned an empty response. Please try again."
                    yield f"data: {orjson.dumps(_provider_error_payload('OpenAI (o1)', empty_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            return
        else:
            await record_provider_success_for_label("OpenAI (o1)", model=model, byok=byok)
            user_message_id, bot_message_id = await save_content_to_db(content, input_tokens, output_tokens, total_tokens, conversation_id, user_id, model, user_message=user_message,
                                                                        input_token_fallback=input_token_fallback,
                                                                        prompt_id=prompt_id, watchdog_config=watchdog_config, watchdog_hint_active=watchdog_hint_active, watchdog_hint_eval_id=watchdog_hint_eval_id,
                                                                        llm_id=llm_id, byok=byok, pending_attachment_refs=pending_attachment_refs,
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
            await record_provider_success_for_label("OpenAI (o1)", model=model, byok=byok)
        elif not error_yielded:
            await record_provider_error_for_label("OpenAI (o1)", message="empty response", model=model, byok=byok)
            empty_msg = "The AI returned an empty response. Please try again."
            yield f"data: {orjson.dumps(_provider_error_payload('OpenAI (o1)', empty_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            yield "data: [DONE]\n\n"
            return
        yield f"data: {orjson.dumps({'token_info': True, 'input_tokens': input_tokens, 'output_tokens': output_tokens}).decode()}\n\n"
        yield "data: [DONE]\n\n"


async def call_llm_api(messages, model, temperature, max_tokens, prompt, conversation_id, current_user, request, api_url, api_key, provider_label, user_message=None, extra_headers=None, custom_timeout=None, tools=None,
                       input_token_fallback=None,
                       pdf_error_metadata=None,
                       prompt_id=None, watchdog_config=None, watchdog_hint_active=False, watchdog_hint_eval_id=None,
                       llm_id=None, save_to_db: bool = True, web_search_mode=None, byok: bool = False, api_model=None,
                       extra_body: dict | None = None,
                       omit_temperature: bool = False,
                       use_max_completion_tokens: bool = False,
                       include_stream_usage: bool = True,
                       pending_attachment_refs: Optional[list[str]] = None,
                       strip_device_action_blocks: bool = False,
                       billing_reservation_id: str | None = None):
    """
    Generic LLM API call function for OpenAI-compatible APIs.
    Used by GPT, xAI, and OpenRouter.

    Args:
        provider_label: Human-readable provider name for user-facing SSE errors.
        extra_headers: Additional headers to include (e.g., for OpenRouter)
        custom_timeout: Override the default timeout in seconds
        tools: List of tools in OpenAI format (optional). When provided,
               the model can decide to call a tool instead of responding.
    """
    global stop_signals
    logger.info("enters call_llm_api")

    user_id = current_user.id
    error_yielded = False

    messages.insert(0, {"role": "system", "content": prompt})
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    # Merge extra headers if provided (for OpenRouter)
    if extra_headers:
        headers.update(extra_headers)

    data = {
        "model": api_model or model,
        "messages": messages,
        "stream": True,
    }
    if _is_gpt5_model(model) or use_max_completion_tokens:
        data["max_completion_tokens"] = max_tokens
    else:
        data["max_tokens"] = max_tokens
    # GPT-5+ and a few OpenAI-compatible providers reject custom temperature.
    if not _is_gpt5_model(model) and not omit_temperature:
        data["temperature"] = temperature
    if include_stream_usage:
        data["stream_options"] = {"include_usage": True}
    if extra_body:
        data.update(extra_body)

    # Shallow copy to avoid mutating the caller's list if server tools are appended later
    if tools:
        data["tools"] = list(tools)
        data["tool_choice"] = "auto"  # Let the model decide when to use tools

    content, function_name, function_arguments = "", "", ""
    tagged_thinking_parser = TaggedThinkingStreamParser()
    tool_call_id = ""  # For tracking tool_calls
    input_tokens = output_tokens = total_tokens = 0
    truncated = False
    thinking_open = False
    reasoning_content_for_message = ""
    reasoning_details_for_message: list[dict] = []
    reasoning_detail_positions: dict[tuple, int] = {}
    reasoning_detail_keys: list[tuple] = []

    def _merge_streamed_value(previous, current) -> tuple[str, str]:
        """Return the reconstructed value and the newly arrived suffix."""
        current = str(current)
        if not isinstance(previous, str) or not previous:
            return current, current
        if current == previous:
            return previous, ""
        if current.startswith(previous):
            return current, current[len(previous):]
        return previous + current, current

    def _reasoning_chunks_from_delta(delta: dict) -> list[str]:
        chunks = []
        reasoning_details = delta.get("reasoning_details")
        if isinstance(reasoning_details, list):
            for detail in reasoning_details:
                if not isinstance(detail, dict):
                    continue

                if detail.get("index") is not None:
                    key = ("index", str(detail["index"]))
                elif detail.get("id") is not None:
                    key = ("id", str(detail["id"]))
                else:
                    key = (
                        "anonymous",
                        str(detail.get("type") or ""),
                        str(detail.get("format") or ""),
                    )

                position = reasoning_detail_positions.get(key)
                if position is None and key[0] == "anonymous":
                    if reasoning_detail_keys and reasoning_detail_keys[-1] == key:
                        position = len(reasoning_details_for_message) - 1
                if position is None:
                    position = len(reasoning_details_for_message)
                    reasoning_details_for_message.append({})
                    reasoning_detail_keys.append(key)
                    if key[0] != "anonymous":
                        reasoning_detail_positions[key] = position

                previous_detail = reasoning_details_for_message[position]
                merged_detail = {**previous_detail, **detail}
                for field in ("text", "summary", "content", "data"):
                    value = detail.get(field)
                    if not isinstance(value, str):
                        continue
                    merged_value, new_value = _merge_streamed_value(
                        previous_detail.get(field), value
                    )
                    merged_detail[field] = merged_value
                    if field != "data" and new_value:
                        chunks.append(new_value)
                reasoning_details_for_message[position] = merged_detail
            return chunks

        reasoning_content = delta.get("reasoning_content")
        if reasoning_content:
            chunks.append(str(reasoning_content))
        return chunks

    logger.debug(f"call_llm_api -> messages: {messages}")

    # Configure timeout: use custom_timeout if provided, otherwise check for reasoning models
    if custom_timeout:
        timeout_seconds = custom_timeout
    elif "grok" in model.lower():
        timeout_seconds = 300  # 5 minutes for Grok reasoning models
    else:
        timeout_seconds = 120  # Default 2 minutes
    timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=10)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.post(api_url, headers=headers, json=data) as response:
                if response.status == 200:
                    # JSON buffer for handling incomplete chunks
                    json_buffer = ""
                    input_tokens = output_tokens = total_tokens = 0

                    async for chunk in response.content.iter_chunked(1024):
                        if stop_signals.get(conversation_id):
                            logger.info("Stop signal received, exiting LLM API call loop.")
                            break

                        chunk_str = chunk.decode("utf-8")
                        json_buffer += chunk_str

                        # Process complete lines from buffer
                        while "\n\n" in json_buffer:
                            line_data, json_buffer = json_buffer.split("\n\n", 1)

                            for line in line_data.split("\n"):
                                line = line.strip()

                                if line.startswith("data: "):
                                    data_part = line[6:]  # Remove 'data: ' prefix

                                    if data_part == "[DONE]":
                                        break

                                    if data_part.startswith("{"):
                                        try:
                                            chunk_data = orjson.loads(data_part)

                                            if 'choices' in chunk_data and chunk_data['choices']:
                                                for choice in chunk_data['choices']:
                                                    if not choice:
                                                        continue
                                                    if 'delta' in choice and choice['delta'] is not None:
                                                        delta = choice['delta']

                                                        for reasoning_chunk in _reasoning_chunks_from_delta(delta):
                                                            reasoning_content_for_message += reasoning_chunk
                                                            if not thinking_open:
                                                                thinking_open = True
                                                                yield f"data: {orjson.dumps({'type': 'thinking_start'}).decode()}\n\n"
                                                            yield f"data: {orjson.dumps({'thinking': reasoning_chunk, 'type': 'thinking'}).decode()}\n\n"

                                                        # Handle tool_calls (new OpenAI format)
                                                        if 'tool_calls' in delta:
                                                            for tc in delta['tool_calls']:
                                                                if tc.get('id'):
                                                                    tool_call_id = tc['id']
                                                                if tc.get('function'):
                                                                    fn = tc['function']
                                                                    if fn.get('name'):
                                                                        function_name = fn['name']
                                                                        function_arguments = ""
                                                                    if fn.get('arguments'):
                                                                        function_arguments += fn['arguments']

                                                        # Handle function_call (deprecated but still supported)
                                                        elif 'function_call' in delta:
                                                            function_chunk = delta['function_call']
                                                            if function_chunk is not None:
                                                                if 'name' in function_chunk:
                                                                    function_name = function_chunk['name']
                                                                    function_arguments = ""
                                                                elif 'arguments' in function_chunk:
                                                                    function_arguments += function_chunk['arguments']

                                                        # Handle content
                                                        if 'content' in delta:
                                                            content_chunk = delta['content']
                                                            if content_chunk is not None:
                                                                for tagged_type, tagged_chunk in tagged_thinking_parser.feed(content_chunk):
                                                                    if tagged_type == 'thinking_start':
                                                                        if not thinking_open:
                                                                            thinking_open = True
                                                                            yield f"data: {orjson.dumps({'type': 'thinking_start'}).decode()}\n\n"
                                                                    elif tagged_type == 'thinking':
                                                                        if not thinking_open:
                                                                            thinking_open = True
                                                                            yield f"data: {orjson.dumps({'type': 'thinking_start'}).decode()}\n\n"
                                                                        yield f"data: {orjson.dumps({'thinking': tagged_chunk, 'type': 'thinking'}).decode()}\n\n"
                                                                    elif tagged_type == 'thinking_end':
                                                                        if thinking_open:
                                                                            thinking_open = False
                                                                            yield f"data: {orjson.dumps({'type': 'thinking_end'}).decode()}\n\n"
                                                                    elif tagged_chunk:
                                                                        if thinking_open:
                                                                            thinking_open = False
                                                                            yield f"data: {orjson.dumps({'type': 'thinking_end'}).decode()}\n\n"
                                                                        content += tagged_chunk
                                                                        yield f"data: {orjson.dumps({'content': tagged_chunk}).decode()}\n\n"

                                                    # Check finish_reason for tool_calls
                                                    finish_reason = choice.get('finish_reason')
                                                    if finish_reason == 'tool_calls' or finish_reason == 'function_call':
                                                        # Tool call completed - will be processed after loop
                                                        continue
                                                    elif finish_reason == 'stop':
                                                        continue
                                                    elif finish_reason in {'length', 'max_tokens', 'max_completion_tokens'}:
                                                        if not truncated:
                                                            truncated = True
                                                            _log_truncated_response(
                                                                provider_label,
                                                                model,
                                                                conversation_id,
                                                                llm_id,
                                                                finish_reason,
                                                                max_tokens,
                                                            )

                                            # Handle usage information
                                            if 'usage' in chunk_data and chunk_data['usage'] and 'total_tokens' in chunk_data['usage']:
                                                input_tokens = chunk_data['usage']['prompt_tokens']
                                                output_tokens = chunk_data['usage']['completion_tokens']
                                                total_tokens = chunk_data['usage']['total_tokens']

                                        except orjson.JSONDecodeError as e:
                                            # Log JSON errors but don't stop processing for Grok reasoning models
                                            if "grok" in model.lower():
                                                logger.warning(f"JSON decode warning for {model}: {e}")
                                            else:
                                                logger.error(f"[call_llm_api] - Error decoding JSON fragment: {e} , data: {data_part[:200]}...")
                else:
                    error_body = await response.text()
                    raw_log = f"[call_llm_api] - Error: Received status code {response.status}. Response body: {error_body}"
                    logger.error(raw_log)
                    human_msg = _extract_human_error_message(error_body, response.status, provider_label)
                    await record_provider_error_for_label(
                        provider_label,
                        message=human_msg,
                        status_code=response.status,
                        model=model,
                        byok=byok,
                    )
                    yield f"data: {orjson.dumps(_provider_error_payload(provider_label, human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
                    error_yielded = True

                    logger.error(f"Request details: URL: {api_url}, Headers: {safe_log_headers(headers)}, "
                                 f"model={data.get('model', '?')}, messages={len(data.get('messages', []))}, "
                                 f"conversation_id={conversation_id}")

                    try:
                        error_json = await response.json()
                        if 'error' in error_json:
                            logger.error(f"API Error details: {error_json['error']}")
                    except:
                        logger.error("Could not parse error response as JSON")

        except asyncio.TimeoutError as exc:
            error_message = f"[call_llm_api] - Request timed out after {timeout_seconds} seconds for model {model}"
            logger.error(error_message)
            human_msg = _human_exception_error(exc, provider_label)
            await record_provider_error_for_label(provider_label, message=human_msg, exception=exc, model=model, byok=byok)
            yield f"data: {orjson.dumps(_provider_error_payload(provider_label, human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            error_yielded = True

        except aiohttp.ClientError as exc:
            error_message = f"[call_llm_api] - Network error occurred: {str(exc)}"
            logger.error(error_message)
            human_msg = _human_exception_error(exc, provider_label)
            await record_provider_error_for_label(provider_label, message=human_msg, exception=exc, model=model, byok=byok)
            yield f"data: {orjson.dumps(_provider_error_payload(provider_label, human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            error_yielded = True

        except Exception as exc:
            error_message = f"[call_llm_api] - Unexpected error: {str(exc)}"
            logger.error(error_message)
            human_msg = _human_exception_error(exc, provider_label)
            await record_provider_error_for_label(provider_label, message=human_msg, exception=exc, model=model, byok=byok)
            yield f"data: {orjson.dumps(_provider_error_payload(provider_label, human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            error_yielded = True

    for tagged_type, tagged_chunk in tagged_thinking_parser.finalize():
        if tagged_type == 'thinking':
            if not thinking_open:
                thinking_open = True
                yield f"data: {orjson.dumps({'type': 'thinking_start'}).decode()}\n\n"
            yield f"data: {orjson.dumps({'thinking': tagged_chunk, 'type': 'thinking'}).decode()}\n\n"
        elif tagged_type == 'thinking_end':
            if thinking_open:
                thinking_open = False
                yield f"data: {orjson.dumps({'type': 'thinking_end'}).decode()}\n\n"
        elif tagged_type == 'content' and tagged_chunk:
            if thinking_open:
                thinking_open = False
                yield f"data: {orjson.dumps({'type': 'thinking_end'}).decode()}\n\n"
            content += tagged_chunk
            yield f"data: {orjson.dumps({'content': tagged_chunk}).decode()}\n\n"

    if thinking_open:
        yield f"data: {orjson.dumps({'type': 'thinking_end'}).decode()}\n\n"

    billing_input_tokens = input_tokens
    billing_output_tokens = output_tokens
    if (
        billing_reservation_id
        and save_to_db
        and (content or function_name or input_tokens or output_tokens)
    ):
        billing_input_tokens, billing_output_tokens = (
            await accumulate_ai_provider_call_usage(
                reservation_id=billing_reservation_id,
                user_id=current_user.id,
                reported_input_tokens=input_tokens,
                reported_output_tokens=output_tokens,
                input_payload=messages,
                output_payload=(content, function_name, function_arguments),
                input_token_fallback=input_token_fallback,
                output_token_cap=max_tokens,
                llm_id=llm_id,
                model=model,
                prompt_id=prompt_id,
                byok=byok,
            )
        )

    # If a tool call was detected, emit it and return without saving to DB
    # The caller (get_ai_response) will handle the tool call and save the result
    # When save_to_db=False (Multi-AI), skip tool handling entirely
    if function_name and save_to_db:
        try:
            # Parse the accumulated arguments as JSON
            parsed_args = orjson.loads(function_arguments) if function_arguments else {}
        except orjson.JSONDecodeError:
            logger.error(f"[call_llm_api] - Failed to parse tool arguments: {function_arguments}")
            parsed_args = {}

        logger.info(f"[call_llm_api] - Tool call detected: {function_name}")
        logger.debug(f"[call_llm_api] - Tool call args: {parsed_args}")

        await record_provider_success_for_label(provider_label, model=model, byok=byok)
        tool_call_payload = {
            'name': function_name,
            'arguments': parsed_args,
            'id': tool_call_id,
            '_billing_usage': {
                'input_tokens': billing_input_tokens,
                'output_tokens': billing_output_tokens,
            },
        }
        if reasoning_content_for_message:
            tool_call_payload["reasoning_content"] = reasoning_content_for_message
        if reasoning_details_for_message:
            tool_call_payload["reasoning_details"] = reasoning_details_for_message
        yield f"data: {orjson.dumps({'tool_call': tool_call_payload}).decode()}\n\n"
        yield f"data: {orjson.dumps({'tool_call_pending': True}).decode()}\n\n"
        return  # Don't save to DB - handler will do it

    # Normal response - save to database
    if save_to_db:
        was_stopped = stop_signals.get(conversation_id, False)
        if not content.strip():
            if was_stopped:
                logger.info(f"User stopped stream before content for conversation {conversation_id}. Skipping save.")
            else:
                logger.warning(f"Empty bot response for conversation {conversation_id}, user {current_user.id}. "
                               f"Provider: llm_api. Not saving to DB.")
                if not error_yielded:
                    await record_provider_error_for_label(provider_label, message="empty response", model=model, byok=byok)
                    empty_msg = "The AI returned an empty response. Please try again."
                    yield f"data: {orjson.dumps(_provider_error_payload(provider_label, empty_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            return
        else:
            await record_provider_success_for_label(provider_label, model=model, byok=byok)
            user_message_id, bot_message_id = await save_content_to_db(content, input_tokens, output_tokens, total_tokens, conversation_id, current_user.id, model, user_message=user_message,
                                                                        input_token_fallback=input_token_fallback,
                                                                        prompt_id=prompt_id, watchdog_config=watchdog_config, watchdog_hint_active=watchdog_hint_active, watchdog_hint_eval_id=watchdog_hint_eval_id,
                                                                        llm_id=llm_id, byok=byok, pending_attachment_refs=pending_attachment_refs,
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
            await record_provider_success_for_label(provider_label, model=model, byok=byok)
        elif not error_yielded:
            await record_provider_error_for_label(provider_label, message="empty response", model=model, byok=byok)
            empty_msg = "The AI returned an empty response. Please try again."
            yield f"data: {orjson.dumps(_provider_error_payload(provider_label, empty_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            yield "data: [DONE]\n\n"
            return
        yield f"data: {orjson.dumps({'token_info': True, 'input_tokens': input_tokens, 'output_tokens': output_tokens}).decode()}\n\n"
        yield "data: [DONE]\n\n"

async def call_gpt_api(messages, model, temperature, max_tokens, prompt, conversation_id, current_user, request, user_message=None, user_api_key=None, tools=None,
                       input_token_fallback=None,
                       pdf_error_metadata=None,
                       prompt_id=None, watchdog_config=None, watchdog_hint_active=False, watchdog_hint_eval_id=None,
                       llm_id=None, save_to_db: bool = True, web_search_mode=None, byok: bool = False,
                       pending_attachment_refs: Optional[list[str]] = None,
                       strip_device_action_blocks: bool = False,
                       billing_reservation_id: str | None = None,
                       reasoning_selection: ReasoningSelection | dict | str | None = None):
    api_url = "https://api.openai.com/v1/chat/completions"
    api_key = user_api_key or openai.api_key  # Use user's key if provided
    reasoning_effort = _chat_reasoning_effort(reasoning_selection)

    async for chunk in call_llm_api(
        messages,
        model,
        temperature,
        max_tokens,
        prompt,
        conversation_id,
        current_user,
        request,
        api_url,
        api_key,
        "OpenAI (GPT)",
        user_message=user_message,
        input_token_fallback=input_token_fallback,
        pdf_error_metadata=pdf_error_metadata,
        tools=tools,
        prompt_id=prompt_id,
        watchdog_config=watchdog_config,
        watchdog_hint_active=watchdog_hint_active,
        watchdog_hint_eval_id=watchdog_hint_eval_id,
        llm_id=llm_id,
        save_to_db=save_to_db,
        web_search_mode=web_search_mode,
        byok=byok,
        extra_body={"reasoning_effort": reasoning_effort} if reasoning_effort is not None else None,
        pending_attachment_refs=pending_attachment_refs,
        strip_device_action_blocks=strip_device_action_blocks,
        billing_reservation_id=billing_reservation_id,
    ):
        yield chunk
