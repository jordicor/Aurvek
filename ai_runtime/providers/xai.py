from integrations.applications.external_execution import provider_request
from integrations.embed.models import EmbedError

from ai_runtime.dependencies import *
from ai_runtime.config import _log_truncated_response
from ai_runtime.errors import _extract_human_error_message, _human_exception_error, _provider_error_payload
from ai_runtime.persistence.messages import persistence_error_payload, persistence_result_payload, save_content_to_db
from ai_runtime.providers.openai_chat import _chat_reasoning_effort, call_llm_api
from ai_runtime.providers.openai_responses import _convert_messages_for_responses_api
from ai_runtime.provider_health import record_provider_error_for_label, record_provider_success_for_label
from billing.usage_reservations import accumulate_ai_provider_call_usage
from ai_runtime.reasoning import ReasoningSelection, parse_reasoning_selection


def _xai_reasoning_payload(
    reasoning_selection: ReasoningSelection | dict | str | None,
) -> dict | None:
    """Translate only the reasoning fields documented by the xAI API."""

    if reasoning_selection is None:
        return None
    selection = parse_reasoning_selection(reasoning_selection)
    if selection.mode == "default":
        return None
    effort = "none" if selection.mode == "off" else selection.mode
    return {"effort": effort}

async def call_xai_api(messages, model, temperature, max_tokens, prompt, conversation_id, current_user, request, user_message=None, user_api_key=None, tools=None,
                       input_token_fallback=None,
                       pdf_error_metadata=None,
                       prompt_id=None, watchdog_config=None, watchdog_hint_active=False, watchdog_hint_eval_id=None,
                       llm_id=None, save_to_db: bool = True, web_search_mode=None, byok: bool = False,
                       pending_attachment_refs: Optional[list[str]] = None,
                       strip_device_action_blocks: bool = False,
                       billing_reservation_id: str | None = None,
                       reasoning_selection: ReasoningSelection | dict | str | None = None):
    api_url = "https://api.x.ai/v1/chat/completions"
    api_key = user_api_key or xai_key  # Use user's key if provided
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
        "xAI (Grok)",
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


async def call_xai_responses_api(messages, model, temperature, max_tokens, prompt, conversation_id, current_user, request, user_message=None, user_api_key=None, tools=None,
                                  input_token_fallback=None,
                                  pdf_error_metadata=None,
                                  prompt_id=None, watchdog_config=None, watchdog_hint_active=False, watchdog_hint_eval_id=None,
                                  llm_id=None, save_to_db: bool = True, web_search_mode=None, byok: bool = False,
                                  pending_attachment_refs: Optional[list[str]] = None,
                                  strip_device_action_blocks: bool = False,
                                  billing_reservation_id: str | None = None,
                                  reasoning_selection: ReasoningSelection | dict | str | None = None):
    """
    xAI Responses API call function. Replaces call_xai_api for all xAI/Grok calls.
    Uses /v1/responses endpoint with semantic SSE events instead of Chat Completions.

    Key differences from OpenAI's call_gpt_responses_api:
    - System prompt goes as first item in input array (no 'instructions' parameter)
    - Citations come as response.citations (flat URL list) + inline [[N]](url) markdown
    - x_search tool available for X/Twitter search alongside web_search

    Emits the same SSE format as other providers for frontend compatibility.
    """
    global stop_signals
    logger.info("enters call_xai_responses_api")

    from integrations.applications.billing import current_application_operation
    from integrations.embed.models import EmbedError
    if current_application_operation(current_user.id) is not None and any(
            item.get("type") in {"web_search", "x_search"} for item in (tools or [])):
        raise EmbedError("application_search_connector_required", 403)

    error_yielded = False
    stream_interruption: BaseException | None = None
    deferred_error_event = None
    api_url = "https://api.x.ai/v1/responses"
    api_key = user_api_key or xai_key

    user_id = current_user.id

    # Convert Chat Completions message format to Responses API format
    messages = _convert_messages_for_responses_api(messages)

    # Build request body
    data = {
        "model": model,
        "input": messages,
        "stream": True,
        "store": False,
    }
    reasoning = _xai_reasoning_payload(reasoning_selection)
    if reasoning is not None:
        data["reasoning"] = reasoning

    # xAI does NOT support 'instructions' — system prompt goes as first item in input
    if prompt:
        data["input"].insert(0, {"role": "system", "content": prompt})

    data["temperature"] = temperature

    if max_tokens:
        data["max_output_tokens"] = max_tokens

    # Shallow copy to avoid mutating the caller's list if server tools are appended later
    if tools:
        data["tools"] = list(tools)
        data["tool_choice"] = "auto"
        data["parallel_tool_calls"] = False
        data["include"] = ["reasoning.encrypted_content"]

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    content = ""
    function_name = ""
    function_arguments = ""
    tool_call_id = ""
    input_tokens = output_tokens = total_tokens = 0
    citations = []
    truncated = False
    thinking_open = False
    response_output_items: list[dict] = []
    function_output_item: dict | None = None
    response_completed = False
    tool_stream_invalid = False
    tool_seen = False
    multiple_tool_calls = False
    function_output_index = None
    function_item_id = None

    def _track_function_event(event, item=None) -> bool:
        # Keep one identity; never concatenate deltas belonging to another call.
        nonlocal tool_seen, multiple_tool_calls, function_output_index, function_item_id, tool_call_id
        item = item or {}
        identity = (event.get("output_index"), item.get("id") or event.get("item_id"),
                    item.get("call_id") or event.get("call_id"))
        previous = (function_output_index, function_item_id, tool_call_id or None)
        if ((item.get("name") and function_name and item["name"] != function_name)
                or any(old is not None and new is not None and old != new
                       for old, new in zip(previous, identity))):
            multiple_tool_calls = True
            return False
        if (tool_seen and event.get("type") == "response.output_item.added"
                and not any(old is not None and old == new for old, new in zip(previous, identity))):
            multiple_tool_calls = True
            return False
        tool_seen = True
        function_output_index = identity[0] if identity[0] is not None else function_output_index
        function_item_id = identity[1] or function_item_id
        tool_call_id = identity[2] or tool_call_id
        return True

    logger.info(f"call_xai_responses_api -> model: {model}, tools: {len(tools) if tools else 0}")

    # Grok models may need extra time for reasoning
    timeout_seconds = 300
    timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=10)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with provider_request(session, api_url, headers=headers, json=data) as response:
                if response.status == 200:
                    buffer = ""
                    raw_remainder = b""

                    async for chunk in response.content.iter_any():
                        if stop_signals.get(conversation_id):
                            logger.info("Stop signal received, exiting xAI Responses API call loop.")
                            break

                        raw_remainder += chunk
                        try:
                            chunk_str = raw_remainder.decode("utf-8")
                            raw_remainder = b""
                        except UnicodeDecodeError:
                            for trim in range(1, 4):
                                try:
                                    chunk_str = raw_remainder[:-trim].decode("utf-8")
                                    raw_remainder = raw_remainder[-trim:]
                                    break
                                except UnicodeDecodeError:
                                    continue
                            else:
                                continue

                        buffer += chunk_str

                        while "\n\n" in buffer:
                            event_block, buffer = buffer.split("\n\n", 1)

                            event_type = None
                            data_str = None
                            for line in event_block.split("\n"):
                                line = line.strip()
                                if line.startswith("event: "):
                                    event_type = line[7:]
                                elif line.startswith("data: "):
                                    data_str = line[6:]

                            if not data_str or data_str == "[DONE]":
                                continue

                            try:
                                event_data = orjson.loads(data_str)
                            except orjson.JSONDecodeError as e:
                                logger.warning(f"[call_xai_responses_api] JSON decode warning: {e}")
                                tool_stream_invalid = True
                                continue

                            etype = event_type or event_data.get("type", "")
                            # Failure/incomplete events can still report real partial usage.
                            response_usage = (event_data.get("response") or {}).get("usage") or {}
                            input_tokens = response_usage.get("input_tokens", input_tokens)
                            output_tokens = response_usage.get("output_tokens", output_tokens)
                            total_tokens = input_tokens + output_tokens

                            # --- Text streaming ---
                            if etype in ("response.output_text.delta", "response.text.delta"):
                                delta = event_data.get("delta", "")
                                if delta:
                                    if thinking_open:
                                        thinking_open = False
                                        yield f"data: {orjson.dumps({'type': 'thinking_end'}).decode()}\n\n"
                                    content += delta
                                    yield f"data: {orjson.dumps({'content': delta}).decode()}\n\n"

                            elif etype in {
                                "response.reasoning_summary_text.delta",
                                "response.reasoning.delta",
                                "response.reasoning_text.delta",
                            }:
                                delta = event_data.get("delta", "")
                                if delta:
                                    if not thinking_open:
                                        thinking_open = True
                                        yield f"data: {orjson.dumps({'type': 'thinking_start'}).decode()}\n\n"
                                    yield f"data: {orjson.dumps({'thinking': delta, 'type': 'thinking'}).decode()}\n\n"

                            elif etype in {
                                "response.reasoning_summary_text.done",
                                "response.reasoning.done",
                                "response.reasoning_text.done",
                            } and thinking_open:
                                thinking_open = False
                                yield f"data: {orjson.dumps({'type': 'thinking_end'}).decode()}\n\n"

                            # --- Web search status ---
                            elif etype == "response.web_search_call.in_progress":
                                yield f"data: {orjson.dumps({'searching': True}).decode()}\n\n"

                            elif etype == "response.web_search_call.searching":
                                pass

                            elif etype == "response.web_search_call.completed":
                                yield f"data: {orjson.dumps({'searching': False}).decode()}\n\n"

                            # --- Function call handling ---
                            elif etype in {"response.output_item.added", "response.output_item.done"}:
                                item = event_data.get("item", {})
                                if isinstance(item, dict):
                                    if etype == "response.output_item.added":
                                        response_output_items.append(item)
                                    if item.get("type") == "function_call" and _track_function_event(
                                            {**event_data, "type": etype}, item):
                                        function_name = item.get("name") or function_name
                                        function_output_item = item
                                        if item.get("arguments") is not None:
                                            function_arguments = item["arguments"]

                            elif etype in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
                                if _track_function_event(event_data):
                                    if etype.endswith(".delta"):
                                        function_arguments += event_data.get("delta", "")
                                    elif "arguments" in event_data:
                                        function_arguments = event_data["arguments"]
                                    if function_output_item is not None:
                                        function_output_item["arguments"] = function_arguments

                            # --- Citations (xAI sends url_citation annotations like OpenAI) ---
                            elif etype == "response.output_text.annotation.added":
                                annotation = event_data.get("annotation", {})
                                if annotation.get("type") == "url_citation":
                                    citations.append({
                                        "url": annotation.get("url", ""),
                                        "title": annotation.get("title", ""),
                                        "start_index": annotation.get("start_index"),
                                        "end_index": annotation.get("end_index"),
                                    })

                            # --- Completion ---
                            elif etype == "response.completed":
                                resp = event_data.get("response", {})
                                final_output = resp.get("output")
                                if isinstance(final_output, list):
                                    response_output_items = final_output
                                    function_items = [(index, item) for index, item in enumerate(final_output)
                                                      if isinstance(item, dict) and item.get("type") == "function_call"]
                                    if tool_seen and not function_items:
                                        tool_stream_invalid = True
                                    if len(function_items) > 1:
                                        tool_seen = multiple_tool_calls = True
                                    elif function_items:
                                        index, item = function_items[0]
                                        if _track_function_event({"output_index": index}, item):
                                            function_output_item = item
                                            function_name = item.get("name") or function_name
                                            function_arguments = item.get("arguments", function_arguments)
                                incomplete_reason = (resp.get("incomplete_details") or {}).get("reason")
                                if not truncated and (resp.get("status") == "incomplete" or incomplete_reason in {"max_output_tokens", "max_tokens"}):
                                    truncated = True
                                    _log_truncated_response(
                                        "xAI Responses",
                                        model,
                                        conversation_id,
                                        llm_id,
                                        incomplete_reason or resp.get("status") or "incomplete",
                                        max_tokens,
                                    )
                                response_completed = (etype == "response.completed"
                                                      and resp.get("status") in {None, "completed"}
                                                      and not resp.get("error") and not incomplete_reason and not truncated)
                                usage = resp.get("usage", {})
                                input_tokens = usage.get("input_tokens", input_tokens)
                                output_tokens = usage.get("output_tokens", output_tokens)
                                total_tokens = input_tokens + output_tokens

                                # Extract citations from response.citations (flat URL list)
                                flat_citations = resp.get("citations", [])
                                for url in flat_citations:
                                    if not any(c["url"] == url for c in citations):
                                        citations.append({"url": url, "title": ""})

                                # Also extract structured annotations from output items
                                for output_item in resp.get("output", []):
                                    if output_item.get("type") == "message":
                                        for part in output_item.get("content", []):
                                            for ann in part.get("annotations", []):
                                                if ann.get("type") == "url_citation":
                                                    url = ann.get("url", "")
                                                    if not any(c["url"] == url and c.get("start_index") == ann.get("start_index") for c in citations):
                                                        citations.append({
                                                            "url": url,
                                                            "title": ann.get("title", ""),
                                                            "start_index": ann.get("start_index"),
                                                            "end_index": ann.get("end_index"),
                                                        })

                            # --- Errors ---
                            elif etype in {"response.failed", "error"}:
                                error_info = (event_data.get("response") or {}).get("error") or event_data.get("error") or event_data
                                error_msg = error_info.get("message", "Unknown API error")
                                error_code = error_info.get("code") or error_info.get("type")
                                if isinstance(error_code, str) and error_code.strip() and error_code.strip() not in error_msg:
                                    error_msg = f"{error_code.strip()}: {error_msg}"
                                logger.error(f"[call_xai_responses_api] Response failed: {error_msg}")
                                await record_provider_error_for_label("xAI (Grok)", message=error_msg, model=model, byok=byok)
                                yield f"data: {orjson.dumps(_provider_error_payload('xAI (Grok)', error_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
                                error_yielded = True

                            elif etype == "response.incomplete":
                                resp = event_data.get("response", {})
                                reason = (resp.get("incomplete_details") or {}).get("reason") or "incomplete"
                                if not truncated:
                                    truncated = True
                                    _log_truncated_response("xAI Responses", model, conversation_id, llm_id, reason, max_tokens)

                            # --- Refusal ---
                            elif etype == "response.refusal.delta":
                                delta = event_data.get("delta", "")
                                if delta:
                                    content += delta
                                    yield f"data: {orjson.dumps({'content': delta}).decode()}\n\n"

                    tool_stream_invalid |= bool(raw_remainder or buffer.strip())

                else:
                    error_body = await response.text()
                    raw_log = f"[call_xai_responses_api] Error: status {response.status}. Body: {error_body}"
                    logger.error(raw_log)
                    human_msg = _extract_human_error_message(error_body, response.status, "xAI (Grok)")
                    await record_provider_error_for_label(
                        "xAI (Grok)",
                        message=human_msg,
                        status_code=response.status,
                        model=model,
                        byok=byok,
                    )
                    yield f"data: {orjson.dumps(_provider_error_payload('xAI (Grok)', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
                    error_yielded = True

        except (asyncio.CancelledError, GeneratorExit) as exc:
            stream_interruption = exc
        except asyncio.TimeoutError as exc:
            error_message = f"[call_xai_responses_api] Request timed out after {timeout_seconds}s for model {model}"
            logger.error(error_message)
            human_msg = _human_exception_error(exc, "xAI (Grok)")
            await record_provider_error_for_label("xAI (Grok)", message=human_msg, exception=exc, model=model, byok=byok)
            deferred_error_event = f"data: {orjson.dumps(_provider_error_payload('xAI (Grok)', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            error_yielded = True

        except aiohttp.ClientError as exc:
            error_message = f"[call_xai_responses_api] Network error: {str(exc)}"
            logger.error(error_message)
            human_msg = _human_exception_error(exc, "xAI (Grok)")
            await record_provider_error_for_label("xAI (Grok)", message=human_msg, exception=exc, model=model, byok=byok)
            deferred_error_event = f"data: {orjson.dumps(_provider_error_payload('xAI (Grok)', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            error_yielded = True

        except EmbedError as exc:
            # Application accounting refused or revoked the call: not a provider fault.
            logger.warning(f"[call_xai_responses_api] Application refused the call: {exc.code}")
            deferred_error_event = f"data: {orjson.dumps({'error': exc.code, 'error_code': exc.code}).decode()}\n\n"
            error_yielded = True

        except Exception as exc:
            error_message = f"[call_xai_responses_api] Unexpected error: {str(exc)}"
            logger.error(error_message)
            human_msg = _human_exception_error(exc, "xAI (Grok)")
            await record_provider_error_for_label("xAI (Grok)", message=human_msg, exception=exc, model=model, byok=byok)
            deferred_error_event = f"data: {orjson.dumps(_provider_error_payload('xAI (Grok)', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            error_yielded = True

    try:
        if thinking_open and stream_interruption is None:
            yield f"data: {orjson.dumps({'type': 'thinking_end'}).decode()}\n\n"

        # Emit citations if any were collected
        if citations and stream_interruption is None:
            yield f"data: {orjson.dumps({'type': 'web_search_citations', 'citations': citations}).decode()}\n\n"
    finally:
        # Closing an SSE consumer must not discard usage already received.
        billing_input_tokens = input_tokens
        billing_output_tokens = output_tokens
        if (
            billing_reservation_id
            and save_to_db
            and (content or tool_seen or input_tokens or output_tokens)
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

    if stream_interruption is not None:
        raise stream_interruption

    # Report exceptions only after accounting for the received output.
    if deferred_error_event is not None:
        yield deferred_error_event

    # Bill the received output before rejecting an unsafe or incomplete call.
    if tool_seen and save_to_db:
        tool_error = None
        if multiple_tool_calls:
            tool_error = "multiple_tool_calls_unsupported"
        elif (error_yielded or tool_stream_invalid or not response_completed or truncated
              or not function_name or not tool_call_id
              or (function_output_item or {}).get("status") not in {None, "completed"}
              or stop_signals.get(conversation_id, False)):
            tool_error = "tool_call_incomplete"
        else:
            try:
                parsed_args = orjson.loads(function_arguments)
                if not isinstance(parsed_args, dict):
                    tool_error = "invalid_tool_arguments"
            except (orjson.JSONDecodeError, TypeError):
                tool_error = "invalid_tool_arguments"
        if tool_error:
            if not error_yielded:
                await record_provider_error_for_label("xAI (Grok)", message=tool_error, model=model, byok=byok)
                yield f"data: {orjson.dumps({'error': tool_error, 'error_code': tool_error}).decode()}\n\n"
            return

        logger.info(f"[call_xai_responses_api] Tool call detected: {function_name}")
        logger.debug(f"[call_xai_responses_api] Tool call args: {parsed_args}")

        await record_provider_success_for_label("xAI (Grok)", model=model, byok=byok)
        if stop_signals.get(conversation_id, False):
            return
        yield f"data: {orjson.dumps({'tool_call': {'name': function_name, 'arguments': parsed_args, 'id': tool_call_id, 'response_output_items': response_output_items, '_billing_usage': {'input_tokens': billing_input_tokens, 'output_tokens': billing_output_tokens}}, 'pre_tool_content': content}).decode()}\n\n"
        yield f"data: {orjson.dumps({'tool_call_pending': True}).decode()}\n\n"
        return

    # Normal response - save to database
    if save_to_db:
        was_stopped = stop_signals.get(conversation_id, False)
        if not content.strip():
            if was_stopped:
                logger.info(f"User stopped stream before content for conversation {conversation_id}. Skipping save.")
            else:
                logger.warning(f"Empty bot response for conversation {conversation_id}, user {current_user.id}. "
                               f"Provider: xai_responses. Not saving to DB.")
                if not error_yielded:
                    await record_provider_error_for_label("xAI (Grok)", message="empty response", model=model, byok=byok)
                    empty_msg = "The AI returned an empty response. Please try again."
                    yield f"data: {orjson.dumps(_provider_error_payload('xAI (Grok)', empty_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            return
        else:
            if not error_yielded:
                await record_provider_success_for_label("xAI (Grok)", model=model, byok=byok)
            citations_data = orjson.dumps(citations).decode() if citations else None
            user_message_id, bot_message_id = await save_content_to_db(content, input_tokens, output_tokens, total_tokens, conversation_id, current_user.id, model, user_message=user_message,
                                                                        input_token_fallback=input_token_fallback,
                                                                        prompt_id=prompt_id, watchdog_config=watchdog_config, watchdog_hint_active=watchdog_hint_active, watchdog_hint_eval_id=watchdog_hint_eval_id,
                                                                        llm_id=llm_id, citations_json=citations_data, byok=byok, pending_attachment_refs=pending_attachment_refs,
                                                                        strip_device_action_blocks=strip_device_action_blocks,
                                                                        billing_reservation_id=billing_reservation_id,
                                                                        billing_only_accumulated_usage=bool(billing_reservation_id))
            persisted = persistence_result_payload(user_message_id, bot_message_id)
            yield f"data: {orjson.dumps(persisted).decode()}\n\n"
            if persisted.get("persistence_error"):
                return

        yield content.strip()
    else:
        if content.strip() and not error_yielded:
            await record_provider_success_for_label("xAI (Grok)", model=model, byok=byok)
        elif not error_yielded:
            await record_provider_error_for_label("xAI (Grok)", message="empty response", model=model, byok=byok)
            empty_msg = "The AI returned an empty response. Please try again."
            yield f"data: {orjson.dumps(_provider_error_payload('xAI (Grok)', empty_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            yield "data: [DONE]\n\n"
            return
        yield f"data: {orjson.dumps({'token_info': True, 'input_tokens': input_tokens, 'output_tokens': output_tokens}).decode()}\n\n"
        yield "data: [DONE]\n\n"
